"""Fit POV views on the pro POV dataset (LIM pro channel).

Target: log10(views). Features: player, org, map, opp_tier, rating, kd,
decider, won, ot, derby, stage, tier, weekday. Per-group SGD alphas,
time-ordered train/val, best val MSE wins.

Usage:
    python scripts/shorts/fit_pov_weights.py [--dataset PATH]
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from _pathsetup import ensure  # noqa: E402

ensure()

from shorts.fit_clip_weights import spearman  # noqa: E402

DATASET = ROOT / ".data" / "pro_pov_dataset.jsonl"
OUT_DEFAULT = ROOT / ".data" / "pov_kind_weights.json"
TRAIN_FRAC = 0.8

GROUPS = ("player", "org", "map", "opp_tier", "rating", "kd", "decider",
          "won", "ot", "derby", "stage", "tier", "weekday")


def _hour_bucket(raw) -> str:
    try:
        hour = int(str(raw).split(":")[0])
    except (TypeError, ValueError):
        return "unknown"
    return f"h{hour // 6 * 6:02d}"


def _derby_bucket(views) -> str:
    try:
        views = float(views or 0)
    except (TypeError, ValueError):
        return "none"
    if views <= 0:
        return "none"
    if views >= 100_000:
        return "hot"
    if views >= 10_000:
        return "warm"
    return "cold"


def features_from_row(row: dict) -> dict:
    return {
        "player": str(row.get("player") or ""),
        "org": str(row.get("org") or "") or None,
        "map": str(row.get("map") or "unknown"),
        "opp_tier": str(row.get("opp_tier") or "unranked"),
        "rating": str(row.get("rating_bucket") or "unknown"),
        "kd": str(row.get("kd_bucket") or "unknown"),
        "decider": str(row.get("decider") or "unknown"),
        "won": str(row.get("won") or "unknown"),
        "ot": str(row.get("ot") or "unknown"),
        "derby": _derby_bucket(row.get("derby_views")),
        "stage": str(row.get("stage") or "other"),
        "tier": str(row.get("tier") or "regular"),
        "weekday": str(row.get("publish_weekday") or "unknown"),
    }


# Back-compat alias (tests + callers use POV-row dicts the same way).
def features_from_pov(row: dict, *, org_of=None) -> dict:
    feats = features_from_row(row)
    if org_of and not feats["org"]:
        feats["org"] = org_of(feats["player"])
    return feats


def new_weights() -> dict:
    return {"bias": 0.0, **{group: {} for group in GROUPS}}


def predict_log_views(feats: dict, weights: dict) -> float:
    total = weights["bias"]
    for group in GROUPS:
        key = feats.get(group)
        if key:
            total += weights[group].get(key, 0.0)
    return total


def predict_log_vpd(feats: dict, weights: dict, *,
                    channel_bias: dict | None = None, org_of=None) -> float:
    del org_of
    total = predict_log_views(feats, weights)
    if channel_bias:
        total += channel_bias.get(feats.get("_channel", ""), 0.0)
    return total


def sgd_epoch(rows, weights: dict, channel_bias: dict,
              *, alphas: dict, org_of=None) -> None:
    l2 = alphas.get("l2", 0.0)
    for row in rows:
        views = row.get("target_views") or 0
        if views <= 0:
            continue
        feats = features_from_row(row)
        pred = predict_log_views(feats, weights)
        if alphas.get("channel"):
            pred += channel_bias.get("single", 0.0)
        err = math.log10(views) - pred
        if alphas.get("channel"):
            channel_bias["single"] = channel_bias.get("single", 0.0) \
                + alphas["channel"] * err
        if alphas.get("bias"):
            weights["bias"] += alphas["bias"] * err
        for group in GROUPS:
            alpha = alphas.get(group, 0.0)
            key = feats.get(group)
            if key and alpha:
                d = weights[group]
                grad = alpha * err - alpha * l2 * d.get(key, 0.0)
                d[key] = d.get(key, 0.0) + grad


def load_dataset(path: Path | None = None) -> list[dict]:
    with (path or DATASET).open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def evaluate(rows, weights: dict) -> dict:
    se_sum, ae_sum, n = 0.0, 0.0, 0
    preds, actual = [], []
    for row in rows:
        views = row.get("target_views") or 0
        if views <= 0:
            continue
        pred = predict_log_views(features_from_row(row), weights)
        label = math.log10(views)
        se_sum += (pred - label) ** 2
        ae_sum += abs(pred - label)
        n += 1
        preds.append(pred)
        actual.append(label)
    mse = se_sum / n if n else float("inf")
    return {"n": n, "mse": mse,
            "rmse_x": 10 ** math.sqrt(mse) if n else float("inf"),
            "mae": ae_sum / n if n else float("inf"),
            "spearman": spearman(preds, actual) if n >= 3 else 0.0}


GRID = {
    "player": [0.0, 0.005],
    "org": [0.0, 0.005],
    "map": [0.0, 0.01],
    "opp_tier": [0.0, 0.005],
    "rating": [0.0, 0.01],
    "kd": [0.0, 0.01],
    "decider": [0.0, 0.005],
    "won": [0.0, 0.005],
    "ot": [0.0, 0.005],
    "derby": [0.0, 0.005],
    "stage": [0.0, 0.005],
    "tier": [0.0, 0.005],
    "weekday": [0.0],
    "bias": [0.01],
    "channel": [0.0],
}
EPOCHS = [6]


def train_run(train, val, alphas: dict, max_epochs: int = 40,
              patience: int = 5) -> tuple[dict, list, int, float]:
    """SGD with early stopping on val MSE. Returns (weights, history,
    best_epoch, best_mse). History holds per-epoch train/val MSE."""
    import copy
    weights = new_weights()
    history: list[dict] = []
    best: tuple[float, dict, int] | None = None
    stale = 0
    for epoch in range(1, max_epochs + 1):
        sgd_epoch(train, weights, {}, alphas=alphas)
        train_mse = evaluate(train, weights)["mse"]
        val_mse = evaluate(val, weights)["mse"]
        history.append({"epoch": epoch, "train_mse": train_mse,
                        "val_mse": val_mse})
        if best is None or val_mse < best[0] - 1e-6:
            best = (val_mse, copy.deepcopy(weights), epoch)
            stale = 0
        else:
            stale += 1
        if stale >= patience:
            break
    assert best is not None
    return best[1], history, best[2], best[0]


REF_ALPHAS = {"player": 0.005, "org": 0.005, "map": 0.0,
              "opp_tier": 0.005, "rating": 0.0, "kd": 0.0,
              "decider": 0.0, "won": 0.0, "ot": 0.0, "derby": 0.0,
              "stage": 0.0, "tier": 0.005, "weekday": 0.0,
              "bias": 0.01, "channel": 0.0, "l2": 0.0}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dataset", type=Path, default=DATASET)
    ap.add_argument("--lr-scales", type=float, nargs="+",
                    default=[0.25, 0.5, 1.0, 2.0, 4.0])
    ap.add_argument("--max-epochs", type=int, default=40)
    ap.add_argument("--patience", type=int, default=5)
    ap.add_argument("--out", type=Path, default=OUT_DEFAULT)
    args = ap.parse_args()

    rows = load_dataset(args.dataset)
    rows.sort(key=lambda r: r.get("published_at") or "")
    cut = int(len(rows) * TRAIN_FRAC)
    train, val = rows[:cut], rows[cut:]
    print(f"rows={len(rows)} train={len(train)} val={len(val)}", flush=True)

    from shorts.ml_common import append_run_log
    run_log = ROOT / ".data" / "fit_pov_runs.jsonl"

    base = evaluate(val, new_weights())
    print(f"baseline(zero): mse={base['mse']:.3f} (~{base['rmse_x']:.1f}x) "
          f"mae={base['mae']:.3f}", flush=True)

    from shorts.ml_common import append_run_log
    run_log = ROOT / ".data" / "fit_pov_runs.jsonl"

    # Phase 1: LR sweep (scale reference alphas) with early stopping.
    lr_results = []
    for scale in args.lr_scales:
        alphas = {k: (v * scale if k not in ("bias", "channel", "l2")
                       else v) for k, v in REF_ALPHAS.items()}
        weights, history, epoch, mse = train_run(
            train, val, alphas, max_epochs=args.max_epochs,
            patience=args.patience)
        append_run_log(run_log, {"model": "pov", "phase": "lr",
                                 "scale": scale, "epoch": epoch,
                                 "mse": mse, "history": history})
        lr_results.append((mse, scale, epoch, weights, history))
        print(f"  [lr] scale={scale:<5} stopped={epoch:<3} mse={mse:.4f}",
              flush=True)
    lr_results.sort(key=lambda t: t[0])
    _, best_scale, best_epoch, _, _ = lr_results[0]
    print(f"  best lr scale={best_scale} epoch={best_epoch}", flush=True)

    ref = {k: (v * best_scale if k not in ("bias", "channel", "l2")
               else v) for k, v in REF_ALPHAS.items()}

    # Phase 2: ablations — drop each active group, add each inactive one.
    def _run(tag: str, alphas: dict) -> tuple[float, dict]:
        weights, _, epoch, mse = train_run(
            train, val, alphas, max_epochs=args.max_epochs,
            patience=args.patience)
        append_run_log(run_log, {"model": "pov", "phase": "ablate",
                                 "tag": tag, "epoch": epoch, "mse": mse})
        print(f"  [ablate] {tag:24s} stopped={epoch:<3} mse={mse:.4f}",
              flush=True)
        return mse, weights

    full_mse, full_w = _run("full", dict(ref))
    best = (full_mse, dict(ref), full_w, "full")
    active = [g for g in GROUPS if ref.get(g, 0.0) > 0]
    for group in active:
        trial = dict(ref)
        trial[group] = 0.0
        mse, weights = _run(f"drop-{group}", trial)
        if mse < best[0]:
            best = (mse, trial, weights, f"drop-{group}")
    inactive = [g for g in GROUPS if ref.get(g, 0.0) == 0.0]
    for group in inactive:
        trial = dict(ref)
        trial[group] = 0.005 * best_scale
        mse, weights = _run(f"add-{group}", trial)
        if mse < best[0]:
            best = (mse, trial, weights, f"add-{group}")

    mse, alphas, weights, tag = best
    score = evaluate(val, weights)
    epochs = None

    payload = {"alphas": alphas, "ablation": tag, "val": score,
               "weights": weights,
               "method": "log10(views) = bias + 13 feature groups; "
                         "per-video SGD; LIM pro dataset"}
    args.out.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    print(f"BEST mse={score['mse']:.3f} (~{score['rmse_x']:.1f}x) "
          f"mae={score['mae']:.3f} spear={score['spearman']:.3f}", flush=True)
    print(f"alphas={alphas}", flush=True)
    for group in GROUPS:
        items = sorted(weights[group].items(), key=lambda kv: -kv[1])[:6]
        shown = [item for item in items if abs(item[1]) > 1e-9]
        if shown:
            print(f"{group}:", flush=True)
            for name, val_ in shown:
                print(f"  {str(name):14s} {val_:+.3f}  (~{10 ** val_:.2f}x)",
                      flush=True)
    print(args.out, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
