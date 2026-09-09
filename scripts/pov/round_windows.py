"""Plan HLAE record windows from CSDM analysis.

Oversize round spans (halftime / tech pause glued onto startTick) make HLAE
die and take the rest of the batch with them. Tech restarts that never
finished are usually draws (no winner) — those rounds are skipped. Rounds
that still have a winner are trimmed to the freeze/action window.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

DEADAIR_SPAN_TICKS = 25_000  # HLAE cannot record brutal timeout/halftime spans
ACTION_LEAD_TICKS = 1_280  # ~20s of freeze before first kill / freeze end
MIN_RECORD_TICKS = 64
VOIDED_FILE = ".voided_rounds.json"
WINDOWS_FILE = "round_windows.json"


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


def plan_round_windows(data: dict) -> list[RoundWindow]:
    """Return the HLAE window for each CSDM round (skip, trim, or as-is)."""
    kills = _real_kills(data)
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
        if span <= DEADAIR_SPAN_TICKS:
            planned.append(RoundWindow(number, start, end))
            continue
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
        planned.append(RoundWindow(
            number, new_start, end, trimmed=new_start > start,
            reason=f"dead-air trim ({span / 64:.0f}s -> {(end - new_start) / 64:.0f}s)",
        ))
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
