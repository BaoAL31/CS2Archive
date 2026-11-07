"""TikTok evidence paths.

What actually works without private access:

* **oEmbed** for an explicit public video URL (author, caption, thumbnail size).
  No metrics are exposed, so views/likes/comments/shares/saves stay NULL.
* **Authorized export import** (``imports`` in the sweep config) for a TikTok
  account-data export the operator owns.

What does NOT work and is never faked:

* Display API ``/v2/video/list/`` needs the ``video.list`` scope. The cached
  token here has ``user.info.basic,video.upload`` only.
* TikTok Research API needs an approved research application.
* The legacy "public discovery" endpoints are unofficial and out of scope.
"""

from __future__ import annotations

import re

from cs2archive.shorts.social.models import (
    FORMAT_SHORT, FORMAT_UNKNOWN, CollectedPost, MetricRecord, PlatformStatus, PostRecord,
)
from cs2archive.shorts.social.safety import parse_int

OEMBED = "https://www.tiktok.com/oembed"
VIDEO_ID_RE = re.compile(r"/video/(\d{6,})")
REQUIREMENTS = [
    "public video URL (oEmbed) — metadata only, no metrics",
    "or TIKTOK_CLIENT_KEY/SECRET with the video.list scope for the Display API",
    "or an authorized TikTok account-data export (imports)",
    "or approved TikTok Research API credentials",
]
UNAVAILABLE = ["views", "plays", "likes", "comments", "shares", "saves", "followers",
               "duration_seconds"]


def platform_status(config, env) -> PlatformStatus:
    has_urls = bool(config.video_urls)
    has_export = bool((config.extra or {}).get("export_paths"))
    if has_urls or has_export:
        return PlatformStatus(
            platform="tiktok", status="partial", mode="oembed",
            reason="public oEmbed / authorized export only: no metrics API access "
                   "(token lacks video.list; Research API not approved)",
            requirements=REQUIREMENTS)
    return PlatformStatus(
        platform="tiktok", status="blocked", mode="oembed",
        reason="no public video URLs or authorized export configured, and the "
               "cached TikTok token scope is user.info.basic,video.upload (no video.list)",
        requirements=REQUIREMENTS)


def collect(ctx) -> list[CollectedPost]:
    collected: list[CollectedPost] = []
    for index, video_url in enumerate(ctx.config.video_urls):
        if len(collected) >= ctx.config.max_posts:
            break
        result = ctx.try_fetch(
            OEMBED, name=f"oembed_{index:03d}", params={"url": video_url},
            stage="tiktok.oembed")
        if result is None:
            break
        payload = result.payload or {}
        if payload.get("error") or not payload.get("title"):
            raise RuntimeError(f"oEmbed returned no video metadata for {video_url}")
        match = VIDEO_ID_RE.search(video_url)
        post_id = match.group(1) if match else video_url.rstrip("/").rsplit("/", 1)[-1]
        width = parse_int(payload.get("width")) or 0
        height = parse_int(payload.get("height")) or 0
        vertical = height > width
        post = PostRecord(
            platform="tiktok",
            post_id=str(post_id),
            url=video_url,
            creator_id=payload.get("author_unique_id"),
            creator_name=payload.get("author_name"),
            caption=payload.get("title"),
            published_at=None,
            duration_seconds=None,
            media_kind=str(payload.get("type") or "video"),
            format_label=FORMAT_SHORT if vertical else FORMAT_UNKNOWN,
            format_confidence=0.85 if vertical else 0.3,
            format_evidence={"method": "oembed_embed_dimensions",
                             "width": width, "height": height,
                             "vertical": vertical, "verified": False,
                             "note": "embed dimensions are not format proof; "
                                     "oEmbed exposes no publish time or metrics"},
        )
        metrics = MetricRecord(metric_source="tiktok_oembed", unavailable=list(UNAVAILABLE))
        collected.append(CollectedPost(post=post, metrics=metrics,
                                       observed_at=ctx.store_clock(),
                                       raw_id=result.raw_id))
    return collected
