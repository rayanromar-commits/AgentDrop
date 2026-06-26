"""
One-shot TikTok integration demo for the App Review screen recording.

Running `python3 -m upload.tiktok_demo` exercises every product + scope in
order, printing clearly what each does, so the demo video shows them all:

  Login Kit + user.info.basic/profile  -> who we're authorized as
  user.info.stats                      -> follower / like / video counts
  video.list                           -> the creator's public videos
  Content Posting API + video.upload   -> upload a video to the inbox
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from agentdrop_common import load_config
from database import db
from tracking.tiktok_stats import fetch_user_stats, fetch_videos
from upload.tiktok_auth import get_access_token
from upload.tiktok_upload import upload_video_tiktok

STATUS_URL = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"


def main() -> None:
    db.init_db()
    cfg = load_config()

    print("\n[1] Login Kit — authorized TikTok account (user.info.basic/profile):")
    user = fetch_user_stats()
    print(f"    Connected as: {user.get('display_name')}")

    print("\n[2] user.info.stats — account statistics:")
    print(f"    Followers: {user.get('follower_count')} | "
          f"Likes: {user.get('likes_count')} | "
          f"Videos: {user.get('video_count')}")

    print("\n[3] video.list — the creator's public videos:")
    vids = fetch_videos(10)
    if not vids:
        print("    (no public videos yet)")
    for v in vids:
        print(f"    {v.get('view_count', 0):>7} views — {(v.get('title') or '')[:50]}")

    print("\n[4] Content Posting API + video.upload — uploading a video to inbox:")
    pending = [r for r in db.videos_missing_platform("tiktok")
               if Path(r["file_path"]).exists()]
    if not pending:
        print("    (no local video available to upload)")
        return
    pid = upload_video_tiktok(pending[0], cfg)
    print(f"    Upload accepted. publish_id = {pid}")

    time.sleep(3)
    token = get_access_token()
    resp = requests.post(
        STATUS_URL,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json; charset=UTF-8"},
        json={"publish_id": pid}, timeout=30,
    )
    status = resp.json().get("data", {}).get("status")
    print(f"    Publish status: {status}")
    print("\n✅ Demo complete — open TikTok > Inbox to see the uploaded video.\n")


if __name__ == "__main__":
    main()
