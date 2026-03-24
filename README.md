# Named Data Networking (NDN) Prototype



## Overview
This project implements a small **NDN prototype** and an **IP-based baseline** to compare:

- Content retrieval latency
- Network traffic overhead
- Cache hit ratio

The NDN model includes:

- Interest/Data packets
- Name-based forwarding using longest-prefix matching (prototype-level)
- In-network caching at intermediate nodes (LRU/FIFO)
- PIT-based Interest aggregation

## Quickstart

### Run a single experiment batch
```bash
python3 run_experiment.py \
  --topology line \
  --nodes 8 \
  --cache-size 25 \
  --cache-policy lru \
  --workload zipf \
  --requests 200 \
  --zipf-alpha 1.2 \
  --seed 42
```

### Run parameter sweeps (cache sizes + Zipf alphas)
```bash
python3 run_experiment.py \
  --topology mesh \
  --nodes 12 \
  --extra-edges 6 \
  --cache-sizes 0,5,10,25 \
  --cache-policy lru \
  --workload zipf \
  --requests 300 \
  --catalog-items 200 \
  --zipf-alphas 0.8,1.2 \
  --link-delay-ms 10 \
  --processing-delay-ms 1 \
  --interest-iat-ms 1 \
  --seed 42 \
  --out-dir out
```

### Generate plots
```bash
python3 plot_results.py --csv out/results.csv --out out/plots
```

### Output
The script prints a summary and writes CSVs to `./out/`:

- `out/results.csv`

## Notes
- Security (signatures), congestion control, and dynamic routing are out of scope.
- The NDN engine uses a lightweight discrete-event model so that PIT aggregation can occur when Interests overlap in time.

## Team
- Abhishek Kumar Shah
- Abhinav Nagar
- Pranjal Nimbodiya
