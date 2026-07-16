"""Shared leaf symbols for the overlay subpackage.

Kept separate from ``overlay_pov`` so the per-concern modules
(``overlay_utilcams``, ``overlay_encode``) can import them without
creating a circular dependency on ``overlay_pov``.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

# -- Point at CS2UtilArchive for overlay pipeline + parquet data ----------
_CS2UTIL_ROOT = Path(r"D:\Projects\CS2UtilArchive")
_CS2UTIL_SCRIPTS = _CS2UTIL_ROOT / "scripts"

TICKRATE = 64.0


def _log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    try:
        print(f"[{ts}] {msg}", flush=True)
    except UnicodeEncodeError:
        safe = msg.encode("ascii", errors="replace").decode("ascii")
        print(f"[{ts}] {safe}", flush=True)


_CLIP_DUR_CACHE: dict[str, float] = {}


def _probe_clip_duration_seconds(clip_path: Path) -> float:
    """Return video duration in seconds (cached). Falls back to 0.0 on error."""
    key = str(clip_path)
    cached = _CLIP_DUR_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=duration",
            "-of", "json", str(clip_path),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, check=True)
        dur = float(json.loads(r.stdout)["streams"][0]["duration"])
    except Exception:
        dur = 0.0
    _CLIP_DUR_CACHE[key] = dur
    return dur
