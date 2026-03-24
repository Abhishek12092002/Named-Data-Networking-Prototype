from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Deque, Dict, Generic, Hashable, Optional, TypeVar


K = TypeVar("K", bound=Hashable)
V = TypeVar("V")


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    evictions: int = 0


class BaseCache(Generic[K, V]):
    def __init__(self, capacity: int):
        if capacity < 0:
            raise ValueError("capacity must be >= 0")
        self.capacity = capacity
        self.stats = CacheStats()

    def get(self, key: K) -> Optional[V]:
        raise NotImplementedError

    def put(self, key: K, value: V) -> None:
        raise NotImplementedError


class LRUCache(BaseCache[K, V]):
    def __init__(self, capacity: int):
        super().__init__(capacity)
        self._data: "OrderedDict[K, V]" = OrderedDict()

    def get(self, key: K) -> Optional[V]:
        if self.capacity == 0:
            self.stats.misses += 1
            return None
        if key not in self._data:
            self.stats.misses += 1
            return None
        self._data.move_to_end(key)
        self.stats.hits += 1
        return self._data[key]

    def put(self, key: K, value: V) -> None:
        if self.capacity == 0:
            return
        if key in self._data:
            self._data[key] = value
            self._data.move_to_end(key)
            return
        if len(self._data) >= self.capacity:
            self._data.popitem(last=False)
            self.stats.evictions += 1
        self._data[key] = value


class FIFOCache(BaseCache[K, V]):
    def __init__(self, capacity: int):
        super().__init__(capacity)
        self._data: Dict[K, V] = {}
        self._q: Deque[K] = deque()

    def get(self, key: K) -> Optional[V]:
        if self.capacity == 0:
            self.stats.misses += 1
            return None
        if key not in self._data:
            self.stats.misses += 1
            return None
        self.stats.hits += 1
        return self._data[key]

    def put(self, key: K, value: V) -> None:
        if self.capacity == 0:
            return
        if key in self._data:
            self._data[key] = value
            return
        if len(self._data) >= self.capacity:
            old = self._q.popleft()
            self._data.pop(old, None)
            self.stats.evictions += 1
        self._data[key] = value
        self._q.append(key)


def make_cache(policy: str, capacity: int) -> BaseCache[str, bytes]:
    p = policy.strip().lower()
    if p == "lru":
        return LRUCache(capacity)
    if p == "fifo":
        return FIFOCache(capacity)
    raise ValueError(f"unknown cache policy: {policy}")
