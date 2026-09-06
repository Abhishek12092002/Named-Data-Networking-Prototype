"""Shared link-traversal timing model, used identically by the IP and NDN
data planes so that "same link, same delay" holds by construction.

Abstraction level (read this before changing anything here): this is a
**logical, event-driven simulator**, not a packet-accurate replacement for
ns-3 / ndnSIM / OMNeT++ / Mininet. Simulation time is a pure logical clock
that advances directly between scheduled events -- nothing in this module
ever calls ``time.sleep`` or otherwise waits in wall-clock time. A 10-second
simulated run can complete in milliseconds of real CPU time.

End-to-end latency emerges from four components per link traversal, per the
project's timing model:

    total_link_delay = propagation_delay + transmission_delay
                        + queueing_delay + processing_delay

- **propagation_delay**: ``Link.effective_propagation_delay_ms()`` --
  either a directly configured value or derived from distance / propagation
  speed. Depends only on the link, never on packet size or load.
- **transmission_delay** (a.k.a. serialization delay): ``size_bits /
  bandwidth_bps``. Depends on packet/message size and link bandwidth --
  NOT a fixed per-hop constant. A 4 KB NDN Data packet takes ~32x longer to
  transmit than a 200-byte Interest on the same link.
- **queueing_delay**: how long a packet actually waits behind previously
  scheduled traffic on this *directed* link before it can start
  transmitting. This emerges from the link's own occupancy state
  (``LinkQueue.busy_until_ms``), not from a fixed constant. An empty queue
  contributes ~0 ms; a busy link contributes more; a queue whose backlog
  would exceed ``queue_limit_packets`` worth of service time causes a drop
  instead of unbounded delay.
- **processing_delay**: ``Link.processing_delay_ms`` (link/interface-level)
  plus any protocol-specific processing the caller supplies (e.g. NDN
  PIT/FIB/Content-Store lookup, IP forwarding-table lookup). Kept separate
  from link processing because it has a different physical/logical source.

Independent random packet loss (``Link.loss_probability``) is a distinct
mechanism from deterministic link failures (``Link.active``) -- do not
conflate them. A link can be "up" and still drop a fraction of traffic;
a link that is "down" drops everything regardless of loss_probability.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

from ..topology import Link, Topology


@dataclass(frozen=True)
class LatencyBreakdown:
    """One link traversal's contribution to end-to-end latency, in ms."""

    propagation_ms: float = 0.0
    transmission_ms: float = 0.0
    queueing_ms: float = 0.0
    processing_ms: float = 0.0

    @property
    def total_ms(self) -> float:
        return self.propagation_ms + self.transmission_ms + self.queueing_ms + self.processing_ms

    def __add__(self, other: "LatencyBreakdown") -> "LatencyBreakdown":
        return LatencyBreakdown(
            self.propagation_ms + other.propagation_ms,
            self.transmission_ms + other.transmission_ms,
            self.queueing_ms + other.queueing_ms,
            self.processing_ms + other.processing_ms,
        )


ZERO_BREAKDOWN = LatencyBreakdown()


@dataclass(frozen=True)
class LinkTraversalResult:
    ok: bool
    arrival_ms: float
    breakdown: LatencyBreakdown
    dropped_reason: Optional[str] = None  # 'link_down' | 'random_loss' | 'queue_full'


class LinkQueue:
    """A single directed link's transmission queue.

    Models an M/D/1-like FIFO service discipline: at most one packet is
    "in service" (being transmitted) at a time; later packets queue behind
    it. ``busy_until_ms`` is the time the link becomes free again --
    equivalently, the departure time of the packet currently in service.
    Queueing delay for a new arrival is simply how far in the future
    ``busy_until_ms`` already is. If that backlog, expressed in units of
    this packet's own service time, would exceed the link's configured
    ``queue_limit_packets``, the packet is dropped instead of queued
    (a bounded/finite queue, per the project's queueing requirements).
    """

    def __init__(self) -> None:
        self.busy_until_ms: float = 0.0

    def occupancy_estimate(self, now_ms: float, service_time_ms: float) -> float:
        """Approximate number of packets' worth of backlog ahead of a new arrival."""
        wait_ms = max(0.0, self.busy_until_ms - now_ms)
        return wait_ms / max(service_time_ms, 1e-9)

    def schedule(self, now_ms: float, service_time_ms: float, capacity_packets: int) -> Tuple[float, bool]:
        """Reserve link time for a packet needing ``service_time_ms``.

        Returns (queueing_delay_ms, accepted). If accepted, updates
        ``busy_until_ms`` to reflect the new departure time.
        """
        wait_ms = max(0.0, self.busy_until_ms - now_ms)
        if wait_ms > capacity_packets * max(service_time_ms, 1e-9):
            return wait_ms, False
        start = max(now_ms, self.busy_until_ms)
        self.busy_until_ms = start + service_time_ms
        queueing_delay_ms = start - now_ms
        return queueing_delay_ms, True


class LinkTimingModel:
    """Owns one :class:`LinkQueue` per directed edge in a topology and
    computes full link-traversal timing (propagation + transmission +
    queueing + processing) for both the IP and NDN data planes.

    This replaces per-engine ad hoc delay formulas so "same link, same
    delay model" holds for every architecture by construction.
    """

    def __init__(self, topo: Topology, rnd: Optional[random.Random] = None):
        self._queues: Dict[Tuple[int, int], LinkQueue] = {edge: LinkQueue() for edge in topo.links}
        self._rnd = rnd  # None disables random loss (deterministic runs / tests)

    def traverse(
        self,
        topo: Topology,
        u: int,
        v: int,
        now_ms: float,
        size_bytes: int,
        protocol_processing_ms: float = 0.0,
    ) -> LinkTraversalResult:
        link: Link = topo.links[(u, v)]

        if not link.active:
            return LinkTraversalResult(False, now_ms, ZERO_BREAKDOWN, "link_down")

        if self._rnd is not None and link.loss_probability > 0.0:
            if self._rnd.random() < link.loss_probability:
                return LinkTraversalResult(False, now_ms, ZERO_BREAKDOWN, "random_loss")

        transmission_ms = (size_bytes * 8.0) / max(link.bandwidth_mbps * 1_000_000.0, 1e-9) * 1000.0
        transmission_ms = max(transmission_ms, 1e-6)

        queue = self._queues.setdefault((u, v), LinkQueue())
        queueing_ms, accepted = queue.schedule(now_ms, transmission_ms, link.queue_limit_packets)
        if not accepted:
            return LinkTraversalResult(False, now_ms, ZERO_BREAKDOWN, "queue_full")

        propagation_ms = link.effective_propagation_delay_ms()
        processing_ms = link.processing_delay_ms + max(0.0, protocol_processing_ms)

        breakdown = LatencyBreakdown(
            propagation_ms=propagation_ms,
            transmission_ms=transmission_ms,
            queueing_ms=queueing_ms,
            processing_ms=processing_ms,
        )
        arrival_ms = now_ms + breakdown.total_ms
        return LinkTraversalResult(True, arrival_ms, breakdown, None)
