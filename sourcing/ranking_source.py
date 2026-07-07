"""
Ranking story source — "Top 5 ___" countdown lists for the space channel.

Each dataset JSON in sourcing/ranking_data/ is ONE ranking = ONE video:
  {title, hook, category, items:[{rank, name, stat, query}]}
where `query` is the NASA image search term for that item (see media/clip_source).

Exposes fetch_stories(config, skip_seen) matching the standard story-dict
contract (post_id/subreddit/title/body/...) so it drops into the pipeline; the
whole ranking travels as JSON in `body`, and video/ranking_assemble.py renders it.

Config (config.yaml):
  ranking:
    dataset_dir: sourcing/ranking_data
"""

import hashlib
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdrop_common import setup_logging
from database import db

log = setup_logging()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIR = "sourcing/ranking_data"


def _dataset_dir(config: dict) -> Path:
    rel = config.get("ranking", {}).get("dataset_dir", DEFAULT_DIR)
    return (PROJECT_ROOT / rel).resolve()


def _post_id(title: str) -> str:
    return "rank_" + hashlib.sha1(title.encode("utf-8")).hexdigest()[:10]


def fetch_stories(config: dict, skip_seen: bool = True) -> list[dict]:
    """Return unused ranking lists as story dicts (shuffled)."""
    ddir = _dataset_dir(config)
    if not ddir.exists():
        raise FileNotFoundError(f"Ranking dataset dir not found: {ddir}")

    files = sorted(ddir.glob("*.json"))
    random.shuffle(files)                       # vary which topic goes next
    stories: list[dict] = []
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("[ranking] bad dataset %s: %s", path.name, e)
            continue
        if not data.get("items") or not data.get("title"):
            continue
        post_id = _post_id(data["title"])
        if skip_seen and db.post_already_seen(post_id):
            continue
        stories.append({
            "post_id": post_id,
            "subreddit": data.get("category", "ranking"),   # category tag
            "title": data["title"],
            "body": json.dumps(data, ensure_ascii=False),   # full list for the renderer
            "score": 0,
            "over_18": False,
            "word_count": len(data["items"]) * 20,          # nominal; skips length filters
        })

    log.info("[ranking] %d unused ranking list(s) available in %s.",
             len(stories), ddir.name)
    return stories


if __name__ == "__main__":
    from agentdrop_common import load_config
    db.init_db()
    cfg = load_config()
    cfg.setdefault("ranking", {})
    for s in fetch_stories(cfg, skip_seen=False):
        d = json.loads(s["body"])
        print(f"\n{s['post_id']}  [{s['subreddit']}]  {s['title']}")
        for it in d["items"]:
            print(f"   #{it['rank']} {it['name']:14} — {it['stat']}   (img: {it['query']})")
