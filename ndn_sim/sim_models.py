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
    random_loss_drops: int = 0
    pit_overflow_drops: int = 0
    producer_serves: int = 0
    control_messages_tx: int = 0
    reconvergence_events: int = 0
    total_reconvergence_ms: float = 0.0
    cache_directives_pushed: int = 0
    # Latency-breakdown accumulators. These sum every successful link
    # traversal's contribution (not just those on completed requests'
    # final paths), so the reported average is "typical per-hop cost
    # observed during the run" -- documented explicitly since it is a
    # different denominator than avg_latency_ms (which averages
    # end-to-end, per-*completed-request* latency).
    total_propagation_ms: float = 0.0
    total_transmission_ms: float = 0.0
    total_queueing_ms: float = 0.0
    total_processing_ms: float = 0.0
    link_traversals: int = 0
    per_consumer_latency_ms: Dict[int, List[float]] = field(default_factory=dict)
    per_node: Dict[int, NodeMetrics] = field(default_factory=dict)

    def record_traversal(self, breakdown) -> None:
        self.total_propagation_ms += breakdown.propagation_ms
        self.total_transmission_ms += breakdown.transmission_ms
        self.total_queueing_ms += breakdown.queueing_ms
        self.total_processing_ms += breakdown.processing_ms
        self.link_traversals += 1

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
            'delivery_ratio': float(self.completed_requests / self.requests) if self.requests else 0.0,
            'avg_latency_hops': float(self.total_latency_hops / denom),
            'avg_latency_ms': float(self.total_latency_ms / denom),
            'interest_tx': float(self.interest_tx),
            'data_tx': float(self.data_tx),
            'dropped_interests': float(self.dropped_interests),
            'dropped_data': float(self.dropped_data),
            'cache_hit_ratio': float(self.cache_hits / denom),
            'cache_hits': float(self.cache_hits),
            'cache_misses': float(self.cache_misses),
            'retransmissions': float(self.retransmissions),
            'queue_drops': float(self.queue_drops),
            'random_loss_drops': float(self.random_loss_drops),
            'pit_overflow_drops': float(self.pit_overflow_drops),
            'producer_serves': float(self.producer_serves),
            'verification_failures': float(self.verification_failures),
            'control_messages_tx': float(self.control_messages_tx),
            'reconvergence_events': float(self.reconvergence_events),
            'avg_reconvergence_ms': float(self.total_reconvergence_ms / self.reconvergence_events) if self.reconvergence_events else 0.0,
            'cache_directives_pushed': float(self.cache_directives_pushed),
            # Per-hop latency breakdown, averaged over every successful link
            # traversal observed during the run (see record_traversal doc).
            'avg_propagation_ms': float(self.total_propagation_ms / self.link_traversals) if self.link_traversals else 0.0,
            'avg_transmission_ms': float(self.total_transmission_ms / self.link_traversals) if self.link_traversals else 0.0,
            'avg_queueing_ms': float(self.total_queueing_ms / self.link_traversals) if self.link_traversals else 0.0,
            'avg_processing_ms': float(self.total_processing_ms / self.link_traversals) if self.link_traversals else 0.0,
        }


def longest_prefix_match(fib: Dict[str, List[int]], name: str) -> List[int]:
    best_len = -1
    best: List[int] = []
    for prefix, next_hops in fib.items():
        if name.startswith(prefix) and len(prefix) > best_len:
            best_len = len(prefix)
            best = list(next_hops)
    return best
