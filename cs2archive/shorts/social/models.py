"""Plain data shapes shared by collectors, store and reports.

Nothing here performs I/O. Counts are ``None`` when a platform does not expose
them — a missing metric is never coerced to 0.
"""

from __future__ import annotations

from dataclasses import dataclass, field

METRIC_FIELDS = ("views", "plays", "likes", "comments", "shares", "saves", "followers")

# Format labels. ``short`` means short-form vertical/square footage (Shorts,
# Reels, TikTok). ``clip`` means a clip artifact whose aspect/format is
# unverified; ``long`` means regular long-form video.
FORMAT_SHORT = "short"
FORMAT_CANDIDATE_SHORT = "candidate_short"  # duration suggests it; not verified
FORMAT_LONG = "long"
FORMAT_CLIP = "clip"
FORMAT_UNKNOWN = "unknown"

STATUS_SUPPORTED = "supported"
STATUS_PARTIAL = "partial"
STATUS_BLOCKED = "blocked"
STATUS_UNAVAILABLE = "unavailable"
STATUSES = (STATUS_SUPPORTED, STATUS_PARTIAL, STATUS_BLOCKED, STATUS_UNAVAILABLE)


@dataclass
class PostRecord:
    platform: str
    post_id: str
    url: str | None = None
    creator_id: str | None = None
    creator_name: str | None = None
    caption: str | None = None
    published_at: str | None = None
    duration_seconds: float | None = None
    media_kind: str | None = None
    format_label: str = FORMAT_UNKNOWN
    format_confidence: float = 0.0
    format_evidence: dict = field(default_factory=dict)
    notes: dict = field(default_factory=dict)


@dataclass
class MetricRecord:
    views: int | None = None
    plays: int | None = None
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    saves: int | None = None
    followers: int | None = None
    metric_source: str = "none"
    unavailable: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    def as_row(self) -> dict:
        row = {name: getattr(self, name) for name in METRIC_FIELDS}
        row["metric_source"] = self.metric_source
        row["unavailable"] = list(self.unavailable)
        row["extra"] = dict(self.extra)
        return row


@dataclass
class FetchResult:
    """One preserved raw platform response and where its bytes live."""

    endpoint: str
    name: str
    payload: object
    http_status: int
    fetched_at: str
    params_note: str = ""
    content_path: str | None = None
    sha256: str | None = None
    byte_size: int = 0
    raw_id: int | None = None


@dataclass
class CollectedPost:
    """One post tied to the raw response and observation time that produced it."""

    post: PostRecord
    metrics: MetricRecord
    observed_at: str
    raw_id: int | None = None


@dataclass
class PlatformStatus:
    platform: str
    status: str
    reason: str
    mode: str
    requirements: list[str] = field(default_factory=list)


@dataclass
class LinkRecord:
    platform: str
    post_id: str
    foundation_clip_id: str | None
    match_id: str | None
    method: str
    confidence: float
    ambiguous: bool
    evidence: dict = field(default_factory=dict)
    map_name: str | None = None
    player: str | None = None
    round_no: int | None = None
