"""
CS2Archive — Data Models

Shared Pydantic models for matches, demos, and download results.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


class DemoSource(str, Enum):
    """Where a demo was obtained from."""
    HLTV = "hltv"
    FACEIT = "faceit"


class MatchInfo(BaseModel):
    """Metadata for a CS2 match."""
    match_id: str = Field(description="Unique match identifier (source-specific)")
    source: DemoSource
    team1: str = Field(default="Unknown")
    team2: str = Field(default="Unknown")
    score: str = Field(default="", description="e.g. '16-9'")
    map_name: str = Field(default="Unknown")
    date: Optional[datetime] = None
    event: str = Field(default="", description="Tournament/event name")
    url: str = Field(default="", description="Source URL for the match page")
    demo_url: str = Field(default="", description="Direct demo download URL or resource URL")

    @property
    def display_name(self) -> str:
        """Human-readable match label."""
        date_str = self.date.strftime("%Y-%m-%d") if self.date else "unknown date"
        score_str = f" ({self.score})" if self.score else ""
        return f"{self.team1} vs {self.team2}{score_str} on {self.map_name} — {date_str}"


class DownloadStatus(str, Enum):
    """Status of a demo download."""
    PENDING = "pending"
    DOWNLOADING = "downloading"
    EXTRACTING = "extracting"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"       # Already downloaded


class DownloadResult(BaseModel):
    """Result of a single demo download attempt."""
    match: MatchInfo
    status: DownloadStatus
    demo_path: Optional[Path] = None
    file_size_mb: float = 0.0
    error: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    @property
    def duration_seconds(self) -> float:
        if self.started_at and self.completed_at:
            return (self.completed_at - self.started_at).total_seconds()
        return 0.0

    @property
    def is_success(self) -> bool:
        return self.status == DownloadStatus.COMPLETED


class DemoRecord(BaseModel):
    """Persistent record of a downloaded demo for history tracking."""
    match_id: str
    source: DemoSource
    match_display: str
    demo_path: str
    file_size_mb: float
    downloaded_at: datetime = Field(default_factory=datetime.now)


class PlayerAccount(BaseModel):
    """Saved player account linking Faceit and Steam profiles."""
    nickname: str = Field(description="Short display name / alias")
    faceit_url: str = Field(default="", description="Full Faceit profile URL")
    faceit_nickname: str = Field(default="", description="Extracted Faceit nickname from URL")
    steam_url: str = Field(default="", description="Full Steam profile URL")
    steam_id: str = Field(default="", description="Steam64 ID (numeric) resolved from steam_url")
    hltv_player_id: str = Field(default="", description="Numeric HLTV player ID from profile URL")
    hltv_player_url: str = Field(default="", description="Canonical HLTV profile URL")
    # Video settings from prosettings.net (optional; used for POV capture aspect)
    resolution: str = Field(default="", description="e.g. 1280x960")
    aspect_ratio: str = Field(default="", description="e.g. 4:3")
    scaling_mode: str = Field(default="", description="Stretched / Native / Black Bars")
    capture_width: int = Field(default=0, description="Capture width for csdm render")
    capture_height: int = Field(default=0, description="Capture height for csdm render")
    video_settings_source: str = Field(default="", description="prosettings | default | manual")
    viewmodel_fov: Optional[float] = Field(default=None, description="viewmodel_fov from prosettings")
    viewmodel_offset_x: Optional[float] = Field(default=None)
    viewmodel_offset_y: Optional[float] = Field(default=None)
    viewmodel_offset_z: Optional[float] = Field(default=None)
    viewmodel_presetpos: Optional[int] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
