"""
Remove the entire renders folder after confirming video is in youtube/.

Usage:
    python scripts/cleanup_renders.py <renders_folder> --youtube <youtube_folder>

Example:
    python scripts/cleanup_renders.py "demos/renders/faze-vs-vitality-ropz-nuke" --youtube "youtube/faze-vs-vitality-iem-atlanta-2026_ropz_nuke"
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Remove renders folder after successful youtube move")
    parser.add_argument("renders", help="Renders folder to remove")
    parser.add_argument("--youtube", "-y", required=True, help="YouTube POV folder with video.mp4")
    args = parser.parse_args()

    renders = Path(args.renders)
    youtube = Path(args.youtube)

    if not renders.is_dir():
        print(f"[ERROR] Renders folder not found: {renders}")
        return

    if not youtube.is_dir():
        print(f"[ERROR] YouTube folder not found: {youtube}")
        return

    video = youtube / "video.mp4"
    if not video.exists():
        print(f"[ERROR] {video} not found — did you move the video yet?")
        return

    mb = sum(f.stat().st_size for f in renders.rglob("*") if f.is_file()) / (1024 * 1024)
    shutil.rmtree(renders)
    print(f"Removed {renders.name} ({mb:.0f} MB freed)")


if __name__ == "__main__":
    main()
