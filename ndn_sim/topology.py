from __future__ import annotations

import math
import random
from collections import deque
from dataclasses import dataclass
from typing import Dict, Iterable, List, Sequence, Set, Tuple


#: Representative fiber propagation speed (~2x10^8 m/s), expressed as km per ms
#: so that ``distance_km / DEFAULT_PROPAGATION_SPEED_KM_PER_MS`` yields milliseconds.
#: This is a simulation assumption, not a claim about any specific medium.
DEFAULT_PROPAGATION_SPEED_KM_PER_MS = 200.0


@dataclass(frozen=True)
class Link:
    """A logical link in the event-driven simulator.

    Two ways to specify propagation delay (do not combine them for the same
    link -- pick one and document which you used):

    1. ``propagation_delay_ms`` directly (the historical/simple path -- used
       when the exact number matters more than geographic realism), or
    2. ``distance_km`` + ``propagation_speed_km_per_ms``, in which case
       :meth:`effective_propagation_delay_ms` derives the delay from
       distance and a configurable propagation speed.

    ``processing_delay_ms`` is the link/interface-level processing cost
    (distinct from protocol-level processing such as an NDN PIT/FIB lookup,
    which callers add separately). ``loss_probability`` models independent
    random packet loss on an otherwise-active link; it is deliberately
    separate from deterministic link up/down failures (``active``).
    """

    propagation_delay_ms: float = 10.0
    bandwidth_mbps: float = 100.0
    queue_limit_packets: int = 64
    active: bool = True
    processing_delay_ms: float = 0.1
    loss_probability: float = 0.0
    distance_km: float | None = None
    propagation_speed_km_per_ms: float = DEFAULT_PROPAGATION_SPEED_KM_PER_MS

    def effective_propagation_delay_ms(self) -> float:
        """Propagation delay actually used by the simulator for this link.

        If ``distance_km`` is set, delay is derived as
        ``distance_km / propagation_speed_km_per_ms`` (a physical/geographic
        model). Otherwise the configured ``propagation_delay_ms`` is used
        directly (a simpler, directly-specified model). Both are legitimate
        simulation choices; this method makes the choice explicit rather
        than silently picking one.
        """
        if self.distance_km is not None:
            return self.distance_km / max(self.propagation_speed_km_per_ms, 1e-9)
        return self.propagation_delay_ms


@dataclass
class Topology:
    adj: Dict[int, List[int]]
    links: Dict[Tuple[int, int], Link]

    @property
    def n(self) -> int:
        return len(self.adj)

    def neighbors(self, u: int) -> Sequence[int]:
        return self.adj[u]

    def is_active(self, u: int, v: int) -> bool:
        return self.links[(u, v)].active

    def set_link_active(self, u: int, v: int, active: bool) -> None:
        import dataclasses
        l = self.links[(u, v)]
        self.links[(u, v)] = dataclasses.replace(l, active=active)
        if (v, u) in self.links:
            rl = self.links[(v, u)]
            self.links[(v, u)] = dataclasses.replace(rl, active=active)


DEFAULT_LINK = Link()


def _add_undirected(adj: Dict[int, Set[int]], links: Dict[Tuple[int, int], Link], u: int, v: int, link: Link | None = None) -> None:
    link = link or DEFAULT_LINK
    adj[u].add(v)
    adj[v].add(u)
    links[(u, v)] = link
    links[(v, u)] = link


def make_line(n: int, link: Link | None = None) -> Topology:
    if n < 2:
        raise ValueError('line topology needs n>=2')
    adj: Dict[int, Set[int]] = {i: set() for i in range(n)}
    links: Dict[Tuple[int, int], Link] = {}
    for i in range(n - 1):
        _add_undirected(adj, links, i, i + 1, link)
    return Topology({k: sorted(v) for k, v in adj.items()}, links)


def make_tree(n: int, branching: int = 2, link: Link | None = None) -> Topology:
    if n < 2:
        raise ValueError('tree topology needs n>=2')
    adj: Dict[int, Set[int]] = {i: set() for i in range(n)}
    links: Dict[Tuple[int, int], Link] = {}
    for child in range(1, n):
        parent = (child - 1) // max(branching, 1)
        _add_undirected(adj, links, parent, child, link)
    return Topology({k: sorted(v) for k, v in adj.items()}, links)


def make_mesh(n: int, extra_edges: int, seed: int, link: Link | None = None) -> Topology:
    if n < 3:
        raise ValueError('mesh topology needs n>=3')
    rnd = random.Random(seed)
    adj: Dict[int, Set[int]] = {i: set() for i in range(n)}
    links: Dict[Tuple[int, int], Link] = {}
    for i in range(n - 1):
        _add_undirected(adj, links, i, i + 1, link)
    _add_undirected(adj, links, n - 1, 0, link)
    candidates = [(i, j) for i in range(n) for j in range(i + 1, n) if j not in adj[i]]
    rnd.shuffle(candidates)
    for (u, v) in candidates[: max(0, extra_edges)]:
        _add_undirected(adj, links, u, v, link)
    return Topology({k: sorted(v) for k, v in adj.items()}, links)


def load_edge_list(path: str, default_link: Link | None = None) -> Topology:
    default_link = default_link or DEFAULT_LINK
    edges: List[Tuple[int, int, Link]] = []
    nodes: Set[int] = set()
    with open(path, 'r', encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            u, v = int(parts[0]), int(parts[1])
            delay = float(parts[2]) if len(parts) > 2 else default_link.propagation_delay_ms
            bw = float(parts[3]) if len(parts) > 3 else default_link.bandwidth_mbps
            qlim = int(parts[4]) if len(parts) > 4 else default_link.queue_limit_packets
            l = Link(delay, bw, qlim, True)
            edges.append((u, v, l))
            nodes.update([u, v])
    adj: Dict[int, Set[int]] = {i: set() for i in range(max(nodes) + 1)}
    links: Dict[Tuple[int, int], Link] = {}
    for u, v, l in edges:
        _add_undirected(adj, links, u, v, l)
    return Topology({k: sorted(v) for k, v in adj.items()}, links)


def shortest_path(topo: Topology, src: int, dst: int, active_only: bool = True) -> List[int]:
    if src == dst:
        return [src]
    prev: Dict[int, int] = {}
    q = deque([src])
    seen = {src}
    while q:
        u = q.popleft()
        for v in topo.neighbors(u):
            if active_only and not topo.is_active(u, v):
                continue
            if v in seen:
                continue
            seen.add(v)
            prev[v] = u
            if v == dst:
                q.clear()
                break
            q.append(v)
    if dst not in seen:
        raise ValueError(f'no path from {src} to {dst}')
    path = [dst]
    while path[-1] != src:
        path.append(prev[path[-1]])
    path.reverse()
    return path


def shortest_path_next_hop(topo: Topology, src: int, dst: int, active_only: bool = True) -> int:
    path = shortest_path(topo, src, dst, active_only=active_only)
    return path[1] if len(path) > 1 else src


def shortest_path_length(topo: Topology, src: int, dst: int, active_only: bool = True) -> int:
    return len(shortest_path(topo, src, dst, active_only=active_only)) - 1
