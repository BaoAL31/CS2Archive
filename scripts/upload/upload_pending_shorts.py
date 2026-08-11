"""
Scan for pending Short uploads and upload them.

Mirrors scripts/upload/upload_pending.py but for Shorts instead of long-form
videos. For every `upload_meta_shorts.json` under a render dir that still has
a pending platform (YouTube, TikTok, or Instagram not completed), it invokes
scripts/upload/upload_youtube_shorts.py <video> --meta <meta>, which performs
the upload/schedule on all remaining platforms and writes status back into the
same meta file.

Resume-safe: a meta whose YouTube (upload_status=completed + youtube_id),
TikTok (tiktok_status=scheduled), and Instagram (instagram_status=scheduled)
are all done is skipped. upload_youtube_shorts.py re-uses the committed slot
for the already-done platforms and only fills in what's left, so re-running
only does missing work.

Usage:
    python scripts/upload/upload_pending_shorts.py              # upload all pending
    python scripts/upload/upload_pending_shorts.py --dry-run    # list what would upload
    python scripts/upload/upload_pending_shorts.py --limit 1    # upload at most one
    python scripts/upload/upload_pending_shorts.py --dir renders/my-short  # restrict scope
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

PY = sys.executable
UPLOAD_SHORTS = SCRIPTS_DIR / "upload" / "upload_youtube_shorts.py"
SHORTS_META_NAME = "upload_meta_shorts.json"
DEFAULT_ROOT = PROJECT_ROOT / "renders"


def _platform_pending(meta: dict) -> dict[str, bool]:
    """Which platforms still need work, keyed by name."""
    yt_done = bool(meta.get("upload_status") == "completed" and meta.get("youtube_id"))
    tt_done = meta.get("tiktok_status") == "scheduled"
    ig_done = meta.get("instagram_status") == "scheduled"
    return {
        "youtube": not yt_done,
        "tiktok": not tt_done,
        "instagram": not ig_done,
    }


def _needs_upload(meta: dict, *, skip_tiktok: bool, skip_instagram: bool) -> bool:
    pending = _platform_pending(meta)
    if pending["youtube"]:
        return True
    if not skip_tiktok and pending["tiktok"]:
        return True
    if not skip_instagram and pending["instagram"]:
        return True
    return False


def find_pending(root: Path, *, skip_tiktok: bool, skip_instagram: bool) -> list[Path]:
    """Return paths of upload_meta_shorts.json files that still need work."""
    pending: list[Path] = []
    for meta_path in sorted(root.rglob(SHORTS_META_NAME)):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  [skip] could not parse {meta_path}: {e}")
            continue
        if not _needs_upload(meta, skip_tiktok=skip_tiktok, skip_instagram=skip_instagram):
            continue
        video = meta.get("video_path")
        if not video or not Path(video).exists():
            # fall back to the short's own folder name convention if video_path
            # is missing/stale, matching upload_youtube_shorts.py
            print(f"  [skip] missing video for {meta_path.parent.name}: {video}")
            continue
        pending.append(meta_path)
    return pending


def upload_one(meta_path: Path, meta: dict, dry_run: bool, *, skip_tiktok: bool, skip_instagram: bool) -> bool:
    video = Path(meta["video_path"])
    pending = _platform_pending(meta)

    if dry_run:
        parts = [name for name, p in pending.items() if p]
        print(
            f"  [dry-run] would upload ({'+'.join(parts) or 'nothing'}): {video} "
            f"(privacy={meta.get('privacy', 'unlisted')})"
        )
        return False

    cmd = [PY, str(UPLOAD_SHORTS), str(video), "--meta", str(meta_path)]
    if skip_tiktok:
        cmd.append("--skip-tiktok")
    if skip_instagram:
        cmd.append("--skip-instagram")

    print(f"  Uploading: {video}")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    r = subprocess.run(cmd, cwd=str(PROJECT_ROOT), env=env)
    if r.returncode != 0:
        print(f"  [FAIL] upload exited {r.returncode}: {video}")
        return False

    try:
        updated = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        updated = {}

    remaining = _platform_pending(updated)
    still = [name for name, p in remaining.items() if p]
    if still:
        print(f"  [WARN] still pending after upload ({', '.join(still)}): {video}")
        return False
    print(f"  [OK] Uploaded: https://youtu.be/{updated.get('youtube_id')}")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Upload every pending Short (upload_meta_shorts.json)")
    parser.add_argument(
        "--dir", default=str(DEFAULT_ROOT),
        help=f"Root dir to scan for {SHORTS_META_NAME} (default: {DEFAULT_ROOT})")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="List pending uploads without actually uploading")
    parser.add_argument(
        "--limit", type=int, default=0,
        help="Upload at most N pending metas (default: 0 = all)")
    parser.add_argument(
        "--skip-tiktok", action="store_true",
        help="Treat TikTok as done / don't require it (passes --skip-tiktok to uploader)")
    parser.add_argument(
        "--skip-instagram", action="store_true",
        help="Treat Instagram as done / don't require it (passes --skip-instagram to uploader)")
    args = parser.parse_args()

    root = Path(args.dir)
    if not root.exists():
        print(f"[ERROR] directory not found: {root}")
        sys.exit(1)

    pending = find_pending(root, skip_tiktok=args.skip_tiktok, skip_instagram=args.skip_instagram)
    print(f"Found {len(pending)} pending short upload(s) under {root}")
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
        if upload_one(meta_path, meta, args.dry_run,
                      skip_tiktok=args.skip_tiktok, skip_instagram=args.skip_instagram):
            ok += 1
        else:
            if not args.dry_run:
                failed += 1

    print(f"\nDone. uploaded={ok} failed={failed} (dry_run={args.dry_run})")
    if failed and not args.dry_run:
        sys.exit(1)


if __name__ == "__main__":
    main()
