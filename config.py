"""
CS2Archive — Central Configuration

All paths, API endpoints, rate limits, and shared settings.
"""

from pathlib import Path
from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    """Application settings loaded from .env file and environment variables."""

    # ── API Keys ──────────────────────────────────────────────────────────
    faceit_api_key: str = Field(default="", description="FACEIT Data API v4 key (player/match lookup only)")
    youtube_api_key: str = Field(default="", description="YouTube Data API v3 key")

    # ── Paths ─────────────────────────────────────────────────────────────
    demo_storage_dir: Path = Field(default=Path("./demos"), description="Root directory for downloaded demos")

    # ── FACEIT ───────────────────────────────────────────────────────────
    faceit_data_api_base: str = "https://open.faceit.com/data/v4"
    hltv_base_url: str = "https://www.hltv.org"
    hltv_demo_download_url: str = "https://www.hltv.org/interfaces/download.php"

    # ── Rate Limiting ─────────────────────────────────────────────────────
    hltv_request_delay_min: float = 2.0   # Minimum seconds between HLTV requests
    hltv_request_delay_max: float = 5.0   # Maximum seconds (randomized within range)
    faceit_request_delay: float = 0.5     # Seconds between FACEIT API requests

    # ── Downloads ─────────────────────────────────────────────────────────
    download_timeout: int = 300           # Seconds before a download times out
    max_concurrent_downloads: int = 2     # Parallel downloads at once
    chunk_size: int = 8192                # Bytes per download chunk

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }

    @property
    def hltv_demo_dir(self) -> Path:
        """Directory for HLTV demos."""
        path = self.demo_storage_dir / "hltv"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def faceit_demo_dir(self) -> Path:
        """Directory for FACEIT demos."""
        path = self.demo_storage_dir / "faceit"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def temp_dir(self) -> Path:
        """Temporary directory for in-progress downloads."""
        path = self.demo_storage_dir / ".tmp"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def has_faceit_key(self) -> bool:
        return bool(self.faceit_api_key and self.faceit_api_key != "your_data_api_key_here")


# Singleton settings instance
settings = Settings()
