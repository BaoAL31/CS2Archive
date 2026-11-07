"""Durable short → demo linkage, recorded at render/upload time.

The render directory is a scratch area that gets purged after upload, so a
rendered Short's evidence must be persisted somewhere durable **before** that
happens. This module writes the same record to two places:

* ``short_linkage`` in the social-evidence SQLite database (queryable), and
* ``data/social-sweep/linkage/<short_id>.json`` plus
  ``data/social-sweep/linkage/<platform>/<post_id>.json`` (a stable mirror that
  survives any render purge).

Lifecycle:

1. ``stage_linkage`` runs at render time, when the demo/round/tick evidence is
   known but the platform post id is not: status ``pending``, keyed by a
   deterministic ``short_id``.
2. ``confirm_linkage`` runs at upload time, when the YouTube video id (or another
   platform's post id) is known: status ``linked``, an exact
   ``clip_identity_links`` row is added so the training path can use it.

Every write is idempotent (re-running with the same evidence changes nothing) and
resumable. Callers must treat a failure here as **non-fatal but loud**: the
helper catches, logs, retries at most 3 times, and never raises into the render.
"""

from __future__ import annotations

import json
from pathlib import Path

from cs2archive.shorts.social.models import LinkRecord
from cs2archive.shorts.social.safety import (
    MAX_ATTEMPTS, dumps, iso, sha256_text, utcnow,
)

LINKAGE_DIR = Path("data/social-sweep/linkage")


def mirror_root_for(database: Path | None) -> Path:
    """Mirror root for a store: beside the database, so a temp DB stays isolated."""
    if database is None:
        return LINKAGE_DIR
    return Path(database).parent / "linkage"
STATUS_PENDING = "pending"
STATUS_LINKED = "linked"
STATUS_UNRESOLVED = "unresolved"

# Evidence keys copied verbatim from the render timeline when present.
_TIMELINE_KEYS = ("demo_path", "map", "round", "start_tick", "end_tick",
                  "kill_ticks", "pov_steam_id", "pov_nick", "short_type",
                  "source", "clutch_initial_count")


class LinkageError(RuntimeError):
    pass


def short_id_from_evidence(evidence: dict) -> str:
    """Deterministic id over the identity-bearing fields (not the video id)."""
    key = {
        "demo": str(evidence.get("demo_path") or "").replace("\\", "/").lower(),
        "player": str(evidence.get("player_steamid") or ""),
        "round": evidence.get("round"),
        "tick_start": evidence.get("tick_start"),
        "tick_end": evidence.get("tick_end"),
        "short_type": str(evidence.get("short_type") or ""),
        "kill_ticks": list(evidence.get("kill_ticks") or []),
    }
    return sha256_text(dumps(key))[:32]


def evidence_from_timeline(timeline: dict, *, selection: dict | None = None,
                           video_path: str | None = None) -> dict:
    """Build the durable evidence record from a ``short_timeline.json`` short."""
    shorts = timeline.get("shorts") or [{}]
    short = shorts[0] if shorts else {}
    allstar = short.get("allstar") or {}
    kills = short.get("kill_ticks") or []
    record = {
        "demo_path": short.get("demo_path") or timeline.get("demo_path"),
        "match_id": _match_id(short.get("demo_path") or timeline.get("demo_path")),
        "map": short.get("map") or timeline.get("map"),
        "player_steamid": str(short.get("pov_steam_id") or "") or None,
        "player_nick": short.get("pov_nick"),
        "round": short.get("round"),
        "tick_start": short.get("start_tick"),
        "tick_end": short.get("end_tick"),
        "kill_ticks": [int(k) for k in kills],
        "kill_count": len(kills),
        "short_type": short.get("short_type"),
        "source": short.get("source") or timeline.get("source"),
        "clutch_initial_count": short.get("clutch_initial_count"),
        "opponent": short.get("opponent"),
        "pov_team": short.get("pov_team"),
        "allstar_clip_id": allstar.get("clip_id"),
        "allstar_views": allstar.get("views"),
        "allstar_confidence": allstar.get("confidence"),
        "video_path": video_path,
        "timeline_keys": sorted(k for k in timeline if k in _TIMELINE_KEYS
                                or k in {"shorts", "short_count"}),
        "evidence_version": 1,
    }
    if selection:
        record["selection"] = selection
    record["short_id"] = short_id_from_evidence(record)
    return record


def evidence_from_timeline_file(timeline_path: Path, *, selection: dict | None = None,
                                video_path: str | None = None) -> dict:
    payload = json.loads(Path(timeline_path).read_text(encoding="utf-8-sig"))
    return evidence_from_timeline(payload, selection=selection, video_path=video_path)


def _match_id(demo_path) -> str | None:
    import re

    text = str(demo_path or "").replace("\\", "/")
    match = re.search(r"/(\d+)-[^/]+/", text)
    return match.group(1) if match else None


def mirror_paths(evidence: dict, *, platform: str | None = None,
                 post_id: str | None = None, root: Path | None = None) -> list[Path]:
    root = Path(root or LINKAGE_DIR)
    out = [root / f"{evidence['short_id']}.json"]
    if platform and post_id:
        out.append(root / str(platform) / f"{post_id}.json")
    return out


def write_mirror(evidence: dict, *, platform: str | None = None,
                 post_id: str | None = None, root: Path | None = None) -> list[str]:
    written = []
    for path in mirror_paths(evidence, platform=platform, post_id=post_id, root=root):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(evidence, indent=1, sort_keys=True,
                                   ensure_ascii=False), encoding="utf-8")
        written.append(str(path))
    return written


def stage_linkage(store, evidence: dict, *, run_id: str | None = None,
                  root: Path | None = None) -> dict:
    """Record render-time evidence (no post id yet). Idempotent."""
    record = dict(evidence)
    record.setdefault("short_id", short_id_from_evidence(record))
    store.upsert_short_linkage(record, status=STATUS_PENDING, run_id=run_id)
    record["mirrors"] = write_mirror(record, root=root or mirror_root_for(store.path))
    return record


def confirm_linkage(store, *, platform: str, post_id: str,
                    evidence: dict | None = None, short_id: str | None = None,
                    run_id: str | None = None, root: Path | None = None,
                    min_confidence: float = 0.8) -> dict:
    """Attach a platform post id to staged evidence and add an exact link."""
    record = None
    search_root = Path(root) if root is not None else mirror_root_for(store.path)
    if evidence is not None:
        record = dict(evidence)
    elif short_id:
        record = store.short_linkage(short_id)
    elif platform and post_id:
        # Resume: a previous run may already have staged and mirrored it.
        for path in (search_root / str(platform) / f"{post_id}.json",
                     search_root / f"{post_id}.json"):
            if path.is_file():
                record = json.loads(path.read_text(encoding="utf-8"))
                break
    if not record:
        raise LinkageError(
            f"no linkage evidence for {platform}/{post_id} (short_id={short_id})")
    record = dict(record)
    record.setdefault("short_id", short_id_from_evidence(record))
    record["platform"] = platform
    record["post_id"] = str(post_id)
    store.upsert_short_linkage(record, status=STATUS_LINKED, run_id=run_id)
    # clip_identity_links has a foreign key onto posts, and an upload-time link
    # is written before any sweep has observed the post. Stub it first, or the
    # link fails with FOREIGN KEY constraint failed and needs a backfill.
    store.ensure_post_stub(platform, str(post_id),
                           url=record.get("url") or None, run_id=run_id)
    store.upsert_link(LinkRecord(
        platform=platform, post_id=str(post_id),
        foundation_clip_id=record.get("allstar_clip_id") or record["short_id"],
        match_id=record.get("match_id"), method="render_upload_linkage",
        confidence=1.0, ambiguous=False,
        evidence={"demo_path": record.get("demo_path"),
                  "round": record.get("round"),
                  "kill_ticks": record.get("kill_ticks"),
                  "short_type": record.get("short_type"),
                  "source": record.get("source")}), run_id=run_id or "linkage")
    record["mirrors"] = write_mirror(record, platform=platform, post_id=post_id,
                                     root=root or mirror_root_for(store.path))
    return record


def try_record(action, *, what: str, log=print, attempts: int = MAX_ATTEMPTS,
               backoff_seconds: float = 0.0, sleep=None):
    """Run a linkage write without ever breaking the caller.

    Linkage is instrumentation: a failure must be loud but must not fail a render
    or an upload. Retries are capped at 3 (AGENTS.md retry policy).
    """
    from cs2archive.shorts.social.safety import retry_call, TerminalError

    attempts = max(1, min(int(attempts), MAX_ATTEMPTS))

    def on_attempt(attempt: int, exc: Exception) -> None:
        log(f"[linkage][WARN] {what}: attempt {attempt}/{attempts} failed: "
            f"{type(exc).__name__}: {exc}")

    try:
        return retry_call(action, code="linkage_failed", attempts=attempts,
                          on_attempt=on_attempt, sleep=sleep,
                          backoff_seconds=backoff_seconds)
    except TerminalError as exc:
        log(f"[linkage][ERROR] {what}: giving up after {attempts} attempts "
            f"({exc}); render/upload continues, this short will need a backfill")
        return None


def open_store(database: Path | None = None, config_path: str = "sources.json"):
    """Best-effort store for the instrumentation hooks."""
    from cs2archive.shorts.social.config import load_config
    from cs2archive.shorts.social.store import SocialStore

    if database is None:
        try:
            database = load_config(Path(config_path)).database
        except Exception:  # noqa: BLE001 - fall back to the default location
            database = Path("data/social-sweep/shorts_evidence.sqlite3")
    return SocialStore(Path(database), secrets=())


def record_render_linkage(timeline_path: Path, *, video_path: Path | None = None,
                          out_dir: Path | None = None, selection: dict | None = None,
                          database: Path | None = None, log=print,
                          config_path: str = "sources.json",
                          root: Path | None = None) -> dict | None:
    """Render-time hook: stage evidence next to the mp4 and in the database."""
    def action() -> dict:
        evidence = evidence_from_timeline_file(
            timeline_path, selection=selection,
            video_path=str(video_path) if video_path else None)
        store = open_store(database, config_path)
        try:
            return stage_linkage(store, evidence, run_id="render",
                                 root=root or mirror_root_for(store.path))
        finally:
            store.close()

    result = try_record(action, what=f"render {Path(timeline_path).name}", log=log)
    if result is not None:
        target = Path(out_dir) / "linkage.json" if out_dir else None
        try:
            if target is not None:
                target.write_text(json.dumps(result, indent=1, sort_keys=True,
                                             ensure_ascii=False), encoding="utf-8")
        except OSError as exc:  # noqa: BLE001 - the DB+mirror copy is authoritative
            log(f"[linkage][WARN] could not write {target}: {exc}")
    return result


def record_upload_linkage(short_path: Path, *, platform: str, post_id: str,
                          meta_path: Path | None = None, database: Path | None = None,
                          log=print, config_path: str = "sources.json",
                          root: Path | None = None) -> dict | None:
    """Upload-time hook: attach the platform post id to staged evidence."""
    def action() -> dict:
        short_dir = Path(short_path).parent
        candidates = [short_dir / "linkage.json", short_dir / "short_timeline.json"]
        store = open_store(database, config_path)
        try:
            evidence = None
            for path in candidates:
                if not path.is_file():
                    continue
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
                if path.name == "linkage.json":
                    evidence = payload
                else:
                    evidence = evidence_from_timeline(payload)
                break
            if evidence is None and meta_path and Path(meta_path).is_file():
                meta = json.loads(Path(meta_path).read_text(encoding="utf-8-sig"))
                evidence = _evidence_from_meta(meta)
            if evidence is None:
                raise LinkageError(
                    f"no linkage.json / short_timeline.json beside {short_path}")
            return confirm_linkage(store, platform=platform, post_id=str(post_id),
                                   evidence=evidence, run_id="upload",
                                   root=root or mirror_root_for(store.path))
        finally:
            store.close()

    return try_record(action, what=f"upload {platform}/{post_id}", log=log)


def _evidence_from_meta(meta: dict) -> dict | None:
    """Last-resort evidence from upload metadata (weaker, but explicit)."""
    demo = meta.get("demo_path") or (meta.get("linkage") or {}).get("demo_path")
    if not demo:
        return None
    return {
        "demo_path": demo,
        "match_id": _match_id(demo),
        "map": meta.get("map"),
        "player_steamid": str(meta.get("steam_id") or "") or None,
        "player_nick": meta.get("player"),
        "round": meta.get("round"),
        "tick_start": meta.get("tick_start"),
        "tick_end": meta.get("tick_end"),
        "kill_ticks": meta.get("kill_ticks") or [],
        "kill_count": len(meta.get("kill_ticks") or []),
        "short_type": meta.get("short_type"),
        "source": "upload_meta",
        "evidence_version": 1,
    }


def resolve_pending(store, *, root: Path | None = None, limit: int = 500,
                    run_id: str = "linkage-resume") -> dict:
    """Resume pass: link pending evidence whose platform post is now known.

    Resolution sources, in order: a stored platform mirror, then the post's own
    ``short_linkage`` row already carrying a post id.
    """
    root = Path(root or LINKAGE_DIR)
    pending = store.pending_linkage(limit=limit)
    stats = {"checked": len(pending), "linked": 0, "unresolved": 0, "skipped": 0}
    for row in pending:
        short_id = row["short_id"]
        mirror = None
        for platform_dir in sorted(p for p in root.iterdir() if p.is_dir()) \
                if root.exists() else []:
            for path in platform_dir.glob("*.json"):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if payload.get("short_id") == short_id:
                    mirror = (platform_dir.name, path.stem, payload)
                    break
            if mirror:
                break
        if mirror is None:
            stats["skipped"] += 1
            continue
        platform, post_id, payload = mirror
        try:
            confirm_linkage(store, platform=platform, post_id=post_id,
                            evidence=payload, run_id=run_id, root=root)
            stats["linked"] += 1
        except Exception as exc:  # noqa: BLE001 - keep going, mark unresolved
            store.mark_linkage(short_id, status=STATUS_UNRESOLVED,
                               reason=f"{type(exc).__name__}: {exc}", run_id=run_id)
            stats["unresolved"] += 1
    return stats


def linkage_summary(store) -> dict:
    rows = list(store.conn.execute(
        "SELECT status, COUNT(*) n FROM short_linkage GROUP BY status"))
    by_status = {row["status"]: row["n"] for row in rows}
    linked = list(store.conn.execute(
        "SELECT platform, COUNT(*) n FROM short_linkage "
        "WHERE status=? AND post_id IS NOT NULL GROUP BY platform", (STATUS_LINKED,)))
    return {
        "total": sum(by_status.values()),
        "by_status": by_status,
        "linked_by_platform": {row["platform"]: row["n"] for row in linked},
        "updated_at": iso(utcnow()),
    }
