"""Reddit clip posts through public subreddit RSS.

``/r/<sub>/new.json`` is blocked (HTTP 403) for this client, so the supported
public path is Atom RSS: post id, title, author, publish time and the outbound
link. RSS exposes **no score or comment count**, so every Reddit metric is NULL.

This is a genuine evidence limitation: a Reddit post's popularity cannot be read
from this path at all, and is never inferred from rank or position.
"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET

from cs2archive.shorts.social.models import (
    FORMAT_CLIP, FORMAT_LONG, FORMAT_SHORT, FORMAT_UNKNOWN, CollectedPost,
    MetricRecord, PlatformStatus, PostRecord,
)
from cs2archive.shorts.social.safety import parse_timestamp, sanitize_text

ATOM = {"a": "http://www.w3.org/2005/Atom"}
BASE = "https://www.reddit.com"
HREF_RE = re.compile(r'href="([^"]+)"')
USER_AGENT = "CS2ArchiveResearch/0.1 (public RSS evidence sweep)"
REQUIREMENTS = [
    "public subreddit RSS (no credentials)",
    "Reddit OAuth app (client id/secret) would be needed for score/comment metrics",
]
UNAVAILABLE = ["views", "plays", "likes", "comments", "shares", "saves", "followers",
               "duration_seconds"]

SHORT_HOSTS = ("youtube.com/shorts", "instagram.com/reel", "tiktok.com", "clips.twitch.tv")
CLIP_HOSTS = ("streamable.com", "medal.tv", "clippy.gg", "outplayed.tv", "imgur.com",
              "gfycat.com", "twitch.tv/clip")


def platform_status(config, env) -> PlatformStatus:
    if not config.communities:
        return PlatformStatus(
            platform="reddit", status="unavailable", mode="rss",
            reason="no communities configured", requirements=REQUIREMENTS)
    return PlatformStatus(
        platform="reddit", status="partial", mode="rss",
        reason="public RSS works (new posts incl. ordinary/poor clips); score and "
               "comment counts are not exposed, so all metrics stay NULL",
        requirements=REQUIREMENTS)


def _outbound_link(entry) -> str | None:
    content = entry.findtext("a:content", default="", namespaces=ATOM)
    for href in HREF_RE.findall(content or ""):
        link = html.unescape(href)
        if "reddit.com" in link:
            continue
        return link
    link_el = entry.find("a:link", ATOM)
    return link_el.get("href") if link_el is not None else None


def _assess(link: str | None) -> tuple[str, float, dict]:
    low = (link or "").lower()
    evidence = {"outbound_link": link, "method": "outbound_link_host",
                "verified": False}
    if "reddit.com" in low or not low:
        return FORMAT_UNKNOWN, 0.0, {**evidence, "note": "self post, no external clip"}
    if any(host in low for host in SHORT_HOSTS):
        return FORMAT_SHORT, 0.8, {**evidence, "verified": False}
    if any(host in low for host in CLIP_HOSTS):
        return FORMAT_CLIP, 0.6, evidence
    if "youtube.com" in low or "youtu.be" in low:
        return FORMAT_LONG, 0.5, evidence
    return FORMAT_CLIP, 0.3, evidence


def collect(ctx) -> list[CollectedPost]:
    config = ctx.config
    collected: list[CollectedPost] = []
    seen: set[str] = set()
    for community in config.communities:
        sub = community.strip().lstrip("r/").strip("/")
        after = None
        for page in range(max(1, config.max_pages)):
            params = {"limit": 50}
            if after:
                params["after"] = after
            result = ctx.try_fetch(
                f"{BASE}/r/{sub}/new.rss", name=f"{sub}_p{page:02d}", params=params,
                headers={"User-Agent": USER_AGENT}, stage="reddit.rss",
                transform=lambda response: response.text)
            if result is None:
                break
            ctx.counters.pages_fetched += 1
            try:
                root = ET.fromstring(result.payload)
            except (ET.ParseError, TypeError) as exc:
                raise RuntimeError(f"RSS parse failed: {exc}") from exc
            entries = root.findall("a:entry", ATOM)
            if not entries:
                break
            oldest = None
            for entry in entries:
                if len(collected) >= config.max_posts:
                    return collected
                raw_id = entry.findtext("a:id", default="", namespaces=ATOM) or ""
                post_id = raw_id.split("_", 1)[-1]
                if not post_id or post_id in seen:
                    continue
                seen.add(post_id)
                published = parse_timestamp(
                    entry.findtext("a:published", default=None, namespaces=ATOM))
                oldest = published if oldest is None or (published and published < oldest) else oldest
                if ctx.window_start and published and published < ctx.window_start:
                    continue
                if ctx.window_end and published and published > ctx.window_end:
                    continue
                link_el = entry.find("a:link", ATOM)
                link = link_el.get("href") if link_el is not None else None
                outbound = _outbound_link(entry)
                label, confidence, evidence = _assess(outbound)
                caption = sanitize_text(
                    entry.findtext("a:title", default="", namespaces=ATOM) or "")
                author = entry.findtext("a:author/a:name", default=None, namespaces=ATOM)
                post = PostRecord(
                    platform="reddit",
                    post_id=post_id,
                    url=link,
                    creator_id=None,
                    creator_name=author,
                    caption=caption,
                    published_at=published,
                    duration_seconds=None,
                    media_kind="link",
                    format_label=label,
                    format_confidence=confidence,
                    format_evidence=evidence,
                    notes={"community": sub},
                )
                metrics = MetricRecord(metric_source="reddit_rss",
                                       unavailable=list(UNAVAILABLE))
                collected.append(CollectedPost(post=post, metrics=metrics,
                                                observed_at=ctx.store_clock(),
                                                raw_id=result.raw_id))
            after = f"t3_{post_id}" if entries else None
            if ctx.window_start and oldest and oldest < ctx.window_start:
                break
            if not after:
                break
    ctx.counters.posts_seen += len(seen)
    return collected

