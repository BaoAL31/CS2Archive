"""Export public long-form uploads from the last N days to CSV (YouTube Data API v3 only).

Requires client_secret.json + token_youtube_readonly.json (scope youtube.readonly).
Filters out Shorts (vertical video) and non-public videos (private/unlisted/scheduled).
Output columns: video_id, title, published_at, views, likes, url.
"""
import argparse
import csv
import os
import random
from datetime import datetime, timedelta, timezone

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ['https://www.googleapis.com/auth/youtube.readonly']
TOKEN_FILE = 'token_youtube_readonly.json'


def get_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file('client_secret.json', SCOPES)
            creds = flow.run_local_server(port=random.randint(5000, 9999), open_browser=True)
        with open(TOKEN_FILE, 'w') as f:
            f.write(creds.to_json())
    return build('youtube', 'v3', credentials=creds)


def iso_to_dt(s):
    return datetime.fromisoformat(s.replace('Z', '+00:00'))


def get_uploads_playlist(youtube):
    resp = youtube.channels().list(part='contentDetails', mine=True).execute()
    return resp['items'][0]['contentDetails']['relatedPlaylists']['uploads']


def iter_playlist_video_ids(youtube, playlist_id):
    next_page = None
    while True:
        resp = youtube.playlistItems().list(
            part='contentDetails', playlistId=playlist_id,
            maxResults=50, pageToken=next_page).execute()
        for item in resp.get('items', []):
            yield item['contentDetails']['videoId']
        next_page = resp.get('nextPageToken')
        if not next_page:
            break


def fetch_video_details(youtube, ids):
    resp = youtube.videos().list(
        part='snippet,statistics,contentDetails,status,fileDetails',
        id=','.join(ids)).execute()
    return resp.get('items', [])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=7, help='lookback window in days')
    ap.add_argument('--csv', default='youtube_export.csv', help='output CSV filename')
    ap.add_argument('--outdir', default='exports', help='output directory')
    args = ap.parse_args()

    youtube = get_service()
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    playlist_id = get_uploads_playlist(youtube)

    rows = []
    ids = list(iter_playlist_video_ids(youtube, playlist_id))
    print(f'Fetched {len(ids)} total uploads, filtering to last {args.days} days...')

    for i in range(0, len(ids), 50):
        batch = ids[i:i + 50]
        for v in fetch_video_details(youtube, batch):
            sn = v['snippet']
            st = v.get('statistics', {})
            published = iso_to_dt(sn['publishedAt'])
            if published < cutoff:
                continue
            # Skip anything not publicly listed (private / unlisted / scheduled).
            if v.get('status', {}).get('privacyStatus') != 'public':
                continue
            # Shorts are vertical (aspect < 1.0).
            aspect = 1.0
            fd = v.get('fileDetails') or {}
            streams = fd.get('videoStreams') if isinstance(fd, dict) else None
            if streams:
                vs = streams[0]
                w, h = vs.get('widthPixels', 0), vs.get('heightPixels', 0)
                if w and h:
                    aspect = w / h
            if aspect < 1.0:
                continue  # skip Shorts
            rows.append({
                'video_id': v['id'],
                'title': sn['title'],
                'published_at': sn['publishedAt'],
                'days_since_upload': (datetime.now(timezone.utc) - published).days,
                'views': int(st.get('viewCount', 0)),
                'likes': int(st.get('likeCount', 0)) if st.get('likeCount') else None,
                'url': f"https://www.youtube.com/watch?v={v['id']}",
            })

    rows.sort(key=lambda r: r['published_at'], reverse=True)
    os.makedirs(args.outdir, exist_ok=True)
    out_path = os.path.join(args.outdir, args.csv)
    try:
        fh = open(out_path, 'w', newline='', encoding='utf-8')
    except PermissionError:
        out_path = os.path.join(args.outdir, args.csv.replace('.csv', '_new.csv'))
        print(f'  WARNING: {out_path} locked, writing fallback')
        fh = open(out_path, 'w', newline='', encoding='utf-8')
    with fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else [])
        w.writeheader()
        w.writerows(rows)

    print(f'Exported {len(rows)} public long-form videos (last {args.days}d) -> {out_path}')


if __name__ == '__main__':
    main()
