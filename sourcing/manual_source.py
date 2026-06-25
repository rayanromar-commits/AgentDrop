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

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdrop_common import setup_logging

log = setup_logging()

STORIES_DIR = Path(__file__).resolve().parent / "manual_stories"


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


def unused_story_count() -> int:
    """How many manual stories haven't been produced yet (restock signal).

    Counts .txt files whose post_id we haven't already seen/used. Mirrors the
    skip_seen filter in fetch_stories() but without the per-call logging.
    """
    from database import db

    if not STORIES_DIR.exists():
        return 0
    return sum(
        1 for path in STORIES_DIR.glob("*.txt")
        if not db.post_already_seen("manual_" + path.stem)
    )
