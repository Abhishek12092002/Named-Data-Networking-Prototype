from __future__ import annotations

import argparse
import csv
import os
from collections import defaultdict
from dataclasses import dataclass
from typing import DefaultDict, Dict, Iterable, List, Tuple

import matplotlib.pyplot as plt


@dataclass(frozen=True)
class Row:
    system: str
    cache_size: int
    zipf_alpha: float
    avg_latency_hops: float
    avg_latency_ms: float
    cache_hit_ratio: float
    interest_tx: float
    data_tx: float


def _read_rows(path: str) -> List[Row]:
    rows: List[Row] = []
    with open(path, newline="") as f:
        r = csv.DictReader(f)
        for d in r:
            rows.append(
                Row(
                    system=str(d["system"]),
                    cache_size=int(float(d.get("cache_size", 0) or 0)),
                    zipf_alpha=float(d.get("zipf_alpha", 0.0) or 0.0),
                    avg_latency_hops=float(d.get("avg_latency_hops", 0.0) or 0.0),
                    avg_latency_ms=float(d.get("avg_latency_ms", 0.0) or 0.0),
                    cache_hit_ratio=float(d.get("cache_hit_ratio", 0.0) or 0.0),
                    interest_tx=float(d.get("interest_tx", 0.0) or 0.0),
                    data_tx=float(d.get("data_tx", 0.0) or 0.0),
                )
            )
    return rows


def _group_by(rows: Iterable[Row]) -> Dict[Tuple[str, float], List[Row]]:
    g: DefaultDict[Tuple[str, float], List[Row]] = defaultdict(list)
    for r in rows:
        if r.system == "ip":
            g[(r.system, -1.0)].append(r)
        else:
            g[(r.system, r.zipf_alpha)].append(r)
    for k in list(g.keys()):
        g[k] = sorted(g[k], key=lambda x: x.cache_size)
    return dict(g)


def _plot_metric(
    groups: Dict[Tuple[str, float], List[Row]],
    out_dir: str,
    metric: str,
    ylabel: str,
    filename: str,
) -> None:
    plt.figure()
    for (system, alpha), rs in sorted(groups.items(), key=lambda x: (x[0][0], x[0][1])):
        x = [r.cache_size for r in rs if system == r.system]
        y = [getattr(r, metric) for r in rs if system == r.system]
        if system == "ip":
            label = "ip"
            plt.plot(x, y, marker="o", linestyle="--", linewidth=2.0, label=label)
        else:
            label = f"{system} (alpha={alpha})"
            plt.plot(x, y, marker="o", label=label)

    plt.xlabel("cache_size")
    plt.ylabel(ylabel)
    plt.grid(True, alpha=0.3)
    plt.legend()
    os.makedirs(out_dir, exist_ok=True)
    plt.tight_layout()
    plt.savefig(os.path.join(out_dir, filename), dpi=200)
    plt.close()


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Plot graphs from out/results.csv")
    ap.add_argument("--csv", default=os.path.join("out", "results.csv"))
    ap.add_argument("--out", default=os.path.join("out", "plots"))
    args = ap.parse_args(argv)

    rows = _read_rows(args.csv)
    if not rows:
        raise SystemExit(f"no rows found in {args.csv}")

    groups = _group_by(rows)

    _plot_metric(groups, args.out, "cache_hit_ratio", "cache_hit_ratio", "hit_ratio_vs_cache.png")
    _plot_metric(groups, args.out, "avg_latency_hops", "avg_latency_hops", "latency_hops_vs_cache.png")
    _plot_metric(groups, args.out, "avg_latency_ms", "avg_latency_ms", "latency_ms_vs_cache.png")

    # Traffic proxy
    plt.figure()
    traffic_groups: Dict[Tuple[str, float], List[Tuple[int, float]]] = {}
    for (system, alpha), rs in groups.items():
        traffic_groups[(system, alpha)] = [(r.cache_size, (r.interest_tx + r.data_tx)) for r in rs]

    for (system, alpha), pts in sorted(traffic_groups.items(), key=lambda x: (x[0][0], x[0][1])):
        x = [p[0] for p in pts]
        y = [p[1] for p in pts]
        if system == "ip":
            plt.plot(x, y, marker="o", linestyle="--", linewidth=2.0, label="ip")
        else:
            plt.plot(x, y, marker="o", label=f"{system} (alpha={alpha})")

    plt.xlabel("cache_size")
    plt.ylabel("interest_tx + data_tx")
    plt.grid(True, alpha=0.3)
    plt.legend()
    os.makedirs(args.out, exist_ok=True)
    plt.tight_layout()
    plt.savefig(os.path.join(args.out, "traffic_vs_cache.png"), dpi=200)
    plt.close()

    print(f"Wrote plots to: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
