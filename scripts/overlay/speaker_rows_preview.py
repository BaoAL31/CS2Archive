"""PREVIEW-ONLY: burn lower-left speaking-player rows onto a rendered round clip.

Visual confirmation of the "speaker HUD" direction (Siez-style rows above the
money display): speaker icon + circular avatar + bold name, one row per active
teammate, fade in/out at talk-segment boundaries. Reuses the production voice
machinery (raw packet activity -> talk segments), NOT decoded-PCM RMS.

This is a standalone preview tool for the research report
(docs/research/cs2-demo-voice-primary-sources.md); it is NOT wired into the
pipeline. If the look is approved, the segment logic gets promoted into the
overlay pass with real tick alignment.

Usage:
    python scripts/overlay/speaker_rows_preview.py \\
        --video renders/<...>/round-001-tick-<A>-to-<B>.mp4 \\
        --demo <demo.dem> --steam-id <POV steam64> \\
        --ticks <A>,<B> --out preview.mp4

--ticks is the round window printed in the clip filename (CSDM sequence
ticks). Voice packets are mapped proportionally: the clip is the compressed
render of ticks [A,B], so clip-time = (tick - A) / (B - A) * clip_duration —
same proportional mapping as mix_team_voice.tick_to_time with a single-round
offsets dict.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(PROJECT_ROOT / "scripts" / "overlay"))
sys.path.insert(0, str(PROJECT_ROOT / "scripts" / "faceit"))

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw, ImageFilter, ImageFont  # noqa: E402
from demoparser2 import DemoParser  # noqa: E402

from mix_team_voice import (  # noqa: E402
    _TICKRATE,
    load_team_map,
)
from voice_shade import _player_talk_segments  # noqa: E402

FONT_PATH = PROJECT_ROOT / "assets" / "fonts" / "Montserrat-Bold.ttf"

# Strip geometry — designed at 2560x1440, scaled to the actual video size
ROW_H_D, ROW_PAD_D, STRIP_W_D = 78, 6, 1000
ICON_D, AVATAR_D, NAME_D = 40, 58, 34
FADE_S = 0.15       # fade in/out seconds at segment boundaries
STRIP_X_D = 80          # left edge at 1440p
STRIP_BOTTOM_D = 1168   # strip bottom just above the money display at 1440p


def _design_scale(w: int, h: int) -> float:
    """Uniform scale from the 1440p design to the actual video height."""
    return h / 1440.0


def _probe_video(video: Path) -> tuple[float, float]:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=avg_frame_rate",
         "-show_entries", "format=duration",
         "-of", "json", str(video)],
        capture_output=True, text=True,
    )
    import json
    d = json.loads(r.stdout)
    n, dn = (d["streams"][0].get("avg_frame_rate") or "0/1").split("/")
    fps = float(n) / float(dn) if float(dn) else 0.0
    dur = float(d["format"]["duration"])
    return fps, dur


def _demo_names(demo: Path) -> dict[str, str]:
    p = DemoParser(str(demo))
    info = p.parse_player_info()
    out: dict[str, str] = {}
    for _, r in info.iterrows():
        sid, nm = r.get("steamid"), r.get("name")
        if sid is None or not nm:
            continue
        s = str(int(sid)) if str(sid).replace(".0", "").isdigit() else str(sid)
        out.setdefault(s, str(nm))
    return out


def _find_avatar(nick: str, steam_id: str = "") -> Path | None:
    """Cached avatar PNG/JPG for a player — steam_id first, nick fallback."""
    if steam_id:
        try:
            from faceit_names import avatar_path_for_steam
            hit = avatar_path_for_steam(steam_id, nick)
            if hit is not None:
                return hit
        except Exception:
            pass
    base = PROJECT_ROOT / "demos" / "avatars"
    cands = [nick.strip().lower()]
    try:
        from faceit_names import canonical_nick
        canon = canonical_nick(nick)
        if canon and canon.lower() not in cands:
            cands.append(canon.lower())
    except Exception:
        pass
    for c in cands:
        folder = base / c
        if not folder.is_dir():
            continue
        for source in ("faceit", "hltv"):
            for ext in (".png", ".jpg", ".jpeg"):
                p = folder / source / f"{c}{ext}"
                if p.exists():
                    return p
    return None


def _speaker_icon(size: int) -> Image.Image:
    """White speaker glyph with black outline, RGBA. size = icon px."""
    s = size
    pad = s // 8
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # driver box + cone
    box_w = s // 5
    d.polygon(
        [(pad, s // 3), (pad + box_w, s // 3), (pad + box_w + s // 6, s // 6),
         (pad + box_w + s // 6, s - s // 6), (pad + box_w, s - s // 3),
         (pad, s - s // 3)],
        fill=(255, 255, 255, 255), outline=(0, 0, 0, 210), width=2,
    )
    # sound arcs
    cx = pad + box_w + s // 6
    cy = s // 2
    for i, (r, w) in enumerate([(s // 6, 3), (s // 3, 3)]):
        bbox = [cx - r + i * 2, cy - r + i * 2, cx + r - i * 2, cy + r - i * 2]
        d.arc(bbox, start=-55, end=55, fill=(255, 255, 255, 255), width=w)
    return img


def _circular_avatar(img: Image.Image, size: int) -> Image.Image:
    img = img.convert("RGB").resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, size - 1, size - 1], fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    # white ring
    ring = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(ring).ellipse(
        [0, 0, size - 1, size - 1], outline=(255, 255, 255, 255), width=4)
    out = Image.alpha_composite(out, ring)
    return out


def _initials_avatar(name: str, size: int) -> Image.Image:
    h = sum(ord(c) for c in name) % 360
    import colorsys
    r, g, b = (int(v * 255) for v in colorsys.hsv_to_rgb(h / 360, 0.55, 0.9))
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse([0, 0, size - 1, size - 1], fill=(r, g, b, 255))
    font = ImageFont.truetype(str(FONT_PATH), size // 2)
    d = ImageDraw.Draw(img)
    initials = "".join(w[0] for w in name.replace("-", " ").split()[:2]).upper() or "?"
    bbox = d.textbbox((0, 0), initials, font=font)
    d.text(((size - bbox[2] + bbox[0]) / 2, (size - bbox[3] + bbox[1]) / 2 - bbox[1]),
           initials, font=font, fill=(255, 255, 255, 255))
    ring = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(ring).ellipse(
        [0, 0, size - 1, size - 1], outline=(255, 255, 255, 255), width=4)
    return Image.alpha_composite(img, ring)


def _render_row(name: str, avatar_path: Path | None, s: float) -> np.ndarray:
    """One full-alpha row RGBA (strip_w x row_h), scaled by design factor s."""
    row_h = int(round(ROW_H_D * s))
    strip_w = int(round(STRIP_W_D * s))
    icon_size = max(16, int(round(ICON_D * s)))
    avatar_size = max(20, int(round(AVATAR_D * s)))
    name_size = max(14, int(round(NAME_D * s)))
    img = Image.new("RGBA", (strip_w, row_h), (0, 0, 0, 0))
    # soft drop shadow behind content
    icon = _speaker_icon(icon_size)
    ax = icon_size + max(6, int(12 * s))
    ay = (row_h - avatar_size) // 2
    if avatar_path:
        try:
            avatar = _circular_avatar(Image.open(avatar_path), avatar_size)
        except Exception:
            avatar = _initials_avatar(name, avatar_size)
    else:
        avatar = _initials_avatar(name, avatar_size)
    font = ImageFont.truetype(str(FONT_PATH), name_size)
    label = f" {name}"
    # shadow layer
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    tx = ax + avatar_size + int(14 * s)
    ty = (row_h - name_size) // 2
    sd.text((tx + 3, ty + 4), label, font=font, fill=(0, 0, 0, 200))
    shadow = shadow.filter(ImageFilter.GaussianBlur(max(2, int(3 * s))))
    img = Image.alpha_composite(img, shadow)
    d = ImageDraw.Draw(img)
    iy = (row_h - icon_size) // 2
    img.paste(icon, (0, iy), icon)
    img.paste(avatar, (ax, ay), avatar)
    d.text((tx, ty), label, font=font, fill=(255, 255, 255, 255),
           stroke_width=2, stroke_fill=(0, 0, 0, 255))
    return np.asarray(img, dtype=np.float32)


def _alpha_track(segments: list[tuple[float, float]], n_frames: int, fps: float,
                 fade_frames: int) -> np.ndarray:
    """0..1 per-frame row opacity for one speaker (fade at segment edges)."""
    alpha = np.zeros(n_frames, dtype=np.float32)
    fade = max(1, fade_frames)
    for t0, t1 in segments:
        a, b = int(t0 * fps), int(t1 * fps)
        lo, hi = max(0, a - fade), min(n_frames, a)
        if hi > lo:
            alpha[lo:hi] = np.maximum(alpha[lo:hi], np.linspace(0, 1, hi - lo))
        lo2, hi2 = max(0, a), min(n_frames, b + fade)
        if hi2 > lo2:
            alpha[lo2:hi2] = np.maximum(alpha[lo2:hi2], 1.0)
        lo3, hi3 = max(0, b), min(n_frames, b + fade)
        if hi3 > lo3:
            alpha[lo3:hi3] = np.maximum(alpha[lo3:hi3], np.linspace(1, 0, hi3 - lo3))
    return alpha


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--video", required=True, type=Path)
    ap.add_argument("--demo", required=True, type=Path)
    ap.add_argument("--steam-id", required=True)
    ap.add_argument("--ticks", required=True, help="A,B round window ticks")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--tickrate", type=int, default=_TICKRATE)
    ap.add_argument("--fade", type=float, default=FADE_S)
    ap.add_argument("--cq", type=int, default=17)
    args = ap.parse_args()

    video, demo, out = args.video, args.demo, args.out
    if not video.exists():
        sys.exit(f"[ERR] video not found: {video}")
    if not demo.exists():
        sys.exit(f"[ERR] demo not found: {demo}")

    fps, dur = _probe_video(video)
    if fps <= 0 or dur <= 0:
        sys.exit("[ERR] could not probe video fps/duration")

    # design geometry scaled to the actual render size
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "json", str(video)],
        capture_output=True, text=True)
    import json as _json
    _st = _json.loads(probe.stdout)["streams"][0]
    vid_w, vid_h = int(_st["width"]), int(_st["height"])
    s = _design_scale(vid_w, vid_h)
    ROW_H = int(round(ROW_H_D * s))
    ROW_PAD = max(2, int(round(ROW_PAD_D * s)))
    STRIP_W = int(round(STRIP_W_D * s))
    STRIP_H = 5 * (ROW_H + ROW_PAD) + int(round(12 * s))
    STRIP_X = int(round(STRIP_X_D * s))
    STRIP_Y = int(round(STRIP_BOTTOM_D * s)) - STRIP_H

    n_frames = int(round(dur * fps))
    fade_frames = int(args.fade * fps)
    a_tick, b_tick = (int(v) for v in args.ticks.split(","))

    # Single-round offsets dict -> tick_to_time maps demo ticks to clip time.
    offsets = {
        "round_offsets": {1: 0.0},
        "per_round_ticks": {1: [a_tick, b_tick]},
        "per_round_durations": {1: dur},
    }
    pov_team = load_team_map(demo, args.steam_id).get(args.steam_id)
    if pov_team is None:
        sys.exit(f"[ERR] POV steam id {args.steam_id} not found in demo team map")

    segments = _player_talk_segments(demo, offsets, pov_team, args.steam_id, args.tickrate)
    if not segments:
        sys.exit("[ERR] no POV-team voice packets mapped into the round window")
    names = _demo_names(demo)

    # Stable row order: earliest first talk in this clip, then steamid.
    order = sorted(
        ((min(t0 for t0, _ in segs), sid) for sid, segs in segments.items()
         if segs),
        key=lambda x: (x[0], x[1]),
    )
    rows: list[tuple[str, np.ndarray, np.ndarray]] = []
    total_s = 0.0
    for _, sid in order:
        name = names.get(sid, sid)
        segs = segments[sid]
        total_s += sum((t1 - t0) for t0, t1 in segs)
        av = _find_avatar(name, sid)
        row = _render_row(name, av, s)
        alpha = _alpha_track(segs, n_frames, fps, fade_frames)
        print(f"  [voice] {name:14s} ({sid}) {len(segs)} seg(s), "
              f"{sum(t1 - t0 for t0, t1 in segs):5.1f}s"
              f"{'' if av else '  [no cached avatar -> initials]'}")
        rows.append((sid, row, alpha))
    print(f"  [voice] {len(rows)} teammates spoke in this clip, "
          f"{total_s:.1f}s total speech")

    # ── burn ─────────────────────────────────────────────────────────────
    out.parent.mkdir(parents=True, exist_ok=True)
    w, h = STRIP_W, STRIP_H
    cmd = [
        "ffmpeg", "-y",
        "-i", str(video),
        "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{w}x{h}",
        "-r", f"{fps:.6f}", "-i", "pipe:0",
        "-filter_complex",
        f"[0:v][1:v]overlay=x={STRIP_X}:y={STRIP_Y}:eof_action=repeat",
        "-map", "0:a?",
        "-c:v", "h264_nvenc", "-preset", "p7", "-rc", "vbr_hq",
        "-b:v", "0", "-cq", str(args.cq),
        "-c:a", "aac", "-b:a", "192k",
        "-nostats", "-v", "warning",
        "-movflags", "+faststart", str(out),
    ]
    err_path = out.with_suffix(".ffmpeg.log")
    with open(err_path, "wb") as errf:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL, stderr=errf)
        try:
            strip = np.zeros((h, w, 4), dtype=np.float32)
            import time
            t0 = time.time()
            for f in range(n_frames):
                strip[:] = 0.0
                active = [(row, alpha[f]) for _, row, alpha in rows if alpha[f] > 0.01]
                for i, (row, a) in enumerate(active):   # bottom row = most recent
                    y = h - (i + 1) * ROW_H - i * ROW_PAD
                    region = strip[y:y + ROW_H, :w]
                    region[:] = row * a
                proc.stdin.write(strip.astype(np.uint8).tobytes())
                if f % 500 == 0:
                    print(f"  [burn] frame {f}/{n_frames} "
                          f"({time.time() - t0:.0f}s)", flush=True)
        finally:
            proc.stdin.close()
            rc = proc.wait()
    if rc != 0:
        err = err_path.read_text(encoding="utf-8", errors="replace")
        sys.exit(f"[ERR] ffmpeg failed ({rc}): {err[-800:]}")
    print(f"[OK] {out}  ({n_frames} frames @ {fps:.3f}fps, {dur:.1f}s)")


if __name__ == "__main__":
    main()
