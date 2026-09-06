# SimN — Unified Network Architecture & Performance Simulator

An event-driven, logical network simulator that compares four architectures under
**identical topology, workload, link characteristics, failure schedule, and random
seed**. The web dashboard (**SimN** — "Discrete-Event Network Architecture
Simulator") is documented in [Web dashboard](#web-dashboard--simn) below:

| | Distributed / static control | Centralized SDN control |
|---|---|---|
| **IP data plane** | `ip` | `ip_sdn` |
| **NDN data plane** | `ndn` | `ndn_sdn` |

SDN is modeled as a **control-plane option**, not a fifth protocol: the meaningful
comparisons this simulator is built around are **IP → IP+SDN** and **NDN → NDN+SDN**,
each isolating exactly the effect of centralized control on that data plane.

## Abstraction level (read this first)

This is a **logical, discrete-event simulator** — architecturally in the spirit of
ns-3 / ndnSIM / OMNeT++, but far smaller in scope. It is not a packet-accurate
replacement for any of them, and doesn't attempt full TCP/IP transport, realistic
crypto, or byte-accurate packet processing.

Simulation time is a pure logical clock: the event loop jumps directly from one
scheduled event to the next. **Nothing in this codebase calls `time.sleep()` to
represent network delay** — a simulated hour of traffic can execute in milliseconds
of real CPU time (`tests/test_timing.py::NoWallClockSleepTests` asserts this).

## Timing model

End-to-end latency is never `hop_count × constant`. Every link traversal accumulates
four independently-modeled components (`ndn_sim/core/timing.py`):

```text
total_link_delay = propagation_delay + transmission_delay + queueing_delay + processing_delay
```

- **propagation_delay** — either a directly configured `Link.propagation_delay_ms`,
  or derived from `distance_km / propagation_speed_km_per_ms` when distance is set.
- **transmission_delay** — `packet_size_bits / bandwidth_bps`. Larger NDN Data
  packets (4096 B default) take measurably longer to transmit than small Interests
  (200 B default) or SDN control messages (256 B) on the same link.
- **queueing_delay** — emerges from actual link occupancy (`LinkQueue.busy_until_ms`),
  not a fixed constant: an idle link contributes ~0 ms, a busy one contributes more,
  and a backlog that would exceed the link's configured `queue_limit_packets` causes
  a drop instead of unbounded delay.
- **processing_delay** — `Link.processing_delay_ms` (link/interface cost) plus
  protocol-specific processing supplied by the data plane (NDN PIT/FIB/Content-Store
  lookup; IP forwarding-table lookup).

Independent random packet loss (`Link.loss_probability`) is deliberately separate
from deterministic link failures (`Link.active`) — a link can be "up" and still drop
a fraction of traffic; a "down" link drops everything regardless of loss probability.

IP and NDN traverse links through the exact same `LinkTimingModel` class
(`ndn_sim.ndn_engine.LinkScheduler` is a thin, backward-compatible subclass of it) —
"same link, same delay model" holds by construction, not by convention.

### Network profiles

`ndn_sim/core/network_profiles.py` provides representative, real-world-*inspired*
(not measured) presets — `DATA_CENTER`, `LAN`, `REGIONAL_WAN`, `INTERNET_WAN`,
`INTERCONTINENTAL` — each documenting a bandwidth/propagation/processing/loss/queue
combination. Use `--network-profile <NAME>` on the CLI or the dashboard's Network
profile picker; every value remains independently overridable via the manual flags
when no profile is selected.

## Architecture

```text
ndn_sim/
  core/
    timing.py            # shared propagation+transmission+queueing+processing model
                          #   (LinkTimingModel, LinkQueue, LatencyBreakdown)
    network_profiles.py   # LAN / DATA_CENTER / REGIONAL_WAN / INTERNET_WAN / INTERCONTINENTAL
  topology.py             # topologies + Link (propagation/bandwidth/distance/loss/processing)
  workload.py              # catalog + request generation (uniform/zipf/bursty/shifted)
  cache.py                 # CachePolicy strategies: lru/fifo/lfu/random (+ SDN-coordinated)
  sim_models.py             # RunStats (shared metrics, incl. latency breakdown)
  ndn_engine.py             # NDN data plane: Interest/Data, PIT, FIB, Content Store
  ip_engine.py               # IP data plane: persistent per-node routing table,
                              #   distributed or SDN-provisioned, per-hop event-driven
  controller.py               # reusable SDN control plane: Dijkstra + selective push +
                              #   control-RTT accounting, with protocol-specific adapters
                              #   (compute_fibs/push_fibs for NDN, compute_routes/push_routes for IP)
  cli.py                       # experiment runner: build_arg_parser, run_sweep, main
run_experiment.py               # convenience wrapper
plot_results.py                 # plot aggregated results
webapp/                         # Flask dashboard: architecture selector, network profiles,
                                 #   comparison panel, topology diagram, metrics table, plots
configs/                        # example JSON configs
examples/                       # sample topology file
tests/                          # timing, CLI architecture filter, engine, controller, dashboard
```

### Why IP and NDN aren't forced into identical internals

IP gets a genuine, persistent `dst_node -> next_hop` routing table per node — not a
Content Store, not a PIT. NDN keeps its `prefix -> next_hop` FIB, PIT, and Content
Store untouched. Both are driven by the *same* control-plane machinery
(`SDNController.compute_routes`/`push_routes` for IP, `compute_fibs`/`push_fibs` for
NDN — both thin adapters over the same Dijkstra computation and the same
selective-push / control-RTT accounting discipline), so "what SDN control costs" is
measured identically for both data planes without pretending an IP routing table and
an NDN FIB are the same structure.

### Control planes

- **Distributed / static** (`ip`, `ndn` without `sdn-centralized`): every node
  recomputes its own routing table / FIB **instantly and locally** whenever the
  topology changes (BFS for IP hop-count routing, the existing NDN FIB rebuild).
  Zero control-plane messages, zero reconvergence delay — this is the baseline SDN
  is compared against.
- **Centralized SDN** (`ip_sdn`, `ndn_sdn`): a logically-centralized
  `SDNController` computes globally delay-weighted-optimal routes via Dijkstra, then
  pushes updates **only to nodes whose route actually changed** (`control_messages_tx`),
  after a configurable **control round-trip time** (`--controller-rtt-ms`). Nodes keep
  forwarding on the *stale* table until the update lands — SDN's centralized-optimum
  benefit and its control-plane/reconvergence cost both show up in the metrics, rather
  than the controller being modeled as free or instantaneous.

## Metrics

Every run reports (`RunStats.as_dict()` in `sim_models.py`):

- **End-to-end:** `avg_latency_ms`, `avg_latency_hops`, `completed_requests`,
  `timed_out_requests`, `delivery_ratio` (`completed_requests / requests`)
- **Latency breakdown** (averaged per link traversal observed during the run —
  a different denominator than `avg_latency_ms`, documented in code):
  `avg_propagation_ms`, `avg_transmission_ms`, `avg_queueing_ms`, `avg_processing_ms`
- **Traffic/loss:** `interest_tx`, `data_tx`, `dropped_interests`, `dropped_data`,
  `retransmissions`, `queue_drops`, `random_loss_drops` (independent loss, separate
  from `queue_drops`), `pit_overflow_drops`, `producer_serves`, `verification_failures`
- **NDN-specific:** `cache_hits`, `cache_misses`, `cache_hit_ratio`
- **SDN-specific** (non-zero only for `*_sdn` architectures): `control_messages_tx`,
  `reconvergence_events`, `avg_reconvergence_ms`, `cache_directives_pushed`

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### Run all four architectures, fairly compared

```bash
python run_experiment.py \
  --topology mesh --nodes 14 --extra-edges 8 --consumer-count 4 \
  --network-profile REGIONAL_WAN \
  --architectures ip,ip_sdn,ndn,ndn_sdn \
  --workload zipf --zipf-alpha 1.1 --requests 400 \
  --cache-policy lfu --cache-size 16 \
  --fail-edge 1,7 --fail-time-ms 60 --recover-time-ms 220 \
  --seeds 11,12,13 \
  --out-dir out/unified
```

`--architecture <name>` (singular) restricts a run to exactly one architecture,
computing only what's needed. `--architectures` (comma list) is the same filter for
several at once. Both ride on the existing `system` x `forwarding` sweep rather than
introducing a second, independent axis that could disagree with it — see the comment
in `cli.py::run_sweep` for why that design was rejected.

Without `--architecture(s)`, the CLI keeps its original default behavior: run `ip`
and `ndn` once per swept `forwarding`/`cache_policy`/etc combination (so
`--forwardings best-route,sdn-centralized` alone already produces all four
architectures — the `architecture` CSV column just labels them explicitly).

### Validate the timing model against hand-worked numbers

```bash
python -m unittest tests.test_timing -v
```

Checks transmission delay against `size/bandwidth`, propagation delay against
`distance/speed`, multi-hop accumulation against a worked 3-node/5ms/100Mbps example,
queueing behavior under load, drop-on-full-queue, and that a long simulated run
completes in well under a second of wall-clock time.

### Other examples

```bash
# IP vs IP+SDN only, on a WAN profile, with a mid-run failure
python -m ndn_sim.cli --architectures ip,ip_sdn --network-profile INTERNET_WAN \
  --nodes 10 --requests 300 --fail-edge 2,6 --fail-time-ms 40 --recover-time-ms 160 \
  --controller-rtt-ms 8 --out-dir out/ip_vs_ip_sdn

# NDN cache-policy sweep (SDN axis untouched)
python -m ndn_sim.cli --architecture ndn --cache-policies lru,lfu,sdn-coordinated \
  --cache-sizes 0,8,16,32 --zipf-alphas 0.8,1.2 --seeds 1,2,3 --out-dir out/cache_sweep

# From a topology file
python -m ndn_sim.cli --topology-file examples/campus_topology.txt \
  --consumer-count 3 --architecture ndn --workload bursty --out-dir out/campus
```

## Web dashboard — SimN

```bash
python webapp/app.py
```

Open **http://127.0.0.1:5000**. **SimN** ("Discrete-Event Network Architecture
Simulator") runs entirely in-process against `ndn_sim.cli.run_sweep` — no
separate implementation to keep in sync with the CLI, no data leaves the
server, and every number displayed is read directly from the same
`RunStats`/aggregation pipeline the CLI writes to CSV.

The UI follows the workflow **Configure → Simulate → Observe → Compare →
Analyze**, laid out as three numbered panels, with the network topology as
the visual centerpiece:

1. **Experiment configuration** (left sidebar) — topology, architecture
   selection, forwarding, caching, workload, network profile, and failure
   injection, grouped exactly as in the CLI's argument groups. SDN-specific
   fields (controller RTT, SDN cache top-K) dim automatically unless an
   `*_sdn` architecture or `sdn-centralized` forwarding is actually selected
   — client-side only, the backend validation is unchanged.
2. **Network visualization** — a server-rendered SVG topology diagram (node
   *role* distinguished by shape: square = producer, triangle = consumer,
   hexagon = SDN controller, circle = relay, not color alone) plus, when a
   trace was captured for this run, a **canvas-based packet playback
   player** underneath it — see [Packet playback](#packet-playback) below.
3. **Results & comparison** — objective highlight cards ("lowest observed
   latency", never "best", since a single metric can't judge that); the
   IP↔IP+SDN / NDN↔NDN+SDN comparison panel with a short metric glossary;
   the full aggregated metrics table (raw column names, e.g.
   `avg_propagation_ms_mean` — accurate over pretty); visual analytics
   (matplotlib plots, only rendered when the underlying metric is
   non-trivial); and a collapsible **Technical details** drawer with the
   full resolved configuration for anyone who wants to verify exactly what
   ran.

An **Experiments** section below the main layout keeps a session-scoped
history of recent runs (topology, architectures, workload, latency
highlight) in the browser's `sessionStorage` — genuinely client-side, since
the backend doesn't persist runs across requests; nothing here is
fabricated data.

Sweeps are capped at 24 total configurations for the web demo (`MAX_COMBOS`
in `webapp/app.py`, overridable via the `SIM_MAX_*` environment variables
described in Deployment below); use the CLI directly for larger sweeps.

### Asynchronous execution

Submitting the form POSTs to `/run/async`, which creates a job, starts it on
a background `threading.Thread`, and returns a job id **immediately** — the
HTTP request that starts the simulation completes in milliseconds regardless
of how long the simulation itself takes. The page then polls
`GET /jobs/<id>/status` (real state — `queued`/`running`/`completed`/
`failed`, plus a genuine "config N of M" count and elapsed wall time, never
a fabricated percentage) and redirects to `GET /jobs/<id>/view` once
`completed`. A classic synchronous fallback (`POST /run`, rendering results
directly) still exists and is used automatically if JavaScript is
unavailable — see `webapp/jobs.py`'s module docstring for why this is a
simple in-memory job store rather than a task queue.

### Packet playback

When the backend captures a trace for the run, a canvas player appears
under the topology diagram with:

- **Play/pause, reset, a scrub bar, and a speed selector** (0.25×–10×) —
  the timeline advances through *real* captured event timestamps, not a
  fixed frame rate standing in for simulated time.
- **Packets animated along actual topology edges**, colored/shaped by
  protocol: IP packets, NDN Interests, and NDN Data are visually distinct
  (see the in-player legend); a pulse on the controller marks a captured
  SDN control update (FIB/route install).
- **Link failures** drawn as a broken (red, dashed) edge for the real
  duration between the run's `link_down` and `link_up` events.
- **An event log** streaming exactly what the simulator emitted
  (`102.4 ms  TX  Interest  Node 2 → Node 3`, etc.) — click any row to
  inspect its full event data.
- **Click-to-inspect** on nodes (role + observed tx/rx counts from the
  trace) and on in-flight packets (via the event log).

The simulation runs to completion on a background thread first; the canvas
then replays the exact captured event sequence at the selected speed — an
accurate post-hoc replay of real simulator output, not a live view of an
in-progress simulation.

## Deployment

The Flask app in `webapp/app.py` is the single source of truth for the web
dashboard; `wsgi.py`, `api/index.py`, and the local dev server (`python
webapp/app.py`) are three different ways of running the *same* `app` object.
`ndn_sim.cli.run_sweep` (what the dashboard calls) never writes to disk --
only the CLI's `main()` does -- so it's safe to run on a read-only
serverless filesystem.

### Render (recommended for full functionality)

Render runs a real, always-on Python process, so there's no serverless
execution-time limit to work around.

1. Push this repo to GitHub (see below).
2. On [render.com](https://render.com): **New → Blueprint**, point it at the
   repo. Render reads `render.yaml` and provisions everything automatically.
   *(Or skip the blueprint: New → Web Service, Build Command
   `pip install -r requirements.txt`, Start Command
   `gunicorn wsgi:app --workers 2 --threads 4 --timeout 120`.)*
3. First deploy takes a few minutes (installs `matplotlib`/`pandas`). After
   that, open the assigned `https://<name>.onrender.com` URL.

Tune `SIM_MAX_NODES` / `SIM_MAX_REQUESTS` / `SIM_MAX_COMBOS` in the Render
dashboard's environment variables if you're on a plan with tighter limits.

### Vercel

Vercel's Python runtime is serverless: each request is a fresh function
invocation with a hard execution-time limit (10s on Hobby, longer on Pro).
`api/index.py` wraps the same Flask app and, specifically for this entry
point, tightens the web-demo's safety caps (`SIM_MAX_NODES=24`,
`SIM_MAX_REQUESTS=400`, `SIM_MAX_COMBOS=8`) so a typical sweep finishes
within the limit -- override these in the Vercel dashboard's project
environment variables if you're on a plan with more headroom.

1. Push this repo to GitHub.
2. On [vercel.com](https://vercel.com): **Add New → Project**, import the
   repo. Vercel detects `vercel.json` (which points at `api/index.py` and
   includes `webapp/templates/`, `webapp/static/`, and `ndn_sim/` in the
   function bundle) and deploys automatically -- no extra configuration
   needed.
3. Every push to the connected branch redeploys automatically.

### Local

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python webapp/app.py            # dev server, http://127.0.0.1:5000
# or, to run it the way production does:
gunicorn wsgi:app --bind 127.0.0.1:8000
```

### Publishing to GitHub

```bash
git init
git add -A
git commit -m "Unified Network Architecture & Performance Simulator"
git branch -M main
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

`.gitignore` already excludes `__pycache__/`, `.venv/`, `out/`, `.vercel/`,
and other local/build artifacts, so nothing environment-specific gets
committed.

## Testing

```bash
python -m unittest discover -s tests -v
```

95 tests, all runnable with only `requirements.txt` installed, across:
- `test_timing.py` — transmission/propagation/queueing formulas, multi-hop
  accumulation, no-wall-clock-sleep, network profiles
- `test_simulator.py` — cache eviction, SDN controller (Dijkstra, selective FIB push,
  cache placement), IP distributed & SDN routing (including reconvergence and a
  same-conditions IP vs IP+SDN fairness test), NDN cache benefit, failure visibility
- `test_cli_architecture.py` — the `--architecture`/`--architectures` filter,
  including the "requested SDN architecture with no explicit forwarding sweep" edge
  case
- `test_sdn_control_plane.py` — proves the SDN control plane is genuinely different
  code (not an aliased label): distributed vs SDN routing disagree on
  heterogeneous-weight topologies, that disagreement actually changes observed
  forwarding/latency, initial SDN provisioning is counted as real control-plane cost,
  and a full failure scenario exercises detection → controller recompute → selective
  push → control-RTT delay → install → reconvergence with non-zero
  `control_messages_tx`/`reconvergence_events`
- `test_research_validity.py` — asserts real, measured results follow known
  networking theory across ~13 configurations (caching, network profiles, SDN
  control, failures, queueing)
- `test_comparative_theses.py` — which architecture (IP/NDN/SDN) wins under which
  configuration, and by how much
- `test_webapp.py` — dashboard rendering, the comparison panel, network-profile
  selection, oversized-sweep rejection, SimN branding/nav, highlight cards, the
  technical-details drawer, shape-based topology rendering, and the full async job
  lifecycle (`/run/async` → poll → `/jobs/<id>/view` → `/jobs/<id>/trace`)

### Real-browser regression suite (optional)

```bash
pip install playwright && python -m playwright install chromium
python -m unittest tests.test_browser_regressions -v
```

(`requirements-dev.txt` pins the same thing: `pip install -r requirements-dev.txt`.)

9 additional tests that drive an actual Chromium instance against the real
Werkzeug server rather than Flask's test client, covering behaviors only a real
browser engine can observe — CSS cascade resolution, HTML form-submission
semantics, and rendered pixel dimensions (the loading overlay's visibility,
whether typed form values actually reach the server, whether the page ever
requires horizontal scrolling, and whether the packet-playback canvas renders at
a sane size). Skipped automatically (not failed) if Playwright isn't installed,
so the main suite above never requires a browser binary.

## Known limitations / honest scope notes

- **The job store is in-memory, single-process, not persisted.** Jobs
  disappear on restart, are not shared across multiple server processes,
  and old jobs are evicted after 200 accumulate. This is a deliberate,
  documented choice for a research/demo tool (see `webapp/jobs.py`'s module
  docstring) — not a bug, and not "distributed" or "production-grade" job
  infrastructure. If SimN ever needs multi-worker horizontal scaling, this
  module is the seam to replace.
- **Packet playback is a post-hoc replay of a completed simulation's real
  event trace, not a live view of an in-progress one.** The simulation
  runs to completion on a background thread first; the canvas then replays
  the exact captured event sequence at a user-selected speed. This is
  intentionally NOT described as "real-time simulation" anywhere in this
  project, because it isn't one.
- **Trace capture is capped at 40 requests per architecture**
  (`TRACE_MAX_REQUESTS` in `ndn_sim/cli.py`) regardless of how many
  requests the actual metrics sweep used, to keep the animated trace small
  and smooth. The aggregate metrics table is unaffected — it always
  reflects the full configured request count.
- **Controller link is a single flat `controller_rtt_ms`.** A fully separate
  controller-node/controller-link entity (with its own bandwidth-derived control
  message transmission delay) was considered and deliberately not built — the added
  fidelity was judged not worth the extra moving parts for what this simulator needs
  to demonstrate (the *fact* of control-plane latency and its effect on
  reconvergence), and `controller_rtt_ms` is documented as a bundled
  processing+propagation+transmission figure rather than silently treated as exact.
- **IP has no fragmentation, ARP, or TCP-level retransmission/congestion control** —
  it is a logical, connectionless request/response abstraction over a routed network,
  not a byte-accurate IP stack.
- **A single link queue models one FIFO service class** — no per-flow fairness,
  QoS classes, or ECMP load-splitting.
- **NDN forwarding strategies** (`flooding`, `probabilistic`) exist as multipath
  alternatives to `best-route`; they are not swept automatically when using the
  `--architecture` filter (which only chooses `best-route` vs `sdn-centralized`) —
  combine `--architecture ndn` with an explicit `--forwardings flooding,probabilistic`
  if you need that sweep too.

## Suggested report/demo story

1. **IP vs NDN** on the same topology/workload (different paradigms — establishes
   the baseline).
2. **IP -> IP+SDN**: does centralized control improve IP routing, and at what
   control-plane cost?
3. **NDN -> NDN+SDN**: same question for NDN.
4. **Failure recovery**: distributed (instant, always-current) vs SDN (globally
   optimal, delayed by `controller_rtt_ms`) — show `reconvergence_events` and
   `avg_reconvergence_ms` alongside the latency win.
5. **Cache policy** (`lru`/`lfu`/`sdn-coordinated`) under `zipf` vs `shifted`
   workloads — where does caching help most?
6. **Network profile** sweep (`LAN` -> `INTERCONTINENTAL`) — which latency component
   (propagation vs transmission vs queueing) dominates at each scale?
