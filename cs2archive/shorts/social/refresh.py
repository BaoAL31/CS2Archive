"""Bounded metric refresh for posts already in the evidence database.

Only platforms with a public per-item metrics path are refreshable:

* YouTube — `videos.list` by id (1 quota unit per 50 ids).
* Instagram — re-list the configured creator's media and match ids.
* TikTok — oEmbed for explicitly configured public URLs (no metrics anyway).

Reddit RSS cannot look up an existing post and the preserved Allstar store is
immutable, so those report an explicit `unavailable` reason instead of pretending
to refresh. Every refresh writes a **new immutable snapshot**; historical
snapshots are never reconstructed.
"""

from __future__ import annotations

from pathlib import Path

from cs2archive.shorts.social.config import SweepConfig
from cs2archive.shorts.social.context import Env, PlatformContext
from cs2archive.shorts.social.models import MetricRecord, PostRecord
from cs2archive.shorts.social.platforms import youtube as youtube_platform
from cs2archive.shorts.social.platforms.instagram import MEDIA_FIELDS, _assess
from cs2archive.shorts.social.safety import (
    RateLimiter, iso, parse_int, parse_timestamp, utcnow,
)
from cs2archive.shorts.social.store import RunCounters

GRAPH = "https://graph.facebook.com/v21.0"


def _refreshable_ids(store, platform: str, min_age_hours: float,
                     max_posts: int) -> list[str]:
    from datetime import datetime, timezone

    cutoff = utcnow().timestamp() - min_age_hours * 3600.0
    rows = store.conn.execute(
        "SELECT p.post_id, MAX(s.observed_at) AS at FROM posts p "
        "JOIN metric_snapshots s ON s.platform=p.platform AND s.post_id=p.post_id "
        "WHERE p.platform=? GROUP BY p.post_id ORDER BY at ASC, p.post_id LIMIT ?",
        (platform, max_posts))
    ids = []
    for row in rows:
        try:
            observed = datetime.fromisoformat(str(row["at"]).replace("Z", "+00:00"))
        except ValueError:
            continue
        if observed.replace(tzinfo=timezone.utc).timestamp() <= cutoff:
            ids.append(row["post_id"])
    return ids


def _youtube_refresh(ctx, ids: list[str], counters: RunCounters) -> int:
    written = 0
    for start in range(0, len(ids), 50):
        batch = ids[start:start + 50]
        result = ctx.fetch(
            f"{youtube_platform.API}/videos", name=f"refresh_{start // 50:03d}",
            params={"part": "snippet,contentDetails,statistics",
                    "id": ",".join(batch), "key": ctx.env.youtube_api_key},
            quota_units=1.0, stage="youtube.refresh")
        fetched_at = result.fetched_at
        for item in (result.payload or {}).get("items") or []:
            video_id = str(item.get("id"))
            stats = item.get("statistics") or {}
            snippet = item.get("snippet") or {}
            details = item.get("contentDetails") or {}
            post = PostRecord(
                platform="youtube", post_id=video_id,
                url=youtube_platform.VIDEO_URL.format(video_id=video_id),
                creator_id=snippet.get("channelId"),
                creator_name=snippet.get("channelTitle"),
                caption=snippet.get("title"),
                published_at=parse_timestamp(snippet.get("publishedAt")),
                duration_seconds=youtube_platform.parse_iso_duration(
                    details.get("duration")))
            unavailable = ["plays", "shares", "saves"]
            if "likeCount" not in stats:
                unavailable.append("likes")
            if "commentCount" not in stats:
                unavailable.append("comments")
            metrics = MetricRecord(
                views=parse_int(stats.get("viewCount")),
                likes=parse_int(stats.get("likeCount")),
                comments=parse_int(stats.get("commentCount")),
                metric_source="youtube_data_api_v3", unavailable=unavailable)
            ctx.store.upsert_post(post, run_id=ctx.run_id, raw_id=result.raw_id,
                                  observed_at=fetched_at)
            inserted = ctx.store.add_snapshot(
                post, metrics, observed_at=fetched_at, run_id=ctx.run_id,
                raw_id=result.raw_id)
            ctx.store.link_snapshot_provenance(
                post, observed_at=fetched_at,
                metric_source=metrics.metric_source, raw_id=result.raw_id,
                run_id=ctx.run_id)
            if inserted:
                written += 1
        counters.pages_fetched += 1
    return written


def _instagram_refresh(ctx, ids: set[str], counters: RunCounters) -> int:
    written = 0
    wanted = set(ids)
    for creator in ctx.config.creators:
        handle = creator.lstrip("@")
        try:
            written += _instagram_refresh_creator(ctx, handle, wanted, counters)
        except Exception as exc:  # noqa: BLE001 - a bad handle is not fatal
            ctx.store.log_error(
                run_id=ctx.run_id, platform="instagram", stage="instagram.refresh",
                code="creator_unavailable", message=f"{handle}: {exc}",
                counters=counters)
            continue
    return written


def _instagram_refresh_creator(ctx, handle: str, wanted: set[str],
                              counters: RunCounters) -> int:
        written = 0
        after = None
        for page in range(max(1, ctx.config.max_pages)):
            fields = (f"business_discovery.username({handle})"
                      f"{{followers_count,media.limit(25)"
                      + (f".after({after})" if after else "")
                      + f"{{{MEDIA_FIELDS}}}}}")
            result = ctx.try_fetch(
                f"{GRAPH}/{ctx.env.meta_ig_user_id}",
                name=f"refresh_{handle}_p{page:02d}",
                params={"fields": fields, "access_token": ctx.env.meta_token},
                stage="instagram.refresh")
            if result is None:
                break
            counters.pages_fetched += 1
            if (result.payload or {}).get("error"):
                raise RuntimeError(str(result.payload["error"].get("message"))[:300])
            discovery = (result.payload or {}).get("business_discovery") or {}
            followers = parse_int(discovery.get("followers_count"))
            media = discovery.get("media") or {}
            for item in media.get("data") or []:
                post_id = str(item.get("id") or "")
                if post_id not in wanted:
                    continue
                observed_at = result.fetched_at
                label, confidence, evidence = _assess(
                    item.get("media_type"), item.get("permalink"))
                post = PostRecord(
                    platform="instagram", post_id=post_id,
                    url=item.get("permalink"), creator_name=handle,
                    caption=item.get("caption"),
                    published_at=parse_timestamp(item.get("timestamp")),
                    media_kind=str(item.get("media_type") or "").lower() or None,
                    format_label=label, format_confidence=confidence,
                    format_evidence=evidence)
                metrics = MetricRecord(
                    likes=parse_int(item.get("like_count")),
                    comments=parse_int(item.get("comments_count")),
                    followers=followers,
                    metric_source="instagram_graph_business_discovery",
                    unavailable=["plays", "shares", "saves", "duration_seconds"])
                ctx.store.upsert_post(post, run_id=ctx.run_id, raw_id=result.raw_id,
                                      observed_at=observed_at)
                inserted = ctx.store.add_snapshot(
                    post, metrics, observed_at=observed_at, run_id=ctx.run_id,
                    raw_id=result.raw_id)
                ctx.store.link_snapshot_provenance(
                    post, observed_at=observed_at,
                    metric_source=metrics.metric_source, raw_id=result.raw_id,
                    run_id=ctx.run_id)
                if inserted:
                    written += 1
            cursors = (media.get("paging") or {}).get("cursors") or {}
            after = cursors.get("after")
            if not after or not (media.get("data") or []):
                break
        return written


def refresh_database(config: SweepConfig, *, env: Env | None = None, client=None,
                     clock=utcnow, platforms: list[str] | None = None,
                     min_age_hours: float = 1.0, max_posts: int = 200) -> dict:
    from cs2archive.shorts.social.collect import env_from_settings, _load_client

    env = env or env_from_settings()
    store_secrets = env.secrets()
    from cs2archive.shorts.social.store import SocialStore

    store = SocialStore(config.database, secrets=store_secrets)
    results = []
    client = client or _load_client()
    try:
        for platform in platforms or ["youtube", "instagram", "tiktok", "reddit",
                                      "allstar"]:
            run_id = store.unique_run_id(
                f"refresh-{platform}-{iso(clock()).replace(':', '')[:15]}")
            store.start_run(run_id=run_id, platform=platform, mode="refresh",
                            config_path=str(config.path), started_at=iso(clock()))
            counters = RunCounters()
            summary = {"platform": platform, "run_id": run_id, "status": "unknown",
                       "snapshots_written": 0}
            if platform in {"reddit", "allstar"}:
                reason = ("public RSS cannot look up an existing post by id"
                          if platform == "reddit" else
                          "preserved Allstar payloads are immutable; no live refresh")
                store.finish_run(run_id, status="unavailable", reason=reason,
                                 counters=counters)
                results.append({**summary, "status": "unavailable", "reason": reason})
                continue
            ids = _refreshable_ids(store, platform, min_age_hours, max_posts)
            if not ids:
                reason = "no post older than the refresh age threshold"
                store.finish_run(run_id, status="ok", reason=reason, counters=counters)
                results.append({**summary, "status": "ok", "reason": reason})
                continue
            pcfg = config.platform(platform)
            ctx = PlatformContext(
                store=store, run_id=run_id, platform=platform, config=pcfg, env=env,
                rate=RateLimiter(config.min_interval_seconds, config.max_requests),
                counters=counters, client=client, clock=clock,
                max_attempts=config.max_attempts, raw_root=config.raw_root,
                quota_limit=config.youtube_daily_quota)
            try:
                if platform == "youtube":
                    if not env.youtube_api_key:
                        raise RuntimeError("no YouTube API key")
                    written = _youtube_refresh(ctx, ids, counters)
                elif platform == "instagram":
                    if not env.meta_token or not env.meta_ig_user_id:
                        raise RuntimeError("no Meta token / IG user id")
                    written = _instagram_refresh(ctx, set(ids), counters)
                else:
                    written = 0
                    counters.notes["reason"] = (
                        "oEmbed exposes no metrics to refresh")
                counters.snapshots_written = written
                status = "ok" if written else "partial"
                store.finish_run(run_id, status=status, counters=counters,
                                 reason=None if written else "no new snapshots written")
                results.append({**summary, "status": status,
                                "snapshots_written": written,
                                "candidates": len(ids)})
            except Exception as exc:  # noqa: BLE001 - refresh failure is recorded
                store.log_error(run_id=run_id, platform=platform, stage="refresh",
                                code="refresh_failed", message=str(exc),
                                terminal=True, counters=counters)
                store.finish_run(run_id, status="failed", reason=str(exc),
                                 counters=counters)
                results.append({**summary, "status": "failed", "reason": str(exc)})
        return {"database": str(config.database), "runs": results,
                "counts": store.counts()}
    finally:
        store.close()
