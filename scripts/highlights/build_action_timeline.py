"""Build an Action Timeline from a FACEIT demo (data only).

Parses demoparser2 events — kills, bomb events, utility usage, round lifecycle —
keeps actions **by** a Recognised Pro (``.data/player_accounts.json``), and writes:

    renders/hl-{demo_stem}/action_timeline.json

Usage:
    python scripts/highlights/build_action_timeline.py <demo_path>
    python scripts/highlights/build_action_timeline.py demos/faceit/some-match.dem
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

    # Round ends — keep one per round (earliest tick wins)
    _re_by_round: dict[int, int] = {}
    if not round_end.empty:
        for _, row in round_end.sort_values("tick").iterrows():
            tick = int(row["tick"])
            if first_freeze is not None and tick < first_freeze:
                continue
            rn = _round_for_tick(tick, round_starts, first_freeze)
            if rn > 0 and rn not in _re_by_round:
                _re_by_round[rn] = tick
    round_ends = [{"round": rn, "tick": t} for rn, t in sorted(_re_by_round.items())]

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

        kills.append({
            "tick": tick,
            "round": _round_for_tick(tick, round_starts, first_freeze),
            "attacker": attacker_name,
            "attacker_steam_id": attacker_sid,
            "victim": victim_name,
            "victim_steam_id": victim_sid,
            "weapon": weapon,
            "is_bomb": is_bomb,
            "headshot": bool(row.get("headshot", False)),
        })

    # --- Bomb events (by anyone — context for the round) ---
    bomb_actions: list[dict] = []
    for label, df in [("plant", bomb_plant), ("defuse", bomb_defuse), ("explode", bomb_explode)]:
        if df.empty:
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

    try:
        demo_rel = str(demo_path.resolve().relative_to(PROJECT_ROOT)).replace("\\", "/")
    except ValueError:
        demo_rel = str(demo_path)

    return {
        "demo_path": demo_rel,
        "map": _map_name(demo_path, header_map),
        "source": "faceit",
        "kill_count": len(kills),
        "kills": kills,
        "bomb_actions": bomb_actions,
        "round_starts": [{"round": rn, "tick": t} for t, rn in round_starts],
        "round_freeze_ends": round_freeze_ends,
        "round_ends": round_ends,
    }


def highlights_run_dir(demo_path: Path) -> Path:
    return PROJECT_ROOT / "renders" / f"hl-{demo_path.stem}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Build FACEIT Action Timeline JSON")
    ap.add_argument("demo_path", type=Path, help="Path to FACEIT .dem under demos/faceit/")
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
    if not _is_faceit_demo(demo):
        print(
            f"[ERR] FACEIT-only: demo must live under demos/faceit/ (got {demo})",
            file=sys.stderr,
        )
        return 1

    timeline = build_action_timeline(demo)
    out = args.output or (highlights_run_dir(demo) / "action_timeline.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(timeline, indent=2), encoding="utf-8")
    print(f"[OK] {timeline['kill_count']} kills, {len(timeline['bomb_actions'])} bomb events -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
