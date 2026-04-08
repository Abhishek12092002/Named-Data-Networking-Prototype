from __future__ import annotations

import heapq
import itertools
import random
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Set, Tuple

from .cache import make_cache
from .sim_models import Data, NodeState, PITEntry, RunStats, longest_prefix_match
from .topology import Topology, shortest_path, shortest_path_next_hop
from .workload import RequestSpec


@dataclass(frozen=True)
class NDNConfig:
    cache_size: int
    cache_policy: str
    producers: Dict[str, int]
    forwarding: str = 'best-route'
    link_delay_ms: float = 10.0
    processing_delay_ms: float = 1.0
    interest_iat_ms: float = 1.0
    interest_lifetime_ms: float = 250.0
    max_retransmissions: int = 1
    pit_capacity: int = 256
    cache_admit_threshold: int = 1
    cache_ttl_ms: float | None = None
    interest_size_bytes: int = 64
    data_size_bytes: int = 2048
    verify_data: bool = False
    verification_delay_ms: float = 0.5
    invalid_data_rate: float = 0.0


@dataclass(frozen=True)
class FailureEventSpec:
    time_ms: float
    edge: Tuple[int, int]
    active: bool


@dataclass(frozen=True)
class InterestEvent:
    req_id: int
    name: str
    at: int
    prev: int
    requester: int
    hops: int


@dataclass(frozen=True)
class DataEvent:
    req_id: int
    name: str
    at: int
    prev: int
    requester: int
    hops: int
    from_cache: bool
    verified: bool


@dataclass(frozen=True)
class TimeoutEvent:
    req_id: int
    name: str
    consumer: int
    attempt: int


@dataclass(frozen=True)
class LinkToggleEvent:
    edge: Tuple[int, int]
    active: bool


@dataclass
class RequestState:
    consumer: int
    name: str
    start_ms: float
    completed: bool = False
    attempts: int = 0
    current_attempt: int = 0
    timeout_happened: bool = False


class LinkScheduler:
    def __init__(self, topo: Topology):
        self.next_free_ms: Dict[Tuple[int, int], float] = {edge: 0.0 for edge in topo.links}

    def schedule(self, topo: Topology, u: int, v: int, now_ms: float, size_bytes: int) -> Tuple[float, bool]:
        link = topo.links[(u, v)]
        if not link.active:
            return now_ms, False
        serialization_ms = (size_bytes * 8.0) / max(link.bandwidth_mbps, 1e-9) / 1000.0
        service_time = max(serialization_ms, 0.01)
        wait_ms = max(0.0, self.next_free_ms[(u, v)] - now_ms)
        if wait_ms > link.queue_limit_packets * service_time:
            return now_ms, False
        start = max(now_ms, self.next_free_ms[(u, v)])
        arrival = start + link.propagation_delay_ms + service_time
        self.next_free_ms[(u, v)] = start + service_time
        return arrival, True


def _find_owner(cfg: NDNConfig, name: str) -> int:
    best = ''
    owner = -1
    for prefix, node in cfg.producers.items():
        if name.startswith(prefix.rstrip('/') + '/') or name == prefix.rstrip('/'):
            if len(prefix) > len(best):
                best = prefix
                owner = node
    if owner == -1:
        raise RuntimeError(f'no producer configured for {name}')
    return owner


def build_ndn_nodes(topo: Topology, cfg: NDNConfig, seed: int = 0) -> Dict[int, NodeState]:
    nodes: Dict[int, NodeState] = {}
    for i in range(topo.n):
        nodes[i] = NodeState(
            cs=make_cache(cfg.cache_policy, cfg.cache_size, admit_threshold=cfg.cache_admit_threshold, ttl_ms=cfg.cache_ttl_ms, seed=seed + i),
            pit={},
            fib={},
        )
    _rebuild_fibs(nodes, topo, cfg)
    return nodes


def _rebuild_fibs(nodes: Dict[int, NodeState], topo: Topology, cfg: NDNConfig) -> None:
    for st in nodes.values():
        st.fib.clear()
    for prefix, producer in cfg.producers.items():
        p = prefix.rstrip('/') + '/'
        for node in nodes:
            if node == producer:
                continue
            try:
                path = shortest_path(topo, node, producer, active_only=True)
                nodes[node].fib[p] = [path[1]]
            except ValueError:
                continue
            if cfg.forwarding in {'flooding', 'probabilistic'}:
                alt = [nbr for nbr in topo.neighbors(node) if topo.is_active(node, nbr)]
                nodes[node].fib[p] = sorted(set(alt))


def ndn_fetch_event_driven(
    topo: Topology,
    cfg: NDNConfig,
    requests: List[RequestSpec],
    payload_db: Dict[str, bytes],
    seed: int = 0,
    failures: List[FailureEventSpec] | None = None,
) -> RunStats:
    rnd = random.Random(seed)
    stats = RunStats(requests=len(requests))
    nodes = build_ndn_nodes(topo, cfg, seed=seed)
    q: List[Tuple[float, int, str, object]] = []
    seq = itertools.count()
    link_sched = LinkScheduler(topo)
    request_states: Dict[int, RequestState] = {rid: RequestState(r.consumer, r.name, r.start_ms) for rid, r in enumerate(requests)}

    def push(time_ms: float, kind: str, ev: object) -> None:
        heapq.heappush(q, (time_ms, next(seq), kind, ev))

    for rid, req in enumerate(requests):
        push(req.start_ms, 'interest', InterestEvent(rid, req.name, req.consumer, -1, req.consumer, 0))
        push(req.start_ms + cfg.interest_lifetime_ms, 'timeout', TimeoutEvent(rid, req.name, req.consumer, 0))
    for fs in failures or []:
        push(fs.time_ms, 'toggle', LinkToggleEvent(fs.edge, fs.active))

    def choose_next_hops(node: int, name: str) -> List[int]:
        fib_hops = list(longest_prefix_match(nodes[node].fib, name))
        active = [nh for nh in fib_hops if topo.is_active(node, nh)]
        if not active:
            return []
        if cfg.forwarding == 'best-route':
            return active[:1]
        if cfg.forwarding == 'flooding':
            return active
        if cfg.forwarding == 'probabilistic':
            return [rnd.choice(active)]
        raise ValueError(f'unknown forwarding strategy {cfg.forwarding}')

    def forward_interest(now_ms: float, u: int, v: int, ev: InterestEvent) -> None:
        arrival, ok = link_sched.schedule(topo, u, v, now_ms + cfg.processing_delay_ms, cfg.interest_size_bytes)
        nm = stats.ensure_node(u)
        nm.forwarded_interests += 1
        if not ok:
            stats.queue_drops += 1
            stats.dropped_interests += 1
            return
        stats.interest_tx += 1
        push(arrival, 'interest', InterestEvent(ev.req_id, ev.name, v, u, ev.requester, ev.hops + 1))

    def forward_data(now_ms: float, u: int, v: int, ev: DataEvent) -> None:
        extra = cfg.verification_delay_ms if cfg.verify_data else 0.0
        arrival, ok = link_sched.schedule(topo, u, v, now_ms + cfg.processing_delay_ms + extra, cfg.data_size_bytes)
        nm = stats.ensure_node(u)
        nm.forwarded_data += 1
        if not ok:
            stats.queue_drops += 1
            stats.dropped_data += 1
            return
        stats.data_tx += 1
        push(arrival, 'data', DataEvent(ev.req_id, ev.name, v, u, ev.requester, ev.hops + 1, ev.from_cache, ev.verified))

    while q:
        time_ms, _, kind, payload = heapq.heappop(q)

        if kind == 'toggle':
            ev = payload
            assert isinstance(ev, LinkToggleEvent)
            topo.set_link_active(ev.edge[0], ev.edge[1], ev.active)
            _rebuild_fibs(nodes, topo, cfg)
            continue

        if kind == 'timeout':
            ev = payload
            assert isinstance(ev, TimeoutEvent)
            rs = request_states[ev.req_id]
            if rs.completed or ev.attempt != rs.current_attempt:
                continue
            if rs.attempts < cfg.max_retransmissions:
                rs.attempts += 1
                rs.current_attempt += 1
                rs.timeout_happened = True
                stats.retransmissions += 1
                push(time_ms, 'interest', InterestEvent(ev.req_id, rs.name, rs.consumer, -1, rs.consumer, 0))
                push(time_ms + cfg.interest_lifetime_ms, 'timeout', TimeoutEvent(ev.req_id, rs.name, rs.consumer, rs.current_attempt))
            else:
                rs.completed = True
                stats.timed_out_requests += 1
            continue

        if kind == 'interest':
            ev = payload
            assert isinstance(ev, InterestEvent)
            rs = request_states[ev.req_id]
            if rs.completed:
                continue
            u = ev.at
            st = nodes[u]
            node_metrics = stats.ensure_node(u)
            expired = [k for k, entry in st.pit.items() if entry.expiry_ms <= time_ms]
            for k in expired:
                st.pit.pop(k, None)

            cached = st.cs.get(ev.name, now_ms=time_ms)
            if cached is not None:
                node_metrics.cache_hits += 1
                stats.cache_hits += 1
                verified = rnd.random() >= cfg.invalid_data_rate
                if ev.prev == -1:
                    rs.completed = True
                    stats.completed_requests += 1
                    stats.total_latency_ms += max(0.0, time_ms - rs.start_ms)
                    stats.total_latency_hops += ev.hops
                    stats.per_consumer_latency_ms.setdefault(rs.consumer, []).append(max(0.0, time_ms - rs.start_ms))
                else:
                    forward_data(time_ms, u, ev.prev, DataEvent(ev.req_id, ev.name, u, ev.prev, ev.requester, 0, True, verified))
                continue
            node_metrics.cache_misses += 1
            stats.cache_misses += 1

            owner = _find_owner(cfg, ev.name)
            if u == owner:
                stats.producer_serves += 1
                verified = rnd.random() >= cfg.invalid_data_rate
                if ev.prev == -1:
                    rs.completed = True
                    stats.completed_requests += 1
                    stats.total_latency_ms += max(0.0, time_ms - rs.start_ms)
                    stats.total_latency_hops += ev.hops
                    stats.per_consumer_latency_ms.setdefault(rs.consumer, []).append(max(0.0, time_ms - rs.start_ms))
                else:
                    forward_data(time_ms, u, ev.prev, DataEvent(ev.req_id, ev.name, u, ev.prev, ev.requester, 0, False, verified))
                continue

            if ev.name in st.pit:
                st.pit[ev.name].faces.add(ev.prev)
                st.pit[ev.name].requester_ids.add(ev.req_id)
                st.pit[ev.name].expiry_ms = max(st.pit[ev.name].expiry_ms, time_ms + cfg.interest_lifetime_ms)
                node_metrics.pit_peak = max(node_metrics.pit_peak, len(st.pit))
                continue

            if len(st.pit) >= cfg.pit_capacity:
                stats.pit_overflow_drops += 1
                stats.dropped_interests += 1
                continue

            next_hops = choose_next_hops(u, ev.name)
            if not next_hops:
                stats.dropped_interests += 1
                continue
            st.pit[ev.name] = PITEntry(faces={ev.prev}, expiry_ms=time_ms + cfg.interest_lifetime_ms, created_ms=time_ms, requester_ids={ev.req_id})
            node_metrics.pit_peak = max(node_metrics.pit_peak, len(st.pit))
            for nh in next_hops:
                if nh == ev.prev and len(next_hops) > 1:
                    continue
                forward_interest(time_ms, u, nh, ev)
            continue

        if kind == 'data':
            ev = payload
            assert isinstance(ev, DataEvent)
            rs = request_states[ev.req_id]
            if rs.completed:
                continue
            u = ev.at
            st = nodes[u]
            node_metrics = stats.ensure_node(u)
            if cfg.verify_data and not ev.verified:
                stats.verification_failures += 1
                continue
            st.cs.put(ev.name, payload_db[ev.name], now_ms=time_ms)
            node_metrics.cache_size_peak = max(node_metrics.cache_size_peak, st.cs.size())

            if u == rs.consumer and ev.prev == -1:
                rs.completed = True
                stats.completed_requests += 1
                stats.total_latency_ms += max(0.0, time_ms - rs.start_ms)
                stats.total_latency_hops += ev.hops
                stats.per_consumer_latency_ms.setdefault(rs.consumer, []).append(max(0.0, time_ms - rs.start_ms))
                continue

            pit_entry = st.pit.pop(ev.name, None)
            if pit_entry is None:
                # Try to steer directly to consumer if this is the consumer itself.
                if u == rs.consumer:
                    rs.completed = True
                    stats.completed_requests += 1
                    stats.total_latency_ms += max(0.0, time_ms - rs.start_ms)
                    stats.total_latency_hops += ev.hops
                    stats.per_consumer_latency_ms.setdefault(rs.consumer, []).append(max(0.0, time_ms - rs.start_ms))
                else:
                    stats.dropped_data += 1
                continue
            for face in pit_entry.faces:
                if face == -1:
                    if u == rs.consumer:
                        rs.completed = True
                        stats.completed_requests += 1
                        stats.total_latency_ms += max(0.0, time_ms - rs.start_ms)
                        stats.total_latency_hops += ev.hops
                        stats.per_consumer_latency_ms.setdefault(rs.consumer, []).append(max(0.0, time_ms - rs.start_ms))
                    else:
                        stats.dropped_data += 1
                else:
                    forward_data(time_ms, u, face, ev)
            continue

        raise RuntimeError(f'unknown event kind: {kind}')

    return stats
