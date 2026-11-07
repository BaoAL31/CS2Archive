"""Measured features from the existing Shorts foundation (never invented).

Gameplay and clip-presentation features come only from a **verified** foundation
clip: an identity link that is unambiguous with confidence >= 0.8, and an
alignment whose own confidence is ``exact``. Partial/unresolved alignments are
recorded as unavailable, not measured. Every feature row carries the alignment's
confidence instead of a blanket 1.0.

Two features are deliberately *not* derived: ``first_action_seconds`` and
``dead_time_seconds`` have no validated measurement yet, so they stay null.

Nothing here reads engagement metrics, so feature extraction cannot leak the
outcome.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from cs2archive.shorts.social.identity import verified_links

GAMEPLAY_FEATURES = (
    "kills", "headshots", "headshot_rate", "distinct_weapons", "kill_span_seconds",
    "kills_per_10s", "fire_events", "hit_events", "hit_event_ratio", "damage_dealt",
    "clutch_size", "multikill",
)
PRESENTATION_FEATURES = (
    "clip_span_seconds", "round_span_seconds", "round_number",
    "first_action_seconds", "dead_time_seconds",
)
# Utility/non-bullet weapons never count as shots or hits.
UTILITY_WEAPONS = frozenset({
    "weapon_flashbang", "weapon_smokegrenade", "weapon_hegrenade", "weapon_molotov",
    "weapon_incgrenade", "weapon_decoy", "weapon_knife", "weapon_bayonet",
    "weapon_taser", "flashbang", "smokegrenade", "hegrenade", "molotov",
    "incgrenade", "decoy", "knife", "taser", "inferno",
})
ALIGNMENT_CONFIDENCE = {"exact": 1.0, "partial": 0.6}


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}


def _events(frame: pd.DataFrame) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    if "key" not in frame.columns or "payload" not in frame.columns:
        return out
    for key, payload in zip(frame["key"], frame["payload"]):
        rows = payload
        if isinstance(rows, str):
            try:
                rows = json.loads(rows)
            except json.JSONDecodeError:
                continue
        if isinstance(rows, dict):
            rows = [rows]
        if isinstance(rows, list):
            out.setdefault(str(key), []).extend(r for r in rows if isinstance(r, dict))
    return out


def alignment_confidence(align: dict) -> float:
    return ALIGNMENT_CONFIDENCE.get(str(align.get("confidence") or ""), 0.0)


def _is_utility(weapon) -> bool:
    name = str(weapon or "").strip().lower()
    if name.startswith("weapon_"):
        name = name[len("weapon_"):]
    return name in UTILITY_WEAPONS


def _flag(value) -> bool | None:
    """Tri-state kill flag: True/False when present, None when the field is absent."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    return None


def foundation_features(clip_dir: Path) -> dict:
    """Return ``{feature: (value, provenance, confidence, evidence)}``."""
    align = _load_json(clip_dir / "alignment.json")
    match = _load_json(clip_dir / "match.json")
    result: dict[str, tuple[float | None, str, float | None, dict]] = {}
    steamid = str(align.get("steamid") or "")
    confidence = alignment_confidence(align)
    exact = confidence >= 1.0

    tick_start, tick_end = align.get("tick_start"), align.get("tick_end")
    round_start, round_end = align.get("round_tick_start"), align.get("round_tick_end")
    if isinstance(tick_start, (int, float)) and isinstance(tick_end, (int, float)):
        result["clip_span_seconds"] = (
            round((tick_end - tick_start) / 64.0, 3), "measured", confidence,
            {"source": "alignment.tick_start/tick_end",
             "alignment_confidence": align.get("confidence")})
    if isinstance(round_start, (int, float)) and isinstance(round_end, (int, float)):
        result["round_span_seconds"] = (
            round((round_end - round_start) / 64.0, 3), "measured", confidence,
            {"source": "alignment.round_tick_start/round_tick_end",
             "alignment_confidence": align.get("confidence")})
    if align.get("round") is not None:
        result["round_number"] = (float(align["round"]), "measured", confidence,
                                  {"source": "alignment.round"})

    kill_ticks = [t for t in (align.get("kill_ticks") or [])
                  if isinstance(t, (int, float))]
    if kill_ticks:
        span = round((max(kill_ticks) - min(kill_ticks)) / 64.0, 3)
        result["kill_span_seconds"] = (span, "measured", confidence,
                                       {"source": "alignment.kill_ticks"})
        result["kills_per_10s"] = (
            round(len(kill_ticks) / max(span, 0.1) * 10.0, 3), "derived", confidence,
            {"source": "alignment.kill_ticks"})

    events_path = clip_dir / "events.parquet"
    if not events_path.exists():
        return result
    try:
        events = _events(pd.read_parquet(events_path))
    except Exception:  # noqa: BLE001 - corrupt parquet means unavailable features
        return result

    kills = [k for k in events.get("kills") or []
             if str(k.get("attacker_steam_id") or "") == steamid]
    if kills:
        flags = [_flag(k.get("headshot")) for k in kills]
        headshots = sum(1 for flag in flags if flag is True)
        weapons = {str(k.get("weapon") or "") for k in kills if k.get("weapon")}
        result["kills"] = (float(len(kills)), "measured", confidence,
                           {"source": "events.kills", "steamid": steamid})
        result["headshots"] = (float(headshots), "measured", confidence,
                               {"source": "events.kills"})
        missing = sum(1 for flag in flags if flag is None)
        if missing:
            # A missing headshot flag is unknown, not False.
            result["headshot_rate"] = (
                None, "unavailable", None,
                {"reason": "headshot flag missing on kill rows",
                 "kills_missing_flag": missing, "source": "events.kills"})
        else:
            result["headshot_rate"] = (
                round(headshots / len(kills), 4), "derived", confidence,
                {"source": "events.kills"})
        result["distinct_weapons"] = (float(len(weapons)), "measured", confidence,
                                      {"source": "events.kills",
                                       "weapons": sorted(weapons)})
        result["multikill"] = (float(len(kills)), "measured", confidence,
                               {"source": "events.kills"})

    fires = [f for f in events.get("weapon_fire") or []
             if str(f.get("user_steamid") or "") == steamid
             and not _is_utility(f.get("weapon"))]
    if fires:
        result["fire_events"] = (float(len(fires)), "measured", confidence,
                                 {"source": "events.weapon_fire",
                                  "note": "bullet-firing events, utility excluded"})
    hits = [h for h in events.get("player_hurt") or []
            if str(h.get("attacker_steam_id") or h.get("attacker_steamid") or "")
            == steamid and not _is_utility(h.get("weapon"))]
    if hits:
        damage = sum(float(h.get("dmg_health") or 0) for h in hits)
        result["hit_events"] = (float(len(hits)), "measured", confidence,
                                {"source": "events.player_hurt",
                                 "note": "non-utility damage events received"})
        result["damage_dealt"] = (round(damage, 1), "measured", confidence,
                                  {"source": "events.player_hurt"})
    if fires and hits:
        # Event-level ratio, NOT shot accuracy: multi-victim bullets and
        # non-bullet damage make a per-shot hit rate unidentifiable here.
        result["hit_event_ratio"] = (
            round(len(hits) / len(fires), 4), "derived", confidence,
            {"source": "events.player_hurt/weapon_fire",
             "note": "hit events per firing event; not validated shot accuracy"})

    kinds = align.get("kinds") or match.get("kinds") or []
    clutch = _clutch_size(kinds)
    if clutch is not None:
        result["clutch_size"] = (float(clutch), "measured", confidence,
                                 {"source": "allstar kinds", "kinds": kinds})
    if not exact:
        # Guard-rail: partial/unresolved alignments never masquerade as measured.
        return {name: (None, "unavailable", None,
                       {"reason": "alignment is not exact",
                        "alignment_confidence": align.get("confidence")})
                for name in result}
    return result


def _clutch_size(kinds) -> int | None:
    for kind in kinds or []:
        text = str(kind)
        if text.startswith("1v") and text[2:3].isdigit():
            return int(text[2])
    return None


def _verified_clip_id(links: list[dict]) -> tuple[str | None, str | None]:
    """One unambiguous verified clip id, or a reason it cannot be used."""
    usable = verified_links(links)
    clip_ids = sorted({str(link["foundation_clip_id"]) for link in usable
                       if link.get("foundation_clip_id")})
    if not usable:
        return None, "no verified identity link"
    if not clip_ids:
        return None, "verified link has no foundation clip"
    if len(clip_ids) > 1:
        return None, "multiple verified clips for one post"
    return clip_ids[0], None


def attach_features(store, *, run_id: str, foundation_dir: Path) -> dict:
    """Rewrite features + explicit missingness rows for every post in the store."""
    links_by_post: dict[tuple[str, str], list[dict]] = {}
    for link in store.conn.execute("SELECT * FROM clip_identity_links"):
        links_by_post.setdefault((link["platform"], link["post_id"]), []).append(
            dict(link))

    stats = {"posts": 0, "linked": 0, "features_written": 0,
             "unavailable_rows": 0, "quarantined_linked": 0,
             "rejected_not_exact": 0, "rejected_unverified_link": 0,
             "rejected_multiple_clips": 0}
    quarantined = store.quarantined_posts()
    for row in store.conn.execute("SELECT * FROM posts"):
        platform, post_id = row["platform"], row["post_id"]
        stats["posts"] += 1
        clip_id, reason = _verified_clip_id(links_by_post.get((platform, post_id), []))
        values: dict[str, tuple] = {}
        if clip_id is None:
            if "multiple" in str(reason):
                stats["rejected_multiple_clips"] += 1
            else:
                stats["rejected_unverified_link"] += 1
            values_reason = reason
        else:
            clip_dir = Path(foundation_dir) / "clips" / clip_id
            align = _load_json(clip_dir / "alignment.json") if clip_dir.exists() else {}
            if not align:
                values_reason = "staged alignment missing"
            elif (platform, post_id) in quarantined or align.get("quarantined"):
                stats["quarantined_linked"] += 1
                values_reason = "quarantined alignment"
            elif str(align.get("confidence")) != "exact":
                stats["rejected_not_exact"] += 1
                values_reason = (f"alignment confidence is "
                                 f"{align.get('confidence') or 'unknown'}")
            else:
                stats["linked"] += 1
                values = foundation_features(clip_dir)
                values_reason = "feature not present in staged events"
        # Rewrite, so renamed/stale derived features never linger.
        store.clear_features(platform, post_id)
        for feature, kind in ([(name, "gameplay") for name in GAMEPLAY_FEATURES]
                              + [(name, "presentation")
                                 for name in PRESENTATION_FEATURES]):
            if feature in values:
                value, provenance, confidence, evidence = values[feature]
                store.set_feature(
                    platform=platform, post_id=post_id, feature=feature,
                    value_num=value, kind=kind, provenance=provenance,
                    confidence=confidence, evidence=evidence, run_id=run_id)
                if value is None:
                    stats["unavailable_rows"] += 1
                else:
                    stats["features_written"] += 1
            else:
                store.set_feature(
                    platform=platform, post_id=post_id, feature=feature,
                    value_num=None, kind=kind, provenance="unavailable",
                    confidence=None,
                    evidence={"reason": values_reason}, run_id=run_id)
                stats["unavailable_rows"] += 1
        _context_features(store, row, run_id)
    return stats


def _context_features(store, row, run_id: str) -> None:
    from cs2archive.shorts.social.safety import parse_timestamp

    caption = str(row["caption"] or "")
    published = parse_timestamp(row["published_at"])
    observed = parse_timestamp(row["first_observed_at"])
    store.set_feature(platform=row["platform"], post_id=row["post_id"],
                      feature="caption_length", value_num=float(len(caption)),
                      kind="context", provenance="measured", confidence=1.0,
                      run_id=run_id)
    store.set_feature(platform=row["platform"], post_id=row["post_id"],
                      feature="caption_hashtags",
                      value_num=float(caption.count("#")), kind="context",
                      provenance="measured", confidence=1.0, run_id=run_id)
    if row["creator_name"]:
        store.set_feature(platform=row["platform"], post_id=row["post_id"],
                          feature="creator_name", value_text=row["creator_name"],
                          kind="context", provenance="measured", confidence=1.0,
                          run_id=run_id)
    if row["duration_seconds"] is not None:
        store.set_feature(platform=row["platform"], post_id=row["post_id"],
                          feature="duration_seconds",
                          value_num=float(row["duration_seconds"]), kind="presentation",
                          provenance="measured", confidence=1.0, run_id=run_id)
    else:
        store.set_feature(platform=row["platform"], post_id=row["post_id"],
                          feature="duration_seconds", value_num=None,
                          kind="presentation", provenance="unavailable",
                          confidence=None, run_id=run_id)
    if published:
        from datetime import datetime
        stamp = datetime.fromisoformat(published.replace("Z", "+00:00"))
        store.set_feature(platform=row["platform"], post_id=row["post_id"],
                          feature="published_weekday", value_num=float(stamp.weekday()),
                          kind="context", provenance="measured", confidence=1.0,
                          run_id=run_id)
        store.set_feature(platform=row["platform"], post_id=row["post_id"],
                          feature="published_hour_utc", value_num=float(stamp.hour),
                          kind="context", provenance="measured", confidence=1.0,
                          run_id=run_id)
        if observed:
            age = (datetime.fromisoformat(observed.replace("Z", "+00:00"))
                   - stamp).total_seconds() / 3600.0
            # Stored for audit/reporting only. It is a measurement-time nuisance
            # variable, never a predictor (see train.MEASUREMENT_TIME_FEATURES).
            store.set_feature(platform=row["platform"], post_id=row["post_id"],
                              feature="age_hours_at_observation", value_num=round(age, 3),
                              kind="context", provenance="derived", confidence=1.0,
                              evidence={"note": "measurement age, excluded from models"},
                              run_id=run_id)


def summarize_features(store) -> dict:
    rows = list(store.conn.execute(
        "SELECT kind, feature, provenance, COUNT(*) AS n, "
        "SUM(CASE WHEN value_num IS NOT NULL OR value_text IS NOT NULL THEN 1 ELSE 0 END) "
        "AS present FROM post_features GROUP BY kind, feature, provenance "
        "ORDER BY kind, feature"))
    totals: dict[str, dict] = {}
    for row in rows:
        entry = totals.setdefault(row["feature"], {
            "kind": row["kind"], "present": 0, "missing": 0, "provenance": set()})
        entry["provenance"].add(row["provenance"])
        if row["provenance"] == "unavailable":
            entry["missing"] += row["n"]
        else:
            entry["present"] += row["present"]
            entry["missing"] += row["n"] - row["present"]
    return {name: {"kind": entry["kind"], "present": entry["present"],
                   "missing": entry["missing"],
                   "provenance": sorted(entry["provenance"])}
            for name, entry in sorted(totals.items())}
