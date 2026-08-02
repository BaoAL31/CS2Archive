"""
Upload a YouTube Short with optional scheduled publish.

Usage:
    python scripts/upload/upload_youtube_shorts.py <short.mp4> --meta upload_meta_shorts.json
    python scripts/upload/upload_youtube_shorts.py youtube/.../short.mp4 --publish-at "2026-06-12 17:00"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from upload_youtube import get_authenticated_service, get_youtube_publish_dates, upload_video
from youtube_schedule import DEFAULT_PUBLISH_TZ, resolve_publish_schedule

SHORTS_META_NAME = "upload_meta_shorts.json"
SHORTS_VIDEO_NAME = "short.mp4"


def ensure_shorts_hashtag(title: str, description: str) -> tuple[str, str]:
    """Ensure #Shorts is present for Shorts shelf discovery."""
    title_out = title if "#shorts" in title.lower() else f"{title} #Shorts"
    if "#shorts" in description.lower():
        return title_out, description
    desc = description.rstrip()
    return title_out, f"{desc}\n\n#Shorts" if desc else "#Shorts"


def main() -> None:
    parser = argparse.ArgumentParser(description="Upload a YouTube Short")
    parser.add_argument(
        "video",
        nargs="?",
        help=f"Path to short video (default: <folder>/{SHORTS_VIDEO_NAME})",
    )
    parser.add_argument("--meta", help=f"Path to {SHORTS_META_NAME}")
    parser.add_argument("--title", help="Video title")
    parser.add_argument("--description", "-d", default="", help="Video description")
    parser.add_argument("--tags", help="Comma-separated tags")
    parser.add_argument("--privacy", choices=["private", "unlisted", "public"], default="unlisted")
    parser.add_argument(
        "--publish-at",
        help="Schedule publish (wall-clock time in --timezone, e.g. '2026-06-12 17:00')",
    )
    parser.add_argument(
        "--timezone",
        default=DEFAULT_PUBLISH_TZ,
        help=f"IANA timezone for --publish-at (default: {DEFAULT_PUBLISH_TZ})",
    )
    args = parser.parse_args()

    if not args.video:
        print("[ERROR] <video> path required", flush=True)
        sys.exit(1)

    video = Path(args.video)
    if not video.exists():
        print(f"[ERROR] Video not found: {video}", flush=True)
        sys.exit(1)

    meta: dict = {}
    if args.meta:
        meta_path = Path(args.meta)
        if not meta_path.exists():
            print(f"[ERROR] Meta file not found: {meta_path}", flush=True)
            sys.exit(1)
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    else:
        default_meta = video.parent / SHORTS_META_NAME
        if default_meta.exists():
            meta = json.loads(default_meta.read_text(encoding="utf-8"))

    title = args.title or meta.get("title")
    if not title:
        print("[ERROR] No title (use --title or upload_meta_shorts.json)", flush=True)
        sys.exit(1)

    description = args.description or meta.get("description", "")
    title, description = ensure_shorts_hashtag(title, description)

    privacy = args.privacy or meta.get("privacy", "unlisted")
    original_privacy = privacy
    occupied_dates: set[str] | None = None
    publish_setting = args.publish_at or meta.get("publish_at", "")
    if publish_setting == "auto":
        print("Authenticating with Google...", flush=True)
        youtube_pre = get_authenticated_service()
        occupied_dates = get_youtube_publish_dates(youtube_pre, exclude_shorts=False) or None
        occupied_tuples = (
            {(d.split("T")[0], d.split("T")[1][:5]) for d in occupied_dates if "T" in d}
            if occupied_dates
            else set()
        )
        # Reuse CS2UtilArchive's shared shorts schedule (SLOT_TIMES +
        # find_next_upload_slot) so CS2Archive and CS2UtilArchive shorts
        # reserve from the same slot pool and never double-book a slot.
        _UTIL_SCRIPTS = Path(r"D:\Projects\CS2UtilArchive\scripts")
        if str(_UTIL_SCRIPTS) not in sys.path:
            sys.path.append(str(_UTIL_SCRIPTS))
        from publish_schedule import find_next_upload_slot
        date_str, time_str = find_next_upload_slot(occupied=occupied_tuples)
        publish_setting = f"{date_str} {time_str}"
        print(
            f"  Auto Shorts slot (shared CS2UtilArchive schedule): "
            f"{date_str} {time_str} ({args.timezone})",
            flush=True,
        )
    try:
        privacy, publish_at_utc, publish_tz, publish_local = resolve_publish_schedule(
            publish_at=publish_setting,
            timezone=args.timezone,
            meta=meta,
            privacy=privacy,
            occupied_dates=occupied_dates,
        )
    except ValueError as exc:
        print(f"[ERROR] {exc}", flush=True)
        sys.exit(1)

    tags = None
    if args.tags:
        tags = [t.strip() for t in args.tags.split(",") if t.strip()]
    elif meta.get("tags"):
        tags = list(meta["tags"])
    if tags and "Shorts" not in tags:
        tags.append("Shorts")

    meta_file_path = str(args.meta or video.parent / SHORTS_META_NAME)

    if publish_at_utc:
        if original_privacy != "private":
            print(
                f"  [WARN] Scheduled Shorts publish requires private; "
                f"overriding {original_privacy!r} -> 'private'",
                flush=True,
            )
        print(
            f"  Scheduled Shorts publish: {publish_local} ({publish_tz}) -> {publish_at_utc} UTC",
            flush=True,
        )

    print("Authenticating with Google...", flush=True)
    youtube = get_authenticated_service()
    print("Uploading Short...", flush=True)
    upload_video(
        youtube,
        str(video),
        title,
        description,
        privacy,
        thumbnail_path=None,
        tags=tags,
        meta_path=meta_file_path,
        publish_at_utc=publish_at_utc,
    )
    print("Done!", flush=True)


if __name__ == "__main__":
    main()
