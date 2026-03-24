from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

from .cache import BaseCache


@dataclass(frozen=True)
class Interest:
    name: str
    nonce: int


@dataclass(frozen=True)
class Data:
    name: str
    payload: bytes


@dataclass
class NodeState:
    cs: BaseCache[str, bytes]
    pit: Dict[str, Set[int]]  # name -> set(incoming faces)
    fib: Dict[str, int]  # prefix -> next hop


def longest_prefix_match(fib: Dict[str, int], name: str) -> Optional[int]:
    best_len = -1
    best_nh: Optional[int] = None
    for prefix, nh in fib.items():
        if name.startswith(prefix) and len(prefix) > best_len:
            best_len = len(prefix)
            best_nh = nh
    return best_nh


@dataclass
class RunStats:
    requests: int = 0
    total_latency_hops: int = 0
    total_latency_ms: float = 0.0
    interest_tx: int = 0
    data_tx: int = 0
    cache_hits: int = 0
    cache_misses: int = 0

    def as_dict(self) -> Dict[str, float]:
        avg_latency = (self.total_latency_hops / self.requests) if self.requests else 0.0
        avg_latency_ms = (self.total_latency_ms / self.requests) if self.requests else 0.0
        hit_ratio = (self.cache_hits / self.requests) if self.requests else 0.0
        return {
            "requests": float(self.requests),
            "avg_latency_hops": float(avg_latency),
            "avg_latency_ms": float(avg_latency_ms),
            "interest_tx": float(self.interest_tx),
            "data_tx": float(self.data_tx),
            "cache_hit_ratio": float(hit_ratio),
        }
