"""Model sweep on the pro POV dataset: linear vs trees vs Poisson.

Same time split + val MSE for all. Run log appended to .data/fit_pov_runs.jsonl.

Usage:
    python scripts/shorts/sweep_models.py
"""
from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from _pathsetup import ensure  # noqa: E402

ensure()

from shorts.fit_pov_weights import features_from_row, load_dataset  # noqa: E402
from shorts.ml_common import append_run_log  # noqa: E402

RUN_LOG = ROOT / ".data" / "fit_pov_runs.jsonl"
TRAIN_FRAC = 0.8


def frame(rows: list[dict]):
    feats = [features_from_row(r) for r in rows]
    keys = sorted({k for f in feats for k, v in f.items() if v})
    cols = sorted({(k, v) for f in feats for k, v in f.items() if v})
    index = {col: i for i, col in enumerate(cols)}
    import numpy as np
    dim = len(cols)
    mat = np.zeros((len(feats), dim))
    for i, f in enumerate(feats):
        for k, v in f.items():
            if v and (k, v) in index:
                mat[i, index[(k, v)]] = 1.0
    return mat, np.array([math.log10(r["target_views"]) for r in rows]), keys


def main() -> int:
    import numpy as np
    from sklearn.dummy import DummyRegressor
    from sklearn.ensemble import (HistGradientBoostingRegressor,
                                  RandomForestRegressor)
    from sklearn.linear_model import (ElasticNetCV, LassoCV, PoissonRegressor,
                                      RidgeCV)
    rows = load_dataset()
    rows.sort(key=lambda r: r.get("published_at") or "")
    cut = int(len(rows) * TRAIN_FRAC)
    train, val = rows[:cut], rows[cut:]
    mat_train, y_train, _ = frame(train)
    mat_val, y_val, _ = frame(val)
    # Align val columns to train frame.
    _, _, _ = None, None, None
    import sklearn.feature_extraction as _fe  # noqa: F401
    from sklearn.feature_extraction import DictVectorizer
    vec = DictVectorizer(sparse=False)
    feats_train = [{k: v for k, v in features_from_row(r).items()
                    if v is not None} for r in train]
    feats_val = [{k: v for k, v in features_from_row(r).items()
                  if v is not None} for r in val]
    mat_train = vec.fit_transform(feats_train)
    mat_val = vec.transform(feats_val)
    print(f"rows={len(rows)} dim={mat_train.shape[1]}", flush=True)

    models = {
        "dummy": DummyRegressor(),
        "ridge": RidgeCV(alphas=np.logspace(-3, 3, 13)),
        "lasso": LassoCV(max_iter=20000),
        "elasticnet": ElasticNetCV(max_iter=20000),
        "poisson": PoissonRegressor(max_iter=5000),
        "rf": RandomForestRegressor(n_estimators=300, min_samples_leaf=5,
                                    random_state=7, n_jobs=-1),
        "hgb": HistGradientBoostingRegressor(max_iter=300, min_samples_leaf=10,
                                             early_stopping=True,
                                             validation_fraction=0.2,
                                             random_state=7),
    }
    for name, model in models.items():
        started = time.time()
        try:
            model.fit(mat_train, y_train)
            pred = model.predict(mat_val)
            mse = float(np.mean((pred - y_val) ** 2))
            note = ""
            if name == "ridge":
                note = f" alpha={model.alpha_}"
        except Exception as exc:  # noqa: BLE001
            mse, note = float("inf"), f" FAILED {type(exc).__name__}: {exc}"
        elapsed = time.time() - started
        append_run_log(RUN_LOG, {"model": f"sweep-{name}", "mse": mse,
                                 "note": note.strip(),
                                 "elapsed_s": round(elapsed, 1)})
        print(f"  {name:12s} mse={mse:.4f} (~{10 ** math.sqrt(mse):.1f}x)"
              f"{note} [{elapsed:.0f}s]", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
