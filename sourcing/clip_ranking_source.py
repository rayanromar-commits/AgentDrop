"""
Ranked-clip story source — "Ranking ___ Moments" lists for the clip channel.

Each dataset JSON in sourcing/clip_ranking_data/ is ONE ranking = ONE video:
  {title, category, items:[{rank, name, label, queries}]}
where `queries` are STOCK VIDEO search terms for that item (see media/video_source).

Exposes fetch_stories(config, skip_seen) matching the standard story-dict
contract (post_id/subreddit/title/body/...) so it drops into the pipeline; the
whole ranking travels as JSON in `body`, and video/clip_assemble.py renders it.

This is a sibling of sourcing/ranking_source.py, not a replacement — the space
channel's datasets, ledger and post_id namespace stay untouched so the pivot is
revertible with a one-line config change.

Config (config.yaml):
  clipranking:
    dataset_dir: sourcing/clip_ranking_data
"""

import hashlib
import json
import random
import re
import sys
from pathlib import Path
from typing import Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdrop_common import setup_logging
from database import db

log = setup_logging()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIR = "sourcing/clip_ranking_data"
# "Already posted" ledger — a CACHE, not the source of truth. Neither the runtime
# DB nor this file survives the cloud (SQLite resets on redeploy, Railway's FS is
# ephemeral), which is exactly how the space channel shipped "Emptiest Places"
# twice. The AUTHORITATIVE record is the live channel — see reconcile_with_channel.
POSTED_LEDGER = PROJECT_ROOT / "sourcing" / "clip_ranking_posted.json"


def _dataset_dir(config: dict) -> Path:
    rel = config.get("clipranking", {}).get("dataset_dir", DEFAULT_DIR)
    return (PROJECT_ROOT / rel).resolve()


def _post_id(title: str) -> str:
    # Distinct prefix from the space channel's "rank_" so the two formats can
    # never collide in the shared DB while both exist.
    return "clip_" + hashlib.sha1(title.encode("utf-8")).hexdigest()[:10]


def _load_posted() -> dict:
    """Read the committed posted-ledger. Returns an empty one if missing."""
    try:
        return json.loads(POSTED_LEDGER.read_text(encoding="utf-8"))
    except Exception:
        return {"posted": []}


def posted_ids() -> set[str]:
    """post_ids of every dataset the ledger records as already uploaded."""
    return {e["post_id"] for e in _load_posted().get("posted", [])
            if isinstance(e, dict) and e.get("post_id")}


# --- authoritative anti-duplicate: reconcile against the LIVE channel ---------

def _norm_title(t: str) -> str:
    """Comparable form of a video title: lowercase, alphanumerics only."""
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def channel_titles(limit: int = 300) -> dict[str, str]:
    """{normalized title -> youtube_id} for every video on the channel.

    Shared with the space channel (same YouTube account), so during the pivot
    this correctly sees BOTH formats' uploads."""
    from sourcing.ranking_source import channel_titles as _titles
    return _titles(limit)


def reconcile_with_channel(config: dict | None = None) -> int:
    """Mark every dataset that ALREADY exists on the channel as posted.

    The clipranking title IS the YouTube title (the genre template is the whole
    point), so a single normalized-title match is conclusive — no variant list to
    check, unlike the space datasets. Best-effort: an API/auth problem logs and
    returns 0 rather than blocking the day's video."""
    try:
        live = channel_titles()
    except Exception as e:
        log.warning("[clip] channel reconcile unavailable (%s); relying on the "
                    "committed ledger only.", e)
        return 0
    if not live:
        return 0

    known = posted_ids()
    marked = 0
    for _path, data in _datasets(config or {}):
        pid = _post_id(data["title"])
        if pid in known:
            continue
        vid = live.get(_norm_title(data["title"]))
        if vid:
            mark_posted(data["title"], youtube_id=vid,
                        note="reconciled from live channel")
            marked += 1
    if marked:
        log.info("[clip] reconcile: marked %d dataset(s) already live on the "
                 "channel. COMMIT sourcing/clip_ranking_posted.json.", marked)
    return marked


# --- near-duplicate detection (same subject, different wording) ---------------

# The genre template contributes the same words to every single title
# ("Ranking Best <X> Moments"), so they carry zero subject information and must
# be stripped before two titles are compared — otherwise every video looks like
# a duplicate of every other one.
_SUPERLATIVES = {
    "ranking", "ranked", "best", "funniest", "craziest", "wildest", "most",
    "satisfying", "worst", "greatest", "insane", "amazing", "incredible",
    "epic", "ultimate", "top", "biggest", "fastest", "scariest",
}
_STOPWORDS = {
    "the", "a", "an", "in", "of", "on", "our", "we", "you", "your", "is", "are",
    "that", "this", "it", "and", "to", "ever", "really", "even",
    "moment", "moments", "fail", "fails", "clip", "clips", "video", "videos",
    "5", "five",
}


def _stem(w: str) -> str:
    """Crude stem so plural/superlative variants of one subject compare equal."""
    for suf in ("iest", "est", "ies", "es", "s"):
        if len(w) > len(suf) + 2 and w.endswith(suf):
            return w[:-len(suf)] + ("y" if suf == "ies" else "")
    return w


def subject_key(title: str) -> frozenset[str]:
    """The SUBJECT of a title, template words and superlatives removed.

    'Ranking Best Shark Moments' and 'Ranking Craziest Shark Fails' both reduce
    to {'shark'} — a collision we must block. When stripping leaves too little to
    tell two lists apart, the superlative goes back in so e.g. 'Best Dog Moments'
    and 'Funniest Dog Fails' can still be separated deliberately."""
    words = [w for w in re.split(r"\W+", (title or "").lower()) if w]
    body = [_stem(w) for w in words
            if w not in _STOPWORDS and w not in _SUPERLATIVES]
    if len(set(body)) < 1:
        body += [_stem(w) for w in words
                 if w in _SUPERLATIVES and w not in ("ranking", "ranked", "top", "most")]
    return frozenset(body)


def _item_names(data: dict) -> set[str]:
    return {_stem(re.sub(r"\W+", "", (it.get("name") or "").lower()))
            for it in data.get("items", []) if it.get("name")}


def _datasets(config: dict) -> list[tuple[Path, dict]]:
    """Every readable dataset on disk as (path, parsed)."""
    out = []
    for path in sorted(_dataset_dir(config).glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("[clip] bad dataset %s: %s", path.name, e)
            continue
        if data.get("items") and data.get("title"):
            out.append((path, data))
    return out


def is_near_duplicate(data: dict, posted: Iterable[dict]) -> str | None:
    """Why `data` duplicates something already posted, or None if it's fresh.

    Two independent tests, because the wording and the content can each drift:
      * same SUBJECT after template words are stripped, and
      * 3+ of the 5 ranked subjects shared with an already-posted list.
    """
    key, names = subject_key(data["title"]), _item_names(data)
    for other in posted:
        if key and key == subject_key(other["title"]):
            return f"same subject as posted {other['title']!r}"
        shared = names & _item_names(other)
        if len(shared) >= 3:
            return (f"{len(shared)}/5 subjects shared with posted "
                    f"{other['title']!r} ({', '.join(sorted(shared))})")
    return None


def mark_posted(title: str, youtube_id: str = "", date: str = "",
                note: str = "") -> None:
    """Append a dataset to the durable posted-ledger (idempotent by post_id)."""
    pid = _post_id(title)
    data = _load_posted()
    entries = data.setdefault("posted", [])
    if any(isinstance(e, dict) and e.get("post_id") == pid for e in entries):
        return
    entries.append({"post_id": pid, "title": title, "youtube_id": youtube_id,
                    "date": date, "note": note})
    try:
        POSTED_LEDGER.parent.mkdir(parents=True, exist_ok=True)
        POSTED_LEDGER.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                                 encoding="utf-8")
        log.info("[clip] marked posted in ledger: %s (%s)", title, pid)
    except Exception as e:
        log.warning("[clip] could not write posted-ledger: %s", e)


def youtube_title(dataset_title: str, seed: str = "",
                  variants: list[str] | None = None) -> str:
    """The YouTube title for this ranking — the dataset title, verbatim.

    Deliberately NOT the space channel's rotate-through-variants behaviour. In
    this genre the template title IS the brand: every reference channel that
    works from a standing start (Mugi 999K subs / 33 videos, Xiro 1.53M) posts
    plain "Ranking Best X Moments" every time. And on the Shorts feed there is no
    thumbnail grid, so title variety was solving a problem this surface doesn't
    have. Signature kept for drop-in compatibility with ranking_source.
    """
    return (dataset_title or "").strip()[:95]


_RECONCILED = False           # channel lookup is once per process, not per call


def fetch_stories(config: dict, skip_seen: bool = True) -> list[dict]:
    """Return unused ranked-clip lists as story dicts (shuffled)."""
    global _RECONCILED
    ddir = _dataset_dir(config)
    if not ddir.exists():
        raise FileNotFoundError(f"Clip dataset dir not found: {ddir}")

    # Re-derive "already posted" from the live channel before choosing anything.
    if skip_seen and not _RECONCILED:
        _RECONCILED = True
        reconcile_with_channel(config)

    datasets = _datasets(config)
    random.shuffle(datasets)                    # vary which topic goes next
    already_posted = posted_ids() if skip_seen else set()
    posted_data = [d for _p, d in datasets if _post_id(d["title"]) in already_posted]
    seen_titles: set[str] = set()
    stories: list[dict] = []
    for path, data in datasets:
        post_id = _post_id(data["title"])
        if skip_seen and (post_id in already_posted or db.post_already_seen(post_id)):
            continue
        if skip_seen:
            why = is_near_duplicate(data, posted_data)
            if why:
                log.info("[clip] near-duplicate skipped (%s): %s", why, path.name)
                continue
        if post_id in seen_titles:
            log.warning("[clip] duplicate-title dataset skipped: %s", path.name)
            continue
        seen_titles.add(post_id)
        stories.append({
            "post_id": post_id,
            "subreddit": data.get("category", "clipranking"),   # category tag
            "title": data["title"],
            "body": json.dumps(data, ensure_ascii=False),       # full list for the renderer
            "score": 0,
            "over_18": False,
            "word_count": len(data["items"]) * 20,              # nominal; skips length filters
        })

    log.info("[clip] %d unused ranked-clip list(s) available in %s.",
             len(stories), ddir.name)
    return stories


if __name__ == "__main__":
    from agentdrop_common import load_config

    cfg = load_config()
    cfg.setdefault("clipranking", {})
    if len(sys.argv) > 1 and sys.argv[1] == "reconcile":
        n = reconcile_with_channel(cfg)
        print(f"marked {n} dataset(s) as already posted.")
    elif len(sys.argv) > 2 and sys.argv[1] == "mark":
        mark_posted(sys.argv[2], youtube_id=sys.argv[3] if len(sys.argv) > 3 else "",
                    note="manual")
        print("marked.")
    else:
        for s in fetch_stories(cfg):
            print(f"  {s['post_id']}  {s['title']}")
