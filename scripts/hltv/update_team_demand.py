"""Build team (and highlight-player) demand from official highlight channels.

POV-channel scrapes already feed `.data/player_demand_index.json` (individual
stars). Highlight videos are Team vs Team with view counts that track which
fixtures the market actually watches — that becomes team stars.

Usage:
    python scripts/hltv/update_team_demand.py
    python scripts/hltv/update_team_demand.py --offline
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from _pathsetup import ensure  # noqa: E402

ensure()

from config import settings  # noqa: E402
from hltv_ranking import RANKING_TEAM_NAMES, TEAM_ALIASES  # noqa: E402
from scrape_pov_channels import (  # noqa: E402
    _load_pro_aliases,
    _pro_mentions,
    collect,
    load_history,
    stale_video_ids,
    upsert_history_rows,
    write_history,
)
from scrapers.trending import HIGHLIGHT_CHANNELS  # noqa: E402

OUTDIR = ROOT / "exports" / "pov_market" / "highlights"
HISTORY_PATH = OUTDIR / "video_history.csv"
TEAM_DEMAND_PATH = ROOT / ".data" / "team_demand_index.json"

WINDOW_DAYS = 180
REFRESH_DAYS = 7
MIN_TEAM_VIDEOS = 4
MIN_PLAYER_VIDEOS = 3
THIN_SAMPLE = 8
THIN_CAP = 1.35
INDEX_FLOOR = 1.08
INDEX_CAP = 1.80
MIN_DURATION_S = 60
MIN_AGE_DAYS = 2

# Highlight titles use short names the ranking table does not.
HIGHLIGHT_ALIASES = {
    "navi": "Natus Vincere",
    "natusvincere": "Natus Vincere",
    "mongolz": "The MongolZ",
    "themongolz": "The MongolZ",
    "themongol": "The MongolZ",
    "furia": "FURIA",
    "nip": "Ninjas in Pyjamas",
    "ninjasinpyjamas": "Ninjas in Pyjamas",
    "betboom": "BETBOOM",
    "bbteam": "BETBOOM",
    "3dmax": "3DMAX",
    "pain": "paiN",
    "gamerlegion": "GamerLegion",
    "parivision": "PARIVISION",
    "faze": "FaZe",
    "fazelan": "FaZe",
}

VS_RE = re.compile(r"\s+(?:vs\.?|v\.?)\s+", re.IGNORECASE)


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.casefold())


def team_lookup(names: list[str] | None = None) -> dict[str, str]:
    """normalized spelling -> canonical HLTV ranking name."""
    lookup: dict[str, str] = {}

    def add(canonical: str, *aliases: str) -> None:
        canon = canonical.strip()
        if not canon:
            return
        lookup[_norm(canon)] = canon
        for alias in aliases:
            key = _norm(alias)
            if key:
                lookup[key] = canon

    for name in names or list(RANKING_TEAM_NAMES):
        add(name)
    if TEAM_DEMAND_PATH.parent.joinpath("hltv_team_ranking.json").exists():
        try:
            payload = json.loads(
                TEAM_DEMAND_PATH.parent.joinpath("hltv_team_ranking.json").read_text(
                    encoding="utf-8"
                )
            )
            for name in (payload.get("teams") or {}):
                add(str(name))
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    for alias, canonical in TEAM_ALIASES.items():
        add(canonical, alias)
    for alias, canonical in HIGHLIGHT_ALIASES.items():
        add(canonical, alias)
    return lookup


def canonical_team(name: str, lookup: dict[str, str] | None = None) -> str | None:
    if not name or not str(name).strip():
        return None
    lookup = lookup or team_lookup()
    return lookup.get(_norm(name))


def resolve_team_name(name: str, lookup: dict[str, str] | None = None) -> str | None:
    """Exact canonical match, then longest team alias found in the string.

    Listener slugs keep the event suffix on team 2 (``furia blast open porto``).
    """
    lookup = lookup or team_lookup()
    exact = canonical_team(name, lookup)
    if exact:
        return exact
    return _match_team(str(name or ""), lookup, prefer="last")


def _match_team(
    segment: str, lookup: dict[str, str], *, prefer: str
) -> str | None:
    haystack = _norm(segment)
    if not haystack:
        return None
    found: list[tuple[int, int, str]] = []
    for key, canonical in lookup.items():
        if not key:
            continue
        start = 0
        while True:
            idx = haystack.find(key, start)
            if idx < 0:
                break
            found.append((idx, len(key), canonical))
            start = idx + 1
    if not found:
        return None
    # Longest alias wins; then last match on the left of "vs", first on the right.
    found.sort(
        key=lambda item: (
            -item[1],
            -item[0] if prefer == "last" else item[0],
        )
    )
    return found[0][2]


def extract_fixture_teams(
    title: str, lookup: dict[str, str] | None = None
) -> tuple[str, str] | None:
    """Return canonical (team1, team2) from a highlight title, if both resolve."""
    lookup = lookup or team_lookup()
    for match in VS_RE.finditer(title):
        left = title[: match.start()]
        right = title[match.end() :]
        team1 = _match_team(left, lookup, prefer="last")
        team2 = _match_team(right, lookup, prefer="first")
        if team1 and team2 and team1 != team2:
            return team1, team2
    return None


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


def _num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _clip_index(value: float, videos: int) -> float:
    if videos < THIN_SAMPLE:
        value = min(value, THIN_CAP)
    return round(min(INDEX_CAP, max(value, 0.0)), 2)


def build_index(
    rows: list[dict],
    *,
    aliases: dict[str, str] | None = None,
    lookup: dict[str, str] | None = None,
    now: datetime | None = None,
) -> dict:
    """Channel-normalized team/player indexes plus per-fixture view records."""
    now = now or datetime.now(timezone.utc)
    lookup = lookup or team_lookup()
    aliases = aliases if aliases is not None else _load_pro_aliases()

    parsed: list[dict] = []
    for row in rows:
        title = str(row.get("title") or "")
        teams = extract_fixture_teams(title, lookup)
        players = [name for _, name in _pro_mentions(title, aliases)]
        parsed.append({**row, "_teams": teams, "_players": players})

    fixtures = []
    for row in parsed:
        if not row["_teams"]:
            continue
        team1, team2 = row["_teams"]
        published = _parse_published(row.get("published_at"))
        fixtures.append({
            "team1": team1,
            "team2": team2,
            "views": int(_num(row.get("views"))),
            "views_per_day": round(_num(row.get("views_per_day")), 2),
            "published_at": published.isoformat() if published else None,
            "title": row.get("title") or "",
            "channel": row.get("channel") or "",
            "video_id": row.get("video_id") or "",
            "players": row["_players"],
        })

    durable: list[dict] = []
    for row in parsed:
        age = _num(row.get("age_days"))
        duration = _num(row.get("duration_seconds"))
        if duration < MIN_DURATION_S or age < MIN_AGE_DAYS:
            continue
        if not row["_teams"] and not row["_players"]:
            continue
        durable.append(row)

    by_channel: dict[str, list[dict]] = {}
    for row in durable:
        by_channel.setdefault(str(row.get("channel") or ""), []).append(row)
    baseline: dict[str, float] = {}
    for channel, items in by_channel.items():
        velocities = [_num(item.get("views_per_day")) for item in items]
        velocities = [v for v in velocities if v > 0]
        if velocities:
            baseline[channel] = median(velocities)

    for row in durable:
        base = baseline.get(str(row.get("channel") or ""), 0.0)
        vpd = _num(row.get("views_per_day"))
        row["_perf"] = (vpd / base) if base else 0.0

    team_scores: dict[str, list[float]] = {}
    player_scores: dict[str, list[float]] = {}
    for row in durable:
        perf = row["_perf"]
        if perf <= 0:
            continue
        if row["_teams"]:
            for team in row["_teams"]:
                team_scores.setdefault(team, []).append(perf)
        for player in row["_players"]:
            player_scores.setdefault(player, []).append(perf)

    def _finalize(
        scores: dict[str, list[float]], min_videos: int, key_casefold: bool
    ) -> tuple[dict[str, float], dict[str, dict]]:
        index: dict[str, float] = {}
        details: dict[str, dict] = {}
        for label, values in scores.items():
            n = len(values)
            value = _clip_index(median(values), n)
            details[label] = {"videos": n, "index": value}
            if n < min_videos or value < INDEX_FLOOR:
                continue
            key = label.casefold() if key_casefold else label
            index[key] = value
        return (
            dict(sorted(index.items(), key=lambda item: (-item[1], item[0]))),
            details,
        )

    teams, team_details = _finalize(team_scores, MIN_TEAM_VIDEOS, False)
    players, player_details = _finalize(player_scores, MIN_PLAYER_VIDEOS, True)

    return {
        "updated_at": now.isoformat(),
        "window_days": WINDOW_DAYS,
        "history_videos": len(rows),
        "durable_videos": len(durable),
        "index": teams,
        "players": players,
        "fixtures": fixtures,
        "teams": team_details,
        "player_details": player_details,
        "method": {
            "channels": list(HIGHLIGHT_CHANNELS.values()),
            "min_team_videos": MIN_TEAM_VIDEOS,
            "min_player_videos": MIN_PLAYER_VIDEOS,
            "thin_sample_cap": THIN_CAP,
            "index_floor": INDEX_FLOOR,
            "index_cap": INDEX_CAP,
            "min_duration_s": MIN_DURATION_S,
            "min_age_days": MIN_AGE_DAYS,
        },
    }


def load_team_demand(path: Path | None = None) -> dict:
    target = path or TEAM_DEMAND_PATH
    if not target.exists():
        return {"index": {}, "players": {}, "fixtures": [], "teams": {}}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"index": {}, "players": {}, "fixtures": [], "teams": {}}
    payload.setdefault("index", {})
    payload.setdefault("players", {})
    payload.setdefault("fixtures", [])
    return payload


def _stale(payload: dict, *, hours: int) -> bool:
    raw = payload.get("updated_at")
    stamp = _parse_published(raw) if raw else None
    if stamp is None:
        return True
    return datetime.now(timezone.utc) - stamp >= timedelta(hours=hours)


async def scrape_highlights(history: list[dict], days: int) -> list[dict]:
    if not settings.youtube_api_key:
        raise RuntimeError("YOUTUBE_API_KEY is not configured in .env")
    now = datetime.now(timezone.utc)
    skip_ids = stale_video_ids(history, now, REFRESH_DAYS)
    _channels, rows = await collect(
        list(HIGHLIGHT_CHANNELS.keys()),
        settings.youtube_api_key,
        days,
        2000,
        skip_ids=skip_ids,
    )
    return rows


def refresh(*, scrape: bool = True, days: int = REFRESH_DAYS) -> dict:
    now = datetime.now(timezone.utc)
    history = load_history(HISTORY_PATH)
    new_rows: list[dict] = []
    if scrape:
        new_rows = asyncio.run(scrape_highlights(history, days))
        history = upsert_history_rows(history, new_rows)
    OUTDIR.mkdir(parents=True, exist_ok=True)
    write_history(HISTORY_PATH, history)
    payload = build_index(history, now=now)
    payload["scraped"] = len(new_rows)
    TEAM_DEMAND_PATH.parent.mkdir(parents=True, exist_ok=True)
    TEAM_DEMAND_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def maybe_refresh(*, max_age_hours: int = 24, scrape: bool = True) -> dict:
    existing = load_team_demand()
    if existing.get("index") and not _stale(existing, hours=max_age_hours):
        return existing
    try:
        return refresh(scrape=scrape)
    except Exception as exc:
        print(f"[demand] highlight scrape failed: {type(exc).__name__}: {exc}",
              flush=True)
        return existing


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Recompute from stored highlight history (no YouTube API)",
    )
    parser.add_argument("--days", type=int, default=REFRESH_DAYS)
    args = parser.parse_args()
    payload = refresh(scrape=not args.offline, days=args.days)
    print(
        f"Team demand: {len(payload['index'])} teams, "
        f"{len(payload.get('players') or {})} highlight-named players, "
        f"{payload['durable_videos']} durable / {payload['history_videos']} videos, "
        f"scraped {payload.get('scraped', 0)}"
    )
    print(TEAM_DEMAND_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
