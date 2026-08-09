"""Overlay a voice-activity shade over the POV team's scoreboard avatars.

Adds a dim shade over each of the POV team's 5 avatar boxes (the CS2 top
scoreboard strip). When a teammate talks, their box's shade fades OUT over
``--fade`` seconds (the full avatar "pops out"); when they stop, the shade
fades back IN. Enemy-team avatars are never touched.

Per-player voice activity comes from the demo's per-player Opus voice (the
same source ``mix_team_voice.py`` uses), aligned to the video timeline via the
concat step's ``combined.round_offsets.json`` sidecar. Player -> box mapping
uses the POV team's slot order (``parse_player_info`` row order) against the
avatar-box geometry in ``avatar_boxes.py``, keyed by the video's resolution.

FFmpeg does all video compositing; Python only computes per-player talk
timelines and encodes each box's shade as a tiny 1x1 RGBA control (one alpha
value per frame), which ffmpeg scales to the box and overlays. No frame-by-frame
video processing in Python.

Usage::

    python scripts/overlay/voice_shade.py \\
        --video <overlay video> --demo <demo.dem> --steam-id <pov steam64> \\
        --offsets renders/<...>/combined.round_offsets.json \\
        --out <output.mp4> [--fade 0.3] [--shade 0.55] [--pov-side right]

Idempotency marker ``<out>.voiceshade.json``; ``--force`` to redo.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(PROJECT_ROOT / "scripts" / "overlay"))
sys.path.insert(0, str(PROJECT_ROOT / "scripts" / "faceit"))

import numpy as np  # noqa: E402
from demoparser2 import DemoParser  # noqa: E402

from overlay._common import _log  # noqa: E402
from avatar_boxes import boxes_for_resolution  # noqa: E402
from mix_team_voice import (  # noqa: E402
    SAMPLE_RATE,
    FRAME_SAMPLES,
    _TICKRATE,
    decode_player_packets,
    detect_channels,
    load_offsets,
    load_team_map,
    load_voice,
    tick_to_time,
)

_TALK_RMS_THRESH = 0.01          # RMS above which a frame counts as "talking"
_TALK_MIN_SEGMENT = 0.12         # min talk-segment length (s) to keep
_TALK_GAP_MERGE = 0.10           # merge talk segments closer than this (s)


def _probe_video_info(video: Path) -> tuple[int, int, float, float]:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate,duration",
         "-of", "json", str(video)],
        capture_output=True, text=True,
    )
    d = json.loads(r.stdout)["streams"][0]
    w, h = int(d["width"]), int(d["height"])
    n, dn = d.get("r_frame_rate", "0/1").split("/")
    fps = float(n) / float(dn) if float(dn) else 0.0
    dur = float(d.get("duration") or 0.0)
    if dur <= 0:
        r2 = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
            capture_output=True, text=True)
        dur = float(r2.stdout.strip() or 0.0)
    return w, h, fps, dur


def _player_voice_pcm(demo: Path, offsets: dict, pov_team: int,
                      duration: float, tickrate: int) -> dict[str, np.ndarray]:
    """Return {steamid: video-timeline PCM buffer} for POV-team players."""
    rows = load_voice(demo)
    team_map = load_team_map(demo)
    by_player: dict[str, list[tuple[int, bytes]]] = {}
    for r in rows:
        if team_map.get(r["steamid"]) != pov_team:
            continue
        by_player.setdefault(r["steamid"], []).append((r["tick"], r["bytes"]))

    nbuf = int(duration * SAMPLE_RATE) + FRAME_SAMPLES + 1
    out: dict[str, np.ndarray] = {}
    for sid, packets in by_player.items():
        packets.sort(key=lambda t: t[0])
        channels = detect_channels(packets[0][1])
        decoded = decode_player_packets(packets, channels)
        buf = np.zeros(nbuf, dtype=np.float32)
        pos = 0
        for tick, pcm in decoded:
            t = tick_to_time(tick, offsets, tickrate)
            if t is not None:
                idx = int(round(t * SAMPLE_RATE))
                if idx > pos + int(0.020 * SAMPLE_RATE):
                    pos = idx  # real pause -> jump forward
            if pos >= len(buf):
                continue
            seg = pcm[:len(buf) - pos] if pos + len(pcm) > len(buf) else pcm
            buf[pos:pos + len(seg)] += seg
            pos += len(seg)
        out[sid] = buf
    return out


def _talk_segments(pcm: np.ndarray, fps: float) -> list[tuple[float, float]]:
    """Return [(start_sec, end_sec), ...] talk segments for one player's PCM."""
    if len(pcm) == 0:
        return []
    hop = max(1, int(SAMPLE_RATE / max(fps, 1)))
    n = len(pcm) // hop
    if n == 0:
        return []
    rms = np.sqrt((pcm[:n * hop].reshape(n, hop) ** 2).mean(axis=1))
    active = rms > _TALK_RMS_THRESH
    segs: list[tuple[int, int]] = []
    i = 0
    while i < n:
        if active[i]:
            j = i
            while j < n and active[j]:
                j += 1
            segs.append((i, j))
            i = j
        else:
            i += 1
    if not segs:
        return []
    merged = [segs[0]]
    for s, e in segs[1:]:
        if (s - merged[-1][1]) * (1 / fps) <= _TALK_GAP_MERGE:
            merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))
    out = []
    for s, e in merged:
        if (e - s) / fps >= _TALK_MIN_SEGMENT:
            out.append((s / fps, e / fps))
    return out


def _box_alpha_timeline(segments: list[tuple[float, float]], n_frames: int, fps: float,
                        fade: float, shade: float) -> np.ndarray:
    """Per-frame shade alpha (0=avatar visible, shade=dimmed) for one box.

    Default ``shade``. For each talk segment: fade OUT to 0 (reveal avatar) at
    the segment start, hold 0 while talking, fade back IN to ``shade`` at the
    segment end.
    """
    alpha = np.full(n_frames, shade, dtype=np.float64)
    fade_f = max(1, int(round(fade * fps)))
    for st, en in segments:
        s_frame = int(st * fps)
        e_frame = int(en * fps)
        # fade out (reveal) over [s_frame, s_frame+fade_f]
        lo = min(n_frames, s_frame)
        hi = min(n_frames, s_frame + fade_f)
        if lo < hi:
            alpha[lo:hi] = np.linspace(shade, 0.0, hi - lo)
        # held open (avatar fully visible) while talking
        held_lo = min(n_frames, s_frame + fade_f)
        held_hi = min(n_frames, e_frame)
        if held_lo < held_hi:
            alpha[held_lo:held_hi] = 0.0
        # fade back in (re-dim) over [e_frame, e_frame+fade_f]
        lo2 = min(n_frames, e_frame)
        hi2 = min(n_frames, e_frame + fade_f)
        if lo2 < hi2:
            alpha[lo2:hi2] = np.linspace(0.0, shade, hi2 - lo2)
    return alpha


def _map_boxes(demo: Path, pov_team: int, side: str, boxes: dict) -> dict[str, tuple[int, int, int, int]]:
    """Map each POV-team player to its avatar box rect {steamid: (x0,y0,x1,y1)}.

    Within a team, box position (left->right) equals parse_player_info slot
    order (verified against both donk and HeavyGod renders). The POV team
    block is the given ``side``.
    """
    info = DemoParser(str(demo)).parse_player_info()
    slot_order = [str(r["steamid"]) for _, r in info.iterrows()
                  if int(r["team_number"]) == pov_team]
    x_ranges = boxes.get(side.upper(), [])
    y0, y1 = boxes["y0"], boxes["y1"]
    out: dict[str, tuple[int, int, int, int]] = {}
    for i, sid in enumerate(slot_order):
        if i >= len(x_ranges):
            break
        x0, x1 = x_ranges[i]
        out[sid] = (x0, y0, x1, y1)
    return out


class VoiceShadeData:
    """Precomputed voice-shade geometry + per-box per-frame alpha timelines.

    This is the reusable output the standalone ``voice_shade.py`` CLI and the
    batched ``overlay_pov.py`` path both consume. ``alpha_frames`` has shape
    ``(n_boxes, n_frames)``; 0 = avatar fully revealed (talking), ``shade`` =
    dimmed (default).
    """

    def __init__(self, box_rects: list[tuple[int, int, int, int]],
                 alpha_frames: np.ndarray, fps: float, total_frames: int,
                 side: str, fade: float, shade: float):
        self.box_rects = box_rects
        self.alpha_frames = alpha_frames  # (n_boxes, n_frames) float 0..1
        self.fps = fps
        self.total_frames = total_frames
        self.side = side
        self.fade = fade
        self.shade = shade


def build_voice_shade_data(
    demo: Path,
    video: Path,
    steam_id: str,
    offsets: dict,
    fps: float,
    duration: float,
    *,
    native_res: str | None = None,
    pov_side: str = "right",
    fade: float = 0.3,
    shade: float = 0.55,
    tickrate: int = _TICKRATE,
) -> VoiceShadeData:
    """Compute POV-team avatar boxes + per-box shade alpha timelines.

    Does NOT run ffmpeg — the caller renders the overlays (standalone full-video
    pass, or sliced per batch in overlay_pov.py). Raises ``SystemExit`` if there
    is no voice activity (nothing to shade).
    """
    w, h, fps_, _dur = _probe_video_info(video)
    if fps <= 0:
        fps = fps_
    native_res = native_res or f"{w}x{h}"
    nw, nh = (int(x) for x in native_res.lower().split("x"))
    boxes = boxes_for_resolution(nw, nh)
    if boxes is None:
        _log(f"[ERROR] no avatar-box geometry for {nw}x{nh} in avatar_boxes.py")
        sys.exit(1)
    scale_x = w / nw
    scale_y = h / nh

    team_map = load_team_map(demo)
    pov_team = team_map.get(steam_id)
    if pov_team is None:
        _log(f"[ERROR] steam id {steam_id} not found in demo player info")
        sys.exit(1)
    side = pov_side.lower()

    pcms = _player_voice_pcm(demo, offsets, pov_team, duration, tickrate)
    box_map = _map_boxes(demo, pov_team, side, boxes)
    if not box_map:
        _log("[ERROR] could not map any POV player to a box")
        sys.exit(1)
    box_map = {sid: (int(round(x0 * scale_x)), int(round(y0 * scale_y)),
                     int(round(x1 * scale_x)), int(round(y1 * scale_y)))
               for sid, (x0, y0, x1, y1) in box_map.items()}

    n_frames = int(round(duration * fps))
    fade = max(0.05, fade)
    shade = max(0.0, min(1.0, shade))
    rects: list[tuple[int, int, int, int]] = []
    alpha_frames: list[np.ndarray] = []
    for sid, rect in sorted(box_map.items()):
        segs = _talk_segments(pcms.get(sid, np.zeros(0)), fps)
        alpha = _box_alpha_timeline(segs, n_frames, fps, fade, shade)
        rects.append(rect)
        alpha_frames.append(alpha)
    return VoiceShadeData(rects, np.array(alpha_frames), fps, n_frames,
                          side, fade, shade)


def _write_alpha_control(path: Path, alpha: np.ndarray) -> None:
    """Write a 1x1 RGBA control (one alpha per frame) for a box."""
    n_frames = len(alpha)
    rgba = np.zeros((n_frames, 1, 1, 4), dtype=np.uint8)
    rgba[:, 0, 0, 3] = (alpha * 255.0).astype(np.uint8)
    path.write_bytes(rgba.tobytes())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--video", required=True, help="input overlay video")
    ap.add_argument("--demo", required=True, help="FACEIT .dem path")
    ap.add_argument("--steam-id", required=True, help="POV player steam64")
    ap.add_argument("--offsets", required=True, help="combined.round_offsets.json")
    ap.add_argument("--out", required=True, help="output mp4")
    ap.add_argument("--pov-side", choices=["left", "right"], default="right",
                    help="scoreboard side of the POV team (default: right, "
                         "matches verified demos)")
    ap.add_argument("--fade", type=float, default=0.3, help="shade fade duration s")
    ap.add_argument("--shade", type=float, default=0.55,
                    help="shade opacity when not talking (0..1)")
    ap.add_argument("--native-res", default=None,
                    help="native render resolution WxH the avatar-box config is keyed to "
                         "(default: probe video res; set when the video was upscaled/stretched, "
                         "e.g. 1920x1080 for a 2560x1440 combined.mp4)")
    ap.add_argument("--tickrate", type=int, default=_TICKRATE)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    video = Path(args.video)
    demo = Path(args.demo)
    out = Path(args.out)
    marker = out.with_suffix(out.suffix + ".voiceshade.json")
    if marker.exists() and not args.force:
        print(f"[skip] {out.name} already has voice shade (--force to redo)")
        return

    w, h, fps, duration = _probe_video_info(video)
    if fps <= 0 or duration <= 0:
        _log("[ERROR] could not probe video fps/duration")
        sys.exit(1)
    _log(f"video {video.name}: {w}x{h} @ {fps:.2f}fps, {duration:.1f}s")

    offsets = load_offsets(Path(args.offsets))
    data = build_voice_shade_data(
        demo, video, args.steam_id, offsets, fps, duration,
        native_res=args.native_res, pov_side=args.pov_side,
        fade=args.fade, shade=args.shade, tickrate=args.tickrate,
    )
    _log(f"POV team boxes on side {data.side.upper()}: {len(data.box_rects)} boxes")

    n_frames = data.total_frames
    fade, shade = data.fade, data.shade
    overlay_inputs: list[Path] = []
    box_rects = data.box_rects
    tmpdir = Path(tempfile.mkdtemp(prefix="voiceshade_"))
    try:
        for bi, alpha in enumerate(data.alpha_frames):
            ctrl = tmpdir / f"box_{bi}.rgba"
            _write_alpha_control(ctrl, alpha)
            overlay_inputs.append(ctrl)

        _log(f"built {len(box_rects)} shade overlays over {n_frames} frames")

        # ---- ffmpeg filter graph -----------------------------------------
        # [0:v] main. Each box: scale its 1x1 RGBA control to box size,
        # overlay at box position.
        parts = ["[0:v]null[v0]"]
        cur = "v0"
        input_idx = 1
        for ctrl, rect in zip(overlay_inputs, box_rects):
            x0, y0, x1, y1 = rect
            bw, bh = x1 - x0 + 1, y1 - y0 + 1
            cur2 = f"{cur}o"
            parts.append(
                f"[{input_idx}:v]scale={bw}:{bh}:flags=bilinear[sc];"
                f"[{cur}][sc]overlay=x={x0}:y={y0}:shortest=1[{cur2}]"
            )
            cur = cur2
            input_idx += 1
        fc = ";".join(parts)

        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write(fc)
            script_path = f.name
        try:
            cmd = ["ffmpeg", "-y", "-i", str(video)]
            for ctrl in overlay_inputs:
                cmd += ["-f", "rawvideo", "-pix_fmt", "rgba", "-s", "1x1",
                        "-r", f"{fps:.3f}", "-i", str(ctrl)]
            cmd += [
                "-filter_complex_script", script_path,
                "-map", f"[{cur}]", "-map", "0:a?", "-shortest",
                "-c:v", "h264_nvenc", "-preset", "p7", "-b:v", "0", "-cq", "15",
                "-maxrate", "60M", "-bufsize", "120M",
                "-profile:v", "high", "-pix_fmt", "yuv420p",
                "-c:a", "copy", "-movflags", "+faststart",
                str(out),
            ]
            _log("Running ffmpeg voice-shade pass...")
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                _log(f"[ERROR] ffmpeg voice-shade failed: {result.stderr[-800:]}")
                sys.exit(1)
        finally:
            Path(script_path).unlink(missing_ok=True)
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

    marker.write_text(json.dumps({
        "video": str(video), "side": data.side, "fade": fade, "shade": shade,
        "boxes": len(box_rects),
        "resolution": f"{w}x{h}",
    }, indent=2), encoding="utf-8")
    _log(f"wrote {out.name} with voice shade")


if __name__ == "__main__":
    main()
