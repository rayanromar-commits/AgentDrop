"""
Story-source switchboard.

Looks at config.yaml's `story_source` setting and routes to either the
manual file reader or the live Reddit API. The rest of AgentDrop calls
fetch_stories() here and doesn't need to know which source was used.

Test it:   python3 sourcing/get_stories.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdrop_common import load_config, setup_logging
from database import db

log = setup_logging()


def fetch_stories(config: dict, skip_seen: bool = True) -> list[dict]:
    source = config.get("story_source", "manual").lower()

    if source == "manual":
        from sourcing import manual_source
        return manual_source.fetch_stories(config, skip_seen=skip_seen)
    elif source == "reddit":
        from sourcing import reddit_source
        return reddit_source.fetch_stories(config, skip_seen=skip_seen)
    else:
        raise ValueError(
            f"Unknown story_source '{source}' in config.yaml. "
            "Use 'manual' or 'reddit'."
        )


if __name__ == "__main__":
    db.init_db()
    cfg = load_config()
    log.info("Story source is set to: %s", cfg.get("story_source"))

    found = fetch_stories(cfg)
    preview = found[:5]
    log.info("Showing first %d of %d stories:", len(preview), len(found))
    for i, s in enumerate(preview, start=1):
        snippet = s["body"].replace("\n", " ")[:200]
        print(f"\n===== Story {i} =====")
        print(f"source r/{s['subreddit']}  |  score {s['score']}  |  "
              f"{s['word_count']} words  |  id {s['post_id']}  |  "
              f"nsfw={s['over_18']}")
        print(f"TITLE: {s['title']}")
        print(f"BODY : {snippet}{'...' if len(s['body']) > 200 else ''}")
