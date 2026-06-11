"""
One-time migration: rename round-NNN.mp4 -> batch-NNN-NNN.mp4 in render folders.

Usage:
    python scripts/rename_legacy_rounds.py <render_folder>

Or recursively scan all pov-* folders:
    python scripts/rename_legacy_rounds.py demos/renders --recursive
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_ROUND_RE = re.compile(r"^round-(\d+)\.mp4$")


def rename_folder(folder: Path, dry_run: bool = False) -> int:
    renamed = 0
    for f in sorted(folder.glob("round-*.mp4")):
        m = _ROUND_RE.match(f.name)
        if not m:
            continue
        new_name = f"batch-{m.group(1)}-{m.group(1)}.mp4"
        dst = f.parent / new_name
        if dst.exists():
            print(f"  [SKIP] {f.name} -> {new_name} (target exists)")
            continue
        if dry_run:
            print(f"  [DRY] {f.name} -> {new_name}")
        else:
            f.rename(dst)
            print(f"  [OK] {f.name} -> {new_name}")
        renamed += 1
    return renamed


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate round-NNN.mp4 to batch-NNN-NNN.mp4")
    parser.add_argument("folder", help="Render folder or parent directory")
    parser.add_argument("--recursive", "-r", action="store_true",
                        help="Scan pov-* subfolders recursively")
    parser.add_argument("--dry-run", "-n", action="store_true",
                        help="Show what would be renamed without doing it")
    args = parser.parse_args()

    folder = Path(args.folder)
    if not folder.is_dir():
        print(f"[ERROR] Folder not found: {folder}")
        sys.exit(1)

    if args.recursive:
        targets = sorted(f for f in folder.iterdir() if f.is_dir() and f.name.startswith("pov-"))
    else:
        targets = [folder]

    total = 0
    for target in targets:
        count = rename_folder(target, dry_run=args.dry_run)
        if count:
            print(f"  {target.name}: {count} file(s)")
        total += count

    if total == 0:
        print("No round-*.mp4 files found.")
    else:
        print(f"\nDone. {total} file(s) {'would be' if args.dry_run else ''} renamed.")


if __name__ == "__main__":
    main()
