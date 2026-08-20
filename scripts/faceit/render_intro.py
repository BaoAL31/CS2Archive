"""Render an Intro Timeline into a 16:9 2560x1440 highlight intro.

Reads ``intro_timeline.json`` (from ``build_intro_timeline.py``), renders the
picked moment via CSDM at the player's native capture resolution, then edits
with MoviePy:

  - Crossfade transitions wherever 15s+ pass with no kill — the dead middle is
    dropped (short buffer kept after the previous kill and before the next),
    and the kept shots dissolve into each other (no dip to black).
  - No title card — just the highlight.
  - The whole intro is capped at 60s (footage keeps its climactic ending).
  - Final encode 2560x1440@60 h264_nvenc CQ 15 (same export profile as the
    overlay final-export / intro_prepend).

Usage:
    python scripts/faceit/render_intro.py renders/hl-<stem>/intro/intro_timeline.json
    python scripts/faceit/render_intro.py <timeline.json> --segment <video.mp4>   # skip CSDM
    python scripts/faceit/render_intro.py <timeline.json> --out <custom>.mp4

Output:
    <out>.mp4   (defaults to the timeline's intro/ dir; segments/ alongside)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT / "scripts"))
from _pathsetup import ensure  # noqa: E402

ensure()

from shorts.render_shorts import (  # noqa: E402
    _build_csdm_config,
    _find_sequence_files,
    _resolve_player_resolution,
    _run_csdm,
)

OUT_WIDTH = 2560
OUT_HEIGHT = 1440
MAX_TOTAL = 60.0
FADE = 0.5
GAP_SECONDS = 15.0
KILL_PRE = 2.0
KILL_POST = 2.5
HEAD_LEAD = 4.0
TAIL_LEAD = 3.0


def _dbg(label: str, msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"  [{ts}] [{label}] {msg}", flush=True)


def fade_cut_plan(kill_secs: list[float], dur: float) -> list[tuple[float, float]]:
    """Plan footage sub-clips as (start, end) segments.

    Any stretch of >= GAP_SECONDS between consecutive kills is dropped (dead
    middle removed, small buffer kept on each side) — every internal boundary
    therefore becomes a crossfade in the assembler. Head/tail are also trimmed
    when the lead-in / out-run is that long.
    """
    kills = sorted(max(0.0, min(k, dur)) for k in kill_secs)

    if not kills:
        return [(0.0, dur)]

    head = 0.0
    if kills[0] >= GAP_SECONDS:
        head = max(0.0, kills[0] - HEAD_LEAD)
    tail = dur
    if dur - kills[-1] >= GAP_SECONDS:
        tail = min(dur, kills[-1] + TAIL_LEAD)

    segs: list[tuple[float, float]] = []
    cursor = head
    prev = kills[0]
    for k in kills[1:]:
        if k - prev >= GAP_SECONDS:
            seg_end = min(prev + KILL_POST, k)
            if seg_end - cursor >= 0.5:
                segs.append((cursor, seg_end))
            cursor = max(prev + KILL_POST, k - KILL_PRE)
        prev = k
    if tail - cursor >= 0.25:
        segs.append((cursor, tail))
    return segs


def _apply_transitions(clip, fade_in: float, fade_out: float):
    """Dissolve-style transitions: mask crossfade + audio fade (no black dip)."""
    from moviepy import afx, vfx

    if fade_in or fade_out:
        effs = []
        if fade_in:
            effs.append(vfx.CrossFadeIn(fade_in))
        if fade_out:
            effs.append(vfx.CrossFadeOut(fade_out))
        clip = clip.with_effects(effs)
        if clip.audio is not None:
            aeffs = []
            if fade_in:
                aeffs.append(afx.AudioFadeIn(fade_in))
            if fade_out:
                aeffs.append(afx.AudioFadeOut(fade_out))
            clip = clip.with_audio(clip.audio.with_effects(aeffs))
    return clip


def _scale_to_canvas(clip, src_w: int, src_h: int, scaling: str,
                     width: int = OUT_WIDTH, height: int = OUT_HEIGHT):
    from moviepy import ColorClip, CompositeVideoClip

    src_aspect = src_w / src_h
    target = width / height
    if scaling == "Stretched" or abs(src_aspect - target) < 0.02:
        return clip.resized(new_size=(width, height))

    scaled = clip.resized(height=height)
    if abs(scaled.w - width) < 2:
        return scaled
    if scaled.w > width:
        return scaled.cropped(
            x_center=scaled.w / 2, y_center=scaled.h / 2, width=width, height=height,
        )
    bg = ColorClip(size=(width, height), color=(0, 0, 0)).with_duration(clip.duration)
    return CompositeVideoClip([bg, scaled.with_position("center")])


def assemble_intro(timeline: dict, segment: Path, out_path: Path,
                   logger=None) -> dict:
    from moviepy import VideoFileClip, concatenate_videoclips

    picked = timeline["picked"]
    src_w, src_h, scaling = _resolve_player_resolution(picked["pov_steam_id"])

    video = VideoFileClip(str(segment))
    dur = video.duration
    span = max(int(picked["end_tick"]) - int(picked["start_tick"]), 1)
    kills = [
        (int(k) - int(picked["start_tick"])) / span * dur
        for k in picked.get("kill_ticks", [])
    ]

    plan = fade_cut_plan(kills, dur)
    _dbg("edit", f"{len(plan)} segment(s), {dur:.1f}s source")

    clips = []
    for i, (a, b) in enumerate(plan):
        sub = video.subclipped(a, b)
        sub = _apply_transitions(
            sub,
            fade_in=FADE if i > 0 else 0.0,
            fade_out=FADE if i < len(plan) - 1 else 0.0,
        )
        clips.append(sub)

    # Negative padding overlaps the clips by FADE so the mask crossfades
    # (CrossFadeIn/Out above) dissolve one shot into the next.
    footage = concatenate_videoclips(
        clips, method="compose", padding=-FADE, bg_color=(0, 0, 0),
    )
    footage = _scale_to_canvas(footage, src_w, src_h, scaling)
    _dbg("edit", f"footage {footage.duration:.1f}s (post crossfade)")

    if footage.duration > MAX_TOTAL:
        footage = footage.subclipped(footage.duration - MAX_TOTAL, footage.duration)
        _dbg("edit", f"footage capped to {footage.duration:.1f}s (keeps ending)")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    final = footage
    final.write_videofile(
        str(out_path),
        fps=60,
        codec="h264_nvenc",
        preset="p7",
        ffmpeg_params=["-cq", "15", "-b:v", "0", "-profile:v", "high",
                       "-level", "5.1"],
        audio_codec="aac",
        audio_bitrate="256k",
        pixel_format="yuv420p",
        logger=logger,
    )
    video.close()
    return {"output": str(out_path), "duration": final.duration}


def render_intro(timeline_path: Path, out_path: Path | None = None,
                 segment_override: Path | None = None, force: bool = False) -> int:
    timeline = json.loads(Path(timeline_path).read_text(encoding="utf-8"))
    picked = timeline.get("picked")
    if not picked:
        print(f"[OK] nothing picked: {timeline.get('reason', 'n/a')}")
        return 0

    demo = Path(timeline["demo_path"])
    run_dir = Path(timeline_path).parent
    out = out_path or (run_dir / "intro.mp4")

    if not force and out.is_file() and out.stat().st_size >= 1024 * 1024:
        print(f"[OK] already rendered: {out} ({out.stat().st_size} bytes)")
        return 0

    segment = segment_override
    if segment is None:
        src_w, src_h, _ = _resolve_player_resolution(picked["pov_steam_id"])
        segments_dir = run_dir / "segments"
        segments_dir.mkdir(parents=True, exist_ok=True)
        existing = _find_sequence_files(segments_dir, 1)
        if not existing:
            _dbg("csdm", f"rendering {picked['label']} @ {src_w}x{src_h} "
                         f"tick {picked['start_tick']}->{picked['end_tick']}")
            cfg = _build_csdm_config(
                [picked], demo, segments_dir, src_width=src_w, src_height=src_h,
            )
            cfg_path = run_dir / "csdm_config.json"
            cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
            rc = _run_csdm(cfg_path)
            if rc != 0:
                print(f"[ERR] CSDM render failed (exit {rc})", file=sys.stderr)
                return 1
            existing = _find_sequence_files(segments_dir, 1)
        if not existing:
            print("[ERR] no CSDM segment produced", file=sys.stderr)
            return 1
        segment = existing[0]
        _dbg("csdm", f"segment: {segment}")

    if not segment.is_file():
        print(f"[ERR] segment not found: {segment}", file=sys.stderr)
        return 1

    try:
        result = assemble_intro(timeline, segment, out)
        print(f"[OK] intro -> {result['output']} ({result['duration']:.1f}s)")
        return 0
    finally:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description="Render an intro timeline to 16:9 mp4")
    ap.add_argument("timeline", type=Path, help="intro_timeline.json path")
    ap.add_argument("--out", "-o", type=Path, default=None, help="Output mp4 path")
    ap.add_argument("--segment", type=Path, default=None,
                    help="Use an existing rendered segment (skip CSDM)")
    ap.add_argument("--force", action="store_true", help="Re-render even if output exists")
    args = ap.parse_args()

    if not args.timeline.is_file():
        print(f"[ERR] timeline not found: {args.timeline}", file=sys.stderr)
        return 1
    return render_intro(args.timeline, out_path=args.out,
                        segment_override=args.segment, force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())