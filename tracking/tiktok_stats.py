"""
Read TikTok analytics via the Display API (user.info.stats + video.list).

Used for the Slack digest (follower growth, per-video views/likes) and for
the app-review demo, which needs to show these scopes in action.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from agentdrop_common import setup_logging
from upload.tiktok_auth import get_access_token

log = setup_logging()

USER_URL = "https://open.tiktokapis.com/v2/user/info/"
VIDEO_LIST_URL = "https://open.tiktokapis.com/v2/video/list/"


def fetch_user_stats() -> dict:
    """Profile + engagement stats (display name, followers, likes, video count)."""
    token = get_access_token()
    fields = ("open_id,display_name,follower_count,following_count,"
              "likes_count,video_count")
    resp = requests.get(
        USER_URL, params={"fields": fields},
        headers={"Authorization": f"Bearer {token}"}, timeout=30,
    )
    data = resp.json()
    if data.get("error", {}).get("code") not in (None, "ok"):
        raise RuntimeError(f"TikTok user/info failed: {data['error']}")
    return data["data"]["user"]


def fetch_videos(max_count: int = 20) -> list[dict]:
    """The creator's public videos with view/like/comment/share counts."""
    token = get_access_token()
    fields = "id,title,view_count,like_count,comment_count,share_count"
    resp = requests.post(
        VIDEO_LIST_URL, params={"fields": fields},
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json; charset=UTF-8"},
        json={"max_count": max_count}, timeout=30,
    )
    data = resp.json()
    if data.get("error", {}).get("code") not in (None, "ok"):
        raise RuntimeError(f"TikTok video/list failed: {data['error']}")
    return data["data"].get("videos", [])


if __name__ == "__main__":
    user = fetch_user_stats()
    print("\n=== TikTok account ===")
    print(f"  {user.get('display_name')} — {user.get('follower_count')} followers, "
          f"{user.get('likes_count')} likes, {user.get('video_count')} videos")
    print("\n=== Recent videos ===")
    for v in fetch_videos(10):
        print(f"  {v.get('view_count', 0):>7} views  {(v.get('title') or '')[:50]}")
