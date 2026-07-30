"""Build an Action Timeline from a demo (HLTV or FACEIT, data only).

Parses demoparser2 events — kills, bomb events, utility usage, round lifecycle —
keeps actions **by** a Recognised Pro (``.data/player_accounts.json``), and writes:

    renders/hl-{demo_stem}/action_timeline.json

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


def build_action_timeline(demo_path: Path) -> dict:
    import demoparser2 as dp

    parser = dp.DemoParser(str(demo_path))

    # Core events
    deaths = parser.parse_event("player_death")
    round_start = parser.parse_event("round_start")
    freeze_end = parser.parse_event("round_freeze_end")
    round_end = parser.parse_event("round_officially_ended")
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

    # --- Kills ---
    kills: list[dict] = []
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
        if not attacker_sid and not is_bomb:
            continue  # world / suicide without attacker
        if attacker_sid and attacker_sid == victim_sid and not is_bomb:
            continue  # suicide

        # Team kill (same side) — skip unless bomb
        if (
            not is_bomb
            and attacker_sid
            and attacker_sid in team_by_sid
            and victim_sid in team_by_sid
            and team_by_sid[attacker_sid] == team_by_sid[victim_sid]
            and team_by_sid[attacker_sid] > 0
        ):
            continue

        attacker_is_pro = bool(attacker_sid and attacker_sid in pro_sids)
        victim_is_pro = bool(victim_sid and victim_sid in pro_sids)
        if not attacker_is_pro and not victim_is_pro:
            continue  # keep kills involving at least one Recognised Pro (either side)

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

        kills.append({
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
        })

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

    try:
        demo_rel = str(demo_path.resolve().relative_to(PROJECT_ROOT)).replace("\\", "/")
    except ValueError:
        demo_rel = str(demo_path)

    source = "hltv" if "demos/hltv" in demo_rel else "faceit"

    return {
        "demo_path": demo_rel,
        "map": _map_name(demo_path, header_map),
        "source": source,
        "kill_count": len(kills),
        "kills": kills,
        "bomb_actions": bomb_actions,
        "round_starts": [{"round": rn, "tick": t} for t, rn in round_starts],
        "round_freeze_ends": round_freeze_ends,
        "round_ends": round_ends,
        "winner_by_round": {str(rn): t for rn, t in winner_by_round.items()},
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
