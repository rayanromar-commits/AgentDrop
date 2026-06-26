"""
Upload a finished AgentDrop video to TikTok via the Content Posting API.

Two modes (set in config.yaml -> tiktok.mode):
  inbox  - pushes the video into the creator's TikTok drafts/inbox. They open
           TikTok and tap "Post" to publish (works BEFORE the app is audited).
  direct - publishes straight to the profile with a caption + privacy level
           (requires the video.publish scope, which unlocks after audit).

Both use FILE_UPLOAD: we send the raw mp4 bytes to a TikTok-provided upload URL.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from agentdrop_common import setup_logging
from database import db
from upload.metadata import generate_tiktok_caption
from upload.tiktok_auth import get_access_token

log = setup_logging()

API = "https://open.tiktokapis.com/v2/post/publish"

# TikTok requires each upload chunk to be 5MB–64MB. A file <=64MB goes as a
# single chunk; larger files are split, with the final chunk absorbing the
# remainder.
MAX_CHUNK = 64 * 1024 * 1024
CHUNK = 32 * 1024 * 1024


def _chunk_plan(size: int):
    """Return (source_info, [(start, end), ...]) per TikTok's chunk rules."""
    if size <= MAX_CHUNK:
        return ({"source": "FILE_UPLOAD", "video_size": size,
                 "chunk_size": size, "total_chunk_count": 1}, [(0, size)])
    total = size // CHUNK  # last chunk absorbs the leftover bytes
    ranges = [(i * CHUNK, size if i == total - 1 else (i + 1) * CHUNK)
              for i in range(total)]
    return ({"source": "FILE_UPLOAD", "video_size": size,
             "chunk_size": CHUNK, "total_chunk_count": total}, ranges)


def _caption_for(video_row) -> str:
    """Build a TikTok caption from the stored video row."""
    title = (video_row["title"] or "").replace(" #Shorts", "")
    story = {"title": title, "subreddit": video_row["subreddit"] or ""}
    return generate_tiktok_caption(story)


def _init(endpoint: str, token: str, body: dict) -> dict:
    resp = requests.post(
        f"{API}/{endpoint}/",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json; charset=UTF-8"},
        json=body,
        timeout=30,
    )
    data = resp.json()
    if data.get("error", {}).get("code") not in (None, "ok"):
        raise RuntimeError(f"TikTok init failed: {data['error']}")
    return data["data"]


def _put_chunks(upload_url: str, file_path: Path, ranges, size: int) -> None:
    with open(file_path, "rb") as f:
        for start, end in ranges:
            f.seek(start)
            chunk = f.read(end - start)
            resp = requests.put(
                upload_url,
                headers={
                    "Content-Type": "video/mp4",
                    "Content-Length": str(end - start),
                    "Content-Range": f"bytes {start}-{end - 1}/{size}",
                },
                data=chunk,
                timeout=600,
            )
            if resp.status_code not in (200, 201, 206):
                raise RuntimeError(
                    f"TikTok chunk {start}-{end} failed "
                    f"({resp.status_code}): {resp.text[:300]}"
                )


def upload_video_tiktok(video_row, config: dict) -> str:
    """Post one video to TikTok. Returns TikTok's publish_id.

    Raises on failure so the caller can report it (e.g. a Slack alert).
    """
    tcfg = config.get("tiktok", {})
    mode = tcfg.get("mode", "inbox")
    file_path = Path(video_row["file_path"])
    if not file_path.exists():
        raise FileNotFoundError(f"Video file missing: {file_path}")

    token = get_access_token()
    size = file_path.stat().st_size
    source_info, ranges = _chunk_plan(size)

    if mode == "direct":
        caption = _caption_for(video_row)
        log.info("TikTok DIRECT post: '%s'", caption[:60])
        data = _init("video/init", token, {
            "post_info": {
                "title": caption,
                "privacy_level": tcfg.get("privacy_level", "SELF_ONLY"),
                "disable_comment": False,
                "disable_duet": False,
                "disable_stitch": False,
            },
            "source_info": source_info,
        })
    else:  # inbox / draft
        log.info("TikTok INBOX upload (lands in drafts to publish manually).")
        data = _init("inbox/video/init", token, {"source_info": source_info})

    _put_chunks(data["upload_url"], file_path, ranges, size)
    publish_id = data["publish_id"]
    log.info("TikTok upload accepted (publish_id=%s)", publish_id)
    db.set_video_status(video_row["post_id"], video_row["status"],
                        tiktok_id=publish_id)
    return publish_id


if __name__ == "__main__":
    from agentdrop_common import load_config
    db.init_db()
    cfg = load_config()
    pending = db.videos_missing_platform("tiktok")
    if not pending:
        log.error("No approved videos waiting for TikTok.")
        sys.exit(1)
    print(f"About to upload to TikTok (mode={cfg['tiktok']['mode']}): "
          f"{pending[0]['title']}")
    upload_video_tiktok(pending[0], cfg)
