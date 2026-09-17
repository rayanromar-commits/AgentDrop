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

import json
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
    """Make sure `post_id` is retired in the posted ledger, with its video id.

    Production already calls mark_posted() when it queues a video, so the usual
    job here is to backfill the YouTube id onto that entry once the upload
    succeeds — which is what lets a ledger row be matched back to the live
    channel. Returns True if the ledger changed.
    """
    src = source_module(config)
    title = None
    for _path, data in src._datasets(config):
        if src._post_id(data["title"]) == post_id:
            title = data["title"]
            break
    if title is None:
        log.warning("[ledger] no dataset on disk for %s — nothing to retire.",
                    post_id)
        return False
    if post_id not in src.posted_ids():
        src.mark_posted(title, youtube_id=youtube_id,
                        date=date.today().isoformat())
        return True
    return _backfill_youtube_id(src, post_id, youtube_id)


def _backfill_youtube_id(src, post_id: str, youtube_id: str) -> bool:
    """Write `youtube_id` onto an already-retired ledger entry that lacks one."""
    if not youtube_id:
        return False
    data = src._load_posted()
    changed = False
    for e in data.get("posted", []):
        if isinstance(e, dict) and e.get("post_id") == post_id and not e.get("youtube_id"):
            e["youtube_id"] = youtube_id
            changed = True
    if not changed:
        return False
    try:
        src.POSTED_LEDGER.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        log.info("[ledger] recorded youtube id %s for %s", youtube_id, post_id)
        return True
    except Exception as e:
        log.warning("[ledger] could not write posted-ledger: %s", e)
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
