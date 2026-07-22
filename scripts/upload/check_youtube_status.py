"""Check YouTube uploads and their publish schedule."""
import json, os, random, sys
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ['https://www.googleapis.com/auth/youtube.readonly']

creds = None
if os.path.exists('token_youtube.json'):
    creds = Credentials.from_authorized_user_file('token_youtube.json', ['https://www.googleapis.com/auth/youtube'])
if not creds or not creds.valid:
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        flow = InstalledAppFlow.from_client_secrets_file('client_secret.json', SCOPES)
        creds = flow.run_local_server(port=random.randint(5000, 9999), open_browser=True)

youtube = build('youtube', 'v3', credentials=creds)

req = youtube.search().list(part='snippet', forMine=True, order='date', maxResults=25).execute()
for item in req.get('items', []):
    vid = item['id']['videoId']
    title = item['snippet']['title']
    published = item['snippet']['publishedAt']
    status = youtube.videos().list(part='status,snippet', id=vid).execute()
    s = status['items'][0]['status']
    privacy = s.get('privacyStatus', '?')
    publish_at = s.get('publishAt') or '-'
    desc = status['items'][0]['snippet']['description'][:80]
    publish_str = publish_at[:19] if publish_at != '-' else '-'
    print(f'{vid} | {privacy:8s} | {published[:19]} | pAt: {publish_str:19s} | {title[:60]}')
