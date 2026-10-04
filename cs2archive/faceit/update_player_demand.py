"""Refresh player demand weights from POV-channel YouTube scrapes.

Includes the eight competitor channels plus @cs2povarchive.

Keeps a deduped history (one row per video_id) under exports/pov_market/.
Scores a rolling 180-day window, blended with a 30-day overlay (recent can
raise or cut the index). Writes .data/player_demand_index.json for scrape_notable.

Usage:
    python cs2archive/faceit/update_player_demand.py
    python cs2archive/faceit/update_player_demand.py --offline
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


from cs2archive.misc.analyze_pov_market import analyze_rows  # noqa: E402
from cs2archive.config import settings  # noqa: E402
from cs2archive.player_accounts import list_accounts  # noqa: E402
from cs2archive.scoring import (  # noqa: E402
    BREAKOUT_DAYS as _BREAKOUT_DAYS,
    BREAKOUT_PI as _BREAKOUT_PI,
    DEMAND_RULE_VERSION as _DEMAND_RULE_VERSION,
)
from cs2archive.misc.scrape_pov_channels import (  # noqa: E402
    DEFAULT_CHANNELS,
    collect,
    load_history,
    stale_video_ids,
    upsert_history_rows,
    write_history,
)

OUTDIR = ROOT / "exports" / "pov_market"
HISTORY_PATH = OUTDIR / "video_history.csv"
DEMAND_PATH = ROOT / ".data" / "player_demand_index.json"
SEED_CSVS = (
    OUTDIR / "video_history.csv",
    OUTDIR / "expanded" / "video_history.csv",
    OUTDIR / "own" / "video_history.csv",
    OUTDIR / "recent_2d" / "video_history.csv",
)
WINDOW_DAYS = 180
REFRESH_DAYS = 7
RECENT_DAYS = 30
MIN_VIDEOS = 8
RECENT_MIN_VIDEOS = 3
THIN_SAMPLE = 8
INDEX_FLOOR = 1.08
INDEX_CAP = 1.80
LONG_BLEND = 0.3
SHORT_BLEND = 0.7
# Breakout window/threshold for the solo gate's breakout clause: a
# Recognised Pro with a long-form video (age >= 2d, like analyze_rows,
# velocity floored at 7d so 2-7d front-load inflation cannot qualify)
# published inside BREAKOUT_DAYS whose views/day clear BREAKOUT_PI times
# its channel median (leave-one-out) with absolute reach (BREAKOUT_VIEWS
# views or BREAKOUT_VPD views/day) earns a solo render even when their
# median-based index sits below the star floor (medians bury one-off
# breakouts).
BREAKOUT_DAYS = _BREAKOUT_DAYS
BREAKOUT_PI = _BREAKOUT_PI
BREAKOUT_VIEWS = 5000.0
BREAKOUT_VPD = 300.0
VELOCITY_FLOOR_DAYS = 7.0


def _parse_published(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def _read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def load_seed_rows() -> list[dict]:
    rows: list[dict] = []
    seen: set[Path] = set()
    paths = list(SEED_CSVS)
    for folder in (OUTDIR, OUTDIR / "expanded", OUTDIR / "own", OUTDIR / "recent_2d"):
        if folder.is_dir():
            paths.extend(folder.glob("videos_*.csv"))
    for path in paths:
        resolved = path.resolve()
        if resolved in seen or not path.exists():
            continue
        seen.add(resolved)
        rows.extend(_read_csv(path))
    return rows


def refresh_velocity(rows: list[dict], now: datetime) -> list[dict]:
    """Recompute age and views/day from stored views so old captures still age."""
    out = []
    for source in rows:
        published = _parse_published(source.get("published_at"))
        if published is None:
            out.append(source)
            continue
        age_days = max((now - published).total_seconds() / 86400, 1 / 24)
        try:
            views = float(source.get("views") or 0)
        except ValueError:
            views = 0.0
        row = dict(source)
        row["age_days"] = round(age_days, 3)
        row["views_per_day"] = round(views / age_days, 2)
        out.append(row)
    return out


def in_window(rows: list[dict], now: datetime, days: int) -> list[dict]:
    cutoff = now - timedelta(days=days)
    out = []
    for row in rows:
        published = _parse_published(row.get("published_at"))
        if published is None or published < cutoff:
            continue
        out.append(row)
    return out


def _row_age_days(row: dict, now: datetime) -> float | None:
    published = _parse_published(row.get("published_at"))
    if published is None:
        return None
    return max((now - published).total_seconds() / 86400, 1 / 24)


def breakout_map(
    rows: list[dict],
    aliases: dict[str, str],
    now: datetime,
    *,
    window_days: int = BREAKOUT_DAYS,
    pi_floor: float = BREAKOUT_PI,
) -> dict[str, float]:
    """Best recent performance index per Recognised Pro.

    Mirrors analyze_rows' exclusions (long-form only, age >= 2d) with two
    additions the council measured in: velocity is floored at
    VELOCITY_FLOOR_DAYS (2-7d front-load inflation would otherwise qualify
    fresh clips on its own), and the channel baseline is leave-one-out
    (a player's own rows never carry their own channel median). The
    qualifying video also needs absolute reach (BREAKOUT_VIEWS views or
    BREAKOUT_VPD views/day) so a 100x spike on a near-zero channel cannot
    qualify. Returns ``{canonical_nick: max_pi}`` for videos published
    inside *window_days*, keeping only players clearing *pi_floor*.
    """
    from statistics import median

    eligible: list[tuple[dict, float, float, float]] = []
    for row in rows:
        try:
            duration = float(row.get("duration_seconds") or 0)
        except (TypeError, ValueError):
            duration = 0
        if duration and duration < 300:
            continue
        age = _row_age_days(row, now)
        if age is None or age < 2:
            continue
        try:
            views = float(row.get("views") or 0)
        except (TypeError, ValueError):
            continue
        floored = views / max(age, VELOCITY_FLOOR_DAYS)
        eligible.append((row, age, views, floored))
    # Baselines mirror analyze_rows (all eligible rows) so PI stays
    # comparable with the index; the reach filter applies to candidates
    # only, never to the baseline.
    by_channel: dict[str, list[tuple[int, float]]] = {}
    for pos, (row, _age, _views, vpd) in enumerate(eligible):
        by_channel.setdefault(row.get("channel") or "?", []).append((pos, vpd))
    loo_median: dict[tuple[str, str], float] = {}
    for channel, items in by_channel.items():
        for skip, _vpd in items:
            rest = [v for pos, v in items if pos != skip]
            if rest:
                loo_median[(channel, str(skip))] = median(rest)
    cutoff = now - timedelta(days=window_days)
    best: dict[str, float] = {}
    for pos, (row, age, views, vpd) in enumerate(eligible):
        if views < BREAKOUT_VIEWS and vpd < BREAKOUT_VPD:
            continue
        if (_parse_published(row.get("published_at")) or now) < cutoff:
            continue
        raw = (row.get("primary_player") or "").strip()
        player = aliases.get(raw.casefold()) if raw else None
        if not player:
            continue
        base = loo_median.get((row.get("channel") or "?", str(pos))) or 0
        if not base:
            continue
        pi = vpd / base
        key = player.casefold()
        if pi > best.get(key, 0.0):
            best[key] = pi
    return {nick: round(pi, 1) for nick, pi in best.items() if pi >= pi_floor}


def recognised_aliases() -> dict[str, str]:
    aliases = {"dev1ce": "device", "device": "device"}
    for account in list_accounts():
        nick = (account.nickname or "").strip()
        if not nick:
            continue
        aliases[nick.casefold()] = nick
        faceit = (account.faceit_nickname or "").strip()
        if faceit:
            aliases[faceit.casefold()] = nick
    return aliases


def canonical_player(label: str, aliases: dict[str, str]) -> str | None:
    nick = (label or "").strip()
    if not nick:
        return None
    return aliases.get(nick.casefold())


def build_index(
    long_report: dict,
    recent_report: dict,
    aliases: dict[str, str],
) -> tuple[dict[str, float], dict[str, dict]]:
    recent_groups = {}
    for group in recent_report.get("groups", {}).get("primary_players", []):
        player = canonical_player(group.get("label") or "", aliases)
        if player:
            recent_groups[player] = group

    index: dict[str, float] = {}
    details: dict[str, dict] = {}
    seen: set[str] = set()
    for group in long_report.get("groups", {}).get("primary_players", []):
        player = canonical_player(group.get("label") or "", aliases)
        if not player or player.casefold() in seen:
            continue
        seen.add(player.casefold())
        long_n = int(group.get("videos") or 0)
        long_median = float(group.get("median_performance_index") or 0)
        recent = recent_groups.get(player)
        recent_n = int(recent.get("videos") or 0) if recent else 0
        recent_median = (
            float(recent["median_performance_index"]) if recent else None
        )
        if long_n < MIN_VIDEOS and recent_n < RECENT_MIN_VIDEOS:
            continue
        value = long_median if long_n >= MIN_VIDEOS else (recent_median or 0.0)
        if (
            recent_n >= RECENT_MIN_VIDEOS
            and recent_median is not None
            and long_n >= MIN_VIDEOS
        ):
            value = LONG_BLEND * long_median + SHORT_BLEND * recent_median
        sample_n = long_n if long_n >= MIN_VIDEOS else recent_n
        if sample_n < THIN_SAMPLE:
            value = min(value, 1.35)
        value = round(min(INDEX_CAP, max(value, 0.0)), 2)
        details[player] = {
            "videos": long_n,
            "median_performance_index": round(long_median, 2),
            "recent_videos": recent_n,
            "recent_median_performance_index": (
                round(recent_median, 2) if recent_median is not None else None
            ),
            "index": value,
        }
        if value >= INDEX_FLOOR:
            index[player.casefold()] = value
            if player.casefold() == "device":
                index["dev1ce"] = value
    return dict(sorted(index.items(), key=lambda item: (-item[1], item[0]))), details


async def scrape_new(history: list[dict], days: int) -> list[dict]:
    if not settings.youtube_api_key:
        raise RuntimeError("YOUTUBE_API_KEY is not configured in .env")
    now = datetime.now(timezone.utc)
    skip_ids = stale_video_ids(history, now, REFRESH_DAYS)
    _channels, rows = await collect(
        list(DEFAULT_CHANNELS),
        settings.youtube_api_key,
        days,
        2000,
        skip_ids=skip_ids,
    )
    return rows


def refresh(*, scrape: bool = True, days: int = REFRESH_DAYS) -> dict:
    now = datetime.now(timezone.utc)
    history = upsert_history_rows(load_history(HISTORY_PATH), load_seed_rows())
    new_rows: list[dict] = []
    if scrape:
        new_rows = asyncio.run(scrape_new(history, days))
        history = upsert_history_rows(history, new_rows)
    write_history(HISTORY_PATH, history)

    aliases = recognised_aliases()
    aged = []
    for row in refresh_velocity(history, now):
        player = canonical_player(row.get("primary_player") or "", aliases)
        if player:
            row = dict(row)
            row["primary_player"] = player
        aged.append(row)
    long_report = analyze_rows(in_window(aged, now, WINDOW_DAYS), source=str(HISTORY_PATH))
    recent_report = analyze_rows(in_window(aged, now, RECENT_DAYS), source="recent")
    index, details = build_index(long_report, recent_report, aliases)
    breakouts = breakout_map(in_window(aged, now, WINDOW_DAYS), aliases, now)
    payload = {
        "updated_at": now.isoformat(),
        "window_days": WINDOW_DAYS,
        "recent_days": RECENT_DAYS,
        "history_videos": len(history),
        "window_videos": long_report["method"]["videos_analyzed"],
        "scraped": len(new_rows),
        "index": index,
        "players": details,
        "breakouts": breakouts,
        "method": {
            "rule_version": _DEMAND_RULE_VERSION,
            "performance_index": long_report["method"]["performance_index"],
            "min_videos": MIN_VIDEOS,
            "recent_min_videos": RECENT_MIN_VIDEOS,
            "thin_sample_cap": 1.35,
            "thin_sample": THIN_SAMPLE,
            "breakout_days": BREAKOUT_DAYS,
            "breakout_pi": BREAKOUT_PI,
            "breakout_views": BREAKOUT_VIEWS,
            "breakout_vpd": BREAKOUT_VPD,
            "velocity_floor_days": VELOCITY_FLOOR_DAYS,
            "long_blend": LONG_BLEND,
            "short_blend": SHORT_BLEND,
            "index_floor": INDEX_FLOOR,
            "index_cap": INDEX_CAP,
            "excluded_under_5_minutes": long_report["method"]["excluded_under_5_minutes"],
            "excluded_younger_than_2_days": long_report["method"]["excluded_younger_than_2_days"],
        },
    }
    DEMAND_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = DEMAND_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(DEMAND_PATH)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Recompute from stored history only (no YouTube API)",
    )
    parser.add_argument("--days", type=int, default=REFRESH_DAYS)
    args = parser.parse_args()
    payload = refresh(scrape=not args.offline, days=args.days)
    print(
        f"Demand index: {len(payload['index'])} players, "
        f"{payload['window_videos']} videos / {WINDOW_DAYS}d, "
        f"scraped {payload['scraped']} new/updated rows"
    )
    print(DEMAND_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
