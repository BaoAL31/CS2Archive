"""Update thumbnail for an existing YouTube video."""
import random
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

SCOPES = ["https://www.googleapis.com/auth/youtube"]
TOKEN_FILE = "token_youtube_thumb.json"

def get_authenticated_service():
    creds = None
    try:
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    except:
        pass
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("client_secret.json", SCOPES)
            creds = flow.run_local_server(port=random.randint(5000, 9999), open_browser=True)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("youtube", "v3", credentials=creds)

def main():
    import sys
    if len(sys.argv) < 3:
        print("Usage: python update_thumbnail.py <video_id> <thumbnail_path>")
        sys.exit(1)
    video_id = sys.argv[1]
    thumb_path = sys.argv[2]
    youtube = get_authenticated_service()
    youtube.thumbnails().set(
        videoId=video_id,
        media_body=MediaFileUpload(thumb_path),
    ).execute()
    print(f"Thumbnail updated for https://youtu.be/{video_id}")

if __name__ == "__main__":
    main()
