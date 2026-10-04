"""Scoring chips shared by FACEIT notable picks and the HLTV listener.

Chips are named bonuses on a 250k scale. FACEIT notable and HLTV card
scoring compose them; they do not live in the FACEIT scraper.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
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

# Payload rule version: bump when the refresh changes what the payload
# means. Readers refuse membership paths on older versions loudly instead
# of silently running a stale rule. 1 = pre-breakout payloads.
DEMAND_RULE_VERSION = 2

# Reader staleness bound for the live demand payload (same bound the
# star-refresh uses for the POV player index): older than this, the gate
# treats the payload as stale and says so loudly (demand_payload_status).
PLAYER_DEMAND_STALE_DAYS = 7

# Breakout clause for the solo gate (breakouts-section lookup, written by
# update_player_demand.breakout_map): a long-form video (age >= 2d)
# published inside BREAKOUT_DAYS whose views/day clear BREAKOUT_PI times
# its channel median. Medians bury one-off breakouts, so the index alone
# never surfaces them — this reads the max instead of the median.
BREAKOUT_DAYS = 30
BREAKOUT_PI = 30.0


def has_recent_breakout(nick: str, path: Path | None = None) -> bool:
    """Whether this player has a breakout video inside BREAKOUT_DAYS.

    Reads the payload's ``breakouts`` section (per-player max performance
    index over qualifying videos). No payload section (index predating
    the breakout field, missing file) means no breakout — unlike
    demand_star_supported, there is no research fallback to keep.
    """
    _, spike, _, _, _ = demand_eligibility(nick, path)
    return spike


def demand_eligibility(nick: str, path: Path | None = None,
                       *, star_floor: float = 1.40,
                       enforce_freshness: bool = True) -> tuple:
    """One shared eligibility read: (star, spike, index, supported, pi).

    ``star`` = demand index >= floor with sample evidence (production
    gate, together with ``spike`` below: the shipped rule is star OR
    breakout — see is_good_faceit_pov). ``spike`` = breakout PI >=
    BREAKOUT_PI. A nick missing from a non-empty ``players`` section
    counts as unsupported (production behaviour); thin-sample support
    rules live in demand_star_supported.
    """
    low = str(nick).casefold()
    payload = load_demand_payload(path)
    raw_index = load_player_demand_index(path).get(low)
    try:
        index = float(raw_index) if raw_index is not None else None
    except (TypeError, ValueError):
        index = None
    # B1: staleness is enforced on both arms, not just logged — a stale
    # payload's star evidence AND its write-time breakout window are both
    # expired relative to now. Replay callers pass enforce_freshness=False
    # (their payloads are rebuilt as-of the replay day).
    max_age = PLAYER_DEMAND_STALE_DAYS if enforce_freshness else None
    try:
        supported = payload_star_supported(nick, payload, max_age_days=max_age)
    except Exception:
        supported = False
    star = index is not None and index >= float(star_floor) and supported
    pi = None
    breakouts = payload.get("breakouts") if isinstance(payload, dict) else None
    if isinstance(breakouts, dict):
        for key, value in breakouts.items():
            if str(key).casefold() != low:
                continue
            try:
                pi = float(value)
            except (TypeError, ValueError):
                pi = None
            break
    try:
        threshold = float(BREAKOUT_PI)
    except (TypeError, ValueError):
        threshold = float("inf")
    spike = pi is not None and pi >= threshold
    if enforce_freshness:
        # Same file already loaded above — no second read.
        age = payload_age_days(payload)
        fresh = (payload_rule_version(payload) == DEMAND_RULE_VERSION
                 and age is not None and age <= PLAYER_DEMAND_STALE_DAYS)
        spike = bool(spike and fresh)
    return star, spike, index, supported, pi


def payload_rule_version(payload: dict) -> int | None:
    """Rule version stamped by the refresh (``method.rule_version``)."""
    if not isinstance(payload, dict):
        return None
    method = payload.get("method")
    if not isinstance(method, dict):
        return None
    try:
        return int(method.get("rule_version"))
    except (TypeError, ValueError):
        return None


def payload_age_days(payload: dict) -> float | None:
    """Age of the payload from its ``updated_at`` stamp (None if unreadable)."""
    if not isinstance(payload, dict):
        return None
    raw = payload.get("updated_at")
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - stamp).total_seconds() / 86400.0


def demand_payload_status(path: Path | None = None) -> tuple[bool, str]:
    """Freshness of the live demand payload: (fresh, reason).

    Fresh means rule_version == DEMAND_RULE_VERSION and age within
    PLAYER_DEMAND_STALE_DAYS. Anything else (missing file, stale
    version, stale stamp) is not fresh — callers must say so loudly
    instead of silently gating on it (log_demand_payload_status).
    """
    payload = load_demand_payload(path)
    if not payload:
        return False, "missing or unreadable payload"
    version = payload_rule_version(payload)
    if version != DEMAND_RULE_VERSION:
        return False, f"rule_version={version} (current={DEMAND_RULE_VERSION})"
    age = payload_age_days(payload)
    if age is None:
        return False, "unreadable updated_at"
    if age > PLAYER_DEMAND_STALE_DAYS:
        return False, f"age {age:.1f}d > {PLAYER_DEMAND_STALE_DAYS}d"
    return True, f"rule_version={version} age {age:.1f}d"


def log_demand_payload_status(path: Path | None = None,
                              *, prefix: str = "[demand]") -> bool:
    """Log the payload freshness loudly; returns it (F2 staleness guard)."""
    fresh, reason = demand_payload_status(path)
    state = "fresh" if fresh else "STALE"
    print(f"{prefix} payload {state}: {reason}", flush=True)
    return fresh


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


# Canonical nick aliases the evidence lookup must honor (F9 rider:
# the index carries both spellings but the players sample table only
# the canonical one — without this, "dev1ce" false-negatives).
_NICK_ALIASES = {"dev1ce": "device"}


def _canonical_nick(nick: str) -> str:
    low = str(nick).casefold()
    return _NICK_ALIASES.get(low, low)


def payload_star_supported(nick: str, payload: dict,
                             *, max_age_days: float | None = None) -> bool:
    """Evidence check against an already-loaded payload (no file read).

    Shared core behind demand_star_supported so consumers holding the
    payload (e.g. the Shorts gate) apply the identical rule (F9).
    ``max_age_days`` enforces the staleness bound (B1): a payload older
    than the bound carries no evidence, even with sample counts.
    Replay/rebuilt payloads pass None — their frame is the replay's
    own concern, not now.
    """
    players = payload.get("players") if isinstance(payload, dict) else None
    if not isinstance(players, dict) or not players:
        return False
    if payload_rule_version(payload) != DEMAND_RULE_VERSION:
        return False
    if max_age_days is not None:
        age = payload_age_days(payload)
        if age is None or age > max_age_days:
            return False
    want = _canonical_nick(nick)
    entry = None
    for key, value in players.items():
        if _canonical_nick(key) == want:
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


def demand_star_supported(nick: str, path: Path | None = None) -> bool:
    """Whether this player's index entry carries enough sample evidence.

    Reads the payload's ``players`` section (per-player sample counts,
    written by update_player_demand.build_index). A live entry counts
    for the solo gate only with recent evidence or a deep track record.
    No live payload (missing file, empty players section, stale rule
    version per demand_payload_status) means no evidence: NOT supported
    (fail-closed — a dead refresh must narrow the gate loudly via
    log_demand_payload_status, never widen it to the research table).
    The staleness bound (B1) is enforced: a payload older than
    PLAYER_DEMAND_STALE_DAYS carries no evidence even with counts.
    """
    return payload_star_supported(nick, load_demand_payload(path),
                                  max_age_days=PLAYER_DEMAND_STALE_DAYS)


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
