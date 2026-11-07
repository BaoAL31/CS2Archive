"""
Upload a YouTube Short (plus TikTok, Instagram, and Facebook Page by default).

YouTube and TikTok schedule via their own APIs/UIs against the shared
CS2UtilArchive slot pool. Facebook Page Reels use Graph native schedule at
the same ``publish_at_utc``. Instagram Graph cannot schedule, so a one-shot
Windows task runs ``publish_due_social.py`` at that slot. Resume-safe: a
completed platform upload is skipped on re-run.

Usage:
    python cs2archive/upload/upload_youtube_shorts.py <short.mp4> --meta upload_meta_shorts.json
    python cs2archive/upload/upload_youtube_shorts.py youtube/.../short.mp4 --publish-at "2026-06-12 17:00"

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
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_UTIL_SCRIPTS = Path(r"D:\Projects\CS2UtilArchive\scripts")
_UTIL_ROOT = _UTIL_SCRIPTS.parent

# CR-01: this module's own imports are package-relative now, so the old
# iterate-and-insert sys.path block is gone. It had to go: the codemod left it
# remove-only, which stripped the sibling's scripts dir from sys.path.
# Nothing here needs sys.path unless a CS2UtilArchive module is imported directly --
# if that changes, call cs2archive.overlay._common.prefer_cs2util_scripts().

from cs2archive.upload.upload_youtube import (  # noqa: E402
    _record_publish_meta,
    get_authenticated_service,
    get_youtube_publish_dates,
    upload_video,
)
from cs2archive.upload.youtube_schedule import DEFAULT_PUBLISH_TZ, resolve_publish_schedule  # noqa: E402
from cs2archive.upload.shorts_player_day import player_blocked_slots, pov_nick_from_meta_path  # noqa: E402

SHORTS_META_NAME = "upload_meta_shorts.json"
SHORTS_VIDEO_NAME = "short.mp4"
SHORTS_COVER_NAME = "cover.png"

# Partial uploads (dropping a platform) are an operator decision, not a default.
# The --skip-* flags stay available for a genuinely broken platform, but they are
# hidden from --help and inert unless this env var is set, so an agent cannot
# narrow a Short to one platform on its own initiative. See
# docs/agents/shorts-picker.md ("Uploading is all four platforms, always").
PARTIAL_UPLOAD_ENV = "CS2ARCHIVE_ALLOW_PARTIAL_UPLOAD"
PLATFORM_FLAGS = {
    "tiktok": "--skip-tiktok",
    "instagram": "--skip-instagram",
    "facebook": "--skip-facebook",
}


def partial_upload_authorized() -> bool:
    return os.environ.get(PARTIAL_UPLOAD_ENV, "").strip().lower() in {"1", "true", "yes"}


def refuse_unauthorized_partial_upload(skipped: list[str]) -> None:
    """Exit loudly when a platform is dropped without operator authorization."""
    if not skipped or partial_upload_authorized():
        return
    flags = ", ".join(PLATFORM_FLAGS[name] for name in skipped)
    print(
        f"[ERROR] refusing a partial upload ({flags}).\n"
        "  A Short is published to YouTube, TikTok, Instagram and Facebook; dropping\n"
        "  a platform is an operator decision, not an agent one. Ask the user first.\n"
        f"  If they approve, re-run with {PARTIAL_UPLOAD_ENV}=1 in the environment.",
        flush=True,
    )
    sys.exit(2)


def ensure_shorts_hashtag(title: str, description: str) -> tuple[str, str]:
    """Return title/description unchanged.

    Per docs/agents/shorts-titles.md the title must NOT contain ``#Shorts``
    (Shorts are detected by vertical aspect ratio + duration, not the hashtag),
    so we intentionally never append it here.
    """
    return title, description


def shorts_occupied_dates(all_dates, long_form_dates) -> set[str]:
    """Dates blocking a Shorts 18:00 slot: other Shorts only.

    Long-form uploads (10:00/16:30 premieres) must never push a Short off
    its day — they share no shelf and no audience window. The per-player
    one-short-per-day rule lives separately in ``shorts_player_day``.
    """
    return set(all_dates or set()) - set(long_form_dates or set())


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
        "--tiktok-api",
        choices=["web", "draft", "direct"],
        default="web",
        help="TikTok upload mode (default: web): 'web' drives TikTok Studio in "
        "CloakBrowser (works while the Content Posting app is in review); "
        "'draft' uploads to the TikTok app inbox via the API for manual "
        "finishing; 'direct' publishes directly via the API (needs "
        "video.publish audit; unaudited = private-only)",
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
    # Hidden on purpose: dropping a platform needs operator authorization
    # (PARTIAL_UPLOAD_ENV), so these must not read as ordinary options.
    for _name, _flag in PLATFORM_FLAGS.items():
        parser.add_argument(
            _flag,
            action="store_true",
            help=argparse.SUPPRESS,
            dest=f"skip_{_name}",
        )
    args = parser.parse_args()

    skipped_platforms = [name for name in PLATFORM_FLAGS if getattr(args, f"skip_{name}")]
    refuse_unauthorized_partial_upload(skipped_platforms)

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
            occupied_all = get_youtube_publish_dates(youtube_pre, exclude_shorts=False) or set()
            long_form = get_youtube_publish_dates(youtube_pre, exclude_shorts=True) or set()
            occupied_dates = shorts_occupied_dates(occupied_all, long_form)
            occupied_tuples = (
                {(d.split("T")[0], d.split("T")[1][:5]) for d in occupied_dates if "T" in d}
                if occupied_dates
                else set()
            )
            # One Short per player per calendar day: if this POV already has a
            # Short booked that date, occupy the daily 18:00 slot so the next
            # clip of theirs lands on a later day.
            from cs2archive.overlay._common import prefer_cs2util_scripts
            prefer_cs2util_scripts()
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

    # Durable short -> demo linkage, keyed by the platform post id. Instrumentation
    # only: never fatal to the upload, retries capped at 3, loudly logged.
    _record_linkage(video, meta_path)

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
            from cs2archive.overlay._common import prefer_cs2util_scripts
            prefer_cs2util_scripts()
            from publish_schedule import wall_clock_to_local_schedule
            browser_date, browser_time = wall_clock_to_local_schedule(pdate, ptime, publish_tz)
        except Exception as exc:
            print(f"  [warn] could not convert slot for browser UIs: {exc}", flush=True)

    slot_utc = None
    meta_now = _read_meta(meta_path)
    slot_utc = meta_now.get("publish_at_utc") or publish_at_utc

    # Every Short goes to all four platforms. A platform is only dropped when the
    # operator authorized it above; the drop is recorded in the meta so the
    # pending runner treats it as settled and an audit can find it later.
    social_failures: list[str] = []
    for name, run in (
        (
            "tiktok",
            lambda: _run_tiktok(
                video,
                title,
                browser_date,
                browser_time,
                meta_file_path,
                mode=args.tiktok_api,
            ),
        ),
        (
            "instagram",
            lambda: _run_instagram(video, title, meta_file_path, publish_at_utc=slot_utc),
        ),
        (
            "facebook",
            lambda: _run_facebook(
                video, title, description, meta_file_path, publish_at_utc=slot_utc
            ),
        ),
    ):
        if name in skipped_platforms:
            print(f"  [{name}] skipped by operator ({PARTIAL_UPLOAD_ENV}=1)", flush=True)
            continue
        try:
            run()
        except Exception as exc:  # noqa: BLE001 - one platform must not block the rest
            print(f"  [{name}][FAILED] {exc}", flush=True)
            social_failures.append(name)

    if skipped_platforms:
        _write_meta(meta_path, skipped_platforms=skipped_platforms)

    if social_failures:
        print(
            f"\n[ERROR] cross-post incomplete: {', '.join(social_failures)} failed. "
            "Re-run this command to retry only the missing platforms.",
            flush=True,
        )
        sys.exit(1)


def _read_meta(meta_path: Path) -> dict:
    try:
        return json.loads(meta_path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def _record_linkage(video, meta_path) -> None:
    """Attach the platform post id(s) to the staged demo linkage.

    Non-fatal by design: a missing linkage record or a write failure logs loudly
    and the upload continues (a backfill can recover the row later).
    """
    if os.environ.get("CS2ARCHIVE_NO_LINKAGE"):
        return
    try:
        from cs2archive.shorts.social.linkage import record_upload_linkage
    except Exception as exc:  # noqa: BLE001 - optional instrumentation
        print(f"[linkage][WARN] import failed: {exc}", flush=True)
        return
    meta = _read_meta(meta_path)
    pairs = [("youtube", meta.get("youtube_id"))]
    if meta.get("instagram_id"):
        pairs.append(("instagram", meta.get("instagram_id")))
    if meta.get("facebook_id"):
        pairs.append(("facebook", meta.get("facebook_id")))
    for platform, post_id in pairs:
        if not post_id:
            continue
        record_upload_linkage(video, platform=platform, post_id=str(post_id),
                              meta_path=meta_path,
                              log=lambda msg: print(msg, flush=True))


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


def _pin_tiktok_fingerprint(seed: int = 73491) -> None:
    """Pin the CloakBrowser fingerprint seed for TikTok.

    cloakbrowser randomizes ``--fingerprint`` per launch; TikTok binds the
    session to the device fingerprint, so a fresh seed logs the account out
    again. Pin one seed for both the login and every Studio run, so the
    persisted session survives. No-op when the sibling module is absent.
    """
    try:
        import cloakbrowser.browser as _cb_browser
    except Exception:
        return
    real = _cb_browser.get_default_stealth_args

    def _pinned() -> list[str]:
        return [
            (f"--fingerprint={seed}" if a.startswith("--fingerprint=") else a)
            for a in real()
        ]

    _cb_browser.get_default_stealth_args = _pinned


def _run_tiktok(
    video: Path,
    title: str,
    browser_date: str | None,
    browser_time: str | None,
    meta_file_path: str,
    *,
    mode: str = "web",
) -> None:
    """Upload the Short to TikTok. Resume-safe.

    ``web`` (default) drives TikTok Studio in CloakBrowser — the same system
    as CS2UtilArchive's upload_short.py default, used while the Content
    Posting app is still in review. ``draft``/``direct`` fall back to the
    sibling ``tiktok_publish.run_tiktok`` API path (draft = app inbox for
    manual finishing; direct = DIRECT_POST, future slots queue a Windows
    task)."""
    meta_path = Path(meta_file_path)
    # Slot: prefer the committed UTC slot, fall back to the local wall-clock.
    slot = None
    meta_now = _read_meta(meta_path)
    if meta_now.get("publish_at_utc"):
        slot = _aware_utc(meta_now["publish_at_utc"])
    if slot is None and browser_date and browser_time:
        try:
            tzname = meta_now.get("publish_timezone") or DEFAULT_PUBLISH_TZ
            slot = datetime.strptime(f"{browser_date} {browser_time}", "%Y-%m-%d %H:%M").replace(
                tzinfo=ZoneInfo(tzname)
            )
        except Exception as exc:
            print(f"  [tiktok] could not parse slot ({exc}); skipping", flush=True)
            return
    if slot is None:
        print("  [tiktok] no schedule slot; skipping", flush=True)
        return

    from cs2archive.overlay._common import prefer_cs2util_scripts

    prefer_cs2util_scripts()

    if mode == "web":
        from tiktok_studio_navigator import (
            DEFAULT_PROFILE_DIR as DEFAULT_TIKTOK_PROFILE_DIR,
            run_schedule_flow as run_tiktok_schedule_flow,
        )

        _pin_tiktok_fingerprint()

        tzname = meta_now.get("publish_timezone") or DEFAULT_PUBLISH_TZ
        # Committed metadata stores UTC; TikTok's picker takes local wall time.
        # Formatting UTC directly selected 07:00 instead of 18:00 Sydney.
        slot = slot.astimezone(ZoneInfo(tzname))
        now_local = datetime.now(ZoneInfo(tzname))
        if slot <= now_local:
            # Slot already passed (instant YouTube publish): schedule TikTok
            # 25+ minutes out — Studio's scheduler requires a >=20 min lead
            # and its time picker only accepts minute multiples of 5.
            slot = now_local + timedelta(minutes=25)
            print(f"  [tiktok] slot passed; scheduling web post at "
                  f"{slot:%Y-%m-%d %H:%M} ({tzname})", flush=True)
        # TikTok's time picker only accepts minute multiples of 5.
        over = slot.minute % 5
        if over or slot.second or slot.microsecond:
            slot += timedelta(minutes=5 - over)
        try:
            run_tiktok_schedule_flow(
                video_path=video,
                schedule_date=f"{slot:%Y-%m-%d}",
                schedule_time=f"{slot:%H:%M}",
                profile_dir=Path(DEFAULT_TIKTOK_PROFILE_DIR),
                headed=True,
                hold_seconds=0,
                upload_timeout_seconds=900,
                submit=True,
                caption=title,
                output_path=None,
            )
        except Exception as exc:
            print(f"  [tiktok] web flow failed: {exc}", flush=True)
            raise
        _write_meta(meta_path, tiktok_status="scheduled",
                    tiktok_publish_at=f"{slot:%Y-%m-%d %H:%M} ({tzname})")
        print("  [tiktok] web flow done — scheduled in TikTok Studio", flush=True)
        return

    from tiktok_publish import run_tiktok

    run_tiktok(video=video, caption=title, slot=slot, meta_path=meta_path, mode=mode)


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
        from cs2archive.upload.meta_graph import MetaGraph

        print("Publishing Instagram Reel via Graph...", flush=True)
        result = MetaGraph().publish_ig_reel_file(video, caption=title)
        ig_id = result.get("id")
        _write_meta(meta_path, instagram_status="published", instagram_id=ig_id)
        print(f"  Instagram published id={ig_id}", flush=True)
        return

    from cs2archive.upload.publish_due_social import schedule_due_meta

    assert slot is not None
    try:
        task_name = schedule_due_meta(meta_path, slot)
    except Exception as exc:
        print(f"  [instagram] Windows task failed: {exc}", flush=True)
        print(
            "  [instagram] queued anyway — run "
            f"python cs2archive/upload/publish_due_social.py --meta {meta_path} at the slot",
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

    from cs2archive.upload.meta_graph import MetaGraph

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
