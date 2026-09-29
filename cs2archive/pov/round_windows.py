"""Plan HLAE record windows from CSDM analysis.

Oversize round spans (halftime / tech pause glued onto startTick) make HLAE
die and take the rest of the batch with them. Tech restarts that never
finished are usually draws (no winner) — those rounds are skipped. Rounds
that still have a winner are trimmed to the freeze/action window.

Save tails: when the POV is alive, lost the round (bomb exploded on CT, or
clock ran out on T), and the POV's whole team has not fired / taken a fight /
thrown util for SAVE_IDLE_TICKS before round end, keep SAVE_KEEP_TICKS after
that last team action and drop the rest of the hide. A lurking POV does not
count as a save while teammates are still fighting. A 30s save and a 60s save
both show 15s of idle.

Obvious defuse tails: CT POV alive, bomb defused with at least 2s still on the
clock, and every T already dead. The record window stops 3s before the defuse
completes so a free plant-defuse is not played out. A last-second defuse or a
live T is kept in full.

Native CSDM play already starts at freeze-128; save/defuse trims use that same
start so buy time is not pulled back in.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

DEADAIR_SPAN_TICKS = 25_000  # HLAE cannot record brutal timeout/halftime spans
ACTION_LEAD_TICKS = 1_280  # ~20s of freeze before first kill / freeze end
CSDM_PLAY_LEAD_TICKS = 128  # native --event rounds starts at freeze-128
MIN_RECORD_TICKS = 64
SAVE_IDLE_TICKS = 30 * 64  # 30s with no fight before round end = committed save
SAVE_KEEP_TICKS = 15 * 64  # show this much idle after last action, then cut
BOMB_LIFETIME_TICKS = 2624  # plant->explode ~41s @64 tick (CS2 C4)
DEFUSE_MIN_LEFT_TICKS = 128  # skip obvious defuse only if >=2s left on the bomb
DEFUSE_PRE_TAIL_TICKS = 192  # stop 3s before a comfortable defuse completes
VOIDED_FILE = ".voided_rounds.json"
WINDOWS_FILE = "round_windows.json"

# CSDM round.endReason integers (CS2 RoundEndReason).
_BOMB_EXPLODED = frozenset({1, "1", "TargetBombed"})
_BOMB_DEFUSED = frozenset({7, "7", "BombDefused"})
_TIME_EXPIRED = frozenset({12, "12", "TargetSaved", "time_ran_out"})
_SIDE_T = 2
_SIDE_CT = 3


@dataclass(frozen=True)
class RoundWindow:
    number: int
    start_tick: int
    end_tick: int
    skip: bool = False
    trimmed: bool = False
    reason: str = ""


def _kill_list(data: dict) -> list[dict]:
    kills = data.get("kills", [])
    if isinstance(kills, dict):
        kills = list(kills.values())
    return [k for k in kills if isinstance(k, dict)]


def _has_winner(round_row: dict) -> bool:
    side = round_row.get("winnerSide")
    if side in (2, 3):
        return True
    name = (round_row.get("winnerTeamName") or round_row.get("winnerName") or "")
    return bool(str(name).strip())


def _real_kills(data: dict) -> dict[int, list[int]]:
    by_round: dict[int, list[int]] = {}
    for k in _kill_list(data):
        try:
            rn, tick = int(k.get("roundNumber")), int(k.get("tick"))
        except (TypeError, ValueError):
            continue
        if not tick:
            continue
        if (k.get("killerName") or "") == (k.get("victimName") or ""):
            continue
        by_round.setdefault(rn, []).append(tick)
    for rn in by_round:
        by_round[rn].sort()
    return by_round


def _steam(val) -> str:
    return str(val or "").strip()


def _event_list(data: dict, key: str) -> list[dict]:
    rows = data.get(key, [])
    if isinstance(rows, dict):
        rows = list(rows.values())
    return [r for r in rows if isinstance(r, dict)]


def _end_reason(round_row: dict):
    return round_row.get("endReason")


def _player_team_name(data: dict, steam_id: str) -> str:
    for p in _event_list(data, "players"):
        if _steam(p.get("steamId")) == steam_id:
            return str(p.get("teamName") or "").strip()
    return ""


def _teammate_steam_ids(data: dict, steam_id: str) -> set[str]:
    """Match-team steam ids (same teamName), always including the POV."""
    steam_id = _steam(steam_id)
    team = _player_team_name(data, steam_id)
    ids = {steam_id} if steam_id else set()
    if not team:
        return ids
    for p in _event_list(data, "players"):
        if str(p.get("teamName") or "").strip() == team:
            sid = _steam(p.get("steamId"))
            if sid:
                ids.add(sid)
    return ids


def _side_steam_ids(data: dict, round_row: dict, side: int) -> set[str]:
    team_a = str((data.get("teamA") or {}).get("name") or "").strip()
    team_b = str((data.get("teamB") or {}).get("name") or "").strip()
    try:
        a_side = int(round_row.get("teamASide"))
        b_side = int(round_row.get("teamBSide"))
    except (TypeError, ValueError):
        return set()
    name = team_a if a_side == side else team_b if b_side == side else ""
    if not name:
        return set()
    ids: set[str] = set()
    for p in _event_list(data, "players"):
        if str(p.get("teamName") or "").strip() == name:
            sid = _steam(p.get("steamId"))
            if sid:
                ids.add(sid)
    return ids


def _round_event_tick(data: dict, key: str, number: int) -> int | None:
    last = None
    for row in _event_list(data, key):
        try:
            if int(row.get("roundNumber")) != number:
                continue
            t = int(row.get("tick"))
        except (TypeError, ValueError):
            continue
        if t and (last is None or t > last):
            last = t
    return last


def _pov_side(data: dict, round_row: dict, steam_id: str) -> int | None:
    team = _player_team_name(data, steam_id)
    if not team:
        return None
    team_a = str((data.get("teamA") or {}).get("name") or "").strip()
    team_b = str((data.get("teamB") or {}).get("name") or "").strip()
    try:
        if team == team_a:
            return int(round_row.get("teamASide"))
        if team == team_b:
            return int(round_row.get("teamBSide"))
    except (TypeError, ValueError):
        return None
    return None


def _pov_died(kills: list[dict], number: int, steam_id: str) -> bool:
    for k in kills:
        try:
            if int(k.get("roundNumber")) != number:
                continue
        except (TypeError, ValueError):
            continue
        if _steam(k.get("victimSteamId")) != steam_id:
            continue
        if (k.get("killerName") or "") == (k.get("victimName") or ""):
            continue
        return True
    return False


def _is_save_loss(data: dict, round_row: dict, number: int, pov_side: int) -> bool:
    reason = _end_reason(round_row)
    exploded = False
    for b in _event_list(data, "bombsExploded"):
        try:
            if int(b.get("roundNumber")) == number:
                exploded = True
                break
        except (TypeError, ValueError):
            continue
    if pov_side == _SIDE_CT and (reason in _BOMB_EXPLODED or exploded):
        return True
    if pov_side == _SIDE_T and reason in _TIME_EXPIRED:
        return True
    return False


def _last_action_tick(
    data: dict, kills: list[dict], number: int, team_ids: set[str],
) -> int | None:
    """Latest fight/util tick involving anyone on the POV's team."""
    last: int | None = None

    def _in_round(row: dict) -> bool:
        try:
            return int(row.get("roundNumber")) == number
        except (TypeError, ValueError):
            return False

    def _on_team(*steam_ids) -> bool:
        return any(_steam(s) in team_ids for s in steam_ids)

    def _keep(tick) -> None:
        nonlocal last
        try:
            t = int(tick)
        except (TypeError, ValueError):
            return
        if t and (last is None or t > last):
            last = t

    for s in _event_list(data, "shots"):
        if _in_round(s) and _on_team(s.get("playerSteamId")):
            _keep(s.get("tick"))
    for dmg in _event_list(data, "damages"):
        if not _in_round(dmg):
            continue
        if _on_team(dmg.get("attackerSteamId"), dmg.get("victimSteamId")):
            _keep(dmg.get("tick"))
    for k in kills:
        if not _in_round(k):
            continue
        if (k.get("killerName") or "") == (k.get("victimName") or ""):
            continue
        if _on_team(k.get("killerSteamId"), k.get("victimSteamId")):
            _keep(k.get("tick"))
    for g in _event_list(data, "grenadeDestroyed"):
        if _in_round(g) and _on_team(g.get("throwerSteamId")):
            _keep(g.get("tick"))
    return last


def _all_dead_by(
    kills: list[dict], number: int, steam_ids: set[str], by_tick: int,
) -> bool:
    if not steam_ids:
        return False
    dead: set[str] = set()
    for k in kills:
        try:
            if int(k.get("roundNumber")) != number:
                continue
            tick = int(k.get("tick"))
        except (TypeError, ValueError):
            continue
        if not tick or tick > by_tick:
            continue
        if (k.get("killerName") or "") == (k.get("victimName") or ""):
            continue
        vid = _steam(k.get("victimSteamId"))
        if vid in steam_ids:
            dead.add(vid)
    return steam_ids <= dead


def _obvious_defuse_cut_end(
    data: dict,
    round_row: dict,
    kills: list[dict],
    number: int,
    end: int,
    steam_id: str,
) -> int | None:
    """Cut a free CT defuse (Ts dead, >=2s left on the bomb)."""
    steam_id = _steam(steam_id)
    if not steam_id:
        return None
    if _pov_died(kills, number, steam_id):
        return None
    side = _pov_side(data, round_row, steam_id)
    if side != _SIDE_CT:
        return None
    reason = _end_reason(round_row)
    defuse = _round_event_tick(data, "bombsDefused", number)
    if defuse is None and reason not in _BOMB_DEFUSED:
        return None
    if defuse is None:
        defuse = end
    plant = _round_event_tick(data, "bombsPlanted", number)
    if plant is None or defuse <= plant:
        return None
    left = BOMB_LIFETIME_TICKS - (defuse - plant)
    if left < DEFUSE_MIN_LEFT_TICKS:
        return None
    t_ids = _side_steam_ids(data, round_row, _SIDE_T)
    if not _all_dead_by(kills, number, t_ids, defuse):
        return None
    last = _last_action_tick(data, kills, number, _teammate_steam_ids(data, steam_id))
    cut = defuse - DEFUSE_PRE_TAIL_TICKS
    if last is not None:
        cut = max(cut, last + 64)
    if cut >= end or cut <= 0:
        return None
    return cut


def _save_cut_end(
    data: dict,
    round_row: dict,
    kills: list[dict],
    number: int,
    end: int,
    steam_id: str,
) -> int | None:
    """Return a new end tick, or None if this is not a committed save tail."""
    steam_id = _steam(steam_id)
    if not steam_id:
        return None
    if _pov_died(kills, number, steam_id):
        return None
    side = _pov_side(data, round_row, steam_id)
    if side not in (_SIDE_T, _SIDE_CT):
        return None
    if not _is_save_loss(data, round_row, number, side):
        return None
    last = _last_action_tick(data, kills, number, _teammate_steam_ids(data, steam_id))
    freeze = round_row.get("freezetimeEndTick")
    try:
        freeze_i = int(freeze) if freeze is not None else None
    except (TypeError, ValueError):
        freeze_i = None
    if last is None:
        last = freeze_i
    if last is None:
        return None
    if end - last < SAVE_IDLE_TICKS:
        return None
    cut = last + SAVE_KEEP_TICKS
    if cut >= end or cut <= last:
        return None
    return cut


def _play_start(round_row: dict, start: int, end: int) -> int:
    """CSDM native play start (freeze-128), not analysis startTick (buy)."""
    freeze = round_row.get("freezetimeEndTick")
    try:
        freeze_i = int(freeze) if freeze is not None else None
    except (TypeError, ValueError):
        freeze_i = None
    if freeze_i is None or not (start < freeze_i < end):
        return start
    return max(start, freeze_i - CSDM_PLAY_LEAD_TICKS)


def _action_start(round_row: dict, kills: list[int], start: int, end: int) -> int:
    freeze = round_row.get("freezetimeEndTick")
    try:
        freeze_i = int(freeze) if freeze is not None else None
    except (TypeError, ValueError):
        freeze_i = None
    if (
        freeze_i is not None
        and start < freeze_i < end
        and (end - freeze_i) <= DEADAIR_SPAN_TICKS
    ):
        return max(start, freeze_i - ACTION_LEAD_TICKS)
    if kills:
        return max(start, kills[0] - ACTION_LEAD_TICKS)
    return max(start, end - 90 * 64)


def plan_round_windows(data: dict, steam_id: str | None = None) -> list[RoundWindow]:
    """Return the HLAE window for each CSDM round (skip, trim, or as-is)."""
    kills = _real_kills(data)
    kill_rows = _kill_list(data)
    pov = _steam(steam_id)
    planned: list[RoundWindow] = []
    for r in data.get("rounds", []):
        try:
            number = int(r.get("number"))
            start = int(r.get("startTick", 0))
            end = int(r.get("endTick", 0))
        except (TypeError, ValueError):
            continue
        if end <= start:
            planned.append(RoundWindow(number, start, end, skip=True,
                                       reason="empty span"))
            continue
        span = end - start
        trimmed = False
        reason = ""
        new_start, new_end = start, end
        if span > DEADAIR_SPAN_TICKS:
            if not _has_winner(r):
                planned.append(RoundWindow(
                    number, start, end, skip=True,
                    reason=f"voided tech/draw ({span / 64:.0f}s, no winner)",
                ))
                continue
            new_start = _action_start(r, kills.get(number, []), start, end)
            new_start = min(new_start, end - MIN_RECORD_TICKS)
            new_start = max(new_start, start)
            if end - new_start > DEADAIR_SPAN_TICKS:
                new_start = max(new_start, end - DEADAIR_SPAN_TICKS)
            trimmed = new_start > start
            if trimmed:
                reason = (
                    f"dead-air trim ({span / 64:.0f}s -> "
                    f"{(end - new_start) / 64:.0f}s)"
                )
        save_end = _save_cut_end(data, r, kill_rows, number, new_end, pov)
        defuse_end = _obvious_defuse_cut_end(data, r, kill_rows, number, new_end, pov)
        extra_end = min(t for t in (save_end, defuse_end) if t is not None) if (
            save_end is not None or defuse_end is not None
        ) else None
        if extra_end is not None:
            play_start = new_start if trimmed else _play_start(r, start, end)
            cut = min(extra_end, new_end)
            if cut - play_start >= MIN_RECORD_TICKS:
                new_start, new_end = play_start, cut
                bits = []
                if save_end is not None:
                    idle_s = (end - (save_end - SAVE_KEEP_TICKS)) / 64
                    bits.append(f"save trim (idle {idle_s:.0f}s)")
                if defuse_end is not None:
                    bits.append("obvious defuse trim")
                extra_bit = "; ".join(bits)
                reason = f"{reason}; {extra_bit}" if reason else extra_bit
                trimmed = True
        if trimmed:
            planned.append(RoundWindow(
                number, new_start, new_end, trimmed=True, reason=reason,
            ))
        else:
            planned.append(RoundWindow(number, start, end))
    return planned


def dump_round_windows(windows: list[RoundWindow], path: Path) -> None:
    path.write_text(json.dumps([asdict(w) for w in windows], indent=2),
                    encoding="utf-8")


def load_voided_rounds(folder: Path) -> set[int]:
    p = folder / VOIDED_FILE
    if not p.exists():
        return set()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return {int(x) for x in raw}
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return set()


def write_voided_rounds(folder: Path, rounds: set[int]) -> None:
    path = folder / VOIDED_FILE
    if not rounds:
        if path.exists():
            path.unlink()
        return
    path.write_text(json.dumps(sorted(rounds)), encoding="utf-8")
