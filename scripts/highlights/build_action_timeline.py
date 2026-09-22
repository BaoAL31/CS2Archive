"""Build an Action Timeline from a demo (HLTV or FACEIT, data only).

Parses demoparser2 events — kills, bomb events, utility (throws, blinds,
util damage), round lifecycle, round-end winners — and writes:

    renders/hl-{demo_stem}/action_timeline.json

Timeline v2 (``timeline_version: 2``) is match-level and multi-POV ready:

- ``kills`` — unchanged legacy list (kills involving a Recognised Pro).
- ``kills_all`` — every legitimate kill (suicides/teamkills/world still
  excluded) with ``attacker_is_pro`` / ``victim_is_pro`` flags.
- ``rounds[]`` — per-round start/freeze/end ticks, authoritative winner
  (``round_end`` side mapped through per-round side snapshots, heuristic
  fallback), win reason, running score, and alive-count progression.
- ``moments[]`` — deterministically derived multikills, clutches (+ lost
  attempts), openers, trades, and bomb plays, each with a tick window and
  ranked ``pov_candidates``. This is the edit layer's input: the goal is to
  make LLM moment-picking unnecessary (or trivially easy).

Usage:
    python scripts/highlights/build_action_timeline.py <demo_path>
    python scripts/highlights/build_action_timeline.py demos/faceit/some-match.dem
    python scripts/highlights/build_action_timeline.py demos/hltv/some-match/some-map.dem
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _pathsetup import ensure

PROJECT_ROOT = ensure()

from faceit_names import canonical_nick, known_pro_steam_ids  # noqa: E402

BOMB_WEAPONS = frozenset({"c4", "planted_c4"})

_CS2_WEAPON_IDS: dict[int, str] = {
    # Pistols
    1: "Desert Eagle", 2: "Dual Berettas", 3: "Five-SeveN", 4: "Glock-18",
    30: "Tec-9", 32: "P2000", 36: "P250", 61: "USP-S",
    63: "CZ75-Auto", 64: "R8 Revolver",
    # Rifles
    7: "AK-47", 8: "AUG", 10: "FAMAS", 13: "Galil AR",
    16: "M4A4", 39: "SG 553", 60: "M4A1-S",
    # Snipers
    9: "AWP", 11: "G3SG1", 38: "SCAR-20", 40: "SSG 08",
    # SMGs
    17: "MAC-10", 19: "P90", 23: "MP5-SD", 24: "UMP-45",
    26: "PP-Bizon", 33: "MP7", 34: "MP9",
    # Heavy
    14: "M249", 25: "XM1014", 27: "MAG-7", 28: "Negev",
    29: "Sawed-Off", 35: "Nova",
    # Equipment
    31: "Zeus x27", 42: "Knife", 49: "C4",
    50: "Kevlar Vest", 51: "Kevlar + Helmet", 52: "Defuse Kit",
    54: "Rescue Kit", 55: "Medi-Shot", 57: "Healthshot", 59: "Knife",
    80: "Shield",
    # Grenades
    43: "Flashbang", 44: "HE Grenade", 45: "Smoke Grenade",
    46: "Molotov", 47: "Decoy Grenade", 48: "Incendiary",
    68: "TA Grenade", 81: "Frag Grenade",
    # Knives
    500: "Bayonet", 503: "Karambit", 505: "Flip Knife",
    506: "Gut Knife", 507: "M9 Bayonet", 508: "Huntsman Knife",
    509: "Falchion Knife", 512: "Bowie Knife", 514: "Butterfly Knife",
    515: "Shadow Daggers", 516: "Paracord Knife", 517: "Survival Knife",
    518: "Ursus Knife", 519: "Navaja Knife", 520: "Nomad Knife",
    521: "Stiletto Knife", 522: "Talon Knife", 523: "Classic Knife",
    525: "Skeleton Knife",
    # Danger Zone tools
    85: "Tablet", 86: "Axe", 87: "Hammer", 88: "Wrench", 89: "Spanner",
}


def _resolve_weapon_id(def_idx: int) -> str:
    return _CS2_WEAPON_IDS.get(def_idx, f"item_{def_idx}")


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


TIMELINE_VERSION = 2

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
        from shorts.build_short_timeline import (
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
        rs_ticks = sorted({t for t, rn in round_starts if rn > 0 and t > 1})
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

    moments.sort(key=lambda m: (m["start_tick"], m["id"]))
    return moments


def build_action_timeline(demo_path: Path) -> dict:
    import demoparser2 as dp

    parser = dp.DemoParser(str(demo_path))

    # Core events
    deaths = parser.parse_event("player_death")
    round_start = parser.parse_event("round_start")
    freeze_end = parser.parse_event("round_freeze_end")
    round_end = parser.parse_event("round_officially_ended")
    round_end_winner = parser.parse_event("round_end")
    hurt = _as_df(parser.parse_event("player_hurt"))
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
    _rs_by_round: dict[int, tuple[int, int]] = {}
    if not round_start.empty:
        for _, row in round_start.sort_values("tick").iterrows():
            t = int(row["tick"])
            rn = int(row.get("round", 0) or 0)
            if t <= 1:
                continue
            if rn <= 0:
                rn = len(_rs_by_round) + 1
            _rs_by_round[rn] = (t, rn)
    round_starts = [v for _, v in sorted(_rs_by_round.items())]

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
    _weapon_query_ticks: list[int] = []
    for t in _death_ticks_raw:
        _weapon_query_ticks.append(t)
        if t > 1:
            _weapon_query_ticks.append(t - 1)
    _weapon_snapshot = parser.parse_ticks(
        ["m_iItemDefinitionIndex"], ticks=_weapon_query_ticks,
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

    def _victim_weapon(death_tick: int, victim_sid: str) -> str:
        for offset in [death_tick, death_tick - 1, death_tick - 2]:
            key = (offset, victim_sid)
            if key in _victim_weapon_map:
                return _resolve_weapon_id(_victim_weapon_map[key])
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
        }
        kills_all.append(record)
        if attacker_is_pro or victim_is_pro:
            kills.append(legacy)

    # --- Bomb events (by anyone — context for the round) ---
    bomb_actions: list[dict] = []
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
            bomb_actions.append({
                "tick": tick,
                "round": rn,
                "type": label,
                "player": str(row.get("user_name", "") or ""),
                "player_steam_id": user_sid,
                "site": str(row.get("site", "") or ""),
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
        })

    # --- Moments (deterministic, multi-POV edit input) ---
    moments = _derive_moments(
        kills_all, round_deaths, rs_tick_by_round, re_by_round,
        winner_by_round, win_reason_by_round, bomb_actions,
        team_by_sid, pro_sids, util_throws=util_throws,
        all_hurts=all_hurts)

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
        "util_throws": util_throws,
        "blinds": blinds,
        "util_damages": util_damages,
    }


def highlights_run_dir(demo_path: Path) -> Path:
    return PROJECT_ROOT / "renders" / f"hl-{demo_path.stem}"


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
    print(f"[OK] {timeline['kill_count']} kills, {len(timeline['bomb_actions'])} bomb events -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
