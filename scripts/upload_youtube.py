"""
Upload a video to YouTube with a thumbnail.

Usage:
    python scripts/upload_youtube.py <video_path> --thumbnail <image.png> --title <title> [--description <desc>] [--privacy private|unlisted|public]

Example:
    python scripts/upload_youtube.py "youtube/faze-vs-vitality-iem-atlanta-2026_ropz_nuke/video.mp4" --thumbnail "youtube/faze-vs-vitality-iem-atlanta-2026_ropz_nuke/thumbnail.png" --title "ropz | 1.54 Rating | FaZe vs Vitality | Nuke | IEM Atlanta 2026"
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import random
import sys
import time
from pathlib import Path

import google.auth
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
CLIENT_SECRET = "client_secret.json"
TOKEN_FILE = "token_youtube.json"


def get_authenticated_service() -> googleapiclient.discovery.Resource:
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CLIENT_SECRET):
                print(f"[ERROR] {CLIENT_SECRET} not found. Download it from Google Cloud Console.")
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET, SCOPES)
            creds = flow.run_local_server(port=random.randint(5000, 9999), open_browser=True)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("youtube", "v3", credentials=creds)


def upload_video(
    youtube, video_path: str, title: str, description: str,
    privacy: str, thumbnail_path: str | None = None,
) -> str:
    body = {
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

    media = MediaFileUpload(video_path, chunksize=256 * 1024 * 1024, resumable=True)
    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )

    response = None
    last_progress = 0
    while response is None:
        status, response = request.next_chunk()
        if status:
            pct = int(status.progress() * 100)
            if pct != last_progress:
                print(f"  Upload: {pct}%")
                last_progress = pct

    video_id = response.get("id")
    print(f"  Uploaded: https://youtu.be/{video_id}")

    if thumbnail_path and video_id:
        try:
            youtube.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(thumbnail_path),
            ).execute()
            print(f"  Thumbnail set")
        except Exception as e:
            print(f"  Thumbnail failed: {e}")

    return video_id


def main() -> None:
    parser = argparse.ArgumentParser(description="Upload video to YouTube")
    parser.add_argument("video", help="Path to video file")
    parser.add_argument("--thumbnail", "-t", help="Path to thumbnail image (PNG)")
    parser.add_argument("--title", required=True, help="Video title")
    parser.add_argument("--description", "-d", default="", help="Video description")
    parser.add_argument("--privacy", choices=["private", "unlisted", "public"], default="unlisted")
    args = parser.parse_args()

    video = Path(args.video)
    if not video.exists():
        print(f"[ERROR] Video not found: {video}")
        sys.exit(1)

    if args.thumbnail and not Path(args.thumbnail).exists():
        print(f"[ERROR] Thumbnail not found: {args.thumbnail}")
        sys.exit(1)

    print("Authenticating with Google...")
    youtube = get_authenticated_service()
    print("Uploading...")
    upload_video(
        youtube, str(video), args.title, args.description,
        args.privacy, args.thumbnail,
    )
    print("Done!")


if __name__ == "__main__":
    main()
