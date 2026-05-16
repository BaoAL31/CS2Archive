"""
Update an existing YouTube video's title, description, and thumbnail.

Usage:
    python scripts/update_video.py <video_id> --title "..." --description "..." --thumbnail <image.png>
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/youtube"]
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


def update_video(youtube, video_id: str, title: str, description: str):
    request = youtube.videos().list(
        part="snippet",
        id=video_id,
    )
    response = request.execute()
    if not response.get("items"):
        print(f"[ERROR] Video {video_id} not found")
        sys.exit(1)

    video = response["items"][0]
    video["snippet"]["title"] = title
    video["snippet"]["description"] = description

    update_request = youtube.videos().update(
        part="snippet",
        body=video,
    )
    update_request.execute()
    print(f"  Title & description updated")


def set_thumbnail(youtube, video_id: str, thumb_path: str):
    youtube.thumbnails().set(
        videoId=video_id,
        media_body=MediaFileUpload(thumb_path),
    ).execute()
    print(f"  Thumbnail set")


def main():
    parser = argparse.ArgumentParser(description="Update YouTube video metadata")
    parser.add_argument("video_id", help="YouTube video ID")
    parser.add_argument("--title", required=True, help="New video title")
    parser.add_argument("--description", "-d", default="", help="New video description")
    parser.add_argument("--thumbnail", "-t", help="Path to new thumbnail image")
    args = parser.parse_args()

    if args.thumbnail:
        thumb = Path(args.thumbnail)
        if not thumb.exists():
            print(f"[ERROR] Thumbnail not found: {thumb}")
            sys.exit(1)

    print("Authenticating...")
    youtube = get_authenticated_service()

    print(f"Updating video {args.video_id}...")
    update_video(youtube, args.video_id, args.title, args.description)

    if args.thumbnail:
        set_thumbnail(youtube, args.video_id, str(args.thumbnail))

    print(f"  Done! https://youtu.be/{args.video_id}")


if __name__ == "__main__":
    main()
