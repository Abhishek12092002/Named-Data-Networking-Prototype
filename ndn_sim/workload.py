from __future__ import annotations

import random
from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class Catalog:
    names: List[str]


def make_catalog(prefix: str, items: int) -> Catalog:
    if items < 1:
        raise ValueError("items must be >= 1")
    p = prefix.rstrip("/")
    names = [f"{p}/segment/{i}" for i in range(items)]
    return Catalog(names=names)


def generate_requests(
    catalog: Catalog,
    requests: int,
    workload: str,
    seed: int,
    zipf_alpha: float,
) -> List[str]:
    if requests < 1:
        raise ValueError("requests must be >= 1")
    rnd = random.Random(seed)
    w = workload.strip().lower()

    if w == "uniform":
        return [rnd.choice(catalog.names) for _ in range(requests)]

    if w == "zipf":
        n = len(catalog.names)
        weights = [1.0 / ((i + 1) ** zipf_alpha) for i in range(n)]
        s = sum(weights)
        probs = [x / s for x in weights]
        cdf = []
        acc = 0.0
        for p in probs:
            acc += p
            cdf.append(acc)

        def sample_one() -> str:
            r = rnd.random()
            lo, hi = 0, n - 1
            while lo < hi:
                mid = (lo + hi) // 2
                if r <= cdf[mid]:
                    hi = mid
                else:
                    lo = mid + 1
            return catalog.names[lo]

        return [sample_one() for _ in range(requests)]

    raise ValueError(f"unknown workload: {workload}")
