from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from .cache import BaseCache


@dataclass(frozen=True)
class Interest:
    name: str
    nonce: int


@dataclass(frozen=True)
class Data:
    name: str
    payload: bytes
    verified: bool = True


@dataclass
class PITEntry:
    faces: Set[int]
    expiry_ms: float
    created_ms: float
    requester_ids: Set[int] = field(default_factory=set)


@dataclass
class NodeState:
    cs: BaseCache[str, bytes]
    pit: Dict[str, PITEntry]
    fib: Dict[str, List[int]]


@dataclass
class NodeMetrics:
    cache_hits: int = 0
    cache_misses: int = 0
    forwarded_interests: int = 0
    forwarded_data: int = 0
    pit_peak: int = 0
    cache_size_peak: int = 0


@dataclass
class RunStats:
    requests: int = 0
    completed_requests: int = 0
    timed_out_requests: int = 0
    dropped_interests: int = 0
    dropped_data: int = 0
    verification_failures: int = 0
    total_latency_hops: float = 0.0
    total_latency_ms: float = 0.0
    interest_tx: int = 0
    data_tx: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    retransmissions: int = 0
    queue_drops: int = 0
    pit_overflow_drops: int = 0
    producer_serves: int = 0
    per_consumer_latency_ms: Dict[int, List[float]] = field(default_factory=dict)
    per_node: Dict[int, NodeMetrics] = field(default_factory=dict)

    def ensure_node(self, node: int) -> NodeMetrics:
        if node not in self.per_node:
            self.per_node[node] = NodeMetrics()
        return self.per_node[node]

    def as_dict(self) -> Dict[str, float]:
        denom = self.completed_requests or 1
        return {
            'requests': float(self.requests),
            'completed_requests': float(self.completed_requests),
            'timed_out_requests': float(self.timed_out_requests),
            'avg_latency_hops': float(self.total_latency_hops / denom),
            'avg_latency_ms': float(self.total_latency_ms / denom),
            'interest_tx': float(self.interest_tx),
            'data_tx': float(self.data_tx),
            'cache_hit_ratio': float(self.cache_hits / denom),
            'cache_hits': float(self.cache_hits),
            'cache_misses': float(self.cache_misses),
            'retransmissions': float(self.retransmissions),
            'queue_drops': float(self.queue_drops),
            'pit_overflow_drops': float(self.pit_overflow_drops),
            'producer_serves': float(self.producer_serves),
            'verification_failures': float(self.verification_failures),
        }


def longest_prefix_match(fib: Dict[str, List[int]], name: str) -> List[int]:
    best_len = -1
    best: List[int] = []
    for prefix, next_hops in fib.items():
        if name.startswith(prefix) and len(prefix) > best_len:
            best_len = len(prefix)
            best = list(next_hops)
    return best
