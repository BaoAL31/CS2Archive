"""Read-only Allstar provenance checks and snapshot deduplication."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def _stamp(value) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp
    except (TypeError, ValueError):
        return None


def deduplicate(rows: list[dict]) -> tuple[list[dict], dict]:
    """Latest observed timestamp when both known, otherwise last append wins.

    Publication time is never used to order view snapshots. Distinct IDs for
    the same play remain distinct observations; no speculative moment merge.
    """
    picked: dict[tuple[str, str], dict] = {}
    unidentified: list[dict] = []
    duplicates = conflicts = append_fallback = 0
    for row in rows:
        cid = str(row.get("clip_id") or "")
        if not cid:
            unidentified.append(row)
            continue
        key = (str(row.get("source") or ""), cid)
        old = picked.get(key)
        if old is not None:
            duplicates += 1
            conflicts += old.get("views") != row.get("views")
            old_time, new_time = _stamp(old.get("observed_at")), _stamp(row.get("observed_at"))
            if old_time is not None and new_time is not None:
                if old_time > new_time:
                    continue
            else:
                append_fallback += 1
        picked[key] = row
    return list(picked.values()) + unidentified, {
        "duplicate_snapshots": duplicates,
        "conflicting_view_snapshots": conflicts,
        "dedup_append_order_fallbacks": append_fallback,
    }


def load_allstar_dataset(path: Path) -> tuple[list[dict], dict]:
    from cs2archive.shorts.clip_observation import observation_from_allstar, parse_stage
    from cs2archive.shorts.popular_events import is_popular_event

    stats = dict(match_records=0, malformed_records=0, nonpopular_records=0,
                 raw_clips=0, fixture_conflicts=0, unusable_clips=0)
    observations = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        try:
            match = json.loads(line)
        except json.JSONDecodeError:
            stats["malformed_records"] += 1
            continue
        if not isinstance(match, dict):
            stats["malformed_records"] += 1
            continue
        stats["match_records"] += 1
        if not is_popular_event(str(match.get("slug") or ""), str(match.get("event_slug") or "")):
            stats["nonpopular_records"] += 1
            continue
        context = {**match, "stage": match.get("match_stage") or match.get("stage")}
        for clip in match.get("clips") or []:
            stats["raw_clips"] += 1
            if not isinstance(clip, dict) or not clip.get("clip_id"):
                stats["unusable_clips"] += 1
                continue
            enclosing, own = str(match.get("match_id") or ""), str(clip.get("match_id") or "")
            if enclosing and own and enclosing != own:
                stats["fixture_conflicts"] += 1
                continue
            if clip.get("source") == "allstar" and "kinds" in clip:
                obs = {**clip, "match_id": own or enclosing,
                       "stage": parse_stage(context["stage"]) or clip.get("stage"),
                       "observed_at": clip.get("observed_at") or match.get("scraped_at")}
                # The old collector stamped scrape time as publication time.
                # Only an explicitly confirmed publication timestamp is usable.
                if not clip.get("publication_time_known"):
                    obs["published_at"] = None
                    obs.pop("age_days", None)
            else:
                obs = observation_from_allstar(clip, context)
            if obs is None or not obs.get("match_id") or obs.get("source") != "allstar":
                stats["unusable_clips"] += 1
                continue
            observations.append(obs)
    stats["observations_before_dedup"] = len(observations)
    observations, dedup_stats = deduplicate(observations)
    stats.update(dedup_stats)
    stats["unique_observations"] = len(observations)
    stats["known_publication_times"] = sum(_stamp(r.get("published_at")) is not None for r in observations)
    stats["known_observation_times"] = sum(_stamp(r.get("observed_at")) is not None for r in observations)
    return observations, stats
