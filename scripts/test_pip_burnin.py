#!/usr/bin/env python3
"""
Isolated test for the PiP burn-in / utility throw flight overlay section of
`scripts/overlay_pov.py`.

Generates a black 2560x1440@60 video, picks 2-3 throw flight clips from an
existing render's `utility_cams/` tree, and composites them via the same
filter chain (`_build_pip_chain` + `_build_pip_overlay`) the live pipeline
uses. Outputs to `renders_test/pip_burnin_test.mp4` plus a few sampled PNGs
so you can eyeball whether the PiP actually shows up at bottom-left.

Run:
    python scripts/test_pip_burnin.py
    python scripts/test_pip_burnin.py --renders-dir renders/<pov-folder>
    python scripts/test_pip_burnin.py --clip <flight.mp4> ...   (repeatable)

No demo, no Steam, no csdm, no keyboard overlay. Just the PiP filter chain
on a black canvas. If the PiP clips don't appear HERE, the bug is in the
filter construction (or in the throw flight clip itself), not in the
keyboard overlay or the rest of the pipeline.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Reuse the live filter builders from the overlay script.
ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
sys.path.insert(0, str(ROOT))

# overlay_pov.py imports `from scripts.render.overlay_assets import ...`.
# That's CS2UtilArchive's `scripts` package, not ours. Add its parent
# to sys.path so `scripts` resolves to the right package.
import os
_CS2UTIL = ROOT.parent / "CS2UtilArchive"
_CS2UTIL_SCRIPTS = _CS2UTIL / "scripts"
if _CS2UTIL_SCRIPTS.is_dir():
    sys.path.insert(0, str(_CS2UTIL))
else:
    sys.exit(f"[FAIL] CS2UtilArchive not found at {_CS2UTIL}")

# Import overlay_pov directly (not via `scripts.overlay_pov`) -- otherwise
# `scripts` would resolve to OUR scripts/ dir and the `from scripts.render.*`
# inside overlay_pov.py would explode. Register the module in sys.modules
# so dataclass introspection (cls.__module__) works.
import importlib.util as _ilu
_spec = _ilu.spec_from_file_location("overlay_pov", SCRIPTS_DIR / "overlay_pov.py")
_overlay_pov = _ilu.module_from_spec(_spec)
sys.modules["overlay_pov"] = _overlay_pov
_spec.loader.exec_module(_overlay_pov)

PipClip = _overlay_pov.PipClip
PIP_WIDTH = _overlay_pov.PIP_WIDTH
PIP_HEIGHT = _overlay_pov.PIP_HEIGHT
PIP_MARGIN = _overlay_pov.PIP_MARGIN
PIP_GAP = _overlay_pov.PIP_GAP
PIP_MAX_SIMULTANEOUS = _overlay_pov.PIP_MAX_SIMULTANEOUS
_build_pip_chain = _overlay_pov._build_pip_chain
_build_pip_overlay = _overlay_pov._build_pip_overlay


# ---- ffmpeg helpers ----------------------------------------------------


def _run(cmd: list[str], label: str) -> None:
    print(f"\n[run] {label}")
    print("  $ " + " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout)
        print(r.stderr, file=sys.stderr)
        raise SystemExit(f"[FAIL] {label} (rc={r.returncode})")
    if r.stderr.strip():
        print("  stderr tail:", r.stderr.strip().splitlines()[-3:])


def make_black_video(path: Path, seconds: int, fps: int = 60,
                     width: int = 2560, height: int = 1440) -> None:
    """Generate a silent black video at the POV render resolution."""
    _run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:r={fps}",
        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
        "-t", str(seconds),
        "-c:v", "h264_nvenc", "-cq", "18", "-preset", "p7",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-shortest",
        str(path),
    ], f"generate black {width}x{height}@{fps} {seconds}s")


def probe_frames(path: Path) -> tuple[int, float]:
    """Return (frame_count, fps) for a video."""
    r = subprocess.run([
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=nb_frames,r_frame_rate",
        "-of", "json", str(path),
    ], capture_output=True, text=True, check=True)
    s = json.loads(r.stdout)["streams"][0]
    fc = int(s.get("nb_frames", 0))
    num, den = s["r_frame_rate"].split("/")
    fps = float(num) / float(den) if int(den) else 0.0
    return fc, fps


def composite_pip(black_video: Path, clips: list[PipClip],
                  width: int, height: int, output: Path,
                  work_dir: Path) -> None:
    """Build & run the PiP filter chain, writing output to `output`."""
    pip_parts, pip_current, sorted_clips = _build_pip_chain(
        clips, width, height)
    pip_fc = ";".join(pip_parts)

    fc_script = work_dir / "pip_fc.txt"
    fc_script.write_text(pip_fc, encoding="utf-8")
    print("\n[filter_complex]\n" + pip_fc)

    cmd = ["ffmpeg", "-y", "-i", str(black_video)]
    for clip in sorted_clips:
        cmd.extend(["-i", str(clip.clip_path)])
    cmd.extend([
        "-filter_complex_script", str(fc_script.resolve()),
        "-map", pip_current, "-map", "0:a?",
        "-c:v", "h264_nvenc", "-cq", "18", "-preset", "p7",
        "-pix_fmt", "yuv420p",
        "-c:a", "copy", "-shortest",
        str(output),
    ])
    _run(cmd, f"composite {len(sorted_clips)} PiP clips")


def sample_frames(video: Path, seconds_list: list[float], out_dir: Path) -> None:
    """Extract single frames at the given timestamps for visual inspection."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for sec in seconds_list:
        target = out_dir / f"frame_t{sec:05.2f}.png"
        _run([
            "ffmpeg", "-y", "-ss", f"{sec:.3f}",
            "-i", str(video), "-frames:v", "1",
            "-f", "image2", str(target),
        ], f"sample frame at t={sec:.2f}s -> {target.name}")


def check_pip_region(video: Path, width: int, height: int,
                     when: list[float], label: str,
                     source_video: Path | None = None) -> dict[str, Any]:
    """Crop the bottom-left PiP region at given timestamps, measure mean luma.

    If `source_video` is provided, also crops the same region from the source
    and computes the mean per-pixel difference. The PiP is visible iff:
      - black source (no source_video): pip_mean_yavg > 5
      - with source_video: pip diff > 10 (PiP is content not in source)

    The bottom-left of a POV video naturally contains the player model, so
    raw luma is misleading. Diff-vs-source is the reliable signal.
    """
    crop_pip = f"crop={PIP_WIDTH}:{PIP_HEIGHT}:{PIP_MARGIN}:{height - PIP_MARGIN - PIP_HEIGHT}"
    crop_ctrl = f"crop={PIP_WIDTH}:{PIP_HEIGHT}:{(width - PIP_WIDTH)//2}:50"

    try:
        from PIL import Image
    except ImportError:
        print(f"  [{label}] PIL not available, skipping region check")
        return {"label": label, "pip_mean_yavg": -1, "ctrl_mean_yavg": -1,
                "pip_diff": -1, "pip_visible": False}

    def _luma(png: Path) -> float:
        img = Image.open(png).convert("L")
        pixels = list(img.getdata())
        return sum(pixels) / len(pixels)

    def _mean_abs_diff(a_png: Path, b_png: Path) -> float:
        a = Image.open(a_png).convert("L")
        b = Image.open(b_png).convert("L")
        if a.size != b.size:
            return -1.0
        ap = list(a.getdata())
        bp = list(b.getdata())
        return sum(abs(x - y) for x, y in zip(ap, bp)) / len(ap)

    measurements: list[dict[str, Any]] = []
    for sec in when:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            pip_png = tdp / "pip.png"
            ctrl_png = tdp / "ctrl.png"
            for png, crop in [(pip_png, crop_pip), (ctrl_png, crop_ctrl)]:
                _run([
                    "ffmpeg", "-y", "-ss", f"{sec:.3f}",
                    "-i", str(video), "-frames:v", "1",
                    "-vf", crop,
                    "-f", "image2", str(png),
                ], f"crop {png.name} @ t={sec:.2f}s")
            pip_yavg = _luma(pip_png)
            ctrl_yavg = _luma(ctrl_png)

            pip_diff = -1.0
            if source_video and source_video.is_file():
                src_pip = tdp / "src_pip.png"
                _run([
                    "ffmpeg", "-y", "-ss", f"{sec:.3f}",
                    "-i", str(source_video), "-frames:v", "1",
                    "-vf", crop_pip,
                    "-f", "image2", str(src_pip),
                ], f"crop src_pip @ t={sec:.2f}s")
                pip_diff = _mean_abs_diff(pip_png, src_pip)

            measurements.append({
                "t": sec, "pip_yavg": pip_yavg, "ctrl_yavg": ctrl_yavg,
                "pip_diff": pip_diff,
            })
            extra = f" diff_vs_src={pip_diff:5.1f}" if pip_diff >= 0 else ""
            print(f"  [{label}] t={sec:5.2f}s pip_yavg={pip_yavg:6.2f} "
                  f"ctrl_yavg={ctrl_yavg:6.2f}{extra}")

    pip_yavgs = [m["pip_yavg"] for m in measurements]
    ctrl_yavgs = [m["ctrl_yavg"] for m in measurements]
    pip_diffs = [m["pip_diff"] for m in measurements if m["pip_diff"] >= 0]
    pip_mean = sum(pip_yavgs) / len(pip_yavgs)
    ctrl_mean = sum(ctrl_yavgs) / len(ctrl_yavgs)
    pip_diff_mean = sum(pip_diffs) / len(pip_diffs) if pip_diffs else -1

    # Decide visibility:
    if source_video and pip_diffs:
        # Compare overlay's pip region to source's same region.
        # If a PiP was drawn, the region will differ significantly from source.
        pip_visible = pip_diff_mean > 10.0
        verdict = f"diff_vs_src={pip_diff_mean:.2f}"
    else:
        # No source: black video means pip > 5 is sufficient.
        pip_visible = pip_mean > 5.0
        verdict = f"yavg={pip_mean:.2f}/255"

    print(f"\n  [{label}] pip region {verdict} "
          f"({'VISIBLE' if pip_visible else 'NOT VISIBLE'})")
    return {
        "label": label, "pip_mean_yavg": pip_mean,
        "ctrl_mean_yavg": ctrl_mean, "pip_diff": pip_diff_mean,
        "pip_visible": pip_visible,
    }


# ---- test driver -------------------------------------------------------


def find_default_clips(renders_dir: Path) -> list[Path]:
    """Pick 3 throw_flight_*.mp4 files spread across different util types
    so the stacking logic gets exercised. Falls back to whatever exists."""
    if not renders_dir.is_dir():
        return []
    all_clips = sorted(renders_dir.rglob("throw_flight_*.mp4"))
    if not all_clips:
        return []
    # Pick first clip from the first 3 different util folders so we get
    # a smoke, a fire, a flash, etc. (or whatever's there).
    seen_util: dict[str, Path] = {}
    for c in all_clips:
        # Path looks like .../unnamed/<util_id>/<demo_id>/<file>.mp4
        try:
            util_id = c.parts[c.parts.index("unnamed") + 1]
        except ValueError:
            util_id = c.parent.parent.name
        if util_id not in seen_util:
            seen_util[util_id] = c
        if len(seen_util) >= 3:
            break
    # Always keep the 3 best spread, or fall back to first 3.
    picked = list(seen_util.values())[:3] if len(seen_util) >= 3 else all_clips[:3]
    return picked


def build_pip_clips(flight_paths: list[Path], total_frames: int) -> list[PipClip]:
    """Wrap raw flight mp4s in PipClip objects with staggered start/end frames.

    Stagger them 1 second apart starting at frame 60 (1s into the black video)
    so the stacking logic (and the second/third PiP row) actually has a chance
    to overlap and exercise `pip_index` assignment.
    """
    out: list[PipClip] = []
    for i, p in enumerate(flight_paths):
        start = 60 + i * 60          # 60, 120, 180 ...
        end = start + 180            # ~3 seconds long
        end = min(end, total_frames - 1)
        out.append(PipClip(
            clip_path=p,
            start_frame=start,
            end_frame=end,
            util_type=p.parent.parent.name.split("_")[1] if "_" in p.parent.parent.name else "util",
        ))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--renders-dir",
                    default=str(ROOT / "renders" / "pov-furia-vs-falcons-m3-inferno_76561198041683378"),
                    help="POV renders folder containing utility_cams/")
    ap.add_argument("--clip", action="append", default=[],
                    help="Explicit throw_flight_*.mp4 (repeatable). Overrides --renders-dir.")
    ap.add_argument("--seconds", type=int, default=10,
                    help="Length of black test video in seconds")
    ap.add_argument("--out", default=str(ROOT / "renders_test" / "pip_burnin_test.mp4"))
    ap.add_argument("--width", type=int, default=2560)
    ap.add_argument("--height", type=int, default=1440)
    ap.add_argument("--fps", type=int, default=60)
    ap.add_argument("--debug-print", action="store_true",
                    help="Just print the filter_complex and exit (no render)")
    ap.add_argument("--check-overlay", default=None,
                    help="Also check a real combined.overlay.mp4 for PiP presence "
                         "(uses same crop region). Pass path to overlay.mp4.")
    ap.add_argument("--check-times", default="0.5,2.0,3.5,5.0",
                    help="Comma-sep timestamps (sec) to sample when checking --check-overlay")
    args = ap.parse_args()

    print(f"PIP constants: width={PIP_WIDTH} height={PIP_HEIGHT} "
          f"margin={PIP_MARGIN} gap={PIP_GAP} max={PIP_MAX_SIMULTANEOUS}")

    # Resolve clips
    if args.clip:
        flight_paths = [Path(p) for p in args.clip]
    else:
        flight_paths = find_default_clips(Path(args.renders_dir))

    if not flight_paths:
        sys.exit("[FAIL] no throw_flight_*.mp4 clips found. pass --clip <path>")
    for p in flight_paths:
        if not p.is_file():
            sys.exit(f"[FAIL] clip missing: {p}")
        print(f"  clip: {p}")

    # Build PipClip objects
    # Use a placeholder total_frames for the 1st pass; we'll re-stagger after
    # we know the actual black video length.
    work_dir = Path(tempfile.mkdtemp(prefix="pip_burnin_"))
    try:
        black_video = work_dir / "black.mp4"
        make_black_video(black_video, args.seconds, args.fps,
                         args.width, args.height)
        total_frames, fps = probe_frames(black_video)
        print(f"\nBlack video: {total_frames} frames @ {fps:.2f}fps")

        clips = build_pip_clips(flight_paths, total_frames)
        for c in clips:
            print(f"  PiP: util={c.util_type} frames={c.start_frame}-{c.end_frame} "
                  f"-> {c.clip_path.name}")

        # Print the filter without rendering
        if args.debug_print:
            pip_parts, pip_current, _ = _build_pip_chain(
                clips, args.width, args.height)
            print("\n[filter_complex]\n" + ";".join(pip_parts))
            print(f"\nfinal map label: {pip_current}")
            return

        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        composite_pip(black_video, clips, args.width, args.height, out, work_dir)
        print(f"\n[OK] wrote {out} ({out.stat().st_size/1024/1024:.1f} MB)")

        # Sample a few frames mid-clip so you can verify visually.
        sample_secs = []
        for c in clips:
            mid_frame = (c.start_frame + c.end_frame) // 2
            sample_secs.append(round(mid_frame / fps, 2))
        sample_secs.append(0.5)
        sample_secs = sorted(set(sample_secs))
        sample_frames(out, sample_secs, out.parent / "pip_burnin_samples")
        print(f"\nSamples: {out.parent / 'pip_burnin_samples'}")

        # Auto-check: did the PiP actually get drawn?
        check_secs = sorted({round((c.start_frame + 5) / fps, 2) for c in clips})
        test_verdict = check_pip_region(out, args.width, args.height,
                                          check_secs, "test")

        # If user passed --check-overlay, also probe the real combined.overlay.mp4
        if args.check_overlay:
            ov = Path(args.check_overlay)
            if not ov.is_file():
                print(f"\n[WARN] --check-overlay file not found: {ov}")
            else:
                ow, oh, ofps, ofc = _overlay_pov._probe_video_info(ov)
                check_secs_live = [float(s) for s in args.check_times.split(",") if s.strip()]
                live_verdict = check_pip_region(ov, ow, oh, check_secs_live, "live")

                print("\n" + "=" * 60)
                print("DIAGNOSIS")
                print("=" * 60)
                print(f"  test filter  -> pip_visible={test_verdict['pip_visible']} "
                      f"(yavg={test_verdict['pip_mean_yavg']:.2f})")
                print(f"  live overlay -> pip_visible={live_verdict['pip_visible']} "
                      f"(yavg={live_verdict['pip_mean_yavg']:.2f})")
                if test_verdict["pip_visible"] and not live_verdict["pip_visible"]:
                    print("\n  >>> The filter chain works (test draws the PiP).")
                    print("  >>> The live pipeline's PiP section is not producing output.")
                    print("  >>> Likely causes: empty flight_clips, bad frame mapping,")
                    print("      CSDM render failures, or thrown flight clips not found.")
                elif not test_verdict["pip_visible"]:
                    print("\n  >>> TEST ITSELF FAILED. PiP filter broken.")
                else:
                    print("\n  >>> Both have PiP. Test passed.")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
