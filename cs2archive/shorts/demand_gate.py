"""Candidate acquisition policy and shared view score.

HLTV extraction keeps recognized-pro candidates (recognition is enforced by
the extractor). It does not spend slots or apply long-form demand/fame vetoes.
Global predicted-view selection happens in render_pending_shorts. FACEIT's
existing evidence gate is retained until it has its own clip-view validation.
"""
from __future__ import annotations

import json
from pathlib import Path

from cs2archive.scoring import load_demand_payload

ROOT = Path(__file__).resolve().parents[2]
DEMAND_PATH = ROOT / ".data" / "player_demand_index.json"
STARS_PATH = ROOT / ".data" / "partial_stars.json"
SHORTS_INDEX_FLOOR = 1.0
SHORTS_MIN_VIDEOS = 8  # Legacy import compatibility; evidence is shared below.


def load_partial_stars(path: Path | None = None) -> dict:
    dest = path or STARS_PATH
    try:
        data = json.loads(dest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def candidate_score(short: dict, stars: dict, *, orgs: list[str] | None = None) -> float:
    """Complete predicted log views; no manual bonuses or intercept cutoff."""
    from cs2archive.shorts.view_prediction import cut_features, predict_log_views
    return predict_log_views(cut_features(short, stars, orgs=orgs), stars)


def _player_qualifies(nick: str, payload: dict) -> bool:
    from cs2archive.scoring import (
        BREAKOUT_PI, DEMAND_RULE_VERSION, PLAYER_DEMAND_STALE_DAYS,
        payload_age_days, payload_rule_version, payload_star_supported,
    )
    key = nick.casefold()
    if not key:
        return False
    try:
        index = {str(k).casefold(): v for k, v in (payload.get("index") or {}).items()}
        for name, info in (payload.get("players") or {}).items():
            if isinstance(info, dict):
                index.setdefault(str(name).casefold(), info.get("index") or 0)
        star = (float(index.get(key, 0)) >= SHORTS_INDEX_FLOOR
                and payload_star_supported(nick, payload, max_age_days=PLAYER_DEMAND_STALE_DAYS))
        breakouts = {str(k).casefold(): v for k, v in (payload.get("breakouts") or {}).items()}
        age = payload_age_days(payload)
        fresh = (payload_rule_version(payload) == DEMAND_RULE_VERSION
                 and age is not None and age <= PLAYER_DEMAND_STALE_DAYS)
        spike = fresh and float(breakouts.get(key, 0)) >= float(BREAKOUT_PI)
        return bool(star or spike)
    except (AttributeError, TypeError, ValueError):
        return False


def passes_shorts_demand_gate(nick: str, *, opponent: str | None = None,
                             orgs: list[str] | None = None, text: str = "",
                             payload: dict | None = None) -> bool:
    """FACEIT-only demand evidence gate; org/text never rescue a player."""
    return _player_qualifies(nick, load_demand_payload(DEMAND_PATH) if payload is None else payload)


def folder_orgs(demo) -> list[str]:
    try:
        from cs2archive.shorts.detect_team import orgs_from_folder
        return [disp for disp, _raw in orgs_from_folder(demo)]
    except (OSError, ValueError):
        return []


def filter_publishable_shorts(shorts: list[dict], *, orgs: list[str] | None = None,
                              payload: dict | None = None, source: str = "hltv",
                              stars: dict | None = None) -> tuple[list[dict], int]:
    """Keep HLTV candidates; FACEIT retains its existing eligibility policy.

    No ledger writes and no daily selection during per-demo extraction.
    ``stars`` remains accepted for compatibility; scoring is render-time only.
    """
    if source != "faceit":
        return list(shorts), 0
    data = load_demand_payload(DEMAND_PATH) if payload is None else payload
    kept = [s for s in shorts if passes_shorts_demand_gate(str(s.get("pov_nick") or ""), payload=data)]
    return kept, len(shorts) - len(kept)


def filter_suffix(dropped_randos: int, dropped_demand: int, *, source: str = "hltv") -> str:
    bits = []
    if dropped_randos:
        bits.append(f"{dropped_randos} non-pro")
    if dropped_demand:
        bits.append(f"{dropped_demand} low-demand")
    return f" ({', '.join(bits)} filtered)" if bits else ""
