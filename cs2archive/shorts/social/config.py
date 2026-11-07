"""Load and validate ``sources.json`` — the sweep configuration.

Defaults are deliberately conservative (small pages, short windows, no free-text
search) so a first run is cheap and bounded.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_MIN_INTERVAL = 1.0
DEFAULT_MAX_REQUESTS = 120
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_WINDOW_DAYS = 120


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [v for v in value if v not in (None, "")]
    return [value]


@dataclass
class PlatformConfig:
    name: str
    enabled: bool = True
    mode: str = ""
    creators: list[str] = field(default_factory=list)
    communities: list[str] = field(default_factory=list)
    video_urls: list[str] = field(default_factory=list)
    video_ids: list[str] = field(default_factory=list)
    max_pages: int = 3
    max_posts: int = 100
    window_days: int | None = None
    extra: dict = field(default_factory=dict)

    def window_start(self, now: datetime) -> str | None:
        days = self.window_days
        if not days:
            return None
        return (now - timedelta(days=int(days))).isoformat().replace("+00:00", "Z")


@dataclass
class SweepConfig:
    path: Path
    database: Path
    raw_root: Path
    window_start: str | None
    window_end: str | None
    min_interval_seconds: float
    max_requests: int
    max_attempts: int
    platforms: dict[str, PlatformConfig]
    imports: list[dict]
    youtube_daily_quota: int
    min_train_rows: int
    foundation_dir: Path
    allstar_store: Path | None
    raw: dict

    def platform(self, name: str) -> PlatformConfig:
        return self.platforms.get(name) or PlatformConfig(name=name, enabled=False)


def _resolve(base: Path, value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (base / path)


def load_config(path: Path) -> SweepConfig:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"sweep config not found: {path}")
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict):
        raise ValueError("sweep config root must be a JSON object")
    base = path.resolve().parent
    now = datetime.now(timezone.utc)
    window = raw.get("window") or {}
    window_start = window.get("start")
    window_end = window.get("end")
    if not window_start:
        days = _as_int(window.get("max_days"), DEFAULT_WINDOW_DAYS)
        window_start = (now - timedelta(days=days)).isoformat().replace("+00:00", "Z")

    defaults = raw.get("defaults") or {}
    platforms: dict[str, PlatformConfig] = {}
    for name, block in (raw.get("platforms") or {}).items():
        block = block or {}
        merged = {**defaults, **block}
        platforms[name] = PlatformConfig(
            name=name,
            enabled=bool(merged.get("enabled", True)),
            mode=str(merged.get("mode") or ""),
            creators=[str(v) for v in _as_list(merged.get("creators"))],
            communities=[str(v) for v in _as_list(merged.get("communities"))],
            video_urls=[str(v) for v in _as_list(merged.get("video_urls"))],
            video_ids=[str(v) for v in _as_list(merged.get("video_ids"))],
            max_pages=_as_int(merged.get("max_pages"), 3),
            max_posts=_as_int(merged.get("max_posts"), 100),
            window_days=merged.get("window_days"),
            extra={k: v for k, v in merged.items() if k not in {
                "enabled", "mode", "creators", "communities", "video_urls",
                "video_ids", "max_pages", "max_posts", "window_days"}},
        )

    storage = raw.get("storage") or {}
    storage.setdefault("database", raw.get("database"))
    storage.setdefault("raw_root", raw.get("raw_root"))
    rate = raw.get("rate_limit") or {}
    retry = raw.get("retry") or {}
    training = raw.get("training") or {}
    foundation = raw.get("foundation") or {}
    allstar_path = foundation.get("allstar_store")

    return SweepConfig(
        path=path,
        database=_resolve(base, storage.get("database", "data/social-sweep/shorts_evidence.sqlite3")),
        raw_root=_resolve(base, storage.get("raw_root", "data/social-sweep/raw")),
        window_start=window_start,
        window_end=window_end,
        min_interval_seconds=_as_float(rate.get("min_interval_seconds"), DEFAULT_MIN_INTERVAL),
        max_requests=_as_int(rate.get("max_requests_per_run"), DEFAULT_MAX_REQUESTS),
        max_attempts=min(_as_int(retry.get("max_attempts"), DEFAULT_MAX_ATTEMPTS), 3),
        platforms=platforms,
        imports=list(raw.get("imports") or []),
        youtube_daily_quota=_as_int(rate.get("youtube_daily_quota"), 5000),
        min_train_rows=_as_int(training.get("min_rows_per_platform"), 25),
        foundation_dir=_resolve(base, foundation.get("dir", "data/shorts-foundation")),
        allstar_store=_resolve(base, allstar_path) if allstar_path else None,
        raw=raw,
    )
