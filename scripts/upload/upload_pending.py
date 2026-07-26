"""
Scan youtube/*/upload_meta.json and upload any that are still pending.

The pipeline (scripts/pov/pipeline.py) only produces the finished video,
thumbnail, and upload_meta.json (with youtube_id=None, upload_status="pending").
This script does the actual uploading: for every upload_meta.json whose
upload_status != "completed", it invokes scripts/upload/upload_youtube.py --meta <path>,
which performs the upload, sets the thumbnail, and writes youtube_id +
upload_status="completed" back into the same meta file.

Because the pipeline writes one independent upload_meta.json per variant
(raw -> youtube/{run_id}/, overlay -> youtube/{run_id}_overlay/), this script
naturally handles dual-upload and overlay-only variants with no special flags.

Resume-safe: a completed meta (youtube_id set, upload_status="completed") is
skipped, so re-running only uploads what's left. upload_youtube.py also stores
resumable_* fields in the meta file for crash recovery within a single upload.

With --also-bilibili, after YouTube (or when YouTube is already done) this also
runs the bilibili.tv upload via upload_youtube.py --also-bilibili /
--bilibili-only. Bilibili status is stored separately in the same meta
(bilibili_aid / bilibili_upload_status).

Usage:
    python scripts/upload/upload_pending.py                 # upload every pending meta
    python scripts/upload/upload_pending.py --dry-run       # list what would upload
    python scripts/upload/upload_pending.py --limit 1       # upload at most one
    python scripts/upload/upload_pending.py --dir youtube/my-match   # restrict scope
    python scripts/upload/upload_pending.py --also-bilibili # YouTube + bilibili.tv
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
from _pathsetup import ensure
ensure()

from upload_bilibili import is_bilibili_pending  # noqa: E402
from youtube_schedule import DEFAULT_PUBLISH_TZ, resolve_auto_publish_schedule  # noqa: E402

PY = sys.executable
UPLOAD_YOUTUBE = SCRIPTS_DIR / "upload" / "upload_youtube.py"
DEFAULT_YOUTUBE_DIR = PROJECT_ROOT / "youtube"


def _is_youtube_pending(meta: dict) -> bool:
    return not (meta.get("upload_status") == "completed" and meta.get("youtube_id"))


def _needs_upload(meta: dict, also_bilibili: bool) -> bool:
    if _is_youtube_pending(meta):
        return True
    if also_bilibili and is_bilibili_pending(meta):
        return True
    return False


def find_pending(youtube_dir: Path, also_bilibili: bool = False) -> list[Path]:
    """Return paths of upload_meta.json files that still need uploading."""
    pending: list[Path] = []
    for meta_path in sorted(youtube_dir.rglob("upload_meta.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  [skip] could not parse {meta_path}: {e}")
            continue
        if not _needs_upload(meta, also_bilibili):
            continue
        video = meta.get("video_path")
        if not video or not Path(video).exists():
            print(f"  [skip] missing video for {meta_path.parent.name}: {video}")
            continue
        pending.append(meta_path)
    return pending


def upload_one(meta_path: Path, meta: dict, dry_run: bool, also_bilibili: bool) -> bool:
    """Upload a single pending meta. Returns True on success.

    Success means upload_youtube.py exited 0 AND the expected platform IDs
    are present in the meta file afterward.
    """
    video = Path(meta["video_path"])
    thumb = meta.get("thumbnail_path")
    privacy = meta.get("privacy", "private")
    yt_pending = _is_youtube_pending(meta)
    bili_pending = also_bilibili and is_bilibili_pending(meta)

    if dry_run:
        parts = []
        if yt_pending:
            parts.append("youtube")
        if bili_pending:
            parts.append("bilibili")
        print(
            f"  [dry-run] would upload ({'+'.join(parts) or 'nothing'}): {video} "
            f"(privacy={privacy}, thumbnail={'yes' if thumb else 'no'})"
        )
        return False

    if yt_pending:
        cmd = [
            PY, str(UPLOAD_YOUTUBE),
            str(video),
            "--meta", str(meta_path),
            "--privacy", privacy,
        ]
        if thumb and Path(thumb).exists():
            cmd += ["--thumbnail", str(thumb)]
        if also_bilibili:
            cmd.append("--also-bilibili")

        print(f"  Uploading: {video}")
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
        r = subprocess.run(cmd, cwd=str(PROJECT_ROOT), env=env)
        if r.returncode != 0:
            print(f"  [FAIL] upload exited {r.returncode}: {video}")
            return False
    elif bili_pending:
        cmd = [
            PY, str(UPLOAD_YOUTUBE),
            str(video),
            "--meta", str(meta_path),
            "--bilibili-only",
        ]
        if thumb and Path(thumb).exists():
            cmd += ["--thumbnail", str(thumb)]
        print(f"  Uploading bilibili only: {video}")
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
        r = subprocess.run(cmd, cwd=str(PROJECT_ROOT), env=env)
        if r.returncode != 0:
            print(f"  [FAIL] bilibili upload exited {r.returncode}: {video}")
            return False
    else:
        return True

    try:
        updated = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        updated = {}

    ok = True
    vid = updated.get("youtube_id")
    if yt_pending or not also_bilibili:
        if vid:
            print(f"  [OK] Uploaded: https://youtu.be/{vid}")
        else:
            print(f"  [WARN] upload_youtube.py exited 0 but no youtube_id in {meta_path.name}")
            ok = False
    if also_bilibili:
        aid = updated.get("bilibili_aid")
        if aid and updated.get("bilibili_upload_status") == "completed":
            print(f"  [OK] Bilibili: aid={aid}")
        else:
            print(f"  [WARN] bilibili not completed in {meta_path.name}")
            ok = False
    return ok


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
    parser.add_argument(
        "--also-bilibili", action="store_true",
        help="Also upload to bilibili.tv (same title/schedule/tags; needs "
             ".bilibili_storage.json). Resume-safe via bilibili_aid in meta.")
    parser.add_argument(
        "--check-schedule", action="store_true",
        help="Show next available YouTube publish slot and exit")
    args = parser.parse_args()

    if args.check_schedule:
        from upload_youtube import get_authenticated_service, get_youtube_publish_dates
        print("Querying YouTube schedule...")
        youtube = get_authenticated_service()
        occupied = get_youtube_publish_dates(youtube) or set()
        privacy, utc, tz, local = resolve_auto_publish_schedule(
            timezone=DEFAULT_PUBLISH_TZ, occupied_dates=occupied,
        )
        print(f"Next free slot:")
        print(f"  Local:  {local} {tz}")
        print(f"  UTC:    {utc}")
        print(f"  Status: {privacy}")
        if occupied:
            print(f"  Occupied dates: {len(occupied)}")
        return

    youtube_dir = Path(args.dir)
    if not youtube_dir.exists():
        print(f"[ERROR] directory not found: {youtube_dir}")
        sys.exit(1)

    pending = find_pending(youtube_dir, also_bilibili=args.also_bilibili)
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
        if upload_one(meta_path, meta, args.dry_run, args.also_bilibili):
            ok += 1
        else:
            if not args.dry_run:
                failed += 1

    print(f"\nDone. uploaded={ok} failed={failed} "
          f"(dry_run={args.dry_run}, also_bilibili={args.also_bilibili})")
    if failed and not args.dry_run:
        sys.exit(1)


if __name__ == "__main__":
    main()
