"""
CS2Archive — Download Manager

Shared infrastructure for downloading, extracting, and tracking demo files.
Handles .rar, .zip, and .gz archives. Tracks download history to avoid duplicates.
"""

from __future__ import annotations

import gzip
import json
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
import rarfile
import zstandard
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

from config import settings
from models import DemoRecord, DemoSource, DownloadResult, DownloadStatus, MatchInfo


# ── Download History ──────────────────────────────────────────────────────────

HISTORY_FILE = Path("download_history.json")


def _load_history() -> list[dict]:
    """Load download history from JSON file."""
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
    return []


def _save_history(records: list[dict]) -> None:
    """Save download history to JSON file."""
    HISTORY_FILE.write_text(
        json.dumps(records, indent=2, default=str), encoding="utf-8"
    )


def is_already_downloaded(match_id: str, source: DemoSource) -> Optional[Path]:
    """Check if a demo has already been downloaded. Returns the path if found."""
    for record in _load_history():
        if record.get("match_id") == match_id and record.get("source") == source.value:
            demo_path = Path(record["demo_path"])
            if demo_path.exists():
                return demo_path
    return None


def record_download(result: DownloadResult) -> None:
    """Record a successful download to history."""
    if not result.is_success or not result.demo_path:
        return

    records = _load_history()
    record = DemoRecord(
        match_id=result.match.match_id,
        source=result.match.source,
        match_display=result.match.display_name,
        demo_path=str(result.demo_path),
        file_size_mb=result.file_size_mb,
        downloaded_at=result.completed_at or datetime.now(),
    )
    records.append(record.model_dump(mode="json"))
    _save_history(records)


def get_download_history() -> list[DemoRecord]:
    """Get all download history records."""
    records = _load_history()
    return [DemoRecord(**r) for r in records]


# ── File Download ─────────────────────────────────────────────────────────────


def _make_download_progress() -> Progress:
    """Create a Rich progress bar for downloads."""
    return Progress(
        TextColumn("[bold blue]{task.description}"),
        BarColumn(bar_width=40),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
    )


async def download_file(
    url: str,
    dest: Path,
    description: str = "Downloading",
    headers: Optional[dict] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> Path:
    """
    Download a file from a URL with a progress bar.
    Returns the path to the downloaded file.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    own_client = client is None

    if own_client:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.download_timeout),
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        )

    try:
        async with client.stream("GET", url, headers=headers) as response:
            response.raise_for_status()
            total = int(response.headers.get("content-length", 0))

            with _make_download_progress() as progress:
                task_id = progress.add_task(description, total=total or None)

                with open(dest, "wb") as f:
                    async for chunk in response.aiter_bytes(settings.chunk_size):
                        f.write(chunk)
                        progress.update(task_id, advance=len(chunk))

        return dest

    finally:
        if own_client:
            await client.aclose()


# ── Archive Extraction ────────────────────────────────────────────────────────


def extract_demo(archive_path: Path, dest_dir: Path) -> Path:
    """
    Extract a .dem file from an archive (.rar, .zip, or .gz).
    Returns the path to the extracted .dem file.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    suffix = archive_path.suffix.lower()

    if suffix == ".gz":
        return _extract_gz(archive_path, dest_dir)
    elif suffix == ".zst":
        return _extract_zst(archive_path, dest_dir)
    elif suffix == ".zip":
        return _extract_zip(archive_path, dest_dir)
    elif suffix == ".rar":
        return _extract_rar(archive_path, dest_dir)
    elif suffix == ".dem":
        # Already a .dem file, just move it
        final_path = dest_dir / archive_path.name
        shutil.move(str(archive_path), str(final_path))
        return final_path
    else:
        raise ValueError(f"Unsupported archive format: {suffix}")


def _extract_gz(archive_path: Path, dest_dir: Path) -> Path:
    """Extract a .dem.gz file."""
    # Strip .gz to get the output filename
    stem = archive_path.stem  # e.g., "match.dem"
    if not stem.endswith(".dem"):
        stem += ".dem"
    output_path = dest_dir / stem

    with gzip.open(archive_path, "rb") as gz_in:
        with open(output_path, "wb") as f_out:
            shutil.copyfileobj(gz_in, f_out)

    return output_path


def _extract_zst(archive_path: Path, dest_dir: Path) -> Path:
    """Extract a .dem.zst file."""
    stem = archive_path.stem
    if stem.endswith(".dem"):
        stem = stem[:-4]
    output_path = dest_dir / f"{stem}.dem"

    dctx = zstandard.ZstdDecompressor()
    with open(archive_path, "rb") as zst_in:
        with open(output_path, "wb") as f_out:
            dctx.copy_stream(zst_in, f_out)

    return output_path


def _extract_zip(archive_path: Path, dest_dir: Path) -> Path:
    """Extract the first .dem file from a .zip archive."""
    with zipfile.ZipFile(archive_path, "r") as zf:
        dem_files = [f for f in zf.namelist() if f.lower().endswith(".dem")]
        if not dem_files:
            raise FileNotFoundError(f"No .dem file found in {archive_path.name}")

        # Extract the first (usually only) .dem file
        zf.extract(dem_files[0], dest_dir)
        return dest_dir / dem_files[0]


def _extract_rar(archive_path: Path, dest_dir: Path) -> Path:
    """Extract the first .dem file from a .rar archive."""
    with rarfile.RarFile(archive_path, "r") as rf:
        dem_files = [f for f in rf.namelist() if f.lower().endswith(".dem")]
        if not dem_files:
            raise FileNotFoundError(f"No .dem file found in {archive_path.name}")

        rf.extract(dem_files[0], dest_dir)
        return dest_dir / dem_files[0]


# ── Utilities ─────────────────────────────────────────────────────────────────


def _sanitize(name: str) -> str:
    """Remove characters that are invalid in folder/file names."""
    invalid = r'<>:"/\|?*'
    for c in invalid:
        name = name.replace(c, "")
    name = name.strip().rstrip(". ")
    return name[:120]


def build_demo_path(match: MatchInfo) -> Path:
    """Build organized demo path: demos/<source>/<event>/<teams> - <map>.dem"""
    event_dir = _sanitize(match.event) if match.event and match.event != "Unknown" else "unknown"

    parts = [match.team1, match.team2]
    if match.map_name and match.map_name != "Unknown":
        parts.append(match.map_name)
    filename = " vs ".join(parts) + ".dem"
    filename = _sanitize(filename)

    base = settings.demo_storage_dir / match.source.value
    return base / event_dir / filename


def file_size_mb(path: Path) -> float:
    """Get file size in megabytes."""
    if path.exists():
        return path.stat().st_size / (1024 * 1024)
    return 0.0


def cleanup_temp(path: Path) -> None:
    """Remove a temporary file if it exists."""
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass
