"""Trim dead gaps (>= GAP_MIN seconds with no kill) out of rendered shorts.

Rule: for every consecutive kill pair whose gap >= GAP_MIN, cut the middle —
keep 2s after the previous kill, resume 5s before the next kill — and join
the segments with a crossfade (xfade + acrossfade).

Reads kill timestamps from the short's short_timeline.json (kill_ticks +
start_tick/end_tick at 64 tick), maps to video time via output duration.

Usage:
    python scripts/shorts/dead_gap_trim.py <short_dir or mp4> [--out NAME]
    python scripts/shorts/dead_gap_trim.py --dry-run <mp4>   # show cut plan

Output: <stem>.trimmed.mp4 next to the source (source never overwritten
unless --overwrite).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from _pathsetup import ensure  # noqa: F401

GAP_MIN = 30.0        # dead gap threshold (s)
KEEP_AFTER_KILL = 5.0 # keep N s after previous kill
RESUME_BEFORE_KILL = 10.0  # resume N s before next kill
FADE_DUR = 1.0        # crossfade (dissolve) duration at each join


def _ffprobe_duration(path: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    return float(r.stdout.strip())


def _kill_times(mp4: Path, tl_path: Path) -> tuple[list[float], float]:
    """Kill times in video-seconds + total video duration."""
    dur = _ffprobe_duration(mp4)
    tl = json.loads(tl_path.read_text())
    shorts = tl["shorts"]
    # Match by stem: shorts-N dir contains one timeline per short entry;
    # pick the entry whose derived filename matches the mp4 stem.
    stem = mp4.stem  # e.g. 4k_multikill-donk-t138645
    entry = None
    for s in shorts:
        cand = f"{s['short_type']}_{(s.get('pov_nick') or '')}-t{s['start_tick']}"
        if cand == stem:
            entry = s
            break
    if entry is None:
        # fall back: single-short timelines
        if len(shorts) == 1:
            entry = shorts[0]
        else:
            raise SystemExit(f"cannot match {mp4.name} to timeline entries")
    tickrate = 64.0
    t0 = entry["start_tick"] / tickrate
    times = [k / tickrate - t0 for k in entry.get("kill_ticks") or []]
    return sorted(times), dur


def plan_cuts(kills: list[float], dur: float) -> list[tuple[float, float]]:
    """Return list of (cut_start, cut_end) spans to remove."""
    cuts: list[tuple[float, float]] = []
    for a, b in zip(kills, kills[1:]):
        gap = b - a
        if gap < GAP_MIN:
            continue
        cs = min(a + KEEP_AFTER_KILL, dur)
        ce = max(b - RESUME_BEFORE_KILL, cs)
        cuts.append((cs, ce))
    return cuts


def _merge_adjacent(cuts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Merge cuts closer than 2*FADE_DUR so fade windows never overlap."""
    merged: list[list[float]] = []
    for cs, ce in cuts:
        if merged and cs - merged[-1][1] < 2 * FADE_DUR:
            merged[-1][1] = ce
        else:
            merged.append([cs, ce])
    return [(a, b) for a, b in merged]


def trim_video(mp4: Path, cuts: list[tuple[float, float]], out: Path) -> None:
    """Cut spans and join remaining segments with dissolves (MoviePy).

    Same pattern as render_intro.py: subclip each segment, CrossFadeIn/Out
    (+ audio fades), then concatenate with negative padding so the clips
    overlap by FADE_DUR and the mask crossfade dissolves one into the next.
    """
    from moviepy import VideoFileClip, concatenate_videoclips
    from moviepy import afx, vfx

    dur = _ffprobe_duration(mp4)
    seg_bounds: list[tuple[float, float]] = []
    prev = 0.0
    for cs, ce in cuts:
        seg_bounds.append((prev, cs))
        prev = ce
    seg_bounds.append((prev, dur))
    seg_bounds = [(a, b) for a, b in seg_bounds if b - a > FADE_DUR * 2]
    if len(seg_bounds) <= 1:
        raise SystemExit("nothing left to join — refusing to render")

    src = VideoFileClip(str(mp4))
    clips = []
    n = len(seg_bounds)
    for i, (a, b) in enumerate(seg_bounds):
        sub = src.subclipped(a, b)
        fade_in = FADE_DUR if i > 0 else 0.0
        fade_out = FADE_DUR if i < n - 1 else 0.0
        effs = []
        if fade_in:
            effs.append(vfx.CrossFadeIn(fade_in))
        if fade_out:
            effs.append(vfx.CrossFadeOut(fade_out))
        if effs:
            sub = sub.with_effects(effs)
        if sub.audio is not None and (fade_in or fade_out):
            aeffs = []
            if fade_in:
                aeffs.append(afx.AudioFadeIn(fade_in))
            if fade_out:
                aeffs.append(afx.AudioFadeOut(fade_out))
            sub = sub.with_audio(sub.audio.with_effects(aeffs))
        clips.append(sub)

    joined = concatenate_videoclips(
        clips, method="compose", padding=-FADE_DUR, bg_color=(0, 0, 0),
    )
    print(f"  [moviepy] {n} segment(s), {joined.duration:.1f}s joined")
    joined.write_videofile(
        str(out), codec="libx264", audio_codec="aac",
        bitrate="12000k", preset="slow", threads=8, logger=None,
    )
    src.close()
    print(f"  [OK] {out.name}: {_ffprobe_duration(out):.1f}s (was {dur:.1f}s)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("target", help="short mp4 or its directory")
    ap.add_argument("--out", default=None, help="output path (default <stem>.trimmed.mp4)")
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    tgt = Path(args.target)
    if tgt.is_dir():
        mp4s = [p for p in tgt.glob("*.mp4")
                if not p.stem.endswith(".trimmed") and p.stat().st_size > 1_000_000]
        if len(mp4s) != 1:
            raise SystemExit(f"expected exactly one mp4 in {tgt}, found {len(mp4s)}")
        mp4 = mp4s[0]
    else:
        mp4 = tgt
    tl = mp4.parent / "short_timeline.json"
    if not tl.exists():
        raise SystemExit(f"no short_timeline.json next to {mp4}")

    kills, dur = _kill_times(mp4, tl)
    print(f"kills @ {[f'{k:.1f}' for k in kills]} | video {dur:.1f}s")
    cuts = _merge_adjacent(plan_cuts(kills, dur))
    if not cuts:
        print("no dead gaps >= %.0fs — nothing to trim" % GAP_MIN)
        return 0
    saved = sum(b - a for a, b in cuts)
    print(f"cut plan: {len(cuts)} gap(s), removing {saved:.1f}s "
          f"-> ~{dur - saved:.1f}s final")
    for cs, ce in cuts:
        print(f"  cut [{cs:.1f}s .. {ce:.1f}s]")
    if args.dry_run:
        return 0

    out = Path(args.out) if args.out else mp4.with_name(mp4.stem + ".trimmed.mp4")
    if out.exists() and not args.overwrite:
        raise SystemExit(f"{out} exists (use --overwrite)")
    trim_video(mp4, cuts, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
