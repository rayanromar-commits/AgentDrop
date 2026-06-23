"""
Step 9 — performance tracking.

Pulls current view / like / comment counts for every uploaded video via
the YouTube Data API and stores a snapshot. Over time these snapshots
let AgentDrop (and you) see which stories/subreddits perform best.

Run a one-off refresh + report:
    python3 -m tracking.stats
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdrop_common import setup_logging
from database import db

log = setup_logging()


def refresh_stats() -> int:
    """Fetch fresh stats for all uploaded videos. Returns count updated."""
    from upload.youtube_upload import get_authenticated_service

    uploaded = db.videos_by_status("uploaded")
    if not uploaded:
        log.info("No uploaded videos to track yet.")
        return 0

    youtube = get_authenticated_service()
    # API allows up to 50 ids per call.
    id_to_row = {r["youtube_id"]: r for r in uploaded if r["youtube_id"]}
    ids = list(id_to_row.keys())

    updated = 0
    for i in range(0, len(ids), 50):
        batch = ids[i:i + 50]
        resp = youtube.videos().list(
            part="statistics", id=",".join(batch)
        ).execute()
        for item in resp.get("items", []):
            row = id_to_row[item["id"]]
            s = item.get("statistics", {})
            db.record_stats(
                post_id=row["post_id"],
                youtube_id=item["id"],
                subreddit=row["subreddit"],
                views=int(s.get("viewCount", 0)),
                likes=int(s.get("likeCount", 0)),
                comments=int(s.get("commentCount", 0)),
            )
            updated += 1
    log.info("Refreshed stats for %d video(s).", updated)
    return updated


def print_report() -> None:
    """Show a quick performance summary by subreddit."""
    perf = db.subreddit_performance()
    if not perf:
        print("No stats recorded yet. Run refresh_stats() after uploads.")
        return
    print("\nAverage views by subreddit (best first):")
    for sub, d in sorted(perf.items(), key=lambda kv: kv[1]["avg_views"], reverse=True):
        print(f"  r/{sub:20} {d['avg_views']:.0f} avg views  ({d['n']} videos)")


if __name__ == "__main__":
    db.init_db()
    refresh_stats()
    print_report()
