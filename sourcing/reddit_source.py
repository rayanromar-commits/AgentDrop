"""
Step 2 — Reddit sourcing.

Connects to Reddit using PRAW and pulls top posts from the subreddits
listed in config.yaml. Returns the title, body, score, and post id of
each story. Posts we've already saved in the database are skipped.

Run directly to test:   python3 sourcing/reddit_source.py
"""

import os
import sys
from pathlib import Path

# Make the project root importable so we can use our shared helpers
# whether this file is run directly or imported by main.py later.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import praw
from dotenv import load_dotenv

from agentdrop_common import load_config, setup_logging
from database import db

log = setup_logging()


def get_reddit_client() -> praw.Reddit:
    """Create a read-only Reddit client from credentials in .env."""
    load_dotenv()  # reads the .env file into environment variables

    client_id = os.getenv("REDDIT_CLIENT_ID")
    client_secret = os.getenv("REDDIT_CLIENT_SECRET")
    user_agent = os.getenv("REDDIT_USER_AGENT")

    missing = [
        name
        for name, val in {
            "REDDIT_CLIENT_ID": client_id,
            "REDDIT_CLIENT_SECRET": client_secret,
            "REDDIT_USER_AGENT": user_agent,
        }.items()
        if not val
    ]
    if missing:
        raise RuntimeError(
            "Missing Reddit credentials in .env: "
            + ", ".join(missing)
            + ". Did you copy .env.example to .env and fill it in?"
        )

    return praw.Reddit(
        client_id=client_id,
        client_secret=client_secret,
        user_agent=user_agent,
    )


def fetch_stories(config: dict, skip_seen: bool = True) -> list[dict]:
    """Pull candidate stories from all configured subreddits."""
    reddit = get_reddit_client()
    reddit.read_only = True

    limit = config["posts_to_scan_per_subreddit"]
    time_filter = config["reddit_time_filter"]

    stories: list[dict] = []
    for sub_name in config["subreddits"]:
        log.info("Scanning r/%s (top %s of the %s)...", sub_name, limit, time_filter)
        subreddit = reddit.subreddit(sub_name)

        for post in subreddit.top(time_filter=time_filter, limit=limit):
            # Self/text posts only — we need a written story to narrate.
            if not post.is_self:
                continue
            if post.stickied:
                continue
            if skip_seen and db.post_already_seen(post.id):
                continue

            body = post.selftext or ""
            stories.append(
                {
                    "post_id": post.id,
                    "subreddit": sub_name,
                    "title": post.title,
                    "body": body,
                    "score": post.score,
                    "over_18": post.over_18,
                    "word_count": len(body.split()),
                }
            )

    log.info("Collected %d candidate stories total.", len(stories))
    return stories


if __name__ == "__main__":
    db.init_db()
    cfg = load_config()
    found = fetch_stories(cfg)

    # Print a short preview of the first few stories so you can eyeball them.
    preview = found[:5]
    log.info("Showing first %d of %d stories:\n", len(preview), len(found))
    for i, s in enumerate(preview, start=1):
        snippet = s["body"].replace("\n", " ")[:200]
        print(f"\n===== Story {i} =====")
        print(f"r/{s['subreddit']}  |  score {s['score']}  |  "
              f"{s['word_count']} words  |  id {s['post_id']}  |  "
              f"nsfw={s['over_18']}")
        print(f"TITLE: {s['title']}")
        print(f"BODY : {snippet}{'...' if len(s['body']) > 200 else ''}")
