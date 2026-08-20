"""
Upload a YouTube Short (plus TikTok and Instagram by default).

Schedules on YouTube, TikTok, and Instagram using the shared
CS2UtilArchive slot pool (17:30 Australia/Sydney, once daily). Resume-safe: a
completed platform upload is skipped on re-run.

Usage:
    python scripts/upload/upload_youtube_shorts.py <short.mp4> --meta upload_meta_shorts.json
    python scripts/upload/upload_youtube_shorts.py youtube/.../short.mp4 --publish-at "2026-06-12 17:00"

Shorts naming convention (see `docs/agents/shorts-titles.md` for the full
guide + approved examples):
    Title must contain the PLAYER name, the clip KIND (clutch or multikill,
    e.g. "1v3 Clutch + 4K", "ACE", "5K"), and the OPPONENT. Wording is
    flexible beyond that. Hashtags go in the TITLE (never a ``tags`` field):
    ``#cs2 #counterstrike #{tournament}`` — tournament hashtag lowercase-
    squashed, e.g. ``#blastbounty2026``. No ``#csgo``, no ``#Shorts``, no map
    hashtags.
    e.g. ``donk's 1v3 Clutch + 4K vs MOUZ #cs2 #counterstrike #blastbounty2026``
    (HLTV/team matches name the org; FACEIT lobbies use an ELO label — number
    at >=3000, "level 10" below.)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
_UPLOAD_DIR = _SCRIPTS_DIR / "upload"
_UTIL_SCRIPTS = Path(r"D:\Projects\CS2UtilArchive\scripts")
# CS2Archive's own scripts/upload must win over the shared CS2UtilArchive
# scripts dir for conflicting module names (upload_youtube has extra helpers
# here). Insert util first, then CS2Archive's dirs so they take precedence.
for _p in (_UTIL_SCRIPTS, _SCRIPTS_DIR, _UPLOAD_DIR):
    while str(_p) in sys.path:
        sys.path.remove(str(_p))
    sys.path.insert(0, str(_p))
_UTIL_ROOT = _UTIL_SCRIPTS.parent

from upload_youtube import (  # noqa: E402
    _record_publish_meta,
    get_authenticated_service,
    get_youtube_publish_dates,
    upload_video,
)
from youtube_schedule import DEFAULT_PUBLISH_TZ, resolve_publish_schedule  # noqa: E402

SHORTS_META_NAME = "upload_meta_shorts.json"
SHORTS_VIDEO_NAME = "short.mp4"
SHORTS_COVER_NAME = "cover.png"

SOCIAL_UPLOAD_TIMEOUT_SECONDS = 1800


def ensure_shorts_hashtag(title: str, description: str) -> tuple[str, str]:
    """Return title/description unchanged.

    Per docs/agents/shorts-titles.md the title must NOT contain ``#Shorts``
    (Shorts are detected by vertical aspect ratio + duration, not the hashtag),
    so we intentionally never append it here.
    """
    return title, description


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
        "--skip-tiktok",
        action="store_true",
        help="Skip the TikTok upload (default: upload to TikTok after YouTube)",
    )
    parser.add_argument(
        "--skip-instagram",
        action="store_true",
        help="Skip the Instagram upload (default: upload to Instagram after YouTube)",
    )
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

    meta_path = Path(args.meta or video.parent / SHORTS_META_NAME)
    if args.meta:
        if not meta_path.exists():
            print(f"[ERROR] Meta file not found: {meta_path}", flush=True)
            sys.exit(1)
    meta = _read_meta(meta_path)

    title = args.title or meta.get("title")
    if not title:
        print("[ERROR] No title (use --title or upload_meta_shorts.json)", flush=True)
        sys.exit(1)

    description = args.description or meta.get("description", "")
    title, description = ensure_shorts_hashtag(title, description)

    meta_now = _read_meta(meta_path)
    youtube_done = bool(meta_now.get("youtube_id") and meta_now.get("upload_status") == "completed")

    privacy = args.privacy or meta.get("privacy", "unlisted")
    original_privacy = privacy
    occupied_dates: set[str] | None = None
    publish_at_utc: str | None = None
    publish_tz = args.timezone or meta.get("publish_timezone") or DEFAULT_PUBLISH_TZ
    publish_local: str | None = None

    if youtube_done and meta_now.get("publish_at_utc"):
        # Reuse the already-committed slot for the remaining platforms so a
        # re-run doesn't move the whole cross-platform schedule.
        publish_at_utc = meta_now["publish_at_utc"]
        publish_tz = meta_now.get("publish_timezone") or publish_tz
        date_str, time_str = _utc_to_local_publish(publish_at_utc, publish_tz)
        publish_local = f"{date_str} {time_str}"
        print(
            f"  Reusing committed slot: {publish_local} ({publish_tz})",
            flush=True,
        )
    else:
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

    meta_file_path = str(meta_path)

    if publish_at_utc and not youtube_done:
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

    if not youtube_done:
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
    else:
        print(f"  [yt] already completed id={meta_now.get('youtube_id')}", flush=True)

    # YouTube is the master schedule. Whatever slot YouTube actually committed
    # (ground truth from the upload response publishAt) is the single source
    # of truth for every other platform. Re-derive the local wall-clock and
    # persist publish fields so tiktok/instagram follow it exactly.
    if publish_at_utc:
        _record_publish_meta(meta_file_path, publish_tz, publish_at_utc)
        meta_now = _read_meta(meta_path)
        if meta_now.get("publish_at_utc"):
            publish_tz = meta_now.get("publish_timezone") or publish_tz
            date_str, time_str = _utc_to_local_publish(meta_now["publish_at_utc"], publish_tz)
            publish_local = f"{date_str} {time_str}"
        print(
            f"  Master schedule (YouTube committed): {publish_local} ({publish_tz})",
            flush=True,
        )

    browser_date = browser_time = None
    if publish_local:
        try:
            pdate, ptime = publish_local.split(" ")
            from publish_schedule import wall_clock_to_local_schedule
            browser_date, browser_time = wall_clock_to_local_schedule(pdate, ptime, publish_tz)
        except Exception as exc:
            print(f"  [warn] could not convert slot for browser UIs: {exc}", flush=True)

    if not args.skip_tiktok:
        _run_tiktok(video, title, browser_date, browser_time, meta_file_path)

    if not args.skip_instagram:
        _run_instagram(video, title, browser_date, browser_time, meta_file_path)


def _read_meta(meta_path: Path) -> dict:
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_meta(meta_path: Path, **fields) -> None:
    try:
        meta = _read_meta(meta_path)
        meta.update(fields)
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"  [meta] write warn: {exc}", flush=True)


def _utc_to_local_publish(publish_at_utc: str, tz: str) -> tuple[str, str]:
    utc_dt = datetime.fromisoformat(publish_at_utc.replace("Z", "+00:00"))
    local_dt = utc_dt.astimezone(ZoneInfo(tz))
    return local_dt.strftime("%Y-%m-%d"), local_dt.strftime("%H:%M")


def _run_tiktok(
    video: Path,
    title: str,
    browser_date: str | None,
    browser_time: str | None,
    meta_file_path: str,
) -> None:
    """Schedule the Short on TikTok Studio. Resume-safe."""
    meta_path = Path(meta_file_path)
    meta_now = _read_meta(meta_path)
    if meta_now.get("tiktok_status") == "scheduled":
        print("  [tiktok] already scheduled", flush=True)
        return
    if not browser_date or not browser_time:
        print("  [tiktok] no schedule slot; skipping", flush=True)
        return

    from tiktok_studio_navigator import (
        DEFAULT_PROFILE_DIR as DEFAULT_TIKTOK_PROFILE_DIR,
        run_schedule_flow as run_tiktok_schedule_flow,
    )
    profile_dir = _UTIL_ROOT / DEFAULT_TIKTOK_PROFILE_DIR
    print("Scheduling TikTok...", flush=True)
    run_tiktok_schedule_flow(
        video_path=video,
        schedule_date=browser_date,
        schedule_time=browser_time,
        profile_dir=profile_dir,
        headed=False,
        hold_seconds=0,
        upload_timeout_seconds=SOCIAL_UPLOAD_TIMEOUT_SECONDS,
        submit=True,
        caption=title,
        output_path=None,
    )
    _write_meta(meta_path, tiktok_status="scheduled")
    print("  TikTok scheduled", flush=True)


def _run_instagram(
    video: Path,
    title: str,
    browser_date: str | None,
    browser_time: str | None,
    meta_file_path: str,
) -> None:
    """Schedule the Short as an Instagram Reel. Resume-safe."""
    meta_path = Path(meta_file_path)
    meta_now = _read_meta(meta_path)
    if meta_now.get("instagram_status") == "scheduled":
        print("  [instagram] already scheduled", flush=True)
        return
    if not browser_date or not browser_time:
        print("  [instagram] no schedule slot; skipping", flush=True)
        return

    from instagram_business_navigator import (
        DEFAULT_ASSET_ID,
        DEFAULT_BUSINESS_ID,
        DEFAULT_PROFILE_DIR as DEFAULT_INSTAGRAM_PROFILE_DIR,
        run_schedule_flow as run_instagram_schedule_flow,
    )
    profile_dir = _UTIL_ROOT / DEFAULT_INSTAGRAM_PROFILE_DIR
    print("Scheduling Instagram Reel...", flush=True)
    run_instagram_schedule_flow(
        video_path=video,
        schedule_date=browser_date,
        schedule_time=browser_time,
        profile_dir=profile_dir,
        headed=False,
        hold_seconds=0,
        upload_timeout_seconds=SOCIAL_UPLOAD_TIMEOUT_SECONDS,
        submit=True,
        asset_id=DEFAULT_ASSET_ID,
        business_id=DEFAULT_BUSINESS_ID,
        caption=title,
        output_path=None,
    )
    _write_meta(meta_path, instagram_status="scheduled")
    print("  Instagram scheduled", flush=True)


if __name__ == "__main__":
    main()
