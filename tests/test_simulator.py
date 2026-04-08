import unittest

from ndn_sim.cache import make_cache
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


if __name__ == '__main__':
    unittest.main()
