"""Build a Short Timeline from any CS2 demo (HLTV or FACEIT).

Detects three Short types:
  - **4K** : 4+ kills by same attacker in a single round (incl. 5-kill aces).
  - **Clutch** : team wins from 2v4 or worse (1v3, 1v4, 1v5, 2v4, 2v5).
  - **1v3** : exactly 3 kills in a single round while at a 1v3-or-worse numbers
    disadvantage, where at least 2 of those kills "punch up" (killed with a
    lower-tier weapon than the victim held). Unlike 4K, the round does NOT need
    to be won.

By default only shorts whose POV player is a Recognised Pro
(``.data/player_accounts.json``) are kept — randos are dropped
(``--include-all-players`` opts out).

Two input modes:
  1. **Direct demo parse** (default): parses the full demo via demoparser2.
  2. **From Action Timeline** (``--from-action-timeline``): reads an existing
     ``action_timeline.json`` (Recognised Pro-gated, FACEIT-only), extracts
     kill events + team assignments, and runs the same 4K/Clutch/1v3 detection.
     This reuses the highlights pipeline's Recognised Pro filtering without
     re-parsing the demo.

Usage:
    python scripts/shorts/build_short_timeline.py <demo_path> [--player <steam_id>]
    python scripts/shorts/build_short_timeline.py <demo_path> --from-action-timeline renders/hl-<stem>/action_timeline.json [--player <steam_id>]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from _pathsetup import ensure

ensure()

from shorts import resolve_output_dir  # noqa: E402
from faceit_names import known_pro_steam_ids  # noqa: E402

_PRE_KILL_TICK_MARGIN = 320  # 5s floor before first kill (at 64 tick)
_POST_KILL_TICK_MARGIN = 128  # 2s after last kill (at 64 tick)
_SHORT_TICK_DURATION = 1280  # 20s target total short length (20 * 64 tick)
_CLUTCH_MIN_DURATION_TICKS = 640  # 10s of playing at a disadvantage for a clutch


def _as_event_df(result):
    """Normalize a demoparser2 parse_event result to a pandas DataFrame.

    Some demoparser2 versions return a plain empty ``list`` for events with no
    rows (e.g. ``bomb_defused`` when nobody defused), but downstream code
    (``detect_shorts``) calls DataFrame APIs (``.empty``, ``sort_values``,
    ``iterrows``, ``["tick"]``). Coerce any non-DataFrame result to an empty
    DataFrame so the pipeline is robust to empty events.
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


# Weapon tiers for multikill filtering: at least two victims must hold a
# weapon of tier >= attacker's primary tier (filters anti-eco farm multikills).
# Handles both player_death short names (e.g. "m4a1") and display names (e.g. "M4A4").
_WEAPON_TIER: dict[str, int] = {
    # Tier 0: Melee / eco items
    "knife": 0, "world": 0, "zeus": 0, "zeus x27": 0,
    "c4": 0, "planted_c4": 0,
    "bayonet": 0, "karambit": 0, "flip knife": 0, "gut knife": 0,
    "m9 bayonet": 0, "huntsman knife": 0, "falchion knife": 0,
    "bowie knife": 0, "butterfly knife": 0, "shadow daggers": 0,
    "paracord knife": 0, "survival knife": 0, "ursus knife": 0,
    "navaja knife": 0, "nomad knife": 0, "stiletto knife": 0,
    "talon knife": 0, "classic knife": 0, "skeleton knife": 0,
    "axe": 0, "hammer": 0, "wrench": 0, "spanner": 0, "tablet": 0,
    # Tier 1: Pistols
    "glock": 1, "glock-18": 1, "usp_silencer": 1, "usp-s": 1,
    "p2000": 1, "p250": 1, "deagle": 1, "desert eagle": 1,
    "elite": 1, "dual berettas": 1, "fiveseven": 1, "five-seveN": 1,
    "tec9": 1, "tec-9": 1, "cz75": 1, "cz75-auto": 1,
    "r8revolver": 1, "r8 revolver": 1,
    # Tier 2: Shotguns
    "nova": 2, "xm1014": 2, "mag7": 2, "mag-7": 2,
    "sawedoff": 2, "sawed-off": 2,
    # Tier 3: SMGs
    "mac10": 3, "mac-10": 3, "mp5sd": 3, "mp5-sd": 3,
    "mp7": 3, "mp9": 3, "p90": 3, "ump45": 3, "ump-45": 3,
    "bizon": 3, "pp-bizon": 3, "m249": 3, "negev": 3,
    # Tier 4: Rifles & Snipers
    "ak47": 4, "ak-47": 4, "m4a4": 4, "m4a1": 4,
    "m4a1_silencer": 4, "m4a1-s": 4, "famas": 4, "galilar": 4,
    "galil ar": 4, "aug": 4, "sg553": 4, "sg 553": 4,
    "awp": 4, "g3sg1": 4, "scar20": 4, "scar-20": 4,
    "ssg08": 4, "ssg 08": 4,
    # Tier 5: Special (always count — grenades, fire, bomb)
    "inferno": 5, "hegrenade": 5, "he grenade": 5,
    "flashbang": 5, "smokegrenade": 5, "smoke grenade": 5,
    "decoy": 5, "decoy grenade": 5, "incendiary": 5,
    "molotov": 5, "tagrenade": 5, "ta grenade": 5,
    "fraggrenade": 5, "frag grenade": 5,
}


def _weapon_tier(weapon: str) -> int:
    return _WEAPON_TIER.get(weapon.strip().lower(), -1)


def _meets_tier_criterion(kills: list[dict]) -> bool:
    """At least two victims held a weapon of tier >= attacker's primary weapon tier.

    Primary weapon = most common weapon across all kills (tie → highest tier).
    Tier 5 (inferno, nades, etc.) always passes.
    """
    if not kills:
        return False
    from collections import Counter
    counts = Counter(k.get("weapon", "") for k in kills)
    if not counts:
        return False
    primary = counts.most_common(1)[0][0]
    attacker_tier = _weapon_tier(primary)
    if attacker_tier >= 5:
        return True
    eligible = 0
    for k in kills:
        vw = k.get("victim_weapon", "")
        if vw and _weapon_tier(vw) >= attacker_tier:
            eligible += 1
    return eligible >= 2


def _is_punch_up(k: dict) -> bool:
    """True when the kill is "punching up": attacker used a LOWER-tier weapon
    than the victim held (e.g. a pistol/knife taking out a rifle).

    A lower tier beats a higher tier — the bigger the gap the more impressive.
    Unknown weapon tiers (-1) never count as punch-up (can't verify).
    """
    at = _weapon_tier(str(k.get("weapon", "") or ""))
    vt = _weapon_tier(str(k.get("victim_weapon", "") or ""))
    if vt < 0 or at < 0:
        return False
    return at < vt


def _punch_up_tags(kills: list[dict]) -> list[str]:
    """Order-preserving list of "<gun>_punch_up" tags, only when at least
    two kills in the multikill punched up with that weapon. Single glock kill
    in a 4k (e.g. 3 rifle + 1 pistol) is not a punch-up 4k — label would be
    misleading. Drives the short folder-name suffix.
    """
    tags: list[str] = []
    for k in kills:
        if not _is_punch_up(k):
            continue
        w = str(k.get("weapon", "") or "").strip().lower()
        if w:
            tags.append(f"{w}_punch_up")
    from collections import Counter
    counts = Counter(tags)
    seen: set[str] = set()
    out: list[str] = []
    for t in tags:
        if t not in seen and counts[t] >= 2:
            seen.add(t)
            out.append(t)
    return out


def _sid(val) -> str:
    import math

    if val is None:
        return ""
    if isinstance(val, float) and math.isnan(val):
        return ""
    s = str(val).strip()
    if not s or s.lower() == "nan":
        return ""
    return s


def _build_nickname_map(info) -> dict[str, str]:
    nid: dict[str, str] = {}
    for _, row in info.iterrows():
        sid = _sid(row.get("steamid"))
        if not sid:
            continue
        name = str(row.get("name", "") or "").strip()
        if name:
            nid[sid] = name
    return nid


def build_short_timeline(demo_path: Path, player: str | None = None,
                         pros_only: bool = True) -> dict:
    """Parse demo via demoparser2 and extract 4K/Clutch Shorts."""
    import demoparser2 as dp

    parser = dp.DemoParser(str(demo_path))

    deaths = _as_event_df(parser.parse_event("player_death"))
    round_start = _as_event_df(parser.parse_event("round_start"))
    freeze_end = _as_event_df(parser.parse_event("round_freeze_end"))
    round_end = _as_event_df(parser.parse_event("round_officially_ended"))
    round_end_winner = _as_event_df(parser.parse_event("round_end"))
    info = parser.parse_player_info()
    bomb_plant = _as_event_df(parser.parse_event("bomb_planted"))
    bomb_defuse = _as_event_df(parser.parse_event("bomb_defused"))
    bomb_explode = _as_event_df(parser.parse_event("bomb_exploded"))

    try:
        header = parser.parse_header()
        header_map = str(header.get("map_name", "") or "")
    except Exception:
        header_map = ""

    nickname_by_sid = _build_nickname_map(info)

    # --- Victim weapon lookup via tick-level active weapon snapshot ---
    victim_weapon_map: dict[tuple[int, str], str] = {}
    try:
        import numpy as np
        from highlights.build_action_timeline import _resolve_weapon_id

        first_freeze = int(freeze_end["tick"].min()) if freeze_end is not None and not freeze_end.empty else None
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
        for _, row in _weapon_snapshot.iterrows():
            sid = _sid(row.get("steamid"))
            t = int(row["tick"])
            val = row.get("m_iItemDefinitionIndex")
            if sid and not (isinstance(val, float) and np.isnan(val)):
                key = (t, sid)
                if key not in victim_weapon_map:
                    victim_weapon_map[key] = _resolve_weapon_id(int(val))
    except Exception:
        victim_weapon_map = {}

    return detect_shorts(
        demo_path=str(demo_path),
        header_map=header_map,
        deaths=deaths,
        round_start=round_start,
        freeze_end=freeze_end,
        round_end=round_end,
        info=info,
        bomb_plant=bomb_plant,
        bomb_defuse=bomb_defuse,
        bomb_explode=bomb_explode,
        victim_weapon_map=victim_weapon_map,
        winner_by_round=_winner_by_round_from_demo(parser, info, round_start, round_end_winner),
        pros_only=pros_only,
    )


def _winner_by_round_from_demo(
    parser,
    info,
    round_start,
    round_end_winner,
) -> dict[int, int]:
    """Authoritative per-round winner (persistent team number).

    ``round_end.winner`` carries the winning *side* ('T'/'CT'), which flips at
    halftime, while ``parse_player_info.team_number`` is the persistent team
    (stable across halves). Sample the side of every player at each round start
    to map side -> persistent team per round, then convert each round_end winner.
    """
    winner_by_round: dict[int, int] = {}
    if round_end_winner is None or round_end_winner.empty:
        return winner_by_round
    if round_start is None or round_start.empty or info is None or info.empty:
        return winner_by_round

    persist_team: dict[str, int] = {}
    for _, row in info.iterrows():
        sid = _sid(row.get("steamid"))
        if sid:
            persist_team[sid] = int(row.get("team_number", 0) or 0)

    rs_ticks = sorted({int(t) for t in round_start["tick"].tolist() if int(t) > 1})
    if not rs_ticks:
        return winner_by_round
    try:
        side_snap = parser.parse_ticks(["steamid", "team_num"], ticks=rs_ticks)
    except Exception:
        return winner_by_round

    side_to_team: dict[int, dict[int, int]] = {}
    for _, row in side_snap.iterrows():
        sid = _sid(row.get("steamid"))
        tick = int(row["tick"])
        side = int(row["team_num"]) if row.get("team_num") == row.get("team_num") else 0
        if sid and sid in persist_team and side in (2, 3):
            side_to_team.setdefault(tick, {})[side] = persist_team[sid]

    # round_start.round -> its start tick. This is the round-numbering used
    # everywhere downstream (kills, clutch triggers, _round_for_tick).
    rs_by_round: dict[int, int] = {}
    for t, rn in zip(round_start["tick"].tolist(), round_start["round"].tolist()):
        rn_i = int(rn or 0)
        t_i = int(t)
        if rn_i > 0 and t_i > 1:
            rs_by_round[rn_i] = t_i
    # Sorted (start_tick, round_num) so a round_end tick can be mapped back to
    # its round via round_start numbering. We cannot trust round_end.round:
    # in many FACEIT demos it is +1 shifted from round_start.round (e.g.
    # round_end.round == 8 fires for the round that round_start.round == 7
    # started). Deriving the round by tick keeps winner_by_round keyed the
    # same way as every other per-round structure.
    _rs_sorted = sorted((t, rn) for rn, t in rs_by_round.items())

    def _round_for_tick(tick: int) -> int:
        rn = 0
        for st_tick, rn_candidate in _rs_sorted:
            if st_tick <= tick:
                rn = rn_candidate
            else:
                break
        return rn

    for _, row in round_end_winner.iterrows():
        side = str(row.get("winner", "") or "").strip().upper()
        if side not in ("T", "CT"):
            continue
        rn = _round_for_tick(int(row["tick"]))
        if rn <= 0:
            continue
        rs_tick = rs_by_round.get(rn)
        if rs_tick is None:
            continue
        slot = 2 if side == "T" else 3
        team = side_to_team.get(rs_tick, {}).get(slot)
        if team:
            winner_by_round[rn] = team
    return winner_by_round


def detect_shorts(
    *,
    demo_path: str,
    header_map: str = "",
    deaths: "pd.DataFrame | None" = None,
    round_start: "pd.DataFrame | None" = None,
    freeze_end: "pd.DataFrame | None" = None,
    round_end: "pd.DataFrame | None" = None,
    round_end_winner: "pd.DataFrame | None" = None,
    info: "pd.DataFrame | None" = None,
    bomb_plant: "pd.DataFrame | None" = None,
    bomb_defuse: "pd.DataFrame | None" = None,
    bomb_explode: "pd.DataFrame | None" = None,
    team_by_sid: dict[str, int] | None = None,
    nickname_by_sid: dict[str, str] | None = None,
    winner_by_round: dict[int, int] | None = None,
    kill_events: list[dict] | None = None,
    round_starts: list[tuple[int, int]] | None = None,
    first_freeze: int | None = None,
    round_freeze_ends: dict[int, int] | None = None,
    round_ends: dict[int, int] | None = None,
    round_win_events: dict[int, list[dict]] | None = None,
    victim_weapon_map: dict[tuple[int, str], str] | None = None,
    pros_only: bool = True,
) -> dict:
    """Detect 4K and Clutch Shorts from parsed or synthetic events.

    Accepts either pandas DataFrames (from demoparser2) or plain Python
    dicts/lists for easy unit testing without a real demo file.

    ``pros_only=True`` keeps only shorts whose POV player is a Recognised Pro
    (``.data/player_accounts.json``) and rewrites their ``pov_nick`` to the
    canonical nickname (e.g. "donk666" -> "donk").
    """
    import pandas as pd

    _pro_sids: dict[str, str] = {}
    if pros_only:
        try:
            _pro_sids = known_pro_steam_ids()
        except Exception:
            _pro_sids = {}
        if not _pro_sids:
            print("[WARN] pros_only is on but .data/player_accounts.json is empty/missing "
                  "— no shorts will be kept. Use --include-all-players to override.",
                  file=sys.stderr)

    # --- Team + name lookup from player_info ---
    _tid: dict[str, int] = {}
    _nid: dict[str, str] = {}
    if team_by_sid is not None:
        _tid = dict(team_by_sid)
    elif info is not None and not info.empty:
        for _, row in info.iterrows():
            sid = _sid(row.get("steamid"))
            if not sid:
                continue
            _tid[sid] = int(row.get("team_number", 0) or 0)
            name = str(row.get("name", "") or "").strip()
            if name:
                _nid[sid] = name
    team_by_sid = _tid
    if nickname_by_sid is None:
        nickname_by_sid = _nid
    else:
        nickname_by_sid = dict(nickname_by_sid)
        # fill missing from info
        for sid, name in _nid.items():
            nickname_by_sid.setdefault(sid, name)

    # --- Round starts ---
    if round_starts is not None:
        _rstarts = list(round_starts)
    elif round_start is not None and not round_start.empty:
        _rs_by_round: dict[int, tuple[int, int]] = {}
        for _, row in round_start.sort_values("tick").iterrows():
            t = int(row["tick"])
            rn = int(row.get("round", 0) or 0)
            if t <= 1:
                continue
            if rn <= 0:
                rn = len(_rs_by_round) + 1
            _rs_by_round[rn] = (t, rn)
        _rstarts = [v for _, v in sorted(_rs_by_round.items())]
    else:
        _rstarts = []
    round_starts = _rstarts

    _ff = first_freeze
    if _ff is None and freeze_end is not None and not freeze_end.empty:
        _ff = int(freeze_end["tick"].min())
    first_freeze = _ff

    if first_freeze is not None:
        round_starts.insert(0, (first_freeze, 0))

    # --- Round freeze ends ---
    if round_freeze_ends is None and freeze_end is not None and not freeze_end.empty:
        _fe: dict[int, int] = {}
        for _, row in freeze_end.sort_values("tick").iterrows():
            tick = int(row["tick"])
            if first_freeze is not None and tick < first_freeze:
                continue
            rn = int(row.get("round", 0) or 0)
            if rn <= 0:
                rn = _round_for_tick(tick, round_starts, first_freeze)
            if rn > 0 and rn not in _fe:
                _fe[rn] = tick
        round_freeze_ends = _fe
    elif round_freeze_ends is None:
        round_freeze_ends = {}

    # --- Round ends ---
    # CS2 emits `round_officially_ended` for round N at the same tick as
    # `round_start` for round N+1. _round_for_tick() then assigns the end
    # to round N+1 (since start_tick <= tick), but it really belongs to
    # round N. Fix: build a set of round_start ticks and bump those ends
    # back by one round.
    _rs_ticks: set[int] = set()
    for st_tick, _ in round_starts:
        if first_freeze is None or st_tick >= first_freeze:
            _rs_ticks.add(st_tick)
    if round_ends is None and round_end is not None and not round_end.empty:
        _re: dict[int, int] = {}
        for _, row in round_end.sort_values("tick").iterrows():
            tick = int(row["tick"])
            if first_freeze is not None and tick < first_freeze:
                continue
            rn = _round_for_tick(tick, round_starts, first_freeze)
            if tick in _rs_ticks and rn > 0:
                rn -= 1
            if rn > 0 and rn not in _re:
                _re[rn] = tick
        round_ends = _re
    elif round_ends is None:
        round_ends = {}

    # --- Winner per round (authoritative) ---
    # CS2's `round_end` event carries the actual winner side ('T'/'CT').
    # It's derived in build_short_timeline() with a per-round side->team
    # mapping (teams swap sides at halftime) and passed in as winner_by_round;
    # if absent, the kills/bomb heuristic below fills the gaps.
    if winner_by_round is None:
        winner_by_round = {}

    # --- Kills ---
    if kill_events is not None:
        # Synthetic kill events list
        kills_by_round: dict[int, list[dict]] = {}
        for ev in kill_events:
            rn = ev.get("round", _round_for_tick(ev["tick"], round_starts, first_freeze))
            kills_by_round.setdefault(rn, []).append({
                "tick": ev["tick"],
                "round": rn,
                "attacker_sid": str(ev.get("attacker_sid", "")),
                "victim_sid": str(ev.get("victim_sid", "")),
                "weapon": str(ev.get("weapon", "")),
                "victim_weapon": str(ev.get("victim_weapon", "")),
            })
    elif deaths is not None and not deaths.empty:
        kills_by_round = {}
        for _, row in deaths.sort_values("tick").iterrows():
            tick = int(row["tick"])
            if first_freeze is not None and tick < first_freeze:
                continue
            attacker_sid = _sid(row.get("attacker_steamid"))
            victim_sid = _sid(row.get("user_steamid"))
            weapon = str(row.get("weapon", "") or "").strip().lower()
            if not victim_sid:
                continue
            if not attacker_sid and weapon not in ("c4", "planted_c4"):
                continue
            if attacker_sid and attacker_sid == victim_sid and weapon not in ("c4", "planted_c4"):
                continue
            victim_weapon = ""
            if victim_weapon_map:
                for offset in (tick, tick - 1, tick - 2):
                    vw = victim_weapon_map.get((offset, victim_sid), "")
                    if vw:
                        victim_weapon = vw
                        break
            rn = _round_for_tick(tick, round_starts, first_freeze)
            kills_by_round.setdefault(rn, []).append({
                "tick": tick,
                "round": rn,
                "attacker_sid": attacker_sid,
                "victim_sid": victim_sid,
                "weapon": weapon,
                "victim_weapon": victim_weapon,
            })
    else:
        kills_by_round = {}

    # --- Bomb/win events ---
    if round_win_events is not None:
        _rwe = round_win_events
    else:
        _rwe: dict[int, list[dict]] = {}
        for label, df in [("plant", bomb_plant), ("defuse", bomb_defuse), ("explode", bomb_explode)]:
            if df is None or df.empty:
                continue
            for _, row in df.sort_values("tick").iterrows():
                tick = int(row["tick"])
                if first_freeze is not None and tick < first_freeze:
                    continue
                rn = _round_for_tick(tick, round_starts, first_freeze)
                if tick in _rs_ticks and rn > 0:
                    rn -= 1
                _rwe.setdefault(rn, []).append({
                    "tick": tick,
                    "event": label,
                    "player_sid": _sid(row.get("user_steamid")),
                })
        round_win_events = _rwe

    # ================================================================
    # 4K DETECTION
    # ================================================================
    shorts: list[dict] = []

    def _round_winner(rn: int) -> int | None:
        """Team number of the round winner, inferred from bomb events + kills.

        Mirrors build_action_timeline: a defuse/explode names the winning player's
        team; otherwise the last surviving killer's team wins.
        """
        for we in round_win_events.get(rn, []):
            if we["event"] in ("defuse", "explode"):
                sid = we["player_sid"]
                if sid and sid in team_by_sid:
                    return team_by_sid[sid]
        dead: set[str] = set()
        for k in kills_by_round.get(rn, []):
            if k["victim_sid"]:
                dead.add(k["victim_sid"])
        for k in reversed(kills_by_round.get(rn, [])):
            aid = k["attacker_sid"]
            if aid and aid not in dead and aid in team_by_sid:
                return team_by_sid[aid]
        return None

    for _rn, rkills in sorted(kills_by_round.items()):
        if _rn <= 0:
            continue  # round 0 = warmup / knife (side-choice) round — not a real round
        _winner = _round_winner(_rn)
        by_attacker: dict[str, list[dict]] = {}
        for k in rkills:
            aid = k["attacker_sid"]
            if aid:
                by_attacker.setdefault(aid, []).append(k)

        for aid, kills in by_attacker.items():
            if len(kills) < 4:
                continue
            if _winner is not None and team_by_sid.get(aid) != _winner:
                continue  # multikill team must win the round
            if not _meets_tier_criterion(kills):
                continue
            ticks = sorted(k["tick"] for k in kills)
            end_tick = ticks[-1] + _POST_KILL_TICK_MARGIN
            start_tick = min(
                end_tick - _SHORT_TICK_DURATION,
                ticks[0] - _PRE_KILL_TICK_MARGIN,
            )
            shorts.append({
                "short_type": "4k",
                "pov_steam_id": aid,
                "pov_nick": nickname_by_sid.get(aid, "Unknown"),
                "start_tick": start_tick,
                "end_tick": end_tick,
                "kill_ticks": ticks,
                "punch_up_tags": _punch_up_tags(kills),
            })

    # ================================================================
    # CLUTCH DETECTION
    # ================================================================
    _all_rounds = sorted(set(kills_by_round.keys()) | set(round_win_events.keys()))
    all_rounds = [r for r in _all_rounds if r > 0]

    # Winner per round — authoritative round_end winners are supplied by the
    # caller when available; fill any remaining rounds from kills/bomb events.
    if winner_by_round is None:
        winner_by_round = {}
    for rn in all_rounds:
        if rn not in winner_by_round:
            w = _round_winner(rn)
            if w is not None:
                winner_by_round[rn] = w

    # Derive actual team numbers from player data (CS2 uses 2/3, not 1/2)
    _team_nums = sorted({t for t in team_by_sid.values() if t > 1})
    if len(_team_nums) < 2:
        _team_nums = [2, 3]
    _team_a, _team_b = _team_nums[:2]

    # ================================================================
    # 1V3 DETECTION (punch-up triple) — no win required
    # ================================================================
    # A player who gets exactly 3 kills in a round while at a 1v3 (or worse)
    # numbers disadvantage, where at least 2 of those kills "punch up" (killed
    # with a lower-tier weapon than the victim held). Unlike the 4K rule, the
    # round does NOT need to be won — the feat is the outnumbered triple with
    # weaker weapons.
    for roundn in all_rounds:
        rk = sorted(kills_by_round.get(roundn, []), key=lambda x: x["tick"])
        if not rk:
            continue
        alive13: dict[int, int] = {_team_a: 5, _team_b: 5}
        # Track, per attacker, the kills they made and the alive counts at the
        # moment of their FIRST kill (that's when the 1v3 state is set).
        attacker_first_kill_state: dict[str, tuple[int, int]] = {}
        attacker_kills: dict[str, list[dict]] = {}
        seen_first: set[str] = set()
        for k in rk:
            aid = k["attacker_sid"]
            if aid:
                if aid not in seen_first:
                    seen_first.add(aid)
                    attacker_first_kill_state[aid] = (alive13[_team_a], alive13[_team_b])
                attacker_kills.setdefault(aid, []).append(k)
            vt = team_by_sid.get(k["victim_sid"], 0)
            if vt in alive13:
                alive13[vt] = max(0, alive13[vt] - 1)

        for aid, kills in attacker_kills.items():
            if len(kills) != 3:
                continue
            at_team = team_by_sid.get(aid, 0)
            if at_team not in (_team_a, _team_b):
                continue
            enemy = _team_b if at_team == _team_a else _team_a
            my_alive, enemy_alive = attacker_first_kill_state.get(aid, (5, 5))
            # 1v3 or worse: the attacker's team is down to 1 vs >=3 enemies at
            # the moment they start fragging.
            if not (my_alive == 1 and enemy_alive >= 3):
                continue
            punch_up = sum(1 for k in kills if _is_punch_up(k))
            if punch_up < 2:
                continue
            ticks = sorted(k["tick"] for k in kills)
            end_tick = ticks[-1] + _POST_KILL_TICK_MARGIN
            start_tick = min(
                end_tick - _SHORT_TICK_DURATION,
                ticks[0] - _PRE_KILL_TICK_MARGIN,
            )
            shorts.append({
                "short_type": "1v3",
                "pov_steam_id": aid,
                "pov_nick": nickname_by_sid.get(aid, "Unknown"),
                "start_tick": start_tick,
                "end_tick": end_tick,
                "kill_ticks": ticks,
                "clutch_initial_count": f"{my_alive}v{enemy_alive}",
                "punch_up_tags": _punch_up_tags(kills),
            })

    for roundn in all_rounds:
        round_kills = kills_by_round.get(roundn, [])
        alive: dict[int, int] = {_team_a: 5, _team_b: 5}
        victim_teams = {}
        for k in round_kills:
            vt = team_by_sid.get(k["victim_sid"], 0)
            if vt and k["victim_sid"]:
                victim_teams[k["victim_sid"]] = vt

        clutch_triggered: dict[int, dict] = {}

        for k in sorted(round_kills, key=lambda x: x["tick"]):
            vt = victim_teams.get(k["victim_sid"], 0)
            if vt > 0 and vt in alive:
                alive[vt] = max(0, alive[vt] - 1)

            for team in (_team_a, _team_b):
                if team not in alive or team in clutch_triggered:
                    continue
                enemy = _team_b if team == _team_a else _team_a
                if team in alive and enemy in alive:
                    if (alive[team] == 2 and alive[enemy] == 5) or (alive[team] == 1 and alive[enemy] >= 3):
                        at_size = alive[team]
                        enemy_size = alive[enemy]
                        clutch_type = f"{at_size}v{enemy_size}"
                        clutch_triggered[team] = {
                            "start_tick": k["tick"],
                            "type": clutch_type,
                        }

        rw_events = sorted(round_win_events.get(roundn, []), key=lambda e: e["tick"])
        for team, trigger in clutch_triggered.items():
            win_tick = None
            win_event = None
            win_player = None

            # Pick the LAST win event for this team at or after the clutch
            # trigger. Plant/explode can fire mid-round, before the team is
            # actually outnumbered.
            for we in rw_events:
                if we["tick"] < trigger["start_tick"]:
                    continue
                if we["event"] not in ("defuse", "explode"):
                    continue  # plant is not a win
                psid = we["player_sid"]
                if psid and psid in team_by_sid and team_by_sid[psid] == team:
                    win_tick = we["tick"]
                    win_event = we["event"]
                    win_player = psid

            if win_tick is None and round_win_events:
                # If the bomb exploded and it wasn't the clutch team, the clutch
                # team LOST (detonation = planting/T side wins). Do not fall back
                # to round_end_winner — it's misaligned for some demos and would
                # wrongly mark a losing round as a won clutch.
                exploded_by_other = any(
                    we["event"] == "explode"
                    and we.get("player_sid")
                    and team_by_sid.get(we["player_sid"]) != team
                    for we in round_win_events.get(roundn, [])
                )
                if exploded_by_other:
                    continue  # bomb detonated for the other side => not a clutch
            if win_tick is None and winner_by_round and roundn in winner_by_round:
                if winner_by_round[roundn] != team:
                    continue  # clutch team did NOT win => skip
                win_tick = round_ends.get(roundn, 0)
                win_event = "team_win"

            # --- 2v5 special: require 4k from POV, switch to survivor if POV dies ---
            if trigger["type"] == "2v5":
                by_attacker: dict[str, list[dict]] = {}
                for k in round_kills:
                    aid = k["attacker_sid"]
                    if aid and team_by_sid.get(aid, 0) == team:
                        by_attacker.setdefault(aid, []).append(k)
                best_sid = None
                best_kills: list[dict] = []
                for aid, ks in by_attacker.items():
                    if len(ks) >= 4 and len(ks) > len(best_kills):
                        best_sid = aid
                        best_kills = ks
                if best_sid is None:
                    continue  # weak 2v5 (e.g. 2k) — not impressive enough
                win_player = best_sid
                clutch_kills = best_kills
                clutch_kill_ticks = sorted(k["tick"] for k in clutch_kills)
                if win_tick is None or win_tick - trigger["start_tick"] < _CLUTCH_MIN_DURATION_TICKS:
                    continue
                # if POV dies before round win, switch cam to surviving teammate
                death_tick = next((k["tick"] for k in round_kills if k["victim_sid"] == win_player), None)
                switch_to = None
                switch_to_nick = None
                switch_tick = None
                if death_tick is not None and death_tick < win_tick and death_tick > trigger["start_tick"]:
                    dead = {k["victim_sid"] for k in round_kills if k["victim_sid"]}
                    survivors = [sid for sid, t in team_by_sid.items() if t == team and sid not in dead and sid != win_player]
                    for k in reversed(round_kills):
                        if k["attacker_sid"] in survivors:
                            switch_to = k["attacker_sid"]
                            break
                    if switch_to is None and survivors:
                        switch_to = survivors[0]
                    if switch_to:
                        switch_tick = death_tick + 64  # 1s after death
                        switch_to_nick = nickname_by_sid.get(switch_to, "Unknown")
                if clutch_kill_ticks:
                    first_kill = min(clutch_kill_ticks)
                    start_tick = min(trigger["start_tick"], first_kill - _PRE_KILL_TICK_MARGIN)
                else:
                    start_tick = trigger["start_tick"]
                short = {
                    "short_type": "clutch",
                    "pov_steam_id": win_player,
                    "pov_nick": nickname_by_sid.get(win_player, "Unknown"),
                    "start_tick": start_tick,
                    "end_tick": win_tick,
                    "clutch_initial_count": trigger["type"],
                    "round_win_tick": win_tick,
                    "win_event": win_event,
                    "kill_ticks": clutch_kill_ticks,
                    "punch_up_tags": _punch_up_tags(clutch_kills),
                }
                if switch_to:
                    short["pov_switch_tick"] = switch_tick
                    short["pov_switch_to"] = switch_to
                    short["pov_switch_to_nick"] = switch_to_nick
                shorts.append(short)
                continue

            win_player = _last_surviving_killer(round_kills, team, team_by_sid, win_player_hint=win_player)

            clutch_kill_ticks = [
                k["tick"] for k in round_kills
                if k["attacker_sid"] == win_player
            ]
            clutch_kills = [
                k for k in round_kills
                if k["attacker_sid"] == win_player
            ]

            if (
                win_tick is not None
                and win_player is not None
                and win_tick - trigger["start_tick"] >= _CLUTCH_MIN_DURATION_TICKS
            ):
                # Start from the player's first kill of the round (minus a
                # lead-in), not the 1vX trigger, so every kill of a multi-kill
                # clutch is on-screen. Falls back to the trigger tick if the
                # player landed no kills before it.
                if clutch_kill_ticks:
                    first_kill = min(clutch_kill_ticks)
                    start_tick = min(trigger["start_tick"], first_kill - _PRE_KILL_TICK_MARGIN)
                else:
                    start_tick = trigger["start_tick"]
                shorts.append({
                    "short_type": "clutch",
                    "pov_steam_id": win_player,
                    "pov_nick": nickname_by_sid.get(win_player, "Unknown"),
                    "start_tick": start_tick,
                    "end_tick": win_tick,
                    "clutch_initial_count": trigger["type"],
                    "round_win_tick": win_tick,
                    "win_event": win_event,
                    "kill_ticks": clutch_kill_ticks,
                    "punch_up_tags": _punch_up_tags(clutch_kills),
                })

    # --- Recognised-Pro gate (drop randos) ---
    # Filter BEFORE the clutch-over-4K dedup so a non-pro clutch can never
    # suppress a pro 4K (and vice versa).
    dropped_randos = 0
    if pros_only and _pro_sids:
        kept: list[dict] = []
        for s in shorts:
            sid = str(s.get("pov_steam_id") or "")
            canon = _pro_sids.get(sid)
            if not canon:
                dropped_randos += 1  # not a catalogued pro -> drop
                continue
            s["pov_nick"] = canon  # canonical nickname for slug/title
            kept.append(s)
        shorts = kept

    # Prioritise clutches over overlapping multikills: when a clutch short's
    # [trigger, win] window overlaps a 4K short, keep the clutch, drop the 4K.
    clutches = [s for s in shorts if s["short_type"] == "clutch"]
    if clutches:
        shorts = [
            s for s in shorts
            if s["short_type"] == "clutch"
            or not any(
                s["start_tick"] <= c["end_tick"] and c["start_tick"] <= s["end_tick"]
                for c in clutches
            )
        ]

    return {
        "short_type": "short_timeline",
        "demo_path": demo_path,
        "map": header_map or "Unknown",
        "short_count": len(shorts),
        "_dropped_randos": dropped_randos,
        "shorts": shorts,
    }


def _round_for_tick(
    tick: int,
    round_starts: list[tuple[int, int]],
    first_freeze: int | None,
) -> int:
    rn = 0
    for start_tick, round_num in round_starts:
        if start_tick <= tick:
            rn = round_num
        else:
            break
    return rn


def _last_surviving_killer(
    round_kills: list[dict],
    team: int,
    team_by_sid: dict[str, int],
    win_player_hint: str | None = None,
) -> str | None:
    """Pick the POV for a team clutch: the last attacker on this team in
    the round who isn't themselves a victim (i.e. survived). Falls back
    to the win event actor (e.g. defuser), then any surviving teammate."""
    dead: set[str] = {k["victim_sid"] for k in round_kills if k["victim_sid"]}
    killers_team = [
        k for k in round_kills
        if k["attacker_sid"]
        and team_by_sid.get(k["attacker_sid"], 0) == team
        and k["attacker_sid"] not in dead
    ]
    if killers_team:
        return killers_team[-1]["attacker_sid"]
    if win_player_hint and team_by_sid.get(win_player_hint) == team and win_player_hint not in dead:
        return win_player_hint
    for sid, t in team_by_sid.items():
        if t == team and sid not in dead:
            return sid
    return None


def build_short_timeline_from_action(action_timeline_path: Path, demo_path: Path,
                                     pros_only: bool = True) -> dict:
    """Build a Short Timeline from an existing action_timeline.json.

    Reads Recognised Pro-gated kills + bomb events from the Action Timeline,
    infers team assignments from the same source demo (player_info only), then
    runs the standard 4K/Clutch detection via ``detect_shorts()``.
    """
    import demoparser2 as dp

    at = json.loads(action_timeline_path.read_text(encoding="utf-8"))

    # Convert action timeline kills -> kill_events format for detect_shorts()
    kill_events: list[dict] = []
    for k in at.get("kills", []):
        kill_events.append({
            "tick": k["tick"],
            "round": k["round"],
            "attacker_sid": str(k.get("attacker_steam_id", "")),
            "victim_sid": str(k.get("victim_steam_id", "")),
            "weapon": str(k.get("weapon", "")),
            "victim_weapon": str(k.get("victim_weapon", "")),
        })

    # Convert bomb actions -> round_win_events format
    round_win_events: dict[int, list[dict]] = {}
    for b in at.get("bomb_actions", []):
        rn = b["round"]
        round_win_events.setdefault(rn, []).append({
            "tick": b["tick"],
            "event": b["type"],
            "player_sid": str(b.get("player_steam_id", "")),
        })

    # Convert round metadata
    round_starts = [(rs["tick"], rs["round"]) for rs in at.get("round_starts", [])]
    round_freeze_ends: dict[int, int] = {}
    for re_item in at.get("round_freeze_ends", []):
        round_freeze_ends[re_item["round"]] = re_item["tick"]
    round_ends: dict[int, int] = {}
    for re_item in at.get("round_ends", []):
        round_ends[re_item["round"]] = re_item["tick"]

    # Winner per round from action timeline
    winner_by_round: dict[int, int] = {}
    for rn_str, team in at.get("winner_by_round", {}).items():
        try:
            winner_by_round[int(rn_str)] = int(team)
        except (ValueError, TypeError):
            pass

    # Team assignments + nicknames from demo (cheap: player_info only)
    parser = dp.DemoParser(str(demo_path))
    info = parser.parse_player_info()
    team_by_sid: dict[str, int] = {}
    nickname_by_sid: dict[str, str] = {}
    for _, row in info.iterrows():
        sid = _sid(row.get("steamid"))
        if not sid:
            continue
        team_by_sid[sid] = int(row.get("team_number", 0) or 0)
        name = str(row.get("name", "") or "").strip()
        if name:
            nickname_by_sid[sid] = name

    # Also pull nicknames from action timeline kills (some players may not appear in info)
    for k in at.get("kills", []):
        sid = str(k.get("attacker_steam_id", ""))
        if sid and sid not in nickname_by_sid:
            nickname_by_sid[sid] = str(k.get("attacker", ""))
        sid = str(k.get("victim_steam_id", ""))
        if sid and sid not in nickname_by_sid:
            nickname_by_sid[sid] = str(k.get("victim", ""))

    return detect_shorts(
        demo_path=str(demo_path),
        header_map=at.get("map", ""),
        team_by_sid=team_by_sid,
        nickname_by_sid=nickname_by_sid,
        winner_by_round=winner_by_round,
        kill_events=kill_events,
        round_starts=round_starts,
        round_ends=round_ends,
        round_freeze_ends=round_freeze_ends,
        round_win_events=round_win_events,
        pros_only=pros_only,
    )


def _build_short_slug(short: dict) -> str:
    st = short["short_type"]
    nick = short.get("pov_nick", "Unknown")
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in nick)
    tick = short.get("start_tick", 0)
    tags = short.get("punch_up_tags") or []
    suffix = ("_" + "_".join(tags)) if tags else ""
    if st == "4k":
        kills = len(short.get("kill_ticks", []))
        return f"{kills}k_multikill-{safe}-t{tick}{suffix}"
    elif st == "clutch":
        cnt = short.get("clutch_initial_count", "XvX")
        kills = len(short.get("kill_ticks", []))
        return f"{cnt}_{kills}k_clutch-{safe}-t{tick}{suffix}"
    elif st == "1v3":
        cnt = short.get("clutch_initial_count", "1v3")
        kills = len(short.get("kill_ticks", []))
        return f"{cnt}_{kills}k_1v3-{safe}-t{tick}{suffix}"
    return f"{st}-{safe}-t{tick}{suffix}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Build Short Timeline JSON from a demo")
    ap.add_argument("demo_path", type=Path, help="Path to .dem file")
    ap.add_argument("--player", type=str, default=None, help="Steam ID for HLTV demo output dir")
    ap.add_argument("--output", "-o", type=Path, default=None, help="Override output base directory (default: renders/shorts/shorts-<demo>)")
    ap.add_argument(
        "--from-action-timeline", "-A",
        type=Path,
        default=None,
        help="Build shorts from an existing action_timeline.json (Recognised Pro-gated). "
             "Demo used only for player_info (team assignments).",
    )
    ap.add_argument(
        "--include-all-players",
        action="store_true",
        help="Keep shorts for any player (default: only Recognised Pros from "
             ".data/player_accounts.json).",
    )
    args = ap.parse_args()

    pros_only = not args.include_all_players

    demo = args.demo_path
    if not demo.is_file():
        print(f"[ERR] demo not found: {demo}", file=sys.stderr)
        return 1

    if args.from_action_timeline:
        at_path = args.from_action_timeline
        if not at_path.is_file():
            print(f"[ERR] action_timeline.json not found: {at_path}", file=sys.stderr)
            return 1
        timeline = build_short_timeline_from_action(at_path, demo, pros_only=pros_only)
    else:
        timeline = build_short_timeline(demo, player=args.player, pros_only=pros_only)

    dropped = timeline.get("_dropped_randos", 0)
    # _dropped_randos is run-reporting only — keep it out of the persisted
    # short_timeline.json schema.
    timeline = {k: v for k, v in timeline.items() if k != "_dropped_randos"}

    shorts_list = timeline.get("shorts", [])
    if not shorts_list:
        suffix = f" ({dropped} non-pro short(s) filtered)" if dropped else ""
        print(f"[OK] 0 shorts detected{suffix} (no output written)")
        return 0

    if args.from_action_timeline:
        base_dir = args.output or at_path.parent
    else:
        base_dir = args.output or resolve_output_dir(demo, player=args.player)

    written = 0
    for short in shorts_list:
        slug = _build_short_slug(short)
        short_dir = base_dir / f"shorts-{slug}"
        short_dir.mkdir(parents=True, exist_ok=True)
        single_tl = {**timeline, "short_count": 1, "shorts": [short]}
        out = short_dir / "short_timeline.json"
        out.write_text(json.dumps(single_tl, indent=2), encoding="utf-8")
        written += 1

    print(f"[OK] {len(shorts_list)} shorts -> {written} files under {base_dir}"
          + (f" ({dropped} non-pro short(s) filtered)" if dropped else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())