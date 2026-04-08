from __future__ import annotations

import heapq
import itertools
from dataclasses import dataclass
from typing import Dict, List, Tuple

from .sim_models import RunStats
from .topology import Topology
from .workload import RequestSpec
from .ndn_engine import LinkScheduler, FailureEventSpec, _find_owner


@dataclass(frozen=True)
class IPConfig:
    producers: Dict[str, int]
    link_delay_ms: float = 10.0
    processing_delay_ms: float = 1.0
    data_size_bytes: int = 2048
    request_size_bytes: int = 64


def ip_fetch_event_driven(
    topo: Topology,
    cfg: IPConfig,
    requests: List[RequestSpec],
    payload_db: Dict[str, bytes],
    failures: List[FailureEventSpec] | None = None,
) -> RunStats:
    stats = RunStats(requests=len(requests))
    link_sched = LinkScheduler(topo)
    q: List[Tuple[float, int, str, object]] = []
    seq = itertools.count()
    starts = {i: req.start_ms for i, req in enumerate(requests)}
    completed = {i: False for i in range(len(requests))}

    def push(t: float, kind: str, ev: object) -> None:
        heapq.heappush(q, (t, next(seq), kind, ev))

    for i, req in enumerate(requests):
        push(req.start_ms, 'request', (i, req.consumer, req.name))
    for fs in failures or []:
        push(fs.time_ms, 'toggle', fs)

    while q:
        time_ms, _, kind, payload = heapq.heappop(q)
        if kind == 'toggle':
            fs = payload
            assert isinstance(fs, FailureEventSpec)
            topo.set_link_active(fs.edge[0], fs.edge[1], fs.active)
            continue
        if kind == 'request':
            rid, consumer, name = payload
            owner = _find_owner(type('Cfg', (), {'producers': cfg.producers})(), name)
            try:
                from .topology import shortest_path
                path = shortest_path(topo, consumer, owner, active_only=True)
            except Exception:
                stats.timed_out_requests += 1
                continue
            now = time_ms
            hops = 0
            for u, v in zip(path[:-1], path[1:]):
                arrival, ok = link_sched.schedule(topo, u, v, now + cfg.processing_delay_ms, cfg.request_size_bytes)
                if not ok:
                    stats.queue_drops += 1
                    stats.dropped_interests += 1
                    break
                stats.interest_tx += 1
                now = arrival
                hops += 1
            else:
                rev = list(reversed(path))
                for u, v in zip(rev[:-1], rev[1:]):
                    arrival, ok = link_sched.schedule(topo, u, v, now + cfg.processing_delay_ms, cfg.data_size_bytes)
                    if not ok:
                        stats.queue_drops += 1
                        stats.dropped_data += 1
                        break
                    stats.data_tx += 1
                    now = arrival
                    hops += 1
                else:
                    stats.completed_requests += 1
                    stats.total_latency_ms += max(0.0, now - starts[rid])
                    stats.total_latency_hops += hops
                    stats.cache_misses += 1
                    stats.per_consumer_latency_ms.setdefault(consumer, []).append(max(0.0, now - starts[rid]))
    return stats
