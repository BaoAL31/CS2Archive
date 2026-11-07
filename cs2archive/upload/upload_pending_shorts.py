"""
Scan for pending Short uploads and upload them.

Mirrors cs2archive/upload/upload_pending.py but for Shorts instead of long-form
videos. For every `upload_meta_shorts.json` under a render dir that still has
a pending platform (YouTube, TikTok, or Instagram not completed), it invokes
cs2archive/upload/upload_youtube_shorts.py <video> --meta <meta>, which performs
the upload/schedule on all remaining platforms and writes status back into the
same meta file.

Resume-safe: a meta whose YouTube (upload_status=completed + youtube_id),
TikTok (tiktok_status=scheduled), Instagram (queued/published), and Facebook
(scheduled/published) are all done is skipped. Instagram Graph cannot natively
schedule, so a queued Reel is a one-shot Windows task at the YouTube slot.

Usage:
    python cs2archive/upload/upload_pending_shorts.py              # upload all pending
    python cs2archive/upload/upload_pending_shorts.py --dry-run    # list what would upload
    python cs2archive/upload/upload_pending_shorts.py --limit 1    # upload at most one
    python cs2archive/upload/upload_pending_shorts.py --dir renders/my-short  # restrict scope
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

PY = sys.executable
UPLOAD_SHORTS = PROJECT_ROOT / "cs2archive" / "upload" / "upload_youtube_shorts.py"
SHORTS_META_NAME = "upload_meta_shorts.json"
DEFAULT_ROOT = PROJECT_ROOT / "renders"

# The partial-upload gate lives with the uploader so both entry points agree on
# the flag names and the authorization rule.
from cs2archive.upload.upload_youtube_shorts import (  # noqa: E402
    PLATFORM_FLAGS,
    refuse_unauthorized_partial_upload,
)


def _platform_pending(meta: dict) -> dict[str, bool]:
    """Which platforms still need work, keyed by name.

    A platform listed in ``skipped_platforms`` was dropped by an authorized
    operator (``CS2ARCHIVE_ALLOW_PARTIAL_UPLOAD=1``) and counts as settled, so
    the runner does not retry it forever.
    """
    skipped = meta.get("upload_status") == "skipped"
    dropped = set(meta.get("skipped_platforms") or ())
    yt_done = skipped or bool(
        meta.get("upload_status") == "completed" and meta.get("youtube_id")
    )
    tt_done = skipped or "tiktok" in dropped or meta.get("tiktok_status") in {
        # "scheduled" = legacy browser-UI flow; the API writes these:
        "scheduled", "uploaded_draft", "queued", "published",
    }
    ig_done = skipped or "instagram" in dropped or meta.get("instagram_status") in {
        "scheduled", "published", "queued",
    }
    fb_done = skipped or "facebook" in dropped or meta.get("facebook_status") in {
        "scheduled", "published",
    }
    return {
        "youtube": not yt_done,
        "tiktok": not tt_done,
        "instagram": not ig_done,
        "facebook": not fb_done,
    }


def _shorts_source(demo) -> str:
    path = str(demo or "").replace("\\", "/").lower()
    if "demos/faceit/" in path:
        return "faceit"
    return "hltv"


def _passes_demand(meta_path: Path, meta: dict, *, payload: dict | None = None, stars: dict | None = None) -> bool:
    from cs2archive.shorts.demand_gate import (
        folder_orgs,
        passes_shorts_demand_gate,
    )
    from cs2archive.upload.shorts_player_day import pov_nick_from_meta_path

    nick = pov_nick_from_meta_path(meta_path) or ""
    text = f"{meta.get('title') or ''} {meta.get('description') or ''}"
    orgs: list[str] = []
    shorts: list[dict] = []
    demo = ""
    timeline = meta_path.parent / "short_timeline.json"
    if timeline.exists():
        try:
            data = json.loads(timeline.read_text(encoding="utf-8-sig"))
            demo = data.get("demo_path") or ""
            if demo:
                orgs = folder_orgs(demo)
            shorts = list(data.get("shorts") or [])
        except Exception:
            pass
    source = _shorts_source(demo)
    if source == "faceit":
        return passes_shorts_demand_gate(nick, orgs=orgs, text=text, payload=payload)
    # HLTV deliverables have already been selected by the view-ranked renderer.
    # Never reapply the retired long-form demand or intercept veto at upload.
    return bool(shorts)


def _mark_skipped(meta_path: Path, meta: dict, reason: str) -> None:
    meta["upload_status"] = "skipped"
    meta["skip_reason"] = reason
    meta["tiktok_status"] = "skipped"
    meta["instagram_status"] = "skipped"
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")


def _mark_skipped_low_demand(meta_path: Path, meta: dict) -> None:
    _mark_skipped(meta_path, meta, "low_demand")


def _needs_upload(meta: dict) -> bool:
    return any(_platform_pending(meta).values())


def find_pending(root: Path) -> list[Path]:
    """Return paths of upload_meta_shorts.json files that still need work."""
    pending: list[Path] = []
    for meta_path in sorted(root.rglob(SHORTS_META_NAME)):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8-sig"))
        except Exception as e:
            print(f"  [skip] could not parse {meta_path}: {e}")
            continue
        if not _needs_upload(meta):
            continue
        video = meta.get("video_path")
        if not video or not Path(video).exists():
            # fall back to the short's own folder name convention if video_path
            # is missing/stale, matching upload_youtube_shorts.py
            print(f"  [skip] missing video for {meta_path.parent.name}: {video}")
            continue
        pending.append(meta_path)
    return pending


def upload_one(
    meta_path: Path,
    meta: dict,
    dry_run: bool,
    *,
    skip_tiktok: bool = False,
    skip_instagram: bool = False,
    skip_facebook: bool = False,
) -> bool:
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
    if skip_facebook:
        cmd.append("--skip-facebook")

    print(f"  Uploading: {video}")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    r = subprocess.run(cmd, cwd=str(PROJECT_ROOT), env=env)
    if r.returncode != 0:
        print(f"  [FAIL] upload exited {r.returncode}: {video}")
        return False

    try:
        updated = json.loads(meta_path.read_text(encoding="utf-8-sig"))
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
    # Hidden on purpose: dropping a platform needs operator authorization
    # (CS2ARCHIVE_ALLOW_PARTIAL_UPLOAD), so these must not read as ordinary options.
    for _name, _flag in PLATFORM_FLAGS.items():
        parser.add_argument(
            _flag, action="store_true", help=argparse.SUPPRESS, dest=f"skip_{_name}")
    args = parser.parse_args()

    skipped_platforms = [name for name in PLATFORM_FLAGS if getattr(args, f"skip_{name}")]
    refuse_unauthorized_partial_upload(skipped_platforms)

    root = Path(args.dir)
    if not root.exists():
        print(f"[ERROR] directory not found: {root}")
        sys.exit(1)

    pending = find_pending(root)
    print(f"Found {len(pending)} pending short upload(s) under {root}")
    if args.limit > 0:
        pending = pending[:args.limit]

    if not pending:
        print("Nothing to upload.")
        return

    # preflight: TikTok uses the Content Posting API token. Instagram/Facebook use Graph.
    if pending and not args.dry_run:
        _TT_DONE = {"scheduled", "uploaded_draft", "queued", "published"}
        need_tt = any(
            json.loads(p.read_text(encoding="utf-8-sig")).get("tiktok_status") not in _TT_DONE
            for p in pending
        )
        if need_tt:
            try:
                from cs2archive.upload.social_session_check import check_tiktok
                ok, msg = check_tiktok()
                print(f"[preflight tiktok] {msg}", flush=True)
                if not ok:
                    print(
                        "  -> TikTok API token missing/expired. Run: "
                        "python scripts/tiktok_api.py login (in CS2UtilArchive). "
                        "The upload will still run and fail loudly on TikTok.",
                        flush=True,
                    )
            except Exception as e:
                print(f"[preflight warn] {e}", flush=True)

    ok = 0
    failed = 0
    skipped = 0
    for meta_path in pending:
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8-sig"))
        except Exception as e:
            print(f"  [skip] could not re-read {meta_path}: {e}")
            failed += 1
            continue
        if not _passes_demand(meta_path, meta):
            title = (meta.get("title") or meta_path.parent.name)[:70]
            source = "hltv"
            timeline = meta_path.parent / "short_timeline.json"
            if timeline.exists():
                try:
                    demo = json.loads(timeline.read_text(encoding="utf-8-sig")).get("demo_path")
                    source = _shorts_source(demo)
                except Exception:
                    pass
            reason = "low_demand" if source == "faceit" else "slot_floor"
            print(f"  [skip] {reason}: {title}")
            if not args.dry_run:
                _mark_skipped(meta_path, meta, reason)
            skipped += 1
            continue
        if upload_one(
            meta_path, meta, args.dry_run,
            skip_tiktok=args.skip_tiktok,
            skip_instagram=args.skip_instagram,
            skip_facebook=args.skip_facebook,
        ):
            ok += 1
        else:
            if not args.dry_run:
                failed += 1

    print(f"\nDone. uploaded={ok} skipped={skipped} failed={failed} "
          f"(dry_run={args.dry_run})")
    if failed and not args.dry_run:
        sys.exit(1)


if __name__ == "__main__":
    main()
