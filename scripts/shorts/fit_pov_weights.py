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
                d[key] = d.get(key, 0.0) + alpha * err


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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dataset", type=Path, default=DATASET)
    ap.add_argument("--out", type=Path, default=OUT_DEFAULT)
    args = ap.parse_args()

    rows = load_dataset(args.dataset)
    rows.sort(key=lambda r: r.get("published_at") or "")
    cut = int(len(rows) * TRAIN_FRAC)
    train, val = rows[:cut], rows[cut:]
    print(f"rows={len(rows)} train={len(train)} val={len(val)}", flush=True)

    base = evaluate(val, new_weights())
    print(f"baseline(zero): mse={base['mse']:.3f} (~{base['rmse_x']:.1f}x) "
          f"mae={base['mae']:.3f}", flush=True)

    keys = [group for group in GROUPS if len(GRID[group]) > 1]
    # Stage 1: single-group screen — keep groups that beat zero alone.
    screened = []
    for group in keys:
        alphas = {other: 0.0 for other in GROUPS}
        alphas[group] = GRID[group][1]
        alphas.update({"bias": GRID["bias"][0],
                       "channel": GRID["channel"][0]})
        weights = new_weights()
        for _ in range(EPOCHS[0]):
            sgd_epoch(train, weights, {}, alphas=alphas)
        score = evaluate(val, weights)
        print(f"  [screen] {group} -> mse={score['mse']:.3f} "
              f"(zero={base['mse']:.3f})", flush=True)
        if score["mse"] < base["mse"]:
            screened.append(group)
    print(f"  screened groups: {screened}", flush=True)
    keys = screened
    best = None
    for combo in itertools.product(*(GRID[k] for k in keys)):
        alphas = {group: 0.0 for group in GROUPS}
        alphas.update({k: v for k, v in zip(keys, combo)})
        alphas.update({"bias": GRID["bias"][0],
                       "channel": GRID["channel"][0]})
        for epochs in EPOCHS:
            weights = new_weights()
            for _ in range(epochs):
                sgd_epoch(train, weights, {}, alphas=alphas)
            score = evaluate(val, weights)
            tag = " ".join(f"{k}={alphas[k]}" for k in keys)
            print(f"  {tag} -> mse={score['mse']:.3f} "
                  f"(~{score['rmse_x']:.1f}x) mae={score['mae']:.3f} "
                  f"spear={score['spearman']:.3f}", flush=True)
            if best is None or score["mse"] < best[0]:
                best = (score["mse"], alphas, epochs, weights, score)

    _, alphas, epochs, weights, score = best
    payload = {"alphas": alphas, "epochs": epochs, "val": score,
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
