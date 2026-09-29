"""Assemble rendered hook clips into one fast-paced, jump-cut cold open.

Reads ``hook_render.json`` (from ``render_hook.py``), joins the clips with
hard cuts, fades the tail to black (the dip-to-black into the intro card is
completed by the intro head's matching fade-in, so the prepend itself stays a
plain stream copy), scales to the POV's exact resolution/fps, and encodes with
the overlay final-export profile — so the later prepend is a plain stream copy.

The clip order is the timeline's edit order (climax last): the hook builds up
and ends on the best moment.

Usage:
    python cs2archive/pov/assemble_hook.py renders/pov-<stem>_<nick>/hook/hook_render.json
    python cs2archive/pov/assemble_hook.py <hook_render.json> --end-fade 0.5
    python cs2archive/pov/assemble_hook.py <hook_render.json> --pov-video youtube/<run>_overlay/video.mp4

Output:
    <hook dir>/hook.mp4
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


from cs2archive.config import settings  # noqa: E402
from cs2archive import encode

FFMPEG = settings.ffmpeg_exe
FFPROBE = settings.ffprobe_exe

OUT_W = 2560
OUT_H = 1440
OUT_FPS = 60
END_FADE_DEFAULT = 0.5
MIN_CLIP_BYTES = 100_000

# Final-export profile: identical to overlay_encode.py so the hook can be
# prepended with `-c copy` (no re-encode of the main video).
NVENC_ARGS = [
    *encode.codec_args(encode.FINAL),
    "-profile:v", "high", "-pix_fmt", "yuv420p",
    "-color_range", "tv", "-colorspace", "bt709",
    "-color_primaries", "bt709", "-color_trc", "bt709",
]
AUDIO_ARGS = ["-c:a", "aac", "-b:a", "256k"]


def _probe(path: Path) -> tuple[float, bool]:
    """(duration_seconds, has_audio) for a clip."""
    r = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries",
         "format=duration:stream=codec_type", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        return 0.0, False
    data = json.loads(r.stdout or "{}")
    try:
        dur = float(data.get("format", {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        dur = 0.0
    has_audio = any(s.get("codec_type") == "audio"
                    for s in data.get("streams", []))
    return dur, has_audio


def _probe_video(path: Path) -> tuple[int, int, float]:
    r = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height,r_frame_rate", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        return OUT_W, OUT_H, float(OUT_FPS)
    st = (json.loads(r.stdout or "{}").get("streams") or [{}])[0]
    num, _, den = str(st.get("r_frame_rate", "60/1")).partition("/")
    try:
        fps = float(num) / float(den or 1)
    except (TypeError, ValueError, ZeroDivisionError):
        fps = float(OUT_FPS)
    return int(st.get("width") or OUT_W), int(st.get("height") or OUT_H), fps


def build_hook_filter(vid_idx: list[int], aud_idx: list[int], end_fade: float,
                      total: float, width: int, height: int, fps: float) -> str:
    """filter_complex joining clips with hard cuts plus a tail fade to black.

    ``vid_idx``/``aud_idx`` are the ffmpeg input indexes carrying each clip's
    video/audio. Clips join via ``concat`` (jump cuts — no dissolves), then the
    tail fades to black over ``end_fade`` seconds so the hook meets the intro
    card's fade-in as a dip-to-black across the stream-copy cut.
    """
    n = len(vid_idx)
    parts: list[str] = []
    for i, (v, a) in enumerate(zip(vid_idx, aud_idx)):
        parts.append(f"[{v}:v]fps={fps:g},format=yuv420p,settb=AVTB[v{i}]")
        parts.append(f"[{a}:a]aformat=sample_fmts=fltp:sample_rates=48000:"
                     f"channel_layouts=stereo,asetpts=PTS-STARTPTS[a{i}]")

    v_chain = "".join(f"[v{i}][a{i}]" for i in range(n))
    parts.append(f"{v_chain}concat=n={n}:v=1:a=1[jv][ja]")

    vtail = f"[jv]scale={width}:{height}:flags=spline,setsar=1,fps={fps:g}"
    if end_fade > 0:
        fade_start = max(total - end_fade, 0.0)
        vtail += f",fade=t=out:st={fade_start:.3f}:d={end_fade:g}"
    parts.append(f"{vtail}[outv]")
    if end_fade > 0:
        fade_start = max(total - end_fade, 0.0)
        parts.append(f"[ja]afade=t=out:st={fade_start:.3f}:d={end_fade:g}[outa]")
    else:
        parts.append("[ja]anull[outa]")
    return ";".join(parts)


def assemble(clips: list[Path], out_path: Path, end_fade: float = END_FADE_DEFAULT,
             width: int = OUT_W, height: int = OUT_H, fps: float = OUT_FPS) -> Path:
    if not clips:
        raise ValueError("no clips to assemble")

    probes = [_probe(c) for c in clips]
    durs = []
    for c, (d, _a) in zip(clips, probes):
        if d <= 0:
            raise RuntimeError(f"could not probe duration: {c}")
        durs.append(d)
    total = sum(durs)
    if end_fade >= total:
        end_fade = max(total / 2.0, 0.0)

    # Each clip contributes one video input plus one audio input (its real
    # audio, or a silent filler when the clip has none).
    cmd: list[str] = [FFMPEG, "-y"]
    vid_idx: list[int] = []
    aud_idx: list[int] = []
    next_idx = 0
    for clip, (dur, has_audio) in zip(clips, probes):
        cmd += ["-i", str(clip)]
        vid_idx.append(next_idx)
        next_idx += 1
        if has_audio:
            aud_idx.append(vid_idx[-1])
        else:
            cmd += ["-f", "lavfi", "-t", f"{dur:.3f}",
                    "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
            aud_idx.append(next_idx)
            next_idx += 1

    fc = build_hook_filter(vid_idx, aud_idx, end_fade, total, width, height, fps)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".part.mp4")
    cmd += [
        "-filter_complex", fc,
        "-map", "[outv]", "-map", "[outa]",
        *NVENC_ARGS, *AUDIO_ARGS,
        "-movflags", "+faststart", "-g", "60", "-keyint_min", "60",
        "-f", "mp4", str(tmp),
    ]
    print(f"  [ffmpeg] {len(clips)} clip(s) jump-cut, end-fade {end_fade:g}s "
          f"-> {total:.2f}s, {width}x{height}@{fps:g}")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
    if r.returncode != 0 or not tmp.is_file():
        print((r.stderr or "")[-4000:])
        tmp.unlink(missing_ok=True)
        raise SystemExit("[ERROR] hook assembly ffmpeg failed")
    tmp.replace(out_path)
    return out_path


def main() -> int:
    ap = argparse.ArgumentParser(description="Assemble hook clips into hook.mp4")
    ap.add_argument("render_json", type=Path, help="hook_render.json")
    ap.add_argument("--out", type=Path, default=None,
                    help="Output mp4 (default: <hook dir>/hook.mp4)")
    ap.add_argument("--end-fade", type=float, default=END_FADE_DEFAULT,
                    help="Fade-to-black duration at the hook tail in seconds "
                         f"(default: {END_FADE_DEFAULT}; meets the intro head's "
                         "fade-in as a dip-to-black). Clips join with jump cuts.")
    ap.add_argument("--pov-video", type=Path, default=None,
                    help="Finished POV video — output resolution/fps are copied "
                         "from it so the prepend stays a stream copy")
    args = ap.parse_args()

    if not args.render_json.is_file():
        print(f"[ERR] not found: {args.render_json}", file=sys.stderr)
        return 1

    payload = json.loads(args.render_json.read_text(encoding="utf-8"))
    clips = []
    for item in payload.get("clips") or []:
        p = Path(item["segment"])
        if not p.is_file() or p.stat().st_size < MIN_CLIP_BYTES:
            print(f"[WARN] skipping missing/tiny clip: {p}", file=sys.stderr)
            continue
        clips.append(p)
    if not clips:
        print("[ERR] no usable clips in hook_render.json", file=sys.stderr)
        return 1

    width, height, fps = OUT_W, OUT_H, float(OUT_FPS)
    if args.pov_video and args.pov_video.is_file():
        width, height, fps = _probe_video(args.pov_video)
        print(f"  [pov] {args.pov_video.name}: {width}x{height} @ {fps:g}fps")

    out = args.out or (args.render_json.resolve().parent / "hook.mp4")
    assemble(clips, out, end_fade=args.end_fade, width=width, height=height, fps=fps)
    dur, _ = _probe(out)
    print(f"  [OK] {out} ({dur:.2f}s, {out.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
