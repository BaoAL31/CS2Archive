"""Collect public performance metrics for competing CS2 POV channels.

Uses the YouTube Data API v3 and upserts each run into video_history.csv
(one row per video_id; newer captures replace older ones).
Defaults to eight active CS2 POV competitor channels.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from urllib.parse import parse_qs, urlparse

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from _pathsetup import ensure  # noqa: E402

ensure()

from config import settings  # noqa: E402

API_ROOT = "https://www.googleapis.com/youtube/v3"
DEFAULT_CHANNELS = (
    "https://www.youtube.com/watch?v=DGN4BfQjXK0",
    "https://www.youtube.com/watch?v=3nacQIa2mxM",
    "https://www.youtube.com/watch?v=ICc19JZuoRI",
    "https://www.youtube.com/watch?v=IkCkY9urTtE",
    "https://www.youtube.com/watch?v=_rx0dc9dvvw",
    "https://www.youtube.com/watch?v=8GbocgSYv7w",
    "https://www.youtube.com/watch?v=G2wsicLJMnQ",
    "@cs2propovs",
)
CS2_MAPS = (
    "ancient",
    "anubis",
    "cache",
    "cobblestone",
    "dust2",
    "dust 2",
    "inferno",
    "mirage",
    "nuke",
    "overpass",
    "train",
    "vertigo",
)
VIDEO_FIELDS = (
    "captured_at",
    "channel_id",
    "channel",
    "subscribers",
    "video_id",
    "title",
    "description",
    "tags",
    "hashtags",
    "category_id",
    "default_language",
    "default_audio_language",
    "published_at",
    "publish_weekday",
    "publish_hour_utc",
    "age_days",
    "duration_seconds",
    "definition",
    "captioned",
    "licensed_content",
    "embeddable",
    "made_for_kids",
    "topic_categories",
    "thumbnail_url",
    "views",
    "likes",
    "comments",
    "views_per_day",
    "views_per_subscriber",
    "like_rate",
    "comment_rate",
    "title_characters",
    "title_words",
    "description_characters",
    "tag_count",
    "primary_player",
    "secondary_players",
    "secondary_player_count",
    "party_type",
    "opponent_pro_players",
    "all_pro_players",
    "kills",
    "deaths",
    "elo",
    "map",
    "has_faceit",
    "has_voicecomms",
    "has_input_overlay",
    "url",
)


def _load_pro_aliases(path: Path = ROOT / ".data" / "player_accounts.json") -> dict[str, str]:
    try:
        accounts = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    aliases: dict[str, str] = {}
    for account in accounts:
        canonical = str(account.get("nickname", "")).strip()
        if not canonical:
            continue
        for key in ("nickname", "faceit_nickname"):
            alias = str(account.get(key, "")).strip()
            if alias:
                aliases[alias.casefold()] = canonical
    return aliases


PRO_ALIASES = _load_pro_aliases()


def _video_id(value: str) -> str | None:
    parsed = urlparse(value)
    host = parsed.netloc.lower().removeprefix("www.")
    if host == "youtu.be":
        return parsed.path.strip("/").split("/")[0] or None
    if host in {"youtube.com", "m.youtube.com"}:
        if parsed.path == "/watch":
            return parse_qs(parsed.query).get("v", [None])[0]
        match = re.match(r"/(?:shorts|live|embed)/([^/?]+)", parsed.path)
        if match:
            return match.group(1)
    return None


def _channel_reference(value: str) -> tuple[str, str]:
    """Return a channels.list lookup parameter and its value."""
    value = value.strip()
    video_id = _video_id(value)
    if video_id:
        return "video", video_id

    parsed = urlparse(value)
    path = parsed.path.strip("/") if parsed.netloc else value.strip("/")
    if path.startswith("channel/"):
        return "id", path.split("/", 1)[1]
    if path.startswith("@"):
        return "forHandle", path[1:]
    if value.startswith("UC"):
        return "id", value
    return "forHandle", value.removeprefix("@")


def _duration_seconds(value: str) -> int:
    match = re.fullmatch(
        r"P(?:(?P<days>\d+)D)?T(?:(?P<hours>\d+)H)?"
        r"(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?",
        value,
    )
    if not match:
        return 0
    parts = {key: int(number or 0) for key, number in match.groupdict().items()}
    return (
        parts["days"] * 86400
        + parts["hours"] * 3600
        + parts["minutes"] * 60
        + parts["seconds"]
    )


def _pro_mentions(title: str, aliases: dict[str, str]) -> list[tuple[int, str]]:
    mentions: list[tuple[int, str]] = []
    for alias, canonical in aliases.items():
        match = re.search(
            rf"(?<![\w]){re.escape(alias)}(?![\w])",
            title,
            re.IGNORECASE,
        )
        if match:
            mentions.append((match.start(), canonical))
    mentions.sort(key=lambda mention: mention[0])
    deduped: list[tuple[int, str]] = []
    seen: set[str] = set()
    for position, canonical in mentions:
        key = canonical.casefold()
        if key not in seen:
            seen.add(key)
            deduped.append((position, canonical))
    return deduped


def _structural_names(segment: str, aliases: dict[str, str]) -> list[str]:
    segment = re.split(
        r"\s+[|-]\s+|\b(?:avg|average|on stream|faceit|the new|pov|duo|trio|triple)\b",
        segment,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    names: list[str] = []
    for candidate in re.split(r"\s*(?:/|&|,|\+|\band\b)\s*", segment, flags=re.IGNORECASE):
        candidate = candidate.strip(" ()[]{}:;!?")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{2,24}", candidate):
            continue
        if candidate.casefold() in {
            "kills",
            "streams",
            "plays",
            "with",
            "voicecomms",
        }:
            continue
        canonical = aliases.get(candidate.casefold(), candidate)
        if canonical.casefold() not in {name.casefold() for name in names}:
            names.append(canonical)
    return names


def _merge_names(*groups: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for name in group:
            key = name.casefold()
            if key not in seen:
                seen.add(key)
                merged.append(name)
    return merged


def _player_features(title: str, aliases: dict[str, str] | None = None) -> dict:
    aliases = PRO_ALIASES if aliases is None else aliases
    vs_match = re.search(r"\bvs\.?\b", title, re.IGNORECASE)
    team_segment = title[: vs_match.start()] if vs_match else title
    opponent_segment = title[vs_match.end() :] if vs_match else ""
    team_mentions = _pro_mentions(team_segment, aliases)
    team_players = [name for _, name in team_mentions]
    known_opponents = [name for _, name in _pro_mentions(opponent_segment, aliases)]
    opponents = _merge_names(
        known_opponents,
        _structural_names(opponent_segment, aliases) if vs_match else [],
    )

    fallback = re.match(r"^[^\w]*([\w.-]+)", title, re.UNICODE)
    fallback_name = fallback.group(1) if fallback else None
    if fallback_name:
        fallback_name = aliases.get(fallback_name.casefold(), fallback_name)
    if team_mentions and (not fallback_name or fallback_name.isdigit() or team_mentions[0][0] <= 12):
        primary = team_mentions[0][1]
    else:
        primary = fallback_name
    has_party_signal = bool(
        re.search(
            r"\bw/|\bwith\b|\bduo\b|\btrio\b|\btriple\b|&",
            team_segment,
            re.IGNORECASE,
        )
    )
    structural_secondary: list[str] = []
    marker = re.search(r"\bw/|\bwith\b", team_segment, re.IGNORECASE)
    if marker:
        structural_secondary = _structural_names(team_segment[marker.end() :], aliases)
    elif "&" in team_segment:
        structural_secondary = _structural_names(team_segment.split("&", 1)[1], aliases)
    secondary = []
    if has_party_signal:
        secondary = _merge_names(
            [name for name in team_players if name.casefold() != str(primary).casefold()],
            structural_secondary,
        )
        secondary = [
            name for name in secondary if name.casefold() != str(primary).casefold()
        ]
    all_players = _merge_names(
        [primary] if primary else [],
        secondary,
        opponents,
        [name for _, name in _pro_mentions(title, aliases)],
    )
    party_size = 1 + len(secondary)
    party_type = {1: "solo", 2: "duo", 3: "trio"}.get(party_size, "squad")
    return {
        "primary_player": primary,
        "secondary_players": json.dumps(secondary, ensure_ascii=False),
        "secondary_player_count": len(secondary),
        "party_type": party_type,
        "opponent_pro_players": json.dumps(opponents, ensure_ascii=False),
        "all_pro_players": json.dumps(all_players, ensure_ascii=False),
    }


def _title_features(title: str) -> dict:
    lower = title.lower()
    kd = re.search(r"(?:\(|\b)(\d{1,2})\s*[-:]\s*(\d{1,2})(?:\)|\b)", title)
    elo = re.search(r"\b(\d{3,5})\s*e[l1]o\b", lower)
    detected_map = next(
        (
            map_name.replace(" ", "")
            for map_name in CS2_MAPS
            if re.search(rf"\b{re.escape(map_name)}\b", lower)
        ),
        None,
    )
    return {
        **_player_features(title),
        "kills": int(kd.group(1)) if kd else None,
        "deaths": int(kd.group(2)) if kd else None,
        "elo": int(elo.group(1)) if elo else None,
        "map": detected_map,
        "has_faceit": "faceit" in lower,
        "has_voicecomms": bool(re.search(r"voice\s*comms?|voicecomms?|\+voice", lower)),
        "has_input_overlay": bool(
            re.search(r"input overlay|keystrokes?|keyboard overlay", lower)
        ),
    }


async def _get(
    client: httpx.AsyncClient,
    endpoint: str,
    api_key: str,
    **params: object,
) -> dict:
    request_params = {
        key: value for key, value in params.items() if value is not None and value != ""
    }
    response = await client.get(
        f"{API_ROOT}/{endpoint}",
        params={**request_params, "key": api_key},
    )
    response.raise_for_status()
    return response.json()


async def _resolve_channel(
    client: httpx.AsyncClient,
    api_key: str,
    reference: str,
) -> dict:
    lookup, value = _channel_reference(reference)
    if lookup == "video":
        payload = await _get(client, "videos", api_key, part="snippet", id=value)
        items = payload.get("items", [])
        if not items:
            raise ValueError(f"Video not found: {reference}")
        value = items[0]["snippet"]["channelId"]
        lookup = "id"

    payload = await _get(
        client,
        "channels",
        api_key,
        part="snippet,statistics,contentDetails",
        **{lookup: value},
    )
    items = payload.get("items", [])
    if not items:
        raise ValueError(f"Channel not found: {reference}")
    item = items[0]
    snippet = item["snippet"]
    statistics = item.get("statistics", {})
    return {
        "channel_id": item["id"],
        "channel": snippet["title"],
        "channel_description": snippet.get("description", ""),
        "channel_created_at": snippet.get("publishedAt"),
        "channel_country": snippet.get("country"),
        "custom_url": snippet.get("customUrl"),
        "subscribers": int(statistics.get("subscriberCount", 0)),
        "hidden_subscriber_count": bool(statistics.get("hiddenSubscriberCount", False)),
        "channel_views": int(statistics.get("viewCount", 0)),
        "channel_videos": int(statistics.get("videoCount", 0)),
        "uploads_playlist": item["contentDetails"]["relatedPlaylists"]["uploads"],
    }


async def _recent_video_ids(
    client: httpx.AsyncClient,
    api_key: str,
    playlist_id: str,
    cutoff: datetime,
    max_videos: int,
    skip_ids: set[str] | None = None,
) -> list[str]:
    ids: list[str] = []
    page_token: str | None = None
    reached_cutoff = False
    skip_ids = skip_ids or set()
    while len(ids) < max_videos and not reached_cutoff:
        payload = await _get(
            client,
            "playlistItems",
            api_key,
            part="contentDetails",
            playlistId=playlist_id,
            maxResults=min(50, max_videos - len(ids)),
            pageToken=page_token or "",
        )
        for item in payload.get("items", []):
            published = datetime.fromisoformat(
                item["contentDetails"]["videoPublishedAt"].replace("Z", "+00:00")
            )
            if published < cutoff:
                reached_cutoff = True
                break
            video_id = item["contentDetails"]["videoId"]
            if video_id in skip_ids:
                continue
            ids.append(video_id)
        page_token = payload.get("nextPageToken")
        if not page_token:
            break
    return ids


def _metric_row(
    item: dict,
    channel: dict,
    captured_at: datetime,
) -> dict:
    snippet = item["snippet"]
    statistics = item.get("statistics", {})
    published = datetime.fromisoformat(snippet["publishedAt"].replace("Z", "+00:00"))
    age_days = max((captured_at - published).total_seconds() / 86400, 1 / 24)
    views = int(statistics.get("viewCount", 0))
    likes = int(statistics.get("likeCount", 0))
    comments = int(statistics.get("commentCount", 0))
    subscribers = channel["subscribers"]
    title = snippet["title"]
    description = snippet.get("description", "")
    tags = snippet.get("tags", [])
    content = item.get("contentDetails", {})
    status = item.get("status", {})
    thumbnails = snippet.get("thumbnails", {})
    thumbnail = next(
        (
            thumbnails[size]["url"]
            for size in ("maxres", "standard", "high", "medium", "default")
            if size in thumbnails
        ),
        None,
    )
    hashtags = sorted(set(re.findall(r"(?<!\w)#[\w-]+", f"{title}\n{description}")))
    return {
        "captured_at": captured_at.isoformat(),
        "channel_id": channel["channel_id"],
        "channel": channel["channel"],
        "subscribers": subscribers,
        "video_id": item["id"],
        "title": title,
        "description": description,
        "tags": json.dumps(tags, ensure_ascii=False),
        "hashtags": " ".join(hashtags),
        "category_id": snippet.get("categoryId"),
        "default_language": snippet.get("defaultLanguage"),
        "default_audio_language": snippet.get("defaultAudioLanguage"),
        "published_at": published.isoformat(),
        "publish_weekday": published.strftime("%A"),
        "publish_hour_utc": published.hour,
        "age_days": round(age_days, 3),
        "duration_seconds": _duration_seconds(content.get("duration", "")),
        "definition": content.get("definition"),
        "captioned": content.get("caption") == "true",
        "licensed_content": content.get("licensedContent"),
        "embeddable": status.get("embeddable"),
        "made_for_kids": status.get("madeForKids"),
        "topic_categories": json.dumps(
            item.get("topicDetails", {}).get("topicCategories", []),
            ensure_ascii=False,
        ),
        "thumbnail_url": thumbnail,
        "views": views,
        "likes": likes,
        "comments": comments,
        "views_per_day": round(views / age_days, 2),
        "views_per_subscriber": round(views / subscribers, 4) if subscribers else None,
        "like_rate": round(likes / views, 4) if views else None,
        "comment_rate": round(comments / views, 4) if views else None,
        "title_characters": len(title),
        "title_words": len(title.split()),
        "description_characters": len(description),
        "tag_count": len(tags),
        **_title_features(title),
        "url": f"https://www.youtube.com/watch?v={item['id']}",
    }


async def collect(
    references: list[str],
    api_key: str,
    days: int,
    max_videos: int,
    skip_ids: set[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    captured_at = datetime.now(timezone.utc)
    cutoff = captured_at - timedelta(days=days)
    channels: list[dict] = []
    rows: list[dict] = []

    async with httpx.AsyncClient(timeout=30) as client:
        seen: set[str] = set()
        for reference in references:
            channel = await _resolve_channel(client, api_key, reference)
            if channel["channel_id"] in seen:
                continue
            seen.add(channel["channel_id"])
            channels.append(channel)
            ids = await _recent_video_ids(
                client,
                api_key,
                channel["uploads_playlist"],
                cutoff,
                max_videos,
                skip_ids=skip_ids,
            )
            for start in range(0, len(ids), 50):
                payload = await _get(
                    client,
                    "videos",
                    api_key,
                    part="snippet,statistics,contentDetails,status,topicDetails",
                    id=",".join(ids[start : start + 50]),
                )
                rows.extend(
                    _metric_row(item, channel, captured_at)
                    for item in payload.get("items", [])
                )
    rows.sort(key=lambda row: (row["channel"], row["published_at"]), reverse=True)
    return channels, rows


def _summaries(channels: list[dict], rows: list[dict]) -> list[dict]:
    summaries = []
    for channel in channels:
        videos = [row for row in rows if row["channel_id"] == channel["channel_id"]]
        views = [row["views"] for row in videos]
        velocity = [row["views_per_day"] for row in videos]
        top = max(videos, key=lambda row: row["views"], default=None)
        summaries.append(
            {
                **{key: value for key, value in channel.items() if key != "uploads_playlist"},
                "sample_videos": len(videos),
                "median_views": round(median(views), 2) if views else 0,
                "median_views_per_day": round(median(velocity), 2) if velocity else 0,
                "top_video": top["title"] if top else None,
                "top_video_views": top["views"] if top else 0,
                "top_video_url": top["url"] if top else None,
            }
        )
    return summaries


def _write_csv(path: Path, rows: list[dict], fieldnames: tuple[str, ...] | list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def load_history(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _captured_at(row: dict) -> str:
    return str(row.get("captured_at") or "")


def upsert_history_rows(existing: list[dict], incoming: list[dict]) -> list[dict]:
    """Keep one row per video_id, preferring the newest capture."""
    by_id: dict[str, dict] = {}
    for row in existing:
        video_id = row.get("video_id")
        if not video_id:
            continue
        prev = by_id.get(video_id)
        if prev is None or _captured_at(row) >= _captured_at(prev):
            by_id[video_id] = row
    for row in incoming:
        video_id = row.get("video_id")
        if not video_id:
            continue
        prev = by_id.get(video_id)
        if prev is None or _captured_at(row) >= _captured_at(prev):
            by_id[video_id] = row
    rows = list(by_id.values())
    rows.sort(key=lambda row: (row.get("published_at") or "", row.get("video_id") or ""))
    return rows


def write_history(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_csv(path, rows, VIDEO_FIELDS)


def stale_video_ids(
    rows: list[dict],
    now: datetime,
    refresh_days: int,
) -> set[str]:
    """Video IDs already stored and older than the refresh window."""
    cutoff = now - timedelta(days=refresh_days)
    stale: set[str] = set()
    for row in rows:
        video_id = row.get("video_id")
        published = row.get("published_at")
        if not video_id or not published:
            continue
        try:
            stamp = datetime.fromisoformat(str(published).replace("Z", "+00:00"))
        except ValueError:
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        if stamp < cutoff:
            stale.add(video_id)
    return stale


def write_outputs(outdir: Path, channels: list[dict], rows: list[dict]) -> list[Path]:
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    videos_path = outdir / f"videos_{stamp}.csv"
    summary_path = outdir / f"channels_{stamp}.json"
    history_path = outdir / "video_history.csv"
    summaries = _summaries(channels, rows)

    _write_csv(videos_path, rows, VIDEO_FIELDS)
    summary_path.write_text(json.dumps(summaries, indent=2), encoding="utf-8")
    merged = upsert_history_rows(load_history(history_path), rows)
    write_history(history_path, merged)
    return [videos_path, summary_path, history_path]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Collect public performance data from CS2 POV YouTube channels."
    )
    parser.add_argument(
        "channels",
        nargs="*",
        help="Channel IDs, @handles, channel URLs, or a video URL",
    )
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--max-videos", type=int, default=2000)
    parser.add_argument("--outdir", type=Path, default=ROOT / "exports" / "pov_market")
    args = parser.parse_args()

    if args.days < 1 or args.max_videos < 1:
        parser.error("--days and --max-videos must be positive")
    if not settings.youtube_api_key:
        parser.error("YOUTUBE_API_KEY is not configured in .env")

    references = args.channels or list(DEFAULT_CHANNELS)
    channels, rows = asyncio.run(
        collect(references, settings.youtube_api_key, args.days, args.max_videos)
    )
    paths = write_outputs(args.outdir, channels, rows)
    print(f"Collected {len(rows)} videos from {len(channels)} channels.")
    for path in paths:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
