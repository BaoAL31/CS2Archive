"""YouTube Shorts collection through the official Data API v3.

Supported because the configured API key covers ``channels.list``,
``playlistItems.list`` and ``videos.list``. Shorts *format* is proven by the
public ``/shorts/<id>`` route **plus a Shorts-specific marker in the response**:

* 200 with a canonical ``/shorts/<id>`` link or ``isShortsEligible: true``
  → verified Short (confidence 0.98).
* redirect to ``/watch?v=<id>`` → verified long-form (0.95).
* 200 without those markers (consent/interstitial/anything else) → unknown.
* duration only → ``candidate_short``/``candidate_long`` at 0.4, never ``short``.

Not exposed by the Data API: shares, saves, plays, impressions, thumbnail CTR,
and any historical "views at 24h" value. Those stay NULL.
"""

from __future__ import annotations

import re

from cs2archive.shorts.social.models import (
    FORMAT_CANDIDATE_SHORT, FORMAT_LONG, FORMAT_SHORT, FORMAT_UNKNOWN,
    CollectedPost, MetricRecord, PlatformStatus, PostRecord,
)
from cs2archive.shorts.social.safety import parse_int, parse_iso_duration, parse_timestamp

API = "https://www.googleapis.com/youtube/v3"
SHORTS_URL = "https://www.youtube.com/shorts/{video_id}"
VIDEO_URL = "https://www.youtube.com/watch?v={video_id}"
UPLOAD_PLAYLIST_MAX = 50
VIDEO_BATCH = 50

REQUIREMENTS = ["YOUTUBE_API_KEY (YouTube Data API v3, read scope)"]
_SHORTS_CANONICAL_RE = re.compile(
    r'<link[^>]+rel="canonical"[^>]+href="https://www\.youtube\.com/shorts/([A-Za-z0-9_-]{6,})"')
_IS_SHORTS_RE = re.compile(r'"isShortsEligible"\s*:\s*true')
_CONSENT_RE = re.compile(r"(consent\.youtube\.com|before you continue)", re.I)


def platform_status(config, env) -> PlatformStatus:
    if not env.youtube_api_key:
        return PlatformStatus(
            platform="youtube", status="unavailable", mode=config.mode or "api",
            reason="no YouTube Data API key configured", requirements=REQUIREMENTS)
    return PlatformStatus(
        platform="youtube", status="supported", mode=config.mode or "api",
        reason="official Data API v3 (uploads playlist + videos.list + marker-validated "
               "Shorts route probe)",
        requirements=REQUIREMENTS)


def _handle(value: str) -> str:
    return value if value.startswith("@") else f"@{value.lstrip('@')}"


def resolve_channel(ctx, creator: str) -> tuple[dict | None, int | None]:
    if creator.startswith("UC") and len(creator) == 24:
        params = {"part": "snippet,contentDetails,statistics", "id": creator,
                  "key": ctx.env.youtube_api_key}
        name = f"channel_{creator}"
    else:
        params = {"part": "snippet,contentDetails,statistics",
                  "forHandle": _handle(creator), "key": ctx.env.youtube_api_key}
        name = f"channel_{creator.lstrip('@')}"
    result = ctx.try_fetch(f"{API}/channels", name=name, params=params,
                           quota_units=1.0, stage="youtube.channels")
    if result is None:
        return None, None
    items = (result.payload or {}).get("items") or []
    return (items[0] if items else None), result.raw_id


def _probe_transform(video_id: str):
    def transform(response) -> dict:
        text = response.text or ""
        canonical = None
        match = _SHORTS_CANONICAL_RE.search(text)
        if match and match.group(1) == video_id:
            canonical = match.group(1)
        return {
            "http_status": response.status_code,
            "location": response.headers.get("location"),
            "canonical_shorts_id": canonical,
            "is_shorts_eligible": bool(_IS_SHORTS_RE.search(text)),
            "looks_like_consent": bool(_CONSENT_RE.search(text[:20000])),
        }
    return transform


def _shorts_probe(ctx, video_id: str) -> dict:
    """Prove the format from YouTube's own route + response markers."""
    url = SHORTS_URL.format(video_id=video_id)
    try:
        result = ctx.try_fetch(
            url, name=f"shorts_probe_{video_id}", allow_status=(200, 301, 302, 303),
            quota_units=0.0, stage="youtube.shorts_probe", follow_redirects=False,
            transform=_probe_transform(video_id))
    except Exception as exc:  # noqa: BLE001 - probe failure must not drop the post
        return {"method": "shorts_route", "ok": False, "verdict": "unknown",
                "reason": f"probe failed: {type(exc).__name__}"}
    if result is None:
        return {"method": "shorts_route", "ok": False, "verdict": "unknown",
                "reason": "budget exhausted"}
    payload = result.payload or {}
    status = int(payload.get("http_status") or 0)
    location = str(payload.get("location") or "")
    base = {"method": "shorts_route", "http_status": status, "url": url,
            "raw_id": result.raw_id}
    if status == 200:
        if payload.get("canonical_shorts_id") or payload.get("is_shorts_eligible"):
            return {**base, "method": "shorts_route_200_marker",
                    "verdict": "short", "confidence": 0.98, "verified": True,
                    "evidence": {"canonical": payload.get("canonical_shorts_id"),
                                 "is_shorts_eligible": payload.get("is_shorts_eligible")}}
        if payload.get("looks_like_consent"):
            return {**base, "verdict": "unknown", "confidence": 0.0,
                    "verified": False, "reason": "consent/interstitial page"}
        return {**base, "verdict": "unknown", "confidence": 0.0, "verified": False,
                "reason": "200 without a Shorts marker"}
    if status in (301, 302, 303):
        if f"/watch" in location and video_id in location:
            return {**base, "method": "shorts_route_redirect_watch",
                    "verdict": "long", "confidence": 0.95, "verified": True,
                    "location": location}
        return {**base, "verdict": "unknown", "confidence": 0.0, "verified": False,
                "reason": f"redirect unrelated to /watch: {location[:120]}"}
    return {**base, "verdict": "unknown", "confidence": 0.0, "verified": False,
            "reason": f"unexpected status {status}"}


def assess_format(duration: float | None, probe: dict) -> tuple[str, float, dict, bool]:
    evidence = {"duration_seconds": duration, "shorts_probe": probe,
                "verified": bool(probe.get("verified"))}
    if probe.get("verdict") == "short" and probe.get("verified"):
        evidence["method"] = probe.get("method")
        return FORMAT_SHORT, 0.98, evidence, True
    if probe.get("verdict") == "long" and probe.get("verified"):
        evidence["method"] = probe.get("method")
        return FORMAT_LONG, 0.95, evidence, True
    if duration is not None:
        label = FORMAT_CANDIDATE_SHORT if duration <= 180 else FORMAT_LONG
        return label, 0.4, {**evidence,
                            "note": "duration-only candidate; not verified"}, False
    return FORMAT_UNKNOWN, 0.0, evidence, False


def _video_batch(ctx, video_ids: list[str], index: int) -> tuple[dict[str, dict], int | None]:
    result = ctx.try_fetch(
        f"{API}/videos", name=f"videos_{index:03d}",
        params={"part": "snippet,contentDetails,statistics",
                "id": ",".join(video_ids), "key": ctx.env.youtube_api_key},
        quota_units=1.0, stage="youtube.videos")
    if result is None:
        return {}, None
    items = {str(item.get("id")): item
             for item in (result.payload or {}).get("items") or []}
    return items, result.raw_id


def collect(ctx) -> list[CollectedPost]:
    config = ctx.config
    collected: list[CollectedPost] = []
    seen: set[str] = set()
    window_start = ctx.window_start
    channel_stats: dict[str, dict] = {}
    candidates: list[dict] = []

    def add_candidate(video_id: str, snippet: dict | None, *, must_include: bool,
                      raw_id: int | None = None) -> None:
        if not video_id or video_id in seen:
            return
        seen.add(video_id)
        candidates.append({"video_id": video_id, "snippet": snippet or {},
                           "must_include": must_include, "raw_id": raw_id})

    # Explicit seeds always come first: they make "ordinary/poor clip" checks cheap.
    for video_id in config.video_ids:
        add_candidate(str(video_id), None, must_include=True)

    for query in config.extra.get("search_queries") or []:
        if ctx.quota_units + 100 > ctx.quota_limit:
            ctx.counters.notes["budget_exhausted"] = True
            break
        result = ctx.try_fetch(
            f"{API}/search", name=f"search_{abs(hash(query)) % 10**8}",
            params={"part": "snippet", "q": str(query), "type": "video",
                    "order": "date", "maxResults": 50,
                    "key": ctx.env.youtube_api_key},
            quota_units=100.0, stage="youtube.search")
        if result is None:
            break
        for item in (result.payload or {}).get("items") or []:
            vid = str(((item.get("id") or {}).get("videoId")) or "")
            add_candidate(vid, item.get("snippet") or {}, must_include=False,
                          raw_id=result.raw_id)

    creators = list(config.creators)
    discovery = config.extra.get("discovery") or {}
    discovery_info = None
    if discovery.get("enabled"):
        from cs2archive.shorts.social.discovery import discover_youtube

        discovery_info = discover_youtube(
            ctx, queries=[str(q) for q in discovery.get("queries") or []],
            max_creators=int(discovery.get("max_creators") or 12),
            min_relevance=float(discovery.get("min_relevance") or 0.5),
            window_start=window_start)
        for row in discovery_info["selected"]:
            if row["creator_id"] not in creators:
                creators.append(row["creator_id"])
    creator_limit = int(discovery.get("max_creators") or 0) if discovery_info else 0
    if creator_limit:
        creators = creators[:creator_limit]
    per_creator_max = int(config.extra.get("per_creator_max") or 0)
    own_channel_id = str(config.extra.get("own_channel_id") or "")
    # The home channel is paginated completely (its full catalogue is the
    # reference population for our own analytics); competitors stay bounded.
    own_max_pages = int(config.extra.get("own_max_pages")
                        or config.max_pages)

    creator_stats: dict[str, dict] = {}
    for creator in creators:
        channel, channel_raw_id = resolve_channel(ctx, creator)
        if channel_raw_id is None:
            break
        if not channel:
            ctx.store.log_error(
                run_id=ctx.run_id, platform="youtube", stage="youtube.channels",
                code="creator_not_found", message=f"channel not found: {creator}",
                counters=ctx.counters)
            continue
        channel_id = str(channel.get("id") or "")
        if channel_id in creator_stats:
            continue  # the same channel resolved twice (handle + discovered id)
        channel_stats[channel_id] = channel.get("statistics") or {}
        uploads = (((channel.get("contentDetails") or {}).get("relatedPlaylists")) or {}
                   ).get("uploads")
        if not uploads:
            continue
        page_token = None
        pages_for_creator = 0
        stopped_by_window = False
        page_limit = own_max_pages if channel_id == own_channel_id else config.max_pages
        page_limit = max(0, page_limit)
        for page in range(page_limit):
            params = {"part": "snippet,contentDetails", "playlistId": uploads,
                      "maxResults": UPLOAD_PLAYLIST_MAX,
                      "key": ctx.env.youtube_api_key}
            if page_token:
                params["pageToken"] = page_token
            result = ctx.try_fetch(
                f"{API}/playlistItems", name=f"uploads_{channel_id}_p{page:02d}",
                params=params, quota_units=1.0, stage="youtube.playlistItems")
            if result is None:
                break
            ctx.counters.pages_fetched += 1
            pages_for_creator += 1
            items = (result.payload or {}).get("items") or []
            if not items:
                break
            oldest_on_page = None
            for item in items:
                snippet = item.get("snippet") or {}
                vid = str(((item.get("contentDetails") or {}).get("videoId")) or "")
                published = parse_timestamp(snippet.get("publishedAt"))
                if published and oldest_on_page is None:
                    # First item of the uploads playlist is the NEWEST upload.
                    oldest_on_page = published
                add_candidate(vid, snippet, must_include=False, raw_id=result.raw_id)
            page_token = (result.payload or {}).get("nextPageToken")
            if not page_token:
                break
            if (window_start and oldest_on_page and oldest_on_page < window_start
                    and channel_id != own_channel_id):
                # The home channel is ingested completely (own_max_pages); the
                # window only bounds competitor catalogues.
                stopped_by_window = True
                break
            if len(candidates) >= config.max_posts * 3:
                break
        creator_stats.setdefault(channel_id, {
            "creator_id": channel_id,
            "handle": creator,
            "name": (channel.get("snippet") or {}).get("title"),
            "pages_fetched": pages_for_creator,
            "page_limit": page_limit,
            "own_channel": channel_id == own_channel_id,
            "next_page_exists": bool(page_token),
            "stopped_by_window": stopped_by_window,
            "uploads_playlist": uploads,
            "candidates": 0,
            "collected": 0,
            "filtered_outside_window": 0,
            "newest_upload_at": None,
            "oldest_upload_at": None,
            "truncated_by": None,
        })

    video_ids = [candidate["video_id"] for candidate in candidates]
    payloads: dict[str, dict] = {}
    batch_raw: dict[str, int | None] = {}
    for start in range(0, len(video_ids), VIDEO_BATCH):
        batch = video_ids[start:start + VIDEO_BATCH]
        items, raw_id = _video_batch(ctx, batch, start // VIDEO_BATCH)
        if not items and raw_id is None:
            break
        payloads.update(items)
        for video_id in batch:
            batch_raw[video_id] = raw_id

    for candidate in candidates:
        if len(collected) >= config.max_posts:
            break
        video_id = candidate["video_id"]
        item = payloads.get(video_id)
        if not item:
            ctx.store.log_error(
                run_id=ctx.run_id, platform="youtube", stage="youtube.videos",
                code="video_unavailable", message=f"video not returned: {video_id}",
                counters=ctx.counters)
            continue
        snippet = item.get("snippet") or {}
        creator_id = str(snippet.get("channelId") or "")
        covered = creator_stats.get(creator_id)
        if covered is not None:
            covered["candidates"] += 1
            # The home channel's bound is own_max_pages (complete catalogue); the
            # balanced per-creator cap only exists to keep competitors comparable.
            if (per_creator_max and covered["collected"] >= per_creator_max
                    and creator_id != own_channel_id):
                covered["truncated_by"] = "per_creator_cap"
                continue
        details = item.get("contentDetails") or {}
        stats = item.get("statistics") or {}
        published_at = parse_timestamp(snippet.get("publishedAt"))
        if covered is not None and published_at:
            if covered["newest_upload_at"] is None or \
                    published_at > covered["newest_upload_at"]:
                covered["newest_upload_at"] = published_at
            if covered["oldest_upload_at"] is None or \
                    published_at < covered["oldest_upload_at"]:
                covered["oldest_upload_at"] = published_at
        if (window_start and published_at and not candidate["must_include"]
                and creator_id != own_channel_id):
            if published_at < window_start:
                if covered is not None:
                    covered["filtered_outside_window"] += 1
                continue
        if ctx.window_end and published_at and published_at > ctx.window_end:
            continue
        duration = parse_iso_duration(details.get("duration"))
        stored = ctx.store.existing_format("youtube", video_id)
        probe = None
        if stored and (stored["format_evidence"].get("shorts_probe") or {}).get(
                "verdict") in {"short", "long"}:
            # Format does not change; reuse the recorded probe instead of
            # re-spending requests on a post we already classified.
            evidence = dict(stored["format_evidence"])
            label = stored["format_label"]
            confidence = float(stored["format_confidence"] or 0.0)
            verified = bool(evidence.get("verified"))
            evidence["probe_reused"] = True
            probe = evidence.get("shorts_probe") or {}
        else:
            probe = _shorts_probe(ctx, video_id)
            label, confidence, evidence, verified = assess_format(duration, probe)
        evidence["verified"] = verified
        channel_id = str(snippet.get("channelId") or "")
        followers = parse_int((channel_stats.get(channel_id) or {}).get("subscriberCount"))
        unavailable = ["plays", "shares", "saves"]
        if "likeCount" not in stats:
            unavailable.append("likes")
        if "commentCount" not in stats:
            unavailable.append("comments")
        if followers is None:
            unavailable.append("followers")
        post = PostRecord(
            platform="youtube",
            post_id=video_id,
            url=VIDEO_URL.format(video_id=video_id),
            creator_id=channel_id or None,
            creator_name=snippet.get("channelTitle"),
            caption=snippet.get("title"),
            published_at=published_at,
            duration_seconds=duration,
            media_kind="video",
            format_label=label,
            format_confidence=confidence,
            format_evidence=evidence,
            notes={"shorts_route": probe.get("method"),
                   "probe_reason": probe.get("reason"),
                   "probe_reused": bool(evidence.get("probe_reused"))},
        )
        metrics = MetricRecord(
            views=parse_int(stats.get("viewCount")),
            likes=parse_int(stats.get("likeCount")),
            comments=parse_int(stats.get("commentCount")),
            followers=followers,
            metric_source="youtube_data_api_v3",
            unavailable=unavailable,
            extra={"definition": details.get("definition"),
                   "caption_status": details.get("caption")})
        collected.append(CollectedPost(
            post=post, metrics=metrics, observed_at=ctx.store_clock(),
            raw_id=batch_raw.get(video_id) or candidate.get("raw_id")))
        if covered is not None:
            covered["collected"] += 1

    for covered in creator_stats.values():
        if covered["truncated_by"] is None:
            if covered["collected"] == 0 and covered["filtered_outside_window"]:
                # Everything the channel publishes is older than the sweep window.
                covered["truncated_by"] = "inactive_outside_window"
            elif covered["next_page_exists"] and \
                    covered["pages_fetched"] >= covered["page_limit"]:
                covered["truncated_by"] = "page_limit"
            elif covered["stopped_by_window"]:
                covered["truncated_by"] = "collection_window"
            elif (per_creator_max and covered["collected"] >= per_creator_max
                  and covered["creator_id"] != own_channel_id):
                covered["truncated_by"] = "per_creator_cap"
            else:
                covered["truncated_by"] = "full_catalog"
    ctx.counters.notes["creator_coverage"] = sorted(
        creator_stats.values(), key=lambda row: row["creator_id"])
    if discovery_info is not None:
        ctx.counters.notes["discovery"] = {
            "run_id": discovery_info["run_id"],
            "candidates": len(discovery_info["scored"]),
            "accepted": len(discovery_info["selected"]),
            "errors": discovery_info["errors"],
        }
    ctx.counters.posts_seen += len(candidates)
    return collected
