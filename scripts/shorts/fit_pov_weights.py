"""Fit POV views on the pro POV dataset (LIM pro channel).

Target: log10(21-day plateau views). Features: player, org, map, opp_tier, rating, kd,
decider, won, ot, derby, stage, tier, weekday. Per-group SGD alphas,
time-ordered train/val/test (80/10/10), best val MSE wins.

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
# Tail after TRAIN_FRAC is split in half: first half val, rest test.
VAL_OF_TAIL = 0.5
# LIM views mostly plateau by ~3 weeks; young videos are scaled up to that
# window so lifetime counts are comparable.
PLATEAU_DAYS = 21.0

GROUPS = ("player", "org", "map", "opp_tier", "rating", "kd", "decider",
          "won", "ot", "derby", "stage", "tier", "weekday", "multi")


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


def opp_tier_of(rank) -> str:
    try:
        rank = int(rank or 0)
    except (TypeError, ValueError):
        rank = 0
    if rank <= 0:
        return "unranked"
    if rank <= 5:
        return "top5"
    if rank <= 10:
        return "top10"
    if rank <= 20:
        return "top20"
    return "top30"


def features_from_row(row: dict) -> dict:
    game_map = str(row.get("map") or "unknown").strip().lower() or "unknown"
    return {
        "_channel": str(row.get("channel") or ""),
        "player": str(row.get("player") or "").strip().lower(),
        "org": str(row.get("org") or "") or None,
        "map": game_map,
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
        "multi": str(row.get("multi") or "unknown"),
    }


def row_from_card(meta: dict, team: str, opponent: str,
                  ranking: dict | None = None, *,
                  derby_views=None) -> dict:
    """Backlog card -> dataset-shaped row so serve uses features_from_row."""
    from shorts.pro_context import (event_tier, kd_bucket, normalize_stage,
                                    parse_kd_ratio, rating_bucket)

    ranking = ranking or {}
    opp = str(opponent or "").strip()
    rank = ranking.get(opp, 0) or 0
    if not rank and opp:
        lowered = {str(k).casefold(): v for k, v in ranking.items()}
        rank = lowered.get(opp.casefold(), 0) or 0
    try:
        raw_rating = meta.get("rating")
        rating = float(raw_rating) if raw_rating not in (None, "") else None
    except (TypeError, ValueError):
        rating = None
    tournament = str(meta.get("tournament") or "")
    heat = derby_views if derby_views is not None else meta.get("derby_views")
    return {
        "player": str(meta.get("player") or "").strip().lower(),
        "org": str(team or "").strip() or None,
        "map": str(meta.get("map") or "unknown"),
        "opp": opp or "unknown",
        "opp_tier": opp_tier_of(rank),
        "rating": rating,
        "rating_bucket": rating_bucket(rating),
        "kd": parse_kd_ratio(meta.get("kd")),
        "kd_bucket": kd_bucket(parse_kd_ratio(meta.get("kd"))),
        "decider": str(meta.get("decider") or "unknown"),
        "won": str(meta.get("won") or "unknown"),
        "ot": str(meta.get("ot") or "unknown"),
        "derby_views": heat,
        "stage": str(meta.get("stage") or normalize_stage(tournament) or "other"),
        "tier": event_tier(tournament),
        "publish_weekday": str(meta.get("publish_weekday") or "unknown"),
        "channel": str(meta.get("channel") or ""),
        "multi": str(meta.get("multi") or "unknown"),
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
            total += weights.get(group, {}).get(key, 0.0)
    return total


def predict_log_vpd(feats: dict, weights: dict, *,
                    channel_bias: dict | None = None, org_of=None) -> float:
    del org_of
    total = predict_log_views(feats, weights)
    if channel_bias:
        total += channel_bias.get(feats.get("_channel", ""), 0.0)
    return total


def label_views(row: dict) -> float:
    """21-day plateau views: mature rows keep lifetime; younger scale up."""
    try:
        views = float(row.get("target_views") or 0)
    except (TypeError, ValueError):
        return 0.0
    if views <= 0:
        return 0.0
    try:
        age = float(row.get("age_days") or 0)
    except (TypeError, ValueError):
        age = 0.0
    if age <= 0:
        return views
    return views * PLATEAU_DAYS / min(age, PLATEAU_DAYS)


def sgd_epoch(rows, weights: dict, channel_bias: dict,
              *, alphas: dict, org_of=None) -> None:
    l2 = alphas.get("l2", 0.0)
    for row in rows:
        views = label_views(row)
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


def time_split(rows: list, *, train_frac: float = TRAIN_FRAC,
               val_of_tail: float = VAL_OF_TAIL) -> tuple[list, list, list]:
    """Oldest train_frac train; remaining tail split val then test."""
    ordered = sorted(rows, key=lambda r: r.get("published_at") or "")
    cut = int(len(ordered) * train_frac)
    tail = ordered[cut:]
    val_cut = int(len(tail) * val_of_tail)
    return ordered[:cut], tail[:val_cut], tail[val_cut:]


def match_key(row: dict) -> str:
    """Same-day fixture pair (order-invariant) for within-match ranking."""
    org = str(row.get("org") or "").strip().casefold()
    opp = str(row.get("opp") or "").strip().casefold()
    a, b = sorted([org, opp])
    day = str(row.get("published_at") or "")[:10]
    return f"{day}|{a}|{b}"


def evaluate(rows, weights: dict) -> dict:
    se_sum, ae_sum, n = 0.0, 0.0, 0
    preds, actual = [], []
    for row in rows:
        views = label_views(row)
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


def evaluate_within_match(rows, weights: dict) -> dict:
    """Pairwise / top-1 among same-day fixture POVs (listener's actual job)."""
    from collections import defaultdict
    groups: dict[str, list] = defaultdict(list)
    for row in rows:
        views = label_views(row)
        if views <= 0:
            continue
        groups[match_key(row)].append(row)
    pair_hit, pair_tot = 0, 0
    top1_hit, top1_n = 0, 0
    spes = []
    usable = 0
    for items in groups.values():
        if len(items) < 2:
            continue
        usable += 1
        preds = [predict_log_views(features_from_row(r), weights) for r in items]
        actual = [math.log10(label_views(r)) for r in items]
        if len(items) >= 3:
            spes.append(spearman(preds, actual))
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                if actual[i] == actual[j]:
                    continue
                pair_tot += 1
                if (preds[i] > preds[j]) == (actual[i] > actual[j]):
                    pair_hit += 1
        best_act = max(actual)
        picked = actual[max(range(len(items)), key=lambda i: preds[i])]
        top1_n += 1
        if picked >= best_act - 1e-12:
            top1_hit += 1
    return {"matches": usable,
            "pairwise_n": pair_tot,
            "pairwise_acc": pair_hit / pair_tot if pair_tot else 0.0,
            "top1_acc": top1_hit / top1_n if top1_n else 0.0,
            "mean_spearman": (sum(spes) / len(spes)) if spes else 0.0}


def _print_split(tag: str, loss: dict, rank: dict) -> None:
    print(f"{tag}: mse={loss['mse']:.3f} (~{loss['rmse_x']:.1f}x) "
          f"mae={loss['mae']:.3f} spear={loss['spearman']:.3f} n={loss['n']}",
          flush=True)
    print(f"{tag} within-match: matches={rank['matches']} "
          f"pair_acc={rank['pairwise_acc']:.3f} (n={rank['pairwise_n']}) "
          f"top1={rank['top1_acc']:.3f} spear={rank['mean_spearman']:.3f}",
          flush=True)


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
    "channel": [0.0, 0.05],
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
              "opp_tier": 0.005, "rating": 0.01, "kd": 0.01,
              "decider": 0.0, "won": 0.005, "ot": 0.0, "derby": 0.0,
              "stage": 0.0, "tier": 0.005, "weekday": 0.0,
              "multi": 0.01,
              "bias": 0.01, "channel": 0.05, "l2": 0.0}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dataset", type=Path, default=DATASET)
    ap.add_argument("--lr-scales", type=float, nargs="+",
                    default=[0.1, 0.25, 0.5, 1.0, 2.0])
    ap.add_argument("--max-epochs", type=int, default=40)
    ap.add_argument("--patience", type=int, default=5)
    ap.add_argument("--out", type=Path, default=OUT_DEFAULT)
    args = ap.parse_args()

    rows = load_dataset(args.dataset)
    train, val, test = time_split(rows)
    print(f"rows={len(rows)} train={len(train)} val={len(val)} "
          f"test={len(test)}", flush=True)

    from shorts.ml_common import append_run_log
    run_log = ROOT / ".data" / "fit_pov_runs.jsonl"

    base = evaluate(val, new_weights())
    print(f"baseline(zero) val: mse={base['mse']:.3f} (~{base['rmse_x']:.1f}x) "
          f"mae={base['mae']:.3f}", flush=True)
    if args.out.exists():
        try:
            prior = json.loads(args.out.read_text(encoding="utf-8"))
            prior_w = prior.get("weights") or {}
            if prior_w:
                print("prior weights on new split:", flush=True)
                _print_split("  prior val", evaluate(val, prior_w),
                             evaluate_within_match(val, prior_w))
                _print_split("  prior test", evaluate(test, prior_w),
                             evaluate_within_match(test, prior_w))
        except (OSError, json.JSONDecodeError, TypeError):
            pass

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
    def _run(tag: str, alphas: dict) -> tuple[tuple, float, dict]:
        weights, _, epoch, mse = train_run(
            train, val, alphas, max_epochs=args.max_epochs,
            patience=args.patience)
        rank = evaluate_within_match(val, weights)
        key = (rank["pairwise_acc"], -mse)
        append_run_log(run_log, {"model": "pov", "phase": "ablate",
                                 "tag": tag, "epoch": epoch, "mse": mse,
                                 "pairwise": rank["pairwise_acc"]})
        print(f"  [ablate] {tag:24s} stopped={epoch:<3} mse={mse:.4f} "
              f"pair={rank['pairwise_acc']:.3f}",
              flush=True)
        return key, mse, weights

    full_key, full_mse, full_w = _run("full", dict(ref))
    best = (full_key, dict(ref), full_w, "full")
    active = [g for g in GROUPS if ref.get(g, 0.0) > 0]
    for group in active:
        trial = dict(ref)
        trial[group] = 0.0
        key, mse, weights = _run(f"drop-{group}", trial)
        if key > best[0]:
            best = (key, trial, weights, f"drop-{group}")
    inactive = [g for g in GROUPS if ref.get(g, 0.0) == 0.0]
    for group in inactive:
        trial = dict(ref)
        trial[group] = 0.005 * best_scale
        key, mse, weights = _run(f"add-{group}", trial)
        if key > best[0]:
            best = (key, trial, weights, f"add-{group}")

    _key, alphas, weights, tag = best
    score = evaluate(val, weights)
    test_score = evaluate(test, weights)
    val_rank = evaluate_within_match(val, weights)
    test_rank = evaluate_within_match(test, weights)

    payload = {"alphas": alphas, "ablation": tag, "val": score,
               "test": test_score, "val_within_match": val_rank,
               "test_within_match": test_rank,
               "split": {"train": len(train), "val": len(val),
                         "test": len(test)},
               "weights": weights,
               "method": "log10(plateau_views) = bias + groups; "
                         "plateau = views * 21 / min(age, 21); LIM pro"}
    args.out.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    print(f"BEST ablation={tag}", flush=True)
    _print_split("val", score, val_rank)
    _print_split("test", test_score, test_rank)
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
