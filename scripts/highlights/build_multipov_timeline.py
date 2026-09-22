"""Multi-POV edit timeline: weighted interval scheduling over action moments.

Level 2 of the SIEZ-style multi-POV edit (level 1 round selection proved
uninformative — see round_score.py docstring). Deterministic, no LLM:

- Every moment gets a value. `primary_pro_sid == ""` -> zero (only pro-
  anchored moments render; SIEZ never shows pure-rando action).
- Rando-attacker moments (our stars farmed) are discounted x0.3 (SIEZ
  skipped match-2 R9: el0t3rorrist 4K vs the four pros).
- Tier-failed multikills (anti-eco farm) are disqualified (SIEZ skipped
  match-2 R4: kyousuke 4K vs Tec-9s); farm-adjacent moments in a tainted
  round are halved.
- Diversity: per (round, type) only the top-2 moments keep full value,
  the rest halve — SIEZ never shows two identical-type clips per round.
- Weighted interval scheduling (DP) picks the max-value non-overlapping
  subset — overlapping moments are alternative views of one play; the DP
  unwinds them.
- Coverage guarantee: every pro with moments gets >=1 picked clip (the
  highest-value one, forced through overlaps).
- Duration budget: windows are trimmed lowest-value-per-second first
  until the cut fits --max-minutes (SIEZ's R25 cut is the length cap,
  not quality).

Output: renders/hl-{demo_stem}/multipov/edit_timeline.json — picked
moments in chronological order with value + why, plus coverage summary.
The render step (CSDM per-POV segment renders) consumes `picked`.

Usage:
    python scripts/highlights/build_multipov_timeline.py <demo_path> [--max-minutes 25]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from _pathsetup import ensure  # noqa: E402

ensure()

from highlights.round_score import (  # noqa: E402
    CHAMPION_VALUES,
    FARM_ADJACENT,
)

TICK_RATE = 64  # CS2 demos are 64 tick
RANDO_ATTACKER_DISCOUNT = 0.3
DIVERSITY_FULL_KEEP = 2
DIVERSITY_DISCOUNT = 0.5


def _base_value(moment: dict) -> float:
    mtype = moment["type"]
    if mtype == "multikill":
        kc = int(moment.get("kill_count", 0) or 0)
        if kc >= 5:
            return float(CHAMPION_VALUES["multikill_5k"])
        if kc == 4:
            return float(CHAMPION_VALUES["multikill_4k"])
        return float(CHAMPION_VALUES["multikill_3k"])
    return float(CHAMPION_VALUES.get(mtype, 0))


def moment_value(moment: dict, tainted_rounds: set[int]) -> tuple[float, list[str]]:
    """Value + human-readable why for one moment. Deterministic."""
    why = []
    base = _base_value(moment)
    why.append(f"{moment['type']}:{moment.get('label', '')}={base:g}")
    if not moment.get("primary_pro_sid"):
        return 0.0, ["no pro anchor -> 0"]
    att = moment.get("attacker_steam_id", "")
    if moment["type"] == "multikill" and not moment.get("tier_ok", True):
        return 0.0, why + ["anti-eco farm -> 0"]
    if att and att != moment["primary_pro_sid"]:
        base *= RANDO_ATTACKER_DISCOUNT
        why.append(f"rando attacker x{RANDO_ATTACKER_DISCOUNT}")
    if moment["round"] in tainted_rounds and moment["type"] in FARM_ADJACENT:
        base *= DIVERSITY_DISCOUNT
        why.append("eco-taint x0.5")
    return base, why


def _tainted_rounds(moments: list[dict]) -> set[int]:
    return {m["round"] for m in moments
            if m["type"] == "multikill" and not m.get("tier_ok", True)}


def _apply_diversity(values: list[tuple[float, list[str]]],
                     moments: list[dict]) -> None:
    """Per (round, type): top-2 keep full value, the rest halve."""
    groups: dict[tuple[int, str], list[int]] = {}
    for i, m in enumerate(moments):
        groups.setdefault((m["round"], m["type"]), []).append(i)
    for idxs in groups.values():
        if len(idxs) <= DIVERSITY_FULL_KEEP:
            continue
        ranked = sorted(idxs, key=lambda i: -values[i][0])
        for i in ranked[DIVERSITY_FULL_KEEP:]:
            v, why = values[i]
            values[i] = (v * DIVERSITY_DISCOUNT, why + ["diversity x0.5"])


def _schedule(moments: list[dict], values: list[float]) -> list[int]:
    """Weighted interval scheduling (DP). Returns picked indices."""
    order = sorted(range(len(moments)), key=lambda i: moments[i]["end_tick"])
    n = len(order)
    # p[j]: rightmost moment (in end order) ending <= start of order[j]
    starts = [moments[i]["start_tick"] for i in order]
    ends = [moments[i]["end_tick"] for i in order]
    p = [0] * n
    for j in range(n):
        lo, hi = 0, j
        while lo < hi:
            mid = (lo + hi) // 2
            if ends[mid] <= starts[j]:
                lo = mid + 1
            else:
                hi = mid
        p[j] = lo - 1
    dp = [0.0] * (n + 1)
    take = [False] * n
    for j in range(n):
        skip = dp[j]
        grab = values[order[j]] + dp[p[j] + 1]
        if grab > skip:
            dp[j + 1] = grab
            take[j] = True
        else:
            dp[j + 1] = skip
    picked: list[int] = []
    j = n - 1
    while j >= 0:
        if take[j]:
            picked.append(order[j])
            j = p[j]
        else:
            j -= 1
    return sorted(picked)


def _force_coverage(moments: list[dict], values: list[float],
                    picked: list[int]) -> list[int]:
    """Every pro with moments gets >=1 clip (best-value, forced)."""
    by_pro: dict[str, int] = {}
    for i, m in enumerate(moments):
        psid = m.get("primary_pro_sid", "")
        if psid and (psid not in by_pro or values[i] > values[by_pro[psid]]):
            by_pro[psid] = i
    out = list(picked)
    for psid, i in sorted(by_pro.items()):
        if any(out_i == i for out_i in out):
            continue
        # drop lower-value overlapping picks, then force the pro's best
        # moment through — coverage beats scheduling optimality; any
        # residual overlap with higher-value picks resolves at render trim
        overlap = [j for j in out
                   if moments[j]["start_tick"] < moments[i]["end_tick"]
                   and moments[i]["start_tick"] < moments[j]["end_tick"]
                   and values[j] < values[i]]
        out = [j for j in out if j not in overlap] + [i]
    return sorted(set(out))


def _fit_budget(moments: list[dict], values: list[float],
                picked: list[int], max_seconds: float) -> list[int]:
    """Trim lowest value-per-second picks until the cut fits."""
    def dur(i: int) -> float:
        return max((moments[i]["end_tick"] - moments[i]["start_tick"])
                   / TICK_RATE, 1.0)

    out = list(picked)
    while out and sum(dur(i) for i in out) > max_seconds:
        worst = min(out, key=lambda i: (values[i] / dur(i), -values[i]))
        out.remove(worst)
    return out


def build_multipov_timeline(action_timeline: dict,
                            max_minutes: float = 25.0) -> dict:
    moments = action_timeline.get("moments", [])
    tainted = _tainted_rounds(moments)
    values: list[tuple[float, list[str]]] = [
        moment_value(m, tainted) for m in moments]
    _apply_diversity(values, moments)
    vals = [v for v, _ in values]

    picked = _schedule(moments, vals)
    picked = [i for i in picked if vals[i] > 0]
    picked = _force_coverage(moments, vals, picked)
    picked = _fit_budget(moments, vals, picked, max_minutes * 60.0)

    pro_sids = {m["primary_pro_sid"] for m in moments
                if m.get("primary_pro_sid")}
    covered = {moments[i]["primary_pro_sid"] for i in picked
               if moments[i].get("primary_pro_sid")}
    total_s = sum((moments[i]["end_tick"] - moments[i]["start_tick"])
                  / TICK_RATE for i in picked)

    return {
        "timeline_version": 2,
        "kind": "multipov",
        "max_minutes": max_minutes,
        "picked": [
            {
                **moments[i],
                "value": round(vals[i], 2),
                "why": values[i][1],
            }
            for i in sorted(picked, key=lambda i: moments[i]["start_tick"])
        ],
        "stats": {
            "moments_total": len(moments),
            "picked_count": len(picked),
            "dropped_count": len(moments) - len(picked),
            "total_duration_s": round(total_s, 1),
            "pros_total": len(pro_sids),
            "pros_covered": len(covered),
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("demo_path", type=Path)
    ap.add_argument("--max-minutes", type=float, default=25.0)
    args = ap.parse_args()

    stem = args.demo_path.stem
    at_path = ROOT.parent / "renders" / f"hl-{stem}" / "action_timeline.json"
    if not at_path.exists():
        print(f"[MULTIPOV_ERROR] no action timeline: {at_path}")
        return 1
    import json

    action_timeline = json.loads(at_path.read_text(encoding="utf-8"))
    if action_timeline.get("timeline_version") != 2:
        print("[MULTIPOV_ERROR] action timeline is v1 — rebuild first")
        return 1

    edit = build_multipov_timeline(action_timeline, args.max_minutes)
    out_dir = at_path.parent / "multipov"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "edit_timeline.json"
    out_path.write_text(json.dumps(edit, indent=2), encoding="utf-8")

    s = edit["stats"]
    print(f"[OK] {s['picked_count']}/{s['moments_total']} moments, "
          f"{s['total_duration_s']:.0f}s, "
          f"POV coverage {s['pros_covered']}/{s['pros_total']} "
          f"-> {out_path}")
    for m in edit["picked"]:
        print(f"  R{m['round']:>2} {m['type']:<12} {m['label']:<24} "
              f"val={m['value']:<6} "
              f"{(m.get('primary_pro_sid') or '')[-6:]:<6} "
              f"{' '.join(m['why'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
