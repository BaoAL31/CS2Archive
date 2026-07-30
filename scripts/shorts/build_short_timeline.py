"""Build a Short Timeline from any CS2 demo (HLTV or FACEIT).

Detects two Short types:
  - **4K** : 4+ kills by same attacker in a single round (incl. 5-kill aces).
  - **Clutch** : team wins from 2v4 or worse (1v3, 1v4, 1v5, 2v4, 2v5).

Two input modes:
  1. **Direct demo parse** (default): parses the full demo via demoparser2.
  2. **From Action Timeline** (``--from-action-timeline``): reads an existing
     ``action_timeline.json`` (Recognised Pro-gated, FACEIT-only), extracts
     kill events + team assignments, and runs the same 4K/Clutch detection.
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

_DEFAULT_TICK_MARGIN = 0


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


def build_short_timeline(demo_path: Path, player: str | None = None) -> dict:
    """Parse demo via demoparser2 and extract 4K/Clutch Shorts."""
    import demoparser2 as dp

    parser = dp.DemoParser(str(demo_path))

    deaths = parser.parse_event("player_death")
    round_start = parser.parse_event("round_start")
    freeze_end = parser.parse_event("round_freeze_end")
    round_end = parser.parse_event("round_officially_ended")
    info = parser.parse_player_info()
    bomb_plant = parser.parse_event("bomb_planted")
    bomb_defuse = parser.parse_event("bomb_defused")
    bomb_explode = parser.parse_event("bomb_exploded")

    try:
        header = parser.parse_header()
        header_map = str(header.get("map_name", "") or "")
    except Exception:
        header_map = ""

    nickname_by_sid = _build_nickname_map(info)

    return detect_shorts(
        demo_path=str(demo_path),
        header_map=header_map,
        deaths=deaths,
        round_start=round_start,
        freeze_end=freeze_end,
        round_end=round_end,
        info=info,
        nickname_by_sid=nickname_by_sid,
        bomb_plant=bomb_plant,
        bomb_defuse=bomb_defuse,
        bomb_explode=bomb_explode,
    )


def detect_shorts(
    *,
    demo_path: str,
    header_map: str = "",
    deaths: "pd.DataFrame | None" = None,
    round_start: "pd.DataFrame | None" = None,
    freeze_end: "pd.DataFrame | None" = None,
    round_end: "pd.DataFrame | None" = None,
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
) -> dict:
    """Detect 4K and Clutch Shorts from parsed or synthetic events.

    Accepts either pandas DataFrames (from demoparser2) or plain Python
    dicts/lists for easy unit testing without a real demo file.
    """
    import pandas as pd

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
            rn = _round_for_tick(tick, round_starts, first_freeze)
            kills_by_round.setdefault(rn, []).append({
                "tick": tick,
                "round": rn,
                "attacker_sid": attacker_sid,
                "victim_sid": victim_sid,
                "weapon": weapon,
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

    for _rn, rkills in sorted(kills_by_round.items()):
        by_attacker: dict[str, list[dict]] = {}
        for k in rkills:
            aid = k["attacker_sid"]
            if aid:
                by_attacker.setdefault(aid, []).append(k)

        for aid, kills in by_attacker.items():
            if len(kills) < 4:
                continue
            ticks = sorted(k["tick"] for k in kills)
            shorts.append({
                "short_type": "4k",
                "pov_steam_id": aid,
                "pov_nick": nickname_by_sid.get(aid, "Unknown"),
                "start_tick": ticks[0] - _DEFAULT_TICK_MARGIN,
                "end_tick": ticks[-1] + _DEFAULT_TICK_MARGIN,
                "kill_ticks": ticks,
            })

    # ================================================================
    # CLUTCH DETECTION
    # ================================================================
    _all_rounds = sorted(set(kills_by_round.keys()) | set(round_win_events.keys()))
    all_rounds = [r for r in _all_rounds if r > 0]

    # Derive actual team numbers from player data (CS2 uses 2/3, not 1/2)
    _team_nums = sorted({t for t in team_by_sid.values() if t > 1})
    if len(_team_nums) < 2:
        _team_nums = [2, 3]
    _team_a, _team_b = _team_nums[:2]

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
                    if alive[team] <= 2 and alive[enemy] >= 4:
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

            if win_tick is None and winner_by_round and roundn in winner_by_round:
                if winner_by_round[roundn] != team:
                    continue  # clutch team did NOT win => skip
                win_tick = round_ends.get(roundn, 0)
                win_event = "team_win"

            win_player = _last_surviving_killer(round_kills, team, team_by_sid, win_player_hint=win_player)

            if win_tick is not None and win_player is not None:
                shorts.append({
                    "short_type": "clutch",
                    "pov_steam_id": win_player,
                    "pov_nick": nickname_by_sid.get(win_player, "Unknown"),
                    "start_tick": trigger["start_tick"],
                    "end_tick": win_tick,
                    "clutch_initial_count": trigger["type"],
                    "round_win_tick": win_tick,
                    "win_event": win_event,
                })

    return {
        "short_type": "short_timeline",
        "demo_path": demo_path,
        "map": header_map or "Unknown",
        "short_count": len(shorts),
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


def build_short_timeline_from_action(action_timeline_path: Path, demo_path: Path) -> dict:
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
    )


def _build_short_slug(short: dict) -> str:
    st = short["short_type"]
    nick = short.get("pov_nick", "Unknown")
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in nick)
    if st == "4k":
        return f"4k-{safe}"
    elif st == "clutch":
        cnt = short.get("clutch_initial_count", "XvX")
        return f"clutch-{safe}-{cnt}"
    return f"{st}-{safe}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Build Short Timeline JSON from a demo")
    ap.add_argument("demo_path", type=Path, help="Path to .dem file")
    ap.add_argument("--player", type=str, default=None, help="Steam ID for HLTV demo output dir")
    ap.add_argument("--output", "-o", type=Path, default=None, help="Override output base directory")
    ap.add_argument(
        "--from-action-timeline", "-A",
        type=Path,
        default=None,
        help="Build shorts from an existing action_timeline.json (Recognised Pro-gated). "
             "Demo used only for player_info (team assignments).",
    )
    args = ap.parse_args()

    demo = args.demo_path
    if not demo.is_file():
        print(f"[ERR] demo not found: {demo}", file=sys.stderr)
        return 1

    if args.from_action_timeline:
        at_path = args.from_action_timeline
        if not at_path.is_file():
            print(f"[ERR] action_timeline.json not found: {at_path}", file=sys.stderr)
            return 1
        timeline = build_short_timeline_from_action(at_path, demo)
        base_dir = args.output or at_path.parent
    else:
        timeline = build_short_timeline(demo, player=args.player)
        base_dir = args.output or resolve_output_dir(demo, player=args.player)

    shorts_list = timeline.get("shorts", [])
    if not shorts_list:
        print("[OK] 0 shorts detected (no output written)")
        return 0

    written = 0
    for short in shorts_list:
        slug = _build_short_slug(short)
        short_dir = base_dir / f"shorts-{slug}"
        short_dir.mkdir(parents=True, exist_ok=True)
        single_tl = {**timeline, "short_count": 1, "shorts": [short]}
        out = short_dir / "short_timeline.json"
        out.write_text(json.dumps(single_tl, indent=2), encoding="utf-8")
        written += 1

    print(f"[OK] {len(shorts_list)} shorts -> {written} files under {base_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())