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

# --- Util PiP geometry (shared with overlay_pov + render_util_cams) --------
# Kept here so pip render sizing and overlay layout stay in sync.
PIP_OUTLINE_THICKNESS = 2       # Pixels. White border around each PiP (0 disables outline).
PIP_CORNER_RADIUS = 16          # Pixels. Rounded corner radius. 0 = square corners.
PIP_MARGIN = 12                 # Pixels. Outline-to-outline gap from video edge.
PIP_GAP = 12                    # Pixels. Outline-to-outline gap between stacked PiPs.
PIP_MAX_SIMULTANEOUS = 3


def _pip_body(video_height: int, max_simultaneous: int | None = None) -> int:
    """Square PiP slot size: prefer height*2/5, shrink so max stack fits."""
    n = max(1, max_simultaneous if max_simultaneous is not None else PIP_MAX_SIMULTANEOUS)
    preferred = video_height * 2 // 5
    available = video_height - 2 * PIP_MARGIN
    max_fit = (available - (n - 1) * PIP_GAP) // n
    return min(preferred, max(1, max_fit))


def _pip_inner(video_height: int, max_simultaneous: int | None = None) -> int:
    """Content area inside the outline."""
    return _pip_body(video_height, max_simultaneous) - 2 * PIP_OUTLINE_THICKNESS


def pip_render_dimensions(
    video_height: int = 1440,
    max_simultaneous: int | None = None,
    supersample: float = 1.0,
) -> tuple[int, int]:
    """16:9 render size for util PiPs, derived from displayed PiP size.

    CS2/HLAE can't render square directly, so we render 16:9 then center-crop
    to square (height x height) in the overlay step. The required render
    height is therefore the displayed inner size (body - 2*outline), optionally
    supersampled for Lanczos downscale crispness.

    Args:
        video_height: final POV video height (1440 for 2560x1440).
        max_simultaneous: max stacked PiPs; defaults to PIP_MAX_SIMULTANEOUS.
        supersample: multiplier on inner size (1.0 = 1:1, 1.2 = 20% supersample).

    Returns:
        (width, height) both even, suitable for yuv420p.
    """
    n = max_simultaneous if max_simultaneous is not None else PIP_MAX_SIMULTANEOUS
    inner = _pip_inner(video_height, n)
    h = int(round(inner * supersample))
    # Clamp to sensible bounds and make even for yuv420p.
    h = max(64, min(h, video_height))
    if h % 2 == 1:
        h += 1
    w = int(round(h * 16 / 9))
    if w % 2 == 1:
        w += 1
    return w, h


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
