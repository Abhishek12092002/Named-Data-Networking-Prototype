"""Deterministic proof that ``sdn-centralized`` is a genuinely different
control plane -- not a configuration label aliasing the same routing code --
and that it produces measurable control-plane activity.

Context: with a topology where every link has *identical* propagation delay,
hop-count BFS (distributed) and delay-weighted Dijkstra (SDN) are
mathematically guaranteed to compute the same routes (minimizing hop count
IS minimizing total weight when every edge weighs the same). That is a
correct property of shortest-path math, not a bug -- but it means a
same-weight demo topology with no failure will show identical IP vs IP+SDN
and NDN vs NDN+SDN latency by construction. These tests use topologies with
*heterogeneous* link weights (so the two algorithms can genuinely disagree)
and explicit failures (so the controller's reconvergence pipeline actually
fires), and check real engine output -- not just the route tables.
"""

import unittest

from ndn_sim.controller import SDNController
from ndn_sim.ip_engine import IPConfig, _build_ip_routes_distributed, ip_fetch_event_driven
from ndn_sim.ndn_engine import FailureEventSpec, NDNConfig, ndn_fetch_event_driven
from ndn_sim.topology import Link, Topology
from ndn_sim.workload import RequestSpec


def _diamond_topology():
    """0-1-2-3 (3 cheap hops, 0.1ms each = 0.3ms) vs 0-3 direct (1 hop, 5ms).
    Hop-count BFS prefers the direct link (fewer hops); delay-weighted
    Dijkstra prefers the 3-hop path (lower total delay). The two algorithms
    are *forced* to disagree on this topology.
    """
    links = {}

    def add(u, v, delay):
        links[(u, v)] = Link(delay, 100.0, 1000, True)
        links[(v, u)] = Link(delay, 100.0, 1000, True)

    add(0, 1, 0.1)
    add(1, 2, 0.1)
    add(2, 3, 0.1)
    add(0, 3, 5.0)
    adj = {0: [1, 3], 1: [0, 2], 2: [1, 3], 3: [0, 2]}
    return Topology(adj, links)


def _ring_topology():
    """5-node ring, uniform 2ms links. Shortest path 0->3 is the 2-hop way
    (0-4-3); failing edge (0,4) forces the controller to recompute the
    3-hop way (0-1-2-3).
    """
    links = {}

    def add(u, v):
        links[(u, v)] = Link(2.0, 50.0, 1000, True)
        links[(v, u)] = Link(2.0, 50.0, 1000, True)

    for u, v in [(0, 1), (1, 2), (2, 3), (3, 4), (4, 0)]:
        add(u, v)
    adj = {0: [1, 4], 1: [0, 2], 2: [1, 3], 3: [2, 4], 4: [0, 3]}
    return Topology(adj, links)


class DistributedAndSDNAreGenuinelyDifferentCode(unittest.TestCase):
    """`sdn-centralized` must not be an alias/no-op label over the same
    routing function as the distributed control plane."""

    def test_ip_distributed_and_sdn_call_different_functions(self):
        # Different modules, different algorithms underneath.
        self.assertNotEqual(_build_ip_routes_distributed, SDNController.compute_routes)
        # _build_ip_routes_distributed is built on topology.shortest_path_next_hop
        # (BFS/hop-count); SDNController.compute_routes is built on
        # controller.dijkstra (delay-weighted). Prove they disagree on a
        # topology engineered so hop-count and delay-weighted optima differ.
        topo = _diamond_topology()
        dist_routes = _build_ip_routes_distributed(topo, [3])
        sdn_routes = SDNController(topo).compute_routes([3])
        self.assertNotEqual(dist_routes[0][3], sdn_routes[0][3])

    def test_ndn_distributed_and_sdn_fib_disagree_on_heterogeneous_topology(self):
        from ndn_sim.ndn_engine import _rebuild_fibs, NodeState

        topo = _diamond_topology()
        nodes = {i: NodeState(cs=None, pit={}, fib={}) for i in topo.adj}
        cfg = NDNConfig(producers={'/x': 3}, cache_size=0, cache_policy='lru')
        _rebuild_fibs(nodes, topo, cfg)
        dist_next_hop = nodes[0].fib['/x/'][0]
        sdn_next_hop = SDNController(topo).compute_fibs({'/x': 3})[0]['/x/'][0]
        self.assertNotEqual(dist_next_hop, sdn_next_hop)


class SDNRouteChoiceActuallyDrivesForwarding(unittest.TestCase):
    """The point of computing a different route is meaningless unless
    packets actually take it. These tests run the full event-driven engine
    (not just the route-table builder) and check the *observed* hop count
    and latency differ in the direction the route computation predicts.
    """

    def test_ip_forwarding_follows_the_sdn_computed_route(self):
        topo = _diamond_topology()
        reqs = [RequestSpec(start_ms=0.0, consumer=0, name='/x/0')]
        payload_db = {'/x/0': b'x'}

        cfg_dist = IPConfig(producers={'/x': 3}, forwarding='distributed', processing_delay_ms=0.0)
        cfg_sdn = IPConfig(producers={'/x': 3}, forwarding='sdn-centralized', processing_delay_ms=0.0)

        stats_dist = ip_fetch_event_driven(_diamond_topology(), cfg_dist, reqs, payload_db, seed=1)
        stats_sdn = ip_fetch_event_driven(_diamond_topology(), cfg_sdn, reqs, payload_db, seed=1)

        self.assertEqual(stats_dist.completed_requests, 1)
        self.assertEqual(stats_sdn.completed_requests, 1)
        # Distributed takes the 1-hop direct link (fewer hops, more delay);
        # SDN takes the 3-hop low-delay path -- opposite hop counts AND
        # opposite latency ordering, which can only happen if forwarding
        # is actually walking two different tables.
        self.assertLess(stats_dist.total_latency_hops, stats_sdn.total_latency_hops)
        self.assertGreater(stats_dist.total_latency_ms, stats_sdn.total_latency_ms)

    def test_ndn_forwarding_follows_the_sdn_computed_fib(self):
        reqs = [RequestSpec(start_ms=0.0, consumer=0, name='/x/0')]
        payload_db = {'/x/0': b'x'}
        cfg_dist = NDNConfig(producers={'/x': 3}, cache_size=0, cache_policy='lru',
                              forwarding='best-route', processing_delay_ms=0.0)
        cfg_sdn = NDNConfig(producers={'/x': 3}, cache_size=0, cache_policy='lru',
                             forwarding='sdn-centralized', processing_delay_ms=0.0)

        stats_dist = ndn_fetch_event_driven(_diamond_topology(), cfg_dist, reqs, payload_db, seed=1)
        stats_sdn = ndn_fetch_event_driven(_diamond_topology(), cfg_sdn, reqs, payload_db, seed=1)

        self.assertNotEqual(stats_dist.total_latency_hops, stats_sdn.total_latency_hops)
        self.assertGreater(stats_dist.total_latency_ms, stats_sdn.total_latency_ms)


class SDNProducesMeasurableControlPlaneActivity(unittest.TestCase):
    """Even with no failure, a real SDN controller has to install initial
    FIBs/routes on every node before it can forward anything -- that
    bootstrap cost should show up as control_messages_tx, not read as zero.
    """

    def test_ip_sdn_bootstrap_is_not_free(self):
        topo = _diamond_topology()
        cfg = IPConfig(producers={'/x': 3}, forwarding='sdn-centralized')
        reqs = [RequestSpec(start_ms=0.0, consumer=0, name='/x/0')]
        stats = ip_fetch_event_driven(topo, cfg, reqs, {'/x/0': b'x'}, seed=1)
        self.assertGreater(stats.control_messages_tx, 0)
        self.assertEqual(stats.reconvergence_events, 0)  # bootstrap != reconvergence

    def test_ndn_sdn_bootstrap_is_not_free(self):
        topo = _diamond_topology()
        cfg = NDNConfig(producers={'/x': 3}, cache_size=0, cache_policy='lru', forwarding='sdn-centralized')
        reqs = [RequestSpec(start_ms=0.0, consumer=0, name='/x/0')]
        stats = ndn_fetch_event_driven(topo, cfg, reqs, {'/x/0': b'x'}, seed=1)
        self.assertGreater(stats.control_messages_tx, 0)
        self.assertEqual(stats.reconvergence_events, 0)

    def test_ip_distributed_bootstrap_is_free(self):
        # Contrast case: distributed control plane genuinely has no
        # controller, so it must report zero control messages even though
        # it also builds a routing table at start.
        topo = _diamond_topology()
        cfg = IPConfig(producers={'/x': 3}, forwarding='distributed')
        reqs = [RequestSpec(start_ms=0.0, consumer=0, name='/x/0')]
        stats = ip_fetch_event_driven(topo, cfg, reqs, {'/x/0': b'x'}, seed=1)
        self.assertEqual(stats.control_messages_tx, 0)


class SDNFailureRecoveryPipeline(unittest.TestCase):
    """The full detection -> controller recompute -> selective push ->
    control-RTT delay -> FIB/route install -> reconvergence pipeline, with
    real non-zero metrics, for both data planes. Also confirms forwarding
    actually reroutes (observed hop count increases) after the failure.
    """

    def test_ip_sdn_failure_triggers_full_reconvergence_pipeline(self):
        topo = _ring_topology()
        cfg = IPConfig(producers={'/x': 3}, forwarding='sdn-centralized', controller_rtt_ms=5.0, processing_delay_ms=0.0)
        # 3 requests before the failure (short 2-hop path), 3 after (forced
        # onto the long 3-hop path once edge (0,4) goes down at t=100).
        reqs = [RequestSpec(start_ms=t, consumer=0, name='/x/0') for t in (10, 20, 30, 200, 210, 220)]
        failures = [FailureEventSpec(100.0, (0, 4), False)]

        stats = ip_fetch_event_driven(topo, cfg, reqs, {'/x/0': b'x'}, failures=failures, seed=1)

        self.assertEqual(stats.completed_requests, 6)
        # Bootstrap (>=1 node) + post-failure selective push (>=1 node changed).
        self.assertGreater(stats.control_messages_tx, 0)
        self.assertEqual(stats.reconvergence_events, 1)  # exactly one topology change
        self.assertAlmostEqual(stats.total_reconvergence_ms, cfg.controller_rtt_ms)
        # Hops accumulate on BOTH the forward and return leg of each request:
        # 3 requests x (2+2) pre-failure hops + 3 requests x (3+3) post-failure hops = 30.
        self.assertEqual(stats.total_latency_hops, 30.0)

    def test_ndn_sdn_failure_triggers_full_reconvergence_pipeline(self):
        topo = _ring_topology()
        cfg = NDNConfig(producers={'/x': 3}, cache_size=0, cache_policy='lru',
                         forwarding='sdn-centralized', controller_rtt_ms=5.0, processing_delay_ms=0.0)
        reqs = [RequestSpec(start_ms=t, consumer=0, name='/x/0') for t in (10, 20, 30, 200, 210, 220)]
        failures = [FailureEventSpec(100.0, (0, 4), False)]

        stats = ndn_fetch_event_driven(topo, cfg, reqs, {'/x/0': b'x'}, failures=failures, seed=1)

        self.assertEqual(stats.completed_requests, 6)
        self.assertGreater(stats.control_messages_tx, 0)
        self.assertEqual(stats.reconvergence_events, 1)
        self.assertAlmostEqual(stats.total_reconvergence_ms, cfg.controller_rtt_ms)
        # NOTE: NDN's total_latency_hops counts only the Data (return) leg's
        # hop count -- ndn_engine.py resets DataEvent.hops to 0 at the
        # producer/cache-hit point (lines ~355, ~371), unlike IP's, which
        # accumulates both the request and response legs. This is a
        # pre-existing metric-definition difference between the two engines,
        # not something introduced here -- flagged, not silently "fixed",
        # since changing it would alter a long-standing NDN metric's meaning
        # without being asked to. 3 requests x 2 hops + 3 requests x 3 hops = 15.
        self.assertEqual(stats.total_latency_hops, 15.0)

    def test_ip_distributed_also_recovers_but_with_zero_controller_cost(self):
        # Contrast case: distributed recovers too (BFS rebuilds instantly on
        # topology change) but with no controller in the loop at all.
        topo = _ring_topology()
        cfg = IPConfig(producers={'/x': 3}, forwarding='distributed', processing_delay_ms=0.0)
        reqs = [RequestSpec(start_ms=t, consumer=0, name='/x/0') for t in (10, 20, 30, 200, 210, 220)]
        failures = [FailureEventSpec(100.0, (0, 4), False)]

        stats = ip_fetch_event_driven(topo, cfg, reqs, {'/x/0': b'x'}, failures=failures, seed=1)

        self.assertEqual(stats.completed_requests, 6)
        self.assertEqual(stats.control_messages_tx, 0)
        self.assertEqual(stats.reconvergence_events, 0)
        self.assertEqual(stats.total_latency_hops, 30.0)  # same reroute, zero control cost


if __name__ == '__main__':
    unittest.main()
