"""
Centralized SDN-style controller for the NDN simulator.

This module is what turns the simulator from "plain NDN" into a hybrid
NDN/SDN system, and is the piece most worth highlighting on a resume:

- ``SDNController.compute_fibs`` acts like an SDN controller that has a full
  view of the topology (an OpenFlow controller's "global view") and computes
  globally-consistent, delay-weighted shortest paths for every node/prefix
  pair, instead of every router computing its own routes locally.
- Updates are only pushed to nodes whose FIB entry actually changed, and a
  configurable control round-trip time (``control_rtt_ms``) is charged before
  the new FIB takes effect. This intentionally avoids the common mistake of
  treating a centralized controller as "free" or "instant" -- the trade-off
  between better/centrally-optimal decisions and slower reaction time to
  failures is the whole point of this comparison.
- ``SDNController.coordinate_cache_placement`` plays the same role for
  in-network caching: instead of every node greedily caching whatever passes
  through it, the controller looks at globally observed popularity (learned
  from a leading "training" window of traffic) and assigns specific content
  to specific, strategically chosen nodes.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Sequence, Set, Tuple

from .topology import Topology


@dataclass
class ControllerStats:
    """Everything needed to report the *cost* of centralization, not just its benefit."""

    fib_pushes: int = 0
    control_messages_tx: int = 0
    reconvergence_events: int = 0
    total_reconvergence_ms: float = 0.0
    cache_directives_pushed: int = 0

    def avg_reconvergence_ms(self) -> float:
        if self.reconvergence_events == 0:
            return 0.0
        return self.total_reconvergence_ms / self.reconvergence_events


def dijkstra(topo: Topology, src: int, active_only: bool = True) -> Tuple[Dict[int, float], Dict[int, int]]:
    """Delay-weighted shortest paths from ``src`` to every reachable node.

    Unlike ``topology.shortest_path`` (a hop-count BFS), this uses each
    link's propagation delay as edge weight, which is what a real SDN
    controller would optimize for when it has full visibility into link
    costs.
    """
    dist: Dict[int, float] = {src: 0.0}
    prev: Dict[int, int] = {}
    visited: Set[int] = set()
    pq: List[Tuple[float, int]] = [(0.0, src)]
    while pq:
        d, u = heapq.heappop(pq)
        if u in visited:
            continue
        visited.add(u)
        for v in topo.neighbors(u):
            if active_only and not topo.is_active(u, v):
                continue
            weight = topo.links[(u, v)].propagation_delay_ms
            nd = d + weight
            if nd < dist.get(v, float('inf')):
                dist[v] = nd
                prev[v] = u
                heapq.heappush(pq, (nd, v))
    return dist, prev


def _path_from_prev(prev: Dict[int, int], src: int, dst: int) -> List[int]:
    if dst == src:
        return [src]
    path = [dst]
    while path[-1] != src:
        nxt = prev.get(path[-1])
        if nxt is None:
            raise ValueError(f'no path from {src} to {dst}')
        path.append(nxt)
    path.reverse()
    return path


class SDNController:
    """Reusable centralized control plane.

    The two data planes speak different table formats -- NDN wants
    ``prefix -> [next_hop]`` FIB entries, IP wants ``dst_node -> next_hop``
    routing-table entries -- so this controller exposes both
    :meth:`compute_fibs`/:meth:`push_fibs` (NDN) and
    :meth:`compute_routes`/:meth:`push_routes` (IP). Both are thin,
    protocol-specific adapters over the *same* underlying route computation
    (:func:`dijkstra`) and the *same* selective-push / control-RTT
    accounting discipline, so "what SDN control costs" is measured
    identically for both data planes -- this is the reusable control-plane
    piece the unified architecture is built around.
    """

    def __init__(self, topo: Topology, control_rtt_ms: float = 5.0):
        self.topo = topo
        self.control_rtt_ms = control_rtt_ms
        self.stats = ControllerStats()
        self._last_fibs: Dict[int, Dict[str, List[int]]] = {}
        self._last_routes: Dict[int, Dict[int, int]] = {}

    def compute_routes(self, destinations: Iterable[int]) -> Dict[int, Dict[int, int]]:
        """Globally-optimal single-next-hop routing table, per node, toward
        every destination node in ``destinations`` -- the IP-shaped
        counterpart of :meth:`compute_fibs`. Both are built from the same
        delay-weighted :func:`dijkstra` computation, rooted at each
        destination in turn.
        """
        new_routes: Dict[int, Dict[int, int]] = {i: {} for i in range(self.topo.n)}
        for dst in destinations:
            _dist, prev = dijkstra(self.topo, dst)
            for node in range(self.topo.n):
                if node == dst:
                    continue
                try:
                    path = _path_from_prev(prev, dst, node)
                except ValueError:
                    continue
                new_routes[node][dst] = path[-2]
        return new_routes

    def push_routes(self, destinations: Iterable[int]) -> Tuple[Dict[int, Dict[int, int]], float]:
        """IP-shaped counterpart of :meth:`push_fibs`: recompute routing
        tables and account for the control-plane cost of applying them
        (selective push -- only nodes whose table actually changed count
        as a control message -- plus the configured control-RTT delay
        before the new tables are installed).
        """
        new_routes = self.compute_routes(destinations)
        changed = 0
        for node, table in new_routes.items():
            if self._last_routes.get(node) != table:
                changed += 1
        self.stats.fib_pushes += 1
        self.stats.control_messages_tx += changed
        delay = self.control_rtt_ms if changed else 0.0
        if changed:
            self.stats.reconvergence_events += 1
            self.stats.total_reconvergence_ms += delay
        self._last_routes = {node: dict(table) for node, table in new_routes.items()}
        return new_routes, delay

    def compute_fibs(self, producers: Dict[str, int]) -> Dict[int, Dict[str, List[int]]]:
        """Globally-optimal single-next-hop FIB for every node, for every prefix."""
        new_fibs: Dict[int, Dict[str, List[int]]] = {i: {} for i in range(self.topo.n)}
        for prefix, producer in producers.items():
            p = prefix.rstrip('/') + '/'
            _dist, prev = dijkstra(self.topo, producer)
            for node in range(self.topo.n):
                if node == producer:
                    continue
                try:
                    path = _path_from_prev(prev, producer, node)
                except ValueError:
                    continue
                next_hop = path[-2]
                new_fibs[node][p] = [next_hop]
        return new_fibs

    def push_fibs(self, producers: Dict[str, int]) -> Tuple[Dict[int, Dict[str, List[int]]], float]:
        """Recompute FIBs and account for the control-plane cost of applying them.

        Returns the new FIB set and the delay (ms) that should elapse before
        it is actually installed on the affected nodes -- i.e. this call does
        *not* apply the FIBs itself, it only tells the caller what to push
        and when.
        """
        new_fibs = self.compute_fibs(producers)
        changed = 0
        for node, fib in new_fibs.items():
            if self._last_fibs.get(node) != fib:
                changed += 1
        self.stats.fib_pushes += 1
        self.stats.control_messages_tx += changed
        delay = self.control_rtt_ms if changed else 0.0
        if changed:
            self.stats.reconvergence_events += 1
            self.stats.total_reconvergence_ms += delay
        self._last_fibs = {node: dict(fib) for node, fib in new_fibs.items()}
        return new_fibs, delay

    def coordinate_cache_placement(
        self,
        popularity: Dict[str, int],
        top_k: int,
        placement_nodes: Sequence[int],
    ) -> Dict[int, Set[str]]:
        """Assign the globally most popular names to controller-chosen nodes.

        This replaces N independent, greedy, locally-informed caching
        decisions with one globally-informed placement plan -- the same
        "coordinated caching" idea explored in SDN-for-ICN research.
        """
        placement_nodes = list(placement_nodes) or [0]
        ranked = sorted(popularity.items(), key=lambda kv: kv[1], reverse=True)[: max(0, top_k)]
        directives: Dict[int, Set[str]] = {n: set() for n in placement_nodes}
        for i, (name, _count) in enumerate(ranked):
            node = placement_nodes[i % len(placement_nodes)]
            directives[node].add(name)
        self.stats.cache_directives_pushed += sum(len(v) for v in directives.values())
        return directives
