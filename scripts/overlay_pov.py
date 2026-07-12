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

# -- Point at CS2UtilArchive for overlay pipeline + parquet data ----------
_CS2UTIL_ROOT = Path(r"D:\Projects\CS2UtilArchive")
_CS2UTIL_SCRIPTS = _CS2UTIL_ROOT / "scripts"
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

# -- Constants -----------------------------------------------------------
TICKRATE = 64.0

# --- Util PiP burn-in geometry -----------------------------------------
# Preferred body = video_height * 2 // 5; shrinks if PIP_MAX_SIMULTANEOUS
# stacked slots (plus gaps/margins) would not fit the frame height.
PIP_OUTLINE_THICKNESS = 2       # Pixels. White border around each PiP (0 disables outline).
PIP_CORNER_RADIUS = 0           # Pixels. Rounded corner radius. 0 = disabled (see note below).
PIP_MARGIN = 12                 # Pixels. Outline-to-outline gap from video edge.
PIP_GAP = 12                    # Pixels. Outline-to-outline gap between stacked PiPs.
PIP_MAX_SIMULTANEOUS = 3

FLIGHT_DIR_NAME = "throw_flights"

OVERLAY_BATCH_PREFIX = "batch-overlay-"


def _overlay_output_valid(path: Path) -> bool:
    """Return True if a batch/final overlay file is present and non-empty."""
    return path.is_file() and path.stat().st_size > 100_000


def _pip_body(video_height: int) -> int:
    """Square PiP slot size: prefer height*2/5, shrink so max stack fits."""
    preferred = video_height * 2 // 5
    available = video_height - 2 * PIP_MARGIN
    n = max(1, PIP_MAX_SIMULTANEOUS)
    max_fit = (available - (n - 1) * PIP_GAP) // n
    return min(preferred, max(1, max_fit))


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
REQUIRED_TICK_FIELDS = (
    "tick", "steamid",
    "FORWARD", "LEFT", "RIGHT", "BACK", "FIRE", "RIGHTCLICK", "WALK",
    "ducked", "ducking", "in_duck_jump", "old_jump_pressed", "buttons",
    "is_airborne", "velocity_Z",
)


def _log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# -- Video probe helpers -------------------------------------------------

# Cache: flight clip path -> duration in seconds. Probing once per clip is
# cheap (~50ms) and skips repeated ffprobe calls for shared clips.
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


def _cs2util_results_dir() -> Path | None:
    d = _CS2UTIL_ROOT / "results"
    return d if d.is_dir() else None


def _find_demo_data_dir(demo_path: Path) -> Path | None:
    """Find CS2UtilArchive data dir for this demo (where throws.parquet lives)."""
    results = _cs2util_results_dir()
    if results is None:
        return None
    exact = demo_path.stem
    broad = re.sub(r"-p\d+$", "", demo_path.stem, flags=re.IGNORECASE)

    # Search all project subdirs: results/*/data/demo=<name>/
    for project_dir in results.iterdir():
        if not project_dir.is_dir():
            continue
        data_dir = project_dir / "data"
        if not data_dir.is_dir():
            continue
        for d in data_dir.iterdir():
            if not d.is_dir() or not d.name.startswith("demo="):
                continue
            dn = d.name[len("demo="):]
            if dn == exact or broad in dn:
                return d
    return None


# -- Round tick ranges ---------------------------------------------------


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
    ticks = sorted(int(t) for t in events["tick"])
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

    t0 = time.time()
    parser = DemoParser(str(demo_path))
    ticks_df = parser.parse_ticks(
        list(REQUIRED_TICK_FIELDS),
        players=[int(steam_id)],
    )
    if ticks_df.empty:
        _log("[ERROR] No tick data from demo")
        sys.exit(1)

    ticks_df = ticks_df.sort_values(["tick"])

    # Build per-tick state lookup
    # apply_jump_inference=False avoids mid-air bhop spam.
    # Inferred jumps: leave-ground crouch burst, or standing leave-ground
    # confirmed by upward vz a tick later (CS2 often omits IN_JUMP).
    tick_states: dict[int, dict[str, int]] = {}
    prev_row = None
    jump_burst = JumpBurstState()
    for _, row in ticks_df.iterrows():
        tick = int(row["tick"])
        states, _ = overlay_tick_from_row(row, apply_jump_inference=False)
        jump, _ = advance_inferred_jump_burst(
            row,
            prev_row,
            duck_on=states["duck"],
            bitmask_jump=states["jump"],
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


@dataclass
class PipClip:
    clip_path: Path
    start_frame: int
    end_frame: int
    util_type: str
    pip_index: int = 0


def _load_player_throws(
    demo_path: Path,
    steam_id: str,
    round_start_tick: int = 0,
    round_end_tick: int = 0,
) -> list[dict[str, Any]]:
    """Load player's renderable throws from CS2UtilArchive throws.parquet.

    Filters to throws with flight_ticks > 0, optionally within round tick range.
    """
    data_dir = _find_demo_data_dir(demo_path)
    if data_dir is None:
        _log("  [throws] No CS2UtilArchive data dir found")
        return []

    throws_path = data_dir / "throws.parquet"
    if not throws_path.is_file():
        _log(f"  [throws] throws.parquet not found at {throws_path}")
        return []

    import pandas as pd
    df = pd.read_parquet(throws_path)
    sid = int(steam_id)
    player_df = df[
        (df["thrower_steamid"] == sid)
        & (df["flight_ticks"] > 0)
    ].copy()

    if round_start_tick > 0:
        end = round_end_tick if round_end_tick > 0 else round_start_tick + int(45 * 60 * TICKRATE)
        player_df = player_df[
            (player_df["throw_tick"] >= round_start_tick)
            & (player_df["throw_tick"] <= end)
        ]

    if player_df.empty:
        _log("  [throws] No throws with flight for this player")
        return []

    _log(f"  [throws] {len(player_df)} renderable throws")
    side_counts = player_df["thrower_side"].value_counts().to_dict()
    t_count = int(side_counts.get("T", 0))
    ct_count = int(side_counts.get("CT", 0))
    _log(f"  [throws] side breakdown: T={t_count} CT={ct_count}")
    if (t_count == 0 or ct_count == 0) and (t_count + ct_count) > 0:
        missing = "CT" if ct_count == 0 else "T"
        _log(f"  [throws] NOTE: zero throws on {missing} side — data observation, not pipeline bug")
    return [dict(row) for _, row in player_df.iterrows()]


def _build_round_frame_ranges(
    round_offsets: dict[int, float],
    round_tick_ranges: dict[int, tuple[int, int]],
    fps: float,
    total_frames: int,
) -> dict[int, tuple[int, int]]:
    """Build per-round frame ranges from round_offsets sidecar.

    Returns {round_num: (start_frame, end_frame)}.
    """
    sorted_rounds = sorted(round_offsets.keys())
    result: dict[int, tuple[int, int]] = {}
    for i, rn in enumerate(sorted_rounds):
        start_frame = int(round_offsets[rn] * fps)
        if i + 1 < len(sorted_rounds):
            end_frame = int(round_offsets[sorted_rounds[i + 1]] * fps) - 1
        else:
            end_frame = total_frames - 1
        result[rn] = (start_frame, end_frame)
    return result


def _rm_empty_dir(d: Path) -> None:
    """Remove *d* if it exists and contains no files (skips subdirs)."""
    if d.is_dir() and not any(d.iterdir()):
        try:
            d.rmdir()
        except OSError:
            pass


def _util_slug_for_throw(throw: dict, demo_path: Path) -> tuple[str, str, str]:
    """Return (util_id, util_slug, demo_id) for a throw row.

    util_id = ``<map>:<util_type>:<side>:<land_x>_<land_y>_<land_z>`` (landing
    position, no match id) — matches CS2UtilArchive's render_utils folder
    architecture. util_slug = util_render_slug(util_id).
    """
    map_name = str(throw.get("map") or throw.get("map_name") or demo_path.stem)
    util_type = str(throw.get("util_type", "unknown")).lower()
    side = str(throw.get("thrower_side", "T") or "T").upper()
    land_x = int(round(float(throw.get("land_x", 0) or 0)))
    land_y = int(round(float(throw.get("land_y", 0) or 0)))
    land_z = int(round(float(throw.get("land_z", 0) or 0)))
    util_id = f"{map_name}:{util_type}:{side}:{land_x}_{land_y}_{land_z}"
    util_slug = util_render_slug(util_id)
    demo_id = re.sub(r"^\d{6,}-", "", str(throw.get("demo_id", demo_path.stem)))
    return util_id, util_slug, demo_id


def _run_batch_util_cams_subprocess(
    demo_path: Path,
    steam_id: str,
    data_dir: Path,
    util_cams_root: Path,
    chunk_size: int = 0,
    demo_data_dir_name: str | None = None,
) -> int:
    """Shell out to scripts/render_util_cams.py for util_cam prep + render.

    Bypasses the inline run_csdm loop (Bug A: random POV instead of chase cam
    when the inject thread races csdm's actions-file write). render_util_cams.py
    handles BOTH prep (filter throws.parquet by steamid, create util_cam dirs
    + _throw_poses.json) and render (call CS2UtilArchive's render_spot_batch
    in one CS2 launch per chunk of N spots). Idempotent — re-runs are no-ops
    for already-rendered clips.
    """
    import subprocess
    script_path = Path(__file__).resolve().parent / "render_util_cams.py"
    # Extract demo_id from the per-demo data dir name. Caller passes the
    # leaf explicitly because the parent (data_dir) doesn't start with "demo=".
    # Leaf: "demo=2395002-furia-vs-falcons-m2-anubis" → "2395002-furia-vs-falcons-m2-anubis".
    demo_id = None
    if demo_data_dir_name and demo_data_dir_name.startswith("demo="):
        demo_id = demo_data_dir_name[len("demo="):]
    elif data_dir and data_dir.name.startswith("demo="):
        demo_id = data_dir.name[len("demo="):]
    cmd = [
        sys.executable, str(script_path),
        "--util-cams-root", str(util_cams_root.resolve()),
        "--data-dir", str(data_dir.resolve()),
        "--steamid", str(steam_id),
        "--chunk-size", str(chunk_size),
    ]
    if demo_id:
        cmd += ["--demo-id", demo_id]
    # Derive demos_dir from CS2UtilArchive project root
    demos_dir = _CS2UTIL_ROOT / "demos" / "extracted"
    cmd += ["--demos-dir", str(demos_dir.resolve())]
    _log(f"  [flight] CMD: {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd, cwd=str(util_cams_root.parent.parent.parent),
            check=False,
        )
        return result.returncode
    except Exception as exc:
        _log(f"  [flight] render_util_cams.py subprocess failed: {exc}")
        return 1


def _scan_utility_cams_clips(video_path: Path) -> dict[str, Path]:
    """Scan utility_cams for pre-rendered clips (orbit + victims + flight).

    Uses _throw_poses.json files to map throw_id -> mp4 clip, since the
    _throws dict in each json maps throw_ids to camera positions for that
    camera pose directory. Multiple throw_ids can share one .mp4 (one-shot
    victim POVs); matched by entity ID in filename when ambiguous.
    """
    pre_rendered: dict[str, Path] = {}
    p = video_path.parent
    util_cams = None
    for _ in range(5):
        cand = p / "utility_cams"
        if cand.is_dir():
            util_cams = cand
            break
        p = p.parent
    if not util_cams or not util_cams.is_dir():
        return {}

    for poses_f in util_cams.rglob("_throw_poses.json"):
        try:
            poses = json.loads(poses_f.read_text())
        except Exception:
            continue
        throw_map = poses.get("_throws", {})
        if not throw_map:
            continue
        mp4s = sorted(poses_f.parent.glob("*.mp4"))
        if not mp4s:
            continue
        for tid in throw_map:
            # Match by entity ID substring in filename.
            # New naming: flight_<short-slug>.mp4 or flight_orbit_<short-slug>.mp4
            # Each throw_id has its own dir + 1 mp4 (1:1 mapping).
            ent_part = tid.split(":")[1] if ":" in tid else ""
            matching = [m for m in mp4s if ent_part and ent_part in m.name]
            if len(matching) == 1:
                pre_rendered[tid] = matching[0]
            elif matching:
                pre_rendered[tid] = matching[0]
    return pre_rendered


def _render_throw_flight_clips(
    demo_path: Path,
    steam_id: str,
    fps: float,
    frame_count: int,
    output_dir: Path,
    video_path: Path | None = None,
    round_offsets: dict[int, float] | None = None,
    round_tick_ranges: dict[int, tuple[int, int]] | None = None,
    total_duration_seconds: float = 0.0,
    util_cams_root: Path | None = None,
) -> list[PipClip]:
    """Render CSDM flight clips for each player throw.

    Shells out to scripts/batch_util_cams.py (Batched CSDM — one CS2 launch
    per chunk of N spots, spec_goto precomputed). Fix for Bug A: inline
    run_csdm loop races csdm's actions-file write → random POV instead of
    chase cam. See scripts/batch_util_cams.py for batching details.
    Outputs 1920x1080 clips to <util_cams_root>/unnamed/<throw_id_slug>/ (match-id
    prefix stripped, matching CS2UtilArchive's render_utils folder architecture).
    Returns PipClip metadata sorted by start_frame.
    """
    # Determine first round tick for filtering
    first_round_tick = 0
    last_round_tick = 0
    if round_tick_ranges and round_offsets:
        first_round = min(round_offsets.keys())
        last_round = max(round_offsets.keys())
        rs, _ = round_tick_ranges.get(first_round, (0, 0))
        _, re = round_tick_ranges.get(last_round, (0, 0))
        first_round_tick = rs
        last_round_tick = re

    throws = _load_player_throws(demo_path, steam_id, first_round_tick, last_round_tick)
    if not throws:
        return []

    # Load trajectories once (per-throw chase-cam injection needs them).
    # Bug A fix: without trajectories + throw_pose + run_csdm inject thread,
    # csdm free-cams a random POV instead of chasing the grenade.
    data_dir = _find_demo_data_dir(demo_path)
    traj_by_throw: dict[str, Any] = {}
    if data_dir is not None:
        traj_path = data_dir / "trajectories.parquet"
        if traj_path.is_file():
            import pandas as _pd
            _traj_df = _pd.read_parquet(traj_path)
            for tid, sub in _traj_df.groupby("throw_id"):
                traj_by_throw[str(tid)] = sub.sort_values("tick").copy()
            _log(f"  [flight] Loaded {len(traj_by_throw)} trajectories")
        else:
            _log(f"  [flight] WARN: {traj_path.name} missing — flight cams will be skipped")
    else:
        _log(f"  [flight] WARN: no CS2UtilArchive data dir — flight cams will be skipped")

    # Resolve utility_cams directory. Explicit --util-cams-root wins (used by
    # pipeline in dual-upload mode to point at the persistent render cache
    # under renders/, not a freshly-created dir under youtube/).
    if util_cams_root is not None:
        util_cams_root = Path(util_cams_root)
        util_cams_root.mkdir(parents=True, exist_ok=True)
    else:
        # Walk up from video looking for an existing utility_cams/ cache.
        video_dir = video_path.parent if video_path else output_dir
        resolved: Path | None = None
        p = video_dir
        for _ in range(5):
            cand = p / "utility_cams"
            if cand.is_dir():
                resolved = cand
                break
            p = p.parent
        if resolved is None:
            resolved = video_dir / "utility_cams"
        resolved.mkdir(parents=True, exist_ok=True)
        util_cams_root = resolved

    # Build per-round frame ranges from round_offsets
    round_frame_ranges = {}
    if round_offsets and round_tick_ranges:
        round_frame_ranges = _build_round_frame_ranges(
            round_offsets, round_tick_ranges, fps, frame_count,
        )
        _log(f"  [flight] {len(round_frame_ranges)} round frame ranges")

    # Scan pre-rendered clips from utility_cams (_throw_poses.json -> mp4)
    pre_rendered: dict[str, Path] = {} if video_path is None else _scan_utility_cams_clips(video_path)
    if pre_rendered:
        _log(f"  [flight] Found {len(pre_rendered)} pre-rendered clips in utility_cams")

    # Determine if any throw still needs rendering (skip subprocess if all done).
    # A throw is "covered" if either:
    #   (a) its throw_id is in pre_rendered, OR
    #   (b) its util_cam dir contains a shared throw_flight_*.mp4 ≥1MB
    #       (a different throw at the same release position was batched)
    needs_render = False
    for throw in throws:
        tid = str(throw.get("throw_id", ""))
        # util_id-keyed dir: unnamed/<util_id_slug>/ (no match id), matching
        # CS2UtilArchive's render_utils folder architecture. Multiple throws
        # at the same landing spot share one dir.
        _, uid_slug, _ = _util_slug_for_throw(throw, demo_path)
        render_dir_check = util_cams_root / "unnamed" / uid_slug
        util_type = str(throw.get("util_type", "unknown")).lower()
        cam = "flight,detonate" if util_type in ("smoke", "fire", "molotov", "incendiary") else "flight"
        clip = render_dir_check / f"{clip_name_for_cameras(cam, tid)}.mp4"
        has_clip = clip.is_file() and clip.stat().st_size > 100_000
        if tid in pre_rendered or has_clip:
            continue
        needs_render = True
        break

    if needs_render and data_dir is not None:
        _log(f"  [flight] Subprocess: batch_util_cams.py (batched, one CS2 launch per chunk)")
        # data_dir is the per-demo dir (e.g. demo=2395002-furia-vs-falcons-m2-anubis).
        # batch_util_cams.py expects the PARENT (containing demo=* subdirs).
        # Pass both: parent to the subprocess, leaf to extract --demo-id.
        data_dir_parent = data_dir.parent
        rc = _run_batch_util_cams_subprocess(
            demo_path=demo_path,
            steam_id=steam_id,
            data_dir=data_dir_parent,
            util_cams_root=util_cams_root,
            demo_data_dir_name=data_dir.name,
        )
        if rc != 0:
            _log(f"  [flight] batch render FAILED (rc={rc}) — aborting flight clips")
            return []
        # Re-scan after batch render to pick up newly written mp4s + _throw_poses.json
        pre_rendered = _scan_utility_cams_clips(video_path) if video_path else {}
        if pre_rendered:
            _log(f"  [flight] After batch: {len(pre_rendered)} clips now available")
    elif needs_render and data_dir is None:
        _log(f"  [flight] WARN: no CS2UtilArchive data dir — cannot batch-render")

    clips: list[PipClip] = []
    for idx, throw in enumerate(throws):
        throw_tick = int(throw["throw_tick"])
        util_type = str(throw.get("util_type", "unknown")).lower()
        throw_round = int(throw.get("round_num", 0))

        # Frame START mapping using per-round ranges (from throw_tick only).
        # NOTE: do NOT derive end_frame from land_tick. For smokes, land_tick is
        # the smoke's LIFE-END (~19s after throw, smoke lifetime, not the brief
        # airborne flight). land_tick for a late-round smoke lands in a LATER
        # round's ticks, which mapped end_frame crossed the round (and 5x
        # batch) boundary. In batched overlay that PipClip's [start,end] window
        # then overlapped the NEXT batch -> re-included there, and the rebase
        # `max(0, start_frame - batch_start)` clamped it to frame 0 -> the
        # full clip replayed at the start of the next round. The window width
        # does NOT trim ffmpeg playback (eof_action=pass plays the whole clip),
        # so end_frame only drives batch inclusion + PiP stacking. We derive
        # it from the actual rendered clip duration, clamped to the throwing
        # round's end, so a PiP can never bleed into a later round's batch.
        round_end_frame: int | None = None
        if throw_round in round_frame_ranges:
            fs, fe = round_frame_ranges[throw_round]
            round_end_frame = fe
            if throw_round in round_tick_ranges:
                rs, re = round_tick_ranges[throw_round]
                rf = (re - rs) or 1
                start_frame = int(fs + (throw_tick - rs) / rf * (fe - fs))
            else:
                start_frame = int(throw_tick * fps / TICKRATE)
        elif first_round_tick > 0:
            start_frame = int((throw_tick - first_round_tick) * fps / TICKRATE)
        else:
            start_frame = int(throw_tick * fps / TICKRATE)

        start_frame = max(0, start_frame)

        throw_id = str(throw.get("throw_id", ""))
        # util_id-keyed dir (no match id); clip name via clip_name_for_cameras,
        # matching what render_util_cams.py / render_spot_batch wrote.
        _, uid_slug, _ = _util_slug_for_throw(throw, demo_path)
        render_dir = util_cams_root / "unnamed" / uid_slug

        def _pick(preferred: Path, want_detonate: bool) -> Path:
            """Best clip for a window: preferred file, else pre_rendered
            fallback, else a shared clip in the dir (filtered for detonate)."""
            if preferred.is_file() and preferred.stat().st_size > 100_000:
                return preferred
            if throw_id in pre_rendered and pre_rendered[throw_id].is_file() \
                    and pre_rendered[throw_id].stat().st_size > 100_000:
                return pre_rendered[throw_id]
            if render_dir.is_dir():
                cands = [p for p in render_dir.glob("*.mp4")
                         if p.stat().st_size > 1_000_000]
                if cands:
                    if want_detonate:
                        cands = [p for p in cands if "detonate" in p.name] or cands
                    else:
                        cands = [p for p in cands if "detonate" not in p.name] or cands
                    cands.sort(key=lambda p: ("victims" in p.name, p.name))
                    return cands[0]
            return preferred

        # Smoke/fire/molotov render a COMBINED "flight_detonate" clip (flight
        # arc + detonation in one file). Use that as the single PiP.
        # flash/he/decoy use the plain flight clip. (Never the standalone
        # detonate_*.mp4.)
        cam = "flight,detonate" if util_type in ("smoke", "fire", "molotov", "incendiary") else "flight"
        clip_path = _pick(render_dir / f"{clip_name_for_cameras(cam, throw_id)}.mp4",
                          want_detonate=(cam == "flight,detonate"))

        if not clip_path.is_file() or clip_path.stat().st_size < 100_000:
            _log(f"  [flight] SKIP {util_type} throw {idx}: "
                 f"no clip at {clip_path.name} (t{throw_tick})")
            continue

        # Window: actual rendered clip length, anchored at the throw frame,
        # ... clamped to the throwing round's frame end so a smoke thrown late
        # in a round cannot bleed into a later round's batch (which would
        # otherwise replay the clip at frame 0 of that batch = next-round start).
        clip_dur = _probe_clip_duration_seconds(clip_path)
        dur_frames = max(1, int(round(clip_dur * fps))) if clip_dur > 0 else 1
        end_frame = start_frame + dur_frames
        if round_end_frame is not None:
            end_frame = min(end_frame, round_end_frame)
        end_frame = min(end_frame, frame_count - 1)
        if start_frame >= end_frame:
            continue

        clips.append(PipClip(
            clip_path=clip_path,
            start_frame=start_frame,
            end_frame=end_frame,
            util_type=util_type,
        ))

    _log(f"  [flight] {len(clips)} throw clips rendered")
    return sorted(clips, key=lambda c: c.start_frame)


# -- Composite overlay ---------------------------------------------------


def _build_pip_chain(
    flight_clips: list[PipClip],
    width: int,
    height: int,
    fps: float,
    start_label: str = "[0:v]",
    pip_input_offset: int = 1,
) -> tuple[list[str], str, list[PipClip]]:
    """Sort clips, assign stack rows, build PiP filter parts.

    ``start_label`` is the filter graph label the PiP chain starts from
    (default ``[0:v]`` = raw input video; pass the keyboard output label
    when batching to chain keyboard + PiP in a single ffmpeg pass).

    ``pip_input_offset`` is the ffmpeg input index for the FIRST flight
    clip. When sprite PNGs occupy inputs 1-18, pass ``1 + len(png_inputs)``
    so clips are referenced as ``[19:v]``, ``[20:v]``, etc.
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
        fc_part, tag = _build_pip_overlay(clip, pip_current, idx, width, height, fps)
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
    # NOTE: Rounded corners (PIP_CORNER_RADIUS > 0) deliberately disabled.
    # Benchmark on this machine (scripts/_bench_geq.py, 576x576 RGBA, 300 frames):
    #   baseline (format=rgba only): 0.50s
    #   with corner geq:            2.89s  (+7.96 ms/frame/pip, +476% CPU)
    # At 60 fps the per-frame budget is 16.67 ms; one pip consumes ~48% of it,
    # two pips blow the budget and the pipeline would need to drop to ~30 fps.
    # Re-enable only if (a) the project moves overlay compositing to a GPU
    # path (e.g. vspipe + glsl, or a hardware overlay layer) or (b) the target
    # framerate is lowered. Filter expression preserved below for that case:
    #
    #   R = PIP_CORNER_RADIUS
    #   half = pip_body // 2
    #   inner_half = half - R
    #   pre_filters.append("format=rgba")
    #   pre_filters.append(
    #       "geq="
    #       f"r='p(X,Y)':g='p(X,Y)':b='p(X,Y)':"
    #       f"a='if(gt(abs(X-{half})-{inner_half},0)*"
    #       f"gt(abs(Y-{half})-{inner_half},0),"
    #       f"if(lte(hypot(abs(X-{half})-{inner_half},"
    #       f"abs(Y-{half})-{inner_half}),{R}),"
    #       f"255,0),255)'"
    #   )
    pre_filters.append(f"setpts=PTS-STARTPTS+{start_seconds:.6f}/TB")
    pre_chain = f"[{input_idx}:v]" + ",".join(pre_filters) + f"[{scaled_tag}]"

    parts = [
        pre_chain,
        f"{current_label}[{scaled_tag}]"
        f"overlay=x={x}:y={pip_y}:eof_action=pass"
        f"[{tag}]",
    ]
    return ";".join(parts), tag


def _compute_batch_boundaries(
    round_offsets: dict[int, float],
    fps: float,
    frame_count: int,
    batch_size: int,
) -> list[tuple[int, int, int, float, float]]:
    """Group sorted rounds into chunks of ``batch_size`` and return
    ``[(round_start, round_end, batch_start_frame, batch_start_sec, batch_end_sec), ...]``.

    The last batch's end_sec clamps to ``frame_count / fps``. ``batch_end_sec``
    for intermediate batches is the start_sec of the next batch's first round.
    """
    if batch_size < 1 or not round_offsets:
        return []
    sorted_rounds = sorted(round_offsets.keys())
    total_seconds = frame_count / fps
    boundaries: list[tuple[int, int, int, float, float]] = []
    for i in range(0, len(sorted_rounds), batch_size):
        chunk = sorted_rounds[i:i + batch_size]
        rn_start, rn_end = chunk[0], chunk[-1]
        start_sec = float(round_offsets[rn_start])
        if i + batch_size < len(sorted_rounds):
            end_sec = float(round_offsets[sorted_rounds[i + batch_size]])
        else:
            end_sec = total_seconds
        start_frame = int(start_sec * fps)
        boundaries.append((rn_start, rn_end, start_frame, start_sec, end_sec))
    return boundaries


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
        # Sidecar lacks per_round_ticks/durations. The concat sidecar's
        # round_offsets are equally spaced (batch_duration / rounds_per_batch)
        # which is WRONG — real rounds vary from ~28s to ~115s.
        # Compute authoritative per-round play ranges (freeze_end - margin →
        # death+margin / round_end+margin) and accumulate correct video offsets.
        # This matches what CSDM --event rounds actually recorded (skip freeze,
        # stop at death or round_end).
        play_ranges = _load_pov_play_tick_ranges(demo_path, steam_id)
        if play_ranges:
            round_tick_ranges = play_ranges
            sorted_rns = sorted(play_ranges.keys())
            cumulative = 0.0
            for rn in sorted_rns:
                ps, pe = play_ranges[rn]
                dur = (pe - ps) / TICKRATE
                round_offsets[rn] = cumulative
                round_video_duration[rn] = dur
                cumulative += dur
            _log(
                f"  [sync] event-driven play ranges (freeze_end→death/round_end): "
                f"{len(play_ranges)} rounds, {cumulative:.2f}s total"
            )
        else:
            round_tick_ranges = _load_round_tick_ranges(demo_path)
            if not round_tick_ranges:
                round_tick_ranges = full_round_tick_ranges
            if round_offsets:
                sorted_rns = sorted(round_offsets.keys())
                for i, rn in enumerate(sorted_rns):
                    nxt = sorted_rns[i + 1] if i + 1 < len(sorted_rns) else None
                    dur = (round_offsets[nxt] - round_offsets[rn]) if nxt else round_video_duration.get(rn, 0.0)
                    if dur and dur > 0:
                        round_video_duration[rn] = dur
            _log(
                f"  [sync] full-round (round_start events) mapping: "
                f"{len(round_tick_ranges)} rounds, {sum(round_video_duration.values()):.2f}s video"
            )

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

    # -- Step 1: Keyboard states -------------------------------------------------
    t1 = time.time()
    _log(f"Extracting keyboard states via demoparser2 (DEMOPARSER_TICK_FIELDS)...")
    per_sig = _extract_keyboard_states(
        demo_path, steam_id, frame_count, fps,
        round_offsets=round_offsets or None,
        round_tick_ranges=round_tick_ranges or None,
        round_video_duration=round_video_duration or None,
    )
    if not per_sig or all(len(v) == 0 for v in per_sig.values()):
        _log("[ERROR] No keyboard states extracted")
        sys.exit(1)
    _log(f"Keyboard: {len(next(iter(per_sig.values())))} frames x {len(per_sig)} signals ({time.time()-t1:.1f}s)")

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
        if not keyboard_fc:
            keyboard_fc = ""
            keyboard_out_label = "[0:v]"

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
        n_expected = len(_load_player_throws(demo_path, steam_id, _exp_lo, _exp_hi))
        if n_clips < n_expected:
            _log(f"[ERROR] Only {n_clips} flight clips for {n_expected} throws")
            _log(f"[ERROR] {n_expected - n_clips} missing — would silently drop PiPs")
            sys.exit(1)

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
                        pip_parts, pip_label, sorted_batch_pips = _build_pip_chain(
                            batch_pips, width, height, fps,
                            start_label=batch_kb_label,
                            pip_input_offset=pip_input_offset,
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
                    _ffmpeg_encode(
                        str(video_path), extra,
                        ["-filter_complex_script", str(fc_script.resolve())],
                        out_label, str(batch_path),
                        segment=(batch_start_sec, batch_end_sec),
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
                flight_clips, width, height, fps)
            pip_fc = ";".join(pip_parts)
            fc_script2 = work_dir / "pip_fc.txt"
            fc_script2.write_text(pip_fc, encoding="utf-8")
            _log(f"Pass 2: PiP composite (1 vid + {len(flight_clips)} flight clips)...")
            _ffmpeg_encode(str(kb_temp), [c.clip_path for c in sorted_clips],
                           ["-filter_complex_script", str(fc_script2.resolve())],
                           pip_current, str(output_path))

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
                flight_clips, width, height, fps)
            pip_fc = ";".join(pip_parts)
            fc_script = work_dir / "pip_fc.txt"
            fc_script.write_text(pip_fc, encoding="utf-8")
            _log(f"PiP composite (1 vid + {len(flight_clips)} flight clips)...")
            _ffmpeg_encode(str(video_path), [c.clip_path for c in sorted_clips],
                           ["-filter_complex_script", str(fc_script.resolve())],
                           pip_current, str(output_path))

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


def _ffmpeg_encode(
    main_input: str,
    extra_inputs: list[Path],
    fc_args: list[str],
    out_label: str,
    output_path: str,
    segment: tuple[float, float] | None = None,
) -> None:
    """Run ffmpeg with h264_nvenc. No CPU fallback (libx forbidden by user).

    When ``segment`` is set, ``-ss {start} -to {end}`` is applied as INPUT
    options on the main video so both video and audio streams are trimmed
    frame-accurately by ffmpeg's demuxer. Keyframe-aligned (input-side
    seeking is fast; visible round-boundary jumps are avoided by the
    round_offsets sidecar using actual per-round frames).

    Atomic write: ffmpeg renders to ``{output}.part`` and the file is
    renamed onto ``output_path`` only after a successful exit. A cancelled /
    crashed encode therefore leaves a stale ``.part`` (never the final name),
    so resume checks (``_overlay_output_valid``) cannot mistake a partial
    file for a complete one.
    """
    out_path = Path(output_path)
    tmp_path = out_path.with_name(out_path.name + ".part")
    tmp_path.unlink(missing_ok=True)
    cmd = ["ffmpeg", "-y"]
    if segment is not None:
        start_sec, end_sec = segment
        if start_sec > 0:
            cmd.extend(["-ss", f"{start_sec:.6f}"])
        cmd.extend(["-to", f"{end_sec:.6f}"])
    cmd.extend(["-i", main_input])
    for inp in extra_inputs:
        cmd.extend(["-i", str(inp)])
    cmd.extend([
        *fc_args, "-map", out_label, "-map", "0:a?", "-shortest",
        # Match raw concat quality (concat_rounds.py): cq 16 / p7
        "-c:v", "h264_nvenc", "-cq", "16", "-preset", "p7",
        "-profile:v", "high", "-pix_fmt", "yuv420p",
        "-color_range", "tv", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
        "-c:a", "aac", "-b:a", "256k",
        "-af", "asetpts=PTS-STARTPTS",
        "-movflags", "+faststart",
        "-g", "60", "-keyint_min", "60",
        "-f", "mp4", str(tmp_path),
    ])
    _log(f"  [ffmpeg] nvenc preset p7 cq 16 (match raw; no libx fallback)")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=21600)  # 6h
    if result.returncode != 0 or not tmp_path.is_file():
        _log(f"[ERROR] nvenc ffmpeg failed: rc={result.returncode}")
        _log(f"  stderr: {(result.stderr or '')[-400:]}")
        tmp_path.unlink(missing_ok=True)
        sys.exit(1)
    os.replace(tmp_path, out_path)


def _ffmpeg_segment_copy(
    video_path: Path,
    start_sec: float,
    end_sec: float,
    output_path: Path,
) -> None:
    """Stream-copy a video segment when no overlay applies to this batch.

    Fast path (no encode) used when a batch has zero key presses AND zero
    flight PiP clips — output is byte-identical (codec params) to the
    other batch-overlay-*.mp4 files so the final concat stream-copy works.
    """
    tmp_path = output_path.with_name(output_path.name + ".part")
    tmp_path.unlink(missing_ok=True)
    cmd = ["ffmpeg", "-y"]
    if start_sec > 0:
        cmd.extend(["-ss", f"{start_sec:.6f}"])
    cmd.extend(["-to", f"{end_sec:.6f}", "-i", str(video_path), "-c", "copy",
                "-movflags", "+faststart", "-f", "mp4", str(tmp_path)])
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if result.returncode != 0 or not tmp_path.is_file():
        _log(f"[ERROR] ffmpeg segment copy failed: rc={result.returncode}")
        _log(f"  stderr: {(result.stderr or '')[-400:]}")
        tmp_path.unlink(missing_ok=True)
        sys.exit(1)
    os.replace(tmp_path, output_path)


def _concat_overlay_batches(batch_files: list[Path], output_path: Path) -> None:
    """Concat batch-overlay-*.mp4 files via ffmpeg stream copy (no re-encode).

    Validates the merged file is non-empty. Raises ``SystemExit`` on ffmpeg
    failure. Stream copy requires all inputs to share codec params (same
    _ffmpeg_encode call produces all batches, so this holds).
    """
    if not batch_files:
        _log("[ERROR] no batch files to concat")
        sys.exit(1)
    with tempfile.TemporaryDirectory() as tmp:
        lst = Path(tmp) / "files.txt"
        with open(lst, "w", encoding="utf-8") as f:
            for bf in batch_files:
                f.write(f"file '{bf.resolve()}'\n")
        tmp_path = output_path.with_name(output_path.name + ".part")
        tmp_path.unlink(missing_ok=True)
        cmd = [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
            "-c", "copy", "-movflags", "+faststart", "-f", "mp4", str(tmp_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if result.returncode != 0 or not tmp_path.is_file():
            _log(f"[ERROR] ffmpeg batch concat failed: rc={result.returncode}")
            _log(f"  stderr: {(result.stderr or '')[-400:]}")
            tmp_path.unlink(missing_ok=True)
            sys.exit(1)
        os.replace(tmp_path, output_path)
    if not _overlay_output_valid(output_path):
        _log(f"[ERROR] concat output too small: {output_path}")
        sys.exit(1)


# -- CLI -----------------------------------------------------------------


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
    run_overlay(Path(args.video), Path(args.demo), args.steam_id, args.round, args.batches,
                util_cams_root=args.util_cams_root, work_dir=args.work_dir)


if __name__ == "__main__":
    main()
