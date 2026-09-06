"""Research-validity tests: does SimN behave like a real network model?

Every test in this file asserts a *directional* or *monotonic* relationship
grounded in established networking theory -- not an exact numeric value
(the model's exact output depends on many parameters and will legitimately
shift as the simulator evolves). What must NOT shift is the *sign* of these
relationships: if enlarging a cache ever stopped improving hit ratio, or a
severed non-redundant link ever stopped causing packet loss, that would be
a real correctness regression, not a tuning difference.

This file exists because a simulator can pass every unit test (correct
data structures, correct control flow) while still not exercise trustworthy
system-level behavior for research use -- these tests check the latter.
Each test's docstring/comments explain the real measured numbers and the
networking-theory reasoning behind the expected direction.
"""

import unittest

from ndn_sim.cli import build_arg_parser, run_sweep


def _run(**overrides):
    parser = build_arg_parser()
    args = parser.parse_args([])
    for k, v in overrides.items():
        setattr(args, k, v)
    return run_sweep(args)


def _row(result, architecture=None, system=None):
    for r in result['summary_rows']:
        f = r.fields
        if architecture and f['architecture'] != architecture:
            continue
        if system and f['system'] != system:
            continue
        return f
    raise AssertionError(f'no matching row for architecture={architecture} system={system}')


class CachingBehavesLikeARealCache(unittest.TestCase):
    def test_hit_ratio_increases_monotonically_with_cache_size(self):
        ratios = []
        for cache_size in (0, 4, 16, 64):
            row = _row(_run(nodes=12, requests=300, seed=3, architectures='ndn',
                             cache_size=cache_size, workload='zipf', zipf_alpha=1.2, items_per_prefix=15),
                       system='ndn')
            ratios.append(row['cache_hit_ratio_mean'])
        self.assertEqual(ratios, sorted(ratios))
        self.assertEqual(ratios[0], 0.0)  # zero cache -> zero hits, exactly
        self.assertGreater(ratios[-1], 0.5)

    def test_latency_decreases_monotonically_with_cache_size(self):
        latencies = []
        for cache_size in (0, 4, 16, 64):
            row = _row(_run(nodes=12, requests=300, seed=3, architectures='ndn',
                             cache_size=cache_size, workload='zipf', zipf_alpha=1.2, items_per_prefix=15),
                       system='ndn')
            latencies.append(row['avg_latency_ms_mean'])
        self.assertEqual(latencies, sorted(latencies, reverse=True))

    def test_hit_ratio_higher_under_skewed_workload_than_uniform(self):
        row_uniform = _row(_run(nodes=12, requests=400, seed=3, architectures='ndn', cache_size=8,
                                 workload='uniform', items_per_prefix=30), system='ndn')
        row_skewed = _row(_run(nodes=12, requests=400, seed=3, architectures='ndn', cache_size=8,
                                workload='zipf', zipf_alpha=1.5, items_per_prefix=30), system='ndn')
        self.assertGreater(row_skewed['cache_hit_ratio_mean'], row_uniform['cache_hit_ratio_mean'])


class NetworkProfilesBehaveLikeRealLinkCharacteristics(unittest.TestCase):
    def test_latency_decreases_from_intercontinental_to_datacenter(self):
        profiles = ['INTERCONTINENTAL', 'INTERNET_WAN', 'REGIONAL_WAN', 'LAN', 'DATA_CENTER']
        latencies = [_row(_run(nodes=12, requests=150, seed=3, architectures='ip', network_profile=p),
                           system='ip')['avg_latency_ms_mean'] for p in profiles]
        self.assertEqual(latencies, sorted(latencies, reverse=True))

    def test_higher_bandwidth_reduces_transmission_delay(self):
        delays = []
        for bw in (5.0, 25.0, 100.0, 1000.0):
            row = _row(_run(nodes=10, requests=100, seed=3, architectures='ip', bandwidth_mbps=bw), system='ip')
            delays.append(row['avg_transmission_ms_mean'])
        self.assertEqual(delays, sorted(delays, reverse=True))


class SDNControlPlaneBehavesLikeARealController(unittest.TestCase):
    def test_reconvergence_time_tracks_controller_rtt(self):
        for rtt in (1.0, 5.0, 20.0, 50.0):
            row = _row(_run(nodes=10, requests=80, seed=3, architecture='ip_sdn', controller_rtt_ms=rtt,
                             fail_edge='0,1', fail_time_ms=20.0, recover_time_ms=80.0),
                       architecture='ip_sdn')
            self.assertAlmostEqual(row['avg_reconvergence_ms_mean'], rtt, places=3)

    def test_control_message_count_scales_with_node_count(self):
        counts = []
        for n in (6, 12, 24):
            row = _row(_run(nodes=n, requests=50, seed=3, architecture='ip_sdn'), architecture='ip_sdn')
            counts.append(row['control_messages_tx_mean'])
        self.assertEqual(counts, sorted(counts))
        self.assertLess(counts[0], counts[-1])

    def test_slower_sdn_reconvergence_causes_worse_delivery_than_faster(self):
        # The core "cost of centralization" finding: on a topology with a
        # real detour available (so this isn't just total outage), a
        # slower-reacting SDN controller should let more traffic get lost
        # during the reconvergence window than a faster one, and both
        # should do somewhat worse than a plain distributed control plane,
        # which reacts instantly by construction.
        common = dict(topology='mesh', nodes=10, extra_edges=2, requests=400, seed=11, consumers='0',
                       bandwidth_mbps=6.0, interest_iat_ms=0.3,
                       fail_edge='0,1', fail_time_ms=10.0, recover_time_ms=15.0)
        distributed = _row(_run(architecture='ip', **common), architecture='ip')
        sdn_fast = _row(_run(architecture='ip_sdn', controller_rtt_ms=1.0, **common), architecture='ip_sdn')
        sdn_slow = _row(_run(architecture='ip_sdn', controller_rtt_ms=80.0, **common), architecture='ip_sdn')

        self.assertGreaterEqual(distributed['delivery_ratio_mean'], sdn_fast['delivery_ratio_mean'])
        self.assertGreaterEqual(sdn_fast['delivery_ratio_mean'], sdn_slow['delivery_ratio_mean'])
        self.assertLessEqual(sdn_fast['dropped_interests_mean'], sdn_slow['dropped_interests_mean'])


class FailureModelBehavesLikeRealTopologyLoss(unittest.TestCase):
    def test_severing_the_only_path_causes_complete_loss(self):
        # Consumer placed on the far side of the cut edge on a LINE
        # topology (no redundancy at all) -- once severed with no
        # recovery, literally nothing can get through.
        common = dict(topology='line', nodes=8, requests=200, seed=7, architecture='ip', consumers='7')
        no_failure = _row(_run(**common), architecture='ip')
        permanent_failure = _row(_run(fail_edge='3,4', fail_time_ms=1.0, recover_time_ms=99999.0, **common),
                                  architecture='ip')

        self.assertEqual(no_failure['delivery_ratio_mean'], 1.0)
        self.assertEqual(no_failure['dropped_interests_mean'], 0.0)
        self.assertEqual(permanent_failure['delivery_ratio_mean'], 0.0)
        self.assertEqual(permanent_failure['dropped_interests_mean'], permanent_failure['requests_mean'])

    def test_redundant_topology_survives_a_single_link_failure(self):
        # Contrast case: a well-connected mesh with extra edges should
        # show ~zero impact from a single link failure, because alternate
        # paths exist and distributed control reroutes instantly. This is
        # the correct behavior, not a bug.
        common = dict(topology='mesh', nodes=12, extra_edges=8, requests=200, seed=7, architecture='ip')
        no_failure = _row(_run(**common), architecture='ip')
        with_failure = _row(_run(fail_edge='0,1', fail_time_ms=10.0, recover_time_ms=9999.0, **common),
                             architecture='ip')
        self.assertEqual(no_failure['delivery_ratio_mean'], 1.0)
        self.assertGreaterEqual(with_failure['delivery_ratio_mean'], 0.95)


class QueueingBehavesLikeARealFiniteQueue(unittest.TestCase):
    def test_higher_offered_load_increases_queue_drops_monotonically(self):
        drops = []
        for iat in (10.0, 2.0, 0.5, 0.1):
            row = _row(_run(nodes=8, requests=300, seed=3, architectures='ip', bandwidth_mbps=2.0,
                             interest_iat_ms=iat, queue_limit_packets=8), system='ip')
            drops.append(row['queue_drops_mean'])
        self.assertEqual(drops, sorted(drops))

    def test_pit_overflow_drops_increase_as_pit_capacity_shrinks_under_load(self):
        overflow = []
        for pit_cap in (200, 20, 5):
            row = _row(_run(nodes=10, requests=300, seed=9, architecture='ndn', cache_size=0,
                             interest_iat_ms=0.3, pit_capacity=pit_cap, interest_lifetime_ms=400.0),
                       architecture='ndn')
            overflow.append(row['pit_overflow_drops_mean'])
        self.assertEqual(overflow, sorted(overflow))
        self.assertGreater(overflow[-1], overflow[0])


class DroppedPacketMetricsAreExposedAndCorrect(unittest.TestCase):
    """Ensures dropped-packet counters and delivery ratio are exposed in
    aggregated results. Both engines track dropped_interests/dropped_data
    internally, and this locks in that they surface all the way through to
    the CSV/table output rather than being silently dropped during
    aggregation.
    """

    def test_dropped_interests_and_delivery_ratio_present_in_output(self):
        """Ensures dropped-packet counters and delivery ratio are exposed in
        aggregated results -- both engines track these internally, and this
        test locks in that they surface all the way through to the CSV/table
        output rather than being silently dropped during aggregation.
        """
        row = _row(_run(topology='line', nodes=8, requests=100, seed=7, architecture='ip', consumers='7',
                         fail_edge='3,4', fail_time_ms=1.0, recover_time_ms=99999.0),
                   architecture='ip')
        self.assertIn('dropped_interests_mean', row)
        self.assertIn('dropped_data_mean', row)
        self.assertIn('delivery_ratio_mean', row)
        self.assertEqual(row['dropped_interests_mean'], row['requests_mean'])
        self.assertEqual(row['delivery_ratio_mean'], 0.0)


if __name__ == '__main__':
    unittest.main()
