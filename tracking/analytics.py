"""
YouTube Analytics — the metrics the Data API can't return.

The Data API (tracking/stats.py) gives views / likes / comments. The deeper
signals that actually decide how far a Short travels — completion %, average
view duration, shares, subscribers gained — live in a DIFFERENT API, the
YouTube Analytics API (youtubeAnalytics v2). This module pulls them.

Why it matters: completion (averageViewPercentage) is the #1 Shorts ranking
signal, and shares are the spread signal that breaks a video out of the
subscriber bubble. Feeding these into the ranker lets AgentDrop learn from
whether people FINISH and SHARE, not just whether they clicked.

Fails SAFE: if either prerequisite below is missing, or the call errors, this
returns {} and the pipeline keeps running on Data-API stats alone — nothing
breaks. TWO one-time setup steps unlock real data:
  1. Enable the "YouTube Analytics API" in the Google Cloud project (Console ->
     APIs & Services -> Enable APIs). Without it the call 403s accessNotConfigured.
  2. Grant the yt-analytics.readonly scope by re-authorizing: delete token.json +
     `python3 -m upload.youtube_upload`, then update Railway's GOOGLE_TOKEN_JSON
     with the new token. Without it the call 403s on scope.

One-off check:  python3 -m tracking.analytics
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdrop_common import setup_logging

log = setup_logging()

# Lifetime window: a fixed early start covers every upload; end is "today".
# averageViewPercentage is averaged over this whole span = lifetime completion.
_LIFETIME_START = "2020-01-01"

# Metrics we ask the Analytics API for, in order. Kept to what's meaningful for
# Shorts; impressions / CTR are intentionally omitted (not exposed for Shorts).
_METRICS = [
    "views",
    "averageViewPercentage",
    "averageViewDuration",
    "estimatedMinutesWatched",
    "shares",
    "subscribersGained",
]


def fetch_video_analytics(youtube_ids: list[str]) -> dict[str, dict]:
    """Return {youtube_id: {avg_view_pct, avg_view_seconds, shares, ...}}.

    One query for all ids (the Analytics API takes up to 500 in a video
    filter). Returns {} on any problem so callers degrade gracefully.
    """
    ids = [i for i in youtube_ids if i]
    if not ids:
        return {}

    from datetime import date

    try:
        from upload.youtube_upload import get_analytics_service
        analytics = get_analytics_service()
    except Exception as e:
        log.warning("[analytics] could not build client (%s); skipping.", e)
        return {}

    out: dict[str, dict] = {}
    today = date.today().isoformat()
    # The video filter accepts up to 500 ids; chunk defensively anyway.
    for i in range(0, len(ids), 200):
        batch = ids[i:i + 200]
        try:
            resp = analytics.reports().query(
                ids="channel==MINE",
                startDate=_LIFETIME_START,
                endDate=today,
                metrics=",".join(_METRICS),
                dimensions="video",
                filters="video==" + ",".join(batch),
                maxResults=500,
            ).execute()
        except Exception as e:
            # 403 here = either the Analytics API isn't enabled in the Cloud
            # project (accessNotConfigured) or the yt-analytics.readonly scope
            # isn't granted yet. Log clearly and fall back to Data-API stats.
            log.warning(
                "[analytics] query failed (%s). If 403: enable the YouTube "
                "Analytics API in Google Cloud AND re-authorize for the "
                "yt-analytics.readonly scope (see module docstring).",
                e,
            )
            return {}

        headers = [h["name"] for h in resp.get("columnHeaders", [])]
        for row in resp.get("rows", []):
            rec = dict(zip(headers, row))
            vid = rec.get("video")
            if not vid:
                continue
            out[vid] = {
                "avg_view_pct": _num(rec.get("averageViewPercentage")),
                "avg_view_seconds": _num(rec.get("averageViewDuration")),
                "shares": _int(rec.get("shares")),
                "est_minutes_watched": _num(rec.get("estimatedMinutesWatched")),
                "subscribers_gained": _int(rec.get("subscribersGained")),
            }

    log.info("[analytics] pulled retention/shares for %d video(s).", len(out))
    return out


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    from database import db

    db.init_db()
    uploaded = db.videos_by_status("uploaded")
    ids = [r["youtube_id"] for r in uploaded if r["youtube_id"]]
    data = fetch_video_analytics(ids)
    if not data:
        print("No analytics returned (scope not granted yet, or no uploads).")
    else:
        print(f"\nAnalytics for {len(data)} video(s):")
        for vid, d in data.items():
            print(f"  {vid}  completion={d['avg_view_pct']}%  "
                  f"avg={d['avg_view_seconds']}s  shares={d['shares']}  "
                  f"subs+={d['subscribers_gained']}")
