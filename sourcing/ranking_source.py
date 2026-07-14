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
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdrop_common import setup_logging
from database import db

log = setup_logging()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIR = "sourcing/ranking_data"
# DURABLE, committed "already posted" ledger. The runtime DB (db.post_already_seen)
# has proven unreliable for this channel — manual uploads bypassed it and the
# Railway DB can reset — which let an already-posted dataset (e.g. "Windiest
# Planets") get re-selected and re-rendered as duplicate content. This in-repo
# ledger ships with every deploy and is checked ALONGSIDE the DB, so a dataset is
# never posted twice regardless of DB state.
POSTED_LEDGER = PROJECT_ROOT / "sourcing" / "ranking_posted.json"


def _dataset_dir(config: dict) -> Path:
    rel = config.get("ranking", {}).get("dataset_dir", DEFAULT_DIR)
    return (PROJECT_ROOT / rel).resolve()


def _post_id(title: str) -> str:
    return "rank_" + hashlib.sha1(title.encode("utf-8")).hexdigest()[:10]


def _load_posted() -> dict:
    """Read the committed posted-ledger. Returns {} if missing/unreadable."""
    try:
        return json.loads(POSTED_LEDGER.read_text(encoding="utf-8"))
    except Exception:
        return {"posted": []}


def posted_ids() -> set[str]:
    """post_ids of every dataset the ledger records as already uploaded."""
    return {e["post_id"] for e in _load_posted().get("posted", [])
            if isinstance(e, dict) and e.get("post_id")}


def mark_posted(title: str, youtube_id: str = "", date: str = "",
                note: str = "") -> None:
    """Append a dataset to the durable posted-ledger (idempotent by post_id) so it
    is never selected again — the reliable anti-duplicate record. Called on every
    automated production; call by hand for manual uploads."""
    pid = _post_id(title)
    data = _load_posted()
    entries = data.setdefault("posted", [])
    if any(isinstance(e, dict) and e.get("post_id") == pid for e in entries):
        return
    entries.append({"post_id": pid, "title": title, "youtube_id": youtube_id,
                    "date": date, "note": note})
    try:
        POSTED_LEDGER.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                                 encoding="utf-8")
        log.info("[ranking] marked posted in ledger: %s (%s)", title, pid)
    except Exception as e:
        log.warning("[ranking] could not write posted-ledger: %s", e)


# Varied YouTube (metadata) titles so the channel isn't every-day "Top 5 Most X
# In The Universe" (that repeated structure reads as duplicate content). The
# on-screen title stays "Top 5 ..." — only the YouTube title rotates.
_YT_TEMPLATES = [
    "Top 5 {s} 🌌", "Ranking the {s}", "The {s}, Ranked", "The Universe's {s}",
    "You Won't Believe the {s} 😳", "These Are the {s}", "I Ranked the {s} 🚀",
    "{s} — From 5 To 1", "The Most Insane {s} In Space", "Nobody Talks About the {s}",
    "Wait Until You See #1 — {s}", "Space Is Scarier Than You Think: {s}",
    "Scientists Ranked the {s}", "{s} That Sound Fake But Are Real",
    "This Is Why Space Terrifies Me — {s}", "I Wasn't Ready for the {s}",
    "The {s} No One Told You About", "Can You Guess the #1 {s}?",
    "{s} (Number 1 Broke My Brain)", "Everything About the {s} Is Wild",
]


def _subject(title: str) -> str:
    """'Top 5 Most DANGEROUS Planets In The Universe' -> 'Most Dangerous Planets'."""
    s = re.sub(r"^\s*top\s*\d+\s*", "", title, flags=re.I)
    s = re.sub(r"\s+in\s+the\s+universe\s*$", "", s, flags=re.I)
    s = re.sub(r"\s+in\s+(our\s+)?solar\s+system\s*$", "", s, flags=re.I)
    return s.strip().title() or title


def youtube_title(dataset_title: str, seed: str,
                  variants: list[str] | None = None) -> str:
    """A varied, non-repetitive YouTube title for this ranking.

    Prefers the wildly-distinct `yt_titles` Claude baked into the dataset (the
    strongest anti-repetition lever); falls back to the template pool for older
    datasets that predate that field.
    """
    rng = random.Random(seed)
    pool = [t.strip() for t in (variants or []) if isinstance(t, str) and t.strip()]
    if pool:
        return rng.choice(pool)[:95]
    return rng.choice(_YT_TEMPLATES).format(s=_subject(dataset_title))[:95]


def fetch_stories(config: dict, skip_seen: bool = True) -> list[dict]:
    """Return unused ranking lists as story dicts (shuffled)."""
    ddir = _dataset_dir(config)
    if not ddir.exists():
        raise FileNotFoundError(f"Ranking dataset dir not found: {ddir}")

    files = sorted(ddir.glob("*.json"))
    random.shuffle(files)                       # vary which topic goes next
    already_posted = posted_ids() if skip_seen else set()
    seen_titles: set[str] = set()               # guard identical-title dup files
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
        # Skip anything already posted per EITHER the durable ledger or the DB,
        # and never surface two datasets with the same title in one run.
        if skip_seen and (post_id in already_posted or db.post_already_seen(post_id)):
            continue
        if post_id in seen_titles:
            log.warning("[ranking] duplicate-title dataset skipped: %s", path.name)
            continue
        seen_titles.add(post_id)
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
    # `python -m sourcing.ranking_source mark "Top 5 ..." [youtube_id]` records a
    # (manual) upload into the durable posted-ledger so it's never posted again.
    if len(sys.argv) > 2 and sys.argv[1] == "mark":
        import datetime
        mark_posted(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "",
                    datetime.date.today().isoformat(), "manual")
        print(f"marked posted: {sys.argv[2]}")
        sys.exit(0)

    from agentdrop_common import load_config
    db.init_db()
    cfg = load_config()
    cfg.setdefault("ranking", {})
    for s in fetch_stories(cfg, skip_seen=False):
        d = json.loads(s["body"])
        print(f"\n{s['post_id']}  [{s['subreddit']}]  {s['title']}")
        for it in d["items"]:
            print(f"   #{it['rank']} {it['name']:14} — {it['stat']}   (img: {it['query']})")
