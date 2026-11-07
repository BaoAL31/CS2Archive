"""Competitor discovery with persisted provenance.

YouTube is the only platform where public APIs let us *find* creators:
``search.list(type=channel)`` followed by ``channels.list`` and one uploads
page per candidate, scored by how many recent upload titles look like CS2
content. Every candidate — accepted or rejected — is persisted with its evidence
so the selection is auditable and repeatable.

Instagram/TikTok/Reddit have no permitted creator-search path here; discovery for
them is explicitly unavailable (configured handles only).
"""

from __future__ import annotations

import math
import re

from cs2archive.shorts.social.safety import parse_int, parse_timestamp

API = "https://www.googleapis.com/youtube/v3"
YOUTUBE_SEARCH_UNITS = 100.0
CHANNELS_BATCH = 50

CS2_TERMS = re.compile(
    r"(?:^|[\s#_\-])(cs2|counter[- ]?strike|counterstrike|csgo|cs:|pov|highlight|"
    r"clutch|ace|4k|3k|nuke|mirage|dust2?|inferno|ancient|overpass|vertigo|train|"
    r"anubis|cache|faceit|hltv|major|iem|blast|esl)(?:$|[\s#_\-!.,])",
    re.I,
)
SHORT_TERMS = re.compile(r"(?:#shorts|\bshorts?\b|#cs2|#counterstrike)", re.I)


def _uploads_of(channel: dict) -> str | None:
    playlists = ((channel.get("contentDetails") or {}).get("relatedPlaylists") or {})
    return playlists.get("uploads")


def relevance_of_titles(titles: list[str]) -> tuple[float, int]:
    if not titles:
        return 0.0, 0
    matches = sum(1 for title in titles if CS2_TERMS.search(str(title or "")))
    return matches / len(titles), matches


def discover_youtube(ctx, *, queries: list[str], max_creators: int,
                     min_relevance: float = 0.5,
                     window_start: str | None = None) -> dict:
    """Search, verify and persist candidate channels. Returns the selection."""
    if window_start is None:
        window_start = ctx.window_start
    window_start = window_start or "1970-01-01T00:00:00Z"
    search_results: list[dict] = []
    query_errors: list[str] = []
    for query in queries[:4]:
        if ctx.quota_units + YOUTUBE_SEARCH_UNITS > ctx.quota_limit:
            query_errors.append(f"quota budget would be exceeded for {query!r}")
            break
        result = ctx.try_fetch(
            f"{API}/search", name=f"discover_search_{abs(hash(query)) % 10**8}",
            params={"part": "snippet", "q": query, "type": "channel",
                    "maxResults": 50, "key": ctx.env.youtube_api_key},
            quota_units=YOUTUBE_SEARCH_UNITS, stage="youtube.discovery.search")
        if result is None:
            query_errors.append("request budget exhausted")
            break
        for item in (result.payload or {}).get("items") or []:
            channel_id = str((item.get("id") or {}).get("channelId") or "")
            snippet = item.get("snippet") or {}
            if channel_id:
                search_results.append({"channel_id": channel_id,
                                       "title": snippet.get("title"),
                                       "description": snippet.get("description"),
                                       "query": query})

    unique: dict[str, dict] = {}
    for row in search_results:
        unique.setdefault(row["channel_id"], row)
    channel_ids = list(unique)[:max_creators * 3]
    channels: dict[str, dict] = {}
    for start in range(0, len(channel_ids), CHANNELS_BATCH):
        batch = channel_ids[start:start + CHANNELS_BATCH]
        result = ctx.try_fetch(
            f"{API}/channels", name=f"discover_channels_{start // CHANNELS_BATCH:03d}",
            params={"part": "snippet,contentDetails,statistics", "id": ",".join(batch),
                    "key": ctx.env.youtube_api_key},
            quota_units=1.0, stage="youtube.discovery.channels")
        if result is None:
            break
        for item in (result.payload or {}).get("items") or []:
            channels[str(item.get("id"))] = item

    scored: list[dict] = []
    for channel_id, channel in channels.items():
        uploads = _uploads_of(channel)
        if not uploads:
            continue
        page = ctx.try_fetch(
            f"{API}/playlistItems", name=f"discover_recent_{channel_id}",
            params={"part": "snippet", "playlistId": uploads, "maxResults": 20,
                    "key": ctx.env.youtube_api_key},
            quota_units=1.0, stage="youtube.discovery.recent")
        if page is None:
            break
        items = (page.payload or {}).get("items") or []
        titles = [str((item.get("snippet") or {}).get("title") or "") for item in items]
        recent_times = [parse_timestamp((item.get("snippet") or {}).get("publishedAt"))
                        for item in items]
        recent_times = [stamp for stamp in recent_times if stamp]
        newest_upload_at = max(recent_times) if recent_times else None
        relevance, matches = relevance_of_titles(titles)
        stats = channel.get("statistics") or {}
        snippet = channel.get("snippet") or {}
        own = str(ctx.config.extra.get("own_channel_id") or "")
        is_own = channel_id == own or str(snippet.get("title") or "").lower() in {
            str(ctx.config.extra.get("own_channel_handle") or "").lower(),
            "@cs2povarchive", "cs2 pov archive"}
        subs = parse_int(stats.get("subscriberCount"))
        video_count = parse_int(stats.get("videoCount")) or 0
        fresh = (newest_upload_at is not None
                 and newest_upload_at >= window_start)
        # Activity inside the collection window is what makes a channel
        # collectable; a dormant channel with CS2 titles is not a competitor
        # we can measure recent performance for.
        score = (relevance * 100.0
                 + (25.0 if fresh else 0.0)
                 + min(20.0, math.log10(max(video_count, 1)) * 5.0)
                 + min(10.0, math.log10(max(subs or 1, 1)) * 2.0))
        scored.append({
            "platform": "youtube",
            "creator_id": channel_id,
            "handle": snippet.get("customUrl"),
            "name": snippet.get("title"),
            "score": round(score, 3),
            "relevance": round(relevance, 4),
            "matched_titles": matches,
            "recent_titles": titles[:8],
            "subscribers": subs,
            "video_count": video_count,
            "uploads_playlist": uploads,
            "is_own": is_own,
            "query": unique.get(channel_id, {}).get("query"),
            "newest_upload_at": newest_upload_at,
            "titles_in_window": sum(1 for stamp in recent_times
                                    if stamp and stamp >= window_start),
            "recent_items_sampled": len(recent_times),
            "active_in_window": fresh,
        })

    scored.sort(key=lambda row: (-row["score"], row["creator_id"]))
    from cs2archive.shorts.social.safety import iso

    run_id = ctx.store.unique_run_id(
        f"discovery-youtube-{iso(ctx.clock()).replace(':', '')[:15]}")
    require_active = bool(ctx.config.extra.get(
        "discovery", {}).get("require_recent_activity", False))
    eligible = [row for row in scored
                if not row["is_own"] and row["relevance"] >= min_relevance
                and (row["active_in_window"] or not require_active)]
    selected = eligible[:max_creators]
    selected_ids = {row["creator_id"] for row in selected}
    for row in scored:
        ctx.store.upsert_discovered_creator(
            platform=row["platform"], creator_id=row["creator_id"],
            handle=row["handle"], name=row["name"], score=row["score"],
            evidence=row, run_id=run_id,
            accepted=row["creator_id"] in selected_ids)
    ctx.store.record_discovery_run(
        run_id=run_id, platform="youtube", queries=queries,
        candidates=len(scored), accepted=len(selected),
        units_spent=ctx.quota_units, notes={
            "min_relevance": min_relevance,
            "search_results": len(search_results),
            "errors": query_errors,
            "rejected_low_relevance": sum(
                1 for row in scored if row["relevance"] < min_relevance),
            "rejected_own": sum(1 for row in scored if row["is_own"]),
            "rejected_dormant": sum(
                1 for row in scored if row["relevance"] >= min_relevance
                and not row["is_own"] and not row["active_in_window"]),
            "require_recent_activity": require_active,
        })
    ctx.counters.notes["discovery"] = {
        "run_id": run_id, "candidates": len(scored),
        "accepted": len(selected), "errors": query_errors,
        "units_spent": ctx.quota_units,
    }
    return {"run_id": run_id, "selected": selected, "scored": scored,
            "errors": query_errors}
