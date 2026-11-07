"""Allstar clip import.

Live Allstar collection is **blocked**: ``prt.allstar.gg/playlist`` answers
``403 {"message":"Error: AUTHENTICATION FAILED"}`` for anonymous clients, and
``allstar.gg/clip/<id>`` serves a single-page app shell (no per-clip metadata).
An Allstar partner/API credential is required for live reads; this adapter
therefore imports evidence that was already collected and preserved
(``.data/allstar_hltv_probe.jsonl``, raw payloads with fetch timestamps), plus
authorized exports supplied through the sweep config.

Imported Allstar metrics are whatever the preserved payload recorded (views at
fetch time). Nothing is refreshed, extrapolated or imputed.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from cs2archive.shorts.social.models import (
    FORMAT_CLIP, CollectedPost, MetricRecord, PlatformStatus, PostRecord,
)
from cs2archive.shorts.social.safety import dumps, parse_int, parse_timestamp, sha256_text

CLIP_URL = "https://allstar.gg/clip/{clip_id}"
REQUIREMENTS = [
    "Allstar partner/API credential for live playlist reads (currently 403 anonymous)",
    "or a preserved Allstar probe store / authorized export path",
]
UNAVAILABLE = ["plays", "likes", "comments", "shares", "saves", "followers",
               "duration_seconds"]


def platform_status(config, env) -> PlatformStatus:
    store = (config.extra or {}).get("store")
    if store and Path(str(store)).exists():
        return PlatformStatus(
            platform="allstar", status="partial", mode="local_store_import",
            reason="live playlist API requires Allstar auth (403 anonymous); imported "
                   "the preserved probe store instead", requirements=REQUIREMENTS)
    return PlatformStatus(
        platform="allstar", status="blocked", mode="local_store_import",
        reason="live playlist API requires Allstar auth (403 anonymous) and no "
               "preserved store/export is configured", requirements=REQUIREMENTS)


def _known(row: dict) -> bool:
    return bool(row.get("publication_time_known"))


def collect(ctx) -> list[CollectedPost]:
    store_path = (ctx.config.extra or {}).get("store")
    if not store_path:
        raise FileNotFoundError("allstar store path not configured")
    path = Path(str(store_path))
    if not path.exists():
        raise FileNotFoundError(f"allstar store not found: {path}")
    index = _foundation_index(ctx)
    # A preserved payload has no "now": importing it again must not invent a new
    # observation time. Fall back to the store file's own mtime, deterministically.
    file_observed = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)         .isoformat().replace("+00:00", "Z")
    candidates: list[dict] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            scraped_at = parse_timestamp(row.get("scraped_at"))
            for clip in row.get("clips") or []:
                if not isinstance(clip, dict) or not clip.get("clip_id"):
                    continue
                observed_at = (parse_timestamp(clip.get("observed_at"))
                               or scraped_at or file_observed)
                published = (parse_timestamp(clip.get("published_at"))
                             if _known(clip) else None)
                if ctx.window_start and published and published < ctx.window_start:
                    continue
                if ctx.window_end and published and published > ctx.window_end:
                    continue
                candidates.append({"clip": clip, "observed_at": observed_at,
                                   "published": published, "row": row,
                                   "row_key": sha256_text(dumps(row))[:12]})
    # Prefer clips that already have staged foundation evidence (so gameplay
    # features are measurable), then the most recent observation. Never by views:
    # ordinary and poor clips must stay in the sweep.
    candidates.sort(key=lambda item: str(item["clip"]["clip_id"]))
    candidates.sort(key=lambda item: item["observed_at"], reverse=True)
    candidates.sort(key=lambda item: 0 if str(item["clip"]["clip_id"]) in index else 1)

    collected: list[CollectedPost] = []
    seen: set[str] = set()
    row_raw: dict[str, int | None] = {}
    for item in candidates:
        if len(collected) >= ctx.config.max_posts:
            break
        clip = item["clip"]
        row = item["row"]
        clip_id = str(clip["clip_id"])
        if clip_id in seen:
            continue
        seen.add(clip_id)
        post = PostRecord(
            platform="allstar",
            post_id=clip_id,
            url=CLIP_URL.format(clip_id=clip_id),
            creator_id=str(clip.get("steamid") or "") or None,
            creator_name=clip.get("player"),
            caption=clip.get("title"),
            published_at=item["published"],
            duration_seconds=None,
            media_kind="video",
            format_label=FORMAT_CLIP,
            format_confidence=0.4,
            format_evidence={
                "method": "allstar_clip_import",
                "note": "clip artifact; aspect/format not independently verified",
                "verified": False,
                "source_store": str(path),
                "observed_at_source": "preserved probe payload",
            },
            notes={"kinds": clip.get("kinds") or [],
                   "match_id": clip.get("match_id") or row.get("match_id"),
                   "round": clip.get("round"),
                   "event_slug": row.get("event_slug")},
        )
        metrics = MetricRecord(
            views=parse_int(clip.get("views")),
            metric_source="allstar_store",
            unavailable=list(UNAVAILABLE))
        row_key = item["row_key"]
        if row_key not in row_raw:
            # Preserve the exact source line this clip came from, per row.
            ref = ctx.store.write_raw(
                platform="allstar", run_id=ctx.run_id,
                name=f"store_row_{row_key}",
                payload=item["row"], endpoint=str(path),
                http_status=None, fetched_at=item["observed_at"],
                params_note="preserved allstar probe row",
                raw_root=ctx.raw_root)
            row_raw[row_key] = ref.raw_id
        collected.append(CollectedPost(post=post, metrics=metrics,
                                        observed_at=item["observed_at"],
                                        raw_id=row_raw[row_key]))
    ctx.counters.posts_seen += len(seen)
    ctx.counters.notes["allstar_sampling"] = (
        "foundation-linked clips first, then most recent observation; never by views")
    ctx.counters.notes["allstar_foundation_index"] = len(index)
    return collected


def _foundation_index(ctx) -> dict:
    from cs2archive.shorts.social.identity import build_foundation_index

    foundation = getattr(ctx, "foundation_dir", None)
    if not foundation:
        return {}
    try:
        return build_foundation_index(Path(str(foundation)))
    except OSError:
        return {}



def raw_payload_for(clip: dict) -> dict:  # pragma: no cover - debug helper
    """The preserved per-clip payload, kept verbatim for provenance."""
    return {k: v for k, v in clip.items()}
