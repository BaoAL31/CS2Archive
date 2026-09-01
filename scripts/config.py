"""
CS2Archive — Central Configuration

All paths, API endpoints, rate limits, and shared settings.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from .env file and environment variables."""

    # ── API Keys ──────────────────────────────────────────────────────────
    faceit_api_key: str = Field(default="", description="FACEIT Data API v4 key (player/match lookup only)")
    faceit_downloads_token: str = Field(default="", description="FACEIT Downloads API token (demo file download)")
    youtube_api_key: str = Field(default="", description="YouTube Data API v3 key")

    # ── Paths ─────────────────────────────────────────────────────────────
    demo_storage_dir: Path = Field(default=Path("./demos"), description="Root directory for downloaded demos")
    csdm_cmd: str = Field(
        default=r"C:\Users\jembo\AppData\Local\Programs\cs-demo-manager\csdm.cmd",
        description="cs-demo-manager CLI (csdm.cmd)",
    )
    ffmpeg_exe: str = Field(
        default=r"C:\Users\jembo\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe",
    )
    ffprobe_exe: str = Field(
        default=r"C:\Users\jembo\AppData\Local\Microsoft\WinGet\Links\ffprobe.exe",
    )
    cs2_cfg_dir: Path = Field(
        default=Path(r"D:\Steam\steamapps\common\Counter-Strike Global Offensive\game\csgo\cfg"),
    )
    cs2util_root: Path = Field(
        default=Path(r"D:\Projects\CS2UtilArchive"),
        description="Sibling CS2UtilArchive checkout (overlay kernel + throws.parquet)",
    )
    hf_home: Path = Field(default=Path(r"D:/.cache/huggingface"))

    # ── FACEIT ───────────────────────────────────────────────────────────
    faceit_data_api_base: str = "https://open.faceit.com/data/v4"
    faceit_downloads_api_base: str = "https://api.faceit.com/download/v2"
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
    def hf_hub_cache(self) -> Path:
        return self.hf_home / "hub"

    @property
    def has_faceit_key(self) -> bool:
        return bool(self.faceit_api_key and self.faceit_api_key != "your_data_api_key_here")

    @property
    def has_faceit_downloads_token(self) -> bool:
        return bool(self.faceit_downloads_token and self.faceit_downloads_token != "your_downloads_token_here")


# Singleton settings instance
settings = Settings()


def apply_runtime_env() -> None:
    """Point HuggingFace caches at the configured drive before hub imports."""
    os.environ.setdefault("HF_HOME", str(settings.hf_home))
    os.environ.setdefault("HF_HUB_CACHE", str(settings.hf_hub_cache))
