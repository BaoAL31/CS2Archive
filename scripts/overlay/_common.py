"""Shared leaf symbols for the overlay subpackage.

Kept separate from ``overlay_pov`` so the per-concern modules
(``overlay_utilcams``, ``overlay_encode``) can import them without
creating a circular dependency on ``overlay_pov``.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from config import settings

# Overlay kernel + throws.parquet live in the sibling CS2UtilArchive checkout.
_CS2UTIL_ROOT = Path(settings.cs2util_root)
_CS2UTIL_SCRIPTS = _CS2UTIL_ROOT / "scripts"


def prefer_cs2util_scripts() -> None:
    """Put CS2UtilArchive on sys.path so ``scripts.render`` / ``scripts.demo_ids`` resolve.

    CS2Archive's own ``scripts/`` package (pov, overlay, faceit, …) must not
    stay cached as ``sys.modules['scripts']`` or those imports miss.
    """
    for _p in (str(_CS2UTIL_SCRIPTS), str(_CS2UTIL_ROOT)):
        if _p in sys.path:
            sys.path.remove(_p)
        sys.path.insert(0, _p)
    cached = sys.modules.get("scripts")
    if cached is not None and not hasattr(cached, "render") and not hasattr(cached, "demo_ids"):
        del sys.modules["scripts"]


TICKRATE = 64.0

# --- Util PiP geometry (shared with overlay_pov + render_util_cams) --------
# Kept here so pip render sizing and overlay layout stay in sync.
PIP_OUTLINE_THICKNESS = 0       # Pixels. White border removed; PiPs carry a drop shadow instead.
PIP_CORNER_RADIUS = 16          # Pixels. Rounded corner radius. 0 = square corners.
PIP_MARGIN = 12                 # Pixels. Content-to-content gap from video edge.
PIP_GAP = 12                    # Pixels. Content-to-content gap between stacked PiPs.
PIP_MAX_SIMULTANEOUS = 3
PIP_SHADOW_OFFSET = (6, 6)      # Pixels. Bottom-right drop-shadow offset (matches intro panes).
PIP_SHADOW_BLUR = 12            # Pixels. Shadow softness.
PIP_SHADOW_OPACITY = 0.28       # Shadow strength.
PIP_SLIDE_SECONDS = 0.3         # Ease-in-out slide duration at each end of a PiP window.

# Util-cam clip is "done" at 1 MB — same floor as CSDM sequence resume.
MIN_CLIP_BYTES = 1_000_000
SMOKE_CAMERAS = ("smoke", "fire", "molotov", "incendiary")


def cameras_for_util_type(util_type: str) -> str:
    """Canonical CS2Util camera set: combined flight+detonate for smokes."""
    return "flight,detonate" if str(util_type).lower() in SMOKE_CAMERAS else "flight"


def pip_cameras_for_util_type(util_type: str) -> str:
    """Camera tokens for the overlay PiP source clip — flight ONLY.

    The smoke ``flight,detonate`` deliverable has CS2UtilArchive's keyboard/
    mouse input overlay burned in (``finalize`` burns every lineup segment).
    The PiP is a small corner inset and must not carry keycaps, so it reads
    the clean ``flight_<throw>.mp4`` standalone the same render also writes.
    """
    return "flight"


# Below the byte floor but above this, ask ffprobe — short flash/HE flights
# are legitimately small (e.g. 119 frames / 993 KB) and must not be rejected.
MIN_PROBE_BYTES = 50_000
MIN_PROBE_FRAMES = 10


def _clip_has_frames(path: Path) -> bool:
    """True when ffprobe sees a decodable video stream with frames."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=nb_frames,avg_frame_rate,duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=15,
        )
        streams = json.loads(r.stdout).get("streams", [])
        if not streams:
            return False
        s = streams[0]
        try:
            n = int(s.get("nb_frames") or 0)
        except (TypeError, ValueError):
            n = 0
        if n >= MIN_PROBE_FRAMES:
            return True
        # nb_frames often missing for h264 — fall back to duration × fps.
        try:
            num, den = (s.get("avg_frame_rate", "0/1").split("/") + ["1"])[:2]
            fps = float(num) / float(den) if float(den) else 0.0
            dur = float(s.get("duration") or 0.0)
        except (TypeError, ValueError):
            return False
        return dur * fps >= MIN_PROBE_FRAMES
    except Exception:
        return False


def clip_is_done(path: Path, min_bytes: int = MIN_CLIP_BYTES) -> bool:
    if not path.is_file():
        return False
    size = path.stat().st_size
    if size >= min_bytes:
        return True
    if size >= MIN_PROBE_BYTES and _clip_has_frames(path):
        return True
    return False


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


def pip_shadow_pad() -> int:
    """Padding around the shadow sprite. Must match imgutil.drop_shadow's pad."""
    return PIP_SHADOW_BLUR + max(abs(PIP_SHADOW_OFFSET[0]), abs(PIP_SHADOW_OFFSET[1])) + 2


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
