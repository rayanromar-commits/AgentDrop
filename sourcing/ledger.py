"""
Format-agnostic "this one is spent" bookkeeping for the upload path.

Replaces the story pipeline's sourcing.manual_source, which archived a posted
script by moving its .txt file. The ranking formats have no per-video file to
move — a dataset is retired by writing its post_id into that format's posted
ledger — so the same two entry points live here and dispatch on content_type:

  archive_story(post_id, config)  -> True if THIS call retired the dataset
  restock_status(config)          -> runway snapshot for the Slack nudges

Both are idempotent and quiet: a second upload of the same post_id (YouTube
then TikTok) returns False rather than double-counting.
"""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdrop_common import setup_logging
from database import db

log = setup_logging()


def source_module(config: dict):
    """The sourcing module backing the configured content_type."""
    if (config or {}).get("content_type", "clipranking") == "ranking":
        from sourcing import ranking_source as src
    else:
        from sourcing import clip_ranking_source as src
    return src


def _unused(config: dict) -> list[dict]:
    """Datasets on disk that have not been posted yet.

    Deliberately does NOT call fetch_stories(): that reconciles against the
    live channel over the network, and this runs on every upload and digest.
    """
    src = source_module(config)
    posted = src.posted_ids()
    return [d for _p, d in src._datasets(config)
            if src._post_id(d["title"]) not in posted
            and not db.post_already_seen(src._post_id(d["title"]))]


def archive_story(post_id: str, config: dict, youtube_id: str = "") -> bool:
    """Retire the dataset behind `post_id` in the posted ledger.

    Returns True only the first time, so the caller's low-stock nudge fires
    once per video rather than once per platform.
    """
    src = source_module(config)
    if post_id in src.posted_ids():
        return False
    for _path, data in src._datasets(config):
        if src._post_id(data["title"]) == post_id:
            src.mark_posted(data["title"], youtube_id=youtube_id,
                            date=date.today().isoformat())
            return post_id in src.posted_ids()
    log.warning("[ledger] no dataset on disk for %s — nothing to retire.",
                post_id)
    return False


def restock_status(config: dict) -> dict:
    """Runway snapshot: stories, uploads, uploads/day, and days of runway.

    One ranking dataset renders exactly one video, so stories == uploads here
    (the story pipeline fanned one script out into ~3 parts).
    """
    count = len(_unused(config))
    per_day = len((config or {}).get("upload", {}).get("upload_times", [])) or 3
    return {"stories": count, "uploads": count, "uploads_per_day": per_day,
            "days_runway": round(count / per_day, 1) if per_day else 0.0}
