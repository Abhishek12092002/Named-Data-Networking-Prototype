"""Comparative thesis tests: which architecture wins, under which
configuration, and by how much. Companion to test_research_validity.py
(which checks each architecture individually behaves correctly) -- this
file checks the comparative claims researchers actually want: where IP
beats NDN, where NDN beats IP, where SDN helps, where it doesn't, and
where two architectures perform identically by mathematical necessity.

Each test's docstring/comments explain the real measured numbers and the
reasoning behind the expected outcome.
"""

import dataclasses
import unittest

from ndn_sim.cli import build_arg_parser, run_sweep
from ndn_sim.controller import SDNController
from ndn_sim.ip_engine import _build_ip_routes_distributed
from ndn_sim.topology import Link, make_mesh


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


class ThesisA_NDNvsIP(unittest.TestCase):
    def test_a1_ndn_beats_ip_when_caching_has_real_benefit(self):
        common = dict(nodes=14, extra_edges=6, requests=400, seed=5, network_profile='REGIONAL_WAN',
                       workload='zipf', zipf_alpha=1.4, items_per_prefix=30)
        ndn = _row(_run(architecture='ndn', cache_size=24, cache_policy='lru', **common), architecture='ndn')
        ip = _row(_run(architecture='ip', **common), architecture='ip')
        self.assertLess(ndn['avg_latency_ms_mean'], ip['avg_latency_ms_mean'])
        self.assertGreater(ndn['cache_hit_ratio_mean'], 0.5)

    def test_a2_ip_beats_ndn_with_no_caching_benefit(self):
        common = dict(nodes=14, extra_edges=6, requests=200, seed=5, network_profile='REGIONAL_WAN',
                       workload='uniform', items_per_prefix=30, interest_iat_ms=5.0, interest_lifetime_ms=500.0)
        ip = _row(_run(architecture='ip', **common), architecture='ip')
        ndn = _row(_run(architecture='ndn', cache_size=0, **common), architecture='ndn')
        self.assertLess(ip['avg_latency_ms_mean'], ndn['avg_latency_ms_mean'])
        self.assertEqual(ndn['cache_hit_ratio_mean'], 0.0)
        # NDN's larger Data payload (4096B default) vs IP's (1500B default)
        # should show up directly as higher transmission delay.
        self.assertGreater(ndn['avg_transmission_ms_mean'], ip['avg_transmission_ms_mean'])

    def test_a2note_retransmissions_and_drops_rise_monotonically_with_load(self):
        # The "retransmission storm" finding: not a bug, a real congestive
        # feedback effect under sustained overload with a lifetime that
        # isn't generous relative to loaded RTT.
        common = dict(nodes=14, extra_edges=6, requests=400, seed=5, network_profile='REGIONAL_WAN',
                       workload='uniform', items_per_prefix=30, interest_lifetime_ms=500.0,
                       architecture='ndn', cache_size=0)
        retrans = []
        for iat in (5.0, 1.0, 0.3):
            row = _row(_run(interest_iat_ms=iat, **common), architecture='ndn')
            retrans.append(row['retransmissions_mean'])
        self.assertEqual(retrans, sorted(retrans))
        self.assertGreater(retrans[-1], retrans[0])

    def test_a3_ndn_interest_aggregation_reduces_upstream_traffic_vs_ip(self):
        common = dict(nodes=12, extra_edges=4, requests=300, seed=8, workload='zipf', zipf_alpha=2.0,
                       items_per_prefix=3, consumer_count=6, interest_iat_ms=0.2)
        ndn = _row(_run(architecture='ndn', cache_size=0, **common), architecture='ndn')
        ip = _row(_run(architecture='ip', **common), architecture='ip')
        # NDN's PIT aggregation should send dramatically fewer upstream
        # Interest transmissions than IP's one-request-per-request model,
        # for the identical number of underlying requests.
        self.assertLess(ndn['interest_tx_mean'], ip['interest_tx_mean'] * 0.5)

    def test_a4_ndn_retransmission_recovers_delivery_during_transient_failure(self):
        # Internal comparison (NDN vs itself) -- deliberately NOT claiming
        # this makes NDN beat IP in this scenario. In this same congested
        # scenario, plain IP (no retransmission mechanism at all) measures
        # a higher delivery ratio than NDN even with retries enabled --
        # retransmission's benefit here is real but internal to NDN, not a
        # claim that it makes NDN outperform IP under heavy congestion.
        common = dict(topology='mesh', nodes=10, extra_edges=2, requests=150, seed=11, consumers='0',
                       bandwidth_mbps=15.0, interest_iat_ms=2.0,
                       fail_edge='0,1', fail_time_ms=10.0, recover_time_ms=15.0,
                       architecture='ndn', cache_size=0, interest_lifetime_ms=30.0)
        no_retry = _row(_run(max_retransmissions=0, **common), architecture='ndn')
        with_retry = _row(_run(max_retransmissions=2, **common), architecture='ndn')
        self.assertGreater(with_retry['delivery_ratio_mean'], no_retry['delivery_ratio_mean'])
        self.assertGreater(with_retry['retransmissions_mean'], 0)


class ThesisB_SDNEffects(unittest.TestCase):
    def test_b1_sdn_and_distributed_are_identical_under_homogeneous_weights_no_failure(self):
        res = _run(nodes=12, extra_edges=6, seed=42, requests=100, architectures='ip,ip_sdn')
        ip = _row(res, architecture='ip')
        ip_sdn = _row(res, architecture='ip_sdn')
        self.assertEqual(ip['avg_latency_ms_mean'], ip_sdn['avg_latency_ms_mean'])

    def test_b2_sdn_finds_better_paths_than_distributed_on_heterogeneous_weights(self):
        import random
        rnd = random.Random(42)
        topo = make_mesh(14, 6, seed=3, link=Link(3.0, 40.0, 200, True))
        new_links = {}
        seen = set()
        for (u, v), link in list(topo.links.items()):
            if (v, u) in seen:
                continue
            seen.add((u, v))
            delay = rnd.choice([1.0, 1.0, 1.0, 15.0])
            jl = dataclasses.replace(link, propagation_delay_ms=delay)
            new_links[(u, v)] = jl
            new_links[(v, u)] = jl
        topo = dataclasses.replace(topo, links=new_links)

        destinations = [8]
        dist_routes = _build_ip_routes_distributed(topo, destinations)
        sdn_routes = SDNController(topo).compute_routes(destinations)

        def path_cost(routes, src, dst):
            cost, cur, seen_nodes = 0.0, src, set()
            while cur != dst:
                if cur in seen_nodes:
                    return None
                seen_nodes.add(cur)
                nh = routes.get(cur, {}).get(dst)
                if nh is None:
                    return None
                cost += topo.links[(cur, nh)].propagation_delay_ms
                cur = nh
            return cost

        better = 0
        for src in range(14):
            if src == 8:
                continue
            dc = path_cost(dist_routes, src, 8)
            sc = path_cost(sdn_routes, src, 8)
            if dc is None or sc is None:
                continue
            self.assertLessEqual(sc, dc + 1e-9)  # SDN never worse than distributed
            if sc < dc - 1e-9:
                better += 1
        self.assertGreater(better, 0)  # and strictly better for at least some nodes

    def test_b3_distributed_beats_sdn_during_reconvergence_window(self):
        common = dict(topology='mesh', nodes=10, extra_edges=2, requests=400, seed=11, consumers='0',
                       bandwidth_mbps=6.0, interest_iat_ms=0.3,
                       fail_edge='0,1', fail_time_ms=10.0, recover_time_ms=15.0)
        distributed = _row(_run(architecture='ip', **common), architecture='ip')
        sdn_fast = _row(_run(architecture='ip_sdn', controller_rtt_ms=1.0, **common), architecture='ip_sdn')
        sdn_slow = _row(_run(architecture='ip_sdn', controller_rtt_ms=80.0, **common), architecture='ip_sdn')
        self.assertGreaterEqual(distributed['delivery_ratio_mean'], sdn_fast['delivery_ratio_mean'])
        self.assertGreaterEqual(sdn_fast['delivery_ratio_mean'], sdn_slow['delivery_ratio_mean'])

    def test_b4_cache_policy_gap_shrinks_to_near_zero_under_uniform_workload(self):
        common = dict(nodes=12, requests=400, seed=6, cache_size=6, items_per_prefix=40, architecture='ndn')
        lfu_skewed = _row(_run(cache_policy='lfu', workload='zipf', zipf_alpha=1.8, **common), architecture='ndn')
        rand_skewed = _row(_run(cache_policy='random', workload='zipf', zipf_alpha=1.8, **common), architecture='ndn')
        lfu_uniform = _row(_run(cache_policy='lfu', workload='uniform', **common), architecture='ndn')
        rand_uniform = _row(_run(cache_policy='random', workload='uniform', **common), architecture='ndn')

        skewed_gap = lfu_skewed['cache_hit_ratio_mean'] - rand_skewed['cache_hit_ratio_mean']
        uniform_gap = abs(lfu_uniform['cache_hit_ratio_mean'] - rand_uniform['cache_hit_ratio_mean'])
        self.assertGreater(skewed_gap, 0.05)   # meaningful LFU advantage under skew
        self.assertLess(uniform_gap, 0.05)     # converges to ~tie under uniform

    def test_b5_sdn_control_message_cost_scales_with_nodes_regardless_of_latency_effect(self):
        counts = []
        for n in (6, 12, 24):
            row = _row(_run(nodes=n, requests=50, seed=3, architecture='ip_sdn'), architecture='ip_sdn')
            counts.append(row['control_messages_tx_mean'])
        self.assertEqual(counts, sorted(counts))
        self.assertEqual(counts, [6.0, 12.0, 24.0])


if __name__ == '__main__':
    unittest.main()
