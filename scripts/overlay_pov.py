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
import traceback
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
    DEMOPARSER_TICK_FIELDS,
)
from scripts.render.csdm import build_flight_command

# -- Constants -----------------------------------------------------------
TICKRATE = 64.0

PIP_WIDTH = 480
PIP_HEIGHT = 270
PIP_MARGIN = 8
PIP_MAX_SIMULTANEOUS = 4
PIP_GAP = 4

FLIGHT_DIR_NAME = "throw_flights"

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

    Prefers rounds.parquet from CS2UtilArchive.
    Falls back to round_start events from demoparser2.
    """
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

    # Fallback: demoparser2 round_start events
    from demoparser2 import DemoParser
    p = DemoParser(str(demo_path))
    events = p.parse_event("round_start")
    if events.empty:
        _log("  [rounds] No round_start events in demo")
        return {}
    all_event_ticks = sorted(int(t) for t in events["tick"])
    result = {}
    for i, t in enumerate(all_event_ticks):
        rn = i + 1
        end = all_event_ticks[i + 1] - 1 if i + 1 < len(all_event_ticks) else t + int(180 * TICKRATE)
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

    # 2) round_freeze_end events (one per round; matches 1:1 with round_num
    #    when sorted — but skips the last/OT round if no freeze).
    p = DemoParser(str(demo_path))
    freeze_ticks: list[int] = []
    death_ticks: list[int] = []
    ev = p.parse_events(["round_freeze_end", "player_death"])
    for name, df in ev:
        if name == "round_freeze_end" and not df.empty:
            freeze_ticks = sorted(int(t) for t in df["tick"])
        elif name == "player_death" and not df.empty:
            col = "user_steamid" if "user_steamid" in df.columns else None
            if col is not None:
                sid_str = str(steam_id)
                s = df[col].astype(str)
                # Steam IDs in demo events can be int64, str, or NaN — compare
                # purely on string form (NaN -> "nan") to be safe.
                df = df[s == sid_str]
            death_ticks = sorted(int(t) for t in df["tick"])

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
        if rn in death_by_round:
            d = death_by_round[rn]
            end = min(re + CSDM_TICK_MARGIN, d + CSDM_TICK_MARGIN)
        else:
            # Survived the round: CSDM adds a post-round buffer of CSDM_TICK_MARGIN
            # ticks (matches sequence filename tick span).
            end = re + CSDM_TICK_MARGIN
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
    # Use apply_jump_inference=False to avoid false positives from movement inference
    # (is_airborne transitions from step-offs, spawn platforms, etc.)
    # Jump detection relies on buttons bitmask + old_jump_pressed column instead.
    tick_states: dict[int, dict[str, int]] = {}
    for _, row in ticks_df.iterrows():
        tick = int(row["tick"])
        states, _ = overlay_tick_from_row(row, apply_jump_inference=False)
        tick_states[tick] = states

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
            # Match by entity ID substring in filename (orbit clips)
            # Format: throw_flight_orbit_<slug>_e{N}_s{M}.mp4 → matching e{N}:s{M}
            ent_part = tid.split(":")[1] if ":" in tid else ""
            matching = [m for m in mp4s if ent_part and ent_part in m.name]
            if not matching:
                # Fallback: victims clips share one .mp4 across one throw_id
                # (single throw_id directories).
                matching = mp4s if len(throw_map) == 1 else []
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
) -> list[PipClip]:
    """Render CSDM flight clips for each player throw.

    Uses existing orbit clips from utility_cams when available.
    Falls back to CS2UtilArchive's build_flight_command() -> csdm -> HLAE.
    Outputs 1920x1080 clips to output_dir/throw_flights/.
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

    # Resolve utility_cams directory near the video (persistent render cache)
    video_dir = video_path.parent if video_path else output_dir
    util_cams_root: Path | None = None
    p = video_dir
    for _ in range(5):
        cand = p / "utility_cams"
        if cand.is_dir():
            util_cams_root = cand
            break
        p = p.parent
    if util_cams_root is None:
        util_cams_root = video_dir / "utility_cams"
    util_cams_root.mkdir(parents=True, exist_ok=True)

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

    clips: list[PipClip] = []
    for idx, throw in enumerate(throws):
        throw_tick = int(throw["throw_tick"])
        land_tick = int(throw["land_tick"])
        det = throw.get("detonate_tick")
        if det is None or (isinstance(det, float) and (det != det or det == float('inf'))):
            detonate_tick = land_tick if land_tick else throw_tick
        else:
            detonate_tick = int(det)
        flight_ticks = int(throw["flight_ticks"])
        util_type = str(throw.get("util_type", "unknown")).lower()
        throw_round = int(throw.get("round_num", 0))

        # Frame mapping using per-round ranges
        if throw_round in round_frame_ranges:
            fs, fe = round_frame_ranges[throw_round]
            if throw_round in round_tick_ranges:
                rs, re = round_tick_ranges[throw_round]
                rf = (re - rs) or 1
                start_frac = (throw_tick - rs) / rf
                end_frac = (land_tick - rs) / rf
                start_frame = int(fs + start_frac * (fe - fs))
                end_frame = int(fs + end_frac * (fe - fs))
            else:
                start_frame = int(throw_tick * fps / TICKRATE)
                end_frame = int(land_tick * fps / TICKRATE)
        elif first_round_tick > 0:
            start_frame = int((throw_tick - first_round_tick) * fps / TICKRATE)
            end_frame = int((land_tick - first_round_tick) * fps / TICKRATE)
        else:
            start_frame = int(throw_tick * fps / TICKRATE)
            end_frame = int(land_tick * fps / TICKRATE)

        start_frame = max(0, start_frame)
        end_frame = min(frame_count - 1, end_frame)
        if start_frame >= end_frame:
            continue

        throw_id = str(throw.get("throw_id", ""))
        throw_id_slug = throw_id.replace(":", "_")
        clip_name = f"throw_flight_{throw_id_slug}"

        # Determine per-throw util render directory from manifest-style slug:
        #   utility_cams/unnamed/<map>_<util>_<side>_<relx>_<rely>_<relz>/<demo_id>/
        map_name = str(throw.get("map", "")) or demo_path.stem
        side = str(throw.get("thrower_side", "T"))[:1].upper()
        rel_x = int(round(float(throw.get("release_x", 0) or 0)))
        rel_y = int(round(float(throw.get("release_y", 0) or 0)))
        rel_z = int(round(float(throw.get("release_z", 0) or 0)))
        util_id = f"{map_name}:{util_type}:{side}:{rel_x}_{rel_y}_{rel_z}"
        util_slug = util_id.replace(":", "_")
        demo_id = str(throw.get("demo_id", demo_path.stem))
        render_dir = util_cams_root / "unnamed" / util_slug / demo_id
        render_dir.mkdir(parents=True, exist_ok=True)
        clip_path = render_dir / f"{clip_name}.mp4"

        # Use pre-rendered clip if available
        if throw_id in pre_rendered:
            src = pre_rendered[throw_id]
            if src.is_file() and src.stat().st_size > 100_000:
                _log(f"  [flight] Using pre-rendered {src.parent.parent.name}/{src.name}")
                clip_path = src
            else:
                _log(f"  [flight] Pre-rendered clip {src} too small, will re-render")

        if clip_path.is_file() and clip_path.stat().st_size > 100_000:
            _log(f"  [flight] {util_type} throw {idx} already rendered, skipping")
        else:
            _log(f"  [flight] Rendering {util_type} throw {idx} "
                 f"(t{throw_tick}, {flight_ticks} ticks, frames {start_frame}-{end_frame})...")

            job: dict[str, Any] = {
                "demo_path": str(demo_path.resolve()),
                "throw_tick": throw_tick,
                "detonate_tick": detonate_tick,
                "throw_id": throw_id,
                "output_name": clip_name,
            }
            try:
                cmd = build_flight_command(job, str(render_dir))
                _log(f"  [flight] CSDM: {' '.join(cmd[:4])} ...")
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=600,
                )
                stderr = (result.stderr or "") + (result.stdout or "")
                if "Steam is not running" in stderr:
                    _log(f"  [flight] FAILED: Steam is not running")
                    continue
                if "Raw files not found" in stderr:
                    _log(f"  [flight] FAILED: HLAE produced no video (check absolute --output)")
                    continue
                if result.returncode != 0 or not clip_path.is_file():
                    if not clip_path.is_file():
                        # CSDM may output sequence-*.mp4; find and rename
                        seqs = sorted(render_dir.glob("sequence-*.mp4"))
                        if seqs:
                            seq_path = seqs[0]
                            seq_path.rename(clip_path)
                            _log(f"  [flight] Renamed {seq_path.name} -> {clip_path.name}")
                        else:
                            _log(f"  [flight] No output file found")
                            continue

                # Write _throw_poses.json so scan finds it next time
                poses_file = render_dir / "_throw_poses.json"
                poses_data: dict[str, Any] = {
                    "1": {"pos": [rel_x, rel_y, rel_z]},
                    "_throws": {throw_id: {"pos": [rel_x, rel_y, rel_z]}},
                }
                poses_file.write_text(json.dumps(poses_data, indent=2), encoding="utf-8")
            except subprocess.TimeoutExpired:
                _log(f"  [flight] TIMEOUT rendering {util_type} throw {idx}")
                continue
            except Exception as exc:
                _log(f"  [flight] Error rendering {util_type} throw {idx}: {exc}")
                traceback.print_exc()
                continue

        if not clip_path.is_file() or clip_path.stat().st_size < 100_000:
            _log(f"  [flight] WARN: {clip_path.name} too small or missing, skipping")
            clip_path.unlink(missing_ok=True)
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
) -> tuple[list[str], str, list[PipClip]]:
    """Sort clips, assign stack rows, build PiP filter parts."""
    sorted_clips = sorted(flight_clips, key=lambda c: c.start_frame)
    active: list[PipClip] = []
    for clip in sorted_clips:
        active = [a for a in active if a.end_frame > clip.start_frame]
        clip.pip_index = min(len(active), PIP_MAX_SIMULTANEOUS - 1)
        active.append(clip)
        _log(f"  PiP: {clip.util_type} @ frames {clip.start_frame}-{clip.end_frame}, row {clip.pip_index}")

    pip_parts: list[str] = []
    pip_current = "[0:v]"
    for idx, clip in enumerate(sorted_clips):
        fc_part, tag = _build_pip_overlay(clip, pip_current, idx + 1, width, height, fps)
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
    x = PIP_MARGIN
    pip_y = height - PIP_MARGIN - PIP_HEIGHT - clip.pip_index * (PIP_HEIGHT + PIP_GAP)
    tag = f"pip{clip.pip_index}_{input_idx}"
    scaled_tag = f"pip_scaled_{input_idx}"
    start_seconds = clip.start_frame / fps

    parts = [
        f"[{input_idx}:v]"
        f"scale=w={PIP_WIDTH}:h={PIP_HEIGHT}:flags=lanczos,"
        f"setpts=PTS-STARTPTS+{start_seconds:.6f}/TB"
        f"[{scaled_tag}]",
        f"{current_label}[{scaled_tag}]"
        f"overlay=x={x}:y={pip_y}:eof_action=pass"
        f"[{tag}]",
    ]
    return ";".join(parts), tag


def run_overlay(
    video_path: Path,
    demo_path: Path,
    steam_id: str,
    round_num: int | None = None,
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
                re = int(b["round_end"])
                dur = float(b["duration_seconds"])
                per_round = dur / (re - rs + 1)
                for rn in range(rs, re + 1):
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
        round_tick_ranges = _load_pov_play_tick_ranges(demo_path, steam_id)
        if not round_tick_ranges:
            round_tick_ranges = full_round_tick_ranges  # fallback
            _log("[warn] POV play ranges unavailable — using full round ranges (overlay may be delayed)")
        else:
            # Override per-round video durations from the actual play tick span.
            # concat_rounds splits batch duration EVENLY across rounds, which is
            # wrong when rounds have different lengths (freeze + death vary).
            # The play range tick span / TICKRATE gives the real per-round video
            # duration CSDM recorded (matches sequence-*.mp4 filenames exactly).
            if round_offsets:
                span_total_old = sum(round_video_duration.values()) if round_video_duration else 0.0
                sorted_rns = sorted(round_offsets.keys())
                for rn in sorted_rns:
                    if rn in round_tick_ranges:
                        ps, pe = round_tick_ranges[rn]
                        round_video_duration[rn] = (pe - ps) / TICKRATE
                # round_offsets[N] = cumulative duration of earlier rounds in the
                # rendered batch. Without this, frames in round N+1 get mapped
                # back into round N (overlay appears "too early").
                cumulative = 0.0
                for rn in sorted_rns:
                    round_offsets[rn] = cumulative
                    cumulative += round_video_duration.get(rn, 0.0)
                span_total_new = cumulative
                _log(
                    f"  [sync] per-round durations recomputed from play tick spans: "
                    f"total {span_total_old:.2f}s -> {span_total_new:.2f}s"
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
    work_dir = Path(tempfile.mkdtemp())
    try:
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
        )
        if flight_clips:
            _log(f"Flight clips: {len(flight_clips)} ({time.time()-t3:.1f}s)")
        else:
            _log(f"No flight clips ({time.time()-t3:.1f}s)")

        # -- Final output: overlay.mp4 sidecar (never modify original) -----------
        output_path = video_path.with_suffix(".overlay.mp4")
        t4 = time.time()

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
        _log(f"Cleanup {work_dir.name}")
        shutil.rmtree(work_dir, ignore_errors=True)
    _log(f"Total: {time.time()-t_overall:.1f}s")


def _ffmpeg_encode(
    main_input: str,
    extra_inputs: list[Path],
    fc_args: list[str],
    out_label: str,
    output_path: str,
) -> None:
    """Run ffmpeg with h264_nvenc, fall back to libx264 on failure."""
    def _build_cmd(encoder: str, crf_or_cq: str, preset: str) -> list[str]:
        cmd = ["ffmpeg", "-y", "-i", main_input]
        for inp in extra_inputs:
            cmd.extend(["-i", str(inp)])
        if encoder == "h264_nvenc":
            cmd.extend([
                *fc_args, "-map", out_label, "-map", "0:a?", "-shortest",
                "-c:v", "h264_nvenc", "-cq", crf_or_cq, "-preset", preset,
                "-profile:v", "high", "-pix_fmt", "yuv420p",
                "-c:a", "copy", "-movflags", "+faststart", output_path,
            ])
        else:
            cmd.extend([
                *fc_args, "-map", out_label, "-map", "0:a?", "-shortest",
                "-c:v", "libx264", "-crf", crf_or_cq, "-preset", preset,
                "-pix_fmt", "yuv420p",
                "-c:a", "copy", output_path,
            ])
        return cmd

    try:
        cmd = _build_cmd("h264_nvenc", "18", "p7")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
        if result.returncode == 0 and Path(output_path).is_file():
            return
        _log(f"  nvenc failed (rc={result.returncode}), retrying libx264...")
        _log(f"  stderr: {(result.stderr or '')[-300:]}")
    except subprocess.TimeoutExpired:
        _log("  nvenc timed out, retrying libx264...")

    Path(output_path).unlink(missing_ok=True)
    cmd_sw = _build_cmd("libx264", "18", "fast")
    r2 = subprocess.run(cmd_sw, capture_output=True, text=True, timeout=7200)
    if r2.returncode != 0 or not Path(output_path).is_file():
        _log(f"[ERROR] ffmpeg failed: {(r2.stderr or '')[-300:]}")
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
    args = parser.parse_args()
    run_overlay(Path(args.video), Path(args.demo), args.steam_id, args.round)


if __name__ == "__main__":
    main()
