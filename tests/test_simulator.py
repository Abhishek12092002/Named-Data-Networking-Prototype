import unittest

from ndn_sim.cache import make_cache
from ndn_sim.controller import SDNController, dijkstra
from ndn_sim.ip_engine import IPConfig, ip_fetch_event_driven
from ndn_sim.ndn_engine import FailureEventSpec, NDNConfig, ndn_fetch_event_driven
from ndn_sim.topology import Link, make_line, make_mesh
from ndn_sim.workload import RequestSpec, generate_requests, make_catalog


class CacheTests(unittest.TestCase):
    def test_lfu_eviction(self):
        c = make_cache('lfu', 2)
        c.put('a', b'a', 0)
        c.put('b', b'b', 0)
        self.assertEqual(c.get('a', 1), b'a')
        c.put('c', b'c', 2)
        self.assertIsNone(c.get('b', 3))


class SimulatorTests(unittest.TestCase):
    def test_ndn_cache_improves_traffic(self):
        topo = make_line(6, Link(4.0, 50.0, 64, True))
        reqs = [RequestSpec(consumer=0, name='/video/segment/0', start_ms=float(i)) for i in range(12)]
        payload_db = {'/video/segment/0': b'x'}
        base = NDNConfig(cache_size=0, cache_policy='lru', producers={'/video': 5})
        cached = NDNConfig(cache_size=8, cache_policy='lfu', producers={'/video': 5})
        s0 = ndn_fetch_event_driven(topo, base, reqs, payload_db, seed=1)
        s1 = ndn_fetch_event_driven(topo, cached, reqs, payload_db, seed=1)
        self.assertGreater(s1.cache_hits, 0)
        self.assertLess(s1.data_tx, s0.data_tx)

    def test_failure_is_visible(self):
        topo = make_line(6, Link(4.0, 50.0, 64, True))
        reqs = [RequestSpec(consumer=0, name='/video/segment/0', start_ms=float(i)) for i in range(20)]
        payload_db = {'/video/segment/0': b'x'}
        cfg = NDNConfig(cache_size=0, cache_policy='lru', producers={'/video': 5}, interest_lifetime_ms=20, max_retransmissions=0)
        stats = ndn_fetch_event_driven(topo, cfg, reqs, payload_db, seed=1, failures=[FailureEventSpec(2.0, (2, 3), False)])
        self.assertGreaterEqual(stats.timed_out_requests + stats.dropped_interests, 1)

    def test_ip_baseline_runs(self):
        topo = make_mesh(8, 3, 2)
        catalog = make_catalog(['/video', '/news'], 10)
        reqs = generate_requests(catalog, requests=30, workload='zipf', seed=4, zipf_alpha=1.2, consumers=[0, 1], interest_iat_ms=1.0)
        payload_db = {name: b'x' for name in catalog.names}
        cfg = IPConfig(producers={'/video': 6, '/news': 7})
        stats = ip_fetch_event_driven(topo, cfg, reqs, payload_db)
        self.assertGreater(stats.completed_requests, 0)


class ControllerTests(unittest.TestCase):
    def test_dijkstra_finds_all_nodes(self):
        topo = make_line(5, Link(4.0, 50.0, 64, True))
        dist, prev = dijkstra(topo, 0)
        self.assertEqual(set(dist.keys()), {0, 1, 2, 3, 4})
        self.assertAlmostEqual(dist[4], 16.0)

    def test_compute_fibs_matches_shortest_path(self):
        topo = make_mesh(8, 3, seed=1, link=Link(5.0, 50.0, 64, True))
        controller = SDNController(topo, control_rtt_ms=5.0)
        fibs = controller.compute_fibs({'/video': 0})
        # every non-producer node should have exactly one next hop toward the producer
        for node in range(1, topo.n):
            self.assertIn('/video/', fibs[node])
            self.assertEqual(len(fibs[node]['/video/']), 1)

    def test_push_fibs_only_charges_control_messages_on_change(self):
        topo = make_line(5, Link(4.0, 50.0, 64, True))
        controller = SDNController(topo, control_rtt_ms=5.0)
        _, delay1 = controller.push_fibs({'/video': 4})
        self.assertGreater(delay1, 0.0)  # first push always "changes" from empty state
        _, delay2 = controller.push_fibs({'/video': 4})
        self.assertEqual(delay2, 0.0)  # nothing changed -> no control messages, no delay
        self.assertEqual(controller.stats.reconvergence_events, 1)

    def test_coordinate_cache_placement_respects_top_k(self):
        topo = make_line(5, Link(4.0, 50.0, 64, True))
        controller = SDNController(topo)
        popularity = {'/video/a': 10, '/video/b': 5, '/video/c': 1}
        directives = controller.coordinate_cache_placement(popularity, top_k=2, placement_nodes=[0, 1])
        total_assigned = sum(len(v) for v in directives.values())
        self.assertEqual(total_assigned, 2)
        self.assertIn('/video/a', directives[0] | directives[1])


class SDNIntegrationTests(unittest.TestCase):
    def test_sdn_centralized_forwarding_completes_requests(self):
        topo = make_mesh(10, 4, seed=3, link=Link(6.0, 40.0, 64, True))
        catalog = make_catalog(['/video', '/news'], 40)
        reqs = generate_requests(catalog, requests=120, workload='zipf', seed=5, zipf_alpha=1.1, consumers=[0, 1, 2], interest_iat_ms=1.0)
        payload_db = {n: b'x' for n in catalog.names}
        cfg = NDNConfig(cache_size=8, cache_policy='lru', producers={'/video': 8, '/news': 9}, forwarding='sdn-centralized')
        stats = ndn_fetch_event_driven(topo, cfg, reqs, payload_db, seed=5)
        self.assertGreater(stats.completed_requests, 0)

    def test_sdn_centralized_reconverges_after_failure(self):
        topo = make_mesh(10, 4, seed=3, link=Link(6.0, 40.0, 64, True))
        catalog = make_catalog(['/video', '/news'], 40)
        reqs = generate_requests(catalog, requests=150, workload='zipf', seed=5, zipf_alpha=1.1, consumers=[0, 1, 2], interest_iat_ms=1.0)
        payload_db = {n: b'x' for n in catalog.names}
        cfg = NDNConfig(cache_size=8, cache_policy='lru', producers={'/video': 8, '/news': 9}, forwarding='sdn-centralized', controller_rtt_ms=5.0)
        stats = ndn_fetch_event_driven(
            topo, cfg, reqs, payload_db, seed=5,
            failures=[FailureEventSpec(10.0, (0, 1), False), FailureEventSpec(60.0, (0, 1), True)],
        )
        self.assertGreaterEqual(stats.reconvergence_events, 1)
        self.assertGreater(stats.control_messages_tx, 0)

    def test_sdn_coordinated_cache_restricts_admission(self):
        topo = make_mesh(10, 4, seed=3, link=Link(6.0, 40.0, 64, True))
        catalog = make_catalog(['/video', '/news'], 40)
        reqs = generate_requests(catalog, requests=200, workload='zipf', seed=9, zipf_alpha=1.2, consumers=[0, 1, 2], interest_iat_ms=1.0)
        payload_db = {n: b'x' for n in catalog.names}
        cfg = NDNConfig(
            cache_size=8, cache_policy='sdn-coordinated', producers={'/video': 8, '/news': 9},
            forwarding='best-route', sdn_cache_top_k=6,
        )
        stats = ndn_fetch_event_driven(topo, cfg, reqs, payload_db, seed=9)
        self.assertGreater(stats.completed_requests, 0)
        self.assertEqual(stats.cache_directives_pushed, 6)


class IPRoutingTests(unittest.TestCase):
    def test_ip_distributed_routing_completes_requests(self):
        topo = make_mesh(8, 3, 2)
        catalog = make_catalog(['/video', '/news'], 10)
        reqs = generate_requests(catalog, requests=30, workload='zipf', seed=4, zipf_alpha=1.2, consumers=[0, 1], interest_iat_ms=1.0)
        payload_db = {name: b'x' for name in catalog.names}
        cfg = IPConfig(producers={'/video': 6, '/news': 7}, forwarding='distributed')
        stats = ip_fetch_event_driven(topo, cfg, reqs, payload_db)
        self.assertGreater(stats.completed_requests, 0)
        self.assertEqual(stats.control_messages_tx, 0)  # distributed control plane has no controller

    def test_ip_sdn_centralized_completes_requests(self):
        topo = make_mesh(10, 4, seed=3, link=Link(6.0, 40.0, 64, True))
        catalog = make_catalog(['/video', '/news'], 40)
        reqs = generate_requests(catalog, requests=120, workload='zipf', seed=5, zipf_alpha=1.1, consumers=[0, 1, 2], interest_iat_ms=1.0)
        payload_db = {n: b'x' for n in catalog.names}
        cfg = IPConfig(producers={'/video': 8, '/news': 9}, forwarding='sdn-centralized', controller_rtt_ms=5.0)
        stats = ip_fetch_event_driven(topo, cfg, reqs, payload_db, seed=5)
        self.assertGreater(stats.completed_requests, 0)

    def test_ip_sdn_reconverges_after_failure(self):
        topo = make_mesh(10, 4, seed=3, link=Link(6.0, 40.0, 64, True))
        catalog = make_catalog(['/video', '/news'], 40)
        reqs = generate_requests(catalog, requests=150, workload='zipf', seed=5, zipf_alpha=1.1, consumers=[0, 1, 2], interest_iat_ms=1.0)
        payload_db = {n: b'x' for n in catalog.names}
        cfg = IPConfig(producers={'/video': 8, '/news': 9}, forwarding='sdn-centralized', controller_rtt_ms=5.0)
        stats = ip_fetch_event_driven(
            topo, cfg, reqs, payload_db, seed=5,
            failures=[FailureEventSpec(10.0, (0, 1), False), FailureEventSpec(60.0, (0, 1), True)],
        )
        self.assertGreaterEqual(stats.reconvergence_events, 1)
        self.assertGreater(stats.control_messages_tx, 0)

    def test_ip_distributed_recovers_from_failure_without_controller_cost(self):
        # Same failure schedule as the SDN test above, but distributed
        # control: routes should still recover (BFS rebuilds instantly),
        # with zero control-plane messages -- the "cost of centralization"
        # this simulator is meant to make visible.
        topo = make_mesh(10, 4, seed=3, link=Link(6.0, 40.0, 64, True))
        catalog = make_catalog(['/video', '/news'], 40)
        reqs = generate_requests(catalog, requests=150, workload='zipf', seed=5, zipf_alpha=1.1, consumers=[0, 1, 2], interest_iat_ms=1.0)
        payload_db = {n: b'x' for n in catalog.names}
        cfg = IPConfig(producers={'/video': 8, '/news': 9}, forwarding='distributed')
        stats = ip_fetch_event_driven(
            topo, cfg, reqs, payload_db, seed=5,
            failures=[FailureEventSpec(10.0, (0, 1), False), FailureEventSpec(60.0, (0, 1), True)],
        )
        self.assertGreater(stats.completed_requests, 0)
        self.assertEqual(stats.control_messages_tx, 0)
        self.assertEqual(stats.reconvergence_events, 0)

    def test_ip_and_ip_sdn_fair_comparison_same_conditions(self):
        # Identical topology/workload/seed/failure schedule for both --
        # the only difference is the control plane, isolating the effect
        # of SDN per the project's fairness requirement.
        def build_topo():
            return make_mesh(10, 4, seed=7, link=Link(5.0, 50.0, 64, True))

        catalog = make_catalog(['/video', '/news'], 30)
        reqs = generate_requests(catalog, requests=100, workload='zipf', seed=2, zipf_alpha=1.0, consumers=[0, 1], interest_iat_ms=1.0)
        payload_db = {n: b'x' for n in catalog.names}
        failures = [FailureEventSpec(20.0, (2, 3), False), FailureEventSpec(80.0, (2, 3), True)]

        distributed_cfg = IPConfig(producers={'/video': 8, '/news': 9}, forwarding='distributed')
        sdn_cfg = IPConfig(producers={'/video': 8, '/news': 9}, forwarding='sdn-centralized', controller_rtt_ms=4.0)

        distributed_stats = ip_fetch_event_driven(build_topo(), distributed_cfg, reqs, payload_db, seed=2, failures=failures)
        sdn_stats = ip_fetch_event_driven(build_topo(), sdn_cfg, reqs, payload_db, seed=2, failures=failures)

        self.assertGreater(distributed_stats.completed_requests, 0)
        self.assertGreater(sdn_stats.completed_requests, 0)
        # SDN reports actual control-plane cost; distributed does not.
        self.assertEqual(distributed_stats.control_messages_tx, 0)
        self.assertGreater(sdn_stats.control_messages_tx, 0)


if __name__ == '__main__':
    unittest.main()
