from __future__ import annotations

import argparse
import json
import os
import statistics
from dataclasses import dataclass
from typing import Dict, Iterable, List

from .ip_engine import IPConfig, ip_fetch_event_driven
from .ndn_engine import FailureEventSpec, NDNConfig, ndn_fetch_event_driven
from .results import ResultRow, ensure_out_dir, write_csv
from .topology import Link, Topology, load_edge_list, make_line, make_mesh, make_tree
from .workload import DEFAULT_PREFIXES, generate_requests, make_catalog


DEFAULT_PRODUCER_MAP = {'/video': 0, '/news': 1, '/iot': 2}


def _make_topology(args: argparse.Namespace) -> Topology:
    link = Link(args.link_delay_ms, args.bandwidth_mbps, args.queue_limit_packets, True)
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
    p.add_argument('--cache-policy', choices=['lru', 'fifo', 'lfu', 'random'], default='lfu')
    p.add_argument('--forwarding', choices=['best-route', 'flooding', 'probabilistic'], default='best-route')
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
    p.add_argument('--link-delay-ms', type=float, default=8.0)
    p.add_argument('--bandwidth-mbps', type=float, default=25.0)
    p.add_argument('--queue-limit-packets', type=int, default=64)
    p.add_argument('--processing-delay-ms', type=float, default=1.0)
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
    return p


def _parse_csv_ints(s: str) -> List[int]:
    return [int(x.strip()) for x in s.split(',') if x.strip()]


def _parse_csv_floats(s: str) -> List[float]:
    return [float(x.strip()) for x in s.split(',') if x.strip()]


def _parse_prefixes(s: str) -> List[str]:
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


def main(argv: List[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.config:
        cfg = _load_overrides(args.config)
        parser.set_defaults(**cfg)
        args = parser.parse_args(argv)
    ensure_out_dir(args.out_dir)

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

    raw_rows: List[Dict[str, object]] = []
    for seed in seeds:
        for cache_size in cache_sizes:
            for zipf_alpha in zipf_alphas:
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
                    cache_policy=args.cache_policy,
                    producers=producers,
                    forwarding=args.forwarding,
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
                )
                ip_cfg = IPConfig(
                    producers=producers,
                    link_delay_ms=args.link_delay_ms,
                    processing_delay_ms=args.processing_delay_ms,
                )

                ndn_stats = ndn_fetch_event_driven(topo=_make_topology(args), cfg=ndn_cfg, requests=reqs, payload_db=payload_db, seed=seed, failures=failures)
                ip_stats = ip_fetch_event_driven(topo=_make_topology(args), cfg=ip_cfg, requests=reqs, payload_db=payload_db, failures=failures)

                common = {
                    'topology': args.topology if not args.topology_file else 'file',
                    'nodes': topo.n,
                    'cache_policy': args.cache_policy,
                    'cache_size': cache_size,
                    'workload': args.workload,
                    'requests': args.requests,
                    'items_per_prefix': args.items_per_prefix,
                    'zipf_alpha': zipf_alpha,
                    'seed': seed,
                    'forwarding': args.forwarding,
                    'consumer_count': len(consumers),
                    'producer_count': len(producers),
                    'link_delay_ms': args.link_delay_ms,
                    'bandwidth_mbps': args.bandwidth_mbps,
                    'queue_limit_packets': args.queue_limit_packets,
                    'pit_capacity': args.pit_capacity,
                    'interest_lifetime_ms': args.interest_lifetime_ms,
                    'failed_edge': args.fail_edge,
                }
                raw_rows.append({**common, 'system': 'ndn', **ndn_stats.as_dict()})
                raw_rows.append({**common, 'system': 'ip', **ip_stats.as_dict()})

    write_csv(os.path.join(args.out_dir, 'results_raw.csv'), [ResultRow(r) for r in raw_rows])
    agg = _aggregate(raw_rows, ['system', 'topology', 'nodes', 'cache_policy', 'cache_size', 'workload', 'zipf_alpha', 'forwarding', 'consumer_count', 'producer_count', 'link_delay_ms', 'bandwidth_mbps', 'queue_limit_packets', 'pit_capacity', 'interest_lifetime_ms', 'failed_edge'])
    write_csv(os.path.join(args.out_dir, 'results_summary.csv'), agg)

    # Per-node analytics for the final NDN run.
    with open(os.path.join(args.out_dir, 'run_config.json'), 'w', encoding='utf-8') as fh:
        json.dump({
            'topology': args.topology,
            'nodes': topo.n,
            'consumers': consumers,
            'producers': producers,
            'seeds': seeds,
            'cache_sizes': cache_sizes,
            'zipf_alphas': zipf_alphas,
            'failures': [f.__dict__ for f in failures],
        }, fh, indent=2)

    print('Wrote:')
    print(f"  {os.path.join(args.out_dir, 'results_raw.csv')}")
    print(f"  {os.path.join(args.out_dir, 'results_summary.csv')}")
    return 0



if __name__ == '__main__':
    raise SystemExit(main())
