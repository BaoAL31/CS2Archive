"""
Upload a video to YouTube with a thumbnail.

Usage:
    python scripts/upload_youtube.py <video_path> --thumbnail <image.png> --title <title> [--description <desc>] [--privacy private|unlisted|public]

Example:
    python scripts/upload_youtube.py "youtube/faze-vs-vitality-iem-atlanta-2026_ropz_nuke/video.mp4" --thumbnail "youtube/faze-vs-vitality-iem-atlanta-2026_ropz_nuke/thumbnail.png" --title "ropz | 1.54 Rating | FaZe vs Vitality | Nuke | IEM Atlanta 2026"
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import pathlib
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

RETRIABLE_EXCEPTIONS = (
    httplib2.HttpLib2Error, IOError, ssl.SSLError, http.client.NotConnected,
    http.client.IncompleteRead, http.client.ImproperConnectionState,
    http.client.CannotSendRequest, http.client.CannotSendHeader,
    http.client.ResponseNotReady, http.client.BadStatusLine,
)
RETRIABLE_STATUS_CODES = [500, 502, 503, 504]
MAX_RETRIES = 20

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
THUMB_SCOPES = ["https://www.googleapis.com/auth/youtube"]
CLIENT_SECRET = "client_secret.json"
TOKEN_FILE = "token_youtube.json"
THUMB_TOKEN_FILE = "token_youtube_thumb.json"


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


def _session_path(video_path: str) -> str:
    return video_path + ".upload_session.json"


def _meta_path(video_path: str) -> str:
    """Path to upload_meta.json alongside the video file."""
    return str(Path(video_path).parent / "upload_meta.json")


def upload_video(
    youtube, video_path: str, title: str, description: str,
    privacy: str, thumbnail_path: str | None = None,
    tags: list[str] | None = None, meta_path: str | None = None,
) -> str:
    body: dict = {
        "snippet": {
            "title": title,
            "description": description,
            "categoryId": "20",
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
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
        with open(mp, "w") as f:
            json.dump(meta, f, indent=2)
    except Exception:
        pass

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
    parser.add_argument("--update-thumbnail", help="Update thumbnail for an existing video ID")
    args = parser.parse_args()

    print("Authenticating with Google...", flush=True)
    youtube = get_authenticated_service()

    # Standalone thumbnail update mode
    if args.update_thumbnail:
        if not args.thumbnail:
            print("[ERROR] --thumbnail required with --update-thumbnail", flush=True)
            sys.exit(1)
        if not Path(args.thumbnail).exists():
            print(f"[ERROR] Thumbnail not found: {args.thumbnail}", flush=True)
            sys.exit(1)
        youtube = get_authenticated_service(scopes=THUMB_SCOPES, token_file=THUMB_TOKEN_FILE)
        _update_thumbnail(youtube, args.update_thumbnail, args.thumbnail)
        print("Done!", flush=True)
        return

    # Upload mode
    if not args.video:
        print("[ERROR] <video> path required (use --update-thumbnail to only set a thumbnail)", flush=True)
        sys.exit(1)

    video = Path(args.video)
    if not video.exists():
        print(f"[ERROR] Video not found: {video}")
        sys.exit(1)

    meta = {}
    if args.meta:
        meta_path = Path(args.meta)
        if not meta_path.exists():
            print(f"[ERROR] Meta file not found: {meta_path}")
            sys.exit(1)
        meta = json.loads(meta_path.read_text())
    elif video.parent.name and (video.parent / "upload_meta.json").exists():
        meta_path = video.parent / "upload_meta.json"
        meta = json.loads(meta_path.read_text())

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

    meta_file_path = str(video.parent / "upload_meta.json")

    print("Uploading...", flush=True)
    upload_video(
        youtube, str(video), title, description,
        privacy, thumbnail, tags, meta_path=meta_file_path,
    )
    print("Done!", flush=True)


if __name__ == "__main__":
    main()
