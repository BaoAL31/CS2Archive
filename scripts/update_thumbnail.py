"""
Update an existing YouTube video's thumbnail.

Usage:
    python scripts/update_thumbnail.py <video_id> --thumbnail <image.png>
    python scripts/update_thumbnail.py keGvOCwAQUQ --thumbnail path/to/thumbnail.png
"""

from __future__ import annotations

import argparse
import os
import random
import sys
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


def get_authenticated_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CLIENT_SECRET):
                print(f"[ERROR] {CLIENT_SECRET} not found.")
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET, SCOPES)
            creds = flow.run_local_server(port=random.randint(5000, 9999), open_browser=True)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("youtube", "v3", credentials=creds)


def main():
    parser = argparse.ArgumentParser(description="Update YouTube video thumbnail")
    parser.add_argument("video_id", help="YouTube video ID")
    parser.add_argument("--thumbnail", "-t", required=True, help="Path to thumbnail image")
    args = parser.parse_args()

    thumb = Path(args.thumbnail)
    if not thumb.exists():
        print(f"[ERROR] Thumbnail not found: {thumb}")
        sys.exit(1)

    print("Authenticating...")
    youtube = get_authenticated_service()

    print(f"Setting thumbnail for video {args.video_id}...")
    try:
        youtube.thumbnails().set(
            videoId=args.video_id,
            media_body=MediaFileUpload(str(thumb)),
        ).execute()
        print(f"  Done! https://youtu.be/{args.video_id}")
    except Exception as e:
        print(f"[ERROR] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
