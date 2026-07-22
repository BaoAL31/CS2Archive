"""Generate a 5s silent outro clip for CS2Archive POV videos.

Usage:
    python scripts/pov/generate_outro.py <video_path> [--output <path>]

Detects resolution and framerate from <video_path>, renders a single black
frame with centered text (top half), and encodes a 5-second h.264 clip
compatible for stream-copy concat.

Output defaults to <video_parent>/outro.mp4
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FONT_PATH = PROJECT_ROOT / "assets" / "fonts" / "Montserrat-Bold.ttf"
DURATION = 5


def _get_font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_PATH), size)


def get_video_info(path: Path) -> tuple[int, int, float]:
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_streams", "-select_streams", "v:0", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        print(f"[ERROR] Could not probe video: {path}")
        sys.exit(1)
    data = json.loads(r.stdout)
    s = data.get("streams", [])
    if not s:
        print(f"[ERROR] No video stream found in {path}")
        sys.exit(1)
    w = s[0]["width"]
    h = s[0]["height"]
    r_frame_rate = s[0].get("r_frame_rate", "30/1")
    num, den = r_frame_rate.split("/")
    fps = float(num) / float(den)
    return w, h, fps


def render_frame(w: int, h: int) -> Image.Image:
    img = Image.new("RGB", (w, h), (0, 0, 0))
    draw = ImageDraw.Draw(img)

    line1 = "Thanks for watching!"
    line2 = "Like & Subscribe"

    font_size1 = max(48, h // 18)
    font_size2 = max(32, h // 27)

    font1 = _get_font(font_size1)
    font2 = _get_font(font_size2)

    bbox1 = draw.textbbox((0, 0), line1, font=font1)
    bbox2 = draw.textbbox((0, 0), line2, font=font2)
    tw1, th1 = bbox1[2] - bbox1[0], bbox1[3] - bbox1[1]
    tw2, th2 = bbox2[2] - bbox2[0], bbox2[3] - bbox2[1]

    cx, cy = w // 2, h // 4
    gap = th1 // 2

    x1 = cx - tw1 // 2
    y1 = cy - (th1 + gap + th2) // 2
    draw.text((x1, y1), line1, font=font1, fill=(255, 255, 255))

    x2 = cx - tw2 // 2
    y2 = y1 + th1 + gap
    draw.text((x2, y2), line2, font=font2, fill=(200, 200, 200))

    return img


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a silent outro clip")
    parser.add_argument("video", help="Path to main video (used for resolution/fps detection)")
    parser.add_argument("--output", "-o", help="Output path for outro clip (default: <video_dir>/outro.mp4)")
    args = parser.parse_args()

    video = Path(args.video)
    if not video.exists():
        print(f"[ERROR] Video not found: {video}")
        sys.exit(1)

    w, h, fps = get_video_info(video)
    print(f"  Source: {w}x{h} @ {fps:.2f} fps")

    if args.output:
        out_path = Path(args.output)
    else:
        out_path = video.parent / "outro.mp4"

    with tempfile.TemporaryDirectory() as tmp:
        frame_path = Path(tmp) / "outro_frame.png"
        img = render_frame(w, h)
        img.save(frame_path)

        print(f"  Encoding {DURATION}s silent outro to {out_path.name}...", end=" ", flush=True)
        r = subprocess.run(
            ["ffmpeg", "-y", "-loop", "1", "-i", str(frame_path),
             "-c:v", "libx264", "-t", str(DURATION),
             "-r", str(fps),
             "-pix_fmt", "yuv420p",
             "-profile:v", "high",
             "-level", "5.1",
             "-vf", f"scale={w}:{h}",
             "-preset", "medium",
             "-crf", "15",
             str(out_path)],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode != 0:
            print("FAILED")
            print(r.stderr[-500:])
            sys.exit(1)

    mb = out_path.stat().st_size / 1024 / 1024
    print(f"OK ({mb:.1f} MB)")


if __name__ == "__main__":
    main()
