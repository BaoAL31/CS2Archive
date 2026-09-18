"""
Upload a YouTube Short (plus TikTok, Instagram, and Facebook Page by default).

YouTube and TikTok schedule via their own APIs/UIs against the shared
CS2UtilArchive slot pool. Facebook Page Reels use Graph native schedule at
the same ``publish_at_utc``. Instagram Graph cannot schedule, so a one-shot
Windows task runs ``publish_due_social.py`` at that slot. Resume-safe: a
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
from datetime import datetime, timedelta, timezone
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
from shorts_player_day import player_blocked_slots, pov_nick_from_meta_path  # noqa: E402

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
        "--skip-facebook",
        action="store_true",
        help="Skip the Facebook Page Reel upload (default: Graph publish after Instagram)",
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
            # One Short per player per calendar day: if this POV already has a
            # Short booked that date, occupy the daily 18:00 slot so the next
            # clip of theirs lands on a later day.
            from publish_schedule import SLOT_TIMES, find_next_upload_slot
            nick = pov_nick_from_meta_path(meta_path)
            if nick:
                occupied_tuples |= player_blocked_slots(
                    Path(__file__).resolve().parents[2] / "renders",
                    nick,
                    publish_tz,
                    SLOT_TIMES,
                    exclude_meta=meta_path,
                )
            date_str, time_str = find_next_upload_slot(occupied=occupied_tuples)
            publish_setting = f"{date_str} {time_str}"
            extra = f" (skipping days already booked for {nick})" if nick else ""
            print(
                f"  Auto Shorts slot (shared CS2UtilArchive schedule): "
                f"{date_str} {time_str} ({args.timezone}){extra}",
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

    slot_utc = None
    meta_now = _read_meta(meta_path)
    slot_utc = meta_now.get("publish_at_utc") or publish_at_utc

    if not args.skip_tiktok:
        _run_tiktok(video, title, browser_date, browser_time, meta_file_path)

    if not args.skip_instagram:
        _run_instagram(video, title, meta_file_path, publish_at_utc=slot_utc)

    if not args.skip_facebook:
        _run_facebook(video, title, description, meta_file_path, publish_at_utc=slot_utc)


def _read_meta(meta_path: Path) -> dict:
    try:
        return json.loads(meta_path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def _write_meta(meta_path: Path, **fields) -> None:
    try:
        meta = _read_meta(meta_path)
        meta.update(fields)
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")  # no BOM

    except Exception as exc:
        print(f"  [meta] write warn: {exc}", flush=True)


def _utc_to_local_publish(publish_at_utc: str, tz: str) -> tuple[str, str]:
    utc_dt = datetime.fromisoformat(publish_at_utc.replace("Z", "+00:00"))
    local_dt = utc_dt.astimezone(ZoneInfo(tz))
    return local_dt.strftime("%Y-%m-%d"), local_dt.strftime("%H:%M")


def _aware_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


_FB_SCHEDULE_MIN = timedelta(minutes=10)
_FB_SCHEDULE_MAX = timedelta(days=29)
_IG_IMMEDIATE = timedelta(seconds=90)


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
    # preflight: fail fast if not logged in instead of hanging on upload
    try:
        from social_session_check import check_tiktok
        ok, msg = check_tiktok(profile_dir)
        if not ok:
            print(f"  [tiktok] {msg}", flush=True)
            print("  [tiktok] skipping — fix login then re-run upload_pending_shorts", flush=True)
            return
        print(f"  [tiktok] preflight OK: {msg}", flush=True)
    except Exception as e:
        print(f"  [tiktok] preflight warn (continuing): {e}", flush=True)
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
    meta_file_path: str,
    *,
    publish_at_utc: str | None,
) -> None:
    """Queue or publish an Instagram Reel via Graph. Resume-safe.

    Graph cannot natively schedule Reels. Future slots register a one-shot
    Windows task that runs ``publish_due_social.py`` at the YouTube time.
    """
    meta_path = Path(meta_file_path)
    meta_now = _read_meta(meta_path)
    if meta_now.get("instagram_status") == "published":
        print("  [instagram] already published", flush=True)
        return

    slot = _aware_utc(publish_at_utc)
    now = datetime.now(timezone.utc)
    due_now = slot is None or slot <= now + _IG_IMMEDIATE
    if (
        meta_now.get("instagram_status") == "queued"
        and not due_now
        and meta_now.get("instagram_due_task")
    ):
        print(f"  [instagram] already queued for {publish_at_utc}", flush=True)
        return

    if due_now:
        from meta_graph import MetaGraph

        print("Publishing Instagram Reel via Graph...", flush=True)
        result = MetaGraph().publish_ig_reel_file(video, caption=title)
        ig_id = result.get("id")
        _write_meta(meta_path, instagram_status="published", instagram_id=ig_id)
        print(f"  Instagram published id={ig_id}", flush=True)
        return

    from publish_due_social import schedule_due_meta

    assert slot is not None
    try:
        task_name = schedule_due_meta(meta_path, slot)
    except Exception as exc:
        print(f"  [instagram] Windows task failed: {exc}", flush=True)
        print(
            "  [instagram] queued anyway — run "
            f"python scripts/upload/publish_due_social.py --meta {meta_path} at the slot",
            flush=True,
        )
        task_name = None
    _write_meta(
        meta_path,
        instagram_status="queued",
        instagram_due_task=task_name,
        video_path=str(video.resolve()),
        title=title,
        publish_at_utc=publish_at_utc,
    )
    local = slot.astimezone()
    print(
        f"  Instagram queued for {local:%Y-%m-%d %H:%M} ({local.tzname()})"
        + (f" task={task_name}" if task_name else ""),
        flush=True,
    )


def _run_facebook(
    video: Path,
    title: str,
    description: str,
    meta_file_path: str,
    *,
    publish_at_utc: str | None,
) -> None:
    """Schedule a Facebook Page Reel via Graph (same slot as YouTube). Resume-safe."""
    meta_path = Path(meta_file_path)
    meta_now = _read_meta(meta_path)
    if meta_now.get("facebook_status") in {"scheduled", "published"}:
        print("  [facebook] already scheduled", flush=True)
        return

    from meta_graph import MetaGraph

    slot = _aware_utc(publish_at_utc)
    now = datetime.now(timezone.utc)
    scheduled_ts = None
    if slot is not None:
        delta = slot - now
        if _FB_SCHEDULE_MIN <= delta <= _FB_SCHEDULE_MAX:
            scheduled_ts = int(slot.timestamp())
        elif delta > timedelta(0):
            print(
                "  [facebook] slot is under 10 minutes away; Graph cannot "
                "schedule a Page Reel that soon — publishing now",
                flush=True,
            )

    if scheduled_ts:
        print("Scheduling Facebook Page Reel via Graph...", flush=True)
    else:
        print("Publishing Facebook Page Reel via Graph...", flush=True)
    result = MetaGraph().publish_page_reel_file(
        video,
        description=description or title,
        title=title,
        scheduled_publish_time=scheduled_ts,
    )
    fb_id = result.get("video_id") or result.get("id")
    status = "scheduled" if scheduled_ts else "published"
    _write_meta(meta_path, facebook_status=status, facebook_id=fb_id)
    if scheduled_ts and slot is not None:
        local = slot.astimezone()
        print(
            f"  Facebook Page scheduled id={fb_id} for {local:%Y-%m-%d %H:%M} ({local.tzname()})",
            flush=True,
        )
    else:
        print(f"  Facebook Page published id={fb_id}", flush=True)


if __name__ == "__main__":
    main()
