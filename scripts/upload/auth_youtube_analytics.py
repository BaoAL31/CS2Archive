"""One-off OAuth consent for YouTube Analytics (read-only) + YouTube read.

Writes token_youtube_analytics.json at the repo root.
Run:  python scripts/upload/auth_youtube_analytics.py
"""
import os
import random
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CLIENT_SECRET = os.path.join(ROOT, 'client_secret.json')
TOKEN = os.path.join(ROOT, 'token_youtube_analytics.json')

SCOPES = [
    'https://www.googleapis.com/auth/yt-analytics.readonly',
    'https://www.googleapis.com/auth/youtube.readonly',
]


def main() -> int:
    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET, SCOPES)
    creds = flow.run_local_server(port=random.randint(5000, 9999), open_browser=True)
    with open(TOKEN, 'w') as f:
        f.write(creds.to_json())
    print('wrote', TOKEN)
    print('scopes:', creds.scopes)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
