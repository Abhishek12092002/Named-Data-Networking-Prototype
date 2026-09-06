from __future__ import annotations

import argparse
import json
import os
import statistics
from dataclasses import dataclass
from typing import Dict, Iterable, List

from .core.network_profiles import PROFILES, get_profile
from .ip_engine import IPConfig, ip_fetch_event_driven
from .ndn_engine import FailureEventSpec, NDNConfig, ndn_fetch_event_driven
from .results import ResultRow, ensure_out_dir, write_csv
from .topology import Link, Topology, load_edge_list, make_line, make_mesh, make_tree
from .workload import DEFAULT_PREFIXES, generate_requests, make_catalog


DEFAULT_PRODUCER_MAP = {'/video': 0, '/news': 1, '/iot': 2}


def _make_topology(args: argparse.Namespace) -> Topology:
    if getattr(args, 'network_profile', ''):
        # A named, documented profile (see core/network_profiles.py)
        # determines bandwidth/propagation/processing/loss/queue-capacity
        # together. Explicit --link-delay-ms/--bandwidth-mbps/etc. flags
        # are ignored in this mode -- pick one or the other per run, so a
        # sweep's link characteristics are never a silent mix of "some
        # from the profile, some from manual flags."
        link = get_profile(args.network_profile).to_link(active=True)
    else:
        link = Link(
            args.link_delay_ms, args.bandwidth_mbps, args.queue_limit_packets, True,
            processing_delay_ms=args.link_processing_delay_ms,
            loss_probability=args.loss_probability,
        )
    if args.topology_file:
        return load_edge_list(args.topology_file, link)
    if args.topology == 'line':
        return make_line(args.nodes, link)
    if args.topology == 'tree':
        return make_tree(args.nodes, branching=args.branching, link=link)
    if args.topology == 'mesh':
        return make_mesh(args.nodes, extra_edges=args.extra_edges, seed=args.seed, link=link)
    raise ValueError(f'unknown topology: {args.topology}')


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description='Resume-level NDN simulator')
    p.add_argument('--topology', choices=['line', 'tree', 'mesh'], default='mesh')
    p.add_argument('--topology-file', default='')
    p.add_argument('--nodes', type=int, default=12)
    p.add_argument('--branching', type=int, default=2)
    p.add_argument('--extra-edges', type=int, default=6)
    p.add_argument('--cache-size', type=int, default=32)
    p.add_argument('--cache-sizes', default='')
    p.add_argument('--cache-policy', choices=['lru', 'fifo', 'lfu', 'random', 'sdn-coordinated'], default='lfu')
    p.add_argument('--cache-policies', default='')
    p.add_argument('--forwarding', choices=['best-route', 'flooding', 'probabilistic', 'sdn-centralized'], default='best-route')
    p.add_argument('--forwardings', default='')
    p.add_argument('--cache-admit-threshold', type=int, default=1)
    p.add_argument('--cache-ttl-ms', type=float, default=0.0)
    p.add_argument('--workload', choices=['uniform', 'zipf', 'bursty', 'shifted'], default='zipf')
    p.add_argument('--requests', type=int, default=500)
    p.add_argument('--items-per-prefix', type=int, default=100)
    p.add_argument('--prefixes', default=','.join(DEFAULT_PREFIXES))
    p.add_argument('--zipf-alpha', type=float, default=1.2)
    p.add_argument('--zipf-alphas', default='')
    p.add_argument('--consumers', default='')
    p.add_argument('--consumer-count', type=int, default=3)
    p.add_argument('--interest-iat-ms', type=float, default=1.0)
    p.add_argument('--interest-lifetime-ms', type=float, default=250.0)
    p.add_argument('--max-retransmissions', type=int, default=1)
    p.add_argument('--pit-capacity', type=int, default=256)
    p.add_argument('--link-delay-ms', type=float, default=8.0,
                    help='Direct propagation delay (ms), used when --network-profile is not set.')
    p.add_argument('--bandwidth-mbps', type=float, default=25.0)
    p.add_argument('--queue-limit-packets', type=int, default=64)
    p.add_argument('--link-processing-delay-ms', type=float, default=0.1,
                    help='Link/interface-level processing delay (ms) -- distinct from the '
                         'protocol-level --processing-delay-ms (PIT/FIB/routing lookup cost).')
    p.add_argument('--loss-probability', type=float, default=0.0,
                    help='Independent random packet loss probability per link traversal, '
                         'separate from deterministic --fail-edge link failures.')
    p.add_argument('--network-profile', choices=[''] + sorted(PROFILES),
                    default='', help='Use a representative, real-world-inspired link profile '
                                      '(see core/network_profiles.py) instead of manual link flags. '
                                      'Choices: ' + ', '.join(sorted(PROFILES)))
    p.add_argument('--processing-delay-ms', type=float, default=1.0,
                    help='Protocol-level processing delay (ms): NDN PIT/FIB/Content-Store lookup '
                         'cost, or IP forwarding-table lookup cost.')
    p.add_argument('--verify-data', action='store_true')
    p.add_argument('--verification-delay-ms', type=float, default=0.5)
    p.add_argument('--invalid-data-rate', type=float, default=0.0)
    p.add_argument('--fail-edge', default='')
    p.add_argument('--fail-time-ms', type=float, default=0.0)
    p.add_argument('--recover-time-ms', type=float, default=0.0)
    p.add_argument('--seed', type=int, default=42)
    p.add_argument('--seeds', default='')
    p.add_argument('--config', default='')
    p.add_argument('--out-dir', default='out')
    p.add_argument('--architecture', choices=['ip', 'ip_sdn', 'ndn', 'ndn_sdn'], default='',
                    help="Restrict the run to a single architecture. The unified comparison "
                         "(topology/workload/seed held fixed) is IP -> IP+SDN and NDN -> NDN+SDN "
                         "-- SDN is a control-plane option on each data plane, not a fifth "
                         "protocol, so this flag is a *filter* over the existing "
                         "system x forwarding sweep, not a separate code path. "
                         "Default: run all four.")
    p.add_argument('--architectures', default='',
                    help="Comma-separated list of architectures (ip,ip_sdn,ndn,ndn_sdn) to run. "
                         "Overrides --architecture if both are given.")
    # --- SDN controller extension ---
    p.add_argument('--controller-rtt-ms', type=float, default=5.0,
                    help='Control round-trip time charged before a centralized FIB update takes effect (sdn-centralized only).')
    p.add_argument('--sdn-cache-top-k', type=int, default=0,
                    help='Number of globally popular names the controller assigns to placement nodes (0 disables coordinated caching).')
    p.add_argument('--sdn-cache-placement', choices=['near-producer', 'near-consumer', 'hybrid'], default='near-producer',
                    help='Which nodes the controller is allowed to place cached content on (sdn-coordinated cache-policy only).')
    p.add_argument('--sdn-cache-learn-fraction', type=float, default=0.3,
                    help='Fraction of the workload (by start time) the controller uses to learn popularity before pushing cache directives.')
    return p


def _parse_csv_ints(s: str) -> List[int]:
    return [int(x.strip()) for x in s.split(',') if x.strip()]


def _parse_csv_floats(s: str) -> List[float]:
    return [float(x.strip()) for x in s.split(',') if x.strip()]


def _parse_prefixes(s: str) -> List[str]:
    return [x.strip() for x in s.split(',') if x.strip()]


def _parse_csv_strs(s: str) -> List[str]:
    return [x.strip() for x in s.split(',') if x.strip()]


def _parse_failures(args: argparse.Namespace) -> List[FailureEventSpec]:
    if not args.fail_edge:
        return []
    u, v = [int(x.strip()) for x in args.fail_edge.split(',')]
    out = [FailureEventSpec(args.fail_time_ms, (u, v), False)]
    if args.recover_time_ms > args.fail_time_ms:
        out.append(FailureEventSpec(args.recover_time_ms, (u, v), True))
    return out


def _aggregate(rows: List[Dict[str, object]], group_keys: List[str]) -> List[ResultRow]:
    groups: Dict[tuple, List[Dict[str, object]]] = {}
    for row in rows:
        key = tuple(row[k] for k in group_keys)
        groups.setdefault(key, []).append(row)
    out: List[ResultRow] = []
    for key, bucket in groups.items():
        merged: Dict[str, object] = {k: v for k, v in zip(group_keys, key)}
        numeric_keys = [k for k, v in bucket[0].items() if isinstance(v, (int, float)) and k not in group_keys]
        for nk in numeric_keys:
            vals = [float(r[nk]) for r in bucket]
            merged[f'{nk}_mean'] = statistics.fmean(vals)
            merged[f'{nk}_std'] = statistics.pstdev(vals) if len(vals) > 1 else 0.0
        out.append(ResultRow(merged))
    return out


def _load_overrides(path: str) -> Dict[str, object]:
    with open(path, 'r', encoding='utf-8') as fh:
        return json.load(fh)


def run_sweep(args: argparse.Namespace) -> Dict[str, object]:
    """Execute the full (forwarding x cache_policy x seed x cache_size x zipf_alpha)
    sweep described by ``args`` and return raw rows, aggregated rows, and run metadata.

    This is the single source of truth for "run the simulator" -- both the CLI
    (``main``) and the web app (``webapp/app.py``) call this so results are
    guaranteed to match between the two.
    """
    topo = _make_topology(args)
    prefixes = _parse_prefixes(args.prefixes)
    catalog = make_catalog(prefixes=prefixes, items_per_prefix=args.items_per_prefix)

    consumers = _parse_csv_ints(args.consumers)
    if not consumers:
        consumers = list(range(min(args.consumer_count, topo.n - len(prefixes))))
    producer_candidates = [n for n in range(topo.n) if n not in consumers]
    producers = {prefix: producer_candidates[i % len(producer_candidates)] for i, prefix in enumerate(prefixes)}
    payload_db: Dict[str, bytes] = {name: f'payload:{name}'.encode('utf-8') for name in catalog.names}
    failures = _parse_failures(args)

    cache_sizes = _parse_csv_ints(args.cache_sizes) or [args.cache_size]
    zipf_alphas = _parse_csv_floats(args.zipf_alphas) or [args.zipf_alpha]
    seeds = _parse_csv_ints(args.seeds) or [args.seed]
    forwardings = _parse_csv_strs(args.forwardings) or [args.forwarding]
    cache_policies = _parse_csv_strs(args.cache_policies) or [args.cache_policy]

    # --architecture/--architectures is a *filter*, not a parallel sweep
    # mechanism: (system, forwarding) already fully determines the
    # architecture -- 'ndn'+'sdn-centralized' IS "NDN+SDN", 'ip'+
    # 'sdn-centralized' IS "IP+SDN" (see ip_forwarding below). Introducing a
    # second, independent axis that also decided "which architecture to
    # run" would let the two disagree and would duplicate the run_sweep
    # loop for no benefit -- rejected for that reason. Filtering the
    # existing loop keeps exactly one source of truth for what an
    # architecture *is*.
    requested_architectures = set(_parse_csv_strs(args.architectures)) if args.architectures else (
        {args.architecture} if args.architecture else None
    )
    if requested_architectures is not None and not args.forwardings:
        # The user asked for specific architectures but didn't explicitly
        # sweep 'forwarding' -- make sure the forwarding sweep actually
        # covers what's needed to produce them (SDN architectures need
        # 'sdn-centralized' in the sweep; non-SDN architectures need the
        # base --forwarding strategy). Without this, e.g. `--architecture
        # ip_sdn` with the default forwardings=['best-route'] would filter
        # every row away and silently produce an empty result.
        needed_forwardings = set()
        if 'ip' in requested_architectures or 'ndn' in requested_architectures:
            needed_forwardings.add(args.forwarding)
        if 'ip_sdn' in requested_architectures or 'ndn_sdn' in requested_architectures:
            needed_forwardings.add('sdn-centralized')
        if needed_forwardings:
            forwardings = sorted(needed_forwardings)

    raw_rows: List[Dict[str, object]] = []
    for forwarding in forwardings:
        for cache_policy in cache_policies:
            for seed in seeds:
                for cache_size in cache_sizes:
                    for zipf_alpha in zipf_alphas:
                        ndn_architecture = 'ndn_sdn' if forwarding == 'sdn-centralized' else 'ndn'
                        ip_forwarding = 'sdn-centralized' if forwarding == 'sdn-centralized' else 'distributed'
                        ip_architecture = 'ip_sdn' if ip_forwarding == 'sdn-centralized' else 'ip'
                        run_ndn = requested_architectures is None or ndn_architecture in requested_architectures
                        run_ip = requested_architectures is None or ip_architecture in requested_architectures
                        if not run_ndn and not run_ip:
                            continue

                        reqs = generate_requests(
                            catalog=catalog,
                            requests=args.requests,
                            workload=args.workload,
                            seed=seed,
                            zipf_alpha=zipf_alpha,
                            consumers=consumers,
                            interest_iat_ms=args.interest_iat_ms,
                        )
                        ndn_cfg = NDNConfig(
                            cache_size=cache_size,
                            cache_policy=cache_policy,
                            producers=producers,
                            forwarding=forwarding,
                            link_delay_ms=args.link_delay_ms,
                            processing_delay_ms=args.processing_delay_ms,
                            interest_iat_ms=args.interest_iat_ms,
                            interest_lifetime_ms=args.interest_lifetime_ms,
                            max_retransmissions=args.max_retransmissions,
                            pit_capacity=args.pit_capacity,
                            cache_admit_threshold=args.cache_admit_threshold,
                            cache_ttl_ms=(args.cache_ttl_ms if args.cache_ttl_ms > 0 else None),
                            verify_data=args.verify_data,
                            verification_delay_ms=args.verification_delay_ms,
                            invalid_data_rate=args.invalid_data_rate,
                            controller_rtt_ms=args.controller_rtt_ms,
                            sdn_cache_top_k=args.sdn_cache_top_k,
                            sdn_cache_placement=args.sdn_cache_placement,
                            sdn_cache_learn_fraction=args.sdn_cache_learn_fraction,
                        )
                        ip_cfg = IPConfig(
                            producers=producers,
                            link_delay_ms=args.link_delay_ms,
                            processing_delay_ms=args.processing_delay_ms,
                            forwarding=ip_forwarding,
                            controller_rtt_ms=args.controller_rtt_ms,
                        )

                        common = {
                            'topology': args.topology if not args.topology_file else 'file',
                            'nodes': topo.n,
                            'cache_policy': cache_policy,
                            'cache_size': cache_size,
                            'workload': args.workload,
                            'requests': args.requests,
                            'items_per_prefix': args.items_per_prefix,
                            'zipf_alpha': zipf_alpha,
                            'seed': seed,
                            'forwarding': forwarding,
                            'consumer_count': len(consumers),
                            'producer_count': len(producers),
                            'link_delay_ms': args.link_delay_ms,
                            'bandwidth_mbps': args.bandwidth_mbps,
                            'queue_limit_packets': args.queue_limit_packets,
                            'pit_capacity': args.pit_capacity,
                            'interest_lifetime_ms': args.interest_lifetime_ms,
                            'failed_edge': args.fail_edge,
                            'controller_rtt_ms': args.controller_rtt_ms,
                            'sdn_cache_top_k': args.sdn_cache_top_k,
                        }
                        if run_ndn:
                            ndn_stats = ndn_fetch_event_driven(topo=_make_topology(args), cfg=ndn_cfg, requests=reqs, payload_db=payload_db, seed=seed, failures=failures)
                            raw_rows.append({**common, 'system': 'ndn', 'architecture': ndn_architecture, **ndn_stats.as_dict()})
                        if run_ip:
                            ip_stats = ip_fetch_event_driven(topo=_make_topology(args), cfg=ip_cfg, requests=reqs, payload_db=payload_db, failures=failures, seed=seed)
                            raw_rows.append({**common, 'system': 'ip', 'architecture': ip_architecture, **ip_stats.as_dict()})

    agg = _aggregate(raw_rows, ['system', 'architecture', 'topology', 'nodes', 'cache_policy', 'cache_size', 'workload', 'zipf_alpha', 'forwarding', 'consumer_count', 'producer_count', 'link_delay_ms', 'bandwidth_mbps', 'queue_limit_packets', 'pit_capacity', 'interest_lifetime_ms', 'failed_edge', 'controller_rtt_ms', 'sdn_cache_top_k'])

    # Optional packet-trace capture for the dashboard's visualization --
    # gated behind an attribute the CLI's own argparse Namespace never
    # sets, so `main()`/the CLI and every pre-existing caller are
    # completely unaffected unless a caller (webapp/app.py) explicitly
    # opts in by setting `args.capture_trace = True` first. Reuses the
    # topology/catalog/producers/payload_db already built above rather
    # than rebuilding scenario state, and runs a small, separately-capped
    # request count purely for animation -- it does not feed into
    # raw_rows/summary_rows and has zero effect on any reported metric.
    traces: Dict[str, dict] = {}
    if getattr(args, 'capture_trace', False):
        TRACE_MAX_REQUESTS = 40
        trace_requested = requested_architectures if requested_architectures is not None else {'ip', 'ndn'}
        trace_forwarding = forwardings[0]
        trace_reqs = generate_requests(
            catalog=catalog, requests=min(args.requests, TRACE_MAX_REQUESTS), workload=args.workload,
            seed=seeds[0], zipf_alpha=zipf_alphas[0], consumers=consumers, interest_iat_ms=args.interest_iat_ms,
        )
        for arch in ('ip', 'ip_sdn', 'ndn', 'ndn_sdn'):
            if arch not in trace_requested:
                continue
            events: List[Dict[str, object]] = []
            if arch in ('ndn', 'ndn_sdn'):
                fwd = 'sdn-centralized' if arch == 'ndn_sdn' else trace_forwarding
                if arch == 'ndn' and fwd == 'sdn-centralized':
                    continue  # can't represent plain "ndn" with sdn-centralized forwarding
                trace_cfg = NDNConfig(
                    cache_size=cache_sizes[0], cache_policy=cache_policies[0], producers=producers,
                    forwarding=fwd, link_delay_ms=args.link_delay_ms, processing_delay_ms=args.processing_delay_ms,
                    interest_iat_ms=args.interest_iat_ms, interest_lifetime_ms=args.interest_lifetime_ms,
                    max_retransmissions=args.max_retransmissions, pit_capacity=args.pit_capacity,
                    cache_admit_threshold=args.cache_admit_threshold,
                    cache_ttl_ms=(args.cache_ttl_ms if args.cache_ttl_ms > 0 else None),
                    verify_data=args.verify_data, verification_delay_ms=args.verification_delay_ms,
                    invalid_data_rate=args.invalid_data_rate, controller_rtt_ms=args.controller_rtt_ms,
                    sdn_cache_top_k=args.sdn_cache_top_k, sdn_cache_placement=args.sdn_cache_placement,
                    sdn_cache_learn_fraction=args.sdn_cache_learn_fraction,
                )
                ndn_fetch_event_driven(topo=_make_topology(args), cfg=trace_cfg, requests=trace_reqs,
                                        payload_db=payload_db, seed=seeds[0], failures=failures,
                                        on_event=events.append)
            else:
                fwd = 'sdn-centralized' if arch == 'ip_sdn' else 'distributed'
                trace_cfg = IPConfig(producers=producers, link_delay_ms=args.link_delay_ms,
                                      processing_delay_ms=args.processing_delay_ms, forwarding=fwd,
                                      controller_rtt_ms=args.controller_rtt_ms)
                ip_fetch_event_driven(topo=_make_topology(args), cfg=trace_cfg, requests=trace_reqs,
                                       payload_db=payload_db, failures=failures, seed=seeds[0],
                                       on_event=events.append)
            traces[arch] = {'events': events, 'requests_captured': len(trace_reqs)}

    meta = {
        'topology': args.topology,
        'nodes': topo.n,
        'consumers': consumers,
        'producers': producers,
        'seeds': seeds,
        'cache_sizes': cache_sizes,
        'zipf_alphas': zipf_alphas,
        'forwardings': forwardings,
        'cache_policies': cache_policies,
        'failures': [f.__dict__ for f in failures],
    }
    return {'raw_rows': raw_rows, 'summary_rows': agg, 'meta': meta, 'traces': traces}


def main(argv: List[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.config:
        cfg = _load_overrides(args.config)
        parser.set_defaults(**cfg)
        args = parser.parse_args(argv)
    ensure_out_dir(args.out_dir)

    result = run_sweep(args)
    raw_rows = result['raw_rows']
    agg = result['summary_rows']
    meta = result['meta']

    write_csv(os.path.join(args.out_dir, 'results_raw.csv'), [ResultRow(r) for r in raw_rows])
    write_csv(os.path.join(args.out_dir, 'results_summary.csv'), agg)

    with open(os.path.join(args.out_dir, 'run_config.json'), 'w', encoding='utf-8') as fh:
        json.dump(meta, fh, indent=2)

    print('Wrote:')
    print(f"  {os.path.join(args.out_dir, 'results_raw.csv')}")
    print(f"  {os.path.join(args.out_dir, 'results_summary.csv')}")
    return 0



if __name__ == '__main__':
    raise SystemExit(main())
