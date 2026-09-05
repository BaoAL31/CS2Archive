"""Learn POV card weights from realized long-form views.

Model: log10(views/day) = channel_bias + bias + w_player + w_org + w_map
       + w_elo. Per-channel intercepts absorb subscriber bases; own-channel
       rows (CS2 Archive) carry the purest content signal.

Usage:
    python scripts/shorts/fit_pov_weights.py
    python scripts/shorts/fit_pov_weights.py --out .data/pov_kind_weights.json

Time-ordered train (oldest 80%) / val (newest 20%), per-group alpha grid,
best mean per-channel Spearman wins. Player alpha 0.0 included (fame loop).
"""
from __future__ import annotations

import argparse
import csv
import itertools
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from _pathsetup import ensure  # noqa: E402

ensure()

from shorts.fit_clip_weights import load_roster_orgs, spearman  # noqa: E402

HISTORY = ROOT / "exports" / "pov_market" / "video_history.csv"
OUT_DEFAULT = ROOT / ".data" / "pov_kind_weights.json"
OWN_CHANNEL = "CS2 Archive"
TRAIN_FRAC = 0.8


def _parse_stamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def _elo_bucket(raw: str | float | None) -> str:
    try:
        elo = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "none"
    if elo >= 5000:
        return "5k+"
    if elo >= 4000:
        return "4k"
    if elo >= 3000:
        return "3k"
    return "sub3k"


def features_from_pov(row: dict, *, org_of=None) -> dict:
    player = str(row.get("primary_player") or "").strip().lower()
    org = (org_of(player) if org_of else None) or None
    return {"player": player,
            "org": org,
            "map": str(row.get("map") or "").strip().lower() or "unknown",
            "elo": _elo_bucket(row.get("elo"))}


def new_weights() -> dict:
    return {"bias": 0.0, "player": {}, "org": {}, "map": {}, "elo": {}}


def predict_log_vpd(feats: dict, weights: dict, *,
                    channel_bias: dict, org_of=None) -> float:
    del org_of
    return (weights["bias"]
            + channel_bias.get(feats.get("_channel", ""), 0.0)
            + weights["player"].get(feats["player"], 0.0)
            + (weights["org"].get(feats["org"], 0.0) if feats.get("org") else 0.0)
            + weights["map"].get(feats["map"], 0.0)
            + weights["elo"].get(feats["elo"], 0.0))


def sgd_epoch(rows, weights: dict, channel_bias: dict,
              *, alphas: dict, org_of) -> None:
    for row in rows:
        try:
            vpd = float(row.get("views_per_day") or 0)
        except (TypeError, ValueError):
            continue
        if vpd <= 0:
            continue
        feats = features_from_pov(row, org_of=org_of)
        feats["_channel"] = str(row.get("channel") or "")
        pred = predict_log_vpd(feats, weights, channel_bias=channel_bias)
        err = math.log10(vpd) - pred
        ch = feats["_channel"]
        if alphas["channel"]:
            channel_bias[ch] = channel_bias.get(ch, 0.0) + alphas["channel"] * err
        if alphas["bias"]:
            weights["bias"] += alphas["bias"] * err
        if feats["player"] and alphas["player"]:
            d = weights["player"]
            d[feats["player"]] = d.get(feats["player"], 0.0) + alphas["player"] * err
        if feats.get("org") and alphas["org"]:
            d = weights["org"]
            d[feats["org"]] = d.get(feats["org"], 0.0) + alphas["org"] * err
        if alphas["map"]:
            d = weights["map"]
            d[feats["map"]] = d.get(feats["map"], 0.0) + alphas["map"] * err
        if feats["elo"] != "none" and alphas["elo"]:
            d = weights["elo"]
            d[feats["elo"]] = d.get(feats["elo"], 0.0) + alphas["elo"] * err


def recognised_aliases() -> dict[str, str]:
    """Lowercase label -> canonical nick (drops smoke/molotov guide rows)."""
    aliases: dict[str, str] = {"dev1ce": "device", "device": "device"}
    try:
        from player_accounts import list_accounts
        for account in list_accounts():
            nick = (account.nickname or "").strip()
            if nick:
                aliases[nick.casefold()] = nick
            faceit = (account.faceit_nickname or "").strip()
            if faceit:
                aliases[faceit.casefold()] = nick
    except Exception:
        pass
    for nick in load_roster_orgs():
        aliases.setdefault(nick, nick)
    return aliases


def load_rows(min_vpd: float = 0.0) -> list[dict]:
    aliases = recognised_aliases()
    rows: list[dict] = []
    with HISTORY.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                vpd = float(row.get("views_per_day") or 0)
            except (TypeError, ValueError):
                continue
            if vpd <= min_vpd or not (row.get("primary_player") or "").strip():
                continue
            canon = aliases.get(str(row["primary_player"]).strip().casefold())
            if not canon:
                continue
            row = dict(row)
            row["primary_player"] = canon
            rows.append(row)
    rows.sort(key=lambda r: _parse_stamp(r.get("published_at"))
              or datetime.min.replace(tzinfo=timezone.utc))
    return rows


def evaluate(rows, weights: dict, *, org_of) -> dict:
    """Mean per-channel Spearman + pairwise accuracy (rank only)."""
    by_channel: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_channel[str(row.get("channel") or "")].append(row)
    spes, pair_hit, pair_tot, n_ch = [], 0, 0, 0
    for channel, items in by_channel.items():
        if len(items) < 5:
            continue
        preds, actual = [], []
        for row in items:
            try:
                vpd = float(row.get("views_per_day") or 0)
            except (TypeError, ValueError):
                continue
            if vpd <= 0:
                continue
            feats = features_from_pov(row, org_of=org_of)
            feats["_channel"] = channel
            preds.append(predict_log_vpd(feats, weights,
                                         channel_bias={channel: 0.0}))
            actual.append(math.log10(vpd))
        if len(preds) < 5:
            continue
        n_ch += 1
        spes.append(spearman(preds, actual))
        for i in range(len(preds)):
            for j in range(i + 1, len(preds)):
                if actual[i] == actual[j]:
                    continue
                pair_tot += 1
                if (preds[i] > preds[j]) == (actual[i] > actual[j]):
                    pair_hit += 1
    return {"channels": n_ch,
            "mean_spearman": sum(spes) / len(spes) if spes else 0.0,
            "pairwise_acc": pair_hit / pair_tot if pair_tot else 0.0}


GRID = {
    "player": [0.0, 0.005],
    "org": [0.0, 0.005],
    "map": [0.0, 0.01],
    "elo": [0.0, 0.005],
    "bias": [0.01],
    "channel": [0.05],
}
EPOCHS = [6]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=OUT_DEFAULT)
    args = ap.parse_args()

    rows = load_rows()
    cut = int(len(rows) * TRAIN_FRAC)
    train, val = rows[:cut], rows[cut:]
    print(f"rows={len(rows)} train={len(train)} val={len(val)}", flush=True)
    orgs = load_roster_orgs()
    org_of = lambda p: orgs.get((p or "").lower())

    base = evaluate(val, new_weights(), org_of=org_of)
    print(f"baseline(zero): spearman={base['mean_spearman']:.3f} "
          f"pairwise={base['pairwise_acc']:.3f} channels={base['channels']}",
          flush=True)

    best = None
    keys = ["player", "org", "map", "elo"]
    for combo in itertools.product(*(GRID[k] for k in keys)):
        alphas = {**{k: v for k, v in zip(keys, combo)},
                  "bias": GRID["bias"][0], "channel": GRID["channel"][0]}
        for epochs in EPOCHS:
            weights = new_weights()
            channel_bias: dict[str, float] = {}
            for _ in range(epochs):
                sgd_epoch(train, weights, channel_bias,
                          alphas=alphas, org_of=org_of)
            score = evaluate(val, weights, org_of=org_of)
            tag = (f"player={alphas['player']} org={alphas['org']} "
                   f"map={alphas['map']} elo={alphas['elo']} ep={epochs}")
            print(f"  {tag} -> spearman={score['mean_spearman']:.3f} "
                  f"pairwise={score['pairwise_acc']:.3f}", flush=True)
            if best is None or score["mean_spearman"] > best[0]:
                best = (score["mean_spearman"], alphas, epochs, weights, score)

    _, alphas, epochs, weights, score = best
    payload = {"alphas": alphas, "epochs": epochs, "val": score,
               "weights": weights,
               "method": "log10(views/day) = channel_bias + bias + player "
                         "+ org + map + elo; per-video SGD"}
    args.out.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    print(f"BEST alphas={alphas} ep={epochs} "
          f"spearman={score['mean_spearman']:.3f} "
          f"pairwise={score['pairwise_acc']:.3f}", flush=True)
    for group in ("org", "map", "elo", "player"):
        items = sorted(weights[group].items(), key=lambda kv: -kv[1])[:8]
        if items:
            print(f"top {group}:", flush=True)
            for name, val_ in items:
                print(f"  {name:14s} {val_:+.3f}  (~{10 ** val_:.2f}x vpd)",
                      flush=True)
    print(args.out, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
