from __future__ import annotations

import heapq
import itertools
import random
from dataclasses import dataclass
from typing import Dict, List, Set, Tuple

from .cache import make_cache
from .sim_models import Data, NodeState, RunStats, longest_prefix_match
from .topology import Topology, shortest_path_next_hop


@dataclass(frozen=True)
class NDNConfig:
    cache_size: int
    cache_policy: str
    producer_node: int
    name_prefix: str
    link_delay_ms: float = 10.0
    processing_delay_ms: float = 1.0
    interest_iat_ms: float = 0.0


def build_ndn_nodes(topo: Topology, cfg: NDNConfig) -> Dict[int, NodeState]:
    nodes: Dict[int, NodeState] = {}
    prefix = cfg.name_prefix.rstrip("/") + "/"

    for i in range(topo.n):
        cs = make_cache(cfg.cache_policy, cfg.cache_size)
        pit: Dict[str, set] = {}
        fib: Dict[str, int] = {}
        if i != cfg.producer_node:
            nh = shortest_path_next_hop(topo, i, cfg.producer_node)
            fib[prefix] = nh
        nodes[i] = NodeState(cs=cs, pit=pit, fib=fib)

    return nodes


def ndn_fetch_sequential(
    topo: Topology,
    cfg: NDNConfig,
    requests: List[str],
    payload_db: Dict[str, bytes],
    consumer_node: int,
    seed: int,
) -> RunStats:
    _ = random.Random(seed)
    stats = RunStats()
    nodes = build_ndn_nodes(topo, cfg)

    @dataclass(frozen=True)
    class InterestEvent:
        req_id: int
        name: str
        at: int
        prev: int
        hops: int

    @dataclass(frozen=True)
    class DataEvent:
        name: str
        at: int
        prev: int
        hops: int
        from_cache: bool

    # req_id -> start_time_ms
    req_start_ms: Dict[int, float] = {}
    # req_id -> interest hops accumulated until it stopped (cache hit / producer / aggregation)
    req_interest_hops: Dict[int, int] = {}
    # name -> list of req_ids that have been issued by the consumer and are pending at the application layer
    pending_by_name: Dict[str, List[int]] = {}

    # time-ordered event queue: (time_ms, seq, kind, payload)
    q: List[Tuple[float, int, str, object]] = []
    seq = itertools.count()

    def _push(time_ms: float, kind: str, ev: object) -> None:
        heapq.heappush(q, (time_ms, next(seq), kind, ev))

    def _edge_delay_ms() -> float:
        return float(cfg.link_delay_ms) + float(cfg.processing_delay_ms)

    def _forward_interest(u: int, v: int, ev: InterestEvent, time_ms: float) -> None:
        stats.interest_tx += 1
        _push(time_ms + _edge_delay_ms(), "interest", InterestEvent(req_id=ev.req_id, name=ev.name, at=v, prev=u, hops=ev.hops + 1))

    def _forward_data(u: int, v: int, ev: DataEvent, time_ms: float) -> None:
        stats.data_tx += 1
        _push(time_ms + _edge_delay_ms(), "data", DataEvent(name=ev.name, at=v, prev=u, hops=ev.hops + 1, from_cache=ev.from_cache))

    def _satisfy_consumer(name: str, ev: DataEvent, time_ms: float) -> None:
        if name not in pending_by_name:
            return
        req_ids = pending_by_name.pop(name)
        for rid in req_ids:
            start = req_start_ms[rid]
            stats.requests += 1
            stats.total_latency_ms += float(time_ms - start)
            i_hops = req_interest_hops.get(rid, 0)
            d_hops = ev.hops
            stats.total_latency_hops += int(i_hops + d_hops)
            if ev.from_cache:
                stats.cache_hits += 1
            else:
                stats.cache_misses += 1

    # Schedule consumer Interests.
    for rid, name in enumerate(requests):
        if name not in payload_db:
            raise KeyError(f"missing payload for {name}")
        t0 = float(rid) * float(cfg.interest_iat_ms)
        req_start_ms[rid] = t0
        _push(t0, "interest", InterestEvent(req_id=rid, name=name, at=consumer_node, prev=-1, hops=0))

    while q:
        time_ms, _, kind, payload = heapq.heappop(q)

        if kind == "interest":
            ev = payload  # type: ignore[assignment]
            assert isinstance(ev, InterestEvent)
            u = ev.at
            st = nodes[u]

            if u == consumer_node and ev.prev == -1:
                pending_by_name.setdefault(ev.name, []).append(ev.req_id)

            cached = st.cs.get(ev.name)
            if cached is not None:
                req_interest_hops.setdefault(ev.req_id, ev.hops)
                if ev.prev != -1:
                    _forward_data(u, ev.prev, DataEvent(name=ev.name, at=u, prev=ev.prev, hops=0, from_cache=True), time_ms)
                else:
                    _satisfy_consumer(ev.name, DataEvent(name=ev.name, at=u, prev=-1, hops=0, from_cache=True), time_ms)
                continue

            if u == cfg.producer_node:
                req_interest_hops.setdefault(ev.req_id, ev.hops)
                _ = Data(name=ev.name, payload=payload_db[ev.name])
                if ev.prev != -1:
                    _forward_data(u, ev.prev, DataEvent(name=ev.name, at=u, prev=ev.prev, hops=0, from_cache=False), time_ms)
                else:
                    _satisfy_consumer(ev.name, DataEvent(name=ev.name, at=u, prev=-1, hops=0, from_cache=False), time_ms)
                continue

            # PIT aggregation / insertion
            if ev.name in st.pit:
                st.pit[ev.name].add(ev.prev)
                req_interest_hops.setdefault(ev.req_id, ev.hops)
                continue
            st.pit[ev.name] = {ev.prev}

            nh = longest_prefix_match(st.fib, ev.name)
            if nh is None:
                raise RuntimeError(f"no FIB route for name {ev.name} at node {u}")
            _forward_interest(u, nh, ev, time_ms)
            continue

        if kind == "data":
            ev = payload  # type: ignore[assignment]
            assert isinstance(ev, DataEvent)
            u = ev.at

            # Cache on-path
            nodes[u].cs.put(ev.name, payload_db[ev.name])

            if ev.name not in nodes[u].pit:
                # Data with no PIT entry: drop.
                continue

            faces: Set[int] = set(nodes[u].pit.pop(ev.name))
            for f in faces:
                if f == -1:
                    _satisfy_consumer(ev.name, ev, time_ms)
                else:
                    _forward_data(u, f, ev, time_ms)
            continue

        raise RuntimeError(f"unknown event kind: {kind}")

    return stats
