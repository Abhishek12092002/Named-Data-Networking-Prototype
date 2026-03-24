from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Dict, List, Sequence, Set


@dataclass(frozen=True)
class Topology:
    adj: Dict[int, List[int]]

    @property
    def n(self) -> int:
        return len(self.adj)

    def neighbors(self, u: int) -> Sequence[int]:
        return self.adj[u]


def _add_undirected(adj: Dict[int, Set[int]], u: int, v: int) -> None:
    adj[u].add(v)
    adj[v].add(u)


def make_line(n: int) -> Topology:
    if n < 2:
        raise ValueError("line topology needs n>=2")
    adj: Dict[int, Set[int]] = {i: set() for i in range(n)}
    for i in range(n - 1):
        _add_undirected(adj, i, i + 1)
    return Topology({k: sorted(v) for k, v in adj.items()})


def make_tree(n: int, branching: int = 2) -> Topology:
    if n < 2:
        raise ValueError("tree topology needs n>=2")
    if branching < 1:
        raise ValueError("branching must be >= 1")
    adj: Dict[int, Set[int]] = {i: set() for i in range(n)}
    for child in range(1, n):
        parent = (child - 1) // branching
        _add_undirected(adj, parent, child)
    return Topology({k: sorted(v) for k, v in adj.items()})


def make_mesh(n: int, extra_edges: int, seed: int) -> Topology:
    if n < 3:
        raise ValueError("mesh topology needs n>=3")
    rnd = random.Random(seed)
    adj: Dict[int, Set[int]] = {i: set() for i in range(n)}
    for i in range(n - 1):
        _add_undirected(adj, i, i + 1)
    _add_undirected(adj, n - 1, 0)

    candidates = [(i, j) for i in range(n) for j in range(i + 1, n) if j not in adj[i]]
    rnd.shuffle(candidates)
    for (u, v) in candidates[: max(0, extra_edges)]:
        _add_undirected(adj, u, v)

    return Topology({k: sorted(v) for k, v in adj.items()})


def shortest_path_next_hop(topo: Topology, src: int, dst: int) -> int:
    if src == dst:
        return src
    prev: Dict[int, int] = {}
    q: List[int] = [src]
    seen = {src}
    for u in q:
        if u == dst:
            break
        for v in topo.neighbors(u):
            if v in seen:
                continue
            seen.add(v)
            prev[v] = u
            q.append(v)

    if dst not in seen:
        raise ValueError(f"no path from {src} to {dst}")

    cur = dst
    while prev[cur] != src:
        cur = prev[cur]
    return cur


def shortest_path_length(topo: Topology, src: int, dst: int) -> int:
    if src == dst:
        return 0
    q: List[int] = [src]
    dist: Dict[int, int] = {src: 0}
    for u in q:
        if u == dst:
            return dist[u]
        for v in topo.neighbors(u):
            if v in dist:
                continue
            dist[v] = dist[u] + 1
            q.append(v)
    raise ValueError(f"no path from {src} to {dst}")
