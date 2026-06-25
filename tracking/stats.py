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


def fetch_channel_stats() -> dict | None:
    """Fetch + record this channel's subscriber / view / video totals.

    Uses channels().list(mine=True), which only needs the youtube.readonly
    scope we already hold. Returns the snapshot dict, or None if the channel
    can't be read.
    """
    from upload.youtube_upload import get_authenticated_service

    youtube = get_authenticated_service()
    resp = youtube.channels().list(part="statistics", mine=True).execute()
    items = resp.get("items", [])
    if not items:
        log.warning("No channel found for the authorized account.")
        return None

    s = items[0].get("statistics", {})
    # Channel viewCount is often hidden (0) and unreliable for Shorts, so
    # prefer the sum of our tracked per-video views; fall back to the API
    # value if that's somehow larger.
    api_views = int(s.get("viewCount", 0))
    snap = {
        "subscribers": int(s.get("subscriberCount", 0)),
        "views": max(db.total_tracked_views(), api_views),
        "videos": int(s.get("videoCount", 0)),
    }
    db.record_channel_stats(**snap)
    log.info("Channel stats: %d subs, %d views, %d videos",
             snap["subscribers"], snap["views"], snap["videos"])
    return snap


def print_report() -> None:
    """Show a performance summary by subreddit, ranked by the learned score."""
    perf = db.subreddit_performance()
    if not perf:
        print("No stats recorded yet. Run refresh_stats() after uploads.")
        return
    print("\nPerformance by subreddit (best first, by age-normalized score):")
    print(f"  {'subreddit':22} {'score':>7} {'views/day':>10} "
          f"{'avg views':>10} {'engmt':>7}  n")
    for sub, d in sorted(perf.items(), key=lambda kv: kv[1]["score"], reverse=True):
        print(f"  r/{sub:20} {d['score']:7.1f} {d['avg_views_per_day']:10.1f} "
              f"{d['avg_views']:10.0f} {d['engagement_rate'] * 100:6.1f}% {d['n']:>3}")


if __name__ == "__main__":
    db.init_db()
    refresh_stats()
    print_report()
