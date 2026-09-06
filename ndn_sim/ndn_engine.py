from __future__ import annotations

import heapq
import itertools
import random
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .cache import make_cache
from .controller import SDNController
from .core.timing import LinkTimingModel, LinkTraversalResult
from .sim_models import Data, NodeState, PITEntry, RunStats, longest_prefix_match
from .topology import Topology, shortest_path, shortest_path_next_hop
from .workload import RequestSpec

# Type of the optional event-instrumentation callback (see
# ndn_fetch_event_driven's `on_event` parameter docstring below).
EventSink = Callable[[dict], None]


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
    interest_size_bytes: int = 200   # representative NDN Interest size (see core/network_profiles.py)
    data_size_bytes: int = 4096      # representative NDN Data payload size
    verify_data: bool = False
    verification_delay_ms: float = 0.5
    invalid_data_rate: float = 0.0
    # --- SDN controller extension ---
    controller_rtt_ms: float = 5.0
    sdn_cache_top_k: int = 0
    sdn_cache_placement: str = 'near-producer'
    sdn_cache_learn_fraction: float = 0.3


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


class LinkScheduler(LinkTimingModel):
    """Thin, backward-compatible name for :class:`core.timing.LinkTimingModel`.

    Historically this class lived here with an ad hoc propagation+
    serialization-only formula. The full propagation/transmission/queueing
    /processing model now lives in ``core.timing`` so the IP and NDN data
    planes share exactly one implementation; this subclass just keeps the
    familiar import path (``from .ndn_engine import LinkScheduler``) working
    for ``ip_engine.py`` and any external callers.

    ``schedule()`` is kept for legacy callers that only want
    ``(arrival_ms, ok)``. New code should call
    :meth:`core.timing.LinkTimingModel.traverse` directly to also get the
    latency breakdown and drop reason.
    """

    def schedule(self, topo: Topology, u: int, v: int, now_ms: float, size_bytes: int) -> Tuple[float, bool]:
        result = self.traverse(topo, u, v, now_ms, size_bytes)
        return result.arrival_ms, result.ok


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


def build_ndn_nodes(
    topo: Topology,
    cfg: NDNConfig,
    seed: int = 0,
    controller: 'SDNController | None' = None,
    cache_directives: Dict[int, Set[str]] | None = None,
) -> Dict[int, NodeState]:
    nodes: Dict[int, NodeState] = {}
    cache_directives = cache_directives or {}
    for i in range(topo.n):
        nodes[i] = NodeState(
            cs=make_cache(cfg.cache_policy, cfg.cache_size, admit_threshold=cfg.cache_admit_threshold, ttl_ms=cfg.cache_ttl_ms, seed=seed + i),
            pit={},
            fib={},
        )
        if cfg.cache_policy == 'sdn-coordinated' and i in cache_directives:
            nodes[i].cs.set_allowed(cache_directives[i])  # type: ignore[attr-defined]

    if cfg.forwarding == 'sdn-centralized' and controller is not None:
        # Initial provisioning: controller pushes globally-optimal FIBs before
        # traffic starts. This is NOT counted as "reconvergence" (that metric
        # is reserved for FIB updates triggered by topology changes after the
        # run starts) but it IS counted as control-plane message cost: a real
        # SDN controller has to install a FIB on every node before it can
        # forward anything, and that bootstrap cost is part of what
        # "centralized control" actually costs -- it should not be invisible
        # in the metrics just because it happens before t=0's first packet.
        new_fibs = controller.compute_fibs(cfg.producers)
        for node_id, fib in new_fibs.items():
            nodes[node_id].fib = fib
        controller._last_fibs = {node_id: dict(fib) for node_id, fib in new_fibs.items()}
        controller.stats.control_messages_tx += sum(1 for fib in new_fibs.values() if fib)
    else:
        _rebuild_fibs(nodes, topo, cfg)
    return nodes


def _choose_placement_nodes(topo: Topology, cfg: NDNConfig, requests: Sequence[RequestSpec]) -> List[int]:
    """Pick which nodes the controller is allowed to place cached content on."""
    mode = cfg.sdn_cache_placement.strip().lower()
    producer_nodes = sorted(set(cfg.producers.values()))
    consumer_nodes = sorted({r.consumer for r in requests})
    if mode == 'near-consumer':
        candidates = consumer_nodes
    elif mode == 'hybrid':
        candidates = sorted(set(producer_nodes) | set(consumer_nodes))
    else:  # 'near-producer' (default)
        candidates = producer_nodes
    return candidates or list(range(topo.n))


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
    on_event: Optional[EventSink] = None,
) -> RunStats:
    """Run the NDN simulation to completion.

    ``on_event``, when provided, is called synchronously for a real subset
    of simulation events as they occur -- link-level tx/rx of Interests and
    Data, cache hit/miss, producer serve, link failure/recovery, and SDN
    FIB installation. This is opt-in, additive instrumentation for the
    dashboard's packet-trace visualization (see webapp/jobs.py); it does
    NOT change simulation semantics, timing, or return value, and costs
    nothing when ``on_event`` is None (the default, used by every existing
    caller -- the CLI, the sweep runner, and all pre-existing tests).
    Each event is a small dict; see the individual ``_emit(...)`` calls
    below for the exact schema per event kind.
    """
    rnd = random.Random(seed)
    stats = RunStats(requests=len(requests))

    def _emit(kind: str, **fields) -> None:
        if on_event is not None:
            on_event({'t': fields.pop('t', None), 'kind': kind, 'proto': 'ndn', **fields})

    controller = SDNController(topo, control_rtt_ms=cfg.controller_rtt_ms) if cfg.forwarding == 'sdn-centralized' else None

    cache_directives: Dict[int, Set[str]] = {}
    if cfg.cache_policy == 'sdn-coordinated' and cfg.sdn_cache_top_k > 0 and requests:
        # The controller "learns" popularity from a leading training window of
        # traffic before pushing fixed placement directives for the rest of
        # the run -- mirrors how a real SDN traffic-engineering controller
        # would operate on observed flow statistics rather than an oracle.
        ordered = sorted(requests, key=lambda r: r.start_ms)
        learn_n = max(1, int(len(ordered) * cfg.sdn_cache_learn_fraction))
        popularity = Counter(r.name for r in ordered[:learn_n])
        placement_nodes = _choose_placement_nodes(topo, cfg, requests)
        placement_controller = controller or SDNController(topo, control_rtt_ms=cfg.controller_rtt_ms)
        cache_directives = placement_controller.coordinate_cache_placement(popularity, cfg.sdn_cache_top_k, placement_nodes)
        stats.cache_directives_pushed = placement_controller.stats.cache_directives_pushed

    nodes = build_ndn_nodes(topo, cfg, seed=seed, controller=controller, cache_directives=cache_directives)
    q: List[Tuple[float, int, str, object]] = []
    seq = itertools.count()
    link_sched = LinkScheduler(topo, rnd=rnd)
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
        if cfg.forwarding in ('best-route', 'sdn-centralized'):
            return active[:1]
        if cfg.forwarding == 'flooding':
            return active
        if cfg.forwarding == 'probabilistic':
            return [rnd.choice(active)]
        raise ValueError(f'unknown forwarding strategy {cfg.forwarding}')

    def forward_interest(now_ms: float, u: int, v: int, ev: InterestEvent) -> None:
        result = link_sched.traverse(topo, u, v, now_ms, cfg.interest_size_bytes,
                                      protocol_processing_ms=cfg.processing_delay_ms)
        nm = stats.ensure_node(u)
        nm.forwarded_interests += 1
        if not result.ok:
            if result.dropped_reason == 'random_loss':
                stats.random_loss_drops += 1
            else:
                stats.queue_drops += 1
            stats.dropped_interests += 1
            _emit('drop', t=now_ms, u=u, v=v, ptype='interest', name=ev.name, req_id=ev.req_id,
                  reason=result.dropped_reason or 'queue_full')
            return
        stats.interest_tx += 1
        stats.record_traversal(result.breakdown)
        _emit('tx', t=now_ms, u=u, v=v, ptype='interest', name=ev.name, req_id=ev.req_id,
              arrival_t=result.arrival_ms, size=cfg.interest_size_bytes)
        push(result.arrival_ms, 'interest', InterestEvent(ev.req_id, ev.name, v, u, ev.requester, ev.hops + 1))

    def forward_data(now_ms: float, u: int, v: int, ev: DataEvent) -> None:
        extra = cfg.verification_delay_ms if cfg.verify_data else 0.0
        result = link_sched.traverse(topo, u, v, now_ms, cfg.data_size_bytes,
                                      protocol_processing_ms=cfg.processing_delay_ms + extra)
        nm = stats.ensure_node(u)
        nm.forwarded_data += 1
        if not result.ok:
            if result.dropped_reason == 'random_loss':
                stats.random_loss_drops += 1
            else:
                stats.queue_drops += 1
            stats.dropped_data += 1
            _emit('drop', t=now_ms, u=u, v=v, ptype='data', name=ev.name, req_id=ev.req_id,
                  reason=result.dropped_reason or 'queue_full')
            return
        stats.data_tx += 1
        stats.record_traversal(result.breakdown)
        _emit('tx', t=now_ms, u=u, v=v, ptype='data', name=ev.name, req_id=ev.req_id,
              arrival_t=result.arrival_ms, size=cfg.data_size_bytes, from_cache=ev.from_cache)
        push(result.arrival_ms, 'data', DataEvent(ev.req_id, ev.name, v, u, ev.requester, ev.hops + 1, ev.from_cache, ev.verified))

    while q:
        time_ms, _, kind, payload = heapq.heappop(q)

        if kind == 'toggle':
            ev = payload
            assert isinstance(ev, LinkToggleEvent)
            topo.set_link_active(ev.edge[0], ev.edge[1], ev.active)
            _emit('link_up' if ev.active else 'link_down', t=time_ms, u=ev.edge[0], v=ev.edge[1])
            if controller is not None:
                # Centralized recompute, but the new FIBs only take effect
                # after a control round-trip -- nodes keep forwarding on the
                # stale FIB in the meantime, same as a real controller push.
                new_fibs, delay = controller.push_fibs(cfg.producers)
                push(time_ms + delay, 'fib_apply', new_fibs)
            else:
                _rebuild_fibs(nodes, topo, cfg)
            continue

        if kind == 'fib_apply':
            new_fibs = payload
            for node_id, fib in new_fibs.items():
                nodes[node_id].fib = fib
            _emit('control_update', t=time_ms, op='fib_install', targets=len(new_fibs))
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
            _emit('rx', t=time_ms, u=u, ptype='interest', name=ev.name, req_id=ev.req_id)

            cached = st.cs.get(ev.name, now_ms=time_ms)
            if cached is not None:
                node_metrics.cache_hits += 1
                stats.cache_hits += 1
                _emit('cache_hit', t=time_ms, u=u, name=ev.name, req_id=ev.req_id)
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
            _emit('cache_miss', t=time_ms, u=u, name=ev.name, req_id=ev.req_id)

            owner = _find_owner(cfg, ev.name)
            if u == owner:
                stats.producer_serves += 1
                _emit('producer_serve', t=time_ms, u=u, name=ev.name, req_id=ev.req_id)
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
            _emit('rx', t=time_ms, u=u, ptype='data', name=ev.name, req_id=ev.req_id, from_cache=ev.from_cache)
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

    if controller is not None:
        stats.control_messages_tx = controller.stats.control_messages_tx
        stats.reconvergence_events = controller.stats.reconvergence_events
        stats.total_reconvergence_ms = controller.stats.total_reconvergence_ms
        stats.cache_directives_pushed = controller.stats.cache_directives_pushed

    return stats
