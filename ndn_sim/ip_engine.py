from __future__ import annotations

import heapq
import itertools
import random
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

from .controller import SDNController
from .sim_models import RunStats
from .topology import Topology, shortest_path_next_hop
from .workload import RequestSpec
from .ndn_engine import LinkScheduler, FailureEventSpec, EventSink, _find_owner


@dataclass(frozen=True)
class IPConfig:
    """IP data-plane configuration.

    ``forwarding`` selects the IP *control plane*: ``'distributed'`` (every
    node's routing table is rebuilt locally/instantly whenever the topology
    changes -- the classical "IP" baseline) or ``'sdn-centralized'`` (a
    logically-centralized :class:`~ndn_sim.controller.SDNController` computes
    globally-optimal routes and pushes selective updates after a configurable
    control round-trip time -- this is what "IP + SDN" means in this
    simulator). This mirrors NDN's ``forwarding`` field exactly so the two
    data planes can be compared under an identical control-plane switch.
    """

    producers: Dict[str, int]
    link_delay_ms: float = 10.0
    processing_delay_ms: float = 1.0
    data_size_bytes: int = 1500      # representative MTU-sized IP packet (see core/network_profiles.py)
    request_size_bytes: int = 64     # small request/ACK-style packet
    forwarding: str = 'distributed'
    controller_rtt_ms: float = 5.0


@dataclass(frozen=True)
class IPPacketEvent:
    """One in-flight IP packet, forwarded hop by hop via each node's own
    (possibly stale) routing table -- the IP-shaped counterpart of NDN's
    ``InterestEvent``/``DataEvent``. ``phase`` distinguishes the
    request-direction leg (consumer -> destination/owner) from the
    response-direction leg (owner -> consumer); a real IP packet doesn't
    know it's part of a "flow," but tracking phase here lets us report
    request/response latency in NDN-comparable terms without pretending
    IP has Interest/Data semantics.
    """

    req_id: int
    dst: int
    at: int
    prev: int
    hops: int
    phase: str  # 'request' | 'response'
    origin_consumer: int


def _build_ip_routes_distributed(topo: Topology, destinations: Iterable[int]) -> Dict[int, Dict[int, int]]:
    """Per-node routing table (dst_node -> next_hop), computed locally via
    hop-count BFS shortest path -- the IP analogue of ``ndn_engine._rebuild_fibs``.
    Instant/local: every node "knows" the current best route the moment the
    topology changes, with no controller round-trip. This is the baseline
    a centralized SDN controller is compared against.
    """
    tables: Dict[int, Dict[int, int]] = {i: {} for i in range(topo.n)}
    for dst in destinations:
        for node in range(topo.n):
            if node == dst:
                continue
            try:
                tables[node][dst] = shortest_path_next_hop(topo, node, dst, active_only=True)
            except ValueError:
                continue
    return tables


def ip_fetch_event_driven(
    topo: Topology,
    cfg: IPConfig,
    requests: List[RequestSpec],
    payload_db: Dict[str, bytes],
    failures: List[FailureEventSpec] | None = None,
    seed: int = 0,
    on_event: Optional[EventSink] = None,
) -> RunStats:
    """Event-driven IP simulation: per-hop forwarding via a persistent,
    per-node routing table (not a fresh shortest-path recompute per
    packet). The routing table is owned by either a distributed control
    plane (instant, local recompute on topology change) or a centralized
    SDN controller (globally-optimal, but nodes keep forwarding on the
    *stale* table until the control-RTT elapses) -- selected by
    ``cfg.forwarding``. This mirrors ``ndn_engine.ndn_fetch_event_driven``'s
    FIB-apply structure so IP and NDN are controlled the same way, which is
    what makes "IP vs IP+SDN" and "NDN vs NDN+SDN" fair, structurally
    parallel comparisons rather than two different simulators.

    ``on_event`` is the same opt-in instrumentation hook as
    ``ndn_fetch_event_driven`` -- see that function's docstring. No-op and
    zero cost when None (the default).
    """
    stats = RunStats(requests=len(requests))
    rnd = random.Random(seed)
    link_sched = LinkScheduler(topo, rnd=rnd)

    def _emit(kind: str, **fields) -> None:
        if on_event is not None:
            on_event({'t': fields.pop('t', None), 'kind': kind, 'proto': 'ip', **fields})

    # Routes are needed toward every producer (request leg) AND toward
    # every consumer that appears in this run's traffic (response leg) --
    # an IP routing table is destination-keyed and has to cover both
    # directions of a request/response exchange, unlike the earlier
    # per-request path computation which got the reverse leg "for free"
    # by just reversing the forward path.
    destinations = sorted(set(cfg.producers.values()) | {r.consumer for r in requests})
    controller = SDNController(topo, control_rtt_ms=cfg.controller_rtt_ms) if cfg.forwarding == 'sdn-centralized' else None

    if controller is not None:
        # Initial provisioning cost -- see the matching comment in
        # ndn_engine.build_ndn_nodes for why this counts as control-plane
        # messages even though it isn't a "reconvergence."
        node_routes = controller.compute_routes(destinations)
        controller._last_routes = {n: dict(t) for n, t in node_routes.items()}
        controller.stats.control_messages_tx += sum(1 for t in node_routes.values() if t)
    else:
        node_routes = _build_ip_routes_distributed(topo, destinations)

    q: List[Tuple[float, int, str, object]] = []
    seq = itertools.count()
    starts = {i: req.start_ms for i, req in enumerate(requests)}

    def push(t: float, kind: str, ev: object) -> None:
        heapq.heappush(q, (t, next(seq), kind, ev))

    for i, req in enumerate(requests):
        owner = _find_owner(type('Cfg', (), {'producers': cfg.producers})(), req.name)
        push(req.start_ms, 'packet', IPPacketEvent(i, owner, req.consumer, -1, 0, 'request', req.consumer))
    for fs in failures or []:
        push(fs.time_ms, 'toggle', fs)

    while q:
        time_ms, _, kind, payload = heapq.heappop(q)

        if kind == 'toggle':
            fs = payload
            assert isinstance(fs, FailureEventSpec)
            topo.set_link_active(fs.edge[0], fs.edge[1], fs.active)
            _emit('link_up' if fs.active else 'link_down', t=time_ms, u=fs.edge[0], v=fs.edge[1])
            if controller is not None:
                new_routes, delay = controller.push_routes(destinations)
                push(time_ms + delay, 'routes_apply', new_routes)
            else:
                node_routes = _build_ip_routes_distributed(topo, destinations)
            continue

        if kind == 'routes_apply':
            node_routes = payload
            _emit('control_update', t=time_ms, op='route_install', targets=len(node_routes))
            continue

        if kind == 'packet':
            ev = payload
            assert isinstance(ev, IPPacketEvent)
            rid = ev.req_id
            _emit('rx', t=time_ms, u=ev.at, ptype='ip_packet', dst=ev.dst, req_id=rid, phase=ev.phase)

            if ev.at == ev.dst:
                if ev.phase == 'request':
                    # Reached the owning producer: turn around as a response.
                    push(time_ms, 'packet', IPPacketEvent(rid, ev.origin_consumer, ev.at, -1, ev.hops, 'response', ev.origin_consumer))
                else:
                    stats.completed_requests += 1
                    stats.total_latency_ms += max(0.0, time_ms - starts[rid])
                    stats.total_latency_hops += ev.hops
                    stats.cache_misses += 1  # kept for CSV-column parity with NDN's miss counter; IP has no cache
                    stats.per_consumer_latency_ms.setdefault(ev.origin_consumer, []).append(max(0.0, time_ms - starts[rid]))
                continue

            table = node_routes.get(ev.at, {})
            next_hop = table.get(ev.dst)
            if next_hop is None:
                # No route to destination in the current table (e.g. a
                # genuinely partitioned network, or a not-yet-provisioned
                # SDN table). Distinct from a live-but-down link, which the
                # link scheduler below reports as 'link_down'.
                if ev.phase == 'request':
                    stats.dropped_interests += 1
                else:
                    stats.dropped_data += 1
                continue

            size_bytes = cfg.request_size_bytes if ev.phase == 'request' else cfg.data_size_bytes
            result = link_sched.traverse(topo, ev.at, next_hop, time_ms, size_bytes,
                                          protocol_processing_ms=cfg.processing_delay_ms)
            if not result.ok:
                if result.dropped_reason == 'random_loss':
                    stats.random_loss_drops += 1
                else:
                    stats.queue_drops += 1
                if ev.phase == 'request':
                    stats.dropped_interests += 1
                else:
                    stats.dropped_data += 1
                _emit('drop', t=time_ms, u=ev.at, v=next_hop, ptype='ip_packet', dst=ev.dst, req_id=rid,
                      phase=ev.phase, reason=result.dropped_reason or 'queue_full')
                continue

            if ev.phase == 'request':
                stats.interest_tx += 1
            else:
                stats.data_tx += 1
            stats.record_traversal(result.breakdown)
            _emit('tx', t=time_ms, u=ev.at, v=next_hop, ptype='ip_packet', dst=ev.dst, req_id=rid,
                  phase=ev.phase, arrival_t=result.arrival_ms, size=size_bytes, ttl=64 - ev.hops)
            push(result.arrival_ms, 'packet',
                 IPPacketEvent(rid, ev.dst, next_hop, ev.at, ev.hops + 1, ev.phase, ev.origin_consumer))
            continue

        raise RuntimeError(f'unknown event kind: {kind}')

    if controller is not None:
        stats.control_messages_tx = controller.stats.control_messages_tx
        stats.reconvergence_events = controller.stats.reconvergence_events
        stats.total_reconvergence_ms = controller.stats.total_reconvergence_ms

    return stats
