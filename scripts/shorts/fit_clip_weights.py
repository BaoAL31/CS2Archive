"""Learn clip star weights from realized Allstar views.

Model: log10(views) = match_bias + bias + w_kind + w_player + w_org
       + w_weapon + w_wallbang. Per-match intercepts absorb fixture
       popularity; feature weights learn what *within* a match predicts.

Usage:
    python scripts/shorts/fit_clip_weights.py
    python scripts/shorts/fit_clip_weights.py --out .data/clip_kind_weights.json

Grid-searches per-group alphas (kind/player/org/weapon) on a time-ordered
train split (oldest 80% of matches), picks best mean per-match Spearman on
val (newest 20%). Player alpha 0.0 included: fame loop guard.
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from _pathsetup import ensure  # noqa: E402

ensure()

from shorts.clip_observation import parse_kinds  # noqa: E402

PROBE = ROOT / ".data" / "allstar_hltv_probe.jsonl"
ROSTER = ROOT / ".data" / "team_roster.json"
OUT_DEFAULT = ROOT / ".data" / "clip_kind_weights.json"
MIN_CLIPS = 8
TRAIN_FRAC = 0.8

_WEAPON_RES = [
    ("awp", r"\bawp\b"), ("deagle", r"\bdesert eagle\b|\bdeagle\b"),
    ("ak47", r"\bak[\s-]?47\b"), ("m4a1", r"\bm4a1\b"),
    ("m4a4", r"\bm4a4\b"), ("usp", r"\busp\b"),
    ("glock", r"\bglock\b"), ("mac10", r"\bmac[\s-]?10\b"),
    ("mp9", r"\bmp9\b"), ("famas", r"\bfamas\b"),
    ("galil", r"\bgalil(?:\s*ar)?\b"), ("p250", r"\bp250\b"),
    ("tec9", r"\btec[\s-]?9\b"), ("knife", r"\bknife\b"),
    ("zeus", r"\bzeus\b"), ("ssg", r"\bssg\b|\bscout\b"),
    ("aug", r"\baug\b"), ("sg553", r"\bsg[\s-]?553\b"),
    ("p90", r"\bp90\b"), ("ump", r"\bump\b"),
    ("nova", r"\bnova\b"), ("negev", r"\bnegev\b"),
    ("dualies", r"\bdual(?:\s*berettas?)?\b|\belite\b"),
    ("fiveseven", r"\bfive[\s-]?seven\b"), ("cz75", r"\bcz75\b"),
    ("g3sg1", r"\bg3sg1\b"), ("xm1014", r"\bxm1014\b"),
]
_WEAPON_RES = [(w, re.compile(p, re.I)) for w, p in _WEAPON_RES]

_KIND_PRIORITY = ("1v5_won", "1v4_won", "1v3_won", "2vx_won",
                  "ace", "4k", "3k", "knife", "wallbang",
                  "perfect_shots", "flick", "defuse")


def weapon_from_text(text: str) -> str:
    for name, rx in _WEAPON_RES:
        if rx.search(text or ""):
            return name
    return "other"


def features_from_clip(clip: dict) -> dict:
    """Pure: title/label/player -> kind, wallbang flag, weapon."""
    text = f"{clip.get('title') or ''} {clip.get('label') or ''}"
    kinds = parse_kinds(text)
    primary = "other"
    for cand in _KIND_PRIORITY:
        if cand in kinds:
            primary = cand
            break
    return {"kind": primary,
            "wallbang": 1 if "wallbang" in kinds else 0,
            "weapon": weapon_from_text(text),
            "player": str(clip.get("player") or "").strip().lower()}


def new_weights() -> dict:
    return {"bias": 0.0, "kind": {}, "player": {}, "org": {},
            "weapon": {}, "wallbang": 0.0}


def predict_log_views(feats: dict, weights: dict, *, org_of) -> float:
    org = org_of(feats["player"])
    return (weights["bias"]
            + weights["kind"].get(feats["kind"], 0.0)
            + weights["player"].get(feats["player"], 0.0)
            + (weights["org"].get(org, 0.0) if org else 0.0)
            + weights["weapon"].get(feats["weapon"], 0.0)
            + (weights["wallbang"] if feats["wallbang"] else 0.0))


def sgd_epoch(matches, weights: dict, match_bias: dict,
              *, alphas: dict, org_of) -> None:
    """One SGD pass. Zero alpha freezes that group (fame-loop guard)."""
    for mid, clips in matches:
        for clip in clips:
            views = clip.get("views") or 0
            if views <= 0:
                continue
            feats = features_from_clip(clip)
            pred = predict_log_views(feats, weights, org_of=org_of)
            pred += match_bias.get(mid, 0.0)
            err = math.log10(views) - pred
            if alphas["match"]:
                match_bias[mid] = match_bias.get(mid, 0.0) + alphas["match"] * err
            if alphas["bias"]:
                weights["bias"] += alphas["bias"] * err
            if feats["kind"] and alphas["kind"]:
                d = weights["kind"]
                d[feats["kind"]] = d.get(feats["kind"], 0.0) + alphas["kind"] * err
            p = feats["player"]
            if p and alphas["player"]:
                d = weights["player"]
                d[p] = d.get(p, 0.0) + alphas["player"] * err
            org = org_of(p)
            if org and alphas["org"]:
                d = weights["org"]
                d[org] = d.get(org, 0.0) + alphas["org"] * err
            if alphas["weapon"]:
                w = feats["weapon"]
                d = weights["weapon"]
                d[w] = d.get(w, 0.0) + alphas["weapon"] * err
                if feats["wallbang"]:
                    weights["wallbang"] += alphas["weapon"] * err


def _ranks(xs: list[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float:
    """Rank correlation in [-1, 1]; 0.0 when degenerate."""
    if len(xs) < 3 or len(set(xs)) < 2 or len(set(ys)) < 2:
        return 0.0
    rx, ry = _ranks(xs), _ranks(ys)
    n = len(xs)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    if vx <= 0 or vy <= 0:
        return 0.0
    return cov / math.sqrt(vx * vy)


def load_roster_orgs(path: Path | None = None) -> dict[str, str]:
    try:
        data = json.loads((path or ROSTER).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, str] = {}
    for info in data.get("players", {}).values():
        nick = (info.get("nickname") or "").strip().lower()
        team = info.get("current_team")
        if nick and team:
            out[nick] = team
    return out


def load_matches(min_clips: int = MIN_CLIPS) -> list[tuple[str, list[dict]]]:
    """(match_id, clips) with enough viewed clips, time-ordered by HLTV id."""
    matches: list[tuple[str, list[dict]]] = []
    with PROBE.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            clips = [c for c in (row.get("clips") or [])
                     if isinstance(c, dict) and (c.get("views") or 0) > 0]
            if len(clips) >= min_clips:
                matches.append((str(row.get("match_id")), clips))
    matches.sort(key=lambda item: int(item[0]) if item[0].isdigit() else 0)
    return matches


def evaluate(matches, weights: dict, *, org_of) -> dict:
    """Val loss: MSE/MAE on log10 views (bias-free: rank only)."""
    spes, pair_hit, pair_tot = [], 0, 0
    se_sum, ae_sum, n = 0.0, 0.0, 0
    for mid, clips in matches:
        preds, actual = [], []
        for clip in clips:
            views = clip.get("views") or 0
            if views <= 0:
                continue
            preds.append(predict_log_views(features_from_clip(clip),
                                           weights, org_of=org_of))
            actual.append(math.log10(views))
            se_sum += (preds[-1] - actual[-1]) ** 2
            ae_sum += abs(preds[-1] - actual[-1])
            n += 1
        if len(preds) < 3:
            continue
        spes.append(spearman(preds, actual))
        for i in range(len(preds)):
            for j in range(i + 1, len(preds)):
                if actual[i] == actual[j]:
                    continue
                pair_tot += 1
                if (preds[i] > preds[j]) == (actual[i] > actual[j]):
                    pair_hit += 1
    mse = se_sum / n if n else float("inf")
    return {"matches": len(spes),
            "mse": mse,
            "rmse_x": 10 ** math.sqrt(mse) if n else float("inf"),
            "mae": ae_sum / n if n else float("inf"),
            "mean_spearman": sum(spes) / len(spes) if spes else 0.0,
            "pairwise_acc": pair_hit / pair_tot if pair_tot else 0.0}


GRID = {
    "kind": [0.02, 0.08],
    "player": [0.0, 0.01],
    "org": [0.0, 0.01],
    "weapon": [0.0, 0.005],
    "bias": [0.02],
    "match": [0.08],
}
EPOCHS = [6]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=OUT_DEFAULT)
    ap.add_argument("--min-clips", type=int, default=MIN_CLIPS)
    args = ap.parse_args()

    matches = load_matches(args.min_clips)
    cut = int(len(matches) * TRAIN_FRAC)
    train, val = matches[:cut], matches[cut:]
    print(f"matches={len(matches)} train={len(train)} val={len(val)}", flush=True)
    orgs = load_roster_orgs()
    org_of = lambda p: orgs.get((p or "").lower())

    base = evaluate(val, new_weights(), org_of=org_of)
    print(f"baseline(zero): mse={base['mse']:.3f} (~{base['rmse_x']:.1f}x) "
          f"mae={base['mae']:.3f}", flush=True)

    best = None
    keys = ["kind", "player", "org", "weapon"]
    for combo in itertools.product(*(GRID[k] for k in keys)):
        alphas = {**{k: v for k, v in zip(keys, combo)},
                  "bias": GRID["bias"][0], "match": GRID["match"][0]}
        for epochs in EPOCHS:
            weights = new_weights()
            match_bias: dict[str, float] = {}
            for _ in range(epochs):
                sgd_epoch(train, weights, match_bias,
                          alphas=alphas, org_of=org_of)
            score = evaluate(val, weights, org_of=org_of)
            tag = (f"kind={alphas['kind']} player={alphas['player']} "
                   f"org={alphas['org']} weapon={alphas['weapon']} ep={epochs}")
            print(f"  {tag} -> mse={score['mse']:.3f} (~{score['rmse_x']:.1f}x) "
                  f"mae={score['mae']:.3f}", flush=True)
            if best is None or score["mse"] < best[0]:
                best = (score["mse"], alphas, epochs, weights, score)

    _, alphas, epochs, weights, score = best
    payload = {"alphas": alphas, "epochs": epochs, "val": score,
               "weights": weights,
               "method": "log10(views) = match_bias + bias + kind + player "
                         "+ org + weapon + wallbang; per-match SGD"}
    args.out.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    print(f"BEST alphas={alphas} ep={epochs} "
          f"mse={score['mse']:.3f} (~{score['rmse_x']:.1f}x) "
          f"mae={score['mae']:.3f}", flush=True)
    print("kind premiums (log10 pts):", flush=True)
    for kind, val_ in sorted(weights["kind"].items(), key=lambda kv: -kv[1]):
        print(f"  {kind:14s} {val_:+.3f}  (~{10 ** val_:.2f}x views)", flush=True)
    for group in ("org", "weapon"):
        items = sorted(weights[group].items(), key=lambda kv: -kv[1])[:8]
        if items:
            print(f"top {group}:", flush=True)
            for name, val_ in items:
                print(f"  {name:14s} {val_:+.3f}", flush=True)
    top_players = sorted(weights["player"].items(),
                         key=lambda kv: -kv[1])[:8]
    if top_players:
        print("top player:", flush=True)
        for name, val_ in top_players:
            print(f"  {name:14s} {val_:+.3f}", flush=True)
    print(args.out, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
