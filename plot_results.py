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
        ('control_messages_tx_mean', 'SDN control messages sent'),
        ('avg_reconvergence_ms_mean', 'FIB reconvergence delay (ms)'),
    ]
    group_cols = [c for c in ('system', 'forwarding', 'cache_policy') if c in df.columns]
    for metric, ylabel in metrics:
        if metric not in df.columns:
            continue
        if df[metric].fillna(0).eq(0).all():
            continue  # nothing to show (e.g. no SDN run in this CSV)
        plt.figure(figsize=(8, 5))
        for key, g in df.groupby(group_cols):
            label = key if isinstance(key, str) else '-'.join(str(k) for k in key)
            g = g.sort_values('cache_size')
            std_col = metric.replace('_mean', '_std')
            yerr = g[std_col] if std_col in g.columns else None
            plt.errorbar(g['cache_size'], g[metric], yerr=yerr, marker='o', capsize=3, label=label)
        plt.xlabel('Cache size')
        plt.ylabel(ylabel)
        plt.grid(True, alpha=0.3)
        plt.legend(fontsize=8)
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
