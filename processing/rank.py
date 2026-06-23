"""
Story ranking — pick the most CAPTIVATING story, not just the first.

Each candidate gets a "captivation score" from engagement signals:
  - upvotes        : the crowd's verdict (Reddit mode; manual = 0)
  - title hooks    : questions + drama/conflict words pull viewers in
  - length fit     : favors the snappy ~45-75s narration sweet spot

The pipeline narrates the highest-scoring story. Later (Step 9) real
view data can further bias this toward what performs on your channel.

Test it:  python3 processing/rank.py
"""

import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdrop_common import load_config, setup_logging

log = setup_logging()

# Words in a TITLE that signal drama / curiosity → higher engagement.
HOOK_WORDS = {
    "caught", "cheated", "cheating", "revenge", "refused", "fired",
    "quit", "exposed", "lied", "stole", "banned", "karma", "entitled",
    "demanded", "snapped", "ruined", "secret", "betrayed", "confronted",
    "petty", "malicious", "nightmare", "insane", "unbelievable",
    "jerk", "asshole", "audacity", "report", "sued", "evicted",
}

# Ideal narrated length (in words) for retention. ~150 wpm => ~45-75s.
IDEAL_MIN_WORDS = 110
IDEAL_MAX_WORDS = 230


def _title_hook_score(title: str) -> float:
    score = 0.0
    if "?" in title:                       # questions invite curiosity
        score += 1.5
    lowered = title.lower()
    hooks = sum(1 for w in HOOK_WORDS if re.search(rf"\b{w}\b", lowered))
    score += min(hooks, 4) * 1.0           # cap so one title can't run away
    if re.search(r"\bAITA\b|\bWIBTA\b", title, re.IGNORECASE):
        score += 1.0
    return score


def _length_fit_score(word_count: int) -> float:
    if IDEAL_MIN_WORDS <= word_count <= IDEAL_MAX_WORDS:
        return 2.0
    # Gentle penalty the further outside the sweet spot we get.
    if word_count < IDEAL_MIN_WORDS:
        return max(0.0, 2.0 - (IDEAL_MIN_WORDS - word_count) / 40)
    return max(0.0, 2.0 - (word_count - IDEAL_MAX_WORDS) / 80)


def captivation_score(story: dict) -> float:
    """Combine signals into one score (higher = more captivating)."""
    # log-scale upvotes so a 50k post doesn't drown out everything.
    upvotes = max(0, story.get("score", 0))
    upvote_score = math.log10(upvotes + 1) * 2.0   # 0..~10

    return (
        upvote_score
        + _title_hook_score(story["title"])
        + _length_fit_score(story.get("word_count", 0))
    )


def rank_stories(stories: list[dict]) -> list[dict]:
    """Return stories sorted most → least captivating, with scores attached."""
    for s in stories:
        s["captivation_score"] = round(captivation_score(s), 2)
    return sorted(stories, key=lambda s: s["captivation_score"], reverse=True)


if __name__ == "__main__":
    from sourcing.get_stories import fetch_stories
    from processing.screen import screen_story
    from database import db

    db.init_db()
    cfg = load_config()
    stories = fetch_stories(cfg, skip_seen=False)
    passing = [s for s in stories if screen_story(s, cfg)[0]]

    ranked = rank_stories(passing)
    log.info("Ranked %d passing stories (most captivating first):\n", len(ranked))
    for i, s in enumerate(ranked, 1):
        print(f"{i}. [{s['captivation_score']:>5}]  {s['title']}")
