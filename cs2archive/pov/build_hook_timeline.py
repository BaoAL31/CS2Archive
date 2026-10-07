"""Build a Hook Timeline: 2-4 impressive POV moments for a video cold open.

The hook is a no-spoiler cold open prepended to a POV: rendered with the
full player HUD but the compact alive-count team bar (N vs N, not the avatar
row) and the score digits blurred in post — the Shorts HUD policy — so no
score information leaks. Multiple moments are
allowed when several are impressive; the assembled hook is heavily edited
(tight kill-anchored trims + crossfades, climax last).

Detection is the Shorts extractor (``cs2archive/shorts/build_short_timeline.py``);
this module only ranks and selects. Tiers are an ordered, configurable list so
the qualification threshold can be tuned without a code change.

NOTE: the shorts extractor only emits multikills at >= 4 kills — there is no
3k/2k short. Irrelevant now: EVERY kill of the POV player becomes a hook
candidate (the ``kill`` tier below), so nothing depends on the extractor's
multikill floor. Kills that land close together merge into one continuous
moment and each enclosed kill adds its score to the segment.

Usage:
    python cs2archive/pov/build_hook_timeline.py <demo.dem> --player <steam64|nick>
    python cs2archive/pov/build_hook_timeline.py <demo.dem> --player donk --max-moments 3
    python cs2archive/pov/build_hook_timeline.py <demo.dem> --player donk \
        --tiers clutch_1v5,clutch_1v4,clutch_1v3,5k,4k
    python cs2archive/pov/build_hook_timeline.py <demo.dem> --include-all-players

Output:
    renders/pov-{demo_stem}_{nick}/hook/hook_timeline.json
    (nothing is written when no moment qualifies — callers treat that as
    "no hook", not as an error)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


from cs2archive.shorts.build_short_timeline import build_short_timeline  # noqa: E402
from cs2archive.highlights.build_action_timeline import (  # noqa: E402
    pov_action_slice,
)
from cs2archive.paths import pov_dir as _pov_dir  # noqa: E402
from cs2archive.weapons import headshot_bonus, weapon_tier  # noqa: E402
from cs2archive.pov.hook_plan import plan_hook, planned_seconds  # noqa: E402
from thumbnail.utils import KILLFEED_POV_SECONDS  # noqa: E402

# Ordered tiers, best first. ``tier_rank`` is the position here.
#
# NOTE on perfect_shots: the Shorts extractor emits ``perfect_shots`` for
# 2-4 gun kills whose *shot count* ~= kill count (ammo efficiency), and it
# deliberately EXCLUDES a 1-bullet-per-kill 4K from the ``4k`` short. That is a
# different idea from the hook tier below: ``insta_kill`` is the LOS-based
# first-contact detector (cs2archive/overlay/victim_rewind.py) — victim visible
# for <= 0.35s before dying, no prior peek, not a trade. Kills with measured
# first contact inside 0.5s but over the insta cap surface as ``peek`` rows:
# no insta moment, but proof enough for the punch-up quality bonus, which
# reads victim weapons from the action timeline. The ammo-efficiency tier is
# gone; a >=4-kill perfect_shots short is folded back into ``4k`` so those 4Ks
# do not silently disappear (see tier_of).
TIER_ORDER = (
    "clutch_1v5",
    "clutch_1v4",
    "clutch_1v3",
    "5k",
    "punch_up",
    "4k",
    "insta_kill",
    "duel",
    "clutch_attempt",
    "opener",
    "trade",
    "kill",
)

TIER_LABELS = {
    "clutch_1v5": "1v5 CLUTCH",
    "clutch_1v4": "1v4 CLUTCH",
    "clutch_1v3": "1v3 CLUTCH",
    "5k": "5K ACE",
    "punch_up": "PUNCH-UP 4K",
    "4k": "4K",
    "insta_kill": "INSTA KILL",
    "duel": "DUEL",
    "clutch_attempt": "1vX CLUTCH ATTEMPT",
    "opener": "OPENER",
    "trade": "TRADE",
    "kill": "KILL",
}

DEFAULT_TIERS = ",".join(TIER_ORDER)

# Round 1 sits ~30s into the finished video, so replaying it as a cold open adds
# nothing the viewer is not about to see anyway. Round 0 is the knife/warmup
# round. Hooks therefore start at round 2 by default (``--min-round``).
MIN_ROUND_DEFAULT = 2


def round_allowed(round_no, min_round: int = MIN_ROUND_DEFAULT) -> bool:
    """False for the knife round, round 1, and anything unresolvable."""
    try:
        return int(round_no) >= int(min_round)
    except (TypeError, ValueError):
        return False


def timeline_matches(payload: dict, *, tiers: list[str] | None = None,
                     min_round: int | None = None,
                     max_moments: int | None = None,
                     max_seconds: float | None = None,
                     min_seconds: float | None = None,
                     rule_version: int | None = None) -> bool:
    """True when a cached hook_timeline.json was built with the same filters.

    The pipeline reuses a cached timeline instead of re-running detection, so a
    stale cache would silently ignore a changed tier/threshold/round filter.
    Callers must rebuild when this returns False.
    """
    params = payload.get("params")
    if not isinstance(params, dict):
        return False  # pre-params cache: rebuild once
    if tiers is not None and list(params.get("tiers") or []) != list(tiers):
        return False
    if min_round is not None and int(params.get("min_round", 1)) != int(min_round):
        return False
    if max_moments is not None and int(params.get("max_moments", 0)) != int(max_moments):
        return False
    if max_seconds is not None and float(params.get("max_seconds", 0)) != float(max_seconds):
        return False
    if min_seconds is not None and float(params.get("min_seconds", 0)) != float(min_seconds):
        return False
    if rule_version is not None and params.get("rule_version") != int(rule_version):
        return False
    return True


def _kills(short: dict) -> int:
    return len(short.get("kill_ticks") or [])


def tier_of(short: dict, tickrate: int = 64) -> str | None:
    """Best tier name for a detected short, or None if it does not qualify.

    Multikills (5k / 4k / punch_up, incl. the perfect_shots fold-back) only
    keep their tier when every kill would still be on the HUD killfeed —
    first-to-last kill within ``KILLFEED_POV_SECONDS`` (the thumbnail
    script's rule). A 4k spread across the round reads as four solos to a
    viewer; it must not headline a cold open. Dropped multikills vanish as
    a tier but their kills still flow through the separate single
    detectors (insta / the every-kill floor), so nothing fast is lost.
    """
    st = short.get("short_type")
    kills = _kills(short)
    if st == "clutch":
        cnt = (short.get("clutch_initial_count") or "").lower()
        name = f"clutch_{cnt}" if cnt in ("1v3", "1v4", "1v5") else None
        if name:
            return name
        return None
    if st == "clutch_attempt":
        return "clutch_attempt"
    if st == "perfect_shots":
        # Ammo-efficiency shorts are not a tier any more, but a >=4-kill one was
        # EXCLUDED from the 4k short by the detector — fold it back so the hook
        # still sees it. A sub-4-kill 4-tap is not hook material.
        if kills < 4:
            return None
        return "4k" if _multikill_fits_killfeed(short, tickrate) else None
    if kills >= 5:
        return "5k" if _multikill_fits_killfeed(short, tickrate) else None
    if kills >= 4:
        if not _multikill_fits_killfeed(short, tickrate):
            return None
        return "punch_up" if short.get("punch_up_tags") else "4k"
    return None


def _multikill_fits_killfeed(short: dict, tickrate: int = 64) -> bool:
    """True when every kill of this multikill sits on the feed together.

    POV killfeed rows live ``KILLFEED_POV_SECONDS`` (7.5s — the Thumbnail
    rule in thumbnail/utils.py). A multikill whose first kill already
    faded is not a multikill on screen; it must not keep the tier.
    """
    try:
        ticks = sorted(int(t) for t in (short.get("kill_ticks") or []))
    except (TypeError, ValueError):
        return False
    if len(ticks) < 2:
        return True
    try:
        window = max(1, int(round(float(tickrate) * KILLFEED_POV_SECONDS)))
    except (TypeError, ValueError):
        window = int(round(64 * KILLFEED_POV_SECONDS))
    return ticks[-1] - ticks[0] <= window


def _rank_reason(tier: str, short: dict) -> str:
    kills = _kills(short)
    if tier.startswith("clutch_1v"):
        return f"{tier.replace('clutch_', '')} clutch won, {kills} kill(s)"
    if tier == "5k":
        return f"5k multikill, {kills} kill(s)"
    if tier == "punch_up":
        return f"punch-up {kills}k on lower-tier guns"
    if tier == "clutch_attempt":
        return f"1vX clutch attempt ({short.get('clutch_initial_count', '?')}), {kills} kill(s)"
    return f"{kills}k multikill"


def _map_mesh_available(map_name: str) -> bool:
    """True when the map collision mesh loads.

    ``closest_hit`` returns None BOTH for "no obstruction" and for "map mesh
    unavailable", and victim_rewind maps None to "LOS open". A missing mesh
    therefore reads as "LOS was always open", which pushes every TTK past the
    0.5s cap and silently yields zero insta kills. Callers must check this
    before believing a zero result.
    """
    if not map_name:
        return False
    try:
        # prefer_cs2util_scripts() inserts BOTH the sibling's scripts/ dir and its repo root.
        # Inserting only the root did not work: the sibling's collision mesh lives at
        # <sibling>/scripts/render/map_collision.py, so `import render.map_collision` failed,
        # the bare except below returned False, and this guard stayed permanently tripped --
        # which is the exact silent-failure mode this function exists to detect.
        from cs2archive.overlay._common import prefer_cs2util_scripts

        prefer_cs2util_scripts()
        from render.map_collision import _grid_for_map

        return _grid_for_map(map_name) is not None
    except Exception:
        return False


INSTA_PAIR_SECONDS = 3.0  # non-flick insta kills must pair within this to qualify
INSTA_CLEAN_HP = 90.0  # ...and at least one victim at/above this (0 disables)
INSTA_RULE_VERSION = 12  # bumped whenever the insta rule changes (stale
# caches rebuild). v12: flick quality only when the real flick detector
# tagged the kill (victim_rewind no longer surfaces raw peak deg/s).
# v11: through-smoke kills deter -25. v10: INSTA_QUALITY_BONUS.

# ── Peek-kill vs back-shot (council-reviewed taper) ─────────────────────
# victim_hold_deg = angle between the victim's view and the direction to the
# attacker at LOS-open. Small = victim already holding the angle; the
# attacker peeked into a prepared crosshair (more impressive). Large =
# back/side shot (victim never saw him — no bonus). Taper rather than a
# cliff: real holds cluster at 0-6 deg, ambiguous lanes sit around 30 deg.
PEEK_HOLD_FULL_DEG = 15.0
PEEK_HOLD_ZERO_DEG = 45.0
PEEK_BONUS_MAX = 100.0

# ── Flick speed (crosshair deg/s before a DETECTED flick) ────────────────
# Only kills the real flick detector tagged (peak + yaw-travel gates in
# shorts/flick.py) carry a flick_speed from victim_rewind. Raw peak deg/s
# on a non-flick is a micro-adjust and must not score. Curve starts above
# ordinary tracking, capped so one flick can't dominate a moment.
FLICK_DPS_FLOOR = 100.0
FLICK_SCALE = 0.75
FLICK_BONUS_CAP = 150.0

# ── Insta quality bonus ──────────────────────────────────────────────────
# A verified insta kill (measured LOS-TTK inside the cap) pays a flat
# per-kill bonus on top of the tier base and the (0.5 - ttk) * 200 speed
# term: reaction kills are the scarcest content in a POV. Fused by tick
# across merged candidates like the headshot bonus (an insta kill inside a
# 3k keeps earning it exactly once).
INSTA_QUALITY_BONUS = 250.0


def peek_kill_bonus(hold_deg) -> float:
    """Tapered peek bonus for one kill (0 when unknown or a back shot)."""
    try:
        h = float(hold_deg)
    except (TypeError, ValueError):
        return 0.0
    if h >= 999.0:  # fail-closed sentinel from angle_between_deg
        return 0.0
    if h <= PEEK_HOLD_FULL_DEG:
        return PEEK_BONUS_MAX
    if h >= PEEK_HOLD_ZERO_DEG:
        return 0.0
    span = PEEK_HOLD_ZERO_DEG - PEEK_HOLD_FULL_DEG
    return PEEK_BONUS_MAX * (PEEK_HOLD_ZERO_DEG - h) / span


def flick_speed_bonus(dps) -> float:
    """Quality points for crosshair speed before the kill (0 below floor)."""
    try:
        d = float(dps)
    except (TypeError, ValueError):
        return 0.0
    if d <= FLICK_DPS_FLOOR:
        return 0.0
    return min(FLICK_BONUS_CAP, (d - FLICK_DPS_FLOOR) * FLICK_SCALE)


def _load_insta_rows(demo_path: Path, player_sid: str, map_name: str,
                       tickrate: int) -> list[dict]:
    """One shared victim_rewind parse (mesh guard + detect, warn-only)."""
    if not _map_mesh_available(map_name):
        print(f"  [WARN] collision mesh unavailable for {map_name or '?'} — "
              f"skipping the insta_kill tier (LOS TTK cannot be trusted)")
        return []
    try:
        from cs2archive.overlay.victim_rewind import detect_from_demo
    except Exception as e:  # noqa: BLE001
        print(f"  [WARN] victim_rewind unavailable ({e}) — no insta_kill tier")
        return []
    try:
        return detect_from_demo(demo_path, player=str(player_sid))
    except Exception as e:  # noqa: BLE001
        print(f"  [WARN] insta_kill detection failed ({e})")
        return []


def insta_kill_candidates(demo_path: Path, player_sid: str, map_name: str,
                           tickrate: int, enabled: list[str]) -> list[dict]:
    """LOS-TTK insta kills for the POV player (victim_rewind detector).

    A kill qualifies when the victim was visible to the attacker for <= 0.5s
    before dying and the kill was not a trade — victim HP is deliberately NOT
    a gate (a 0.25s flick onto a tagged victim is still elite). Single-kill
    moments — that is the point: they are invisible to the multikill-based
    Shorts extractor.
    """
    if "insta_kill" not in enabled or not player_sid:
        return []
    rows = _load_insta_rows(demo_path, player_sid, map_name, tickrate)
    return pair_insta_rows(rows, player_sid, enabled, tickrate)


def pair_insta_rows(rows: list[dict], player_sid: str,
                    enabled: list[str], tickrate: int = 64) -> list[dict]:
    """Group detector rows into hook moments (pure; no demo access).

    Flick-tagged rows stay solo-eligible. Non-flick insta rows form runs of
    >= 2 within INSTA_PAIR_SECONDS per round (distinct victims) with at
    least one clean victim (HP >= INSTA_CLEAN_HP) — each run becomes ONE
    chained moment. Lone rows become solo moments (a single verified
    instakill is hook material on its own).
    """
    out: list[dict] = []
    solos: list[dict] = []
    pairable: list[dict] = []
    for r in rows:
        reasons = r.get("reasons") or []
        tick = int(r["kill_tick"])
        los = r.get("los_open_tick")
        ttk = (tick - int(los)) / tickrate if los else None
        is_flick = any(x in ("flick", "awp_flick") for x in reasons)
        hp = r.get("victim_hp")
        try:
            hp = float(hp) if hp is not None else None
        except (TypeError, ValueError):
            hp = None
        head = (r.get("hitgroup") or "").lower() == "head"
        base = {
            "tier": "insta_kill",
            "tier_rank": enabled.index("insta_kill"),
            "short_type": "insta_kill",
            "round": int(r.get("round") or 0),
            "pov_steam_id": str(r.get("attacker_sid") or player_sid),
            "pov_nick": None,
            "round_win_tick": None,
            "clutch_initial_count": "",
            "tick": tick,
            "ttk": ttk,
            "head": head,
            "weapon": str(r.get("weapon") or ""),
            "hp": hp,
            "victim_sid": str(r.get("victim_sid") or ""),
            "flick": r.get("flick_speed"),
            "hold": r.get("victim_hold_deg"),
        }
        if is_flick:
            # Flick singles stay solo-eligible: a long snap onto one bullet
            # is self-sufficient content, not a routine entry frag.
            label = "FLICK" + (" HEAD" if head else "")
            tick_bonus = headshot_bonus(str(r.get("weapon") or "")) if head else 0.0
            solos.append({**base,
                          "label": label,
                           "start_tick": tick - int(2.5 * tickrate),
                           "end_tick": tick + int(2.0 * tickrate),
                           "kill_ticks": [tick],
                           "hs_ticks": {tick: tick_bonus} if tick_bonus else {},
                           "hs_bonus": tick_bonus,
                           "insta_ticks": {tick: INSTA_QUALITY_BONUS},
                           "peek_ticks": {tick: peek_kill_bonus(r.get("victim_hold_deg"))},
                           "flick_speed": r.get("flick_speed"),
                           "rank_reason": (
                              f"flick: {r.get('weapon') or '?'} "
                              f"{(r.get('hitgroup') or '').strip()}")})
        elif "insta_kill" in reasons:
            pairable.append(base)
    # Group pairable kills by round into runs with consecutive gaps <=
    # INSTA_PAIR_SECONDS and distinct victims; each run of >= 2 with at
    # least one clean victim becomes ONE chained moment. Lone rows become
    # solo moments — a single verified instakill is hook material on its
    # own (no clean-victim gate: victim HP was never an insta gate).
    pair_gap = INSTA_PAIR_SECONDS * tickrate
    by_round: dict[int, list[dict]] = {}
    for b in sorted(pairable, key=lambda m: m["tick"]):
        by_round.setdefault(b["round"], []).append(b)
    for rnd in sorted(by_round):
        run: list[dict] = []
        runs: list[list[dict]] = []
        for b in by_round[rnd]:
            if (run and b["tick"] - run[-1]["tick"] <= pair_gap
                    and b["victim_sid"] != run[-1]["victim_sid"]):
                run.append(b)
            else:
                if run:
                    runs.append(run)
                run = [b]
        if run:
            runs.append(run)
        for run in runs:
            if len(run) >= 2 and (
                    INSTA_CLEAN_HP <= 0
                    or any(m["hp"] is not None and m["hp"] >= INSTA_CLEAN_HP
                           for m in run)):
                # Pair-level clean gate: at least one victim undamaged, so
                # tagged-cleanup doubles don't chain as ONE moment (0 disables
                # the gate). Failed runs split into solos below — each kill
                # is still a verified instakill on its own.
                out.append(_pair_moment(run, enabled, tickrate))
            else:
                for m in run:
                    out.append(_solo_moment(m, enabled, tickrate))
    out.extend(solos)
    # Fastest reaction first when several are available.
    out.sort(key=lambda m: (m["ttk"] if m["ttk"] is not None else 99.0,
                            m["start_tick"]))
    return out


def _solo_moment(m: dict, enabled: list[str], tickrate: int) -> dict:
    """One solo moment for a lone non-flick insta kill."""
    tick = m["tick"]
    ttk = m["ttk"]
    label = "INSTA"
    if ttk is not None:
        label += f" {ttk:.2f}s"
    if m["head"]:
        label += " HEAD"
    tick_bonus = (headshot_bonus(str(m.get("weapon") or ""))
                  if m["head"] else 0.0)
    return {
        "tier": "insta_kill",
        "tier_rank": enabled.index("insta_kill"),
        "label": label,
        "short_type": "insta_kill",
        "round": m["round"],
        "pov_steam_id": m["pov_steam_id"],
        "pov_nick": None,
        "start_tick": tick - int(2.5 * tickrate),
        "end_tick": tick + int(2.0 * tickrate),
        "kill_ticks": [tick],
        "round_win_tick": None,
        "clutch_initial_count": "",
        "rank_reason": (f"insta solo: {tick}"
                        + (f" ({ttk:.2f}s)" if ttk is not None else "")),
        "ttk": ttk,
        "hs_ticks": {tick: tick_bonus} if tick_bonus else {},
        "hs_bonus": tick_bonus,
        "insta_ticks": {tick: INSTA_QUALITY_BONUS},
        "peek_ticks": {tick: peek_kill_bonus(m.get("hold"))},
        "flick_speed": m.get("flick"),
    }


def _pair_moment(run: list[dict], enabled: list[str], tickrate: int) -> dict:
    """One chained moment for a run of paired insta kills."""
    ticks = [m["tick"] for m in run]
    ttks = [m["ttk"] for m in run if m["ttk"] is not None]
    fastest = min(ttks) if ttks else None
    heads = all(m["head"] for m in run)
    hs_ticks: dict[int, float] = {}
    peek_ticks: dict[int, float] = {}
    flick: float | None = None
    for m in run:
        if m["head"]:
            b = headshot_bonus(m.get("weapon") or "")
            if b:
                hs_ticks[m["tick"]] = b
        pb = peek_kill_bonus(m.get("hold"))
        if pb:
            peek_ticks[m["tick"]] = pb
        f = m.get("flick")
        if f is not None and (flick is None or f > flick):
            flick = f
    hs_bonus = sum(hs_ticks.values())
    label = f"{len(run)}× INSTA"
    if fastest is not None:
        label += f" {fastest:.2f}s"
    if heads:
        label += " HEAD"
    return {
        "tier": "insta_kill",
        "tier_rank": enabled.index("insta_kill"),
        "label": label,
        "short_type": "insta_kill",
        "round": run[0]["round"],
        "pov_steam_id": run[0]["pov_steam_id"],
        "pov_nick": None,
        "start_tick": ticks[0] - int(2.5 * tickrate),
        "end_tick": ticks[-1] + int(2.0 * tickrate),
        "kill_ticks": ticks,
        "round_win_tick": None,
        "clutch_initial_count": "",
        "chained": True,
        "rank_reason": ("insta pair: "
                        + ", ".join(f"{t} ({m['ttk']:.2f}s)" if m["ttk"] is not None else str(t)
                                    for t, m in zip(ticks, run))),
        "ttk": fastest,
        "hs_ticks": hs_ticks,
        "hs_bonus": hs_bonus,
        "insta_ticks": {t: INSTA_QUALITY_BONUS for t in ticks},
        "peek_ticks": peek_ticks,
        "flick_speed": flick,
    }


# ── Kill quality bonuses (never gates) ──────────────────────────────────
# Every kill of the POV player is a candidate ("kill" tier floor). These
# are SCORE bonuses layered on top — punch-up, wallbang, noscope,
# through-smoke — read from the action-timeline kill record. A kill with
# none of them still counts; a kill with all of them just outranks.
PUNCH_UP_BONUS = 200.0     # pistol (tier 1) headshot beats a rifle (tier >= 4)
WALLBANG_BONUS = 150.0     # penetrated >= 1
NOSCOPE_BONUS = 150.0      # engine noscope flag
THRU_SMOKE_DETER = -25.0   # smoked kill: mild deter (spam-averse), not a bonus


def kill_flag_bonus(k: dict) -> float:
    """Quality bonus points for ONE action-timeline kill (pure).

    Bonuses, never gates: the kill qualifies for the hook by existing;
    these only rank it. ``k`` is a ``kills_all`` record from the action
    timeline (carries ``headshot``, ``penetrated`` and the engine
    ``kill_event`` noscope flag). A through-smoke kill deters slightly —
    the engine flag is noisy and smoke-spam shouldn't outscore aim.
    """
    bonus = 0.0
    if (k.get("headshot")
            and weapon_tier(str(k.get("weapon") or "")) == 1
            and weapon_tier(str(k.get("victim_weapon") or "")) >= 4):
        bonus += PUNCH_UP_BONUS
    try:
        if int(k.get("penetrated") or 0) >= 1:
            bonus += WALLBANG_BONUS
    except (TypeError, ValueError):
        pass
    ev = k.get("kill_event") or {}
    if isinstance(ev, dict):
        if ev.get("noscope"):
            bonus += NOSCOPE_BONUS
        if ev.get("thrusmoke"):
            bonus += THRU_SMOKE_DETER
    return bonus


_MOMENT_TIER_TYPES = ("duel", "opener", "trade")


def timeline_moment_candidates(moments: list[dict], player_sid: str,
                               enabled: list[str],
                               timeline_kills: list[dict] | None = None,
                               rows: list[dict] | None = None) -> list[dict]:
    """Duel / opener / trade moments involving the POV player (pure).

    ``moments`` should already be the POV slice (pov_action_slice) — the
    involvement check is repeated defensively. Bomb/util moments are never
    hook material and are skipped. Windows come from the moment bounds; the
    hook planner trims them kill-anchored like any other moment.
    ``timeline_kills`` (the slice's kills) funds the headshot bonus for the
    POV player's own headshot kills in the moment; ``rows`` (rewind rows)
    fund the peek/flick enrichment on the same ticks.
    Kill sets are POV-only: a moment window can carry teammates' kills
    (e.g. T-on-T teamkills inside a duel engagement) and the planner chains
    + windows on every tick it is given — other people's kills under a
    spec-locked camera are dead air, so ticks outside ``timeline_kills``
    are dropped, and moments with no POV kill left are skipped outright.
    (``timeline_kills=None`` keeps the legacy unfiltered sets; the only
    production caller always passes them.)
    """
    by_tick: dict[int, dict] = {}
    for k in timeline_kills or []:
        try:
            by_tick.setdefault(int(k.get("tick", 0)), k)
        except (TypeError, ValueError):
            continue
    hold_by_tick: dict[int, object] = {}
    flick_by_tick: dict[int, object] = {}
    for r in rows or []:
        try:
            rtick = int(r["kill_tick"])
        except (TypeError, ValueError):
            continue
        hold_by_tick.setdefault(rtick, r.get("victim_hold_deg"))
        flick_by_tick.setdefault(rtick, r.get("flick_speed"))
    out: list[dict] = []
    for m in moments or []:
        t = str(m.get("type") or "")
        if t not in _MOMENT_TIER_TYPES or t not in enabled:
            continue
        kills = sorted({int(k) for k in (m.get("kill_ticks") or [])})
        if timeline_kills is not None:
            kills = [tick for tick in kills if tick in by_tick]
        if not kills:
            continue
        hs_bonus = 0.0
        hs_ticks: dict[int, float] = {}
        for tick in kills:
            k = by_tick.get(tick)
            if (k is not None
                    and str(k.get("attacker_steam_id") or "") == str(player_sid)
                    and k.get("headshot")):
                b = headshot_bonus(str(k.get("weapon") or ""))
                if b:
                    hs_ticks[tick] = b
        hs_bonus = sum(hs_ticks.values())
        peek_ticks: dict[int, float] = {}
        flick: float | None = None
        for tick in kills:
            pb = peek_kill_bonus(hold_by_tick.get(tick))
            if pb:
                peek_ticks[tick] = pb
            f = flick_by_tick.get(tick)
            if f is not None and (flick is None or f > flick):
                flick = f
        if t == "duel":
            label = str(m.get("label") or "DUEL").upper()
        else:
            label = TIER_LABELS.get(t, t.upper())
        out.append({
            "tier": t,
            "tier_rank": enabled.index(t),
            "label": label,
            "short_type": t,
            "round": int(m.get("round") or 0),
            "pov_steam_id": str(player_sid),
            "pov_nick": None,
            "start_tick": int(m.get("start_tick", kills[0])),
            "end_tick": int(m.get("end_tick", kills[-1])),
            "kill_ticks": kills,
            # Trade moments are per-kill revenge by construction, so every
            # kill here is a trade (duel/opener carry none — a revenge kill
            # within 5s would have its own trade moment and merge).
            "trade_ticks": list(kills) if t == "trade" else [],
            "round_win_tick": None,
            "clutch_initial_count": "",
            "rank_reason": f"{t} moment ({m.get('id', '?')})",
            "ttk": None,
            "hs_ticks": hs_ticks,
            "hs_bonus": hs_bonus,
            "peek_ticks": peek_ticks,
            "flick_speed": flick,
        })
    return out


def every_kill_candidates(kills: list[dict], player_sid: str | None,
                          enabled: list[str],
                          tickrate: int = 64) -> list[dict]:
    """One hook moment per kill the POV player lands (pure).

    Every single kill counts — no qualification gate. ``kills`` are
    action-timeline kill records when available (they carry headshot /
    penetrated / engine noscope-thrusmoke flags), falling back to the
    shorts-timeline kill list (weapon + penetrated only). The stronger
    detectors (4k/5k, insta, duels) rank ON TOP of this floor: they carry
    a higher tier base, and their per-kill detail fuses into any merged
    moment by tick. Kills within CHAIN_GAP_SECONDS merge into one
    continuous moment in pick_moments, so a burst of close kills pools
    its per-kill score into one segment instead of three flash cuts.
    """
    if not player_sid or "kill" not in enabled:
        return []
    out: list[dict] = []
    for k in kills or []:
        if str(k.get("attacker_steam_id") or "") != str(player_sid):
            continue
        try:
            tick = int(k["tick"])
        except (TypeError, ValueError):
            continue
        try:
            rnd = int(k.get("round") or 0)
        except (TypeError, ValueError):
            rnd = 0
        weapon = str(k.get("weapon") or "")
        hs_ticks: dict[int, float] = {}
        if k.get("headshot"):
            b = headshot_bonus(weapon)
            if b:
                hs_ticks[tick] = b
        flag = kill_flag_bonus(k)
        out.append({
            "tier": "kill",
            "tier_rank": enabled.index("kill"),
            "label": TIER_LABELS.get("kill", "KILL"),
            "short_type": "kill",
            "round": rnd,
            "pov_steam_id": str(player_sid),
            "pov_nick": None,
            "start_tick": tick - int(2.5 * tickrate),
            "end_tick": tick + int(2.0 * tickrate),
            "kill_ticks": [tick],
            "round_win_tick": None,
            "clutch_initial_count": "",
            "rank_reason": f"kill: {tick} ({weapon or '?'})",
            "ttk": None,
            "hs_ticks": hs_ticks,
            "hs_bonus": sum(hs_ticks.values()),
            "insta_ticks": {},
            "flag_ticks": {tick: flag} if flag else {},
            "peek_ticks": {},
            "flick_speed": None,
        })
    return out


def _round_for_tick(kills: list[dict], tick: int) -> int | None:
    """Round of the kill nearest ``tick`` (the timeline's kill list carries it)."""
    best, best_d = None, None
    for k in kills:
        d = abs(int(k.get("tick", 0)) - tick)
        if best_d is None or d < best_d:
            best, best_d = int(k.get("round", 0)), d
    return best


def _overlaps(a: dict, b: dict, slack: int = 64) -> bool:
    return not (a["end_tick"] + slack < b["start_tick"]
                or b["end_tick"] + slack < a["start_tick"])


def _moment(short: dict, tier: str, tier_rank: int, timeline: dict,
            player_nick: str) -> dict:
    kill_ticks = [int(k) for k in short.get("kill_ticks") or []]
    start_tick = int(short["start_tick"])
    end_tick = int(short["end_tick"])
    rnd = _round_for_tick(timeline.get("kills") or [], kill_ticks[0] if kill_ticks else start_tick)
    return {
        "tier": tier,
        "tier_rank": tier_rank,
        "label": TIER_LABELS.get(tier, tier.upper()),
        "short_type": short.get("short_type"),
        "round": rnd,
        "pov_steam_id": str(short.get("pov_steam_id", "")),
        "pov_nick": short.get("pov_nick") or player_nick,
        "start_tick": start_tick,
        "end_tick": end_tick,
        "kill_ticks": [t for t in kill_ticks if start_tick <= t <= end_tick],
        "round_win_tick": short.get("round_win_tick"),
        "clutch_initial_count": short.get("clutch_initial_count", ""),
        "pov_switch_tick": short.get("pov_switch_tick"),
        "pov_switch_to": short.get("pov_switch_to"),
        "pov_switch_to_nick": short.get("pov_switch_to_nick"),
        "rank_reason": _rank_reason(tier, short),
    }


def _resolve_player_sid(player: str | None) -> str | None:
    """steam64 passthrough, or nickname -> steam_id via player_accounts.json."""
    if not player:
        return None
    who = str(player).strip()
    if who.isdigit() and len(who) >= 17:
        return who
    try:
        accounts = json.loads(
            (_PROJECT_ROOT / ".data" / "player_accounts.json").read_text(encoding="utf-8"))
    except Exception:
        return None
    for a in accounts:
        nick = (a.get("nickname") or a.get("faceit_nickname") or "").strip().lower()
        if nick and nick == who.lower():
            return str(a.get("steam_id") or "") or None
    return None


def kill_credit_index(demo_path: Path, sid: str | None,
                      candidates: list[dict],
                      tickrate: int = 64) -> dict[int, dict]:
    """Per-kill scoring credit fused across candidates.

    Returns ``{kill_tick: {"hs": bool, "peek": float, "trade": bool,
    "insta": bool}}``.
    Sibling candidates hold per-kill data the merged moment may lack (e.g.
    an insta candidate's headshot/peek for a kill the clutch candidate
    also references), so hs/peek/trade/insta fuse by tick across every
    candidate. Pure candidate reads — no demo I/O, never raises.
    """
    credit: dict[int, dict] = {}
    for cand in candidates:
        hs_ticks = cand.get("hs_ticks") or {}
        peek_ticks = cand.get("peek_ticks") or {}
        insta_ticks = cand.get("insta_ticks") or {}
        trade_ticks = {int(k) for k in (cand.get("trade_ticks") or [])
                       if str(k).lstrip("-").isdigit()}
        for raw in cand.get("kill_ticks") or []:
            try:
                tick = int(raw)
            except (TypeError, ValueError):
                continue
            entry = credit.setdefault(
                tick, {"hs": False, "peek": 0.0, "trade": False, "insta": False,
                       "flag": 0.0})
            try:
                if float(hs_ticks.get(tick, hs_ticks.get(str(tick), 0)) or 0) > 0:
                    entry["hs"] = True
            except (TypeError, ValueError):
                pass
            try:
                entry["peek"] = max(
                    float(entry["peek"]),
                    float(peek_ticks.get(tick, peek_ticks.get(str(tick), 0)) or 0))
            except (TypeError, ValueError):
                pass
            if str(tick) in insta_ticks or tick in insta_ticks:
                entry["insta"] = True
            try:
                entry["flag"] = max(
                    float(entry["flag"]),
                    float((cand.get("flag_ticks") or {}).get(tick, 0) or 0))
            except (TypeError, ValueError):
                pass
            if tick in trade_ticks:
                entry["trade"] = True
    return credit


def _sort_key(m: dict):
    """Tier first, then kill count desc, then faster TTK, then tick."""
    return (m["tier_rank"], -len(m.get("kill_ticks") or []),
            m.get("ttk") if m.get("ttk") is not None else 99.0,
            m["start_tick"])


# A traded kill forfeits the maximum reaction credit an insta can earn: the
# ttk term peaks at (0.5 - 0) * 200 = 100, so each traded tick deters 100.
# Tier/kill/headshot points are untouched — a traded 2K still outscores a
# traded single, it just never earns reaction credit for the revenge.
TRADE_DETER_POINTS = 100.0


def _window_kills(window: dict, kill_ticks: list[int]) -> list[int]:
    """Kill ticks enclosed by one planned window, in order."""
    a, b = int(window["start_tick"]), int(window["end_tick"])
    return sorted(k for k in kill_ticks if a <= k <= b)


def segment_quality(window_kills: list[int], moment: dict,
                    kill_credit: dict[int, dict]) -> float:
    """Score one planned window (what the viewer experiences as one clip).

    A window enclosing ALL the moment's kills keeps the full moment score
    (tier base, flick/TTK included) — an unbroken chain is one continuous
    piece of footage. A fragment window scores only what it encloses, with
    no tier base: per kill +100, +50 headshot, +peek bonus, −100 traded.
    Scattered multi-kill pieces therefore rank as the weak singles they
    play as, instead of pooling tier + count at moment level. No smoke
    concept: a smoke kill inside a fragment simply earns no reaction
    credit, and an unbroken smoke-heavy chain keeps whatever its tier
    and headshots pay.
    """
    kills = [int(k) for k in (moment.get("kill_ticks") or [])]
    enclosed = sorted(set(window_kills) & set(kills))
    if enclosed and enclosed == sorted(set(kills)):
        return moment_quality(moment)
    score = 0.0
    for tick in enclosed:
        credit = kill_credit.get(int(tick)) or {}
        score += 100.0
        try:
            if float(credit.get("hs") or 0.0) > 0:
                score += 50.0
        except (TypeError, ValueError):
            pass
        try:
            score += max(0.0, float(credit.get("peek") or 0.0))
        except (TypeError, ValueError):
            pass
        if credit.get("insta"):
            score += INSTA_QUALITY_BONUS
        try:
            score += float(credit.get("flag") or 0.0)  # may deter (negative)
        except (TypeError, ValueError):
            pass
        if credit.get("trade"):
            score -= TRADE_DETER_POINTS
    return score


def chain_segments(chain: dict, tickrate: int,
                   kill_credit: dict[int, dict],
                   all_kill_ticks: list[int] | None = None,
                   ) -> tuple[float, list[tuple[dict, float]]]:
    """Plan a chain's windows and score each: (best score, [(window, score)]).

    The chain's quality IS its best single segment — the unit scored is
    the unit watched. Fill/budget/assembly below consume chains in this
    order with unchanged mechanics.
    """
    from cs2archive.pov.hook_plan import plan_hook
    kills = sorted({int(k) for k in (chain.get("kill_ticks") or [])})
    windows = []
    for planned in plan_hook([{**chain, "kill_ticks": kills}], tickrate,
                             all_kill_ticks=all_kill_ticks):
        windows.extend(planned.get("windows") or [])
    scored = []
    for window in windows:
        enclosed = _window_kills(window, kills)
        scored.append((window, segment_quality(enclosed, chain, kill_credit)))
    best = max((score for _, score in scored), default=0.0)
    return best, scored


def moment_quality(m: dict) -> float:
    """Impressiveness score for one picked moment (higher = better).

    The tier ladder dominates (a 4K always outranks any single, whatever the
    TTK); within a tier more kills win, then headshot bonuses stack flat
    (every headshot the same, counted once per kill tick no matter how many
    fused candidates reference it — chained kills add up, so 2 headshots
    outscore 1), then faster reactions. Traded kills deter flat (see
    TRADE_DETER_POINTS) — a revenge kill never earns reaction credit.
    There is deliberately no clutch-disadvantage bonus: the tier base
    already pays clutches top dollar, so an extra would double-count.
    Pure — drives the least-impressive-first assembly order so the hook
    builds to its climax.
    """
    rank = int(m.get("tier_rank", 99))
    score = (len(TIER_ORDER) - rank) * 1000.0
    score += len(m.get("kill_ticks") or []) * 100.0
    try:
        score += float(m.get("hs_bonus") or 0.0)
    except (TypeError, ValueError):
        pass
    # Peek-kill bonuses stack per kill (deduped per tick on merges).
    peek = m.get("peek_ticks")
    if isinstance(peek, dict):
        try:
            score += sum(float(v) for v in peek.values())
        except (TypeError, ValueError):
            pass
    # Verified insta kills pay a flat per-kill bonus (deduped per tick on
    # merges — an insta kill folded into a 3k earns it exactly once).
    insta = m.get("insta_ticks")
    if isinstance(insta, dict):
        try:
            score += len(insta) * INSTA_QUALITY_BONUS
        except (TypeError, ValueError):
            pass
    # Flag bonuses (punch-up / wallbang / noscope / through-smoke) stack
    # per kill — sum across merged candidates.
    flag = m.get("flag_ticks")
    if isinstance(flag, dict):
        try:
            score += sum(float(v) for v in flag.values())
        except (TypeError, ValueError):
            pass
    # Flick speed: the faster the crosshair was moving, the more quality.
    score += flick_speed_bonus(m.get("flick_speed"))
    ttk = m.get("ttk")
    if ttk is not None:
        try:
            score += max(0.0, 0.5 - float(ttk)) * 200.0
        except (TypeError, ValueError):
            pass
    try:
        score -= len(m.get("trade_ticks") or []) * TRADE_DETER_POINTS
    except TypeError:
        pass
    return score


CHAIN_GAP_SECONDS = 5.0    # chain moments this close instead of cutting; cut at/above
CHAIN_MAX_SPAN_SECONDS = 15.0  # refuse unions beyond this (fall back to overlap-drop)


def _kill_gap(a: dict, b: dict) -> float:
    """Smallest tick gap between two moments' kill sets (inf when either
    has no kills — those fall back to bounds overlap)."""
    ka = sorted(int(k) for k in (a.get("kill_ticks") or []))
    kb = sorted(int(k) for k in (b.get("kill_ticks") or []))
    if not ka or not kb:
        return 0.0 if _overlaps(a, b) else float("inf")
    return float(min(abs(x - y) for x in ka for y in kb))


def _merge_moments(target: dict, src: dict) -> None:
    """Fold ``src`` into ``target`` in place: union bounds + kills, best tier
    wins, flagged chained so the planner keeps one continuous window.

    Headshot bonuses always add up (2 headshots in one segment outscore 1)
    and the fastest TTK wins — scoring stacks even though the label keeps
    the best tier's name.
    """
    target["start_tick"] = min(int(target["start_tick"]), int(src["start_tick"]))
    target["end_tick"] = max(int(target["end_tick"]), int(src["end_tick"]))
    kills = sorted({int(k) for k in (target.get("kill_ticks") or [])}
                   | {int(k) for k in (src.get("kill_ticks") or [])})
    target["kill_ticks"] = kills
    merged_hs: dict[int, float] = {}
    for src_map in (src.get("hs_ticks") or {}, target.get("hs_ticks") or {}):
        if isinstance(src_map, dict):
            for k, v in src_map.items():
                try:
                    merged_hs[int(k)] = float(v)
                except (TypeError, ValueError):
                    continue
    target["hs_ticks"] = merged_hs
    target["hs_bonus"] = sum(merged_hs.values())
    merged_insta: dict[int, float] = {}
    for src_map in (src.get("insta_ticks") or {}, target.get("insta_ticks") or {}):
        if isinstance(src_map, dict):
            for k, v in src_map.items():
                try:
                    merged_insta[int(k)] = float(v)
                except (TypeError, ValueError):
                    continue
    target["insta_ticks"] = merged_insta
    merged_flag: dict[int, float] = {}
    for src_map in (src.get("flag_ticks") or {}, target.get("flag_ticks") or {}):
        if isinstance(src_map, dict):
            for k, v in src_map.items():
                try:
                    merged_flag[int(k)] = merged_flag.get(int(k), 0.0) + float(v)
                except (TypeError, ValueError):
                    continue
    target["flag_ticks"] = merged_flag
    merged_peek: dict[int, float] = {}
    for src_map in (src.get("peek_ticks") or {}, target.get("peek_ticks") or {}):
        if isinstance(src_map, dict):
            for k, v in src_map.items():
                try:
                    merged_peek[int(k)] = float(v)
                except (TypeError, ValueError):
                    continue
    target["peek_ticks"] = merged_peek
    # Trade ticks union like kills: a traded kill stays deterred no matter
    # which chain absorbs it.
    merged_trade: set[int] = set()
    for src_ticks in (target.get("trade_ticks"), src.get("trade_ticks")):
        for k in src_ticks or []:
            try:
                merged_trade.add(int(k))
            except (TypeError, ValueError):
                continue
    target["trade_ticks"] = sorted(merged_trade)
    # Flick speed: the fastest crosshair wins — never summed.
    try:
        f_src = src.get("flick_speed")
        f_tgt = target.get("flick_speed")
        vals = [float(f) for f in (f_src, f_tgt) if f is not None]
        target["flick_speed"] = max(vals) if vals else None
    except (TypeError, ValueError):
        pass
    ttks = [t for t in (target.get("ttk"), src.get("ttk"))
            if t is not None]
    try:
        target["ttk"] = min(float(t) for t in ttks) if ttks else None
    except (TypeError, ValueError):
        pass
    if int(src.get("tier_rank", 99)) < int(target.get("tier_rank", 99)):
        for key in ("tier", "tier_rank", "label", "rank_reason", "round",
                    "pov_steam_id", "pov_nick", "short_type"):
            if src.get(key) is not None:
                target[key] = src[key]
    target["chained"] = True


def pick_moments(candidates: list[dict], tickrate: int,
                 max_moments: int) -> list[dict]:
    """Best-first pick with chaining: moments whose kills land within
    ``CHAIN_GAP_SECONDS`` merge into one continuous moment (union bounds +
    kills, best tier wins, flagged ``chained``) instead of jump-cut clips;
    cut only at/above the gap. A merged chain counts as ONE moment toward
    ``max_moments`` (merges never consume slots, so scanning never stops
    early — a chainable late candidate still merges after the quota fills).
    Overlapping leftovers that would exceed the chain span cap are dropped
    (old overlap rule). ``candidates`` must be sorted best-first (see
    ``_sort_key``). Transitive chains resolve to a fixpoint (a bridge merged
    into one chain is re-checked against the rest).
    """
    chain_gap = CHAIN_GAP_SECONDS * tickrate
    chain_span = CHAIN_MAX_SPAN_SECONDS * tickrate

    def _span(a: dict, b: dict) -> float:
        return (max(int(a["end_tick"]), int(b["end_tick"]))
                - min(int(a["start_tick"]), int(b["start_tick"])))

    chains: list[dict] = []
    appends = 0
    overflow: list[dict] = []  # quota-blocked; re-scanned as chains grow
    for c in candidates:
        merged = False
        for ch in chains:
            if _kill_gap(c, ch) >= chain_gap or _span(c, ch) > chain_span:
                continue
            _merge_moments(ch, c)
            merged = True
            break
        if merged:
            continue
        if any(_overlaps(c, ch) for ch in chains):
            continue
        if appends >= max_moments:
            # Not dropped: a chain that grows later (absorbing a neighbour)
            # can pull this candidate inside its merge gap — the fixpoint
            # re-scan below gives it a second chance WITHOUT a new slot.
            overflow.append({"kill_ticks": list(c.get("kill_ticks") or []), **c})
            continue
        chains.append({"kill_ticks": list(c.get("kill_ticks") or []), **c})
        appends += 1
    # Fixpoint: a merge can bring two chains within chaining distance, and
    # a grown chain can pull in an overflow candidate. Re-scan until stable.
    while True:
        merged_any = False
        for o in overflow[:]:
            for ch in chains:
                if _kill_gap(o, ch) >= chain_gap or _span(o, ch) > chain_span:
                    continue
                _merge_moments(ch, o)
                overflow.remove(o)
                merged_any = True
                break
            if merged_any:
                break
        if not merged_any:
            for i, a in enumerate(chains):
                for b in chains[i + 1:]:
                    if _kill_gap(a, b) >= chain_gap or _span(a, b) > chain_span:
                        continue
                    _merge_moments(a, b)
                    chains.remove(b)
                    merged_any = True
                    break
                if merged_any:
                    break
        if not merged_any:
            break
    return chains


def fill_to_target(chains: list[dict], *, tickrate: int,
                   min_seconds: float,
                   all_kill_ticks: list[int] | None = None) -> tuple[list[dict], float]:
    """Accumulate chains best-first until planned footage reaches the bar.

    Returns (picked, total_planned_seconds). Chains are sorted by quality
    desc (ties: earlier first), so this is the minimum number of segments
    to reach ``min_seconds``. Chains without kills are unplannable and
    never accumulate. Short of the bar: ([], total) — the caller ships no
    hook. Pure except the deterministic hook planner.
    """
    ordered = sorted(chains, key=lambda m: (-m.get("quality", 0.0),
                                            m.get("start_tick", 0)))
    picked: list[dict] = []
    total = 0.0
    for m in ordered:
        if not m.get("kill_ticks"):
            continue
        total += planned_seconds(plan_hook([m], tickrate,
                                           all_kill_ticks=all_kill_ticks),
                                 tickrate)
        picked.append(m)
        if total >= min_seconds:
            break
    if total < min_seconds:
        return [], total
    return picked, total


def enforce_footage_budget(chains: list[dict], tickrate: int,
                           max_seconds: float,
                           all_kill_ticks: list[int] | None = None,
                           ) -> list[dict]:
    """Drop the weakest chains until the PLANNED footage fits max_seconds.

    The budget bounds what gets rendered, so it must measure planned windows
    (what the viewer sees), not uncut detection spans: a two-kill moment can
    span ~60s of detection while shipping 4s, and budgeting spans evicts good
    chains on phantom footage. Quality must already be stamped (weakest drops
    first); always keeps at least one chain. Pure.
    """
    chains = list(chains)
    while len(chains) > 1:
        total = sum(
            planned_seconds(plan_hook([m], tickrate,
                                      all_kill_ticks=all_kill_ticks), tickrate)
            for m in chains)
        if total <= max_seconds:
            break
        chains.remove(min(chains,
                          key=lambda m: (m.get("quality", 0.0),
                                         m.get("start_tick", 0))))
    return chains


def build_hook_timeline(
    demo_path: Path,
    player: str | None = None,
    pros_only: bool = True,
    tiers: str = DEFAULT_TIERS,
    max_moments: int = 8,
    max_seconds: float = 60.0,
    min_round: int = MIN_ROUND_DEFAULT,
    tickrate: int = 64,
    min_seconds: float = 10.0,
    pov_dir: str | Path | None = None,
) -> dict:
    """Rank a demo's hook-worthy moments and pick the minimum set that fills
    ``min_seconds`` of planned footage.

    ``player`` (steam64 or nickname) restricts to one POV — the POV of the
    video the hook is prepended to. Chains accumulate best-first (quality
    order) until the planned footage reaches ``min_seconds``; at most
    ``max_moments`` chains and ``max_seconds`` of uncut footage. Assembly
    order is least-impressive-first (climax last). Moments whose kills land
    within 5s chain into one continuous moment (no jump cut); wider gaps
    stay separate clips. Short of the bar: no hook (normal skip).
    """
    demo_path = Path(demo_path)
    enabled = [t.strip() for t in tiers.split(",") if t.strip() in TIER_ORDER]
    if not enabled:
        raise ValueError(f"no valid tiers in {tiers!r} (known: {', '.join(TIER_ORDER)})")

    timeline = build_short_timeline(demo_path, pros_only=pros_only, player=player)
    shorts = timeline.get("shorts") or []

    want = None
    if player:
        want = str(player).strip().lower()

    candidates: list[dict] = []
    for s in shorts:
        if want:
            sid = str(s.get("pov_steam_id", "")).lower()
            nick = (s.get("pov_nick") or "").strip().lower()
            if want not in (sid, nick):
                continue
        tier = tier_of(s, int(timeline.get("tickrate") or 64))
        if tier is None or tier not in enabled:
            continue
        nick = s.get("pov_nick") or (player or "Unknown")
        candidates.append(_moment(s, tier, enabled.index(tier), timeline, nick))

    # LOS-TTK insta kills: single-kill moments the multikill-based
    # Shorts extractor cannot see (e.g. a 0.19s pistol headshot). Only the
    # POV player's own kills. One shared rewind parse feeds both the insta
    # pairs and the duel/opener/trade enrichment below.
    sid = _resolve_player_sid(player)
    map_name = str(timeline.get("map") or "")
    rows: list[dict] = []
    if bool({"insta_kill", "duel", "opener", "trade"} & set(enabled)) and sid:
        rows = _load_insta_rows(demo_path, sid, map_name, tickrate)

    # Action-timeline data (POV-local cache via --pov-dir, self-healed on
    # demand). duel/opener/trade come from the POV slice; its kill records
    # (headshot/penetrated/noscope/thrusmoke flags) also power the
    # every-kill floor's quality bonuses.
    atl_kills: list[dict] = []
    atl_moments: list[dict] = []
    if bool({"kill", "duel", "opener", "trade"} & set(enabled)) and sid:
        atl_path = _action_timeline_for(demo_path, pov_dir)
        if atl_path is not None:
            try:
                atl = json.loads(atl_path.read_text(encoding="utf-8"))
                sl = pov_action_slice(atl, sid)
                atl_kills = sl["kills"]
                atl_moments = sl["moments"]
            except Exception as e:  # noqa: BLE001
                print(f"  [WARN] action timeline slice failed ({e})")

    # Every kill counts: one candidate per POV kill (action-timeline
    # records preferred — they carry the quality flags). Stronger moments
    # rank on top; plain kills only surface alone when nothing better
    # covers them.
    candidates.extend(every_kill_candidates(atl_kills or timeline.get("kills"),
                                            sid, enabled, tickrate=tickrate))

    if "insta_kill" in enabled and rows:
        insta = pair_insta_rows(rows, sid, enabled, tickrate)
        for m in insta:
            m["pov_nick"] = m.get("pov_nick") or (player or "Unknown")
        candidates.extend(insta)

    if atl_moments:
        for m in timeline_moment_candidates(atl_moments, sid, enabled,
                                            timeline_kills=atl_kills,
                                            rows=rows):
            m["pov_nick"] = m.get("pov_nick") or (player or "Unknown")
            candidates.append(m)

    # Round filter: a hook must not replay the opening round (see round_allowed).
    before = len(candidates)
    candidates = [c for c in candidates if round_allowed(c.get("round"), min_round)]
    dropped_early = before - len(candidates)
    if dropped_early:
        print(f"  [round] dropped {dropped_early} moment(s) from rounds < {min_round}")

    # Best first, then kills desc.
    candidates.sort(key=_sort_key)
    chains = pick_moments(candidates, tickrate, max_moments)

    # Target fill: stamp each chain with its best single segment, then
    # accumulate best-first until the PLANNED footage reaches min_seconds.
    # The unit scored is the unit watched: a chain ranks by its strongest
    # continuous window, never by pooling tier + count across scattered
    # pieces. Short of the bar: no hook (normal skip).
    # POV kills only (same input render_hook plans with — any player's
    # deaths would cap tails the render leaves alone).
    try:
        all_ticks = sorted({int(k.get("tick", 0))
                            for k in (timeline.get("kills") or [])
                            if str(k.get("attacker_steam_id") or "") == str(sid)})
    except (TypeError, ValueError):
        all_ticks = []
    credit = kill_credit_index(demo_path, sid, candidates, tickrate)
    for m in chains:
        best, _windows = chain_segments(
            m, tickrate, credit,
            all_kill_ticks=all_ticks or None)
        m["quality"] = best
    chains = enforce_footage_budget(chains, tickrate, max_seconds,
                                    all_kill_ticks=all_ticks or None)
    picked, planned_total = fill_to_target(chains, tickrate=tickrate,
                                           min_seconds=min_seconds,
                                           all_kill_ticks=all_ticks or None)
    if not picked and chains:
        print(f"  [skip] planned hook {planned_total:.1f}s < minimum {min_seconds:g}s "
              f"— no hook for this POV")

    # Climax last: assemble least-impressive-first so the hook builds
    # instead of peaking early.
    picked.sort(key=lambda m: (m["quality"], m["start_tick"]))

    player_sid = picked[0]["pov_steam_id"] if picked else (player or "")
    player_nick = (picked[0].get("pov_nick") if picked else None) or (player or "Unknown")
    if picked:
        # Canonical nick per moment (player_accounts by steam_id): the demo's
        # raw name is often wrong or a bare steam64, which misses the
        # prosettings lookup and drops the hook onto a different crosshair
        # from the POV.
        from cs2archive.crosshair_resolve import canonical_nick
        for m in picked:
            m["pov_nick"] = canonical_nick(
                m.get("pov_steam_id"), m.get("pov_nick") or (player or "Unknown"))
        player_nick = picked[0].get("pov_nick") or player_nick
    return {
        "hook_type": "hook_timeline",
        "demo_path": str(demo_path),
        "map": timeline.get("map", "Unknown"),
        "tickrate": int(timeline.get("tickrate") or tickrate),
        "player": {"steam_id": player_sid, "nick": player_nick},
        "params": {
            "tiers": enabled,
            "min_round": int(min_round),
            "max_moments": int(max_moments),
            "max_seconds": float(max_seconds),
            "min_seconds": float(min_seconds),
            "rule_version": INSTA_RULE_VERSION,
        },
        "picked": picked,
        "candidates": candidates,
        "reason": ("; ".join(m["rank_reason"] for m in reversed(picked))
                   if picked else
                   (f"planned footage below minimum {min_seconds:g}s" if chains
                    else (f"no qualifying moment above round {min_round - 1}" if dropped_early
                          else "no qualifying moment"))),
    }


def hook_run_dir(demo_path: Path, player: str | None,
                 pov_dir: str | Path | None = None) -> Path:
    """Hook home: {pov_dir}/hook (explicit) or the derived POV folder.

    The derived home mirrors the pipeline render dir
    (renders/pov-{stem}_{nick}); the nick is canonicalized by steam_id so a
    bare-sid invocation lands in the same folder as the pipeline run.
    """
    if pov_dir:
        return Path(pov_dir) / "hook"
    who = (player or "all").strip()
    try:
        from cs2archive.crosshair_resolve import canonical_nick
        sid = _resolve_player_sid(who)
        if sid:
            who = canonical_nick(sid, who)
    except Exception:
        pass
    return _pov_dir(Path(demo_path).stem, who) / "hook"


def _action_timeline_for(demo_path: Path, pov_dir: str | Path | None) -> Path | None:
    """POV-local action-timeline cache (self-heal for older demos).

    Written into the POV folder — never into renders/hl-*, which is
    reserved for the multi-pros highlights pipeline.
    """
    from cs2archive.highlights.build_action_timeline import ensure_action_timeline

    out = Path(pov_dir) / "action_timeline.json" if pov_dir else None
    return ensure_action_timeline(demo_path, output=out)


def main() -> int:
    ap = argparse.ArgumentParser(description="Build a Hook Timeline JSON from a demo")
    ap.add_argument("demo_path", type=Path, help="Path to .dem file")
    ap.add_argument("--player", default=None,
                    help="POV player to build the hook for (steam64 or nickname)")
    ap.add_argument("--tiers", default=DEFAULT_TIERS,
                    help=f"Comma-separated qualifying tiers, best first "
                         f"(default: {DEFAULT_TIERS})")
    ap.add_argument("--max-moments", type=int, default=8,
                    help="Keep at most this many moments (default: 8 — 2s "
                         "singles need ~7 segments to fill the 15s bar)")
    ap.add_argument("--max-seconds", type=float, default=60.0,
                    help="Total uncut footage budget in seconds (default: 60)")
    ap.add_argument("--min-seconds", type=float, default=10.0,
                    help="Minimum planned footage: accumulate best-first until "
                         "the hook reaches this (default: 10). Short of it, "
                         "no hook ships.")
    ap.add_argument("--min-round", type=int, default=MIN_ROUND_DEFAULT,
                    help=f"Earliest round a hook may use (default: {MIN_ROUND_DEFAULT} — "
                         f"round 1 is ~30s into the video, so replaying it "
                         f"as a cold open is wasted; 0/1 are always useless)")
    ap.add_argument("--pov-dir", type=Path, default=None,
                    help="POV render dir (default: derived renders/pov-{stem}_{nick}). "
                         "The hook lands in {pov-dir}/hook and the action-timeline "
                         "cache in {pov-dir}/action_timeline.json.")
    ap.add_argument("--include-all-players", action="store_true",
                    help="Accept moments from any player (default: Recognised Pros only; "
                         "ignored when --player is given)")
    ap.add_argument("--output", "-o", type=Path, default=None,
                    help="Override the output JSON path")
    args = ap.parse_args()

    demo = args.demo_path
    if not demo.is_file():
        print(f"[ERR] demo not found: {demo}", file=sys.stderr)
        return 1

    timeline = build_hook_timeline(
        demo,
        player=args.player,
        pros_only=not args.include_all_players,
        tiers=args.tiers,
        max_moments=args.max_moments,
        max_seconds=args.max_seconds,
        min_round=args.min_round,
        min_seconds=args.min_seconds,
        pov_dir=args.pov_dir,
    )

    if not timeline["picked"]:
        print(f"[OK] no hook: {timeline['reason']} (no output written)")
        return 0

    out = args.output or (hook_run_dir(demo, args.player,
                                       pov_dir=args.pov_dir) / "hook_timeline.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(timeline, indent=2), encoding="utf-8")

    print(f"[OK] {len(timeline['picked'])} moment(s) for "
          f"{timeline['player']['nick']} | {timeline['map']} -> {out} "
          f"(assembly order: least impressive first)")
    for i, m in enumerate(timeline["picked"], 1):
        print(f"  {i}. {m['label']:22s} r{m['round']} "
              f"ticks {m['start_tick']}->{m['end_tick']} "
              f"({len(m['kill_ticks'])} kill(s), quality {m.get('quality', 0):.0f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
