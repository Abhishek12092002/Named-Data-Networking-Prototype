# Named Data Networking Performance Simulator

A resume-level, event-driven NDN simulator for comparing **Named Data Networking (NDN)** against a host-centric **IP baseline** under multi-consumer, multi-producer workloads.

## What is implemented

### Core NDN pipeline
- Named Interest/Data forwarding
- Per-node Content Store (cache)
- PIT-based Interest aggregation
- Prefix-based FIB forwarding
- On-path caching
- Multi-consumer, multi-producer operation


- **Forwarding strategies:** `best-route`, `flooding`, `probabilistic`
- **Cache policies:** `lru`, `fifo`, `lfu`, `random`
- **Selective admission:** cache objects only after a configurable popularity threshold
- **TTL-aware caching**
- **Bounded PIT:** configurable capacity and Interest lifetime
- **Retransmission support** after timeout
- **Link model:** propagation delay + serialization delay + bounded queue
- **Failure modeling:** link down / link recovery events with FIB recomputation
- **Richer workloads:** `uniform`, `zipf`, `bursty`, `shifted`
- **Experimental harness:** multi-seed runs, raw CSV, summary CSV with mean/std
- **Plotting:** error-bar plots from aggregated results
- **Smoke tests:** cache, failure handling, IP baseline, NDN cache benefit

## Project structure

```text
ndn_sim/
  cache.py          # cache policies + admission/TTL logic
  topology.py       # synthetic/file-based topologies + link attributes
  workload.py       # catalog + workload generation
  sim_models.py     # shared dataclasses and metrics
  ndn_engine.py     # event-driven NDN simulator
  ip_engine.py      # event-driven IP baseline
  cli.py            # experiment runner
run_experiment.py   # convenience wrapper
plot_results.py     # plot aggregated results
configs/            # example JSON configs
examples/           # sample topology file
tests/              # smoke tests
```

## Installation

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Quick start

### 1) Run the demo configuration

```bash
python run_experiment.py --config configs/demo_mesh.json
python plot_results.py
```

Outputs are written under `out/demo_mesh/` by default.

### 2) Simple manual run

```bash
python -m ndn_sim.cli \
  --topology mesh \
  --nodes 12 \
  --consumer-count 4 \
  --cache-sizes 0,8,16,32 \
  --cache-policy lfu \
  --forwarding best-route \
  --workload zipf \
  --zipf-alphas 0.8,1.2 \
  --requests 400 \
  --items-per-prefix 80 \
  --seeds 11,12,13 \
  --out-dir out/manual
```

### 3) Run with a topology file

```bash
python -m ndn_sim.cli \
  --topology-file examples/campus_topology.txt \
  --consumer-count 3 \
  --cache-policy lfu \
  --forwarding flooding \
  --requests 300 \
  --workload bursty \
  --out-dir out/campus
```

## Failure experiment

Bring down an edge at 40 ms and recover it at 140 ms:

```bash
python -m ndn_sim.cli \
  --topology mesh \
  --nodes 10 \
  --consumer-count 3 \
  --requests 250 \
  --fail-edge 2,7 \
  --fail-time-ms 40 \
  --recover-time-ms 140 \
  --out-dir out/failure
```

## How results are reported

Each run writes:
- `results_raw.csv` — one row per seed/system/configuration
- `results_summary.csv` — grouped mean/std summary
- `run_config.json` — exact config used

Main metrics:
- `avg_latency_ms`
- `avg_latency_hops`
- `cache_hit_ratio`
- `interest_tx`
- `data_tx`
- `retransmissions`
- `queue_drops`
- `pit_overflow_drops`
- `producer_serves`
- `verification_failures`

## Testing

Run smoke tests:

```bash
python -m unittest discover -s tests -v
```

What is covered:
- LFU eviction behavior
- NDN cache reduces traffic relative to no-cache NDN
- Failure injection produces visible degradation
- IP baseline completes successfully

## Suggested report/demo story

A good presentation arc is:
1. **NDN vs IP** on the same topology
2. effect of **cache size**
3. effect of **workload skew** (`zipf_alpha`)
4. compare **forwarding strategies**
5. show **failure recovery** and timeout/retransmission behavior
6. compare **cache policies** under `zipf` vs `shifted`

## Honest scope note

This is still a **logical / event-driven simulator**, not `ndnSIM` or packet-accurate ns-3. It models queueing and serialization in a simplified form, but it does **not** model full TCP/IP transport, realistic crypto, or byte-accurate packet processing.


