"""
Step 8 — upload approved videos to YouTube (Data API v3).

Uses OAuth: the first run opens your browser to grant permission, then
saves a reusable token (token.json) so future runs are automatic.

Files (kept in the project root, git-ignored — they're secrets):
  client_secret.json  <- you download this from Google Cloud
  token.json          <- created automatically after you authorize once

Upload the next approved video:
    python3 -m upload.youtube_upload
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdrop_common import load_config, setup_logging
from database import db

log = setup_logging()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CLIENT_SECRET = PROJECT_ROOT / "client_secret.json"
TOKEN_FILE = PROJECT_ROOT / "token.json"

# Scopes = exactly what we're allowed to do. upload = post videos,
# readonly = pull stats later (Step 9). Requesting both now avoids
# having to re-authorize later.
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]


def get_authenticated_service():
    """Return an authorized YouTube API client (runs OAuth if needed)."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CLIENT_SECRET.exists():
                raise FileNotFoundError(
                    f"Missing {CLIENT_SECRET.name}. Download your OAuth "
                    "client secret from Google Cloud and place it here."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CLIENT_SECRET), SCOPES
            )
            # Opens your browser to approve, then captures the result.
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
        log.info("Saved authorization token to %s", TOKEN_FILE.name)

    return build("youtube", "v3", credentials=creds)


def upload_video(video_row, config: dict) -> str:
    """Upload one approved video. Returns the new YouTube video id."""
    from googleapiclient.http import MediaFileUpload

    youtube = get_authenticated_service()
    up = config["upload"]

    tags = [t.strip() for t in (video_row["tags"] or "").split(",") if t.strip()]
    body = {
        "snippet": {
            "title": video_row["title"][:100],   # YouTube hard limit
            "description": video_row["description"][:4900],
            "tags": tags,
            "categoryId": str(up.get("category_id", "22")),
        },
        "status": {
            "privacyStatus": up.get("privacy_status", "private"),
            "selfDeclaredMadeForKids": bool(up.get("made_for_kids", False)),
        },
    }

    file_path = video_row["file_path"]
    log.info("Uploading '%s' (privacy=%s)...",
             body["snippet"]["title"], body["status"]["privacyStatus"])

    media = MediaFileUpload(file_path, chunksize=-1, resumable=True,
                            mimetype="video/mp4")
    request = youtube.videos().insert(
        part="snippet,status", body=body, media_body=media
    )

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            log.info("  upload progress: %d%%", int(status.progress() * 100))

    video_id = response["id"]
    log.info("Uploaded! https://youtube.com/watch?v=%s", video_id)
    db.set_video_status(video_row["post_id"], "uploaded", youtube_id=video_id)
    return video_id


if __name__ == "__main__":
    db.init_db()
    cfg = load_config()

    approved = db.videos_by_status("approved")
    if not approved:
        log.error("No approved videos to upload. Approve one with "
                  "'python3 -m review.review' first.")
        sys.exit(1)

    row = approved[0]
    print(f"\nAbout to upload (PRIVACY = {cfg['upload']['privacy_status']}):")
    print(f"  {row['title']}")
    confirm = input("Type 'yes' to upload, anything else to cancel > ").strip().lower()
    if confirm != "yes":
        print("Cancelled.")
        sys.exit(0)

    upload_video(row, cfg)
