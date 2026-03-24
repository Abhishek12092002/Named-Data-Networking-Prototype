from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

from .sim_models import RunStats
from .topology import Topology, shortest_path_length


@dataclass(frozen=True)
class IPConfig:
    server_node: int
    link_delay_ms: float = 10.0
    processing_delay_ms: float = 1.0


def ip_fetch_sequential(
    topo: Topology,
    cfg: IPConfig,
    requests: List[str],
    payload_db: Dict[str, bytes],
    client_node: int,
) -> RunStats:
    stats = RunStats()

    hop_len = shortest_path_length(topo, client_node, cfg.server_node)

    for name in requests:
        stats.requests += 1
        if name not in payload_db:
            raise KeyError(f"missing payload for {name}")

        stats.cache_misses += 1
        interest_like = hop_len
        data_like = hop_len

        edge_delay = float(cfg.link_delay_ms) + float(cfg.processing_delay_ms)
        stats.total_latency_ms += float((interest_like + data_like) * edge_delay)

        stats.interest_tx += interest_like
        stats.data_tx += data_like
        stats.total_latency_hops += interest_like + data_like

    return stats
