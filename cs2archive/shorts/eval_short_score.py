"""Read-only, match-held-out predicted-view loss against actual Allstar views.

python -m cs2archive.shorts.eval_short_score [--json] [--output report.json]

No selection simulation and no production model/ledger writes. The default is
five-fold grouped out-of-fold evaluation: each clip is predicted by a model
that never saw its match. No chronological claims are made from scrape dates.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

from cs2archive.shorts.allstar_data import load_allstar_dataset
from cs2archive.shorts.fit_partial_stars import ALLSTAR_JSONL, _recognised_steamids, fit_partial_stars, rows_in_window
from cs2archive.shorts.view_prediction import predict_log_views


def match_fold(match_id: str, *, seed: str, folds: int) -> int:
    return int.from_bytes(hashlib.sha256(f"{seed}:{match_id}".encode()).digest()[:8], "big") % folds


def losses(actual: list[float], predictions: list[float]) -> dict:
    if not actual or len(actual) != len(predictions):
        raise ValueError("loss requires nonempty matching actual/prediction vectors")
    errors = [p - a for p, a in zip(predictions, actual)]
    log_errors = [math.log(p) - math.log(a) for p, a in zip(predictions, actual)]
    return {
        "n": len(actual),
        "view_mse": sum(e * e for e in errors) / len(errors),
        "view_rmse": math.sqrt(sum(e * e for e in errors) / len(errors)),
        "view_mae": sum(abs(e) for e in errors) / len(errors),
        "log_mse": sum(e * e for e in log_errors) / len(errors),
        "log_rmse": math.sqrt(sum(e * e for e in log_errors) / len(errors)),
        "log_mae": sum(abs(e) for e in log_errors) / len(errors),
    }


def evaluate(rows: list[dict], *, folds: int = 5, seed: str = "shorts-v2",
             l2: float = 8.0, recognised: set[str] | None = None) -> dict:
    if folds < 2:
        raise ValueError("at least two held-out folds are required")
    fold_ids = [match_fold(str(r["match_id"]), seed=seed, folds=folds) for r in rows]
    recognised = _recognised_steamids() if recognised is None else recognised
    actual, predicted, geometric, arithmetic = [], [], [], []
    pro_actual, pro_predicted, pro_geometric, pro_arithmetic = [], [], [], []
    splits = []
    for fold in range(folds):
        train = [r for r, f in zip(rows, fold_ids) if f != fold]
        test = [r for r, f in zip(rows, fold_ids) if f == fold]
        if not test or len(train) < 2:
            raise ValueError(f"fold {fold} has insufficient matches; use fewer folds or more data")
        model = fit_partial_stars(train, l2=l2, recognised=recognised)
        geo = math.exp(sum(math.log(float(r["views"])) for r in train) / len(train))
        mean = sum(float(r["views"]) for r in train) / len(train)
        train_matches = {str(r["match_id"]) for r in train}
        test_matches = {str(r["match_id"]) for r in test}
        if not train_matches.isdisjoint(test_matches):
            raise ValueError("training/test match overlap")
        splits.append(dict(fold=fold, train_clips=len(train), test_clips=len(test),
                           train_matches=len(train_matches), test_matches=len(test_matches)))
        for row in test:
            actual.append(float(row["views"]))
            predicted.append(math.exp(predict_log_views(row, model)))
            geometric.append(geo)
            arithmetic.append(mean)
            if str(row.get("steamid") or "") in recognised:
                pro_actual.append(actual[-1])
                pro_predicted.append(predicted[-1])
                pro_geometric.append(geo)
                pro_arithmetic.append(mean)
    return {
        "protocol": "Allstar-only match-grouped out-of-fold; refit on train only",
        "target": "natural log of positive snapshot views; exp prediction is geometric/typical",
        "seed": seed, "folds": splits, "l2": l2,
        "model": losses(actual, predicted),
        "geometric_baseline": losses(actual, geometric),
        "arithmetic_baseline": losses(actual, arithmetic),
        "recognized_pro_subset": {
            "model": losses(pro_actual, pro_predicted),
            "geometric_baseline": losses(pro_actual, pro_geometric),
            "arithmetic_baseline": losses(pro_actual, pro_arithmetic),
        } if pro_actual else {"n": 0},
        "limitations": [
            "Label-derived clip features; not exact detector-window validation.",
            "Distinct clip IDs for the same moment are not merged.",
            "Snapshot views, not final lifetime views or our YouTube channel views.",
            "Missing publication times retained with age=0; no temporal coverage claim.",
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", type=Path, default=ALLSTAR_JSONL)
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--seed", default="shorts-v2")
    ap.add_argument("--l2", type=float, default=8.0)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--output", type=Path)
    args = ap.parse_args()
    if not math.isfinite(args.l2) or args.l2 < 0:
        ap.error("l2 must be finite and nonnegative")
    rows, stats = load_allstar_dataset(args.data)
    usable = []
    invalid = zero = missing_match = 0
    for row in rows:
        try:
            views = float(row.get("views"))
        except (TypeError, ValueError):
            invalid += 1
            continue
        if not math.isfinite(views) or views < 0:
            invalid += 1
        elif views == 0:
            zero += 1
        elif not row.get("match_id"):
            missing_match += 1
        else:
            usable.append(row)
    before_window = len(usable)
    usable = rows_in_window(usable)
    stats.update(invalid_views=invalid, zero_views=zero, missing_match_id=missing_match,
                 excluded_by_age=before_window - len(usable), evaluated_clips=len(usable),
                 evaluated_matches=len({r["match_id"] for r in usable}),
                 known_ages=sum(r.get("age_days") is not None for r in usable))
    try:
        report = {"data": stats, **evaluate(usable, folds=args.folds, seed=args.seed, l2=args.l2)}
    except ValueError as exc:
        ap.error(str(exc))
    encoded = json.dumps(report, indent=2, allow_nan=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    if args.json:
        print(encoded)
    else:
        print(report["protocol"])
        print(f"Data: {json.dumps(stats, sort_keys=True)}")
        print(f"Seed={args.seed}, l2={args.l2}")
        for split in report["folds"]:
            print(f"  {split}")
        for label in ("model", "geometric_baseline", "arithmetic_baseline"):
            m = report[label]
            print(f"{label}: n={m['n']}, views RMSE={m['view_rmse']:,.0f}, "
                  f"MAE={m['view_mae']:,.0f}; log RMSE={m['log_rmse']:.3f}, MAE={m['log_mae']:.3f}")
        subset = report["recognized_pro_subset"]
        if "model" in subset:
            m = subset["model"]
            print(f"recognized_pro_subset: n={m['n']}, views RMSE={m['view_rmse']:,.0f}, "
                  f"MAE={m['view_mae']:,.0f}; log RMSE={m['log_rmse']:.3f}, MAE={m['log_mae']:.3f}")
        for limitation in report["limitations"]:
            print(f"Note: {limitation}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
