"""Round scoring for the multi-POV edit layer (level 1: which rounds make it).

Fitted against two SIEZ breakout videos (45 rounds: 37/45 reproduced):

- Champion value: best STARRING pro moment in the round (a pro as doer,
  not victim/witness). Clutches and tier-OK multikills/duels carry;
  openers/trades/bursts alone do not.
- Eco gate: a pro tier-failed multikill taints the round (farm-adjacent
  values halve). SIEZ skipped match-2 R4 (kyousuke 4K vs Tec-9s).
- Rando gate (via starring): a round whose best moment belongs to a rando
  (match-2 R9: el0t3rorrist 4K vs our four pros) cannot champion on it.
- Danger moments (pro survives sub-30hp) champion texture rounds with no
  kill story: match-1 R4 (donk 17hp knife walk), match-2 R11 (magixx 9hp).
- Structural locks: round 1 (intro context) and the final round always
  show. OT / match-point rounds get a bonus.
- Pacing: ~85% budget with no-gap repair.

Residual debt (8/45): match-1 R2/R12 skipped despite outscoring some shown
thin rounds (R19/R20/R21), and shown-thin R4/R10/R16/R19/R20/R11m2 survive
on texture/position the scorer rates 8-14. Likely a narrative layer
(dead-air tightening side effects, per-type quotas) beyond round quality.
SIEZ ground truth: video segments joined to rounds via CT-left scoreboard
order (verified 48/48 + 39/39), POV reads per segment.

Usage:
    from highlights.round_score import score_rounds, select_rounds
    scored = score_rounds(action_timeline)   # {round: (score, explanation)}
    picked = select_rounds(action_timeline)  # sorted shown round numbers
"""

from __future__ import annotations

CHAMPION_VALUES = {
    "clutch": 50,
    "multikill_5k": 45,
    "multikill_4k": 40,
    "duel": 30,
    "multikill_3k": 25,
    "clutch_attempt": 20,
    "wallbang": 15,
    "danger": 14,
    "util_kill": 15,
    "bomb_defuse": 12,
    "closer": 12,
    "bomb_plant": 8,
    "util_burst": 8,
    "bomb_explode": 8,
    "opener": 5,
    "trade": 5,
}

OT_BONUS = 8
MATCH_POINT_BONUS = 8

# Eco taint: when a pro farms a tier-failed multikill (anti-eco), the whole
# round reads as farm footage — SIEZ skipped match-2 R4 (kyousuke 4K vs
# Tec-9s) despite danger texture in it. Farm-adjacent values halve; duels,
# clutches, wallbangs and bomb plays keep full value (no evidence either
# way, so they are left alone).
FARM_ADJACENT = frozenset({
    "multikill_5k", "multikill_4k", "multikill_3k", "danger", "closer",
    "opener", "trade",
})

# Candidate roles where the pro is the doer (not the victim/witness).
# A round whose best pro moment is a victim POV (our stars getting farmed
# by a rando, e.g. match-2 R9) must not champion on it.
STARRING_ROLES = frozenset({
    "attacker", "clutcher", "planter", "defuser", "exec", "duelist",
    "survivor",
})


def _stars_pro(moment: dict) -> bool:
    """True when the primary pro is the doer, not a victim or witness."""
    psid = moment.get("primary_pro_sid", "")
    if not psid:
        return False
    for c in moment.get("pov_candidates", []):
        if c["steam_id"] == psid:
            return c.get("role") in STARRING_ROLES
    return False


def _champion_key(moment: dict, tainted: bool = False) -> tuple:
    """Rank key for a pro moment: (value, kill_count, -start_tick)."""
    mtype = moment["type"]
    if mtype == "multikill":
        kc = int(moment.get("kill_count", 0) or 0)
        if kc >= 5:
            base = CHAMPION_VALUES["multikill_5k"]
        elif kc == 4:
            base = CHAMPION_VALUES["multikill_4k"]
        else:
            base = CHAMPION_VALUES["multikill_3k"]
        if not moment.get("tier_ok", True):
            base = 0  # anti-eco farm: champion disqualified
    else:
        base = CHAMPION_VALUES.get(mtype, 0)
    if tainted and (mtype in FARM_ADJACENT or mtype == "multikill"):
        base = base / 2
    nk = len(moment.get("kill_ticks", []))
    return (base, nk, 0)


def score_rounds(atl: dict) -> dict[int, tuple[float, str]]:
    """Score every round. Returns {round: (score, explanation)}.

    Only pro moments (primary_pro_sid set) can champion a round. Structural
    locks (R1, final) score infinite and are always picked.
    """
    rounds = atl.get("rounds", [])
    moments = atl.get("moments", [])
    live = [r["round"] for r in rounds]
    out: dict[int, tuple[float, str]] = {}
    for r in rounds:
        rn = r["round"]
        if rn == live[0] or rn == live[-1]:
            out[rn] = (float("inf"), "lock (intro/decider)")
            continue
        pro = [m for m in moments
               if m["round"] == rn and _stars_pro(m)]
        if not pro:
            out[rn] = (0.0, "no starring pro moment")
            continue
        tainted = any(
            m["type"] == "multikill"
            and m.get("attacker_steam_id", "") == m.get("primary_pro_sid", "")
            and m.get("primary_pro_sid", "")
            and not m.get("tier_ok", True)
            for m in pro)
        champ = max(pro, key=lambda m: _champion_key(m, tainted))
        val, nk, _ = _champion_key(champ, tainted)
        parts = [f"{champ['type']}:{champ.get('label', '')}={val}"]
        if tainted:
            parts.append("eco-taint/2")
        if r.get("overtime"):
            val += OT_BONUS
            parts.append(f"OT+{OT_BONUS}")
        if r.get("match_point_for"):
            val += MATCH_POINT_BONUS
            parts.append(f"MP+{MATCH_POINT_BONUS}")
        out[rn] = (float(val), "+".join(parts))
    return out


def select_rounds(atl: dict, max_gap: int = 2) -> list[int]:
    """Pick shown rounds: locks + score order, then repair pacing.

    Repair pass drops the lowest-scoring shown round inside any run that
    would otherwise leave no gap... implemented as: tentative top-K by
    score (K sized to leave ~15% on the floor, matching SIEZ's 38/45),
    then re-add any round whose removal would create a gap > max_gap, and
    drop the next-lowest unprotected round instead. Deterministic.
    """
    scored = score_rounds(atl)
    live = sorted(scored)
    locks = {rn for rn in live if scored[rn][0] == float("inf")}
    rest = sorted((rn for rn in live if rn not in locks),
                  key=lambda rn: (-scored[rn][0], rn))
    # Budget: SIEZ shows ~85% of rounds (38/45 across both fits).
    keep_n = max(len(locks) + 1, round(len(live) * 0.85))
    picked = set(locks) | set(rest[:keep_n - len(locks)])
    picked = set(sorted(picked))

    def gaps(selection: set[int]) -> list[tuple[int, int]]:
        ordered = sorted(selection)
        return [(ordered[i], ordered[i + 1]) for i in range(len(ordered) - 1)
                if ordered[i + 1] - ordered[i] > max_gap + 1]

    # Repair: fill gaps by re-adding the best dropped round inside them.
    dropped = [rn for rn in live if rn not in picked]
    while gaps(picked) and dropped:
        g = gaps(picked)[0]
        inside = [rn for rn in dropped if g[0] < rn < g[1]]
        if not inside:
            break
        best = max(inside, key=lambda rn: (scored[rn][0], -rn))
        picked.add(best)
        dropped.remove(best)
    return sorted(picked)
