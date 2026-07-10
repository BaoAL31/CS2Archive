"""
Scan youtube/*/upload_meta.json and upload any that are still pending.

The pipeline (scripts/pipeline.py) only produces the finished video,
thumbnail, and upload_meta.json (with youtube_id=None, upload_status="pending").
This script does the actual uploading: for every upload_meta.json whose
upload_status != "completed", it invokes scripts/upload_youtube.py --meta <path>,
which performs the upload, sets the thumbnail, and writes youtube_id +
upload_status="completed" back into the same meta file.

Because the pipeline writes one independent upload_meta.json per variant
(raw -> youtube/{run_id}/, overlay -> youtube/{run_id}_overlay/), this script
naturally handles dual-upload and overlay-only variants with no special flags.

Resume-safe: a completed meta (youtube_id set, upload_status="completed") is
skipped, so re-running only uploads what's left. upload_youtube.py also stores
resumable_* fields in the meta file for crash recovery within a single upload.

Usage:
    python scripts/upload_pending.py                 # upload every pending meta
    python scripts/upload_pending.py --dry-run       # list what would upload
    python scripts/upload_pending.py --limit 1       # upload at most one
    python scripts/upload_pending.py --dir youtube/my-match   # restrict scope
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

PY = sys.executable
UPLOAD_YOUTUBE = SCRIPTS_DIR / "upload_youtube.py"
DEFAULT_YOUTUBE_DIR = PROJECT_ROOT / "youtube"


def _is_pending(meta: dict) -> bool:
    return not (meta.get("upload_status") == "completed" and meta.get("youtube_id"))


def find_pending(youtube_dir: Path) -> list[Path]:
    """Return paths of upload_meta.json files that still need uploading."""
    pending: list[Path] = []
    for meta_path in sorted(youtube_dir.rglob("upload_meta.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  [skip] could not parse {meta_path}: {e}")
            continue
        if not _is_pending(meta):
            continue
        video = meta.get("video_path")
        if not video or not Path(video).exists():
            print(f"  [skip] missing video for {meta_path.parent.name}: {video}")
            continue
        pending.append(meta_path)
    return pending


def upload_one(meta_path: Path, meta: dict, dry_run: bool) -> bool:
    """Upload a single pending meta. Returns True on success.

    Success means upload_youtube.py exited 0 AND the meta file now carries a
    youtube_id. Failure (or dry-run) returns False.
    """
    video = Path(meta["video_path"])
    thumb = meta.get("thumbnail_path")
    privacy = meta.get("privacy", "private")

    cmd = [
        PY, str(UPLOAD_YOUTUBE),
        str(video),
        "--meta", str(meta_path),
        "--privacy", privacy,
    ]
    if thumb and Path(thumb).exists():
        cmd += ["--thumbnail", str(thumb)]

    if dry_run:
        print(f"  [dry-run] would upload: {video} "
              f"(privacy={privacy}, thumbnail={'yes' if thumb else 'no'})")
        return False

    print(f"  Uploading: {video}")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    r = subprocess.run(cmd, cwd=str(PROJECT_ROOT), env=env)
    if r.returncode != 0:
        print(f"  [FAIL] upload exited {r.returncode}: {video}")
        return False

    # Confirm the meta now carries a youtube_id (upload_youtube.py writes it).
    try:
        updated = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        updated = {}
    vid = updated.get("youtube_id")
    if vid:
        print(f"  [OK] Uploaded: https://youtu.be/{vid}")
        return True
    print(f"  [WARN] upload_youtube.py exited 0 but no youtube_id in {meta_path.name}")
    return False


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Upload every pending youtube/*/upload_meta.json")
    parser.add_argument(
        "--dir", default=str(DEFAULT_YOUTUBE_DIR),
        help=f"Root dir to scan for upload_meta.json (default: {DEFAULT_YOUTUBE_DIR})")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="List pending uploads without actually uploading")
    parser.add_argument(
        "--limit", type=int, default=0,
        help="Upload at most N pending metas (default: 0 = all)")
    args = parser.parse_args()

    youtube_dir = Path(args.dir)
    if not youtube_dir.exists():
        print(f"[ERROR] directory not found: {youtube_dir}")
        sys.exit(1)

    pending = find_pending(youtube_dir)
    print(f"Found {len(pending)} pending upload(s) under {youtube_dir}")
    if args.limit > 0:
        pending = pending[:args.limit]

    if not pending:
        print("Nothing to upload.")
        return

    ok = 0
    failed = 0
    for meta_path in pending:
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  [skip] could not re-read {meta_path}: {e}")
            failed += 1
            continue
        if upload_one(meta_path, meta, args.dry_run):
            ok += 1
        else:
            if not args.dry_run:
                failed += 1

    print(f"\nDone. uploaded={ok} failed={failed} "
          f"(dry_run={args.dry_run})")
    if failed and not args.dry_run:
        sys.exit(1)


if __name__ == "__main__":
    main()
