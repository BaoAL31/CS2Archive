"""Gate which Recognised-Pro Shorts are worth a daily upload slot.

HLTV cuts use Candidate score (Partial stars). FACEIT still uses the player
index / NAVI-Spirit-Vitality hook until FACEIT scoring reuses the same model.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEMAND_PATH = ROOT / ".data" / "player_demand_index.json"
STARS_PATH = ROOT / ".data" / "partial_stars.json"

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


def load_partial_stars(path: Path | None = None) -> dict:
    dest = path or STARS_PATH
    if not dest.is_file():
        return {"intercept": 0.0, "player": {}, "opponent": {}, "stage": {}, "kind": {}}
    try:
        data = json.loads(dest.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {"intercept": 0.0, "player": {}, "opponent": {}, "stage": {}, "kind": {}}
    return data if isinstance(data, dict) else {}


def candidate_score(short: dict, stars: dict, *, orgs: list[str] | None = None) -> float:
    """Intercept + player + opponent + stage + every active kind. Missing add 0."""
    from shorts.clip_observation import kinds_from_cut, opponent_of_cut

    intercept = float(stars.get("intercept") or 0)
    sid = str(short.get("pov_steam_id") or "")
    player = float((stars.get("player") or {}).get(sid) or 0) if sid else 0.0
    opp_canon = opponent_of_cut(short, orgs)
    opponent = float((stars.get("opponent") or {}).get(opp_canon) or 0) if opp_canon else 0.0
    stage = short.get("stage")
    stage_star = float((stars.get("stage") or {}).get(stage) or 0) if stage else 0.0
    kinds = short.get("kinds")
    if not isinstance(kinds, (list, tuple)):
        kinds = kinds_from_cut(short)
    kind_table = stars.get("kind") or {}
    kind_sum = sum(float(kind_table.get(k) or 0) for k in kinds)
    return intercept + player + opponent + stage_star + kind_sum


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
    source: str = "hltv",
    stars: dict | None = None,
) -> tuple[list[dict], int]:
    """Keep shorts that pass the demand gate. Returns (kept, dropped_count)."""
    if source == "faceit":
        kept: list[dict] = []
        dropped = 0
        for short in shorts:
            if passes_shorts_demand_gate(
                str(short.get("pov_nick") or ""),
                orgs=orgs,
                opponent=short.get("opponent"),
                payload=payload,
            ):
                kept.append(short)
            else:
                dropped += 1
        return kept, dropped

    table = stars if stars is not None else load_partial_stars()
    intercept = float(table.get("intercept") or 0)
    scored: list[tuple[float, dict]] = []
    dropped = 0
    for short in shorts:
        score = candidate_score(short, table, orgs=orgs)
        if score > intercept:
            scored.append((score, short))
        else:
            dropped += 1
    scored.sort(key=lambda item: -item[0])
    return [short for _, short in scored], dropped


def filter_suffix(dropped_randos: int, dropped_demand: int, *, source: str = "hltv") -> str:
    bits = []
    if dropped_randos:
        bits.append(f"{dropped_randos} non-pro")
    if dropped_demand:
        label = "low-demand" if source == "faceit" else "slot-floor"
        bits.append(f"{dropped_demand} {label}")
    return f" ({', '.join(bits)} filtered)" if bits else ""
