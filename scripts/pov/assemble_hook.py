"""Assemble rendered hook clips into one fast-paced, crossfaded cold open.

Reads ``hook_render.json`` (from ``render_hook.py``), joins the clips with
dissolves, scales to the POV's exact resolution/fps, and encodes with the
overlay final-export profile — so the later prepend is a plain stream copy.

The clip order is the timeline's edit order (climax last): the hook builds up
and ends on the best moment.

Usage:
    python scripts/pov/assemble_hook.py renders/hook-<stem>_<player>/hook_render.json
    python scripts/pov/assemble_hook.py <hook_render.json> --fade 0.2
    python scripts/pov/assemble_hook.py <hook_render.json> --pov-video youtube/<run>_overlay/video.mp4

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
sys.path.insert(0, str(_PROJECT_ROOT / "scripts"))
from _pathsetup import ensure  # noqa: E402

ensure()

from config import settings  # noqa: E402

FFMPEG = settings.ffmpeg_exe
FFPROBE = settings.ffprobe_exe

OUT_W = 2560
OUT_H = 1440
OUT_FPS = 60
FADE_DEFAULT = 0.25
MIN_CLIP_BYTES = 100_000

# Final-export profile: identical to overlay_encode.py so the hook can be
# prepended with `-c copy` (no re-encode of the main video).
NVENC_ARGS = [
    "-c:v", "h264_nvenc", "-preset", "p7", "-b:v", "0", "-cq", "15",
    "-maxrate", "60M", "-bufsize", "120M",
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


def crossfade_offsets(durations: list[float], fade: float) -> list[float]:
    """xfade start times for a chain of clips joined with ``fade`` dissolves."""
    offsets = []
    total = durations[0]
    for dur in durations[1:]:
        offsets.append(total - fade)
        total = total - fade + dur
    return offsets


def assemble(clips: list[Path], out_path: Path, fade: float = FADE_DEFAULT,
             width: int = OUT_W, height: int = OUT_H, fps: float = OUT_FPS) -> Path:
    if not clips:
        raise ValueError("no clips to assemble")
    if len(clips) == 1:
        fade = 0.0

    probes = [_probe(c) for c in clips]
    durs = []
    for c, (d, _a) in zip(clips, probes):
        if d <= 0:
            raise RuntimeError(f"could not probe duration: {c}")
        durs.append(d)

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

    parts: list[str] = []
    for i in range(len(clips)):
        parts.append(f"[{vid_idx[i]}:v]fps={fps:g},format=yuv420p,settb=AVTB[v{i}]")
        parts.append(f"[{aud_idx[i]}:a]aformat=sample_fmts=fltp:sample_rates=48000:"
                     f"channel_layouts=stereo,asetpts=PTS-STARTPTS[a{i}]")

    cur_v, cur_a = "v0", "a0"
    if fade > 0:
        offsets = crossfade_offsets(durs, fade)
        for i, off in enumerate(offsets, start=1):
            nv, na = f"vx{i}", f"ax{i}"
            parts.append(f"[{cur_v}][v{i}]xfade=transition=fade:duration={fade:g}:"
                         f"offset={off:.3f}[{nv}]")
            parts.append(f"[{cur_a}][a{i}]acrossfade=d={fade:g}[{na}]")
            cur_v, cur_a = nv, na

    parts.append(f"[{cur_v}]scale={width}:{height}:flags=spline,setsar=1,"
                 f"fps={fps:g}[outv]")
    total = sum(durs) - fade * max(len(clips) - 1, 0)
    parts.append(f"[{cur_a}]afade=t=out:st={max(total - 0.25, 0):.3f}:d=0.25[outa]")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".part.mp4")
    cmd += [
        "-filter_complex", ";".join(parts),
        "-map", "[outv]", "-map", "[outa]",
        *NVENC_ARGS, *AUDIO_ARGS,
        "-movflags", "+faststart", "-g", "60", "-keyint_min", "60",
        "-f", "mp4", str(tmp),
    ]
    print(f"  [ffmpeg] {len(clips)} clip(s), {len(durs)} durations, "
          f"fade {fade:g}s -> {total:.2f}s, {width}x{height}@{fps:g}")
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
    ap.add_argument("--fade", type=float, default=FADE_DEFAULT,
                    help=f"Crossfade duration in seconds (default: {FADE_DEFAULT})")
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
    assemble(clips, out, fade=args.fade, width=width, height=height, fps=fps)
    dur, _ = _probe(out)
    print(f"  [OK] {out} ({dur:.2f}s, {out.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
