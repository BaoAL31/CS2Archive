"""Collection orchestration: bounded runs, explicit statuses, no silent coverage.

Each platform run is recorded in ``collection_runs`` with one of
``ok`` / ``partial`` / ``blocked`` / ``unavailable`` / ``failed``. A blocked or
unavailable platform leaves a run row plus an error row naming the credential it
needs — it never produces placeholder posts.
"""

from __future__ import annotations

import json
from pathlib import Path

from cs2archive.shorts.social.config import SweepConfig, load_config
from cs2archive.shorts.social.context import Env, PlatformContext
from cs2archive.shorts.social.features import attach_features
from cs2archive.shorts.social.identity import link_posts
from cs2archive.shorts.social.models import CollectedPost
from cs2archive.shorts.social.platforms import adapter_for
from cs2archive.shorts.social.safety import (
    BudgetExceeded, RateLimiter, TerminalError, iso, sha256_text, utcnow,
)
from cs2archive.shorts.social.store import RunCounters, SocialStore


def env_from_settings() -> Env:
    from cs2archive.config import settings

    return Env(
        youtube_api_key=settings.youtube_api_key,
        meta_system_user_token=settings.meta_system_user_token,
        meta_page_access_token=settings.meta_page_access_token,
        meta_ig_user_id=settings.meta_ig_user_id,
    )


def _config_digest(config: SweepConfig) -> str:
    return sha256_text(json.dumps(config.raw, sort_keys=True, default=str))


def _load_client():
    import httpx

    return httpx.Client(timeout=30, follow_redirects=True)


def _write_post(store: SocialStore, item: CollectedPost, *, run_id: str) -> bool:
    store.upsert_post(item.post, run_id=run_id, raw_id=item.raw_id,
                      observed_at=item.observed_at)
    inserted = store.add_snapshot(item.post, item.metrics,
                                  observed_at=item.observed_at, run_id=run_id,
                                  raw_id=item.raw_id)
    store.link_snapshot_provenance(
        item.post, observed_at=item.observed_at,
        metric_source=item.metrics.metric_source, raw_id=item.raw_id, run_id=run_id)
    return inserted


def _apply_imports(config: SweepConfig) -> None:
    """Attach top-level ``imports`` entries to their platform configs."""
    for entry in config.imports:
        platform = str(entry.get("platform") or "").strip()
        path = entry.get("path")
        if not platform or not path:
            continue
        pcfg = config.platforms.get(platform)
        if pcfg is None:
            from cs2archive.shorts.social.config import PlatformConfig
            pcfg = PlatformConfig(name=platform)
            config.platforms[platform] = pcfg
        paths = list(pcfg.extra.get("export_paths") or [])
        resolved = Path(path)
        if not resolved.is_absolute():
            resolved = (Path(config.path).resolve().parent / path).resolve()
        paths.append(str(resolved))
        pcfg.extra["export_paths"] = paths
        pcfg.mode = "authorized_export"
    if config.allstar_store and "allstar" in config.platforms:
        config.platforms["allstar"].extra.setdefault("store", str(config.allstar_store))


def collect_platform(config: SweepConfig, store: SocialStore, platform: str, *,
                     env: Env, client=None, clock=utcnow, dry_run: bool = False,
                     max_posts: int | None = None,
                     run_id: str | None = None) -> dict:
    pcfg = config.platform(platform)
    if max_posts is not None:
        pcfg.max_posts = max_posts
    mode = pcfg.mode or "default"
    digest = _config_digest(config)
    started_at = iso(clock())
    run_id = run_id or f"{platform}-{started_at.replace(':', '').replace('-', '')[:15]}-{digest[:6]}"
    run_id = store.unique_run_id(run_id)
    store.start_run(run_id=run_id, platform=platform, mode=mode,
                    config_path=str(config.path), config_sha256=digest,
                    window_start=config.window_start, window_end=config.window_end,
                    started_at=started_at)
    counters = RunCounters()
    summary = {"platform": platform, "run_id": run_id, "mode": mode,
               "status": "unknown", "reason": None, "posts_kept": 0,
               "snapshots_written": 0, "errors": 0}

    if not pcfg.enabled:
        reason = "platform disabled in sweep config"
        store.finish_run(run_id, status="unavailable", reason=reason, counters=counters)
        summary.update(status="unavailable", reason=reason)
        return summary

    adapter = adapter_for(platform, pcfg)
    status = adapter.platform_status(pcfg, env)
    summary["declared_status"] = status.status
    if status.status in {"blocked", "unavailable"}:
        store.log_error(
            run_id=run_id, platform=platform, stage="preflight",
            code="blocked" if status.status == "blocked" else "unavailable",
            message=f"{status.reason} | requirements: {'; '.join(status.requirements)}",
            terminal=False, request_note="preflight", counters=counters)
        store.finish_run(run_id, status=status.status, reason=status.reason,
                         counters=counters)
        summary.update(status=status.status, reason=status.reason,
                       requirements=status.requirements)
        return summary

    if dry_run:
        store.finish_run(run_id, status="unavailable", reason="dry run: no collection",
                         counters=counters)
        summary.update(status="dry_run", reason="dry run: no collection")
        return summary

    interval = float(pcfg.extra.get("min_interval_seconds")
                     or config.min_interval_seconds)
    max_requests = int(pcfg.extra.get("max_requests_per_run") or config.max_requests)
    ctx = PlatformContext(
        store=store, run_id=run_id, platform=platform, config=pcfg, env=env,
        rate=RateLimiter(interval, max_requests),
        counters=counters, client=client or _load_client(), clock=clock,
        max_attempts=config.max_attempts, raw_root=config.raw_root,
        window_start=config.window_start, window_end=config.window_end,
        quota_limit=config.youtube_daily_quota, foundation_dir=config.foundation_dir)
    try:
        collected = adapter.collect(ctx)
    except TerminalError as exc:
        counters.units_spent = ctx.quota_units
        ctx.terminal(stage="collect", exc=exc, code=exc.code)
        store.finish_run(run_id, status="failed", reason=str(exc), counters=counters)
        summary.update(status="failed", reason=str(exc), errors=counters.error_count)
        return summary
    except BudgetExceeded as exc:
        # Adapters catch budget exhaustion inside their loops so already-fetched
        # posts survive; reaching here means the budget hit a one-shot call.
        store.log_error(run_id=run_id, platform=platform, stage="budget",
                        code="budget_exceeded", message=str(exc), terminal=False,
                        counters=counters)
        collected = []
        store.finish_run(run_id, status="partial", reason=str(exc), counters=counters)
        summary.update(status="partial", reason=str(exc), errors=counters.error_count)
        return summary
    except Exception as exc:  # noqa: BLE001 - terminal by policy
        ctx.terminal(stage="collect", exc=exc, code="collect_failed")
        store.finish_run(run_id, status="failed", reason=str(exc), counters=counters)
        summary.update(status="failed", reason=str(exc), errors=counters.error_count)
        return summary

    for item in collected:
        if not item.post.post_id:
            continue
        if _write_post(store, item, run_id=run_id):
            counters.snapshots_written += 1
        counters.posts_kept += 1
    counters.units_spent = ctx.quota_units
    counters.notes["quota_units"] = ctx.quota_units
    budget_hit = bool(counters.notes.get("budget_exhausted"))
    if budget_hit:
        counters.notes["partial_reason"] = "request budget exhausted mid-run"
    ok_status = "ok" if counters.error_count == 0 and not budget_hit else "partial"
    store.finish_run(
        run_id, status=ok_status, counters=counters,
        reason="request budget exhausted; partial collection kept" if budget_hit else None)
    summary.update(status=ok_status, posts_kept=counters.posts_kept,
                   snapshots_written=counters.snapshots_written,
                   errors=counters.error_count,
                   pages_fetched=counters.pages_fetched,
                   quota_units=ctx.quota_units)
    return summary


def enrich_database(config_path: Path, *, env: Env | None = None,
                    clock=utcnow) -> dict:
    """Re-run identity linking and feature extraction on an existing database."""
    config = load_config(Path(config_path))
    env = env or env_from_settings()
    store = SocialStore(config.database, secrets=env.secrets())
    try:
        run_tag = f"enrich-{iso(clock()).replace(':', '')[:15]}"
        links = link_posts(store, foundation_dir=config.foundation_dir, run_id=run_tag)
        features = attach_features(store, run_id=run_tag,
                                   foundation_dir=config.foundation_dir)
        return {"links": links, "features": features, "counts": store.counts()}
    finally:
        store.close()


def run_collection(config_path: Path, *, platforms: list[str] | None = None,
                   env: Env | None = None, client=None, clock=utcnow,
                   dry_run: bool = False, max_posts: int | None = None,
                   enrich: bool = True) -> dict:
    config = load_config(Path(config_path))
    _apply_imports(config)
    env = env or env_from_settings()
    secrets = env.secrets()
    store = SocialStore(config.database, secrets=secrets)
    try:
        # Preserve the config used for this sweep in the raw provenance tree.
        store.write_raw(
            platform="_config", run_id=clock().strftime("%Y%m%dT%H%M%SZ"),
            name="sources", payload=config.raw, endpoint=str(config.path),
            http_status=None, fetched_at=iso(clock()), raw_root=config.raw_root)
        names = platforms or list(config.platforms)
        summaries = []
        for name in names:
            summaries.append(collect_platform(
                config, store, name, env=env, client=client, clock=clock,
                dry_run=dry_run, max_posts=max_posts))
        enrichment = {}
        if enrich and not dry_run:
            run_tag = f"enrich-{iso(clock()).replace(':', '')[:15]}"
            # Links first: feature extraction consumes the identity links.
            enrichment["links"] = link_posts(
                store, foundation_dir=config.foundation_dir, run_id=run_tag)
            enrichment["features"] = attach_features(
                store, run_id=run_tag, foundation_dir=config.foundation_dir)
        return {"database": str(config.database), "runs": summaries,
                "enrichment": enrichment, "counts": store.counts()}
    finally:
        store.close()
