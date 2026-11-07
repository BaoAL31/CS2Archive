"""Instagram Reels collection via Meta Graph API Business Discovery.

Supported with the configured system-user token: Business Discovery reads a
*public* business/creator account's media (caption, permalink, timestamp, likes,
comments, follower count). No login, no scraping, nothing private.

Only handles that actually resolve can be collected: an unknown/renamed username
returns HTTP 400 ``Cannot find user``. One bad handle is logged and skipped — it
never fails the platform run.

Not exposed by Business Discovery: play/view counts, shares, saves, video
duration. Those stay NULL and are recorded as unavailable.
"""

from __future__ import annotations

from cs2archive.shorts.social.models import (
    FORMAT_CLIP, FORMAT_SHORT, FORMAT_UNKNOWN, CollectedPost, MetricRecord,
    PlatformStatus, PostRecord,
)
from cs2archive.shorts.social.safety import parse_int, parse_timestamp

GRAPH = "https://graph.facebook.com/v21.0"
MEDIA_FIELDS = ("id,media_type,permalink,timestamp,like_count,comments_count,caption")
REQUIREMENTS = [
    "META_SYSTEM_USER_TOKEN (or META_PAGE_ACCESS_TOKEN) with instagram_basic + pages_read_engagement",
    "META_IG_USER_ID of a professional Instagram account",
    "target account must be public and a business/creator account",
]


def platform_status(config, env) -> PlatformStatus:
    if not env.meta_token or not env.meta_ig_user_id:
        return PlatformStatus(
            platform="instagram", status="unavailable",
            mode=config.mode or "graph_business_discovery",
            reason="no Meta system-user token / IG user id configured",
            requirements=REQUIREMENTS)
    if not config.creators:
        return PlatformStatus(
            platform="instagram", status="unavailable",
            mode=config.mode or "graph_business_discovery",
            reason="no creator handles configured", requirements=REQUIREMENTS)
    return PlatformStatus(
        platform="instagram", status="partial",
        mode=config.mode or "graph_business_discovery",
        reason="Business Discovery works for public creator media, but exposes "
               "likes/comments only (no plays, shares, saves or duration)",
        requirements=REQUIREMENTS)


def _assess(media_type: str | None, permalink: str | None):
    kind = str(media_type or "").upper()
    link = str(permalink or "")
    evidence = {"media_type": kind, "permalink": link, "verified": False}
    if kind == "VIDEO" and "/reel/" in link:
        return (FORMAT_SHORT, 0.9,
                {**evidence, "method": "reel_permalink+media_type", "verified": True})
    if kind == "VIDEO":
        return FORMAT_CLIP, 0.6, {**evidence, "method": "video_media_type"}
    if kind in {"IMAGE", "CAROUSEL_ALBUM"}:
        return FORMAT_UNKNOWN, 0.2, {**evidence, "method": "non_video"}
    return FORMAT_UNKNOWN, 0.0, evidence


def _fields(handle: str, limit: int, after: str | None = None) -> str:
    media = f"media.limit({limit})"
    if after:
        media = f"media.limit({limit}).after({after})"
    return (f"business_discovery.username({handle})"
            f"{{followers_count,media_count,{media}{{{MEDIA_FIELDS}}}}}")


def _creator_failure(ctx, handle: str, exc: Exception) -> None:
    ctx.store.log_error(
        run_id=ctx.run_id, platform="instagram",
        stage="instagram.business_discovery", code="creator_unavailable",
        message=f"{handle}: {exc}", counters=ctx.counters)


def collect_creator(ctx, handle: str, limit: int | None = None) -> list[CollectedPost]:
    """One creator's recent media. Raises when the handle cannot be resolved."""
    config = ctx.config
    budget = config.max_posts if limit is None else max(0, int(limit))
    per_page = int((config.extra or {}).get("per_page") or 25)
    out: list[CollectedPost] = []
    after = None
    for page in range(max(1, config.max_pages)):
        result = ctx.try_fetch(
            f"{GRAPH}/{ctx.env.meta_ig_user_id}", name=f"discovery_{handle}_p{page:02d}",
            params={"fields": _fields(handle, per_page, after),
                    "access_token": ctx.env.meta_token},
            stage="instagram.business_discovery")
        if result is None:
            break
        ctx.counters.pages_fetched += 1
        payload = result.payload or {}
        if payload.get("error"):
            raise RuntimeError(str(payload["error"].get("message"))[:300])
        discovery = payload.get("business_discovery") or {}
        followers = parse_int(discovery.get("followers_count"))
        media = discovery.get("media") or {}
        for item in media.get("data") or []:
            if len(out) >= budget:
                return out
            published = parse_timestamp(item.get("timestamp"))
            if ctx.window_start and published and published < ctx.window_start:
                continue
            if ctx.window_end and published and published > ctx.window_end:
                continue
            label, confidence, evidence = _assess(
                item.get("media_type"), item.get("permalink"))
            unavailable = ["plays", "shares", "saves", "duration_seconds"]
            if item.get("like_count") is None:
                unavailable.append("likes")
            if item.get("comments_count") is None:
                unavailable.append("comments")
            if followers is None:
                unavailable.append("followers")
            post = PostRecord(
                platform="instagram",
                post_id=str(item.get("id") or ""),
                url=item.get("permalink"),
                creator_id=None,
                creator_name=handle,
                caption=item.get("caption"),
                published_at=published,
                duration_seconds=None,
                media_kind=str(item.get("media_type") or "").lower() or None,
                format_label=label,
                format_confidence=confidence,
                format_evidence=evidence,
            )
            metrics = MetricRecord(
                views=None, plays=None,
                likes=parse_int(item.get("like_count")),
                comments=parse_int(item.get("comments_count")),
                followers=followers,
                metric_source="instagram_graph_business_discovery",
                unavailable=unavailable)
            out.append(CollectedPost(post=post, metrics=metrics,
                                     observed_at=ctx.store_clock(),
                                     raw_id=result.raw_id))
        cursors = (media.get("paging") or {}).get("cursors") or {}
        after = cursors.get("after")
        if not after or not (media.get("data") or []):
            break
    return out


def collect(ctx) -> list[CollectedPost]:
    config = ctx.config
    collected: list[CollectedPost] = []
    failures: list[str] = []
    resolved: list[str] = []
    for creator in config.creators:
        handle = creator.lstrip("@")
        if len(collected) >= config.max_posts:
            break
        try:
            items = collect_creator(ctx, handle, limit=config.max_posts - len(collected))
        except Exception as exc:  # noqa: BLE001 - a bad handle is not fatal
            failures.append(f"{handle}: {exc}")
            _creator_failure(ctx, handle, exc)
            continue
        resolved.append(handle)
        collected.extend(items)
    ctx.counters.notes["resolved_creators"] = resolved
    if failures:
        ctx.counters.notes["creator_failures"] = failures
    if failures and not resolved:
        raise RuntimeError(
            "all configured Instagram creators failed: " + "; ".join(failures)[:500])
    ctx.counters.posts_seen += len(collected)
    return collected
