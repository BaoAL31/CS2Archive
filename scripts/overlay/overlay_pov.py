#!/usr/bin/env python3
"""
Overlay keyboard input + utility throw flight clips onto POV video.

Keyboard overlay:
  - 18 sprite PNGs (9 keys x idle/pressed), 76x76 per cap, rounded rects
  - Release fade (12-frame stepped fade) so 1-frame taps visible
  - Self-extracts button states via demoparser2 with full DEMOPARSER_TICK_FIELDS
    (per-column booleans + buttons bitmask) using overlay_states_from_tick_row
  - Per-round tick-to-frame mapping using round_offsets sidecar from concat step

Utility throw PiP:
  - Reads throws.parquet from CS2UtilArchive results (full-match throw metadata)
  - For each player throw with valid flight, renders CSDM flight clip
    (in-game camera chasing the nade, via build_flight_command)
  - Stacks overlapping clips vertically at bottom-left corner

Flow: demoparser2 full extraction -> sprite PNGs -> render throw flight clips ->
      composite via ffmpeg filter_complex (keyboard + throw PiP).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from bisect import bisect_right
from pathlib import Path
from dataclasses import dataclass
from typing import Any

# scripts/ on path so `overlay.*` package imports resolve when run as a file
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _pathsetup import ensure
ensure()

# -- Point at CS2UtilArchive for overlay pipeline + parquet data ----------
# Shared constants/helpers live in the overlay subpackage's _common module
# (also imported by overlay_utilcams / overlay_encode) to avoid cycles.
from overlay._common import (
    _CS2UTIL_ROOT,
    _CS2UTIL_SCRIPTS,
    TICKRATE,
    _log,
    _probe_clip_duration_seconds,
)
for _p in (str(_CS2UTIL_SCRIPTS), str(_CS2UTIL_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from scripts.render.overlay_assets import (
    generate_key_assets,
    overlay_png_input_paths,
    build_png_overlay_filter,
)
from scripts.render.overlay_layout import _OVERLAY_SIGNALS
from scripts.input_overlay_decode import (
    decode_button_mask,
    overlay_tick_from_row,
    advance_inferred_jump_burst,
    JumpBurstState,
    DEMOPARSER_TICK_FIELDS,
)
from scripts.render.paths import flight_clip_name, clip_name_for_cameras, util_render_slug

# Per-concern modules (kept out of this file to limit its size).
from overlay.overlay_utilcams import (
    PipClip,
    _find_demo_data_dir,
    _cs2util_results_dir,
    _ensure_cs2util_data,
    _load_player_throws,
    _build_round_frame_ranges,
    _rm_empty_dir,
    _util_slug_for_throw,
    _run_batch_util_cams_subprocess,
    _scan_utility_cams_clips,
    _render_throw_flight_clips,
)
from overlay.overlay_encode import (
    _overlay_output_valid,
    _ffmpeg_encode,
    _ffmpeg_segment_copy,
    _concat_overlay_batches,
    _compute_batch_boundaries,
)

# -- Constants -----------------------------------------------------------

# --- Util PiP burn-in geometry -----------------------------------------
# Preferred body = video_height * 2 // 5; shrinks if PIP_MAX_SIMULTANEOUS
# stacked slots (plus gaps/margins) would not fit the frame height.
PIP_OUTLINE_THICKNESS = 2       # Pixels. White border around each PiP (0 disables outline).
PIP_CORNER_RADIUS = 16          # Pixels. Rounded corner radius. 0 = square corners.
PIP_MARGIN = 12                 # Pixels. Outline-to-outline gap from video edge.
PIP_GAP = 12                    # Pixels. Outline-to-outline gap between stacked PiPs.
PIP_MAX_SIMULTANEOUS = 3

FLIGHT_DIR_NAME = "throw_flights"

OVERLAY_BATCH_PREFIX = "batch-overlay-"




def _pip_body(video_height: int) -> int:
    """Square PiP slot size: prefer height*2/5, shrink so max stack fits."""
    preferred = video_height * 2 // 5
    available = video_height - 2 * PIP_MARGIN
    n = max(1, PIP_MAX_SIMULTANEOUS)
    max_fit = (available - (n - 1) * PIP_GAP) // n
    return min(preferred, max(1, max_fit))


def _make_rounded_corner_mask(path: Path, size: int, radius: int) -> None:
    """Write a grayscale rounded-rect mask (white 255 interior, black 0 corners).

    Used with ``alphamerge`` to punch the four outer corners out of a PiP
    body (including its white outline) so the main video shows through.
    ffmpeg's ``alphamerge`` maps the SECOND input's luma to the alpha
    channel, so the mask must be a black-and-white image (0 = transparent,
    255 = opaque) — an RGBA mask would contribute its white luma everywhere.
    This is ~O(1) per pixel (channel select), unlike a per-pixel ``geq``
    which measured +7.96 ms/frame/pip (see scripts/_bench_geq.py) and blows
    the 60 fps budget with two simultaneous PiPs.
    """
    from PIL import Image, ImageDraw

    radius = max(1, min(radius, size // 2))
    img = Image.new("L", (size, size), 255)
    d = ImageDraw.Draw(img)
    r = radius
    # Erase the four corner squares (the slivers the arcs clip off), then
    # restore the quarter-circles so the rounded arc itself stays opaque.
    d.rectangle((0, 0, r, r), fill=0)
    d.rectangle((size - r, 0, size, r), fill=0)
    d.rectangle((0, size - r, r, size), fill=0)
    d.rectangle((size - r, size - r, size, size), fill=0)
    for cx, cy in ((r, r), (size - r, r), (r, size - r), (size - r, size - r)):
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=255)
    img.save(path)


# Reference size for 1440p (test helpers); render path uses _pip_body(height).
PIP_BODY = _pip_body(1440)


def _pip_geometry(pip_index: int, video_width: int, video_height: int) -> dict[str, int]:
    """Compute PiP slot, content, and position for given stack row.

    Returns dict with:
      body   - total slot size on screen (square; see _pip_body)
      inner  - content area inside the outline (body - 2*outline)
      x, y   - top-left of the slot in main video coords
      outline- outline thickness in pixels
    """
    ol = PIP_OUTLINE_THICKNESS
    body = _pip_body(video_height)
    inner = body - 2 * ol
    x = PIP_MARGIN
    y = video_height - PIP_MARGIN - body - pip_index * (body + PIP_GAP)
    return {"body": body, "inner": inner, "x": x, "y": y, "outline": ol}

# Column subset we actually need (avoid fetching huge unnecessary fields)
# CS2 (Source 2) usercmd: button state lives in usercmd_buttonstate_1 (IN_* bitmask),
# mouse in usercmd_mouse_dx/dy. The legacy CS:GO `buttons`/`FORWARD`/`FIRE` fields are
# dropped on CS2 demos, so we read the usercmd fields and rename buttonstate_1 -> buttons
# in _extract_keyboard_states so the bitmask decoder can consume it.
REQUIRED_TICK_FIELDS = (
    "tick", "steamid",
    "usercmd_buttonstate_1", "usercmd_buttonstate_2",
    "usercmd_mouse_dx", "usercmd_mouse_dy",
    "ducked", "ducking",
    "is_airborne", "velocity_Z",
)


# Cache: flight clip path -> duration in seconds. Probing once per clip is
# cheap (~50ms) and skips repeated ffprobe calls for shared clips.
_CLIP_DUR_CACHE: dict[str, float] = {}



def _probe_video_info(video_path: Path) -> tuple[int, int, float, int]:
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate,nb_frames",
        "-of", "json",
        str(video_path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, check=True)
    s = json.loads(r.stdout)["streams"][0]
    w, h = int(s["width"]), int(s["height"])
    num, den = s["r_frame_rate"].split("/")
    fps = float(num) / float(den)
    fc = int(s.get("nb_frames", 0))
    return w, h, fps, fc




# -- CS2UtilArchive data dir lookup --------------------------------------
def _load_round_tick_ranges(demo_path: Path) -> dict[int, tuple[int, int]]:
    """Load round (start_tick, end_tick) pairs.

    Prefers the demo's round_start events (authoritative, aligned to the
    actual recorded video). Falls back to rounds.parquet from CS2UtilArchive.
    """
    events_ranges = _load_round_tick_ranges_events(demo_path)
    if events_ranges:
        return events_ranges

    # Fallback: rounds.parquet
    data_dir = _find_demo_data_dir(demo_path)
    if data_dir is not None:
        rp = data_dir / "rounds.parquet"
        if rp.is_file():
            import pandas as pd
            df = pd.read_parquet(rp)
            result: dict[int, tuple[int, int]] = {}
            prev_end = 0
            for _, row in df.sort_values("round_num").iterrows():
                rn = int(row["round_num"])
                start = prev_end + 1
                end = int(row["end_tick"])
                result[rn] = (start, end)
                prev_end = end
            if result:
                _log(f"  [rounds] {len(result)} rounds from rounds.parquet")
                return result
    _log("  [rounds] No round tick ranges available")
    return {}


def _load_round_tick_ranges_events(demo_path: Path) -> dict[int, tuple[int, int]]:
    """Authoritative round boundaries from the demo's round_start events.

    Round N spans [round_start[N], round_start[N+1]) — the POV video (csdm
    ``--event rounds``) records continuously from one round's start to the
    next, in real time. The last round's end is estimated from the latest
    throw tick (or a generous 200s buffer) so throws late in the match still
    map inside their round.
    """
    from demoparser2 import DemoParser
    p = DemoParser(str(demo_path))
    events = p.parse_event("round_start")
    if events.empty:
        _log("  [rounds] No round_start events in demo")
        return {}
    # Drop tick-0 warmup round_start. CS2 demos often emit a phantom
    # round_start at tick 0 before the real pistol round; counting it as
    # round 1 shifts every subsequent play range by +1 so keyboard/util
    # overlays map to the wrong round (and throw round_num lookups miss).
    ticks = sorted(int(t) for t in events["tick"] if int(t) > 0)
    if not ticks:
        _log("  [rounds] Only warmup round_start (tick 0) in demo")
        return {}
    demo_end = None
    data_dir = _find_demo_data_dir(demo_path)
    if data_dir is not None:
        tp = data_dir / "throws.parquet"
        if tp.is_file():
            import pandas as pd
            demo_end = int(pd.read_parquet(tp)["throw_tick"].max()) + 2000
    if demo_end is None:
        demo_end = ticks[-1] + int(200 * TICKRATE)
    result = {}
    for i, t in enumerate(ticks):
        rn = i + 1
        end = ticks[i + 1] - 1 if i + 1 < len(ticks) else demo_end
        result[rn] = (t, end)
    _log(f"  [rounds] {len(result)} rounds from round_start events")
    return result


# CSDM (csdm video render) does NOT record the full round. It records roughly:
#   start = round_freeze_end - CSDM_TICK_MARGIN  (skip buy/freeze period)
#   end   = (player death tick + CSDM_TICK_MARGIN)  OR  (round end tick)
# This matches the actual rendered video duration. Overlay frame->tick
# mapping MUST use these trimmed ranges, not the full round range.
CSDM_TICK_MARGIN = 128  # ~2s @ 64 tick


def _load_pov_play_tick_ranges(
    demo_path: Path, steam_id: str
) -> dict[int, tuple[int, int]]:
    """Per-round (play_start_tick, play_end_tick) matching what CSDM actually
    recorded: freeze_end - margin  →  player_death + margin (or round_end).

    Requires demo events resolve cleanly; falls back to full round ranges
    (with a warning) if any needed event is missing.
    """
    from demoparser2 import DemoParser

    # 1) Full round (start, end) ranges (reuse existing helper for the round_end
    #    boundaries), then rebuild a per-round start using freeze events.
    full = _load_round_tick_ranges(demo_path)
    if not full:
        return {}

    # 2) round_freeze_end + round_end + player_death events.
    #    round_end is needed for survived rounds (no death): the play range
    #    ends at round_end + margin, NOT round_start_next (which spans gaps
    #    like half-time breaks and inflates the duration).
    p = DemoParser(str(demo_path))
    freeze_ticks: list[int] = []
    round_end_ticks: list[int] = []
    death_ticks: list[int] = []
    ev = p.parse_events(["round_freeze_end", "round_end", "player_death"])
    for name, df in ev:
        if name == "round_freeze_end" and not df.empty:
            freeze_ticks = sorted(int(t) for t in df["tick"])
        elif name == "round_end" and not df.empty:
            round_end_ticks = sorted(int(t) for t in df["tick"])
        elif name == "player_death" and not df.empty:
            col = "user_steamid" if "user_steamid" in df.columns else None
            if col is not None:
                sid_str = str(steam_id)
                s = df[col].astype(str)
                # Steam IDs in demo events can be int64, str, or NaN — compare
                # purely on string form (NaN -> "nan") to be safe.
                df = df[s == sid_str]
            death_ticks = sorted(int(t) for t in df["tick"])

    # 2b) Assign round_end ticks to rounds (1:1 with round_start events).
    round_end_by_round: dict[int, int] = {}
    ri = 0
    for rn in sorted(full):
        while ri < len(round_end_ticks) and round_end_ticks[ri] < full[rn][0]:
            ri += 1
        if ri < len(round_end_ticks):
            round_end_by_round[rn] = round_end_ticks[ri]
            ri += 1

    # 3) Assign each freeze tick to the round whose [start,end] contains it.
    freeze_by_round: dict[int, int] = {}
    fi = 0
    for rn in sorted(full):
        rs, re = full[rn]
        # advance freeze pointer past this round boundary
        while fi < len(freeze_ticks) and freeze_ticks[fi] < rs:
            fi += 1
        if fi < len(freeze_ticks) and rs <= freeze_ticks[fi] <= re:
            freeze_by_round[rn] = freeze_ticks[fi]
            fi += 1

    # 4) Deaths -> per round (first death inside the round).
    death_by_round: dict[int, int] = {}
    di = 0
    for rn in sorted(full):
        rs, re = full[rn]
        while di < len(death_ticks) and death_ticks[di] < rs:
            di += 1
        if di < len(death_ticks) and rs <= death_ticks[di] <= re:
            death_by_round[rn] = death_ticks[di]
            # leave the death pointer; multiple deaths per round shouldn't
            # happen for the SAME steamid, advance for next round scan.
            di += 1

    # 5) Build play ranges.
    play: dict[int, tuple[int, int]] = {}
    missing_freeze = 0
    for rn in sorted(full):
        rs, re = full[rn]
        fz = freeze_by_round.get(rn)
        if fz is not None:
            start = max(rs, fz - CSDM_TICK_MARGIN)
        else:
            start = rs  # last round / OT — fall back to round start
            missing_freeze += 1
        re_actual = round_end_by_round.get(rn, re)  # fall back to round_start_next
        if rn in death_by_round:
            d = death_by_round[rn]
            end = min(re_actual + CSDM_TICK_MARGIN, d + CSDM_TICK_MARGIN)
        else:
            # Survived the round: CSDM adds a post-round buffer of CSDM_TICK_MARGIN
            # ticks (matches sequence filename tick span).
            end = re_actual + CSDM_TICK_MARGIN
        play[rn] = (start, end)

    _log(
        f"  [pov-play] {len(play)} play ranges "
        f"(freeze-skipped, {missing_freeze} without freeze_end, "
        f"{len(death_by_round)} rounds where POV died)"
    )
    return play


# -- Keyboard: full-match self-extraction --------------------------------


def _extract_keyboard_states(
    demo_path: Path,
    steam_id: str,
    frame_count: int,
    fps: float,
    round_offsets: dict[int, float] | None = None,
    round_tick_ranges: dict[int, tuple[int, int]] | None = None,
    round_video_duration: dict[int, float] | None = None,
) -> dict[str, list[int]]:
    """Extract full keyboard states using demoparser2 with per-column booleans.

    Uses overlay_states_from_tick_row from CS2UtilArchive for reliable
    per-signal state detection (per-column booleans + bitmask).

    When round_offsets and round_tick_ranges are provided, maps each video
    frame to the correct round and demo tick using the sidecar data from
    concat_rounds.py. This handles concatenated multi-round videos correctly.

    Returns per_signal frame lists (0/1 per frame).
    """
    from demoparser2 import DemoParser
    from usercmd_extract import _run_cli, _parse_rows, signals_from_rows, _validate_from_rows

    t0 = time.time()

    # Corrected per-tick usercmd input (delta_data decoded correctly via the
    # vendored Rust parser — demoparser2 0.41.x misaligns usercmd_buttonstate).
    _rows = _parse_rows(_run_cli(str(demo_path), steam_id))
    corrected = signals_from_rows(_rows)
    _log(f"  [usercmd] corrected signals for {len(corrected)} ticks")
    # Sanity check: extracted A/D keys vs the player's actual velocity.
    _chk = _validate_from_rows(_rows)
    _am = _chk.get("a_mismatch_rate"); _dm = _chk.get("d_mismatch_rate")
    _log(f"  [usercmd] key-vs-velocity check: A mismatch={_am:.0%} D mismatch={_dm:.0%}"
         if _am is not None else f"  [usercmd] key-vs-velocity: {_chk}")
    if (_am is not None and _am > 0.5) or (_dm is not None and _dm > 0.5):
        _log(f"  [WARN] input key-vs-velocity mismatch high — check A/D mapping: {_chk}")

    # demoparser2 only for jump inference (is_airborne / velocity_Z); the
    # movement/duck/walk/attack signals come from the corrected usercmd above.
    parser = DemoParser(str(demo_path))
    jump_df = parser.parse_ticks(
        ["is_airborne", "velocity_Z"],
        players=[int(steam_id)],
    )
    if jump_df.empty:
        _log("[ERROR] No tick data from demo")
        sys.exit(1)
    jump_df = jump_df.sort_values(["tick"])

    # Build per-tick state lookup from corrected input, plus jump inference.
    # apply_jump_inference=False avoids mid-air bhop spam.
    # Inferred jumps: leave-ground crouch burst, or standing leave-ground
    # confirmed by upward vz a tick later (CS2 often omits IN_JUMP).
    zero = {"w": 0, "a": 0, "s": 0, "d": 0, "jump": 0, "duck": 0,
            "lmb": 0, "rmb": 0, "walk": 0}
    tick_states: dict[int, dict[str, int]] = {}
    prev_row = None
    jump_burst = JumpBurstState()
    for _, row in jump_df.iterrows():
        tick = int(row["tick"])
        states = dict(zero)
        states.update(corrected.get(tick, {}))
        jump, _ = advance_inferred_jump_burst(
            row,
            prev_row,
            duck_on=states["duck"],
            bitmask_jump=0,  # CS2 doesn't record IN_JUMP in buttonstate; infer only
            state=jump_burst,
        )
        states["jump"] = jump
        tick_states[tick] = states
        prev_row = row

    all_ticks = sorted(tick_states.keys())
    _log(f"  [demoparser2] {len(tick_states)} ticks [{all_ticks[0]}..{all_ticks[-1]}] in {time.time()-t0:.1f}s")

    zero = {s: 0 for s in _OVERLAY_SIGNALS}
    per_sig: dict[str, list[int]] = {s: [] for s in _OVERLAY_SIGNALS}

    if round_offsets and round_tick_ranges:
        # Per-round frame-to-tick mapping using sidecar
        # CRITICAL: Video compresses round time (e.g. 161s game -> 50.5s video).
        # Must use video round duration, not game-time duration.
        _log(f"  [mapping] Per-round frame mapping ({len(round_offsets)} rounds)")
        sorted_rounds = sorted(round_offsets.keys())
        # Build per-round batch duration lookup
        round_end_sec: dict[int, float] = {}
        for i, rn in enumerate(sorted_rounds):
            if i + 1 < len(sorted_rounds):
                round_end_sec[rn] = round_offsets[sorted_rounds[i + 1]]
            else:
                if len(sorted_rounds) > 1:
                    prev = sorted_rounds[-2]
                    prev_dur = round_offsets[sorted_rounds[-1]] - round_offsets[prev]
                    round_end_sec[rn] = round_offsets[rn] + prev_dur
                else:
                    round_end_sec[rn] = frame_count / fps

        for f_idx in range(frame_count):
            sec = f_idx / fps
            # Find round containing this frame second
            pos = bisect_right([round_offsets[r] for r in sorted_rounds], sec)
            if pos == 0:
                rn = sorted_rounds[0]
            elif pos >= len(sorted_rounds):
                rn = sorted_rounds[-1]
            else:
                idx = pos - 1
                rn = sorted_rounds[idx]
                if sec >= round_end_sec.get(rn, sec + 1):
                    rn = sorted_rounds[min(idx + 1, len(sorted_rounds) - 1)]

            round_start_sec = round_offsets.get(rn, 0.0)
            offset_sec = max(0.0, sec - round_start_sec)
            rs, re = round_tick_ranges.get(rn, (all_ticks[0], all_ticks[-1]))
            tick_range = re - rs
            # Use video round duration (NOT game-time duration) for tick scaling
            vid_dur = (round_video_duration or {}).get(rn)
            if vid_dur and vid_dur > 0 and tick_range > 0:
                tick = rs + int((offset_sec / vid_dur) * tick_range)
            else:
                # Fallback: offset_sec * TICKRATE (wrong for compressed video, but used only as last resort)
                tick = rs + int(offset_sec * TICKRATE)
            tick = max(rs, min(re, tick))
            states = tick_states.get(tick, zero)
            for sig in _OVERLAY_SIGNALS:
                per_sig[sig].append(int(states.get(sig, 0)))
    else:
        # Fallback: linear tick mapping from base_tick
        base_tick = all_ticks[0] if all_ticks else 0
        _log(f"  [mapping] Linear tick mapping from tick {base_tick}")
        for f_idx in range(frame_count):
            t = base_tick + int(f_idx * TICKRATE / fps)
            states = tick_states.get(t, zero)
            for sig in _OVERLAY_SIGNALS:
                per_sig[sig].append(int(states.get(sig, 0)))

    total_pressed = sum(sum(v) for v in per_sig.values())
    _log(f"  [keyboard] {total_pressed} non-idle across {len(per_sig)} signals")
    return per_sig


# -- Utility throw: CSDM flight renders ----------------------------------


def _build_pip_chain(
    flight_clips: list[PipClip],
    width: int,
    height: int,
    fps: float,
    start_label: str = "[0:v]",
    pip_input_offset: int = 1,
    mask_input_idx: int | None = None,
) -> tuple[list[str], str, list[PipClip]]:
    """Sort clips, assign stack rows, build PiP filter parts.

    ``start_label`` is the filter graph label the PiP chain starts from
    (default ``[0:v]`` = raw input video; pass the keyboard output label
    when batching to chain keyboard + PiP in a single ffmpeg pass).

    ``pip_input_offset`` is the ffmpeg input index for the FIRST flight
    clip. When sprite PNGs occupy inputs 1-18, pass ``1 + len(png_inputs)``
    so clips are referenced as ``[19:v]``, ``[20:v]``, etc.

    ``mask_input_idx`` is the ffmpeg input index of the rounded-corner mask
    PNG (looped), used by ``alphamerge`` when PIP_CORNER_RADIUS > 0. Pass
    ``None`` to keep square corners.
    """
    sorted_clips = sorted(flight_clips, key=lambda c: c.start_frame)
    active: list[PipClip] = []
    for clip in sorted_clips:
        active = [a for a in active if a.end_frame > clip.start_frame]
        # Round-robin: 1st→0, 2nd→1, … then wrap (4th covers spot 0 when max=3)
        clip.pip_index = len(active) % PIP_MAX_SIMULTANEOUS
        active.append(clip)
        _log(f"  PiP: {clip.util_type} @ frames {clip.start_frame}-{clip.end_frame}, row {clip.pip_index}")

    pip_parts: list[str] = []
    pip_current = start_label
    for idx, clip in enumerate(sorted_clips, start=pip_input_offset):
        fc_part, tag = _build_pip_overlay(
            clip, pip_current, idx, width, height, fps,
            mask_input_idx=mask_input_idx,
        )
        pip_parts.append(fc_part)
        pip_current = f"[{tag}]"
    return pip_parts, pip_current, sorted_clips


def _build_pip_overlay(
    clip: PipClip,
    current_label: str,
    input_idx: int,
    width: int,
    height: int,
    fps: float,
    mask_input_idx: int | None = None,
) -> tuple[str, str]:
    """Build filter string + tag for one PiP overlay at bottom-left.

    Scales flight clip (input_idx:v) to PIP size and delays its PTS so the
    clip's first frame aligns with clip.start_frame on the main timeline.
    We MUST NOT use ``enable='between(n,...)'`` here: the overlay filter is a
    sync filter that consumes the secondary stream frame-by-frame regardless
    of ``enable``. With ``enable=false`` for frames 0..start_frame-1, ffmpeg
    drains the entire short clip before the window opens, so at start_frame
    the secondary is already EOF and overlay shows a single frozen frame.

    Delaying PTS via ``setpts=PTS-STARTPTS+start/TB`` makes the clip's frames
    arrive exactly during the window. ``eof_action=pass`` lets the main video
    show through once the clip finishes (no frozen last-frame held over the
    rest of the video).
    """
    geom = _pip_geometry(clip.pip_index, width, height)
    pip_body = geom["body"]
    pip_inner = geom["inner"]
    x = geom["x"]
    pip_y = geom["y"]
    ol = geom["outline"]
    tag = f"pip{clip.pip_index}_{input_idx}"
    scaled_tag = f"pip_scaled_{input_idx}"
    start_seconds = clip.start_frame / fps
    # Cap clip playback to its PiP window (seconds on the clip's native
    # timeline). Without this a late-round smoke (whose window we clamped to
    # the round end) would otherwise keep playing for the full clip duration
    # and bleed past the round boundary into later rounds within the same
    # overlay batch.
    play_seconds = max(0.0, (clip.end_frame - clip.start_frame) / fps)

    # Build pre-overlay filter chain for the flight clip:
    #   0. trim to the PiP window length (native clip seconds)
    #   1. scale to inner content size (body - 2*outline)
    #   2. pad back up to body with white border = the outline
    #   3. format=rgba + optional geq for rounded corners
    #   4. setpts to align first frame with clip.start_frame on main timeline
    # Aspect-preserving scale (force increase) + center-crop to square.
    # Avoids horizontal squeeze on wide flight clips (1920x1080 -> 568x568).
    pre_filters = []
    if play_seconds > 0:
        pre_filters.append(f"trim=end={play_seconds:.6f}")
        # trim keeps PTS; reset so setpts math is deterministic.
        pre_filters.append("setpts=PTS-STARTPTS")
    pre_filters += [
        f"scale=w={pip_inner}:h={pip_inner}:force_original_aspect_ratio=increase:flags=lanczos",
        f"crop={pip_inner}:{pip_inner}",
    ]
    if ol > 0:
        pre_filters.append(
            f"pad=w={pip_body}:h={pip_body}:x={ol}:y={ol}:color=white"
        )
    # Rounded corners: punch out the four outer corners with a static RGBA
    # mask via alphamerge. This is ~O(1) per pixel (channel select), unlike a
    # per-pixel `geq` which measured +7.96 ms/frame/pip (see scripts/_bench_geq.py)
    # and blows the 60 fps budget with two simultaneous PiPs.
    rounded_tag = scaled_tag
    if PIP_CORNER_RADIUS > 0 and mask_input_idx is not None:
        pre_filters.append("format=rgba")
        rounded_tag = f"pip_rounded_{input_idx}"
    pre_filters.append(f"setpts=PTS-STARTPTS+{start_seconds:.6f}/TB")
    pre_chain = f"[{input_idx}:v]" + ",".join(pre_filters) + f"[{scaled_tag}]"

    parts = [
        pre_chain,
    ]
    if rounded_tag != scaled_tag:
        parts.append(
            f"[{scaled_tag}][{mask_input_idx}:v]alphamerge[{rounded_tag}]"
        )
    parts.append(
        f"{current_label}[{rounded_tag}]"
        f"overlay=x={x}:y={pip_y}:eof_action=pass"
        f"[{tag}]",
    )
    return ";".join(parts), tag


def run_overlay(
    video_path: Path,
    demo_path: Path,
    steam_id: str,
    round_num: int | None = None,
    batches: int = 5,
    util_cams_root: Path | None = None,
    work_dir: Path | None = None,
) -> None:
    """Apply keyboard overlay + utility throw flight PiP onto video_path (in place)."""
    if not video_path.exists():
        _log(f"[ERROR] Video not found: {video_path}")
        sys.exit(1)
    if not demo_path.exists():
        _log(f"[ERROR] Demo not found: {demo_path}")
        sys.exit(1)

    t_overall = time.time()
    width, height, fps, frame_count = _probe_video_info(video_path)
    fn = os.fspath(video_path.name)
    _log(f"Video: {fn}: {width}x{height} @ {fps:.2f}fps, {frame_count} frames")

    # Load round offset sidecar (from concat_rounds.py)
    round_offsets: dict[int, float] = {}
    round_video_duration: dict[int, float] = {}
    video_total_seconds = 0.0
    offset_path = video_path.with_suffix(".mp4").with_name(f"{video_path.stem}.round_offsets.json")
    if offset_path.is_file():
        try:
            with open(offset_path) as f:
                off_data = json.load(f)
            video_secs = frame_count / fps if fps > 0 else 0.0
            from concat_rounds import validate_round_offsets_sidecar
            sidecar_errs = validate_round_offsets_sidecar(
                off_data, video_duration_seconds=video_secs,
            )
            if sidecar_errs:
                for err in sidecar_errs:
                    _log(f"[WARN] sidecar: {err}")
                # Only hard-fail on truly corrupt data (negative durations,
                # non-monotonic timestamps). Gaps/skip-first-round from
                # --skip-failed-rounds are intentional.
                critical = [e for e in sidecar_errs if "negative" in e.lower()
                            or "not monotonic" in e.lower()
                            or "duration" in e.lower() and "round_offsets" not in e.lower()]
                if critical:
                    _log(f"[ERROR] Refusing to overlay: {offset_path.name} has critical errors")
                    sys.exit(1)
                _log(f"  [WARN] Proceeding with sidecar despite non-critical warnings")
            round_offsets = {int(k): v for k, v in off_data.get("round_offsets", {}).items()}
            video_total_seconds = float(off_data.get("total_duration_seconds", 0))
            # Compute per-round video duration from batches
            # Each batch divides its duration equally among its rounds
            for b in off_data.get("batches", []):
                rs = int(b["round_start"])
                re_end = int(b["round_end"])  # avoid shadowing re module
                dur = float(b["duration_seconds"])
                per_round = dur / (re_end - rs + 1)
                for rn in range(rs, re_end + 1):
                    round_video_duration[rn] = per_round
            _log(f"Round offsets: {len(round_offsets)} rounds from {offset_path.name}")
            _log(f"  Batch durations: {len(off_data.get('batches', []))} batches")
            _log(
                f"  [OK] sidecar validated vs video "
                f"({video_secs:.2f}s / claimed {video_total_seconds:.2f}s)"
            )
        except SystemExit:
            raise
        except Exception as e:
            _log(f"[warn] Failed to load round offsets: {e}")

    # Load round tick ranges (full round incl freeze + post-death) — used
    # only for legacy round_start_tick detection. Frame->tick mapping below
    # uses the trimmed play ranges so the overlay matches what CSDM
    # actually recorded (freeze_end - margin  ->  death + margin / round_end).
    full_round_tick_ranges = _load_round_tick_ranges(demo_path)
    if full_round_tick_ranges:
        _log(f"Round tick ranges: {len(full_round_tick_ranges)} rounds loaded")

    # Play ranges: what CSDM actually recorded. Prefer authoritative
    # per-round tick ranges + durations written by concat_rounds.py when it
    # found sequence-*-tick-N-to-M.mp4 files (ground truth from CSDM).
    # Fall back to event-driven heuristic (freeze_end->death) when absent.
    sidecar_play_ticks: dict[int, tuple[int, int]] = {}
    sidecar_play_durations: dict[int, float] = {}
    if offset_path.is_file() and round_offsets:
        try:
            with open(offset_path) as f:
                off_full = json.load(f)
            for k, v in (off_full.get("per_round_ticks") or {}).items():
                sidecar_play_ticks[int(k)] = (int(v[0]), int(v[1]))
            for k, v in (off_full.get("per_round_durations") or {}).items():
                sidecar_play_durations[int(k)] = float(v)
        except Exception as e:
            _log(f"[warn] sidecar per_round parse failed: {e}")

    if sidecar_play_ticks and sidecar_play_durations:
        round_tick_ranges = sidecar_play_ticks
        sorted_rns = sorted(round_offsets.keys())
        cumulative = 0.0
        for rn in sorted_rns:
            round_offsets[rn] = cumulative
            if rn in sidecar_play_durations:
                round_video_duration[rn] = sidecar_play_durations[rn]
            cumulative += round_video_duration.get(rn, 0.0)
        _log(
            f"  [sync] per-round ticks/durations from CSDM sequence files: "
            f"{len(sidecar_play_ticks)} rounds, {cumulative:.2f}s total"
        )
    else:
        # Sidecar is missing per_round_ticks/per_round_durations. Without the
        # authoritative per-round tick + duration data we CANNOT map demo ticks
        # to video frames correctly — every silent fallback (event-driven play
        # ranges, full-round tick spans) drifts from the actual recorded video
        # and desyncs the overlay (observed ~78s off on uniform-round videos,
        # plus an inverted round-1 range). Refuse instead of guessing.
        #
        # Fix at the source: concat_rounds.py must persist per_round_ticks +
        # per_round_durations into the sidecar, derived from the real CSDM
        # sequence-*-tick-N-to-M.mp4 clips. If the render used
        # --concatenate-sequences those clips are deleted and the sidecar is
        # untrustworthy — render per-round sequences (no --concatenate-sequences)
        # so concat can recover the tick spans.
        _log(
            "[ERROR] Sidecar missing per_round_ticks/per_round_durations — "
            "cannot sync overlay ticks to video frames. Refusing to guess. "
            "Re-run concat with per-round sequence clips preserved (no "
            "--concatenate-sequences) so concat_rounds.py writes authoritative "
            "per-round tick/duration data into the sidecar."
        )
        sys.exit(1)

    # Determine round_start_tick (needed for legacy single-round mode)
    round_start_tick = 0
    if round_num is not None:
        # --round flag given explicitly
        if full_round_tick_ranges and round_num in full_round_tick_ranges:
            round_start_tick, _ = full_round_tick_ranges[round_num]
            _log(f"Round {round_num} start tick: {round_start_tick} (from parquet)")
        else:
            from demoparser2 import DemoParser
            p = DemoParser(str(demo_path))
            events = p.parse_event("round_start")
            if not events.empty:
                for _, r in events.iterrows():
                    if int(r["round"]) == round_num:
                        round_start_tick = int(r["tick"])
                        break
            if round_start_tick > 0:
                _log(f"Round {round_num} start tick: {round_start_tick} (from round_start event)")
            else:
                _log(f"[ERROR] Round {round_num} not found")
                sys.exit(1)
    elif round_offsets:
        # Auto-detect from first round in sidecar. round_start_tick should
        # refer to the full round start (incl freeze) — legacy callers.
        first_round = min(round_offsets.keys())
        if full_round_tick_ranges and first_round in full_round_tick_ranges:
            round_start_tick, _ = full_round_tick_ranges[first_round]
            _log(f"Auto first round {first_round} start tick: {round_start_tick}")
    elif round_tick_ranges:
        # No offsets but have ranges - use first round
        first_round = min(round_tick_ranges.keys())
        round_start_tick, _ = round_tick_ranges[first_round]
        _log(f"First round {first_round} start tick: {round_start_tick}")

    # -- PBDEMS2 note ------------------------------------------------------------
    # All CS2 demos (HLTV + FACEIT) are PBDEMS2 containers, but they DO carry usercmd
    # input (usercmd_buttonstate_*). The old assumption that PBDEMS2 lacks input data
    # was wrong and silently disabled the keyboard overlay on every demo. Always decode.
    is_pbdems2 = False

    # -- Step 1: Keyboard states -------------------------------------------------
    if not is_pbdems2:
        t1 = time.time()
        _log(f"Extracting keyboard states via demoparser2 (DEMOPARSER_TICK_FIELDS)...")
        per_sig = _extract_keyboard_states(
            demo_path, steam_id, frame_count, fps,
            round_offsets=round_offsets or None,
            round_tick_ranges=round_tick_ranges or None,
            round_video_duration=round_video_duration or None,
        )
        if not per_sig or all(len(v) == 0 for v in per_sig.values()):
            _log("[WARN] No keyboard states extracted — rendering utility-only overlay")
            per_sig = {s: [] for s in _OVERLAY_SIGNALS}
        _log(f"Keyboard: {len(next(iter(per_sig.values())))} frames x {len(per_sig)} signals ({time.time()-t1:.1f}s)")
    else:
        per_sig = {s: [] for s in _OVERLAY_SIGNALS}

    # -- Step 2: Generate keyboard sprite PNGs -----------------------------------
    if work_dir is not None:
        work_dir = Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        work_dir_created = False
    else:
        work_dir = Path(tempfile.mkdtemp())
        work_dir_created = True
    try:
        # output_path is created inside work_dir; defined early so the finally
        # cleanup can relocate it even if an exception fires before line below.
        output_path = video_path.with_suffix(".overlay.mp4")
        t4 = time.time()
        if not is_pbdems2:
            t2 = time.time()
            _log(f"Generating key cap sprites...")
            assets = generate_key_assets(work_dir / "sprites")
            png_inputs = overlay_png_input_paths(assets)
            _log(f"{len(png_inputs)} PNGs ({time.time()-t2:.1f}s)")

            keyboard_fc, keyboard_out_label = build_png_overlay_filter(
                per_sig,
                assets=assets,
                placement="bottom-center",
                video_width=width,
                video_height=height,
                pressed_release_fade_frames=0,
                pressed_release_fade_steps=0,
                video_label="[0:v]",
                png_input_offset=1,
            )
        else:
            _log("[PBDEMS2] Skipping keyboard sprite generation")
            assets = None
            png_inputs = []
            keyboard_fc = ""
            keyboard_out_label = "[0:v]"
        if not keyboard_fc:
            keyboard_fc = ""
            keyboard_out_label = "[0:v]"

        # Pre-render a static rounded-corner mask for the PiP body. One mask
        # is shared by all PiPs in every batch/pass; the looped PNG is fed to
        # ffmpeg as an extra input and applied via alphamerge in each
        # _build_pip_overlay when PIP_CORNER_RADIUS > 0.
        pip_mask_path: Path | None = None
        if PIP_CORNER_RADIUS > 0:
            pip_body_size = _pip_body(height)
            pip_mask_path = work_dir / f"pip_corner_mask_{pip_body_size}.png"
            if not pip_mask_path.exists():
                _make_rounded_corner_mask(pip_mask_path, pip_body_size, PIP_CORNER_RADIUS)
            _log(f"  Rounded PiP corners radius={PIP_CORNER_RADIUS}px mask={pip_mask_path.name}")

        # -- Step 3: Render utility throw flight clips ---------------------------
        t3 = time.time()
        _log(f"Rendering utility throw flight clips...")
        flight_clips = _render_throw_flight_clips(
            demo_path, steam_id, fps, frame_count, work_dir,
            video_path=video_path,
            round_offsets=round_offsets or None,
            round_tick_ranges=round_tick_ranges or None,
            total_duration_seconds=video_total_seconds,
            util_cams_root=util_cams_root,
        )
        n_clips = len(flight_clips) if flight_clips else 0
        _log(f"Flight clips: {n_clips} ({time.time()-t3:.1f}s)")
        # Validate ALL renderable throws have clips — never silently skip.
        # throws is loaded inside _render_throw_flight_clips, so recompute the
        # expected count here from the same source (flight_ticks > 0 filter).
        # Use the real demo tick span (not round numbers) for the range filter.
        if round_tick_ranges:
            _exp_lo = min(rt[0] for rt in round_tick_ranges.values())
            _exp_hi = max(rt[1] for rt in round_tick_ranges.values())
        elif round_offsets:
            _exp_lo, _exp_hi = 0, 0
        else:
            _exp_lo, _exp_hi = 0, 0
        n_expected_raw = _load_player_throws(demo_path, steam_id, _exp_lo, _exp_hi)
        if n_expected_raw is None:
            _log("[ERROR] CS2UtilArchive data missing for this demo — cannot "
                 "validate/ render utility-cam overlay. Extract+analyze the "
                 "demo in CS2UtilArchive first.")
            sys.exit(1)
        # Exclude decoys from expected count — they have no flight clip
        # (decoy stands upright, no flight arc to chase), so the batch
        # render can't produce a clip for them. They'd cause a false
        # `n_clips < n_expected` failure.
        n_expected = len([t for t in n_expected_raw
                          if str(t.get("util_type", "")).lower() != "decoy"])
        if n_clips < n_expected:
            _log(f"[WARN] Only {n_clips} flight clips for {n_expected} throws "
                  f"({n_expected - n_clips} missing — edge cases; continuing with {n_clips} clips)")

        if batches > 0 and round_offsets:
            _log(f"Batched overlay: {batches} round(s) per batch")
            if _overlay_output_valid(output_path):
                _log(f"  [skip] Overlay output already exists: {output_path.name}")
                return

            boundaries = _compute_batch_boundaries(round_offsets, fps, frame_count, batches)
            if not boundaries:
                _log("  [warn] No batch boundaries computed; falling through to single-pass")
            else:
                batch_dir = video_path.parent
                pip_input_offset = 1 + len(png_inputs)
                t5 = time.time()
                for batch_start_rn, batch_end_rn, batch_start_frame, batch_start_sec, batch_end_sec in boundaries:
                    batch_name = f"{OVERLAY_BATCH_PREFIX}{batch_start_rn:03d}-{batch_end_rn:03d}.mp4"
                    batch_path = batch_dir / batch_name
                    batch_end_frame = min(int(batch_end_sec * fps), frame_count)
                    if batch_end_frame <= batch_start_frame:
                        _log(f"  [batch] {batch_name} empty range, skipping")
                        continue

                    if _overlay_output_valid(batch_path):
                        _log(f"  [batch] {batch_name} exists, skipping")
                        continue

                    _log(f"  [batch] {batch_name} frames {batch_start_frame}-{batch_end_frame}")

                    # Slice per_sig to this batch's frame range.
                    batch_per_sig = {
                        sig: per_sig[sig][batch_start_frame:batch_end_frame]
                        for sig in per_sig
                    }

                    # Rebuild keyboard filter (smaller graph per batch).
                    if not is_pbdems2:
                        batch_kb_fc, batch_kb_label = build_png_overlay_filter(
                            batch_per_sig,
                            assets=assets,
                            placement="bottom-center",
                            video_width=width,
                            video_height=height,
                            pressed_release_fade_frames=0,
                            pressed_release_fade_steps=0,
                            video_label="[0:v]",
                            png_input_offset=1,
                        )
                    else:
                        batch_kb_fc = ""
                        batch_kb_label = "[0:v]"
                    if not batch_kb_fc:
                        batch_kb_fc = ""
                        batch_kb_label = "[0:v]"

                    # Filter + rebase PiP clips to this batch's local frame range.
                    batch_pips: list[PipClip] = []
                    batch_frame_count = batch_end_frame - batch_start_frame
                    for clip in flight_clips:
                        if clip.end_frame <= batch_start_frame or clip.start_frame >= batch_end_frame:
                            continue
                        local = PipClip(
                            clip_path=clip.clip_path,
                            start_frame=max(0, clip.start_frame - batch_start_frame),
                            end_frame=min(batch_frame_count, clip.end_frame - batch_start_frame),
                            util_type=clip.util_type,
                        )
                        if local.start_frame < local.end_frame:
                            batch_pips.append(local)

                    pip_fc = ""
                    pip_label = batch_kb_label
                    sorted_batch_pips: list[PipClip] = []
                    if batch_pips:
                        mask_idx = None
                        if pip_mask_path is not None:
                            mask_idx = pip_input_offset + len(batch_pips)
                        pip_parts, pip_label, sorted_batch_pips = _build_pip_chain(
                            batch_pips, width, height, fps,
                            start_label=batch_kb_label,
                            pip_input_offset=pip_input_offset,
                            mask_input_idx=mask_idx,
                        )
                        pip_fc = ";".join(pip_parts)

                    # Merge filter chains into a single ffmpeg pass.
                    if batch_kb_fc and pip_fc:
                        combined_fc = f"{batch_kb_fc};{pip_fc}"
                        out_label = pip_label
                    elif batch_kb_fc:
                        combined_fc = batch_kb_fc
                        out_label = batch_kb_label
                    elif pip_fc:
                        combined_fc = pip_fc
                        out_label = pip_label
                    else:
                        _log(f"  [batch] {batch_name} no overlay, re-encoding segment for codec consistency")
                        _ffmpeg_encode(
                            str(video_path), [],
                            ["-filter_complex", "[0:v]null[outv]"],
                            "[outv]", str(batch_path),
                            segment=(batch_start_sec, batch_end_sec),
                        )
                        continue

                    fc_script = work_dir / f"batch_{batch_start_rn:03d}_{batch_end_rn:03d}.txt"
                    fc_script.write_text(combined_fc, encoding="utf-8")
                    extra = list(png_inputs) + [c.clip_path for c in sorted_batch_pips]
                    loops = {str(pip_mask_path)} if pip_mask_path is not None else None
                    if pip_mask_path is not None:
                        extra.append(pip_mask_path)
                    _ffmpeg_encode(
                        str(video_path), extra,
                        ["-filter_complex_script", str(fc_script.resolve())],
                        out_label, str(batch_path),
                        segment=(batch_start_sec, batch_end_sec),
                        loop_inputs=loops,
                    )

                _log(f"  [batch] All batches encoded in {time.time()-t5:.1f}s")
                batch_files = sorted(
                    [bf for bf in batch_dir.glob(f"{OVERLAY_BATCH_PREFIX}*.mp4")],
                    key=lambda f: int(re.match(rf"{OVERLAY_BATCH_PREFIX}(\d+)-\d+\.mp4$", f.name).group(1)),
                )
                if not batch_files:
                    _log("[ERROR] no batch files produced; cannot concat")
                    sys.exit(1)
                _log(f"  [concat] {len(batch_files)} batch files -> {output_path.name}")
                t6 = time.time()
                _concat_overlay_batches(batch_files, output_path)
                _log(f"  [concat] OK in {time.time()-t6:.1f}s")
                for bf in batch_files:
                    bf.unlink(missing_ok=True)
                mb = output_path.stat().st_size / 1024 / 1024
                _log(f"Overlay: {output_path.name} ({mb:.0f} MB) in {time.time()-t4:.1f}s")
                return

        if keyboard_fc and flight_clips:
            # 2-pass: keyboard then PiP
            kb_temp = work_dir / "kb_temp.mp4"
            fc_script = work_dir / "kb_fc.txt"
            fc_script.write_text(keyboard_fc, encoding="utf-8")
            fc_args = ["-filter_complex_script", str(fc_script.resolve())]
            if len(keyboard_fc) <= 6000:
                fc_args = ["-filter_complex", keyboard_fc]
            _log(f"Pass 1: keyboard overlay (1 vid + {len(png_inputs)} sprites)...")
            _ffmpeg_encode(str(video_path), png_inputs, fc_args,
                           keyboard_out_label, str(kb_temp))
            _log(f"  ({time.time()-t4:.1f}s)")

            pip_parts, pip_current, sorted_clips = _build_pip_chain(
                flight_clips, width, height, fps,
                mask_input_idx=(1 + len(flight_clips) if pip_mask_path is not None else None))
            pip_fc = ";".join(pip_parts)
            fc_script2 = work_dir / "pip_fc.txt"
            fc_script2.write_text(pip_fc, encoding="utf-8")
            _log(f"Pass 2: PiP composite (1 vid + {len(flight_clips)} flight clips)...")
            pass2_inputs: list[Path] = [c.clip_path for c in sorted_clips]
            pass2_loops = {str(pip_mask_path)} if pip_mask_path is not None else None
            if pip_mask_path is not None:
                pass2_inputs.append(pip_mask_path)
            _ffmpeg_encode(str(kb_temp), pass2_inputs,
                           ["-filter_complex_script", str(fc_script2.resolve())],
                           pip_current, str(output_path),
                           loop_inputs=pass2_loops)

        elif keyboard_fc:
            # Keyboard only
            fc_script = work_dir / "kb_fc.txt"
            fc_script.write_text(keyboard_fc, encoding="utf-8")
            fc_args = ["-filter_complex_script", str(fc_script.resolve())]
            if len(keyboard_fc) <= 6000:
                fc_args = ["-filter_complex", keyboard_fc]
            _log(f"Keyboard overlay (1 vid + {len(png_inputs)} sprites)...")
            _ffmpeg_encode(str(video_path), png_inputs, fc_args,
                           keyboard_out_label, str(output_path))

        elif flight_clips:
            # PiP only
            pip_parts, pip_current, sorted_clips = _build_pip_chain(
                flight_clips, width, height, fps,
                mask_input_idx=(1 + len(flight_clips) if pip_mask_path is not None else None))
            pip_fc = ";".join(pip_parts)
            fc_script = work_dir / "pip_fc.txt"
            fc_script.write_text(pip_fc, encoding="utf-8")
            _log(f"PiP composite (1 vid + {len(flight_clips)} flight clips)...")
            pip_only_inputs: list[Path] = [c.clip_path for c in sorted_clips]
            pip_only_loops = {str(pip_mask_path)} if pip_mask_path is not None else None
            if pip_mask_path is not None:
                pip_only_inputs.append(pip_mask_path)
            _ffmpeg_encode(str(video_path), pip_only_inputs,
                           ["-filter_complex_script", str(fc_script.resolve())],
                           pip_current, str(output_path),
                           loop_inputs=pip_only_loops)

        else:
            _log("No overlay to apply.")
            return

        mb = output_path.stat().st_size / 1024 / 1024
        _log(f"Overlay: {output_path.name} ({mb:.0f} MB) in {time.time()-t4:.1f}s")
    finally:
        # Only remove the work dir if WE created it (tempdir). When the caller
        # passes an explicit --work-dir (the pipeline does), the output sidecar
        # (video.overlay.mp4) lives inside it and the caller owns cleanup —
        # deleting it here would destroy the just-built overlay.
        if work_dir_created:
            _log(f"Cleanup {work_dir.name}")
            shutil.rmtree(work_dir, ignore_errors=True)
        else:
            _log(f"Leaving work dir {work_dir} (caller-owned) with overlay output")
    _log(f"Total: {time.time()-t_overall:.1f}s")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Overlay keyboard + utility throw flight PiP on POV video."
    )
    parser.add_argument("--video", required=True, help="Path to video.mp4 (modified in place)")
    parser.add_argument("--demo", required=True, help="Path to .dem file")
    parser.add_argument("--steam-id", required=True, help="Steam64 ID")
    parser.add_argument("--round", type=int, default=None,
                        help="Round number (1-indexed, optional)")
    parser.add_argument("--batches", type=int, default=5,
                        help="Rounds per overlay batch (default 5). "
                             "0=single-pass (slow on large videos). "
                             "Smaller filter graph per batch -> 2-3x speedup; "
                             "crash resumes from last completed batch.")
    parser.add_argument("--util-cams-root", type=Path, default=None,
                        help="Path to utility_cams/ cache dir. Default: walk up from video.")
    parser.add_argument("--work-dir", type=Path, default=None,
                        help="Working directory for temp files (default: tempdir)")
    args = parser.parse_args()

    # Ensure CS2UtilArchive has extracted+analyzed this demo (throws.parquet).
    # Auto-extract if missing so the overlay step works without manual setup.
    _ensure_cs2util_data(Path(args.demo))

    run_overlay(Path(args.video), Path(args.demo), args.steam_id, args.round, args.batches,
                util_cams_root=args.util_cams_root, work_dir=args.work_dir)


if __name__ == "__main__":
    main()
