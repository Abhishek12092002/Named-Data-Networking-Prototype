from __future__ import annotations

import argparse
import os
import pandas as pd
import matplotlib.pyplot as plt


def plot_all(csv_path: str, out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    df = pd.read_csv(csv_path)
    if df.empty:
        return
    metrics = [
        ('avg_latency_ms_mean', 'Latency (ms)'),
        ('cache_hit_ratio_mean', 'Cache hit ratio'),
        ('interest_tx_mean', 'Interest transmissions'),
        ('data_tx_mean', 'Data transmissions'),
    ]
    for metric, ylabel in metrics:
        if metric not in df.columns:
            continue
        plt.figure(figsize=(8, 5))
        for (system, forwarding), g in df.groupby(['system', 'forwarding']):
            g = g.sort_values('cache_size')
            yerr = g[metric.replace('_mean', '_std')] if metric.replace('_mean', '_std') in g.columns else None
            plt.errorbar(g['cache_size'], g[metric], yerr=yerr, marker='o', capsize=3, label=f'{system}-{forwarding}')
        plt.xlabel('Cache size')
        plt.ylabel(ylabel)
        plt.grid(True, alpha=0.3)
        plt.legend()
        plt.tight_layout()
        plt.savefig(os.path.join(out_dir, f'{metric}.png'), dpi=160)
        plt.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--csv', default='out/demo_mesh/results_summary.csv')
    ap.add_argument('--out-dir', default='out/demo_mesh/plots')
    args = ap.parse_args()
    plot_all(args.csv, args.out_dir)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
