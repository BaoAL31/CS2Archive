"""
Upload a video to YouTube with a thumbnail.

Usage:
    python scripts/upload/upload_youtube.py <video_path> --thumbnail <image.png> --title <title> [--description <desc>] [--privacy private|unlisted|public]

Example:
    python scripts/upload/upload_youtube.py "youtube/faze-vs-vitality-iem-atlanta-2026_ropz_nuke/video.mp4" --thumbnail "youtube/faze-vs-vitality-iem-atlanta-2026_ropz_nuke/thumbnail.png" --title "ropz | 1.54 Rating | FaZe vs Vitality | Nuke | IEM Atlanta 2026"
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import random
import ssl
import sys
import time
from pathlib import Path

import google.auth
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
import httplib2

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from youtube_schedule import (
    AUTO_PUBLISH_MODE,
    AUTO_PUBLISH_TIMES,
    DEFAULT_PUBLISH_TZ,
    resolve_publish_schedule,
)

RETRIABLE_EXCEPTIONS = (
    httplib2.HttpLib2Error, IOError, ssl.SSLError, http.client.NotConnected,
    http.client.IncompleteRead, http.client.ImproperConnectionState,
    http.client.CannotSendRequest, http.client.CannotSendHeader,
    http.client.ResponseNotReady, http.client.BadStatusLine,
)
RETRIABLE_STATUS_CODES = [500, 502, 503, 504]
MAX_RETRIES = 20

SCOPES = ["https://www.googleapis.com/auth/youtube"]
THUMB_SCOPES = ["https://www.googleapis.com/auth/youtube"]
CLIENT_SECRET = "client_secret.json"
TOKEN_FILE = "token_youtube.json"
THUMB_TOKEN_FILE = "token_youtube_thumb.json"
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def get_authenticated_service(scopes: list[str] | None = None, token_file: str | None = None) -> googleapiclient.discovery.Resource:
    scopes = scopes or SCOPES
    token_file = token_file or TOKEN_FILE
    creds = None
    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, scopes)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CLIENT_SECRET):
                print(f"[ERROR] {CLIENT_SECRET} not found. Download it from Google Cloud Console.")
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET, scopes)
            creds = flow.run_local_server(port=random.randint(5000, 9999), open_browser=True)
        with open(token_file, "w") as f:
            f.write(creds.to_json())
    return build("youtube", "v3", credentials=creds)


def get_youtube_publish_dates(youtube, *, exclude_shorts: bool = True) -> set[str]:
    """Return set of YYYY-MM-DD dates with scheduled or published long-form vids.

    Queries channel uploads playlist (paginated), then ``videos().list`` in
    batches of 50 to fetch ``status.publishAt`` (actual public date for
    scheduled vids) and ``status.privacyStatus``. Skips Shorts and unlisted
    when *exclude_shorts* is True (default). Raises on API error — no silent fallback.
    """
    from datetime import datetime
    channels = youtube.channels().list(part="contentDetails", mine=True).execute()
    if not channels.get("items"):
        return set()
    uploads_id = channels["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]

    video_ids: list[str] = []
    page_token: str | None = None
    while True:
        resp = youtube.playlistItems().list(
            part="snippet",
            playlistId=uploads_id,
            maxResults=50,
            pageToken=page_token,
        ).execute()
        for item in resp.get("items", []):
            vid = (
                item.get("contentDetails", {}).get("videoId")
                or item.get("snippet", {}).get("resourceId", {}).get("videoId")
            )
            if vid:
                video_ids.append(vid)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    if not video_ids:
        return set()

    import re

    def _is_short(v: dict) -> bool:
        """True if this video is a YouTube Short.

        Shorts are vertical clips YouTube classifies as such: either tagged
        with #Shorts, or <= 3 minutes (YouTube's Short duration limit). They
        MUST be excluded from the long-form publish schedule so they don't
        consume a daily long-form slot — long-form POV matches (20+ min) are
        the only uploads that should reserve one.
        """
        title = v.get("snippet", {}).get("title", "")
        if "#Shorts" in title or "#shorts" in title:
            return True
        dur = v.get("contentDetails", {}).get("duration", "")
        if dur:
            # ISO 8601 duration: PT#H#M#S (Shorts are < 3 min, so no H).
            m = re.match(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$", dur)
            if m:
                total = (int(m.group(1) or 0) * 3600
                         + int(m.group(2) or 0) * 60
                         + int(m.group(3) or 0))
                if total <= 180:
                    return True
        return False

    occupied: set[str] = set()
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i + 50]
        resp = youtube.videos().list(
            part="status,snippet,contentDetails",
            id=",".join(batch),
        ).execute()
        for v in resp.get("items", []):
            if exclude_shorts and _is_short(v):
                continue
            st = v.get("status", {})
            privacy = st.get("privacyStatus", "public")
            if privacy == "unlisted":
                continue
            pub = st.get("publishAt") or v.get("snippet", {}).get("publishedAt", "")
            if not pub:
                continue
            dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
            try:
                from zoneinfo import ZoneInfo
                local_dt = dt.astimezone(ZoneInfo(DEFAULT_PUBLISH_TZ))
                occupied.add(local_dt.strftime("%Y-%m-%dT%H:%M"))
            except Exception:
                occupied.add(dt.strftime("%Y-%m-%dT%H:%M"))
    return occupied


def last_long_form_upload_date(youtube) -> date | None:
    """Return date+1 of latest long-form publish date. Raises on error.

    Kept for backwards compat. New code should use
    ``get_youtube_publish_dates`` which returns full set.
    """
    from datetime import timedelta
    dates = get_youtube_publish_dates(youtube)
    if not dates:
        return None
    latest = max(dates)
    return latest + timedelta(days=1)


def _session_path(video_path: str) -> str:
    return video_path + ".upload_session.json"


def _meta_path(video_path: str) -> str:
    """Path to upload_meta.json alongside the video file."""
    return str(Path(video_path).parent / "upload_meta.json")


def _record_publish_meta(
    meta_file_path: str,
    publish_tz: str,
    publish_at_utc_fallback: str,
) -> None:
    """Write publish fields from ground truth (response publishAt) if present,
    else the freshly resolved fallback. Local wall-clock is derived from UTC
    so meta never contradicts what YouTube actually scheduled."""
    path = Path(meta_file_path)
    if not path.exists():
        return
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
        actual = meta.get("publish_at_utc") or publish_at_utc_fallback
        meta["publish_at_utc"] = actual
        try:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            dt = datetime.fromisoformat(actual.replace("Z", "+00:00"))
            local = dt.astimezone(ZoneInfo(publish_tz))
            meta["publish_at"] = local.strftime("%Y-%m-%d %H:%M")
            meta["publish_timezone"] = publish_tz
        except Exception:
            pass
        path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    except Exception:
        pass


def upload_video(
    youtube, video_path: str, title: str, description: str,
    privacy: str, thumbnail_path: str | None = None,
    tags: list[str] | None = None, meta_path: str | None = None,
    publish_at_utc: str | None = None,
) -> str:
    status: dict = {
        "privacyStatus": privacy,
        "selfDeclaredMadeForKids": False,
    }
    if publish_at_utc:
        status["privacyStatus"] = "private"
        status["publishAt"] = publish_at_utc

    body: dict = {
        "snippet": {
            "title": title,
            "description": description,
            "categoryId": "20",
        },
        "status": status,
    }
    if tags:
        body["snippet"]["tags"] = tags

    media = MediaFileUpload(video_path, chunksize=32 * 1024 * 1024, resumable=True)
    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )

    # Restore saved session from meta file (crash recovery)
    mp = meta_path or _meta_path(video_path)
    # Also check legacy session file for backward compat
    legacy_sp = _session_path(video_path)
    candidates = [mp, legacy_sp]
    for candidate in candidates:
        if os.path.exists(candidate):
            mp = candidate
            break
    if os.path.exists(mp):
        try:
            with open(mp) as f:
                session = json.load(f)
            stored_path = os.path.normpath(session.get("video_path", ""))
            input_path = os.path.normpath(video_path)
            if (stored_path == input_path
                    and session.get("video_size") == media.size()
                    and session.get("resumable_uri")):
                request.resumable_uri = session["resumable_uri"]
                request.resumable_progress = session["resumable_progress"]
                pct = int(session["resumable_progress"] / media.size() * 100) if media.size() else 0
                print(f"  Resuming at {pct}% ({session['resumable_progress']}/{media.size()} bytes)", flush=True)
        except Exception as e:
            print(f"  Could not resume: {e}", flush=True)

    response = None
    error = None
    retry = 0
    last_progress = 0
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                pct = int(status.progress() * 100)
                if pct != last_progress:
                    print(f"  Upload: {pct}%", flush=True)
                    last_progress = pct
                # Persist session in meta file for crash recovery
                if request.resumable_uri:
                    try:
                        meta = {}
                        if os.path.exists(mp):
                            with open(mp) as f:
                                meta = json.load(f)
                        meta["video_path"] = os.path.normpath(video_path)
                        meta["resumable_uri"] = request.resumable_uri
                        meta["resumable_progress"] = request.resumable_progress
                        meta["video_size"] = media.size()
                        with open(mp, "w") as f:
                            json.dump(meta, f, indent=2)
                    except Exception:
                        pass
        except HttpError as e:
            if e.resp.status in RETRIABLE_STATUS_CODES:
                error = f"A retriable HTTP error {e.resp.status} occurred:\n{e.content}"
            else:
                raise
        except RETRIABLE_EXCEPTIONS as e:
            error = f"A retriable error occurred: {e}"

        if error is not None:
            print(f"  {error}", flush=True)
            retry += 1
            if retry > MAX_RETRIES:
                print(f"  [ERROR] Giving up after {MAX_RETRIES} retries", flush=True)
                sys.exit(1)
            max_sleep = 2 ** retry
            sleep_seconds = random.random() * max_sleep
            print(f"  Retrying in {sleep_seconds:.1f}s...", flush=True)
            time.sleep(sleep_seconds)
            error = None

    # Success — clear resumable fields, record youtube_id in meta file
    try:
        with open(mp, "r+") as f:
            meta = json.load(f)
        meta.pop("resumable_uri", None)
        meta.pop("resumable_progress", None)
        meta.pop("video_size", None)
        meta["youtube_id"] = response.get("id")
        meta["upload_status"] = "completed"
        # Record the ACTUAL scheduled publish time from the upload response.
        # A resumed session keeps the publishAt committed at session creation,
        # which may differ from a freshly resolved slot — the response is the
        # only ground truth (the freshly resolved value can be a lie).
        actual_pub = (response.get("status") or {}).get("publishAt")
        if actual_pub:
            meta["publish_at_utc"] = actual_pub
        elif publish_at_utc and not meta.get("publish_at_utc"):
            meta["publish_at_utc"] = publish_at_utc
        with open(mp, "w") as f:
            json.dump(meta, f, indent=2)
    except Exception as e:
        print(f"  [WARN] failed to write youtube_id into meta: {e}", flush=True)

    # Clean up legacy session file if it exists
    try:
        os.remove(_session_path(video_path))
    except Exception:
        pass

    video_id = response.get("id")
    print(f"  Uploaded: https://youtu.be/{video_id}", flush=True)

    if thumbnail_path and video_id:
        try:
            youtube.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(thumbnail_path),
            ).execute()
            print(f"  Thumbnail set", flush=True)
        except Exception as e:
            print(f"  Thumbnail failed: {e}", flush=True)

    return video_id


def youtube_upload_completed(meta: dict) -> bool:
    """True when this meta already has a finished YouTube object."""
    return bool(meta.get("youtube_id") and meta.get("upload_status") == "completed")


def _update_thumbnail(youtube, video_id: str, thumb_path: str) -> None:
    youtube.thumbnails().set(
        videoId=video_id,
        media_body=MediaFileUpload(thumb_path),
    ).execute()
    print(f"  Thumbnail updated for https://youtu.be/{video_id}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Upload video to YouTube or update a thumbnail")
    parser.add_argument("video", nargs="?", help="Path to video file")
    parser.add_argument("--thumbnail", "-t", help="Path to thumbnail image (PNG)")
    parser.add_argument("--title", help="Video title")
    parser.add_argument("--description", "-d", default="", help="Video description")
    parser.add_argument("--tags", help="Comma-separated tags (max 500 chars total)")
    parser.add_argument("--privacy", choices=["private", "unlisted", "public"], default="unlisted")
    parser.add_argument("--meta", help="Path to upload_meta.json (overrides --title, --description, --tags)")
    parser.add_argument(
        "--publish-at",
        default=None,
        help="Schedule publish: 'auto' = next future 16:30 in --timezone (default), or wall-clock like '2026-06-12 17:00'",
    )
    parser.add_argument(
        "--timezone",
        default=DEFAULT_PUBLISH_TZ,
        help=f"IANA timezone for --publish-at (default: {DEFAULT_PUBLISH_TZ})",
    )
    parser.add_argument("--update-thumbnail", help="Update thumbnail for an existing video ID")
    parser.add_argument(
        "--also-bilibili",
        action="store_true",
        help="After a successful YouTube upload, also upload to bilibili.tv "
             "(requires .bilibili_storage.json; uses scripts/upload/upload_bilibili.py)",
    )
    parser.add_argument(
        "--bilibili-only",
        action="store_true",
        help="Skip YouTube; only upload to bilibili.tv from --meta / video",
    )
    args = parser.parse_args()

    # Standalone thumbnail update mode
    if args.update_thumbnail:
        if not args.thumbnail:
            print("[ERROR] --thumbnail required with --update-thumbnail", flush=True)
            sys.exit(1)
        if not Path(args.thumbnail).exists():
            print(f"[ERROR] Thumbnail not found: {args.thumbnail}", flush=True)
            sys.exit(1)
        print("Authenticating with Google...", flush=True)
        youtube = get_authenticated_service(scopes=THUMB_SCOPES, token_file=THUMB_TOKEN_FILE)
        _update_thumbnail(youtube, args.update_thumbnail, args.thumbnail)
        print("Done!", flush=True)
        return

    # Upload mode
    if not args.video and not args.meta and not args.bilibili_only:
        print("[ERROR] <video> path required (use --update-thumbnail to only set a thumbnail)", flush=True)
        sys.exit(1)

    meta = {}
    meta_path_obj: Path | None = None
    if args.meta:
        meta_path_obj = Path(args.meta)
        if not meta_path_obj.exists():
            print(f"[ERROR] Meta file not found: {meta_path_obj}")
            sys.exit(1)
        meta = json.loads(meta_path_obj.read_text())

    resuming = bool(meta.get("resumable_uri"))

    video = Path(args.video or meta.get("video_path", ""))
    if not video.exists():
        print(f"[ERROR] Video not found: {video}")
        sys.exit(1)

    if not meta_path_obj and (video.parent / "upload_meta.json").exists():
        meta_path_obj = video.parent / "upload_meta.json"
        meta = json.loads(meta_path_obj.read_text())

    title = args.title or meta.get("title")
    if not title:
        print("[ERROR] No title provided (use --title or ensure upload_meta.json exists)")
        sys.exit(1)

    description = args.description or meta.get("description", "")
    privacy = args.privacy or meta.get("privacy", "unlisted")
    tags = None
    if args.tags:
        tags = [t.strip() for t in args.tags.split(",") if t.strip()]
    elif meta.get("tags"):
        tags = meta["tags"]

    if tags:
        total = sum(len(t) for t in tags)
        if total > 500:
            print(f"[WARN] Tags too long ({total} chars), truncating to 500", flush=True)
            while tags and sum(len(t) for t in tags) > 500:
                tags.pop()

    thumbnail = args.thumbnail or meta.get("thumbnail_path")
    if thumbnail and not Path(thumbnail).exists():
        print(f"[ERROR] Thumbnail not found: {thumbnail}")
        sys.exit(1)

    meta_file_path = str(meta_path_obj or (video.parent / "upload_meta.json"))

    def _run_bilibili() -> None:
        from upload_bilibili import is_bilibili_pending, resolve_publish, upload_to_bilibili

        meta_now = meta
        if meta_path_obj and meta_path_obj.exists():
            meta_now = json.loads(meta_path_obj.read_text(encoding="utf-8"))
        if not is_bilibili_pending(meta_now):
            print(
                f"  [bili] already completed aid={meta_now.get('bilibili_aid')}",
                flush=True,
            )
            return
        thumb_path = Path(thumbnail) if thumbnail else None
        if thumb_path and not thumb_path.exists():
            alt = thumb_path.with_suffix(".jpg")
            thumb_path = alt if alt.exists() else None
        print("Uploading to bilibili.tv...", flush=True)
        # Prefer schedule already written into meta (CLI --publish-at / auto).
        pub = resolve_publish(meta_now)
        aid = upload_to_bilibili(
            video,
            title=title,
            description=description,
            tags=list(tags or []),
            thumbnail_path=thumb_path,
            meta_path=meta_path_obj,
            publish_at=pub,
            variant=meta_now.get("variant"),
        )
        print(f"  Bilibili aid={aid}", flush=True)

    yt_done = youtube_upload_completed(meta)

    if args.bilibili_only:
        try:
            _run_bilibili()
            print("Done!", flush=True)
        except Exception as e:
            print(f"[ERROR] bilibili upload failed: {e}", flush=True)
            sys.exit(1)
        return

    if yt_done:
        print(
            f"  [skip] YouTube already completed id={meta.get('youtube_id')}",
            flush=True,
        )
        if args.also_bilibili:
            try:
                _run_bilibili()
            except Exception as e:
                print(f"[ERROR] bilibili upload failed: {e}", flush=True)
                sys.exit(1)
        print("Done!", flush=True)
        return

    print("Authenticating with Google...", flush=True)
    youtube = get_authenticated_service()

    if resuming:
        # Resumed session already has its status/publishAt metadata committed
        # at session creation. Re-resolving would both print a misleading
        # schedule and occupy a slot that the video will never use — the
        # response's actual publishAt is recorded instead.
        args.publish_at = meta.get("publish_at")
    elif args.publish_at is None and "publish_at" not in meta:
        args.publish_at = AUTO_PUBLISH_MODE

    original_privacy = privacy
    from datetime import date as _date
    start_date: _date | None = None
    yt_publish_dates: set[str] = set()
    if args.publish_at == AUTO_PUBLISH_MODE:
        yt_publish_dates = get_youtube_publish_dates(youtube) or set()
        if yt_publish_dates:
            print(
                f"  [Schedule] YouTube: {len(yt_publish_dates)} occupied slots",
                flush=True,
            )
        else:
            print("  [Schedule] No existing videos found on channel — no occupied dates to reserve", flush=True)
    occupied_dates: set[str] = set()
    if args.publish_at == AUTO_PUBLISH_MODE:
        occupied_dates = set(yt_publish_dates)
    privacy, publish_at_utc, publish_tz, publish_local = resolve_publish_schedule(
            publish_at=args.publish_at,
            timezone=args.timezone,
            meta=meta,
            privacy=privacy,
            start_date=start_date,
            occupied_dates=occupied_dates,
            publish_times=AUTO_PUBLISH_TIMES,
        )

    if publish_at_utc:
        if original_privacy != "private":
            print(
                f"  [WARN] Scheduled publish requires private upload; "
                f"overriding privacy {original_privacy!r} -> 'private'",
                flush=True,
            )
        print(
            f"  Scheduled publish: {publish_local} ({publish_tz}) -> {publish_at_utc} UTC",
            flush=True,
        )

    print("Uploading...", flush=True)
    upload_video(
        youtube, str(video), title, description,
        privacy, thumbnail, tags, meta_path=meta_file_path,
        publish_at_utc=publish_at_utc,
    )
    if publish_at_utc:
        # Fresh upload: derive display fields from the response's actual
        # publishAt (already in meta via upload_video). Resume case needs no
        # call — upload_video recorded the session-committed time directly.
        _record_publish_meta(meta_file_path, publish_tz, publish_at_utc)
    if args.also_bilibili:
        try:
            _run_bilibili()
        except Exception as e:
            print(f"[ERROR] bilibili upload failed: {e}", flush=True)
            sys.exit(1)
    print("Done!", flush=True)


if __name__ == "__main__":
    main()
