import unittest

from ndn_sim.core.timing import LinkTimingModel
from ndn_sim.core.network_profiles import get_profile, PROFILES
from ndn_sim.topology import Link, Topology, make_line


class TransmissionDelayTests(unittest.TestCase):
    def test_transmission_delay_matches_formula(self):
        # 1500-byte packet over 100 Mbps: transmission = size_bits / bw_bps
        topo = make_line(2, Link(propagation_delay_ms=0.0, bandwidth_mbps=100.0,
                                  queue_limit_packets=1000, processing_delay_ms=0.0))
        model = LinkTimingModel(topo)
        result = model.traverse(topo, 0, 1, now_ms=0.0, size_bytes=1500)
        expected_ms = (1500 * 8) / (100 * 1_000_000) * 1000.0  # = 0.12 ms
        self.assertTrue(result.ok)
        self.assertAlmostEqual(result.breakdown.transmission_ms, expected_ms, places=6)

    def test_larger_packet_takes_longer_to_transmit(self):
        topo = make_line(2, Link(propagation_delay_ms=0.0, bandwidth_mbps=100.0,
                                  queue_limit_packets=1000, processing_delay_ms=0.0))
        model = LinkTimingModel(topo)
        small = model.traverse(topo, 0, 1, now_ms=0.0, size_bytes=200)
        large = model.traverse(topo, 0, 1, now_ms=100.0, size_bytes=4096)
        self.assertGreater(large.breakdown.transmission_ms, small.breakdown.transmission_ms)

    def test_lower_bandwidth_increases_transmission_delay(self):
        topo_fast = make_line(2, Link(propagation_delay_ms=0.0, bandwidth_mbps=1000.0,
                                       queue_limit_packets=1000, processing_delay_ms=0.0))
        topo_slow = make_line(2, Link(propagation_delay_ms=0.0, bandwidth_mbps=10.0,
                                       queue_limit_packets=1000, processing_delay_ms=0.0))
        fast = LinkTimingModel(topo_fast).traverse(topo_fast, 0, 1, 0.0, 4096)
        slow = LinkTimingModel(topo_slow).traverse(topo_slow, 0, 1, 0.0, 4096)
        self.assertGreater(slow.breakdown.transmission_ms, fast.breakdown.transmission_ms)


class PropagationDelayTests(unittest.TestCase):
    def test_direct_propagation_delay_used_when_no_distance(self):
        topo = make_line(2, Link(propagation_delay_ms=5.0, bandwidth_mbps=100.0))
        link = topo.links[(0, 1)]
        self.assertAlmostEqual(link.effective_propagation_delay_ms(), 5.0)

    def test_propagation_delay_derived_from_distance(self):
        # 100 km at 200 km/ms (representative fiber speed) -> 0.5 ms
        link = Link(distance_km=100.0, propagation_speed_km_per_ms=200.0)
        self.assertAlmostEqual(link.effective_propagation_delay_ms(), 0.5)

    def test_1000km_and_10000km_scale_linearly(self):
        l1 = Link(distance_km=1000.0)
        l2 = Link(distance_km=10000.0)
        self.assertAlmostEqual(l1.effective_propagation_delay_ms(), 5.0)
        self.assertAlmostEqual(l2.effective_propagation_delay_ms(), 50.0)


class MultiHopAccumulationTests(unittest.TestCase):
    def test_multi_hop_latency_accumulates_not_hopcount_times_constant(self):
        # 3-node line, each link: bw=100Mbps, prop=5ms, proc=0.1ms -> per spec example
        link = Link(propagation_delay_ms=5.0, bandwidth_mbps=100.0,
                    queue_limit_packets=1000, processing_delay_ms=0.1)
        topo = make_line(3, link)
        model = LinkTimingModel(topo)
        now = 0.0
        total = 0.0
        for u, v in [(0, 1), (1, 2)]:
            result = model.traverse(topo, u, v, now, size_bytes=1500)
            self.assertTrue(result.ok)
            total += result.breakdown.total_ms
            now = result.arrival_ms
        # ~5.22ms per hop (5 + 0.12 + 0.1) x2 hops =~ 10.44ms, per spec's example
        self.assertAlmostEqual(total, 10.44, delta=0.05)
        # Explicitly not hop_count * a fixed constant like 1000ms/hop
        self.assertLess(total, 100.0)


class QueueingDelayTests(unittest.TestCase):
    def test_empty_queue_has_no_extra_delay(self):
        topo = make_line(2, Link(propagation_delay_ms=1.0, bandwidth_mbps=1000.0,
                                  queue_limit_packets=1000, processing_delay_ms=0.0))
        model = LinkTimingModel(topo)
        result = model.traverse(topo, 0, 1, now_ms=0.0, size_bytes=100)
        self.assertAlmostEqual(result.breakdown.queueing_ms, 0.0, places=6)

    def test_busy_link_increases_queueing_delay(self):
        topo = make_line(2, Link(propagation_delay_ms=0.0, bandwidth_mbps=1.0,  # slow link
                                  queue_limit_packets=1000, processing_delay_ms=0.0))
        model = LinkTimingModel(topo)
        first = model.traverse(topo, 0, 1, now_ms=0.0, size_bytes=100000)
        second = model.traverse(topo, 0, 1, now_ms=0.0, size_bytes=100)  # arrives same instant
        self.assertTrue(first.ok and second.ok)
        self.assertGreater(second.breakdown.queueing_ms, 0.0)

    def test_full_queue_causes_drop(self):
        topo = make_line(2, Link(propagation_delay_ms=0.0, bandwidth_mbps=0.001,  # very slow
                                  queue_limit_packets=1, processing_delay_ms=0.0))
        model = LinkTimingModel(topo)
        ok_count = 0
        drop_count = 0
        for _ in range(20):
            result = model.traverse(topo, 0, 1, now_ms=0.0, size_bytes=10000)
            if result.ok:
                ok_count += 1
            else:
                drop_count += 1
                self.assertEqual(result.dropped_reason, 'queue_full')
        self.assertGreater(drop_count, 0)


class NoWallClockSleepTests(unittest.TestCase):
    def test_large_simulated_duration_runs_fast_in_wall_clock(self):
        import time
        link = Link(propagation_delay_ms=50.0, bandwidth_mbps=10.0, queue_limit_packets=1000)
        topo = make_line(2, link)
        model = LinkTimingModel(topo)
        t0 = time.time()
        now = 0.0
        for _ in range(2000):  # simulate a long run of traffic
            result = model.traverse(topo, 0, 1, now, size_bytes=1500)
            now = result.arrival_ms if result.ok else now + 1.0
        elapsed_wall_s = time.time() - t0
        # 2000 traversals x ~50ms propagation each = 100+ seconds of *simulated*
        # time, but this must not take anywhere near that long in real time.
        self.assertLess(elapsed_wall_s, 2.0)
        self.assertGreater(now, 50_000.0)  # plenty of simulated time elapsed


class ProfileTests(unittest.TestCase):
    def test_all_profiles_buildable(self):
        for name in PROFILES:
            profile = get_profile(name)
            link = profile.to_link()
            self.assertGreater(link.bandwidth_mbps, 0)

    def test_datacenter_faster_than_intercontinental(self):
        dc = get_profile('DATA_CENTER').to_link()
        ic = get_profile('INTERCONTINENTAL').to_link()
        self.assertLess(dc.effective_propagation_delay_ms(), ic.effective_propagation_delay_ms())


class IPvsNDNSharedModelTests(unittest.TestCase):
    def test_ip_and_ndn_share_link_timing_model_class(self):
        from ndn_sim.ndn_engine import LinkScheduler
        from ndn_sim.core.timing import LinkTimingModel
        self.assertTrue(issubclass(LinkScheduler, LinkTimingModel))


if __name__ == '__main__':
    unittest.main()
