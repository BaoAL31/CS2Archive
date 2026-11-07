"""SQLite schema for the Shorts social-evidence store.

Design rules enforced here (not just documented):

* ``metric_snapshots`` is append-only: BEFORE UPDATE/DELETE triggers raise.
* ``raw_responses`` is append-only for the same reason (provenance).
* Counts that a platform does not expose are stored as SQL NULL, never 0.
* Uniqueness on ``(platform, post_id)`` and a content-addressed snapshot id make
  re-ingesting the same preserved raw response a no-op.
"""

from __future__ import annotations

SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS collection_runs (
        run_id TEXT PRIMARY KEY,
        platform TEXT NOT NULL,
        mode TEXT NOT NULL,
        status TEXT NOT NULL,
        status_reason TEXT,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        config_path TEXT,
        config_sha256 TEXT,
        window_start TEXT,
        window_end TEXT,
        pages_fetched INTEGER NOT NULL DEFAULT 0,
        posts_seen INTEGER NOT NULL DEFAULT 0,
        posts_kept INTEGER NOT NULL DEFAULT 0,
        snapshots_written INTEGER NOT NULL DEFAULT 0,
        error_count INTEGER NOT NULL DEFAULT 0,
        units_spent REAL,
        notes TEXT
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS collection_errors (
        error_id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL REFERENCES collection_runs(run_id),
        platform TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        stage TEXT NOT NULL,
        error_code TEXT NOT NULL,
        message TEXT NOT NULL,
        attempt INTEGER,
        max_attempts INTEGER,
        http_status INTEGER,
        terminal INTEGER NOT NULL DEFAULT 0,
        request_note TEXT
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS discovery_runs (
        run_id TEXT PRIMARY KEY,
        platform TEXT NOT NULL,
        queries TEXT NOT NULL,
        candidates INTEGER NOT NULL,
        accepted INTEGER NOT NULL,
        units_spent REAL,
        started_at TEXT NOT NULL,
        notes TEXT
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS discovered_creators (
        platform TEXT NOT NULL,
        creator_id TEXT NOT NULL,
        handle TEXT,
        name TEXT,
        score REAL,
        accepted INTEGER NOT NULL DEFAULT 0,
        evidence TEXT,
        run_id TEXT,
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        PRIMARY KEY (platform, creator_id)
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS short_linkage (
        short_id TEXT PRIMARY KEY,
        platform TEXT,
        post_id TEXT,
        demo_path TEXT NOT NULL,
        match_id TEXT,
        map TEXT,
        player_steamid TEXT,
        player_nick TEXT,
        round INTEGER,
        tick_start INTEGER,
        tick_end INTEGER,
        kill_ticks TEXT,
        kill_count INTEGER,
        short_type TEXT,
        source TEXT,
        selection TEXT,
        status TEXT NOT NULL,
        reason TEXT,
        evidence TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS analytics_snapshots (
        analytics_id TEXT PRIMARY KEY,
        platform TEXT NOT NULL,
        post_id TEXT NOT NULL,
        metric_family TEXT NOT NULL,
        window_start TEXT NOT NULL,
        window_end TEXT NOT NULL,
        fetched_at TEXT NOT NULL,
        run_id TEXT,
        impressions INTEGER,
        views INTEGER,
        ctr REAL,
        average_view_duration REAL,
        average_view_percentage REAL,
        estimated_minutes_watched REAL,
        subscribers_gained INTEGER,
        unavailable TEXT,
        raw_response_id INTEGER,
        UNIQUE (platform, post_id, metric_family, window_start, window_end)
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS raw_responses (
        raw_id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        platform TEXT NOT NULL,
        name TEXT NOT NULL,
        endpoint TEXT NOT NULL,
        fetched_at TEXT NOT NULL,
        http_status INTEGER,
        content_path TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        byte_size INTEGER NOT NULL,
        params_note TEXT,
        UNIQUE (run_id, platform, name)
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS posts (
        platform TEXT NOT NULL,
        post_id TEXT NOT NULL,
        url TEXT,
        creator_id TEXT,
        creator_name TEXT,
        caption TEXT,
        published_at TEXT,
        first_observed_at TEXT NOT NULL,
        last_observed_at TEXT NOT NULL,
        duration_seconds REAL,
        media_kind TEXT,
        format_label TEXT,
        format_confidence REAL,
        format_evidence TEXT,
        source_run_id TEXT,
        raw_response_id INTEGER,
        notes TEXT,
        PRIMARY KEY (platform, post_id)
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS metric_snapshots (
        snapshot_id TEXT PRIMARY KEY,
        platform TEXT NOT NULL,
        post_id TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        run_id TEXT NOT NULL,
        views INTEGER,
        plays INTEGER,
        likes INTEGER,
        comments INTEGER,
        shares INTEGER,
        saves INTEGER,
        followers INTEGER,
        metric_source TEXT NOT NULL,
        unavailable TEXT,
        raw_response_id INTEGER,
        FOREIGN KEY (platform, post_id) REFERENCES posts (platform, post_id)
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS snapshot_provenance (
        snapshot_id TEXT NOT NULL,
        raw_id INTEGER NOT NULL,
        run_id TEXT NOT NULL,
        UNIQUE (snapshot_id, raw_id),
        FOREIGN KEY (snapshot_id) REFERENCES metric_snapshots (snapshot_id)
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS clip_identity_links (
        link_id TEXT PRIMARY KEY,
        platform TEXT NOT NULL,
        post_id TEXT NOT NULL,
        foundation_clip_id TEXT,
        match_id TEXT,
        map_name TEXT,
        player TEXT,
        round_no INTEGER,
        method TEXT NOT NULL,
        confidence REAL NOT NULL,
        ambiguous INTEGER NOT NULL DEFAULT 0,
        evidence TEXT,
        created_run_id TEXT,
        FOREIGN KEY (platform, post_id) REFERENCES posts (platform, post_id)
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS post_features (
        platform TEXT NOT NULL,
        post_id TEXT NOT NULL,
        feature TEXT NOT NULL,
        value_num REAL,
        value_text TEXT,
        kind TEXT NOT NULL,
        provenance TEXT NOT NULL,
        confidence REAL,
        evidence TEXT,
        run_id TEXT,
        PRIMARY KEY (platform, post_id, feature),
        FOREIGN KEY (platform, post_id) REFERENCES posts (platform, post_id)
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS quarantines (
        platform TEXT NOT NULL,
        post_id TEXT NOT NULL,
        reason TEXT NOT NULL,
        detail TEXT,
        created_at TEXT NOT NULL,
        run_id TEXT,
        resolved_at TEXT,
        PRIMARY KEY (platform, post_id, reason),
        FOREIGN KEY (platform, post_id) REFERENCES posts (platform, post_id)
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS authorized_imports (
        import_id TEXT PRIMARY KEY,
        platform TEXT NOT NULL,
        kind TEXT NOT NULL,
        path TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        rows_read INTEGER NOT NULL,
        rows_kept INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        notes TEXT
    ) STRICT
    """,
    "CREATE INDEX IF NOT EXISTS idx_posts_platform_published "
    "ON posts (platform, published_at)",
    "CREATE INDEX IF NOT EXISTS idx_snapshots_post "
    "ON metric_snapshots (platform, post_id, observed_at)",
    "CREATE INDEX IF NOT EXISTS idx_links_post ON clip_identity_links (platform, post_id)",
    "CREATE INDEX IF NOT EXISTS idx_features_kind ON post_features (kind, feature)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_linkage_post "
    "ON short_linkage (platform, post_id) WHERE post_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_linkage_status ON short_linkage (status)",
    "CREATE INDEX IF NOT EXISTS idx_analytics_post "
    "ON analytics_snapshots (platform, post_id, window_start)",
    """
    CREATE TRIGGER IF NOT EXISTS metric_snapshots_no_update
    BEFORE UPDATE ON metric_snapshots
    BEGIN
        SELECT RAISE(ABORT, 'metric_snapshots is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS metric_snapshots_no_delete
    BEFORE DELETE ON metric_snapshots
    BEGIN
        SELECT RAISE(ABORT, 'metric_snapshots is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS analytics_snapshots_no_update
    BEFORE UPDATE ON analytics_snapshots
    BEGIN
        SELECT RAISE(ABORT, 'analytics_snapshots is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS analytics_snapshots_no_delete
    BEFORE DELETE ON analytics_snapshots
    BEGIN
        SELECT RAISE(ABORT, 'analytics_snapshots is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS raw_responses_no_update
    BEFORE UPDATE ON raw_responses
    BEGIN
        SELECT RAISE(ABORT, 'raw_responses is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS raw_responses_no_delete
    BEFORE DELETE ON raw_responses
    BEGIN
        SELECT RAISE(ABORT, 'raw_responses is append-only');
    END
    """,
)
