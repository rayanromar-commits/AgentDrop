"""
Manual story source.

Reads stories from plain-text files you drop into
sourcing/manual_stories/. Use this while waiting for Reddit API access.

FILE FORMAT (one story per .txt file):
    Line 1            -> the TITLE
    Line 2            -> (optional) a line starting with "subreddit:" e.g.
                         subreddit: tifu
    Remaining lines   -> the BODY of the story

The file name (without .txt) is used as the unique post id, so each
file is only ever used once (dedup works just like with Reddit).
"""

import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdrop_common import setup_logging

log = setup_logging()

STORIES_DIR = Path(__file__).resolve().parent / "manual_stories"
# Scripts of stories that have been posted are moved here so they're never
# produced again (repetitive content gets a channel throttled/removed). Lives
# in a SUBfolder of STORIES_DIR, which unused_story_count()'s non-recursive
# glob ignores — so archiving a file automatically drops the unused count.
USED_DIR = STORIES_DIR / "used"


def _base_stem(post_id: str) -> str:
    """Map a (possibly decorated) post_id back to its source-file stem.

    Production tags the source id with suffixes like ``_p1`` (part N of a
    split series) or ``_cap`` (captioned variant). Strip the ``manual_``
    prefix and any trailing ``_p<n>`` / ``_cap`` decorations so we land on
    the original ``<stem>.txt`` filename.
    """
    stem = post_id[len("manual_"):] if post_id.startswith("manual_") else post_id
    # Strip repeated trailing _p<digits> / _cap in any order/combination.
    while True:
        new = re.sub(r"_(?:p\d+|cap)$", "", stem)
        if new == stem:
            return stem
        stem = new


def archive_story(post_id: str) -> bool:
    """Move a posted story's source .txt into the ``used/`` archive.

    Called when a video goes live so the script is retired and the leftover
    story count drops by one. Idempotent and quiet: returns False (no error)
    for non-manual posts, already-archived files, or stories whose file was
    removed by hand — so multi-part series only archive once and re-posts are
    harmless.
    """
    if not post_id.startswith("manual_"):
        return False

    src = STORIES_DIR / f"{_base_stem(post_id)}.txt"
    if not src.exists():
        return False

    USED_DIR.mkdir(exist_ok=True)
    dest = USED_DIR / src.name
    src.replace(dest)
    log.info("Archived used story %s -> %s", src.name, dest)
    return True


def fetch_stories(config: dict, skip_seen: bool = True) -> list[dict]:
    """Read all .txt stories from the manual_stories folder."""
    from database import db  # imported here to avoid circular imports

    STORIES_DIR.mkdir(exist_ok=True)
    stories: list[dict] = []

    txt_files = sorted(STORIES_DIR.glob("*.txt"))
    if not txt_files:
        log.warning(
            "No story files found in %s. Drop a .txt file in there.",
            STORIES_DIR,
        )
        return stories

    for path in txt_files:
        post_id = "manual_" + path.stem
        if skip_seen and db.post_already_seen(post_id):
            continue

        raw_lines = path.read_text(encoding="utf-8").splitlines()
        # Drop leading blank lines.
        while raw_lines and not raw_lines[0].strip():
            raw_lines.pop(0)
        if not raw_lines:
            log.warning("Skipping empty file: %s", path.name)
            continue

        title = raw_lines[0].strip()
        rest = raw_lines[1:]

        subreddit = "manual"
        if rest and rest[0].lower().startswith("subreddit:"):
            subreddit = rest[0].split(":", 1)[1].strip()
            rest = rest[1:]

        body = "\n".join(rest).strip()
        stories.append(
            {
                "post_id": post_id,
                "subreddit": subreddit,
                "title": title,
                "body": body,
                "score": 0,          # not applicable for manual stories
                "over_18": False,
                "word_count": len(body.split()),
            }
        )

    log.info("Loaded %d manual stories from %s.", len(stories), STORIES_DIR)
    return stories


def _unused_paths() -> list[Path]:
    """Top-level .txt stories not yet produced (unseen post_id)."""
    from database import db
    if not STORIES_DIR.exists():
        return []
    return [
        p for p in STORIES_DIR.glob("*.txt")
        if not db.post_already_seen("manual_" + p.stem)
    ]


def unused_story_count() -> int:
    """How many manual stories haven't been produced yet (restock signal).

    Counts .txt files whose post_id we haven't already seen/used. Mirrors the
    skip_seen filter in fetch_stories() but without the per-call logging.
    """
    return len(_unused_paths())


def _story_total_words(path: Path) -> int:
    """Word count of a story file (title + body), skipping the subreddit line."""
    lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    if not lines:
        return 0
    title, rest = lines[0], lines[1:]
    if rest and rest[0].lower().startswith("subreddit:"):
        rest = rest[1:]
    return len((title + " " + " ".join(rest)).split())


def _parts_for(total_words: int, config: dict) -> int:
    """How many uploads (parts) a story becomes — mirrors the production logic.

    0 means the story would be SKIPPED (too long even to condense).
    """
    split_cfg = (config or {}).get("splitting", {})
    if not split_cfg.get("enabled"):
        return 1
    wpp = split_cfg.get("words_per_part", 120)
    maxp = split_cfg.get("max_parts", 3)
    ceiling = wpp * maxp
    if total_words <= ceiling:
        return max(1, math.ceil(total_words / wpp))
    ccfg = (config or {}).get("condense", {})
    if ccfg.get("enabled") and total_words <= ccfg.get("max_source_words", 1200):
        return maxp                      # condensed down to fit the cap
    return 0                             # too long -> skipped


def unused_upload_count(config: dict) -> int:
    """Total UPLOADS (videos) the unused stories will produce.

    Each story fans out into multiple parts, so uploads is the truer runway
    measure than story count. Skipped (too-long) stories contribute 0.
    """
    return sum(_parts_for(_story_total_words(p), config) for p in _unused_paths())


def restock_status(config: dict) -> dict:
    """Runway snapshot: stories, uploads, uploads/day, and days of runway."""
    stories = unused_story_count()
    uploads = unused_upload_count(config)
    per_day = len((config or {}).get("upload", {}).get("upload_times", [])) or 3
    days = round(uploads / per_day, 1) if per_day else 0.0
    return {"stories": stories, "uploads": uploads,
            "uploads_per_day": per_day, "days_runway": days}
