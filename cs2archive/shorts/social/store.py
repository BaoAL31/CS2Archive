"""SQLite persistence layer: append-only snapshots, repeat-safe upserts, provenance.

Everything that reaches the database or the raw-response folder passes through
:mod:`cs2archive.shorts.social.safety` sanitisation first.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from cs2archive.shorts.social.models import METRIC_FIELDS, LinkRecord, MetricRecord, PostRecord
from cs2archive.shorts.social.safety import (
    dumps, redact_payload, sanitize_text, sanitize_url_params, sha256_text, utcnow,
)
from cs2archive.shorts.social.schema import SCHEMA_STATEMENTS

# Columns a repeat collection may improve but must never null out.
_POST_FILL_COLUMNS = (
    "url", "creator_id", "creator_name", "caption", "published_at",
    "duration_seconds", "media_kind", "format_label", "format_confidence",
    "format_evidence", "notes",
)


@dataclass
class RawRef:
    raw_id: int
    content_path: str
    sha256: str
    byte_size: int


@dataclass
class RunCounters:
    pages_fetched: int = 0
    posts_seen: int = 0
    posts_kept: int = 0
    snapshots_written: int = 0
    error_count: int = 0
    units_spent: float = 0.0
    notes: dict = field(default_factory=dict)


class SocialStore:
    def __init__(self, path: Path, *, secrets: tuple[str, ...] = ()):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.secrets = tuple(s for s in secrets if s)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._init_schema()

    # ── lifecycle ─────────────────────────────────────────────────────────
    def _init_schema(self) -> None:
        with self.conn:
            for statement in SCHEMA_STATEMENTS:
                self.conn.execute(statement)
            self.conn.execute(
                "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('schema_version', '2')")
        self._migrate()

    def _migrate(self) -> None:
        """Additive migrations for databases created by an earlier sweep."""
        with self.conn:
            # Pre-fix rows lost their `mode` to a placeholder mismatch. The run id
            # prefix is authoritative, so repair the label rather than the data.
            self.conn.execute(
                "UPDATE collection_runs SET mode='refresh' "
                "WHERE run_id LIKE 'refresh-%' AND mode <> 'refresh'")
        columns = {row["name"] for row in self.conn.execute(
            "PRAGMA table_info(clip_identity_links)")}
        additions = {"map_name": "TEXT", "player": "TEXT", "round_no": "INTEGER"}
        with self.conn:
            for name, sql_type in additions.items():
                if name not in columns:
                    self.conn.execute(
                        f"ALTER TABLE clip_identity_links ADD COLUMN {name} {sql_type}")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> SocialStore:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ── secrets ───────────────────────────────────────────────────────────
    def scrub(self, text: str) -> str:
        return sanitize_text(text, self.secrets)

    def scrub_params(self, params: dict | None) -> str:
        return sanitize_url_params(params, self.secrets)

    # ── runs and errors ───────────────────────────────────────────────────
    def unique_run_id(self, base: str) -> str:
        """Disambiguate a run id so two sweeps with the same clock never collide."""
        candidate = base
        suffix = 2
        while self.conn.execute(
                "SELECT 1 FROM collection_runs WHERE run_id=?", (candidate,)).fetchone():
            candidate = f"{base}-r{suffix}"
            suffix += 1
        return candidate

    def start_run(self, *, run_id: str, platform: str, mode: str,
                  config_path: str | None = None, config_sha256: str | None = None,
                  window_start: str | None = None, window_end: str | None = None,
                  started_at: str | None = None) -> str:
        with self.conn:
            self.conn.execute(
                "INSERT INTO collection_runs (run_id, platform, mode, status, "
                "started_at, config_path, config_sha256, window_start, window_end) "
                "VALUES (?,?,?,'collecting',?,?,?,?,?)",
                (run_id, platform, mode, started_at or utcnow().isoformat(),
                 config_path, config_sha256, window_start, window_end))
        return run_id

    def ensure_run(self, run_id: str, platform: str, *,
                   mode: str = "instrument") -> str:
        """Guarantee the run row exists so error rows satisfy their FK.

        Instrumentation (analytics, linkage) can be invoked outside a platform
        collection, so it must not depend on the caller having started a run.
        """
        if self.conn.execute("SELECT 1 FROM collection_runs WHERE run_id=?",
                             (run_id,)).fetchone():
            return run_id
        return self.start_run(run_id=run_id, platform=platform, mode=mode)

    def finish_run(self, run_id: str, *, status: str, reason: str | None = None,
                   counters: RunCounters | None = None, finished_at: str | None = None) -> None:
        counters = counters or RunCounters()
        with self.conn:
            self.conn.execute(
                "UPDATE collection_runs SET status=?, status_reason=?, finished_at=?, "
                "pages_fetched=?, posts_seen=?, posts_kept=?, snapshots_written=?, "
                "error_count=?, units_spent=?, notes=? WHERE run_id=?",
                (status, self.scrub(reason) if reason else None,
                 finished_at or utcnow().isoformat(), counters.pages_fetched,
                 counters.posts_seen, counters.posts_kept, counters.snapshots_written,
                 counters.error_count, counters.units_spent,
                 dumps(counters.notes), run_id))

    def log_error(self, *, run_id: str, platform: str, stage: str, code: str,
                  message: str, attempt: int | None = None, max_attempts: int | None = None,
                  http_status: int | None = None, terminal: bool = False,
                  request_note: str = "", occurred_at: str | None = None,
                  counters: RunCounters | None = None) -> None:
        if counters is not None:
            counters.error_count += 1
        with self.conn:
            self.conn.execute(
                "INSERT INTO collection_errors (run_id, platform, occurred_at, stage, "
                "error_code, message, attempt, max_attempts, http_status, terminal, "
                "request_note) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, platform, occurred_at or utcnow().isoformat(), stage, code,
                 self.scrub(message)[:2000], attempt, max_attempts, http_status,
                 1 if terminal else 0, self.scrub(request_note)))

    # ── raw responses ─────────────────────────────────────────────────────
    def write_raw(self, *, platform: str, run_id: str, name: str, payload,
                  endpoint: str, http_status: int | None, fetched_at: str,
                  params_note: str = "", raw_root: Path | None = None) -> RawRef:
        raw_root = Path(raw_root or (self.path.parent / "raw"))
        safe_name = "".join(c if c.isalnum() or c in "-_.@" else "_" for c in str(name))
        rel = Path(platform) / run_id / f"{safe_name}.json"
        target = raw_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        clean = redact_payload(payload, self.secrets)
        text = json.dumps(clean, ensure_ascii=False, indent=1, sort_keys=True)
        target.write_text(text, encoding="utf-8")
        digest = sha256_text(text)
        size = len(text.encode("utf-8"))
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO raw_responses (run_id, platform, name, endpoint, fetched_at, "
                "http_status, content_path, sha256, byte_size, params_note) "
                "VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT (run_id, platform, name) DO NOTHING",
                (run_id, platform, safe_name, self.scrub(endpoint)[:500], fetched_at,
                 http_status, str(rel).replace("\\", "/"), digest, size,
                 self.scrub(params_note)[:500]))
            if cur.rowcount:
                raw_id = int(cur.lastrowid)
            else:
                raw_id = int(self.conn.execute(
                    "SELECT raw_id FROM raw_responses WHERE run_id=? AND platform=? AND name=?",
                    (run_id, platform, safe_name)).fetchone()["raw_id"])
        return RawRef(raw_id=raw_id, content_path=str(target), sha256=digest, byte_size=size)

    # ── posts ─────────────────────────────────────────────────────────────
    def upsert_post(self, post: PostRecord, *, run_id: str, raw_id: int | None,
                    observed_at: str) -> None:
        values = {
            "platform": post.platform,
            "post_id": str(post.post_id),
            "url": post.url,
            "creator_id": post.creator_id,
            "creator_name": post.creator_name,
            "caption": post.caption,
            "published_at": post.published_at,
            "first_observed_at": observed_at,
            "last_observed_at": observed_at,
            "duration_seconds": post.duration_seconds,
            "media_kind": post.media_kind,
            "format_label": post.format_label,
            "format_confidence": post.format_confidence,
            "format_evidence": dumps(post.format_evidence or {}),
            "source_run_id": run_id,
            "raw_response_id": raw_id,
            "notes": dumps(post.notes or {}),
        }
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        updates = ", ".join(
            f"{col} = COALESCE(excluded.{col}, posts.{col})" for col in _POST_FILL_COLUMNS)
        with self.conn:
            self.conn.execute(
                f"INSERT INTO posts ({columns}) VALUES ({placeholders}) "
                f"ON CONFLICT (platform, post_id) DO UPDATE SET {updates}, "
                "last_observed_at = MAX(posts.last_observed_at, excluded.last_observed_at)",
                tuple(values.values()))

    def ensure_post_stub(self, platform: str, post_id: str, *, url: str | None = None,
                         observed_at: str | None = None,
                         run_id: str | None = None) -> None:
        """Create a placeholder ``posts`` row so a link can reference it.

        Upload-time linkage knows the platform post id before any sweep has
        observed the post, and ``clip_identity_links`` has a foreign key onto
        ``posts`` — without this stub every upload-time link failed with
        ``FOREIGN KEY constraint failed`` and the upload had to be backfilled.
        The stub records only what is known; a later sweep fills the rest
        through ``upsert_post``'s COALESCE updates.
        """
        now = observed_at or datetime.now(timezone.utc).isoformat()
        with self.conn:
            self.conn.execute(
                "INSERT INTO posts (platform, post_id, url, first_observed_at, "
                "last_observed_at, source_run_id, notes) "
                "VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT (platform, post_id) DO NOTHING",
                (platform, str(post_id), url, now, now, run_id,
                 dumps({"stub": "upload_linkage"})))

    def post_exists(self, platform: str, post_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM posts WHERE platform=? AND post_id=?",
            (platform, str(post_id))).fetchone()
        return row is not None

    def existing_format(self, platform: str, post_id: str) -> dict | None:
        """Stored format label/confidence/evidence, for probe reuse on repeats."""
        row = self.conn.execute(
            "SELECT format_label, format_confidence, format_evidence FROM posts "
            "WHERE platform=? AND post_id=?", (platform, str(post_id))).fetchone()
        if not row or not row["format_label"]:
            return None
        try:
            import json as _json

            evidence = _json.loads(row["format_evidence"] or "{}")
        except (TypeError, ValueError):
            evidence = {}
        return {"format_label": row["format_label"],
                "format_confidence": row["format_confidence"],
                "format_evidence": evidence}

    def latest_observed_at(self, platform: str, post_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT MAX(observed_at) AS at FROM metric_snapshots WHERE platform=? AND post_id=?",
            (platform, str(post_id))).fetchone()
        return row["at"] if row else None

    # ── metric snapshots (append-only) ────────────────────────────────────
    @staticmethod
    def snapshot_id(post: PostRecord, observed_at: str, metric_source: str) -> str:
        return sha256_text(
            f"{post.platform}|{post.post_id}|{observed_at}|{metric_source}")

    def add_snapshot(self, post: PostRecord, metrics: MetricRecord, *,
                     observed_at: str, run_id: str, raw_id: int | None) -> bool:
        snapshot_id = self.snapshot_id(post, observed_at, metrics.metric_source)
        row = {name: getattr(metrics, name) for name in METRIC_FIELDS}
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO metric_snapshots (snapshot_id, platform, post_id, observed_at, "
                "run_id, views, plays, likes, comments, shares, saves, followers, "
                "metric_source, unavailable, raw_response_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT (snapshot_id) DO NOTHING",
                (snapshot_id, post.platform, str(post.post_id), observed_at, run_id,
                 row["views"], row["plays"], row["likes"], row["comments"], row["shares"],
                 row["saves"], row["followers"], metrics.metric_source,
                 dumps(metrics.unavailable), raw_id))
        return bool(cur.rowcount)

    def link_snapshot_provenance(self, post: PostRecord, *, observed_at: str,
                                 metric_source: str, raw_id: int | None,
                                 run_id: str) -> None:
        """Additive provenance backlink; the immutable snapshot is not touched.

        Needed for re-imported observations: their snapshot id is identical on
        every import, so the raw-response association lives here instead.
        """
        if raw_id is None:
            return
        snapshot_id = self.snapshot_id(post, observed_at, metric_source)
        with self.conn:
            self.conn.execute(
                "INSERT INTO snapshot_provenance (snapshot_id, raw_id, run_id) "
                "VALUES (?,?,?) ON CONFLICT (snapshot_id, raw_id) DO NOTHING",
                (snapshot_id, raw_id, run_id))

    # ── links, features, quarantine ───────────────────────────────────────
    def upsert_link(self, link: LinkRecord, *, run_id: str) -> None:
        link_id = sha256_text(
            f"{link.platform}|{link.post_id}|{link.method}|{link.foundation_clip_id}")
        with self.conn:
            self.conn.execute(
                "INSERT INTO clip_identity_links (link_id, platform, post_id, "
                "foundation_clip_id, match_id, map_name, player, round_no, method, "
                "confidence, ambiguous, evidence, created_run_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT (link_id) DO UPDATE SET "
                "confidence = MAX(clip_identity_links.confidence, excluded.confidence), "
                "ambiguous = MIN(clip_identity_links.ambiguous, excluded.ambiguous), "
                "map_name = COALESCE(excluded.map_name, clip_identity_links.map_name), "
                "player = COALESCE(excluded.player, clip_identity_links.player), "
                "round_no = COALESCE(excluded.round_no, clip_identity_links.round_no), "
                "evidence = excluded.evidence",
                (link_id, link.platform, str(link.post_id), link.foundation_clip_id,
                 link.match_id, link.map_name, link.player, link.round_no,
                 link.method, float(link.confidence),
                 1 if link.ambiguous else 0, dumps(link.evidence or {}), run_id))

    def set_feature(self, *, platform: str, post_id: str, feature: str,
                    value_num: float | None = None, value_text: str | None = None,
                    kind: str, provenance: str, confidence: float | None = None,
                    evidence: dict | None = None, run_id: str | None = None) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO post_features (platform, post_id, feature, value_num, "
                "value_text, kind, provenance, confidence, evidence, run_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT (platform, post_id, feature) DO UPDATE SET "
                "value_num = excluded.value_num, value_text = excluded.value_text, "
                "kind = excluded.kind, provenance = excluded.provenance, "
                "confidence = excluded.confidence, evidence = excluded.evidence, "
                "run_id = excluded.run_id",
                (platform, str(post_id), feature, value_num, value_text, kind,
                 provenance, confidence, dumps(evidence or {}), run_id))

    def clear_features(self, platform: str, post_id: str) -> None:
        """Derived features are rewritten on each enrich; metrics are not."""
        with self.conn:
            self.conn.execute(
                "DELETE FROM post_features WHERE platform=? AND post_id=?",
                (platform, str(post_id)))

    def clear_links(self) -> int:
        """Identity links are derived (rebuildable). Unlike metric snapshots and
        raw responses they are rewritten on every enrich so stale hypotheses from
        an older matcher cannot inflate later reports."""
        with self.conn:
            count = self.conn.execute("SELECT COUNT(*) FROM clip_identity_links")
            removed = int(count.fetchone()[0])
            self.conn.execute("DELETE FROM clip_identity_links")
        return removed

    def add_quarantine(self, *, platform: str, post_id: str, reason: str,
                       detail: dict | None = None, run_id: str | None = None) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO quarantines (platform, post_id, reason, detail, created_at, run_id) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT (platform, post_id, reason) DO NOTHING",
                (platform, str(post_id), reason, dumps(detail or {}),
                 utcnow().isoformat(), run_id))

    def record_import(self, *, platform: str, kind: str, path: str, digest: str,
                      rows_read: int, rows_kept: int, notes: dict | None = None) -> str:
        import_id = sha256_text(f"{platform}|{kind}|{digest}")
        with self.conn:
            self.conn.execute(
                "INSERT INTO authorized_imports (import_id, platform, kind, path, sha256, "
                "rows_read, rows_kept, created_at, notes) VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT (import_id) DO NOTHING",
                (import_id, platform, kind, str(path), digest, rows_read, rows_kept,
                 utcnow().isoformat(), dumps(notes or {})))
        return import_id

    # ── durable short → demo linkage ──────────────────────────────────────
    _LINKAGE_FIELDS = (
        "short_id", "platform", "post_id", "demo_path", "match_id", "map",
        "player_steamid", "player_nick", "round", "tick_start", "tick_end",
        "kill_ticks", "kill_count", "short_type", "source", "selection",
        "status", "reason", "evidence",
    )

    def upsert_short_linkage(self, record: dict, *, status: str,
                             run_id: str | None = None) -> str:
        """Idempotent: the same evidence + status rewrites nothing observable."""
        short_id = str(record.get("short_id") or "")
        if not short_id:
            raise ValueError("linkage record needs a short_id")
        now = utcnow().isoformat()
        values = {
            "short_id": short_id,
            "platform": record.get("platform"),
            "post_id": str(record["post_id"]) if record.get("post_id") else None,
            "demo_path": str(record.get("demo_path") or ""),
            "match_id": record.get("match_id"),
            "map": record.get("map"),
            "player_steamid": record.get("player_steamid"),
            "player_nick": record.get("player_nick"),
            "round": record.get("round"),
            "tick_start": record.get("tick_start"),
            "tick_end": record.get("tick_end"),
            "kill_ticks": dumps(record.get("kill_ticks") or []),
            "kill_count": record.get("kill_count"),
            "short_type": record.get("short_type"),
            "source": record.get("source"),
            "selection": dumps(record.get("selection") or {}),
            "status": status,
            "reason": record.get("reason"),
            # Explicit evidence wins so consumers do not have to drill into a
            # nested copy; otherwise the whole record is the provenance.
            "evidence": dumps(
                record["evidence"] if isinstance(record.get("evidence"), dict)
                else {k: v for k, v in record.items()
                      if k not in {"mirrors", "selection"}}),
        }
        columns = ", ".join(self._LINKAGE_FIELDS)
        placeholders = ", ".join("?" for _ in self._LINKAGE_FIELDS)
        # NOTE: SQLite rejects a table-qualified column on the left of SET, so the
        # always-overwrite fields are listed plainly and the rest use COALESCE.
        updates = ", ".join(
            f"{name} = COALESCE(excluded.{name}, short_linkage.{name})"
            for name in self._LINKAGE_FIELDS
            if name not in {"short_id", "status"})
        updates += (", status = excluded.status, "
                    "updated_at = excluded.updated_at")
        with self.conn:
            self.conn.execute(
                f"INSERT INTO short_linkage ({columns}, created_at, updated_at) "
                f"VALUES ({placeholders}, ?, ?) "
                f"ON CONFLICT (short_id) DO UPDATE SET {updates}",
                tuple(values[name] for name in self._LINKAGE_FIELDS) + (now, now))
        return short_id

    def mark_linkage(self, short_id: str, *, status: str, reason: str,
                     run_id: str | None = None) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE short_linkage SET status=?, reason=?, updated_at=? "
                "WHERE short_id=?",
                (status, self.scrub(str(reason))[:1000], utcnow().isoformat(),
                 short_id))

    def short_linkage(self, short_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM short_linkage WHERE short_id=?", (str(short_id),)).fetchone()
        return dict(row) if row else None

    def linkage_for_post(self, platform: str, post_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM short_linkage WHERE platform=? AND post_id=?",
            (platform, str(post_id))).fetchone()
        return dict(row) if row else None

    def pending_linkage(self, *, limit: int = 500) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM short_linkage WHERE status='pending' "
            "ORDER BY created_at LIMIT ?", (limit,)))

    def linkage_rows(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM short_linkage ORDER BY status, platform, post_id"))

    # ── analytics (separate metric family, never the public views target) ──
    def add_analytics_snapshot(self, *, platform: str, post_id: str, window_start: str,
                               window_end: str, metrics: dict, metric_family: str,
                               fetched_at: str, run_id: str,
                               raw_id: int | None = None,
                               unavailable: list[str] | None = None) -> bool:
        analytics_id = sha256_text(
            f"{platform}|{post_id}|{metric_family}|{window_start}|{window_end}")
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO analytics_snapshots (analytics_id, platform, post_id, "
                "metric_family, window_start, window_end, fetched_at, run_id, "
                "impressions, views, ctr, average_view_duration, "
                "average_view_percentage, estimated_minutes_watched, "
                "subscribers_gained, unavailable, raw_response_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT (analytics_id) DO NOTHING",
                (analytics_id, platform, str(post_id), metric_family, window_start,
                 window_end, fetched_at, run_id,
                 metrics.get("impressions"), metrics.get("views"), metrics.get("ctr"),
                 metrics.get("average_view_duration"),
                 metrics.get("average_view_percentage"),
                 metrics.get("estimated_minutes_watched"),
                 metrics.get("subscribers_gained"),
                 dumps(unavailable or []), raw_id))
        return bool(cur.rowcount)

    def analytics_for_post(self, platform: str, post_id: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM analytics_snapshots WHERE platform=? AND post_id=? "
            "ORDER BY window_start, metric_family", (platform, str(post_id))))

    def analytics_counts(self) -> dict:
        rows = list(self.conn.execute(
            "SELECT platform, metric_family, COUNT(*) n, "
            "COUNT(DISTINCT post_id) posts FROM analytics_snapshots "
            "GROUP BY platform, metric_family"))
        return {f"{row['platform']}:{row['metric_family']}":
                {"rows": row["n"], "posts": row["posts"]} for row in rows}

    # ── discovery provenance ──────────────────────────────────────────────
    def record_discovery_run(self, *, run_id: str, platform: str, queries: list[str],
                             candidates: int, accepted: int, units_spent: float,
                             notes: dict | None = None) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO discovery_runs (run_id, platform, queries, candidates, "
                "accepted, units_spent, started_at, notes) VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT (run_id) DO NOTHING",
                (run_id, platform, dumps(list(queries)), candidates, accepted,
                 units_spent, utcnow().isoformat(), dumps(notes or {})))

    def upsert_discovered_creator(self, *, platform: str, creator_id: str,
                                  handle: str | None, name: str | None,
                                  score: float, evidence: dict, run_id: str,
                                  accepted: bool) -> None:
        now = utcnow().isoformat()
        with self.conn:
            self.conn.execute(
                "INSERT INTO discovered_creators (platform, creator_id, handle, name, "
                "score, accepted, evidence, run_id, first_seen, last_seen) "
                "VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT (platform, creator_id) DO UPDATE SET "
                "handle = COALESCE(excluded.handle, discovered_creators.handle), "
                "name = COALESCE(excluded.name, discovered_creators.name), "
                "score = excluded.score, accepted = excluded.accepted, "
                "evidence = excluded.evidence, run_id = excluded.run_id, "
                "last_seen = excluded.last_seen",
                (platform, creator_id, handle, name, score, 1 if accepted else 0,
                 dumps(evidence), run_id, now, now))

    def accepted_creators(self, platform: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM discovered_creators WHERE platform=? AND accepted=1 "
            "ORDER BY score DESC, creator_id", (platform,)))

    def quarantined_posts(self) -> set[tuple[str, str]]:
        return {(r["platform"], r["post_id"]) for r in self.conn.execute(
            "SELECT platform, post_id FROM quarantines WHERE resolved_at IS NULL")}

    # ── read helpers for report / train ───────────────────────────────────
    def runs(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM collection_runs ORDER BY started_at DESC"))

    def errors(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM collection_errors ORDER BY error_id DESC"))

    def counts(self) -> dict:
        def one(sql: str) -> int:
            return int(self.conn.execute(sql).fetchone()[0])
        return {
            "posts": one("SELECT COUNT(*) FROM posts"),
            "snapshots": one("SELECT COUNT(*) FROM metric_snapshots"),
            "raw_responses": one("SELECT COUNT(*) FROM raw_responses"),
            "links": one("SELECT COUNT(*) FROM clip_identity_links"),
            "features": one("SELECT COUNT(*) FROM post_features"),
            "quarantines": one("SELECT COUNT(*) FROM quarantines"),
            "runs": one("SELECT COUNT(*) FROM collection_runs"),
            "errors": one("SELECT COUNT(*) FROM collection_errors"),
        }

    def latest_snapshots(self) -> list[sqlite3.Row]:
        """Newest snapshot per post, joined with post metadata (read-only)."""
        return list(self.conn.execute(
            """
            SELECT s.*, p.creator_id, p.creator_name, p.published_at, p.duration_seconds,
                   p.format_label, p.format_confidence, p.format_evidence,
                   p.media_kind, p.url, p.caption
            FROM metric_snapshots s
            JOIN posts p ON p.platform = s.platform AND p.post_id = s.post_id
            WHERE s.snapshot_id = (
                SELECT s2.snapshot_id FROM metric_snapshots s2
                WHERE s2.platform = s.platform AND s2.post_id = s.post_id
                ORDER BY s2.observed_at DESC, s2.snapshot_id DESC LIMIT 1
            )
            ORDER BY s.platform, s.observed_at
            """))

    def links_with_confidence(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM clip_identity_links WHERE ambiguous = 0 AND confidence >= 0.8"))

    def features_for(self, platform: str, post_id: str) -> dict[str, sqlite3.Row]:
        rows = self.conn.execute(
            "SELECT * FROM post_features WHERE platform=? AND post_id=?",
            (platform, str(post_id)))
        return {r["feature"]: r for r in rows}
