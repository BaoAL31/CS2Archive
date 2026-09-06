"""Shared ML kit for the view fitters: time folds, RNG search, run log."""
from __future__ import annotations

import json
import math
import random
from datetime import datetime, timezone
from pathlib import Path


def time_folds(keys: list, n_folds: int = 3, train_frac: float = 0.8):
    """Time-ordered (train, val) folds. keys must be pre-sorted oldest-first.

    Fold k trains on [:cut_k] and vals on [cut_k:cut_{k+1}] so every val
    block sits strictly after its train block (no leakage).
    """
    n = len(keys)
    cuts = [int(n * (train_frac + (1.0 - train_frac) * k / n_folds))
            for k in range(n_folds + 1)]
    cuts[0], cuts[-1] = min(cuts[0], n - 1), n
    for k in range(n_folds):
        lo, mid = cuts[k], cuts[k + 1] if False else None
        _ = mid
        hi = cuts[k + 1] if k + 1 <= n_folds else n
        lo_bound = cuts[k]
        hi_bound = cuts[k + 1]
        yield keys[:lo_bound], keys[lo_bound:hi_bound]


def loguniform(rng: random.Random, lo: float, hi: float) -> float:
    return math.exp(rng.uniform(math.log(lo), math.log(hi)))


def sample_config(rng: random.Random, groups: list[str],
                  lo: float = 1e-3, hi: float = 2e-1) -> dict[str, float]:
    """Per-group learning rates + L2, log-uniform. Zero allowed via coin flip."""
    cfg: dict[str, float] = {}
    for group in groups:
        if rng.random() < 0.25:
            cfg[group] = 0.0
        else:
            cfg[group] = loguniform(rng, lo, hi)
    cfg["l2"] = loguniform(rng, 1e-5, 1e-1) if rng.random() < 0.75 else 0.0
    return cfg


def append_run_log(path: Path, record: dict) -> None:
    record = {"at": datetime.now(timezone.utc).isoformat(), **record}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
