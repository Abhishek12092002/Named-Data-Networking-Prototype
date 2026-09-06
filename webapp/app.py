"""
Web dashboard for the NDN/SDN simulator.

Runs entirely in-process against ``ndn_sim`` -- no shelling out to the CLI,
no files written to disk. A form collects topology/workload/SDN parameters,
``ndn_sim.cli.run_sweep`` executes the simulation, and the results are
rendered back as a table, a topology diagram (server-rendered SVG), and a
set of matplotlib plots (embedded as base64 PNGs).

Run with:
    cd ndn_final
    pip install -r requirements.txt
    python webapp/app.py
then open http://127.0.0.1:5000
"""

from __future__ import annotations

import base64
import io
import math
import os
import sys
import time
from typing import Dict, List, Tuple

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

from flask import Flask, jsonify, render_template, request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ndn_sim.cli import build_arg_parser, run_sweep  # noqa: E402
from ndn_sim.core.network_profiles import PROFILES  # noqa: E402
from ndn_sim.results import ResultRow  # noqa: E402
from webapp.jobs import job_manager  # noqa: E402

app = Flask(__name__)

# Safety limits so a browser form can't accidentally kick off a huge sweep.
MAX_NODES = int(os.environ.get('SIM_MAX_NODES', 40))
MAX_REQUESTS = int(os.environ.get('SIM_MAX_REQUESTS', 1500))
MAX_COMBOS = int(os.environ.get('SIM_MAX_COMBOS', 24))

FORWARDING_CHOICES = ['best-route', 'flooding', 'probabilistic', 'sdn-centralized']
CACHE_POLICY_CHOICES = ['lru', 'fifo', 'lfu', 'random', 'sdn-coordinated']
TOPOLOGY_CHOICES = ['mesh', 'line', 'tree']
WORKLOAD_CHOICES = ['uniform', 'zipf', 'bursty', 'shifted']
PLACEMENT_CHOICES = ['near-producer', 'near-consumer', 'hybrid']
ARCHITECTURE_CHOICES = ['ip', 'ip_sdn', 'ndn', 'ndn_sdn']
ARCHITECTURE_LABELS = {
    'ip': 'IP', 'ip_sdn': 'IP + SDN', 'ndn': 'NDN', 'ndn_sdn': 'NDN + SDN',
}
NETWORK_PROFILE_CHOICES = [''] + sorted(PROFILES)

COLORS = {
    'ndn': '#4FD1C5',
    'ip': '#E8935A',
    'accent': '#E85D75',
    'grid': '#2B3752',
}


def _defaults() -> Dict[str, object]:
    parser = build_arg_parser()
    ns = parser.parse_args([])
    return vars(ns)


def _parse_csv(s: str, caster):
    return [caster(x.strip()) for x in s.split(',') if x.strip()]


def _build_args(form) -> 'object':
    parser = build_arg_parser()
    args = parser.parse_args([])

    def s(name, default):
        v = form.get(name, '')
        return v.strip() if v and v.strip() else default

    def i(name, default):
        v = form.get(name, '')
        return int(v) if v and v.strip() else default

    def f(name, default):
        v = form.get(name, '')
        return float(v) if v and v.strip() else default

    args.topology = s('topology', args.topology)
    args.nodes = min(max(i('nodes', args.nodes), 3), MAX_NODES)
    args.extra_edges = max(0, i('extra_edges', args.extra_edges))
    args.branching = max(1, i('branching', args.branching))
    args.cache_size = max(0, i('cache_size', args.cache_size))
    args.cache_sizes = s('cache_sizes', '')
    args.cache_policy = s('cache_policy', args.cache_policy)
    args.cache_policies = s('cache_policies', '')
    args.forwarding = s('forwarding', args.forwarding)
    args.forwardings = s('forwardings', '')
    args.workload = s('workload', args.workload)
    args.requests = min(max(i('requests', args.requests), 10), MAX_REQUESTS)
    args.items_per_prefix = max(1, i('items_per_prefix', args.items_per_prefix))
    args.zipf_alpha = f('zipf_alpha', args.zipf_alpha)
    args.zipf_alphas = s('zipf_alphas', '')
    args.consumer_count = max(1, i('consumer_count', args.consumer_count))
    args.network_profile = s('network_profile', '')
    args.link_delay_ms = max(0.1, f('link_delay_ms', args.link_delay_ms))
    args.bandwidth_mbps = max(0.1, f('bandwidth_mbps', args.bandwidth_mbps))
    args.queue_limit_packets = max(1, i('queue_limit_packets', args.queue_limit_packets))
    args.link_processing_delay_ms = max(0.0, f('link_processing_delay_ms', args.link_processing_delay_ms))
    args.loss_probability = min(max(f('loss_probability', args.loss_probability), 0.0), 1.0)
    args.interest_lifetime_ms = max(1.0, f('interest_lifetime_ms', args.interest_lifetime_ms))
    args.pit_capacity = max(1, i('pit_capacity', args.pit_capacity))
    args.fail_edge = s('fail_edge', '')
    args.fail_time_ms = f('fail_time_ms', args.fail_time_ms)
    args.recover_time_ms = f('recover_time_ms', args.recover_time_ms)
    args.seed = i('seed', args.seed)
    args.seeds = s('seeds', '')
    args.controller_rtt_ms = max(0.0, f('controller_rtt_ms', args.controller_rtt_ms))
    args.sdn_cache_top_k = max(0, i('sdn_cache_top_k', args.sdn_cache_top_k))
    args.sdn_cache_placement = s('sdn_cache_placement', args.sdn_cache_placement)
    args.sdn_cache_learn_fraction = min(max(f('sdn_cache_learn_fraction', args.sdn_cache_learn_fraction), 0.05), 0.95)

    # Architecture checkboxes: request.form supports getlist for repeated
    # keys; a plain dict (as used by some tests) does not, so fall back to
    # a single value. Empty selection = run the full IP/IP+SDN/NDN/NDN+SDN
    # comparison via cli.run_sweep's own default (no filter applied).
    if hasattr(form, 'getlist'):
        selected = form.getlist('architectures')
    else:
        v = form.get('architectures', '')
        selected = [x.strip() for x in v.split(',') if x.strip()]
    args.architectures = ','.join(selected)
    args.architecture = ''
    return args


def _estimate_combos(args) -> int:
    def n(csv_val, single):
        vals = _parse_csv(csv_val, str) if csv_val else []
        return len(vals) if vals else 1

    forwarding_n = n(args.forwardings, args.forwarding)
    if not args.forwardings and getattr(args, 'architectures', ''):
        # Mirrors cli.run_sweep's own auto-expansion of the forwarding
        # sweep to cover whichever architectures were requested (see
        # run_sweep's `needed_forwardings` logic) -- kept in sync so the
        # web demo's combo-count safety check isn't an undercount.
        archs = set(_parse_csv(args.architectures, str))
        needs_base = bool(archs & {'ip', 'ndn'})
        needs_sdn = bool(archs & {'ip_sdn', 'ndn_sdn'})
        forwarding_n = max((1 if needs_base else 0) + (1 if needs_sdn else 0), 1)

    return (
        n(args.cache_sizes, args.cache_size)
        * n(args.zipf_alphas, args.zipf_alpha)
        * n(args.seeds, args.seed)
        * forwarding_n
        * n(args.cache_policies, args.cache_policy)
    )


def _fig_to_base64() -> str:
    buf = io.BytesIO()
    plt.tight_layout()
    plt.savefig(buf, format='png', dpi=150, facecolor='#0B1220')
    plt.close()
    buf.seek(0)
    return base64.b64encode(buf.read()).decode('ascii')


def _style_axes(ax):
    ax.set_facecolor('#0B1220')
    ax.tick_params(colors='#AEB8CC')
    ax.xaxis.label.set_color('#E6EAF2')
    ax.yaxis.label.set_color('#E6EAF2')
    ax.title.set_color('#E6EAF2')
    for spine in ax.spines.values():
        spine.set_color(COLORS['grid'])
    ax.grid(True, alpha=0.25, color=COLORS['grid'])


def _make_plots(summary_rows: List[ResultRow]) -> Dict[str, str]:
    if not summary_rows:
        return {}
    rows = [r.fields for r in summary_rows]
    cache_sizes = sorted({r['cache_size'] for r in rows})
    multi_cache = len(cache_sizes) > 1

    def group_key(r):
        return (r['system'], r['forwarding'], r['cache_policy'])

    groups: Dict[tuple, List[dict]] = {}
    for r in rows:
        groups.setdefault(group_key(r), []).append(r)

    metrics = [
        ('avg_latency_ms_mean', 'Avg latency (ms)'),
        ('cache_hit_ratio_mean', 'Cache hit ratio'),
        ('interest_tx_mean', 'Interest transmissions'),
        ('control_messages_tx_mean', 'SDN control messages'),
        ('avg_reconvergence_ms_mean', 'FIB reconvergence delay (ms)'),
    ]
    plots: Dict[str, str] = {}
    plt.rcParams.update({'font.size': 10})

    for metric, label in metrics:
        if not any(metric in r for r in rows):
            continue
        if all(float(r.get(metric, 0.0)) == 0.0 for r in rows):
            continue

        fig, ax = plt.subplots(figsize=(7, 4.2))
        _style_axes(ax)

        if multi_cache:
            for key, bucket in groups.items():
                bucket = sorted(bucket, key=lambda r: r['cache_size'])
                xs = [r['cache_size'] for r in bucket]
                ys = [float(r.get(metric, 0.0)) for r in bucket]
                stds = [float(r.get(metric.replace('_mean', '_std'), 0.0)) for r in bucket]
                system, fwd, cp = key
                linestyle = '--' if 'sdn' in fwd or 'sdn' in cp else '-'
                base_color = COLORS['ndn'] if system == 'ndn' else COLORS['ip']
                ax.errorbar(xs, ys, yerr=stds, marker='o', capsize=3, color=base_color,
                             linestyle=linestyle, label=f'{system}/{fwd}/{cp}', alpha=0.9)
            ax.set_xlabel('Cache size')
        else:
            keys = list(groups.keys())
            xs = list(range(len(keys)))
            ys = [float(bucket[0].get(metric, 0.0)) for bucket in groups.values()]
            colors = [COLORS['ndn'] if k[0] == 'ndn' else COLORS['ip'] for k in keys]
            ax.bar(xs, ys, color=colors, width=0.55)
            ax.set_xticks(xs)
            ax.set_xticklabels([f'{k[0]}\n{k[1]}\n{k[2]}' for k in keys], fontsize=8)

        ax.set_ylabel(label)
        ax.set_title(label)
        if multi_cache:
            leg = ax.legend(fontsize=7, facecolor='#121A2B', edgecolor=COLORS['grid'])
            for text in leg.get_texts():
                text.set_color('#E6EAF2')
        plots[metric] = _fig_to_base64()

    return plots


def _node_positions(n: int, width: int, height: int) -> Dict[int, Tuple[float, float]]:
    """Circular node layout, shared by the static SVG topology diagram and
    the canvas packet-animation player so a packet drawn "leaving node 3"
    lands exactly where node 3's marker is drawn in the SVG -- one layout
    algorithm, not two that could drift apart.
    """
    cx, cy = width / 2, height / 2 - 6
    r = min(width, height) / 2 - 62
    return {
        i: (cx + r * math.cos(2 * math.pi * i / n - math.pi / 2),
            cy + r * math.sin(2 * math.pi * i / n - math.pi / 2))
        for i in range(n)
    }


def _topology_svg(topo, producers: Dict[str, int], consumers: List[int], show_controller: bool,
                   width: int = 560, height: int = 420) -> str:
    """Server-rendered SVG topology diagram.

    Node roles are distinguished by SHAPE, not just color (square =
    producer, triangle = consumer, hexagon = SDN controller, circle =
    ordinary relay), so the diagram stays legible without relying on color
    perception alone.
    """
    n = topo.n
    pos = _node_positions(n, width, height)
    cx, cy = width / 2, height / 2 - 6

    edge_svgs = []
    seen = set()
    for (u, v) in topo.links:
        if (v, u) in seen:
            continue
        seen.add((u, v))
        x1, y1 = pos[u]
        x2, y2 = pos[v]
        edge_svgs.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
                          f'stroke="#2B3752" stroke-width="1.4" />')

    def _square(x, y, size, fill):
        h = size
        return (f'<rect x="{x - h:.1f}" y="{y - h:.1f}" width="{2 * h:.1f}" height="{2 * h:.1f}" '
                f'rx="2.5" fill="{fill}" stroke="#0B1220" stroke-width="2"/>')

    def _triangle(x, y, size, fill):
        pts = f'{x:.1f},{y - size:.1f} {x - size:.1f},{y + size * 0.8:.1f} {x + size:.1f},{y + size * 0.8:.1f}'
        return f'<polygon points="{pts}" fill="{fill}" stroke="#0B1220" stroke-width="2"/>'

    def _circle(x, y, size, fill):
        return f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{size:.1f}" fill="{fill}" stroke="#0B1220" stroke-width="2"/>'

    def _hexagon(x, y, size, fill):
        pts = ' '.join(
            f'{x + size * math.cos(math.pi / 3 * k - math.pi / 2):.1f},'
            f'{y + size * math.sin(math.pi / 3 * k - math.pi / 2):.1f}'
            for k in range(6)
        )
        return f'<polygon points="{pts}" fill="{fill}" stroke="#0B1220" stroke-width="2"/>'

    producer_nodes = set(producers.values())
    consumer_nodes = set(consumers)
    node_svgs = []
    for i in range(n):
        x, y = pos[i]
        if i in producer_nodes:
            shape_svg = _square(x, y, 12, '#F5A623')
        elif i in consumer_nodes:
            shape_svg = _triangle(x, y, 14, '#4FD1C5')
        else:
            shape_svg = _circle(x, y, 11, '#3A4A6B')
        node_svgs.append(shape_svg)
        node_svgs.append(f'<text x="{x:.1f}" y="{y + 4:.1f}" font-size="10" text-anchor="middle" '
                          f'fill="#0B1220" font-family="monospace" font-weight="600">{i}</text>')

    controller_svg = ''
    if show_controller:
        ctrl_edges = []
        for i in range(n):
            x, y = pos[i]
            ctrl_edges.append(f'<line x1="{cx:.1f}" y1="{cy:.1f}" x2="{x:.1f}" y2="{y:.1f}" '
                               f'stroke="#8B7FE8" stroke-width="0.6" stroke-dasharray="3,3" opacity="0.35" />')
        controller_svg = (
            ''.join(ctrl_edges)
            + _hexagon(cx, cy, 15, '#8B7FE8')
            + f'<text x="{cx:.1f}" y="{cy - 24:.1f}" font-size="11" text-anchor="middle" '
              f'fill="#8B7FE8" font-family="ui-monospace, monospace">SDN controller</text>'
        )

    return (
        f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" '
        f'style="width:100%;height:auto;display:block;">'
        + ''.join(edge_svgs)
        + controller_svg
        + ''.join(node_svgs)
        + '</svg>'
    )


def _make_highlights(summary_rows: List[ResultRow]) -> Dict[str, Dict[str, str]]:
    """Objective, data-derived call-outs -- e.g. "lowest observed latency".

    Deliberately never says an architecture is "best": a lower latency
    might come with a reliability or overhead cost elsewhere, which this
    summary can't judge. Each entry just names which architecture recorded
    the extreme value for that one metric, computed directly from the same
    aggregated rows the table renders (nothing here is a new measurement).
    """
    rows = [r.fields for r in summary_rows]
    if not rows:
        return {}

    def arch_label(r):
        arch = r.get('architecture', r.get('system', '?'))
        return ARCHITECTURE_LABELS.get(arch, str(arch))

    highlights: Dict[str, Dict[str, str]] = {}

    lat_rows = [r for r in rows if r.get('avg_latency_ms_mean') is not None]
    if lat_rows:
        best = min(lat_rows, key=lambda r: float(r['avg_latency_ms_mean']))
        highlights['latency'] = {'label': arch_label(best), 'value': f"{float(best['avg_latency_ms_mean']):.3f} ms"}

    cache_rows = [r for r in rows if float(r.get('cache_hit_ratio_mean', 0.0)) > 0.0]
    if cache_rows:
        best = max(cache_rows, key=lambda r: float(r['cache_hit_ratio_mean']))
        highlights['cache'] = {'label': arch_label(best), 'value': f"{float(best['cache_hit_ratio_mean']) * 100:.1f}%"}

    sdn_rows = [r for r in rows if float(r.get('control_messages_tx_mean', 0.0)) > 0.0]
    if sdn_rows:
        best = min(sdn_rows, key=lambda r: float(r['control_messages_tx_mean']))
        highlights['control_overhead'] = {'label': arch_label(best), 'value': f"{float(best['control_messages_tx_mean']):.1f} msgs"}

    completion_rows = [r for r in rows if r.get('delivery_ratio_mean') is not None]
    if completion_rows:
        best = max(completion_rows, key=lambda r: float(r['delivery_ratio_mean']))
        highlights['completion'] = {'label': arch_label(best), 'value': f"{float(best['delivery_ratio_mean']) * 100:.1f}% delivered"}

    return highlights


def _make_comparison(summary_rows: List[ResultRow]) -> List[Dict[str, object]]:
    """Build the IP vs IP+SDN / NDN vs NDN+SDN side-by-side comparison.

    Averages across whatever summary rows share an architecture label (a
    sweep may produce several combos per architecture); the comparison is
    only meaningful when both sides of a pair were actually run under the
    same topology/workload/seed/failure schedule, which is exactly what
    the (identical) form fields feeding both architectures guarantee.
    """
    rows = [r.fields for r in summary_rows]

    def avg_for(architecture: str):
        matched = [r for r in rows if r.get('architecture') == architecture]
        if not matched:
            return None
        keys = ['avg_latency_ms_mean', 'completed_requests_mean',
                'control_messages_tx_mean', 'avg_reconvergence_ms_mean', 'reconvergence_events_mean',
                'queue_drops_mean', 'random_loss_drops_mean',
                'avg_propagation_ms_mean', 'avg_transmission_ms_mean', 'avg_queueing_ms_mean', 'avg_processing_ms_mean']
        out = {k: (sum(float(r.get(k, 0.0)) for r in matched) / len(matched)) for k in keys}
        out['n_combos'] = len(matched)
        return out

    pairs = []
    for base, sdn in (('ip', 'ip_sdn'), ('ndn', 'ndn_sdn')):
        b, s = avg_for(base), avg_for(sdn)
        if b is None and s is None:
            continue
        latency_delta_pct = None
        if b and s and b['avg_latency_ms_mean']:
            latency_delta_pct = 100.0 * (s['avg_latency_ms_mean'] - b['avg_latency_ms_mean']) / b['avg_latency_ms_mean']
        pairs.append({
            'base_key': base, 'sdn_key': sdn,
            'base_label': ARCHITECTURE_LABELS[base], 'sdn_label': ARCHITECTURE_LABELS[sdn],
            'base': b, 'sdn': s,
            'latency_delta_pct': round(latency_delta_pct, 1) if latency_delta_pct is not None else None,
        })
    return pairs


@app.route('/', methods=['GET'])
def index():
    return render_template(
        'index.html',
        defaults=_defaults(),
        forwarding_choices=FORWARDING_CHOICES,
        cache_policy_choices=CACHE_POLICY_CHOICES,
        topology_choices=TOPOLOGY_CHOICES,
        workload_choices=WORKLOAD_CHOICES,
        placement_choices=PLACEMENT_CHOICES,
        architecture_choices=ARCHITECTURE_CHOICES,
        architecture_labels=ARCHITECTURE_LABELS,
        network_profile_choices=NETWORK_PROFILE_CHOICES,
        result=None,
        error=None,
        form_values=_defaults(),
        selected_architectures=list(ARCHITECTURE_CHOICES),
    )


def _execute(args) -> Tuple[dict | None, str | None, int, dict]:
    """Run one sweep and build the exact `result` dict the template
    expects. Shared by the classic synchronous `/run` path and the
    background-thread job path (`webapp/jobs.py`) so both produce
    byte-identical results from the same config -- this function is the
    single place "run the simulation and shape the result" happens.

    Returns (result_or_None, error_or_None, combos, traces). `traces` is
    the raw captured event dict (architecture -> {events, ...}) -- kept
    separate from `result` so it never accidentally ends up serialized
    into the rendered HTML page; only `/jobs/<id>/trace` exposes it, on
    request, as JSON.
    """
    combos = _estimate_combos(args)
    if combos > MAX_COMBOS:
        error = (f'This sweep would run {combos} configurations (limit is {MAX_COMBOS} for the web demo). '
                  f'Reduce the number of comma-separated values in the sweep fields, or use the CLI for larger sweeps.')
        return None, error, combos, {}

    try:
        t0 = time.time()
        args.capture_trace = True  # opt-in flag run_sweep checks; CLI Namespaces never set this
        sweep = run_sweep(args)
        elapsed = time.time() - t0
        summary_rows: List[ResultRow] = sweep['summary_rows']
        meta = sweep['meta']
        traces = sweep.get('traces', {})

        table_rows = []
        display_cols = ['system', 'architecture', 'forwarding', 'cache_policy', 'cache_size', 'zipf_alpha',
                         'avg_latency_ms_mean', 'avg_propagation_ms_mean', 'avg_transmission_ms_mean',
                         'avg_queueing_ms_mean', 'avg_processing_ms_mean',
                         'completed_requests_mean', 'delivery_ratio_mean', 'cache_hit_ratio_mean',
                         'interest_tx_mean', 'data_tx_mean', 'dropped_interests_mean', 'dropped_data_mean',
                         'queue_drops_mean', 'random_loss_drops_mean',
                         'control_messages_tx_mean', 'avg_reconvergence_ms_mean',
                         'cache_directives_pushed_mean']
        for r in summary_rows:
            row = {c: r.fields.get(c, '') for c in display_cols}
            for k, v in row.items():
                if isinstance(v, float):
                    row[k] = round(v, 3)
            table_rows.append(row)
        table_rows.sort(key=lambda r: (r['system'], r['architecture'], r['forwarding'], r['cache_policy'], r['cache_size']))

        plots = _make_plots(summary_rows)
        comparison = _make_comparison(summary_rows)
        highlights = _make_highlights(summary_rows)

        from ndn_sim.cli import _make_topology  # local import to avoid polluting module namespace
        topo = _make_topology(args)
        show_controller = 'sdn-centralized' in meta['forwardings']
        svg = _topology_svg(topo, meta['producers'], meta['consumers'], show_controller)
        positions = _node_positions(topo.n, 560, 420)

        result = {
            'columns': display_cols,
            'rows': table_rows,
            'plots': plots,
            'svg': svg,
            'meta': meta,
            'elapsed': round(elapsed, 3),
            'combos': combos,
            'comparison': comparison,
            'highlights': highlights,
            'trace_architectures': sorted(traces.keys()),
            'positions': {str(k): v for k, v in positions.items()},
        }
        return result, None, combos, traces
    except Exception as exc:  # surfaced to the caller, never a raw 500
        return None, f'{type(exc).__name__}: {exc}', combos, {}


def _render_result(result, error, form_values, selected_architectures, job_id=None):
    return render_template(
        'index.html',
        defaults=_defaults(),
        forwarding_choices=FORWARDING_CHOICES,
        cache_policy_choices=CACHE_POLICY_CHOICES,
        topology_choices=TOPOLOGY_CHOICES,
        workload_choices=WORKLOAD_CHOICES,
        placement_choices=PLACEMENT_CHOICES,
        architecture_choices=ARCHITECTURE_CHOICES,
        architecture_labels=ARCHITECTURE_LABELS,
        network_profile_choices=NETWORK_PROFILE_CHOICES,
        result=result,
        error=error,
        form_values=form_values,
        selected_architectures=selected_architectures,
        job_id=job_id,
    )


@app.route('/run', methods=['POST'])
def run():
    """Classic synchronous path: always available as a fallback (works with
    JS disabled, and is exactly what every pre-existing test exercises).
    The JS-driven UI instead calls /run/async and polls -- see run_async().
    """
    form_values = {k: v for k, v in request.form.items()}
    form_values['architectures'] = request.form.getlist('architectures')
    args = _build_args(request.form)
    result, error, _combos, _traces = _execute(args)
    selected_architectures = form_values.get('architectures') or list(ARCHITECTURE_CHOICES)
    return _render_result(result, error, form_values, selected_architectures)


@app.route('/run/async', methods=['POST'])
def run_async():
    """Non-blocking entry point: creates a job, starts it on a background
    thread, and returns immediately with a job id for the frontend to poll
    (see webapp/static/app.js). This is what actually fixes "the page gets
    stuck on Running simulation" for slow/large sweeps -- the HTTP request
    for `/run/async` itself completes in milliseconds regardless of how
    long the simulation takes; the browser is never blocked waiting on it.
    """
    form_values = {k: v for k, v in request.form.items()}
    form_values['architectures'] = request.form.getlist('architectures')
    args = _build_args(request.form)

    job = job_manager.create()
    job.combos_total = min(_estimate_combos(args), MAX_COMBOS)
    job.args = args

    def _target(job):
        job.stage = 'simulating'
        result, error, combos, traces = _execute(args)
        job.combos_done = combos
        job.traces = traces
        if error is not None:
            raise RuntimeError(error)
        job.result = result
        job.stage = 'done'

    job_manager.run_in_background(job, _target)
    return jsonify({'job_id': job.id})


@app.route('/jobs/<job_id>/status', methods=['GET'])
def job_status(job_id):
    job = job_manager.get(job_id)
    if job is None:
        return jsonify({'status': 'not_found'}), 404
    return jsonify(job.as_status_dict())


@app.route('/jobs/<job_id>/view', methods=['GET'])
def job_view(job_id):
    job = job_manager.get(job_id)
    if job is None or job.status != 'completed':
        # Never a bare 404/500 page -- fall back to the normal empty-state
        # index so the user always lands somewhere coherent.
        return _render_result(None, 'That job is no longer available (jobs are kept in memory only). '
                                     'Please run the simulation again.', _defaults(), list(ARCHITECTURE_CHOICES))
    return _render_result(job.result, None, _defaults(), list(ARCHITECTURE_CHOICES), job_id=job_id)


@app.route('/jobs/<job_id>/trace', methods=['GET'])
def job_trace(job_id):
    """Packet-trace JSON for the canvas animation player. `architecture`
    selects which captured trace to return when more than one architecture
    was run (see cli.run_sweep's `traces` output). Events are exactly what
    the simulator emitted via `on_event` -- nothing here is generated or
    interpolated beyond what the real run produced.
    """
    job = job_manager.get(job_id)
    if job is None or job.status != 'completed' or job.result is None:
        return jsonify({'error': 'job not available'}), 404

    architecture = request.args.get('architecture', '')
    available = job.result.get('trace_architectures', [])
    if not available:
        return jsonify({'error': 'no trace captured for this run'}), 404
    if architecture not in available:
        architecture = available[0]

    # The trace itself isn't stored on `result` (kept out of the rendered
    # HTML/session-history payloads, which can get large) -- it lives on
    # the job object directly, re-derived from the same run that produced
    # `result`, never re-simulated for this endpoint.
    events = job.traces.get(architecture, {}).get('events', [])

    from ndn_sim.cli import _make_topology  # local import, mirrors _execute()
    topo = _make_topology(job.args)
    edges = []
    seen = set()
    for (u, v) in topo.links:
        if (v, u) in seen:
            continue
        seen.add((u, v))
        edges.append([u, v])

    return jsonify({
        'architecture': architecture,
        'available': available,
        'positions': job.result.get('positions', {}),
        'producers': job.result.get('meta', {}).get('producers', {}),
        'consumers': job.result.get('meta', {}).get('consumers', []),
        'edges': edges,
        'events': events,
    })


if __name__ == '__main__':
    # Local/dev entry point. In production (Render, etc.) this file is
    # imported by a WSGI server (see wsgi.py / Procfile) instead of being
    # run directly, so debug mode and the dev server never run there.
    debug = os.environ.get('FLASK_DEBUG', '0') == '1'
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=debug, host='0.0.0.0', port=port)
