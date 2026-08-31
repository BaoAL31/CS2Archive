"""Gate which Recognised-Pro Shorts are worth a daily upload slot.

Recognised-Pro status is too wide: r1nkle/z4KR clips take the two Shorts
slots and die at <10 views. Keep a clip if the POV has measured YouTube
demand, or the match/title names NAVI, Spirit, or Vitality (the orgs that
actually rescue an unknown POV on this channel).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEMAND_PATH = ROOT / ".data" / "player_demand_index.json"

SHORTS_INDEX_FLOOR = 1.0
SHORTS_MIN_VIDEOS = 8
HOOK_ORG_KEYS = frozenset({
    "navi",
    "natusvincere",
    "spirit",
    "teamspirit",
    "vitality",
})
_HOOK_TEXT = re.compile(
    r"\bnavi\b|\bnatus\s*vincere\b|\bspirit\b|\bvitality\b",
    re.IGNORECASE,
)
_ORG_KEY = re.compile(r"[^a-z0-9]+")


def _org_key(name: str) -> str:
    return _ORG_KEY.sub("", name.casefold())


def _load_payload() -> dict:
    if not DEMAND_PATH.exists():
        return {}
    try:
        data = json.loads(DEMAND_PATH.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _player_qualifies(nick: str, payload: dict) -> bool:
    key = nick.casefold()
    if not key:
        return False
    index = {
        str(name).casefold(): float(value)
        for name, value in (payload.get("index") or {}).items()
    }
    if key in index and index[key] >= SHORTS_INDEX_FLOOR:
        return True
    for name, info in (payload.get("players") or {}).items():
        if str(name).casefold() != key or not isinstance(info, dict):
            continue
        videos = int(info.get("videos") or 0)
        value = float(info.get("index") or 0)
        return videos >= SHORTS_MIN_VIDEOS and value >= SHORTS_INDEX_FLOOR
    if payload.get("index") or payload.get("players"):
        return False
    try:
        from scrape_notable import PLAYER_DEMAND_INDEX
    except Exception:
        return False
    return float(PLAYER_DEMAND_INDEX.get(key, 0)) >= SHORTS_INDEX_FLOOR


def _orgs_qualify(orgs: list[str] | None) -> bool:
    for org in orgs or []:
        if _org_key(org) in HOOK_ORG_KEYS:
            return True
    return False


def passes_shorts_demand_gate(
    nick: str,
    *,
    opponent: str | None = None,
    orgs: list[str] | None = None,
    text: str = "",
    payload: dict | None = None,
) -> bool:
    """True when this Short should take a public slot."""
    data = _load_payload() if payload is None else payload
    if _player_qualifies(nick, data):
        return True
    names = list(orgs or [])
    if opponent:
        names.append(opponent)
    if _orgs_qualify(names):
        return True
    return bool(text and _HOOK_TEXT.search(text))


def folder_orgs(demo) -> list[str]:
    """Display names of the two teams from an HLTV demo folder, if any."""
    try:
        from shorts.detect_team import orgs_from_folder
        return [disp for disp, _raw in orgs_from_folder(demo)]
    except Exception:
        return []


def filter_publishable_shorts(
    shorts: list[dict],
    *,
    orgs: list[str] | None = None,
    payload: dict | None = None,
) -> tuple[list[dict], int]:
    """Keep shorts that pass the demand gate. Returns (kept, dropped_count)."""
    kept: list[dict] = []
    dropped = 0
    for short in shorts:
        if passes_shorts_demand_gate(
            str(short.get("pov_nick") or ""),
            orgs=orgs,
            payload=payload,
        ):
            kept.append(short)
        else:
            dropped += 1
    return kept, dropped


def filter_suffix(dropped_randos: int, dropped_demand: int) -> str:
    bits = []
    if dropped_randos:
        bits.append(f"{dropped_randos} non-pro")
    if dropped_demand:
        bits.append(f"{dropped_demand} low-demand")
    return f" ({', '.join(bits)} filtered)" if bits else ""
