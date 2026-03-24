from __future__ import annotations

import argparse
import os
from dataclasses import asdict
from typing import Dict, List

from .ip_engine import IPConfig, ip_fetch_sequential
from .ndn_engine import NDNConfig, ndn_fetch_sequential
from .results import ResultRow, ensure_out_dir, write_csv
from .topology import Topology, make_line, make_mesh, make_tree
from .workload import generate_requests, make_catalog


def _make_topology(args: argparse.Namespace) -> Topology:
    if args.topology == "line":
        return make_line(args.nodes)
    if args.topology == "tree":
        return make_tree(args.nodes, branching=args.branching)
    if args.topology == "mesh":
        return make_mesh(args.nodes, extra_edges=args.extra_edges, seed=args.seed)
    raise ValueError(f"unknown topology: {args.topology}")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="NDN prototype vs IP baseline experiment runner")

    p.add_argument("--topology", choices=["line", "tree", "mesh"], default="line")
    p.add_argument("--nodes", type=int, default=8)
    p.add_argument("--branching", type=int, default=2)
    p.add_argument("--extra-edges", type=int, default=4)

    p.add_argument("--cache-size", type=int, default=25)
    p.add_argument("--cache-sizes", default="")
    p.add_argument("--cache-policy", choices=["lru", "fifo"], default="lru")

    p.add_argument("--workload", choices=["uniform", "zipf"], default="zipf")
    p.add_argument("--requests", type=int, default=200)
    p.add_argument("--catalog-items", type=int, default=100)
    p.add_argument("--zipf-alpha", type=float, default=1.2)
    p.add_argument("--zipf-alphas", default="")

    p.add_argument("--link-delay-ms", type=float, default=10.0)
    p.add_argument("--processing-delay-ms", type=float, default=1.0)
    p.add_argument("--interest-iat-ms", type=float, default=1.0)

    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--out-dir", default="out")

    return p


def _parse_csv_ints(s: str) -> List[int]:
    t = s.strip()
    if not t:
        return []
    return [int(x.strip()) for x in t.split(",") if x.strip()]


def _parse_csv_floats(s: str) -> List[float]:
    t = s.strip()
    if not t:
        return []
    return [float(x.strip()) for x in t.split(",") if x.strip()]


def main(argv: List[str] | None = None) -> int:
    p = build_arg_parser()
    args = p.parse_args(argv)

    topo = _make_topology(args)

    consumer = 0
    producer = topo.n - 1
    prefix = "/video"

    catalog = make_catalog(prefix=prefix, items=args.catalog_items)
    payload_db: Dict[str, bytes] = {name: f"payload:{name}".encode("utf-8") for name in catalog.names}

    cache_sizes = _parse_csv_ints(args.cache_sizes) or [args.cache_size]
    zipf_alphas = _parse_csv_floats(args.zipf_alphas) or [args.zipf_alpha]

    rows: List[ResultRow] = []

    for cache_size in cache_sizes:
        for zipf_alpha in zipf_alphas:
            reqs = generate_requests(
                catalog=catalog,
                requests=args.requests,
                workload=args.workload,
                seed=args.seed,
                zipf_alpha=zipf_alpha,
            )

            ndn_cfg = NDNConfig(
                cache_size=cache_size,
                cache_policy=args.cache_policy,
                producer_node=producer,
                name_prefix=prefix,
                link_delay_ms=args.link_delay_ms,
                processing_delay_ms=args.processing_delay_ms,
                interest_iat_ms=args.interest_iat_ms,
            )
            ip_cfg = IPConfig(
                server_node=producer,
                link_delay_ms=args.link_delay_ms,
                processing_delay_ms=args.processing_delay_ms,
            )

            ndn_stats = ndn_fetch_sequential(
                topo=topo,
                cfg=ndn_cfg,
                requests=reqs,
                payload_db=payload_db,
                consumer_node=consumer,
                seed=args.seed,
            )

            ip_stats = ip_fetch_sequential(
                topo=topo,
                cfg=ip_cfg,
                requests=reqs,
                payload_db=payload_db,
                client_node=consumer,
            )

            row_ndn = {
                "system": "ndn",
                "topology": args.topology,
                "nodes": args.nodes,
                "cache_policy": args.cache_policy,
                "cache_size": cache_size,
                "workload": args.workload,
                "requests": args.requests,
                "catalog_items": args.catalog_items,
                "zipf_alpha": zipf_alpha,
                "seed": args.seed,
                "link_delay_ms": args.link_delay_ms,
                "processing_delay_ms": args.processing_delay_ms,
                "interest_iat_ms": args.interest_iat_ms,
                **ndn_stats.as_dict(),
            }
            row_ip = {
                "system": "ip",
                "topology": args.topology,
                "nodes": args.nodes,
                "cache_policy": "none",
                "cache_size": cache_size,
                "workload": args.workload,
                "requests": args.requests,
                "catalog_items": args.catalog_items,
                "zipf_alpha": zipf_alpha,
                "seed": args.seed,
                "link_delay_ms": args.link_delay_ms,
                "processing_delay_ms": args.processing_delay_ms,
                "interest_iat_ms": args.interest_iat_ms,
                **ip_stats.as_dict(),
            }

            edge_delay = float(args.link_delay_ms) + float(args.processing_delay_ms)
            row_ndn["avg_latency_ms_from_hops"] = float(row_ndn["avg_latency_hops"]) * edge_delay
            row_ip["avg_latency_ms_from_hops"] = float(row_ip["avg_latency_hops"]) * edge_delay
            row_ndn["avg_latency_hops_from_ms"] = (float(row_ndn["avg_latency_ms"]) / edge_delay) if edge_delay else 0.0
            row_ip["avg_latency_hops_from_ms"] = (float(row_ip["avg_latency_ms"]) / edge_delay) if edge_delay else 0.0

            rows.append(ResultRow(row_ndn))
            rows.append(ResultRow(row_ip))

    ensure_out_dir(args.out_dir)
    out_csv = os.path.join(args.out_dir, "results.csv")
    write_csv(out_csv, rows)

    print("=== Summary ===")
    print(f"Topology: {args.topology}  Nodes: {args.nodes}")
    print(f"Workload: {args.workload}  Requests: {args.requests}")
    print(f"Wrote: {out_csv}")

    return 0
