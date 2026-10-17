"""Numbers for the perf tests: percentiles, and where results are kept."""

from __future__ import annotations

import json
import time
from pathlib import Path

RESULTS = Path(__file__).resolve().parents[2] / ".perf" / "results.jsonl"


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(p / 100 * (len(ordered) - 1))))
    return ordered[index]


def summary(values: list[float]) -> dict[str, float]:
    return {
        "n": len(values),
        "p50": round(percentile(values, 50), 3),
        "p95": round(percentile(values, 95), 3),
        "p99": round(percentile(values, 99), 3),
        "max": round(max(values), 3),
    }


def keep(test: str, name: str, numbers: dict) -> None:
    entry = {"test": test, "name": name, "at": time.time(), **numbers}
    RESULTS.parent.mkdir(exist_ok=True)
    with RESULTS.open("a") as f:
        f.write(json.dumps(entry) + "\n")
