"""Scoring chips shared by FACEIT notable picks and the HLTV listener.

Chips are named bonuses on a 250k scale. FACEIT notable and HLTV card
scoring compose them; they do not live in the FACEIT scraper.
"""
from __future__ import annotations

import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEMAND_INDEX_PATH = PROJECT_ROOT / ".data" / "player_demand_index.json"

# Fallback table if `.data/player_demand_index.json` is missing. Live indexes
# clip to 1.08–1.80 (thin samples n<8 stop at 1.35) and blend 30% 180-day /
# 70% last-30-day. Missing players use the neutral 1.0 baseline.
PLAYER_DEMAND_INDEX = {
    "ropz": 1.69,
    "donk": 1.50,
    "s1mple": 1.50,
    "xantares": 1.44,
    "zont1x": 1.41,
    "teses": 1.35,
    "flamez": 1.32,
    "device": 1.28,
    "dev1ce": 1.28,
    "nocries": 1.23,
    "m0nesy": 1.21,
    "apex": 1.20,
    "electronic": 1.18,
    "niko": 1.17,
    "heavygod": 1.15,
    "kyousuke": 1.12,
    "rain": 1.12,
    "tn1r": 1.12,
    "magnojez": 1.10,
    "sh1ro": 1.09,
    "zywoo": 1.08,
}

DEMAND_SCALE = 250_000

# Sample-evidence gate for a live index entry (players-section lookup).
# An above-floor index only counts for the solo gate when the player has
# recent evidence (>= DEMAND_RECENT_MIN_VIDEOS videos in the last 30d) or
# a deep track record (>= DEMAND_DEEP_TRACK_RECORD videos overall) —
# long-window-only thin samples (e.g. 8 videos, 0 recent) go stale
# silently and must not auto-pass the gate.
DEMAND_RECENT_MIN_VIDEOS = 3
DEMAND_DEEP_TRACK_RECORD = 25


def load_demand_payload(path: Path | None = None) -> dict:
    """The raw demand-index payload (``{}`` when absent or unreadable).

    CR-03: the canonical reader. Besides the ``index`` mapping (what
    ``load_player_demand_index`` returns) the payload carries a ``players`` section with per-player
    sample counts, which the Shorts demand gate needs to require a minimum number of videos before
    trusting a thin index. Reading the file in one place keeps the fallback policy identical for
    both consumers.
    """
    index_path = path if path is not None else DEMAND_INDEX_PATH
    if not index_path.exists():
        return {}
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def load_player_demand_index(path: Path | None = None) -> dict[str, float]:
    """Live YouTube-derived index, falling back to the last researched table."""
    payload = load_demand_payload(path)
    raw = payload.get("index", payload) if payload else None
    if isinstance(raw, dict) and raw:
        try:
            return {str(key).casefold(): float(value) for key, value in raw.items()}
        except (TypeError, ValueError):
            pass
    return dict(PLAYER_DEMAND_INDEX)


def demand_star_supported(nick: str, path: Path | None = None) -> bool:
    """Whether this player's index entry carries enough sample evidence.

    Reads the payload's ``players`` section (per-player sample counts,
    written by update_player_demand.build_index). A live entry counts
    for the solo gate only with recent evidence or a deep track record.
    No live payload (missing file -> the research fallback table, which
    has no sample counts) keeps the old behaviour: supported.
    """
    payload = load_demand_payload(path)
    players = payload.get("players")
    if not isinstance(players, dict) or not players:
        return True
    entry = players.get(nick)
    if entry is None:
        low = str(nick).casefold()
        for key, value in players.items():
            if str(key).casefold() == low:
                entry = value
                break
    if not isinstance(entry, dict):
        return False
    try:
        recent_n = int(entry.get("recent_videos") or 0)
        videos_n = int(entry.get("videos") or 0)
    except (TypeError, ValueError):
        return False
    return (recent_n >= DEMAND_RECENT_MIN_VIDEOS
            or videos_n >= DEMAND_DEEP_TRACK_RECORD)


def market_demand_bonus(nick: str, path: Path | None = None) -> int:
    """Reward measured player demand above the neutral 1.0 market baseline."""
    index = load_player_demand_index(path).get(nick.casefold(), 1.0)
    return round(max(0.0, index - 1.0) * DEMAND_SCALE)


def demand_points(index: float) -> int:
    """Same 250k-scale as market_demand_bonus, from a raw index value."""
    return round(max(0.0, float(index) - 1.0) * DEMAND_SCALE)


def demand_as_raw_star(nick: str, path: Path | None = None) -> int:
    """Demand chip in org-rank raw units so ``star_bonus`` * K/D still applies.

    ``star_bonus`` does ``raw // 2``; this is ``market_demand_bonus * 2``.
    Used when HLTV org rank is 0 (unranked FACEIT stars like s1mple).
    """
    return market_demand_bonus(nick, path) * 2


def lobby_elo_bonus(avg_elo: int | float) -> int:
    """Scale 2500-4000 average lobby ELO into a 0-300k quality signal."""
    return round(max(0.0, min(1.0, (avg_elo - 2500) / 1500)) * 300_000)


def costar_bonus(pros: list[str]) -> int:
    """Trio+ stacks help, but 300k let a 5-man CIS queue outrank a 28-9.

    40k per extra pro above a duo, capped at 120k.
    """
    return min(max(len(set(pros)) - 2, 0) * 40_000, 120_000)


def star_bonus(raw_star: int, won: bool = False, kd: float = 1.0) -> int:
    """Org-rank chip, scaled by this map's K/D within [0.5, 2.0].

    ``won`` is accepted for call-site compatibility and not used. A star
    with a 2.0 K/D is worth twice a 1.0 and that is the ceiling: one
    frags line must not dominate the weight (jL 6.5 K/D used to pay a
    1.3M chip, 88% of the total). A disaster map still pays at least
    half — the name, not the map, is the signal.
    """
    del won
    if raw_star <= 0:
        return 0
    try:
        ratio = min(max(float(kd), 0.5), 2.0)
    except (TypeError, ValueError):
        ratio = 1.0
    return round((raw_star // 2) * ratio)


def perf_bonus(kd: float, adr: float, kills: int, won: bool) -> int:
    """Bounded quality signal. The 80k win chip requires K/D >= 1.0."""
    kd_points = min(max(kd - 1.0, 0.0) * 40_000, 80_000)
    adr_points = min(max(adr - 70.0, 0.0) * 1_000, 50_000)
    kill_points = min(max(kills - 20, 0) * 3_000, 45_000)
    win_points = 80_000 if won and kd >= 1.0 else 0
    return round(kd_points + adr_points + kill_points + win_points)
