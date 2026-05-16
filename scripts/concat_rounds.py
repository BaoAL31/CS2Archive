"""
Concatenate round-NNN.mp4 files into a single video.

Usage:
    python scripts/concat_rounds.py <folder> [--output <path>]

Examples:
    python scripts/concat_rounds.py "demos/renders/faze-vs-vitality-ropz-nuke"
    python scripts/concat_rounds.py "demos/renders/faze-vs-vitality-ropz-nuke" --output "youtube/video.mp4"
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Concatenate round clips into one video")
    parser.add_argument("folder", help="Folder containing round-NNN.mp4 files")
    parser.add_argument("--output", "-o", help="Output path (default: folder/combined.mp4)")
    args = parser.parse_args()

    folder = Path(args.folder)
    if not folder.is_dir():
        print(f"[ERROR] Folder not found: {folder}")
        sys.exit(1)

    files = sorted(folder.glob("round-*.mp4"))
    if not files:
        print("[ERROR] No round-*.mp4 files found")
        sys.exit(1)

    print(f"Found {len(files)} round files")

    with tempfile.TemporaryDirectory() as tmp:
        list_path = Path(tmp) / "files.txt"
        with open(list_path, "w") as f:
            for fp in files:
                f.write(f"file '{fp.resolve()}'\n")

        output = Path(args.output) if args.output else folder / "combined.mp4"
        print(f"Concatenating -> {output}")

        result = subprocess.run(
            ["ffmpeg", "-f", "concat", "-safe", "0", "-i", str(list_path),
             "-c", "copy", str(output)],
            capture_output=True, text=True, timeout=3600,
        )

        if result.returncode == 0:
            mb = output.stat().st_size / 1024 / 1024
            print(f"Done: {mb:.0f} MB")
        else:
            print(f"[ERROR] ffmpeg failed: {result.stderr[-300:]}")
            sys.exit(1)


if __name__ == "__main__":
    main()
