"""Scoring chips shared by FACEIT notable picks and the HLTV listener.

Chips are named bonuses on a 250k scale. FACEIT notable and HLTV card
scoring compose them; they do not live in the FACEIT scraper.
"""
from __future__ import annotations

import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEMAND_INDEX_PATH = PROJECT_ROOT / ".data" / "player_demand_index.json"

# Channel-normalized median performance from the 3,012-video longform study
# (n>=10, index clipped to the 1.08-1.69 band so a tiny-sample spike cannot
# dominate ELO/star), plus a 2-day recency overlay for 100 Thieves. Missing
# players use the neutral 1.0 baseline. Team rank still adapts daily.
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


def load_player_demand_index(path: Path | None = None) -> dict[str, float]:
    """Live YouTube-derived index, falling back to the last researched table."""
    index_path = path if path is not None else DEMAND_INDEX_PATH
    if index_path.exists():
        try:
            payload = json.loads(index_path.read_text(encoding="utf-8"))
            raw = payload.get("index", payload)
            return {str(key).casefold(): float(value) for key, value in raw.items()}
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass
    return dict(PLAYER_DEMAND_INDEX)


def market_demand_bonus(nick: str, path: Path | None = None) -> int:
    """Reward measured player demand above the neutral 1.0 market baseline."""
    index = load_player_demand_index(path).get(nick.casefold(), 1.0)
    return round(max(0.0, index - 1.0) * DEMAND_SCALE)


def demand_points(index: float) -> int:
    """Same 250k-scale as market_demand_bonus, from a raw index value."""
    return round(max(0.0, float(index) - 1.0) * DEMAND_SCALE)


def lobby_elo_bonus(avg_elo: int | float) -> int:
    """Scale 2500-4000 average lobby ELO into a 0-300k quality signal."""
    return round(max(0.0, min(1.0, (avg_elo - 2500) / 1500)) * 300_000)


def costar_bonus(pros: list[str]) -> int:
    """Trio+ stacks help, but 300k let a 5-man CIS queue outrank a 28-9.

    40k per extra pro above a duo, capped at 120k.
    """
    return min(max(len(set(pros)) - 2, 0) * 40_000, 120_000)


def star_bonus(raw_star: int, won: bool = False, kd: float = 1.0) -> int:
    """Org rank is who the POV is, not how one map went.

    No K/D gate: a minus map does not erase the badge (YEKINDAR Cache
    17-22 still carries FURIA #3). ``won``/``kd`` accepted for call-site
    compatibility and not used.
    """
    del won, kd
    if raw_star <= 0:
        return 0
    return raw_star // 2


def perf_bonus(kd: float, adr: float, kills: int, won: bool) -> int:
    """Bounded quality signal. The 80k win chip requires K/D >= 1.0."""
    kd_points = min(max(kd - 1.0, 0.0) * 40_000, 80_000)
    adr_points = min(max(adr - 70.0, 0.0) * 1_000, 50_000)
    kill_points = min(max(kills - 20, 0) * 3_000, 45_000)
    win_points = 80_000 if won and kd >= 1.0 else 0
    return round(kd_points + adr_points + kill_points + win_points)
