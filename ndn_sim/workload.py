from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Dict, List, Sequence


@dataclass(frozen=True)
class Catalog:
    names: List[str]
    prefix_to_names: Dict[str, List[str]]


@dataclass(frozen=True)
class RequestSpec:
    consumer: int
    name: str
    start_ms: float


DEFAULT_PREFIXES = ('/video', '/news', '/iot')


def make_catalog(prefixes: Sequence[str], items_per_prefix: int) -> Catalog:
    if items_per_prefix < 1:
        raise ValueError('items_per_prefix must be >= 1')
    names: List[str] = []
    p2n: Dict[str, List[str]] = {}
    for prefix in prefixes:
        p = prefix.rstrip('/')
        bucket = [f'{p}/segment/{i}' for i in range(items_per_prefix)]
        p2n[p] = bucket
        names.extend(bucket)
    return Catalog(names=names, prefix_to_names=p2n)


def _zipf_sample(items: List[str], rnd: random.Random, alpha: float) -> str:
    n = len(items)
    weights = [1.0 / ((i + 1) ** alpha) for i in range(n)]
    s = sum(weights)
    cdf = []
    acc = 0.0
    for w in weights:
        acc += w / s
        cdf.append(acc)
    r = rnd.random()
    lo, hi = 0, n - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if r <= cdf[mid]:
            hi = mid
        else:
            lo = mid + 1
    return items[lo]


def generate_requests(
    catalog: Catalog,
    requests: int,
    workload: str,
    seed: int,
    zipf_alpha: float,
    consumers: Sequence[int],
    interest_iat_ms: float,
    burst_size: int = 10,
) -> List[RequestSpec]:
    if requests < 1:
        raise ValueError('requests must be >= 1')
    if not consumers:
        raise ValueError('need at least one consumer')
    rnd = random.Random(seed)
    prefixes = list(catalog.prefix_to_names)
    specs: List[RequestSpec] = []
    workload = workload.strip().lower()

    def choose_name(i: int) -> str:
        if workload == 'uniform':
            return rnd.choice(catalog.names)
        if workload == 'zipf':
            prefix = rnd.choice(prefixes)
            return _zipf_sample(catalog.prefix_to_names[prefix], rnd, zipf_alpha)
        if workload == 'bursty':
            hot_prefix = prefixes[(i // max(1, burst_size)) % len(prefixes)]
            hot = catalog.prefix_to_names[hot_prefix]
            if rnd.random() < 0.8:
                return _zipf_sample(hot, rnd, max(zipf_alpha, 1.1))
            return rnd.choice(catalog.names)
        if workload == 'shifted':
            pivot = requests // 2
            hot_prefix = prefixes[0 if i < pivot else min(1, len(prefixes) - 1)]
            return _zipf_sample(catalog.prefix_to_names[hot_prefix], rnd, max(zipf_alpha, 1.1))
        raise ValueError(f'unknown workload: {workload}')

    t = 0.0
    for i in range(requests):
        name = choose_name(i)
        consumer = consumers[i % len(consumers)]
        if len(consumers) > 1:
            consumer = rnd.choice(list(consumers))
        specs.append(RequestSpec(consumer=consumer, name=name, start_ms=t))
        t += float(interest_iat_ms)
    return specs
