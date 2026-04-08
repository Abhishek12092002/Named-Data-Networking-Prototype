from __future__ import annotations

import random
from collections import Counter, OrderedDict, deque
from dataclasses import dataclass
from typing import Deque, Dict, Generic, Hashable, Optional, TypeVar

K = TypeVar('K', bound=Hashable)
V = TypeVar('V')


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    evictions: int = 0
    admissions_rejected: int = 0


class BaseCache(Generic[K, V]):
    def __init__(self, capacity: int, admit_threshold: int = 1, ttl_ms: float | None = None, seed: int = 0):
        if capacity < 0:
            raise ValueError('capacity must be >= 0')
        self.capacity = capacity
        self.admit_threshold = max(1, admit_threshold)
        self.ttl_ms = ttl_ms
        self.stats = CacheStats()
        self._seen = Counter()
        self._inserted_at: Dict[K, float] = {}
        self._rnd = random.Random(seed)

    def _can_admit(self, key: K) -> bool:
        self._seen[key] += 1
        ok = self._seen[key] >= self.admit_threshold
        if not ok:
            self.stats.admissions_rejected += 1
        return ok

    def _expired(self, key: K, now_ms: float) -> bool:
        if self.ttl_ms is None:
            return False
        ins = self._inserted_at.get(key)
        if ins is None:
            return False
        return now_ms - ins >= self.ttl_ms

    def _touch_insert_time(self, key: K, now_ms: float) -> None:
        if self.ttl_ms is not None:
            self._inserted_at[key] = now_ms

    def get(self, key: K, now_ms: float = 0.0) -> Optional[V]:
        raise NotImplementedError

    def put(self, key: K, value: V, now_ms: float = 0.0) -> None:
        raise NotImplementedError

    def size(self) -> int:
        raise NotImplementedError


class LRUCache(BaseCache[K, V]):
    def __init__(self, capacity: int, **kwargs):
        super().__init__(capacity, **kwargs)
        self._data: OrderedDict[K, V] = OrderedDict()

    def get(self, key: K, now_ms: float = 0.0) -> Optional[V]:
        if self.capacity == 0:
            self.stats.misses += 1
            return None
        if key not in self._data:
            self.stats.misses += 1
            return None
        if self._expired(key, now_ms):
            self._data.pop(key, None)
            self._inserted_at.pop(key, None)
            self.stats.misses += 1
            return None
        self._data.move_to_end(key)
        self.stats.hits += 1
        return self._data[key]

    def put(self, key: K, value: V, now_ms: float = 0.0) -> None:
        if self.capacity == 0 or not self._can_admit(key):
            return
        if key in self._data:
            self._data[key] = value
            self._data.move_to_end(key)
            self._touch_insert_time(key, now_ms)
            return
        if len(self._data) >= self.capacity:
            old, _ = self._data.popitem(last=False)
            self._inserted_at.pop(old, None)
            self.stats.evictions += 1
        self._data[key] = value
        self._touch_insert_time(key, now_ms)

    def size(self) -> int:
        return len(self._data)


class FIFOCache(BaseCache[K, V]):
    def __init__(self, capacity: int, **kwargs):
        super().__init__(capacity, **kwargs)
        self._data: Dict[K, V] = {}
        self._q: Deque[K] = deque()

    def get(self, key: K, now_ms: float = 0.0) -> Optional[V]:
        if self.capacity == 0 or key not in self._data:
            self.stats.misses += 1
            return None
        if self._expired(key, now_ms):
            self._data.pop(key, None)
            self._inserted_at.pop(key, None)
            try:
                self._q.remove(key)
            except ValueError:
                pass
            self.stats.misses += 1
            return None
        self.stats.hits += 1
        return self._data[key]

    def put(self, key: K, value: V, now_ms: float = 0.0) -> None:
        if self.capacity == 0 or not self._can_admit(key):
            return
        if key in self._data:
            self._data[key] = value
            self._touch_insert_time(key, now_ms)
            return
        if len(self._data) >= self.capacity:
            old = self._q.popleft()
            self._data.pop(old, None)
            self._inserted_at.pop(old, None)
            self.stats.evictions += 1
        self._data[key] = value
        self._q.append(key)
        self._touch_insert_time(key, now_ms)

    def size(self) -> int:
        return len(self._data)


class LFUCache(BaseCache[K, V]):
    def __init__(self, capacity: int, **kwargs):
        super().__init__(capacity, **kwargs)
        self._data: Dict[K, V] = {}
        self._freq: Dict[K, int] = {}
        self._age: Dict[K, int] = {}
        self._tick = 0

    def get(self, key: K, now_ms: float = 0.0) -> Optional[V]:
        if self.capacity == 0 or key not in self._data:
            self.stats.misses += 1
            return None
        if self._expired(key, now_ms):
            self._data.pop(key, None)
            self._freq.pop(key, None)
            self._age.pop(key, None)
            self._inserted_at.pop(key, None)
            self.stats.misses += 1
            return None
        self._tick += 1
        self._freq[key] += 1
        self._age[key] = self._tick
        self.stats.hits += 1
        return self._data[key]

    def put(self, key: K, value: V, now_ms: float = 0.0) -> None:
        if self.capacity == 0 or not self._can_admit(key):
            return
        self._tick += 1
        if key in self._data:
            self._data[key] = value
            self._freq[key] += 1
            self._age[key] = self._tick
            self._touch_insert_time(key, now_ms)
            return
        if len(self._data) >= self.capacity:
            victim = min(self._data, key=lambda k: (self._freq.get(k, 0), self._age.get(k, 0)))
            self._data.pop(victim, None)
            self._freq.pop(victim, None)
            self._age.pop(victim, None)
            self._inserted_at.pop(victim, None)
            self.stats.evictions += 1
        self._data[key] = value
        self._freq[key] = 1
        self._age[key] = self._tick
        self._touch_insert_time(key, now_ms)

    def size(self) -> int:
        return len(self._data)


class RandomCache(BaseCache[K, V]):
    def __init__(self, capacity: int, **kwargs):
        super().__init__(capacity, **kwargs)
        self._data: Dict[K, V] = {}

    def get(self, key: K, now_ms: float = 0.0) -> Optional[V]:
        if self.capacity == 0 or key not in self._data:
            self.stats.misses += 1
            return None
        if self._expired(key, now_ms):
            self._data.pop(key, None)
            self._inserted_at.pop(key, None)
            self.stats.misses += 1
            return None
        self.stats.hits += 1
        return self._data[key]

    def put(self, key: K, value: V, now_ms: float = 0.0) -> None:
        if self.capacity == 0 or not self._can_admit(key):
            return
        if key in self._data:
            self._data[key] = value
            self._touch_insert_time(key, now_ms)
            return
        if len(self._data) >= self.capacity:
            victim = self._rnd.choice(list(self._data.keys()))
            self._data.pop(victim, None)
            self._inserted_at.pop(victim, None)
            self.stats.evictions += 1
        self._data[key] = value
        self._touch_insert_time(key, now_ms)

    def size(self) -> int:
        return len(self._data)


def make_cache(policy: str, capacity: int, admit_threshold: int = 1, ttl_ms: float | None = None, seed: int = 0) -> BaseCache[str, bytes]:
    p = policy.strip().lower()
    common = dict(admit_threshold=admit_threshold, ttl_ms=ttl_ms, seed=seed)
    if p == 'lru':
        return LRUCache(capacity, **common)
    if p == 'fifo':
        return FIFOCache(capacity, **common)
    if p == 'lfu':
        return LFUCache(capacity, **common)
    if p == 'random':
        return RandomCache(capacity, **common)
    raise ValueError(f'unknown cache policy: {policy}')
