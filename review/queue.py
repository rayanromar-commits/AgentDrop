"""
Step 7 — review queue.

A finished video is "submitted" to the queue: its metadata is generated
and stored, and the file is moved into review/pending/ (manual mode) or
review/approved/ (auto mode). You then approve/reject pending videos
with review/review.py.

Folders:
  review/pending/   awaiting your decision  (manual mode)
  review/approved/  cleared for upload
  review/rejected/  discarded
"""

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdrop_common import DATA_DIR, setup_logging
from database import db
from upload.metadata import generate_metadata

log = setup_logging()

REVIEW_ROOT = DATA_DIR / "review"
DIRS = {
    "pending": REVIEW_ROOT / "pending",
    "approved": REVIEW_ROOT / "approved",
    "rejected": REVIEW_ROOT / "rejected",
}


def _dir_for(status: str) -> Path:
    d = DIRS.get(status, DIRS["pending"])
    d.mkdir(parents=True, exist_ok=True)
    return d


def submit_video(story: dict, video_path: Path, config: dict) -> dict:
    """Register a finished video and move it into the right folder."""
    meta = generate_metadata(story)

    # Manual mode -> wait for approval. Auto mode -> straight to approved.
    status = "approved" if config.get("approval_mode") == "auto" else "pending"

    dest_dir = _dir_for(status)
    dest = dest_dir / Path(video_path).name
    shutil.move(str(video_path), str(dest))

    db.upsert_video(
        post_id=story["post_id"],
        subreddit=story.get("subreddit", ""),
        title=meta["title"],
        description=meta["description"],
        tags=meta["tags"],
        file_path=str(dest),
        status=status,
    )
    log.info("Submitted '%s' to review queue as '%s'.", meta["title"], status)
    return {"status": status, "path": dest, "meta": meta}


def move_to_status(post_id: str, new_status: str) -> Path | None:
    """Move a video's file to the folder matching its new status."""
    rows = [r for r in (db.videos_by_status("pending")
                        + db.videos_by_status("approved"))
            if r["post_id"] == post_id]
    if not rows:
        return None
    row = rows[0]
    src = Path(row["file_path"])
    dest = _dir_for(new_status) / src.name
    if src.exists():
        shutil.move(str(src), str(dest))
    db.set_video_status(post_id, new_status, file_path=str(dest))
    return dest
