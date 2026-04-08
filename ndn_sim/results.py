from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from typing import Dict, Iterable, List


@dataclass(frozen=True)
class ResultRow:
    fields: Dict[str, object]


def ensure_out_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def write_csv(path: str, rows: List[ResultRow]) -> None:
    if not rows:
        return
    fieldnames: List[str] = sorted({k for r in rows for k in r.fields.keys()})
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r.fields)
