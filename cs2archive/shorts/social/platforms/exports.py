"""Authorized platform-export import (CSV or JSON the operator owns).

This exists because direct read access is unavailable for some platforms. The
imported rows are marked with ``metric_source = "<platform>_authorized_export"``,
labelled ``imported`` in feature provenance, and never mixed silently with API
measurements.

Documented shape (JSON)::

    {"platform": "tiktok",
     "posts": [{"post_id": "...", "url": "...", "creator_id": "...",
                "creator_name": "...", "caption": "...",
                "published_at": "2026-10-01T00:00:00Z",
                "duration_seconds": 21.5,
                "metrics": {"views": 1, "plays": null, "likes": 4,
                            "comments": 1, "shares": null, "saves": null,
                            "followers": null}}]}

CSV uses the same field names as columns (``metrics`` flattened).
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from cs2archive.shorts.social.models import (
    FORMAT_CLIP, FORMAT_SHORT, CollectedPost, MetricRecord, PlatformStatus, PostRecord,
)
from cs2archive.shorts.social.safety import (
    dumps, parse_int, parse_timestamp, redact_payload, sha256_text,
)

COLUMNS = ("post_id", "url", "creator_id", "creator_name", "caption", "published_at",
           "duration_seconds", "views", "plays", "likes", "comments", "shares",
           "saves", "followers")
REQUIREMENTS = ["an account-data export the operator is authorized to use"]


def platform_status(config, env) -> PlatformStatus:
    if (config.extra or {}).get("export_paths"):
        return PlatformStatus(
            platform=config.name, status="partial", mode="authorized_export",
            reason="imports an authorized export; not a live API read",
            requirements=REQUIREMENTS)
    return PlatformStatus(
        platform=config.name, status="unavailable", mode="authorized_export",
        reason="no authorized export configured", requirements=REQUIREMENTS)


def _rows_from_json(payload) -> list[dict]:
    if isinstance(payload, dict):
        rows = payload.get("posts") or payload.get("items") or []
    elif isinstance(payload, list):
        rows = payload
    else:
        rows = []
    return [row for row in rows if isinstance(row, dict)]


def _rows_from_csv(text: str) -> list[dict]:
    return [dict(row) for row in csv.DictReader(io.StringIO(text))]


def load_export(path: Path) -> list[dict]:
    raw = Path(path).read_bytes()
    digest = sha256_text(raw.decode("utf-8", errors="replace"))
    if Path(path).suffix.lower() == ".csv":
        return _rows_from_csv(raw.decode("utf-8-sig")), digest
    return _rows_from_json(json.loads(raw.decode("utf-8-sig"))), digest


def _metrics_from(row: dict) -> MetricRecord:
    nested = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
    def pick(name: str):
        return parse_int(nested.get(name, row.get(name)))
    unavailable = [name for name in
                   ("views", "plays", "likes", "comments", "shares", "saves", "followers")
                   if pick(name) is None]
    return MetricRecord(
        views=pick("views"), plays=pick("plays"), likes=pick("likes"),
        comments=pick("comments"), shares=pick("shares"), saves=pick("saves"),
        followers=pick("followers"), metric_source="authorized_export",
        unavailable=unavailable)


def _format_for(platform: str, row: dict) -> tuple[str, float, dict]:
    duration = row.get("duration_seconds")
    try:
        duration = float(duration) if duration not in (None, "") else None
    except (TypeError, ValueError):
        duration = None
    evidence = {"method": "authorized_export", "duration_seconds": duration,
                "note": "export carries no format/aspect metadata"}
    if platform == "tiktok" and duration is not None and duration <= 180:
        return FORMAT_SHORT, 0.6, {**evidence, "note": "TikTok product is short-form; "
                                   "export does not prove vertical format"}
    return FORMAT_CLIP, 0.3, evidence


def collect(ctx) -> list[CollectedPost]:
    platform = ctx.platform
    paths = (ctx.config.extra or {}).get("export_paths") or []
    collected: list[CollectedPost] = []
    for raw_path in paths:
        path = Path(str(raw_path))
        if not path.exists():
            raise FileNotFoundError(f"authorized export not found: {path}")
        rows, digest = load_export(path)
        kept = 0
        for row in rows:
            if len(collected) >= ctx.config.max_posts:
                break
            post_id = str(row.get("post_id") or row.get("id") or "").strip()
            if not post_id:
                continue
            published = parse_timestamp(row.get("published_at") or row.get("created_at"))
            if ctx.window_start and published and published < ctx.window_start:
                continue
            if ctx.window_end and published and published > ctx.window_end:
                continue
            label, confidence, evidence = _format_for(platform, row)
            duration = evidence.get("duration_seconds")
            post = PostRecord(
                platform=platform,
                post_id=post_id,
                url=row.get("url"),
                creator_id=str(row.get("creator_id") or "") or None,
                creator_name=row.get("creator_name"),
                caption=row.get("caption"),
                published_at=published,
                duration_seconds=duration,
                media_kind="video",
                format_label=label,
                format_confidence=confidence,
                format_evidence=evidence,
                notes={"export_path": str(path), "export_sha256": digest},
            )
            observed_at = (parse_timestamp(row.get("observed_at"))
                           or ctx.store_clock())
            collected.append(CollectedPost(post=post, metrics=_metrics_from(row),
                                           observed_at=observed_at))
            kept += 1
        ctx.store.record_import(
            platform=platform, kind="authorized_export", path=str(path), digest=digest,
            rows_read=len(rows), rows_kept=kept,
            notes={"columns": list(rows[0]) if rows else []})
        ctx.counters.pages_fetched += 1
    ctx.counters.posts_seen += len(collected)
    return collected


def preserve_export(store, *, platform: str, path: Path, raw_root: Path) -> str:
    """Copy a redacted copy of the export next to the raw API responses."""
    raw = Path(path).read_bytes()
    text = raw.decode("utf-8-sig", errors="replace")
    target_dir = Path(raw_root) / platform / "imports"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{sha256_text(text)[:16]}_{Path(path).name}"
    if Path(path).suffix.lower() == ".csv":
        target.write_text(store.scrub(text), encoding="utf-8")
    else:
        target.write_text(dumps(redact_payload(json.loads(text), store.secrets)),
                          encoding="utf-8")
    return str(target)
