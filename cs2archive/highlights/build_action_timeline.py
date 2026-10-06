"""Build an Action Timeline from a demo (HLTV or FACEIT, data only).

Parses demoparser2 events — kills, bomb events, utility (throws, blinds,
util damage), round lifecycle, round-end winners — and writes:

    renders/hl-{demo_stem}/action_timeline.json

Timeline v2 (``timeline_version: 2``) is match-level and multi-POV ready:

- ``kills`` — unchanged legacy list (kills involving a Recognised Pro).
- ``kills_all`` — every legitimate kill (suicides/teamkills/world still
  excluded) with ``attacker_is_pro`` / ``victim_is_pro`` flags, plus
  ``attacker_place`` / ``victim_place`` engine callouts (``last_place_name``
  at the kill tick, "" when unavailable).
- ``rounds[]`` — per-round start/freeze/end ticks, authoritative winner
  (``round_end`` side mapped through per-round side snapshots, heuristic
  fallback), win reason, running score, alive-count progression, plus a
  ``stakes`` block (buy classification from ``current_equip_value`` sampled
  post-freeze, win streaks, deficit, swing/conversion flags) and
  ``hot_zones`` (modal kill callouts).
- ``moments[]`` — deterministically derived multikills, clutches (+ lost
  attempts), openers, trades, and bomb plays, each with a tick window,
  ranked ``pov_candidates`` and a ``zone`` (``place`` + ``site``, None when
  neither is known). This is the edit layer's input: the goal is to
  make LLM moment-picking unnecessary (or trivially easy).
- ``arc`` — match-level narrative shape (lead changes, longest run, max
  deficit, pistol rounds).
- ``pros[]`` — per-Recognised-Pro per-round ledger (kills/deaths/openers/
  multis/clutches + tags) with a match ``arc`` (best round, silent rounds,
  longest scoring run, revenge list). Pure post-processing, no demo access.

Timeline v3 (``timeline_version: 3``) adds ``stakes`` / ``hot_zones`` /
``zone`` / ``arc`` / ``pros``. All v3 fields are additive and nullable —
downstream must tolerate their absence on v2 caches. The multipov edit
layer accepts v2 and v3; the future stakes-aware scorer will require v3.

Economy sampling note: buys are read at ``freeze_end`` / ``freeze_end`` + 64
ticks (post-buy), NOT at round-start ticks (pre-buy — players have not bought
yet, so a round-start sample would misread every full buy as an eco). The
side snapshot (``_side_map_by_round``) stays on round starts; economy is a
separate snapshot on freeze ticks.

Usage:
    python cs2archive/highlights/build_action_timeline.py <demo_path>
    python cs2archive/highlights/build_action_timeline.py demos/faceit/some-match.dem
    python cs2archive/highlights/build_action_timeline.py demos/hltv/some-match/some-map.dem
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]

from cs2archive.faceit.faceit_names import canonical_nick, known_pro_steam_ids  # noqa: E402
from cs2archive.weapons import resolve_weapon_id

BOMB_WEAPONS = frozenset({"c4", "planted_c4"})

def _is_faceit_demo(path: Path) -> bool:
    try:
        path.resolve().relative_to((PROJECT_ROOT / "demos" / "faceit").resolve())
        return True
    except ValueError:
        return "demos/faceit" in str(path).replace("\\", "/")


def _sid(val) -> str:
    if val is None:
        return ""
    if isinstance(val, float) and math.isnan(val):
        return ""
    s = str(val).strip()
    if not s or s.lower() == "nan":
        return ""
    return s


def _round_for_tick(
    tick: int,
    round_starts: list[tuple[int, int]],
    first_freeze: int | None,
) -> int:
    """Map tick -> round number using round_start events (last start <= tick).
    Ticks before the first round_start are round 0 (warmup)."""
    rn = 0
    for start_tick, round_num in round_starts:
        if start_tick <= tick:
            rn = round_num
        else:
            break
    return rn


def _map_name(demo_path: Path, header_map: str) -> str:
    if header_map:
        raw = header_map
    else:
        m = re.search(r"(de_[a-z0-9]+)", demo_path.name, re.I)
        raw = m.group(1) if m else ""
    if raw.lower().startswith("de_"):
        return raw[3:].capitalize() if raw.lower() != "de_dust2" else "Dust2"
    return raw or "Unknown"


TIMELINE_VERSION = 3

# Rebuild token for the v3 derived layers (stakes / zones / pro ledger).
# Bump when classification rules change so stale caches rebuild, mirroring
# INSTA_RULE_VERSION discipline. Bumped to 2 when the eco-ratio branch was
# dropped (Q1 debate verdict: the 0.2x rule could only fire on force buys and
# mislabelled them eco, and no real round ever needed it).
STAKES_VERSION = 2

# --- Stakes (v3): buy classification from per-player current_equip_value ---
# Pistol rounds are MR12 half-openers (round 1 and 13); overtime starts with
# full money, so no pistol class there. Full/eco cutoffs come from live
# probes (pistol ~650/player, eco ~200-260, full buy ~4300-5700). There is
# deliberately no relative (fraction-of-opponent) rule: at 0.2x it could only
# fire on force buys, mislabelling them eco — see the Q1 debate verdict.
STAKES_PISTOL_ROUNDS = (1, 13)
STAKES_FULL_BUY_AVG = 3500
STAKES_ECO_AVG = 1000
# Minimum sampled players for a team's buy to count: a 1-2 player sample
# cannot represent a 5-man buy, so those teams read unknown, never eco/full.
STAKES_MIN_SAMPLED_PLAYERS = 3

_MOMENT_PRE_TICKS = 320   # 5s lead-in before the first kill of a moment
_MOMENT_POST_TICKS = 128  # 2s out-run after the last kill of a moment
_TRADE_WINDOW_TICKS = 320  # 5s revenge window for a trade
_BURST_WINDOW_TICKS = 640  # 10s window clustering a util exec
_BURST_MIN_THROWS = 3
_CT_TIME_WIN_REASONS = frozenset({
    "time_ran_out",
    "RoundEndReasonHostagesNotRescued",
})

# Damage-dealing utility (flash does no damage — it is tracked via blinds).
UTIL_DAMAGE_WEAPONS = frozenset({"hegrenade", "inferno", "molotov", "incendiary"})
UTIL_KILL_WEAPONS = frozenset({"hegrenade", "inferno"})
UTIL_THROW_EVENTS = (
    ("hegrenade_detonate", "he"),
    ("flashbang_detonate", "flash"),
    ("smokegrenade_detonate", "smoke"),
    ("inferno_startburn", "molotov"),
)


def _as_df(result):
    """Coerce a demoparser2 parse_event result to a pandas DataFrame.

    Empty events come back as a plain list in some versions; downstream code
    uses ``.empty`` / ``.iterrows`` / ``sort_values``.
    """
    import pandas as pd

    if isinstance(result, pd.DataFrame):
        return result
    if result is None:
        return pd.DataFrame(columns=["tick"])
    try:
        return pd.DataFrame(list(result), columns=["tick"])
    except Exception:
        return pd.DataFrame(columns=["tick"])


def _tier_helpers():
    """Weapon-tier rules, shared with the shorts builder (lazy: shorts imports
    this module lazily, so importing it here at top level would be circular)."""
    try:
        from cs2archive.shorts.build_short_timeline import (
            _is_punch_up,
            _is_wallbang_rifle,
            _meets_tier_criterion,
            _punch_up_tags,
        )
        return _is_punch_up, _meets_tier_criterion, _punch_up_tags, _is_wallbang_rifle
    except Exception:
        return None, None, None, None


def _penetrated_count(val) -> int:
    try:
        if val is None:
            return 0
        n = int(val)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def _side_map_by_round(parser, round_starts: list[tuple[int, int]],
                       team_by_sid: dict[str, int]) -> dict[int, dict[str, int]]:
    """Map round -> {"T": persistent_team, "CT": persistent_team}.

    ``round_end.winner`` names the winning *side* (flips at halftime) while
    ``player_info.team_number`` is the persistent faction. Sampling every
    player's side at each round start bridges the two (same approach as the
    shorts builder's ``_winner_by_round_from_demo``).
    """
    out: dict[int, dict[str, int]] = {}
    try:
        rs_ticks = sorted({t for t, rn in round_starts if rn > 0})
        if not rs_ticks:
            return out
        snap = parser.parse_ticks(["steamid", "team_num"], ticks=rs_ticks)
        tick_to_round = {t: rn for t, rn in round_starts if rn > 0}
        per_tick: dict[int, dict[int, int]] = {}
        for _, row in snap.iterrows():
            sid = _sid(row.get("steamid"))
            tick = int(row["tick"])
            try:
                side = int(row.get("team_num"))
            except (TypeError, ValueError):
                continue
            if sid and sid in team_by_sid and side in (2, 3):
                per_tick.setdefault(tick, {})[side] = team_by_sid[sid]
        for tick, mapping in per_tick.items():
            rn = tick_to_round.get(tick)
            if rn and 2 in mapping and 3 in mapping:
                out[rn] = {"T": mapping[2], "CT": mapping[3]}
    except Exception:
        return {}
    return out


def _authoritative_winners(
    round_end_df,
    round_starts: list[tuple[int, int]],
    side_map: dict[int, dict[str, int]],
) -> tuple[dict[int, int], dict[int, str]]:
    """Per-round winner as persistent team_number + engine win reason.

    ``round_end.round`` is +1-shifted in many FACEIT demos, so rounds are
    derived by tick (last round_start <= tick), mirroring the shorts builder.
    """
    winners: dict[int, int] = {}
    reasons: dict[int, str] = {}
    try:
        if round_end_df is None or round_end_df.empty:
            return winners, reasons
        rs_sorted = sorted(round_starts)

        def _rn_for_tick(tick: int) -> int:
            rn = 0
            for st, r in rs_sorted:
                if st <= tick:
                    rn = r
                else:
                    break
            return rn

        rs_tick_by_round = {rn: t for t, rn in round_starts}
        for _, row in round_end_df.iterrows():
            side = str(row.get("winner", "") or "").strip().upper()
            if side not in ("T", "CT"):
                continue
            tick = int(row["tick"])
            rn = _rn_for_tick(tick)
            if rn <= 0:
                continue
            team = side_map.get(rn, {}).get(side)
            if team:
                winners[rn] = team
            reason = row.get("reason", "")
            if reason == reason and reason not in ("", None):
                reasons[rn] = str(reason).strip()
    except Exception:
        return {}, {}
    return winners, reasons


def _pov_candidates(ordered_sids: list[str], roles: dict[str, str],
                    pro_sids: dict[str, str], cap: int = 4) -> tuple[list[dict], str]:
    """Rank POV steam_ids: pros first (keeping caller order), then the rest.

    Returns (candidates, primary_pro_sid). ``primary_pro_sid`` is "" when no
    candidate is a Recognised Pro (edit layer cannot render those POVs).
    """
    seen: list[str] = []
    for sid in ordered_sids:
        if sid and sid not in seen:
            seen.append(sid)
    pros = [s for s in seen if s in pro_sids]
    rest = [s for s in seen if s not in pro_sids]
    ranked = (pros + rest)[:cap]
    return (
        [{"steam_id": s, "role": roles.get(s, "involved"),
          "is_pro": s in pro_sids} for s in ranked],
        pros[0] if pros else "",
    )


def _derive_moments(
    kills_all: list[dict],
    round_deaths: dict[int, list[tuple[int, str]]],
    round_starts: dict[int, int],
    round_ends: dict[int, int],
    winner_by_round: dict[int, int],
    win_reason_by_round: dict[int, str],
    bomb_actions: list[dict],
    team_by_sid: dict[str, int],
    pro_sids: dict[str, str],
    util_throws: list[dict] | None = None,
    all_hurts: list[dict] | None = None,
) -> list[dict]:
    """Deterministic match-level moments for the multi-POV edit layer.

    Pure function of parsed structures (unit-testable, no demo needed).
    Moment types: multikill (3+), clutch (+ lost clutch_attempt), opener,
    trade, duel (1v1/1v2), closer, wallbang, util_kill, util_burst,
    danger (pro survives below 30hp), bomb_plant, bomb_defuse, bomb_explode.
    """
    _is_punch_up, _meets_tier_criterion, _punch_up_tags, _is_wallbang_rifle = _tier_helpers()
    moments: list[dict] = []
    team_ids = sorted({t for t in team_by_sid.values() if t and t >= 2})

    def _window(ticks: list[int], rn: int) -> tuple[int, int]:
        start = max(min(ticks) - _MOMENT_PRE_TICKS, round_starts.get(rn, 0))
        end = max(ticks) + _MOMENT_POST_TICKS
        if rn in round_ends:
            end = min(end, round_ends[rn] + _MOMENT_POST_TICKS)
        return start, end

    kills_by_round: dict[int, list[dict]] = {}
    for k in kills_all:
        kills_by_round.setdefault(k["round"], []).append(k)

    for rn in sorted(kills_by_round):
        if rn <= 0:
            continue
        rkills = sorted(kills_by_round[rn], key=lambda k: k["tick"])
        winner = winner_by_round.get(rn)
        n = 0

        def _emit(mtype: str, ticks: list[int], ordered: list[str],
                  roles: dict[str, str], detail: dict) -> None:
            nonlocal n
            n += 1
            start, end = _window(ticks, rn)
            cands, primary_pro = _pov_candidates(ordered, roles, pro_sids)
            moments.append({
                "id": f"r{rn}-{mtype}-{n}",
                "type": mtype,
                "round": rn,
                "start_tick": start,
                "end_tick": end,
                "kill_ticks": sorted(ticks),
                "pov_candidates": cands,
                "primary_pro_sid": primary_pro,
                "round_won_by": winner,
                **detail,
            })

        # --- Multikills (3+ by one attacker; tier/punch-up notes, not gates) ---
        by_attacker: dict[str, list[dict]] = {}
        for k in rkills:
            if k["attacker_steam_id"]:
                by_attacker.setdefault(k["attacker_steam_id"], []).append(k)
        for aid, ak in by_attacker.items():
            if len(ak) < 3:
                continue
            tier_ok = bool(_meets_tier_criterion(ak)) if _meets_tier_criterion else True
            punch = sum(1 for k in ak if _is_punch_up(k)) if _is_punch_up else 0
            tags = _punch_up_tags(ak) if _punch_up_tags else []
            blinded = sum(1 for k in ak if k.get("blinded_by"))
            _emit(
                "multikill", [k["tick"] for k in ak], [aid, *[k["victim_steam_id"] for k in ak]],
                {aid: "attacker", **{k["victim_steam_id"]: "victim" for k in ak}},
                {"label": f"{len(ak)}K", "attacker_steam_id": aid,
                 "kill_count": len(ak), "tier_ok": tier_ok,
                 "attacker_team": team_by_sid.get(aid),
                 "victim_teams": sorted(
                     {team_by_sid.get(k["victim_steam_id"]) for k in ak}
                     - {None, 0}),
                 "punch_up_kills": punch, "punch_up_tags": tags,
                 "blinded_kills": blinded,
                 "round_won": (winner is not None and team_by_sid.get(aid) == winner)},
            )

        # --- Util kills (HE / molotov — always noteworthy) ---
        for k in rkills:
            if not k.get("util_kill"):
                continue
            aid = k["attacker_steam_id"]
            if not aid:
                continue
            w = str(k.get("weapon", "") or "")
            _emit(
                "util_kill", [k["tick"]], [aid, k["victim_steam_id"]],
                {aid: "attacker", k["victim_steam_id"]: "victim"},
                {"label": "HE KILL" if "hegrenade" in w else "MOLOTOV",
                 "attacker_steam_id": aid, "weapon": w},
            )

        # --- Clutches (2v5 or 1v3+ trigger per team; won or lost attempt) ---
        deaths = sorted(round_deaths.get(rn, []))
        alive = {t: 5 for t in team_ids}
        triggered: dict[int, dict] = {}
        # victim team at each death, in order
        for tick, victim in deaths:
            vt = team_by_sid.get(victim, 0)
            if vt in alive:
                alive[vt] = max(0, alive[vt] - 1)
            for team in team_ids:
                if team in triggered:
                    continue
                enemy = next((t for t in team_ids if t != team), None)
                if enemy is None or enemy not in alive:
                    continue
                if (alive[team] == 2 and alive[enemy] == 5) or (
                        alive[team] == 1 and alive[enemy] >= 3):
                    triggered[team] = {
                        "start_tick": tick,
                        "initial": f"{alive[team]}v{alive[enemy]}",
                    }
        for team, trig in triggered.items():
            post = [k for k in rkills if k["tick"] >= trig["start_tick"]
                    and team_by_sid.get(k["attacker_steam_id"]) == team]
            if not post:
                continue  # scoreless lockdown is not a moment
            # Clutcher: most post-trigger kills (tie -> latest kill).
            by_killer: dict[str, list[dict]] = {}
            for k in post:
                by_killer.setdefault(k["attacker_steam_id"], []).append(k)
            clutcher = max(
                by_killer,
                key=lambda a: (len(by_killer[a]), max(k["tick"] for k in by_killer[a])),
            )
            # Resolution: bomb win by this team after the trigger, else the
            # authoritative round winner. CT clock wins are not clutches.
            if win_reason_by_round.get(rn) in _CT_TIME_WIN_REASONS:
                continue
            won: bool | None = None
            win_tick = None
            for b in bomb_actions:
                if (b["round"] == rn and b["tick"] >= trig["start_tick"]
                        and b["type"] in ("defuse", "explode")
                        and team_by_sid.get(b["player_steam_id"]) == team):
                    won, win_tick = True, b["tick"]
                    break
            if won is None:
                if rn in winner_by_round:
                    won = winner_by_round[rn] == team
                    win_tick = round_ends.get(rn)
                else:
                    continue  # unknown outcome — not a moment
            mates = sorted(
                {k["attacker_steam_id"] for k in post},
                key=lambda a: (-len(by_killer[a]), a not in pro_sids),
            )
            ticks = [k["tick"] for k in post]
            if won and win_tick:
                ticks.append(win_tick)
            _emit(
                "clutch" if won else "clutch_attempt", ticks,
                [clutcher, *mates],
                {clutcher: "clutcher",
                 **{a: "teammate" for a in mates if a != clutcher}},
                {"label": trig["initial"] + (" CLUTCH" if won else " ATTEMPT"),
                 "clutcher_steam_id": clutcher,
                 "clutch_initial_count": trig["initial"], "won": won},
            )

        # --- Opener (first kill of the round) ---
        first = rkills[0]
        if first["attacker_steam_id"]:
            _emit(
                "opener", [first["tick"]],
                [first["attacker_steam_id"], first["victim_steam_id"]],
                {first["attacker_steam_id"]: "attacker",
                 first["victim_steam_id"]: "victim"},
                {"label": "OPENER", "attacker_steam_id": first["attacker_steam_id"],
                 "victim_steam_id": first["victim_steam_id"]},
            )

        # --- Wallbangs (rifle/AWP through cover) ---
        for k in rkills:
            if not k.get("penetrated"):
                continue
            if _is_wallbang_rifle and not _is_wallbang_rifle(
                    str(k.get("weapon", "") or "")):
                continue
            aid = k["attacker_steam_id"]
            if not aid:
                continue
            _emit(
                "wallbang", [k["tick"]], [aid, k["victim_steam_id"]],
                {aid: "attacker", k["victim_steam_id"]: "victim"},
                {"label": "WALLBANG", "attacker_steam_id": aid,
                 "weapon": str(k.get("weapon", "") or "")},
            )

        # --- Duels (first 1v1 / 1v2 / 2v1 state, won) ---
        # Close finishes below the clutch trigger. Needs a pro alive at the
        # trigger or a pro kill after it; CT clock wins don't count.
        members = {t: [s for s, tno in team_by_sid.items() if tno == t]
                   for t in team_ids}
        dead_so_far: set[str] = set()
        duel = None
        all_deaths = sorted(round_deaths.get(rn, []))
        for idx, (tick, victim) in enumerate(all_deaths):
            dead_so_far.add(victim)
            counts = {t: sum(1 for s in members.get(t, []) if s not in dead_so_far)
                      for t in team_ids}
            vals = sorted(counts.values())
            if vals == [1, 1] or vals == [1, 2]:
                alive_now = {t: [s for s in members.get(t, []) if s not in dead_so_far]
                             for t in team_ids}
                # Tightest state reached from here to round end (a 1v2 that
                # becomes a 1v1 is labeled 1v1).
                tight = vals
                probe_dead = set(dead_so_far)
                for tick2, victim2 in all_deaths[idx + 1:]:
                    probe_dead.add(victim2)
                    c2 = sorted(
                        sum(1 for s in members.get(t, []) if s not in probe_dead)
                        for t in team_ids)
                    if min(c2) == 0:
                        break  # round over — a 0vX state is not a duel
                    if sum(c2) < sum(tight):
                        tight = c2
                duel = {"tick": tick, "counts": counts, "alive": alive_now,
                        "tight": tight}
                break
        if duel and win_reason_by_round.get(rn) not in _CT_TIME_WIN_REASONS:
            winside = [t for t in team_ids
                       if winner_by_round.get(rn) == t]
            if winside:
                alive_pros = [s for t in team_ids for s in duel["alive"][t]
                              if s in pro_sids]
                post_pro_kills = [k for k in rkills
                                  if k["tick"] > duel["tick"]
                                  and k["attacker_steam_id"] in pro_sids]
                if alive_pros or post_pro_kills:
                    ordered = alive_pros + [
                        k["attacker_steam_id"] for k in post_pro_kills
                        if k["attacker_steam_id"] not in alive_pros]
                    roles = {s: "duelist" for s in ordered}
                    a, b = duel["tight"]
                    ticks = [duel["tick"], *sorted(
                        k["tick"] for k in rkills if k["tick"] > duel["tick"])]
                    _emit(
                        "duel", ticks, ordered, roles,
                        {"label": f"{a}V{b}",
                         "duel_state": f"{a}v{b}"},
                    )

        # --- Closer (last kill of the round) ---
        if rkills:
            last = max(rkills, key=lambda k: k["tick"])
            if last["attacker_steam_id"]:
                _emit(
                    "closer", [last["tick"]],
                    [last["attacker_steam_id"], last["victim_steam_id"]],
                    {last["attacker_steam_id"]: "attacker",
                     last["victim_steam_id"]: "victim"},
                    {"label": "CLOSER",
                     "attacker_steam_id": last["attacker_steam_id"]},
                )

        # --- Trades (revenge within the window) ---
        seen_trade_attackers: set[str] = set()
        for k in rkills:
            aid, vid = k["attacker_steam_id"], k["victim_steam_id"]
            if not aid or not vid or aid in seen_trade_attackers:
                continue
            # Did the victim just kill one of the attacker's teammates?
            revenged = False
            for k2 in rkills:
                if (k2["attacker_steam_id"] == vid
                        and team_by_sid.get(k2["victim_steam_id"])
                        == team_by_sid.get(aid)
                        and 0 < k["tick"] - k2["tick"] <= _TRADE_WINDOW_TICKS):
                    revenged = True
                    break
            if revenged:
                seen_trade_attackers.add(aid)
                _emit(
                    "trade", [k["tick"]], [aid, vid],
                    {aid: "attacker", vid: "victim"},
                    {"label": "TRADE", "attacker_steam_id": aid,
                     "victim_steam_id": vid},
                )

    # --- Util bursts (exec setups: 3+ team throws inside 10s, same round) ---
    # Quality bar: at least 2 util types including a setup piece (smoke or
    # flash). Lone-flash chains and pure HE/molotov spam are not execs.
    # One burst per (round, team) at most: the largest window (most throws,
    # tie -> earliest). The edit layer wants "the exec", not fragments.
    throws_by_round_team: dict[tuple[int, int], list[dict]] = {}
    for u in util_throws or []:
        t = team_by_sid.get(u["player_steam_id"], 0)
        if t:
            throws_by_round_team.setdefault((u["round"], t), []).append(u)
    for (rn, team), throws in sorted(throws_by_round_team.items()):
        if rn <= 0:
            continue
        throws.sort(key=lambda u: u["tick"])
        windows: list[list[dict]] = []
        i = 0
        while i < len(throws):
            window = [u for u in throws[i:]
                      if u["tick"] - throws[i]["tick"] <= _BURST_WINDOW_TICKS]
            kinds = sorted({u["util"] for u in window})
            if (len(window) >= _BURST_MIN_THROWS and len(kinds) >= 2
                    and ("smoke" in kinds or "flash" in kinds)):
                windows.append(window)
                i += len(window)
            else:
                i += 1
        if not windows:
            continue
        window = max(windows, key=lambda w: (len(w), -w[0]["tick"]))
        kinds = sorted({u["util"] for u in window})
        order: dict[str, int] = {}
        for u in window:
            order[u["player_steam_id"]] = order.get(u["player_steam_id"], 0) + 1
        throwers = sorted(order, key=lambda s: (-order[s], s not in pro_sids))
        ticks = [u["tick"] for u in window]
        start = max(min(ticks) - _MOMENT_PRE_TICKS, round_starts.get(rn, 0))
        end = max(ticks) + _MOMENT_POST_TICKS
        if rn in round_ends:
            end = min(end, round_ends[rn] + _MOMENT_POST_TICKS)
        cands, primary_pro = _pov_candidates(
            throwers, {s: "exec" for s in throwers}, pro_sids)
        moments.append({
            "id": f"r{rn}-t{team}-burst-1",
            "type": "util_burst",
            "round": rn,
            "start_tick": start,
            "end_tick": end,
            "kill_ticks": [],
            "pov_candidates": cands,
            "primary_pro_sid": primary_pro,
            "round_won_by": winner_by_round.get(rn),
            "label": f"EXEC ({'+'.join(kinds)})",
            "utils": kinds,
            "throw_count": len(window),
        })

    # --- Danger (pro dropped below 30hp who survives 5s+; SIEZ plays
    # these low-HP survivals as tension with no kill attached) ---
    # NOTE (accepted blindspot, v1 scope): all_hurts covers gun damage only
    # list); a pro who dies purely to util (HE/molly/burn, no gun hit) has
    # no all_hurts row and is invisible here — accepted v1 scope, not a bug
    # in the survival check below.
    for h in all_hurts or []:
        if h["health"] >= 30:
            continue
        rn, tick, vid = h["round"], h["tick"], h["victim_steam_id"]
        if rn <= 0:
            continue
        # survived = no death of this victim from this hit through the
        # next 5s (fatal blow lands on the same tick as the death).
        survived = not any(tick <= dtick <= tick + _TRADE_WINDOW_TICKS
                           and dvid == vid
                           for dtick, dvid in round_deaths.get(rn, []))
        if not survived:
            continue
        start = max(tick - _MOMENT_PRE_TICKS, round_starts.get(rn, 0))
        end = tick + _MOMENT_POST_TICKS + _TRADE_WINDOW_TICKS
        if rn in round_ends:
            end = min(end, round_ends[rn] + _MOMENT_POST_TICKS)
        cands, primary_pro = _pov_candidates(
            [vid], {vid: "survivor"}, pro_sids)
        moments.append({
            "id": f"r{rn}-danger-{tick}",
            "type": "danger",
            "round": rn,
            "start_tick": start,
            "end_tick": end,
            "kill_ticks": [],
            "pov_candidates": cands,
            "primary_pro_sid": primary_pro,
            "round_won_by": winner_by_round.get(rn),
            "label": f"LOW {int(h['health'])}HP",
            "survivor_steam_id": vid,
        })

    # --- Bomb plays (anyone; planter/defuser POV) ---
    for b in bomb_actions:
        rn = b["round"]
        if rn <= 0:
            continue
        sid = b["player_steam_id"]
        if not sid:
            continue
        role = {"plant": "planter", "defuse": "defuser"}.get(b["type"], "involved")
        cands, primary_pro = _pov_candidates([sid], {sid: role}, pro_sids)
        moments.append({
            "id": f"r{rn}-bomb_{b['type']}-{b['tick']}",
            "type": f"bomb_{b['type']}",
            "round": rn,
            "start_tick": max(b["tick"] - _MOMENT_PRE_TICKS,
                              round_starts.get(rn, 0)),
            "end_tick": b["tick"] + _MOMENT_POST_TICKS,
            "kill_ticks": [],
            "pov_candidates": cands,
            "primary_pro_sid": primary_pro,
            "round_won_by": winner_by_round.get(rn),
            "label": b["type"].upper(),
            "site": b.get("site", ""),
        })

    # --- Zones (v3): attacker's engine callout on each moment ---
    # First kill tick with a known attacker place wins (same-tick double
    # kills collide on (round, tick) — first kept; same fight, same area in
    # the common case). Bomb moments carry site but no kill ticks, so they
    # get a site-only zone. Moments with neither read as None, never "".
    kill_place: dict[tuple[int, int], str] = {}
    for k in kills_all:
        ap = k.get("attacker_place", "")
        if ap:
            kill_place.setdefault((k["round"], k["tick"]), ap)
    for m in moments:
        place = ""
        for t in m.get("kill_ticks", []):
            if (m["round"], t) in kill_place:
                place = kill_place[(m["round"], t)]
                break
        site = m.get("site", "")
        m["zone"] = {"place": place, "site": site} if (place or site) else None

    moments.sort(key=lambda m: (m["start_tick"], m["id"]))
    return moments


def _classify_buy(avg, is_pistol) -> str:
    """One team's buy class for a round (v3 stakes).

    ``avg`` is per-player mean current_equip_value (None when unsampled or
    under-sampled). Pistol rounds classify as pistol regardless of value —
    including unsampled teams, since pistols are game-guaranteed on rounds
    1/13; anything between eco and full is a force/half-buy. Absolute
    cutoffs only.
    """
    if is_pistol:
        return "pistol"
    if avg is None:
        return "unknown"
    if avg >= STAKES_FULL_BUY_AVG:
        return "full"
    if avg <= STAKES_ECO_AVG:
        return "eco"
    return "force"


def buys_veto_fires(buys: dict, victim_teams, kill_count) -> bool:
    """True when a 4K+ was farmed exclusively off eco-buy victims.

    Single source of truth shared by the moment scorer
    (``build_multipov_timeline.moment_value``) and the round scorer
    (``round_score.score_rounds`` taint) so the two verdicts cannot diverge.
    ``buys`` maps str(team) -> buy class; unknown/absent teams never veto.
    """
    try:
        kc = int(kill_count or 0)
    except (TypeError, ValueError):
        return False
    teams = [t for t in (victim_teams or []) if t]
    if kc < 4 or not teams:
        return False
    return all((buys or {}).get(str(t)) == "eco" for t in teams)


def _derive_stakes(rounds, equip_by_round, team_ids) -> tuple[dict, dict]:
    """Per-round stakes + match arc (v3). Pure function of parsed structures.

    ``rounds`` is the built rounds[] list (round, winner_team, score_after,
    match_point_for). ``equip_by_round`` maps round -> team ->
    {"total": int, "players": int} (may omit rounds/teams — those read as
    unknown buys, never as zeros). Streaks/deficit/arc need no ticks, so
    they are always computed; only ``buys`` degrade to unknown.
    """
    stakes_by_round: dict[int, dict] = {}
    arc: dict = {}
    streak = {t: 0 for t in team_ids}
    prev_scores = {t: 0 for t in team_ids}
    last_leader = None
    lead_changes = 0
    first_lead_change_round = None
    longest_run = {"team": None, "count": 0}
    max_deficit = {"round": None, "team": None, "deficit": 0}

    def _score(r, t) -> int:
        try:
            return int(r.get("score_after", {}).get(str(t), 0))
        except (TypeError, ValueError):
            return 0

    for r in rounds:
        rn = r["round"]
        deficit = {}
        for t in team_ids:
            opp_best = max([prev_scores[o] for o in team_ids if o != t],
                           default=prev_scores[t])
            deficit[t] = prev_scores[t] - opp_best
            if -deficit[t] > max_deficit["deficit"]:
                max_deficit = {"round": rn, "team": t, "deficit": -deficit[t]}

        eq = equip_by_round.get(rn, {}) if equip_by_round else {}
        totals: dict = {}
        avgs: dict = {}
        sampled: dict = {}
        for t in team_ids:
            e = eq.get(t)
            if not e or (e.get("players") or 0) < STAKES_MIN_SAMPLED_PLAYERS:
                totals[t] = None
                avgs[t] = None
                sampled[t] = (e or {}).get("players")
            else:
                totals[t] = e["total"]
                avgs[t] = e["total"] / e["players"]
                sampled[t] = e["players"]
        has_ticks = any(v is not None for v in totals.values())
        is_pistol = rn in STAKES_PISTOL_ROUNDS
        buys = {}
        for t in team_ids:
            if not has_ticks:
                buys[str(t)] = "unknown"
                continue
            buys[str(t)] = _classify_buy(avgs[t], is_pistol)

        w = r.get("winner_team")
        swing = bool(w in streak and streak[w] == 0
                     and any(streak[o] >= 3 for o in team_ids if o != w))
        conversion = bool(r.get("match_point_for") is not None
                          and r.get("match_point_for") == w)
        stakes_by_round[rn] = {
            "buys": buys,
            "equip_value": {str(t): totals[t] for t in team_ids},
            "equip_players": {str(t): sampled[t] for t in team_ids},
            "win_streak_before": {str(t): streak[t] for t in team_ids},
            "deficit_at_start": {str(t): deficit[t] for t in team_ids},
            "swing": swing,
            "conversion": conversion,
            "is_pistol": is_pistol,
            "buy_source": "ticks" if has_ticks else "unknown",
        }

        if w in streak:
            for t in team_ids:
                streak[t] = streak[t] + 1 if t == w else 0
            if streak[w] > (longest_run["count"] or 0):
                longest_run = {"team": w, "count": streak[w]}
        scores_now = {t: _score(r, t) for t in team_ids}
        top = max(scores_now.values(), default=0)
        leaders = [t for t in team_ids if scores_now[t] == top and top > 0]
        leader = leaders[0] if len(leaders) == 1 else None
        if leader is not None and last_leader is not None and leader != last_leader:
            lead_changes += 1
            if first_lead_change_round is None:
                first_lead_change_round = rn
        if leader is not None:
            last_leader = leader
        prev_scores = scores_now

    live = sorted(r["round"] for r in rounds)
    arc = {
        "total_rounds": len(rounds),
        "pistol_rounds": [rn for rn in live if rn in STAKES_PISTOL_ROUNDS],
        "lead_changes": lead_changes,
        "first_lead_change_round": first_lead_change_round,
        "longest_run": longest_run,
        "max_deficit": max_deficit,
    }
    return stakes_by_round, arc


def _economy_by_round(parser, fe_by_round) -> tuple[dict, bool]:
    """Per-round per-sid max current_equip_value over (freeze_end, +64 ticks).

    The +64 sample covers buys landing just after freeze_end. Returns
    ({round: {sid: equip}}, resolved); resolved is False when the prop is
    absent (parse_ticks silently drops unknown props) or the snapshot fails
    — callers must then emit buy_source "unknown", never trust zeros.
    ``balance`` is deliberately NOT read: buy classification needs equipment
    value (what a team brings to the round), not banked cash. Distinguishing
    a deliberate save from a broke eco would need money in hand — a future
    use, not this verdict.
    """
    try:
        fe_ticks = sorted(set(fe_by_round.values()))
        if not fe_ticks:
            return {}, False
        query = sorted(set(fe_ticks) | {t + 64 for t in fe_ticks})
        snap = parser.parse_ticks(["steamid", "current_equip_value"], ticks=query)
        if snap is None or getattr(snap, "empty", True):
            return {}, False
        if "current_equip_value" not in list(getattr(snap, "columns", [])):
            return {}, False
        tick_to_round = {}
        for rn, t in fe_by_round.items():
            tick_to_round[t] = rn
            tick_to_round[t + 64] = rn
        import pandas as _pd

        snap["_equip"] = _pd.to_numeric(snap["current_equip_value"], errors="coerce")
        out: dict[int, dict[str, float]] = {}
        for _, row in snap.iterrows():
            rn = tick_to_round.get(int(row["tick"]))
            sid = _sid(row.get("steamid"))
            val = row["_equip"]
            if rn is None or not sid or val != val:  # NaN check
                continue
            prev = out.setdefault(rn, {}).get(sid)
            if prev is None or val > prev:
                out[rn][sid] = float(val)
        return out, bool(out)
    except Exception:
        return {}, False


def _derive_pro_ledger(kills_all, round_deaths, moments, live_rounds, pro_sids,
                       team_by_sid=None, winner_by_round=None) -> list[dict]:
    """Per-Recognised-Pro per-round ledger + match arc (v3).

    Pure post-processing of action_timeline structures (no demo access).
    Deaths are counted from ``round_deaths`` (which includes teamkills,
    suicides and world deaths, matching ``rounds[].alive``) — never from
    ``kills_all``. ``openers`` is factual (first kill of the round, won or
    lost — the "entry" tag stays factual with it); ``openers_won`` counts
    only openers in rounds the opener's team won (needs ``team_by_sid`` +
    ``winner_by_round``; without them every opener reads as unwon, never as
    won). Tags carry no weighting and there is no impact score; the edit
    layer decides value. Pros with no kills and no deaths are omitted (no
    story). best_round is None when the pro scored no kills (honest, not
    round 1 by default).
    """
    team_by_sid = team_by_sid or {}
    winner_by_round = winner_by_round or {}
    per: dict[str, dict[int, dict]] = {
        sid: {rn: {"kills": 0, "deaths": 0, "openers": 0, "openers_won": 0,
                   "multikills": 0, "best_multi": 0,
                   "clutches_won": 0, "clutch_attempts": 0}
              for rn in live_rounds}
        for sid in pro_sids
    }
    for k in kills_all:
        rn = k.get("round", 0)
        a = k.get("attacker_steam_id", "")
        if a in per and rn in per[a]:
            per[a][rn]["kills"] += 1
    for rn in live_rounds:
        for _tick, vid in (round_deaths or {}).get(rn, []):
            if vid in per and rn in per[vid]:
                per[vid][rn]["deaths"] += 1
    for m in moments:
        rn = m.get("round", 0)
        t = m.get("type")
        if t == "opener":
            a = m.get("attacker_steam_id", "")
            if a in per and rn in per[a]:
                per[a][rn]["openers"] += 1
                if (winner_by_round.get(rn) is not None
                        and winner_by_round.get(rn) == team_by_sid.get(a)):
                    per[a][rn]["openers_won"] += 1
        elif t == "multikill":
            a = m.get("attacker_steam_id", "")
            if a in per and rn in per[a]:
                per[a][rn]["multikills"] += 1
                try:
                    kc = int(m.get("kill_count", 0) or 0)
                except (TypeError, ValueError):
                    kc = 0
                per[a][rn]["best_multi"] = max(per[a][rn]["best_multi"], kc)
        elif t in ("clutch", "clutch_attempt"):
            c = m.get("clutcher_steam_id", "")
            if c in per and rn in per[c]:
                key = "clutches_won" if t == "clutch" else "clutch_attempts"
                per[c][rn][key] += 1

    kills_set = {(k.get("attacker_steam_id", ""), k.get("victim_steam_id", ""),
                  k.get("round", 0)) for k in kills_all}
    killed_by: dict[tuple[str, int], str] = {}
    for k in kills_all:
        if k.get("attacker_steam_id") and k.get("victim_steam_id"):
            killed_by.setdefault(
                (k["victim_steam_id"], k.get("round", 0)), k["attacker_steam_id"])

    pros = []
    for sid, nick in pro_sids.items():
        if sid not in per:
            continue
        rounds_list = []
        tot_k = tot_d = tot_m = tot_cw = 0
        for rn in live_rounds:
            d = per[sid][rn]
            tags = []
            if d["openers"]:
                tags.append("entry")
            if d["multikills"]:
                tags.append("multi")
            if d["clutches_won"]:
                tags.append("clutch")
            if d["clutch_attempts"] and not d["clutches_won"]:
                tags.append("attempt")
            if d["kills"] == 0:
                tags.append("silent")
            rounds_list.append({"round": rn, "kills": d["kills"],
                                "deaths": d["deaths"],
                                "openers": d["openers"],
                                "openers_won": d["openers_won"],
                                "multikills": d["multikills"],
                                "best_multi": d["best_multi"],
                                "clutches_won": d["clutches_won"],
                                "clutch_attempts": d["clutch_attempts"],
                                "tags": tags})
            tot_k += d["kills"]
            tot_d += d["deaths"]
            tot_m += d["multikills"]
            tot_cw += d["clutches_won"]
        if tot_k == 0 and tot_d == 0:
            continue
        best_round = None
        if tot_k > 0:
            best_round = min(live_rounds,
                             key=lambda rn: (-per[sid][rn]["kills"], rn))
        silent_rounds = [rn for rn in live_rounds if per[sid][rn]["kills"] == 0]
        longest_streak, cur = 0, 0
        for rn in live_rounds:
            cur = cur + 1 if per[sid][rn]["kills"] > 0 else 0
            longest_streak = max(longest_streak, cur)
        revenge = []
        seen_rev = set()
        for (victim, rn), killer in killed_by.items():
            if victim != sid or not killer:
                continue
            for rn2 in (rn, rn + 1):
                if ((sid, killer, rn2) in kills_set
                        and (rn2, killer) not in seen_rev):
                    seen_rev.add((rn2, killer))
                    revenge.append({"round": rn2, "on_steam_id": killer})
        pros.append({
            "steam_id": sid,
            "nick": nick,
            "rounds": rounds_list,
            "arc": {"best_round": best_round,
                    "silent_rounds": silent_rounds,
                    "longest_streak": longest_streak,
                    "revenge": sorted(revenge, key=lambda r: (r["round"], r["on_steam_id"]))},
            "totals": {"kills": tot_k, "deaths": tot_d,
                       "multikills": tot_m, "clutches_won": tot_cw},
        })
    pros.sort(key=lambda p: (-p["totals"]["kills"], p["steam_id"]))
    return pros


def _round_starts_from_events(rows) -> dict[int, int]:
    """{round: start_tick} from round_start rows [(tick, round)].

    One entry per round number, last seen wins (demos repeat round starts —
    FACEIT emits round 1 four times). Tick-0/1 rows are kept when they carry
    a real round number (HLTV emits round 1 at tick 1; dropping it silently
    deletes the first round — CR-20) and dropped only as warmup phantoms
    (tick <= 1 with no round number). Rows without a round number are
    numbered sequentially in tick order.
    """
    by_round: dict[int, tuple[int, int]] = {}
    for t, rn in sorted(rows):
        if t <= 1 and rn <= 0:
            continue  # warmup phantom
        if rn <= 0:
            rn = len(by_round) + 1
        by_round[rn] = (t, rn)
    return {rn: t for rn, (t, _rn) in sorted(by_round.items())}


def _bomb_action_site(label, rn, tick, sid, place_at, plant_sites) -> str:
    """Bombsite callout for a bomb action (CR-19).

    demoparser2's `site` column on bomb events is a C4 entity handle, never a
    bombsite. Plants/defuses read the actor's engine callout at the action
    tick (the actor is standing on the site); explodes reuse the round's
    plant site because the planter may have rotated away by detonation,
    falling back to the actor's callout when no plant was recorded.
    ``plant_sites`` maps round -> site and is filled by the caller.
    """
    if label == "explode" and plant_sites.get(rn):
        return plant_sites[rn]
    return place_at(tick, sid)


def build_action_timeline(demo_path: Path) -> dict:
    import demoparser2 as dp
    from cs2archive.shorts.event_data import EVENT_DATA_VERSION, kill_event_details, raw_event_table

    parser = dp.DemoParser(str(demo_path))

    # Core events
    deaths = parser.parse_event("player_death")
    round_start = parser.parse_event("round_start")
    freeze_end = parser.parse_event("round_freeze_end")
    round_end = parser.parse_event("round_officially_ended")
    round_end_winner = parser.parse_event("round_end")
    hurt = _as_df(parser.parse_event("player_hurt"))
    weapon_fire = _as_df(parser.parse_event("weapon_fire"))
    blind = _as_df(parser.parse_event("player_blind"))
    throw_dfs = {}
    for ev_name, _util in UTIL_THROW_EVENTS:
        throw_dfs[ev_name] = _as_df(parser.parse_event(ev_name))
    info = parser.parse_player_info()

    # Bomb events
    bomb_plant = parser.parse_event("bomb_planted")
    bomb_defuse = parser.parse_event("bomb_defused")
    bomb_explode = parser.parse_event("bomb_exploded")

    try:
        header = parser.parse_header()
        header_map = str(header.get("map_name", "") or "")
    except Exception:
        header_map = ""

    pro_sids = known_pro_steam_ids()  # steam_id -> canonical nick

    team_by_sid: dict[str, int] = {}
    name_by_sid: dict[str, str] = {}
    for _, row in info.iterrows():
        sid = _sid(row.get("steamid"))
        if not sid:
            continue
        team_by_sid[sid] = int(row.get("team_number", 0) or 0)
        name_by_sid[sid] = str(row.get("name", "") or "").strip()

    # Drop phantom tick-0 / duplicate warmup round_starts: keep one per round number (last seen).
    _rs_rows: list[tuple[int, int]] = []
    if not round_start.empty:
        for _, row in round_start.sort_values("tick").iterrows():
            _rs_rows.append((int(row["tick"]), int(row.get("round", 0) or 0)))
    _rs_by_round = _round_starts_from_events(_rs_rows)
    round_starts = [(t, rn) for rn, t in sorted(_rs_by_round.items())]

    first_freeze = None
    if not freeze_end.empty:
        first_freeze = int(freeze_end["tick"].min())

    # Add round 0 (warmup) start if we have first_freeze
    if first_freeze is not None:
        round_starts.insert(0, (first_freeze, 0))

    # Round freeze ends — per-live-round playable start signal.
    _fe_by_round: dict[int, int] = {}
    if not freeze_end.empty:
        for _, row in freeze_end.sort_values("tick").iterrows():
            tick = int(row["tick"])
            if first_freeze is not None and tick < first_freeze:
                continue
            rn = int(row.get("round", 0) or 0)
            if rn <= 0:
                rn = _round_for_tick(tick, round_starts, first_freeze)
            if rn > 0 and rn not in _fe_by_round:
                _fe_by_round[rn] = tick
    round_freeze_ends = [{"round": rn, "tick": t} for rn, t in sorted(_fe_by_round.items())]

    # Round ends — CS2 emits round_officially_ended for round N at the same
    # tick as round_start for round N+1. Bump those back by one round.
    _rs_ticks: set[int] = set()
    for st_tick, _ in round_starts:
        if first_freeze is None or st_tick >= first_freeze:
            _rs_ticks.add(st_tick)
    _re_by_round: dict[int, int] = {}
    if not round_end.empty:
        for _, row in round_end.sort_values("tick").iterrows():
            tick = int(row["tick"])
            if first_freeze is not None and tick < first_freeze:
                continue
            rn = _round_for_tick(tick, round_starts, first_freeze)
            if tick in _rs_ticks and rn > 0:
                rn -= 1
            if rn > 0 and rn not in _re_by_round:
                _re_by_round[rn] = tick
    round_ends = [{"round": rn, "tick": t} for rn, t in sorted(_re_by_round.items())]

    # --- Victim weapon lookup via tick-level active weapon snapshot ---
    import numpy as np

    _death_ticks_raw = sorted(set(
        int(r["tick"]) for _, r in deaths.iterrows()
        if first_freeze is None or int(r["tick"]) >= first_freeze
    ))
    # Bomb-action ticks join the snapshot so the actor's engine callout is
    # available at plant/defuse/explode ticks (CR-19: demoparser2's `site`
    # column on these events is a C4 entity handle, not a bombsite).
    import pandas as _pd
    _bomb_ticks_raw: set[int] = set()
    for _bdf in (bomb_plant, bomb_defuse, bomb_explode):
        if isinstance(_bdf, _pd.DataFrame) and not _bdf.empty:
            for _, _brow in _bdf.iterrows():
                _bt = int(_brow["tick"])
                if first_freeze is None or _bt >= first_freeze:
                    _bomb_ticks_raw.add(_bt)
    _weapon_query_ticks: list[int] = []
    for t in sorted(set(_death_ticks_raw) | _bomb_ticks_raw):
        _weapon_query_ticks.append(t)
        if t > 1:
            _weapon_query_ticks.append(t - 1)
        if t > 2:
            _weapon_query_ticks.append(t - 2)
    _weapon_snapshot = parser.parse_ticks(
        ["m_iItemDefinitionIndex", "last_place_name"], ticks=_weapon_query_ticks,
    )
    _victim_weapon_map: dict[tuple[int, str], int] = {}
    for _, row in _weapon_snapshot.iterrows():
        sid = _sid(row.get("steamid"))
        t = int(row["tick"])
        val = row.get("m_iItemDefinitionIndex")
        if sid and not (isinstance(val, float) and np.isnan(val)):
            key = (t, sid)
            if key not in _victim_weapon_map:
                _victim_weapon_map[key] = int(val)

    # Engine callouts at kill ticks (v3 zones). The column may be absent on
    # demos where the prop does not resolve — then every place reads "" and
    # place_source reports "unknown" (a zone-less timeline must be
    # distinguishable from a timeline whose place prop failed).
    _place_map: dict[tuple[int, str], str] = {}
    if "last_place_name" in list(getattr(_weapon_snapshot, "columns", [])):
        for _, row in _weapon_snapshot.iterrows():
            sid = _sid(row.get("steamid"))
            t = int(row["tick"])
            place = str(row.get("last_place_name") or "").strip()
            if (sid and place and place.lower() != "nan"
                    and (t, sid) not in _place_map):
                _place_map[(t, sid)] = place
    place_source = "ticks" if _place_map else "unknown"

    def _victim_weapon(death_tick: int, victim_sid: str) -> str:
        for offset in [death_tick, death_tick - 1, death_tick - 2]:
            key = (offset, victim_sid)
            if key in _victim_weapon_map:
                return resolve_weapon_id(_victim_weapon_map[key])
        return ""

    def _place_at(tick: int, sid: str) -> str:
        for offset in [tick, tick - 1, tick - 2]:
            key = (offset, sid)
            if key in _place_map:
                return _place_map[key]
        return ""

    def _fnum(v):
        try:
            f = float(v)
            return f if f == f else None
        except (TypeError, ValueError):
            return None

    def _pro_or_raw(sid: str, raw: str) -> str:
        return pro_sids.get(sid) or (raw or "")

    # --- Utility: throws, blinds, util damage ---
    util_throws: list[dict] = []
    for ev_name, util in UTIL_THROW_EVENTS:
        df = throw_dfs[ev_name]
        if df.empty:
            continue
        for _, row in df.sort_values("tick").iterrows():
            tick = int(row["tick"])
            if first_freeze is not None and tick < first_freeze:
                continue
            rn = _round_for_tick(tick, round_starts, first_freeze)
            if rn <= 0:
                continue
            sid = _sid(row.get("user_steamid"))
            util_throws.append({
                "tick": tick,
                "round": rn,
                "player": _pro_or_raw(sid, str(row.get("user_name", "") or "")),
                "player_steam_id": sid,
                "util": util,
                "x": _fnum(row.get("x")),
                "y": _fnum(row.get("y")),
            })
    util_throws.sort(key=lambda u: u["tick"])

    blinds: list[dict] = []
    if not blind.empty:
        for _, row in blind.sort_values("tick").iterrows():
            tick = int(row["tick"])
            if first_freeze is not None and tick < first_freeze:
                continue
            rn = _round_for_tick(tick, round_starts, first_freeze)
            if rn <= 0:
                continue
            vid = _sid(row.get("user_steamid"))
            if not vid:
                continue
            aid = _sid(row.get("attacker_steamid"))
            blinds.append({
                "tick": tick,
                "round": rn,
                "attacker": _pro_or_raw(aid, str(row.get("attacker_name", "") or "")),
                "attacker_steam_id": aid,
                "victim": _pro_or_raw(vid, str(row.get("user_name", "") or "")),
                "victim_steam_id": vid,
                "duration": _fnum(row.get("blind_duration")) or 0.0,
            })

    util_damages: list[dict] = []
    if not hurt.empty:
        for _, row in hurt.sort_values("tick").iterrows():
            w = str(row.get("weapon", "") or "").strip().lower()
            if w not in UTIL_DAMAGE_WEAPONS:
                continue
            dmg = _fnum(row.get("dmg_health")) or 0.0
            if dmg <= 0:
                continue
            tick = int(row["tick"])
            if first_freeze is not None and tick < first_freeze:
                continue
            rn = _round_for_tick(tick, round_starts, first_freeze)
            if rn <= 0:
                continue
            vid = _sid(row.get("user_steamid"))
            if not vid:
                continue
            aid = _sid(row.get("attacker_steamid"))
            util_damages.append({
                "tick": tick,
                "round": rn,
                "attacker": _pro_or_raw(aid, str(row.get("attacker_name", "") or "")),
                "attacker_steam_id": aid,
                "victim": _pro_or_raw(vid, str(row.get("user_name", "") or "")),
                "victim_steam_id": vid,
                "weapon": w,
                "dmg": dmg,
            })

    blinds_by_victim: dict[str, list[dict]] = {}
    for b in blinds:
        blinds_by_victim.setdefault(b["victim_steam_id"], []).append(b)
    hurts_by_victim: dict[str, list[dict]] = {}
    for d in util_damages:
        hurts_by_victim.setdefault(d["victim_steam_id"], []).append(d)

    # All hurt rows (gun damage too) for danger moments: pro dropped below
    # 30hp who survives. Kept separate from util_damages (timeline facts).
    all_hurts: list[dict] = []
    if not hurt.empty:
        for _, row in hurt.sort_values("tick").iterrows():
            tick = int(row["tick"])
            if first_freeze is not None and tick < first_freeze:
                continue
            rn = _round_for_tick(tick, round_starts, first_freeze)
            if rn <= 0:
                continue
            vid = _sid(row.get("user_steamid"))
            if not vid or vid not in pro_sids:
                continue
            hp = _fnum(row.get("health"))
            if hp is None:
                continue
            all_hurts.append({"tick": tick, "round": rn,
                              "victim_steam_id": vid, "health": hp})

    # --- Kills ---
    # ``kills`` is the legacy list (pro-involved only, byte-identical schema —
    # downstream consumers depend on it). ``kills_all`` keeps every legitimate
    # kill with pro flags; ``round_deaths`` tracks every death (incl. suicides
    # and teamkills) so per-round alive counts stay exact.
    kills: list[dict] = []
    kills_all: list[dict] = []
    round_deaths: dict[int, list[tuple[int, str]]] = {}
    for _, row in deaths.sort_values("tick").iterrows():
        tick = int(row["tick"])
        if first_freeze is not None and tick < first_freeze:
            continue  # warmup

        attacker_sid = _sid(row.get("attacker_steamid"))
        victim_sid = _sid(row.get("user_steamid"))
        weapon = str(row.get("weapon", "") or "").strip().lower()
        is_bomb = weapon in BOMB_WEAPONS

        if not victim_sid:
            continue
        round_no = _round_for_tick(tick, round_starts, first_freeze)
        if round_no <= 0:
            continue  # knife/warmup round — setup, not a real round
        round_deaths.setdefault(round_no, []).append((tick, victim_sid))
        if not attacker_sid and not is_bomb:
            continue  # world / suicide without attacker
        if attacker_sid and attacker_sid == victim_sid and not is_bomb:
            continue  # suicide
        if (
            not is_bomb
            and attacker_sid
            and attacker_sid in team_by_sid
            and victim_sid in team_by_sid
            and team_by_sid[attacker_sid] == team_by_sid[victim_sid]
            and team_by_sid[attacker_sid] > 0
        ):
            continue  # team kill (same side) — skip unless bomb

        attacker_is_pro = bool(attacker_sid and attacker_sid in pro_sids)
        victim_is_pro = bool(victim_sid and victim_sid in pro_sids)

        attacker_name = (
            pro_sids.get(attacker_sid)
            or canonical_nick(str(row.get("attacker_name", "") or ""))
            or name_by_sid.get(attacker_sid, "")
            or str(row.get("attacker_name", "") or "")
        )
        victim_name = (
            pro_sids.get(victim_sid)
            or canonical_nick(str(row.get("user_name", "") or ""))
            or name_by_sid.get(victim_sid, "")
            or str(row.get("user_name", "") or "")
        )

        victim_weapon = _victim_weapon(tick, victim_sid) if victim_sid else ""

        # Flash assist: latest blind on the victim still active at kill time.
        blinded_by = ""
        blind_tick = -1
        for b in blinds_by_victim.get(victim_sid, []):
            if b["tick"] <= tick <= b["tick"] + b["duration"] * 64:
                if b["tick"] >= blind_tick:
                    blinded_by = b["attacker_steam_id"]
                    blind_tick = b["tick"]
        # Util softening: util damage on the victim in the 5s before the kill.
        util_dmg_before = round(sum(
            d["dmg"] for d in hurts_by_victim.get(victim_sid, [])
            if tick - _TRADE_WINDOW_TICKS <= d["tick"] <= tick
        ), 1)

        legacy = {
            "tick": tick,
            "round": _round_for_tick(tick, round_starts, first_freeze),
            "attacker": attacker_name,
            "attacker_steam_id": attacker_sid,
            "victim": victim_name,
            "victim_steam_id": victim_sid,
            "weapon": weapon,
            "victim_weapon": victim_weapon,
            "is_bomb": is_bomb,
            "headshot": bool(row.get("headshot", False)),
        }
        record = {
            **legacy,
            "attacker_is_pro": attacker_is_pro,
            "victim_is_pro": victim_is_pro,
            "blinded_by": blinded_by,
            "util_dmg_before": util_dmg_before,
            "util_kill": weapon in UTIL_KILL_WEAPONS,
            "penetrated": _penetrated_count(row.get("penetrated")),
            "attacker_place": _place_at(tick, attacker_sid),
            "victim_place": _place_at(tick, victim_sid),
            # Engine kill-feed evidence is distinct from the inferred
            # blinded_by/duration fields above. Missing flags stay null.
            "kill_event": kill_event_details(row),
        }
        kills_all.append(record)
        if attacker_is_pro or victim_is_pro:
            kills.append(legacy)

    # --- Bomb events (by anyone — context for the round) ---
    # NOTE: demoparser2's `site` column on bomb events is a C4 entity handle
    # (e.g. "210"), not a bombsite — it is never read. The site comes from
    # the actor's engine callout at the action tick instead (CR-19).
    bomb_actions: list[dict] = []
    plant_site_by_round: dict[int, str] = {}
    import pandas as _pd
    for label, df in [("plant", bomb_plant), ("defuse", bomb_defuse), ("explode", bomb_explode)]:
        if not isinstance(df, _pd.DataFrame) or df.empty:
            continue
        for _, row in df.sort_values("tick").iterrows():
            tick = int(row["tick"])
            if first_freeze is not None and tick < first_freeze:
                continue
            user_sid = _sid(row.get("user_steamid"))
            rn = _round_for_tick(tick, round_starts, first_freeze)
            site = _bomb_action_site(label, rn, tick, user_sid,
                                     _place_at, plant_site_by_round)
            if label == "plant" and site and rn not in plant_site_by_round:
                plant_site_by_round[rn] = site
            bomb_actions.append({
                "tick": tick,
                "round": rn,
                "type": label,
                "player": str(row.get("user_name", "") or ""),
                "player_steam_id": user_sid,
                "site": site,
            })

    # --- Winner per round ---
    # Compute which team won each round from kills + bomb events + team assignments.
    winner_by_round: dict[int, int] = {}
    # Gather all round numbers that have data
    kill_rounds = {k["round"] for k in kills}
    bomb_rounds = {b["round"] for b in bomb_actions}
    for rn in sorted(kill_rounds | bomb_rounds):
        _rkills = sorted([k for k in kills if k["round"] == rn], key=lambda k: k["tick"])
        _rbombs = [b for b in bomb_actions if b["round"] == rn]

        # Bomb win
        for b in _rbombs:
            if b["type"] == "explode":
                sid = b["player_steam_id"]
                if sid in team_by_sid:
                    winner_by_round[rn] = team_by_sid[sid]
            elif b["type"] == "defuse":
                sid = b["player_steam_id"]
                if sid in team_by_sid:
                    winner_by_round[rn] = team_by_sid[sid]

        # If no bomb win, winner = team of last surviving killer
        if rn not in winner_by_round and _rkills:
            dead: set[str] = set()
            for k in _rkills:
                if k["victim_steam_id"]:
                    dead.add(k["victim_steam_id"])
            # Last killer not in dead → their team wins
            for k in reversed(_rkills):
                aid = k["attacker_steam_id"]
                if aid and aid not in dead and aid in team_by_sid:
                    winner_by_round[rn] = team_by_sid[aid]
                    break

    # --- Authoritative winners (round_end side + per-round side snapshots) ---
    # Overrides the heuristic wherever round_end data exists; the heuristic
    # stays as fallback for demos/rounds missing it.
    side_map = _side_map_by_round(parser, round_starts, team_by_sid)
    auth_winners, win_reason_by_round = _authoritative_winners(
        round_end_winner, round_starts, side_map)
    for rn, team in auth_winners.items():
        winner_by_round[rn] = team

    # --- Rounds (structure + score + alive progression) ---
    rs_tick_by_round = {rn: t for t, rn in round_starts if rn > 0}
    fe_by_round = {d["round"]: d["tick"] for d in round_freeze_ends}
    re_by_round = {d["round"]: d["tick"] for d in round_ends}
    # Final round often has no round_end event — fall back to its last
    # kill/bomb tick so end_tick is never None on a played round.
    for rn in rs_tick_by_round:
        if rn not in re_by_round:
            tails = [k["tick"] for k in kills_all if k["round"] == rn]
            tails += [b["tick"] for b in bomb_actions if b["round"] == rn]
            if tails:
                re_by_round[rn] = max(tails)
    team_ids = sorted({t for t in team_by_sid.values() if t and t >= 2})
    # --- Economy snapshot (v3 stakes): post-freeze equip per round ---
    equip_by_sid, _equip_resolved = _economy_by_round(parser, fe_by_round)
    equip_by_round: dict[int, dict[int, dict]] = {}
    if _equip_resolved:
        for rn, per_sid in equip_by_sid.items():
            agg: dict[int, dict] = {}
            for sid, val in per_sid.items():
                t = team_by_sid.get(sid, 0)
                if not t or t < 2:
                    continue
                cell = agg.setdefault(t, {"total": 0, "players": 0})
                cell["total"] += int(val)
                cell["players"] += 1
            equip_by_round[rn] = agg
    score: dict[int, int] = {t: 0 for t in team_ids}
    rounds: list[dict] = []
    live_rounds = sorted(rs_tick_by_round)
    for rn in live_rounds:
        alive = {t: 5 for t in team_ids}
        progression = [{"tick": rs_tick_by_round[rn],
                        "teams": dict(alive)}]
        for tick, victim in sorted(round_deaths.get(rn, [])):
            vt = team_by_sid.get(victim, 0)
            if vt in alive:
                alive[vt] = max(0, alive[vt] - 1)
            progression.append({"tick": tick, "teams": dict(alive)})
        w = winner_by_round.get(rn)
        # Match point (MR12 regulation: first to 13): a team on 12 wins
        # before round 25 can close. OT formats vary — OT rounds are only
        # tagged overtime:true, plus the actual closer on the final round.
        match_point_for = next(
            (t for t in team_ids if score.get(t) == 12 and rn <= 24), None)
        if w in score:
            score[w] += 1
        _round_places = [k.get("attacker_place", "") for k in kills_all
                         if k["round"] == rn and k.get("attacker_place")]
        hot_zones = [p for p, _ in Counter(_round_places).most_common(3)]
        rounds.append({
            "round": rn,
            "start_tick": rs_tick_by_round[rn],
            "freeze_end_tick": fe_by_round.get(rn),
            "end_tick": re_by_round.get(rn),
            "winner_team": w,
            "win_reason": win_reason_by_round.get(rn, ""),
            "score_after": {str(t): score[t] for t in team_ids},
            "match_point_for": match_point_for,
            "overtime": rn > 24,
            "closes_map": w if rn == live_rounds[-1] else None,
            "alive": progression,
            "hot_zones": hot_zones,
        })

    # --- Stakes + arc (v3, pure — streaks/arc always computed, buys degrade
    # to unknown without tick data) ---
    stakes_by_round, arc = _derive_stakes(rounds, equip_by_round, team_ids)
    for r in rounds:
        r["stakes"] = stakes_by_round[r["round"]]

    # --- Moments (deterministic, multi-POV edit input) ---
    moments = _derive_moments(
        kills_all, round_deaths, rs_tick_by_round, re_by_round,
        winner_by_round, win_reason_by_round, bomb_actions,
        team_by_sid, pro_sids, util_throws=util_throws,
        all_hurts=all_hurts)

    # --- Per-pro ledger (v3, pure post-processing) ---
    pros = _derive_pro_ledger(kills_all, round_deaths, moments,
                              live_rounds, pro_sids,
                              team_by_sid, winner_by_round)

    try:
        demo_rel = str(demo_path.resolve().relative_to(PROJECT_ROOT)).replace("\\", "/")
    except ValueError:
        demo_rel = str(demo_path)

    source = "hltv" if "demos/hltv" in demo_rel else "faceit"

    # Team membership {team_number_str: [steam_id, ...]} — used by the edit
    # timeline to pick a closer POV from the round's winning team. Only real
    # teams (2 = T, 3 = CT); 1 = unassigned/spectators.
    teams: dict[str, list[str]] = {}
    for sid, team_no in team_by_sid.items():
        if team_no > 1:
            teams.setdefault(str(team_no), []).append(sid)
    for team_no in teams:
        teams[team_no].sort()

    return {
        "timeline_version": TIMELINE_VERSION,
        "event_data_version": EVENT_DATA_VERSION,
        # Preserve already-parsed source data before gameplay/pro filters.
        # player_hurt includes gun damage too, not only util_damages.
        "raw_events": {
            "player_death": raw_event_table(deaths),
            "player_hurt": raw_event_table(hurt),
            "weapon_fire": raw_event_table(weapon_fire),
        },
        "raw_player_info": raw_event_table(info),
        "stakes_version": STAKES_VERSION,
        "place_source": place_source,
        "demo_path": demo_rel,
        "map": _map_name(demo_path, header_map),
        "source": source,
        "kill_count": len(kills),
        "kills": kills,
        "kills_all": kills_all,
        "pro_kill_count": sum(1 for k in kills_all
                              if k["attacker_is_pro"] or k["victim_is_pro"]),
        "bomb_actions": bomb_actions,
        "round_starts": [{"round": rn, "tick": t} for t, rn in round_starts],
        "round_freeze_ends": round_freeze_ends,
        "round_ends": round_ends,
        "winner_by_round": {str(rn): t for rn, t in winner_by_round.items()},
        "win_reason_by_round": {str(rn): r for rn, r in win_reason_by_round.items()},
        "sides_by_round": {
            str(rn): side_map.get(rn, {})
            for rn in sorted(rs_tick_by_round)
        },
        "teams": teams,
        "rounds": rounds,
        "moments": moments,
        "arc": arc,
        "pros": pros,
        "util_throws": util_throws,
        "blinds": blinds,
        "util_damages": util_damages,
    }


def highlights_run_dir(demo_path: Path) -> Path:
    return PROJECT_ROOT / "renders" / f"hl-{demo_path.stem}"


def ensure_action_timeline(demo_path: Path,
                           output: Path | None = None) -> Path | None:
    """Build + cache the action timeline for a demo, or reuse the cache.

    Default home is ``renders/hl-{stem}/action_timeline.json`` (the
    multi-pros highlights pipeline). Pass ``output`` for a POV-local copy —
    the POV flow keeps its cache inside the pov folder and never creates
    hl-* dirs. Rebuilds when the cached ``timeline_version`` is older than
    ``TIMELINE_VERSION`` or it predates the raw engine-event schema. Failures
    return None — callers must treat that as
    "no timeline", never as an error (backlog cards and hooks must still
    land).
    """
    from cs2archive.shorts.event_data import EVENT_DATA_VERSION
    demo_path = Path(demo_path)
    out = Path(output) if output else highlights_run_dir(demo_path) / "action_timeline.json"
    if out.is_file():
        try:
            cached = json.loads(out.read_text(encoding="utf-8"))
            if (int(cached.get("timeline_version") or 0) >= TIMELINE_VERSION
                    and int(cached.get("event_data_version") or 0) >= EVENT_DATA_VERSION):
                return out
        except Exception:
            pass
    try:
        timeline = build_action_timeline(demo_path)
    except Exception as e:  # noqa: BLE001
        print(f"  [WARN] action timeline failed for {demo_path.name} ({e})")
        return None
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(timeline, indent=2), encoding="utf-8")
    except OSError as e:
        print(f"  [WARN] action timeline cache write failed ({e})")
        return None
    return out


def pov_action_slice(timeline: dict, steam_id: str) -> dict:
    """POV-player view of an action timeline (pure; no demo access).

    Returns the POV player's kills (from ``kills_all`` — victim weapons and
    engine callouts intact), deaths, and the moments they are involved in
    under a non-victim role, plus the shared rounds/stakes tables the
    thumbnail tiebreak needs. Empty involvement reads as empty lists,
    never as an error.
    """
    want = str(steam_id or "")
    kills_all = timeline.get("kills_all") or []
    moments = timeline.get("moments") or []
    kills = [k for k in kills_all if str(k.get("attacker_steam_id") or "") == want]
    deaths = [k for k in kills_all if str(k.get("victim_steam_id") or "") == want]

    involved: list[dict] = []
    for m in moments:
        if str(m.get("attacker_steam_id") or "") == want:
            involved.append(m)
            continue
        if str(m.get("clutcher_steam_id") or "") == want:
            involved.append(m)
            continue
        roles = {str(c.get("steam_id") or ""): str(c.get("role") or "")
                 for c in (m.get("pov_candidates") or [])}
        if roles.get(want, "victim") != "victim" and want in roles:
            involved.append(m)
    involved.sort(key=lambda m: (m.get("start_tick", 0), m.get("id", "")))
    return {
        "steam_id": want,
        "map": timeline.get("map", "Unknown"),
        "tickrate": int(timeline.get("tickrate") or 64),
        "kills": kills,
        "deaths": deaths,
        "moments": involved,
        "rounds": timeline.get("rounds") or [],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Build Action Timeline JSON (HLTV or FACEIT)")
    ap.add_argument("demo_path", type=Path, help="Path to .dem file")
    ap.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Override output JSON path (default: renders/hl-{stem}/action_timeline.json)",
    )
    args = ap.parse_args()

    demo = args.demo_path
    if not demo.is_file():
        print(f"[ERR] demo not found: {demo}", file=sys.stderr)
        return 1

    timeline = build_action_timeline(demo)
    out = args.output or (highlights_run_dir(demo) / "action_timeline.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(timeline, indent=2), encoding="utf-8")
    if timeline.get("place_source") == "unknown":
        print("[WARN] last_place_name unresolved — zones empty", file=sys.stderr)
    print(f"[OK] {timeline['kill_count']} kills, {len(timeline['bomb_actions'])} bomb events -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
