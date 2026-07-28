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
from typing import Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdrop_common import setup_logging
from database import db

log = setup_logging()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIR = "sourcing/ranking_data"
# "Already posted" ledger — a CACHE of what the channel already holds, not the
# source of truth. Neither the runtime DB nor this file survives the cloud: the
# SQLite DB resets on redeploy and Railway's filesystem is ephemeral, so a
# mark_posted() written by a cloud run is gone by the next deploy. That is
# exactly how "Emptiest Places" shipped twice (2026-07-23 rMSp7ciBiUo and
# 2026-07-28 n4zx3Z5fyOg, same dataset, same title). The AUTHORITATIVE record is
# the live channel itself — see reconcile_with_channel(), which runs before every
# selection and re-derives this ledger from the uploads playlist.
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


# --- authoritative anti-duplicate: reconcile against the LIVE channel ---------

def _norm_title(t: str) -> str:
    """Comparable form of a video title: lowercase, alphanumerics only.

    Strips emoji/punctuation/casing so the same title uploaded twice matches
    regardless of how it was typed."""
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def channel_titles(limit: int = 300) -> dict[str, str]:
    """{normalized title -> youtube_id} for every video on the channel.

    This is the ONE record that survives redeploys and DB wipes, so it drives
    duplicate detection. Raises on auth/API failure — callers decide whether a
    lookup miss should block production."""
    from upload.youtube_upload import get_authenticated_service

    yt = get_authenticated_service()
    ch = yt.channels().list(part="contentDetails", mine=True).execute()["items"][0]
    uploads = ch["contentDetails"]["relatedPlaylists"]["uploads"]
    out: dict[str, str] = {}
    page = None
    while len(out) < limit:
        resp = yt.playlistItems().list(part="snippet", playlistId=uploads,
                                       maxResults=50, pageToken=page).execute()
        for it in resp.get("items", []):
            sn = it["snippet"]
            out.setdefault(_norm_title(sn["title"]), sn["resourceId"]["videoId"])
        page = resp.get("nextPageToken")
        if not page:
            break
    return out


def reconcile_with_channel(config: dict | None = None) -> int:
    """Mark every dataset that ALREADY exists on the channel as posted.

    A dataset is matched by its on-screen title or ANY of the YouTube title
    variants baked into it — the uploader only ever picks from that set, so a
    hit means this exact list has been published. Returns how many NEW datasets
    were marked. Best-effort: any API/auth problem logs and returns 0 rather
    than blocking the day's video (the ledger + DB still apply)."""
    try:
        live = channel_titles()
    except Exception as e:
        log.warning("[ranking] channel reconcile unavailable (%s); relying on "
                    "the committed ledger only.", e)
        return 0
    if not live:
        return 0

    known = posted_ids()
    marked = 0
    for path, data in _datasets(config or {}):
        pid = _post_id(data["title"])
        if pid in known:
            continue
        for cand in [data["title"], *(data.get("yt_titles") or [])]:
            vid = live.get(_norm_title(cand))
            if vid:
                mark_posted(data["title"], youtube_id=vid,
                            note="reconciled from live channel")
                marked += 1
                break
    if marked:
        log.info("[ranking] reconcile: marked %d dataset(s) already live on the "
                 "channel. COMMIT sourcing/ranking_posted.json.", marked)
    return marked


# --- near-duplicate detection (same subject, different wording) ---------------

# Superlatives carry no subject information: "BIGGEST Black Holes" and "Most
# MASSIVE Black Holes" are the same video (they shared 4/5 objects and one had
# to be pulled). Drop them when comparing subjects.
_SUPERLATIVES = {
    "top", "most", "best", "worst", "biggest", "largest", "massive", "huge",
    "giant", "enormous", "greatest", "strangest", "weirdest", "craziest",
    "wildest", "scariest", "creepiest", "deadliest", "dangerous", "insane",
    "terrifying", "incredible", "amazing", "powerful", "extreme", "violent",
    "emptiest", "loneliest", "brightest", "coldest", "hottest", "fastest",
    "slowest", "longest", "oldest", "iconic", "important", "famous",
}
_STOPWORDS = {
    "the", "a", "an", "in", "of", "on", "our", "we", "you", "your", "is", "are",
    "that", "this", "it", "and", "to", "ever", "really", "even", "universe",
    "space", "cosmos", "cosmic", "5", "five", "solar", "system", "known",
}


def _stem(w: str) -> str:
    """Crude stem so plural/superlative variants of one subject compare equal."""
    for suf in ("iest", "est", "ies", "es", "s"):
        if len(w) > len(suf) + 2 and w.endswith(suf):
            return w[:-len(suf)] + ("y" if suf == "ies" else "")
    return w


def subject_key(title: str) -> frozenset[str]:
    """The SUBJECT of a ranking title, superlatives and filler removed.

    'Top 5 BIGGEST Black Holes In The Universe' and 'Top 5 Most MASSIVE Black
    Holes' both reduce to {'black','hole'} — a collision we must block. When
    stripping leaves too little to distinguish two lists (e.g. both are just
    'places'), the superlative goes back in, so 'Emptiest Places' and 'Weirdest
    Places' stay separate videos."""
    words = [w for w in re.split(r"\W+", (title or "").lower()) if w]
    body = [_stem(w) for w in words if w not in _STOPWORDS and w not in _SUPERLATIVES]
    if len(set(body)) < 2:                       # too thin — keep the superlative
        body += [_stem(w) for w in words
                 if w in _SUPERLATIVES and w not in ("top", "most")]
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
            log.warning("[ranking] bad dataset %s: %s", path.name, e)
            continue
        if data.get("items") and data.get("title"):
            out.append((path, data))
    return out


def is_near_duplicate(data: dict, posted: Iterable[dict]) -> str | None:
    """Why `data` duplicates something already posted, or None if it's fresh.

    Two independent tests, because the wording and the content can each drift:
      * same SUBJECT after superlatives are stripped, and
      * 3+ of the 5 ranked objects shared with an already-posted list.
    """
    key, names = subject_key(data["title"]), _item_names(data)
    for other in posted:
        if key and key == subject_key(other["title"]):
            return f"same subject as posted {other['title']!r}"
        shared = names & _item_names(other)
        if len(shared) >= 3:
            return (f"{len(shared)}/5 objects shared with posted "
                    f"{other['title']!r} ({', '.join(sorted(shared))})")
    return None


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


_RECONCILED = False           # channel lookup is once per process, not per call


def fetch_stories(config: dict, skip_seen: bool = True) -> list[dict]:
    """Return unused ranking lists as story dicts (shuffled)."""
    global _RECONCILED
    ddir = _dataset_dir(config)
    if not ddir.exists():
        raise FileNotFoundError(f"Ranking dataset dir not found: {ddir}")

    # Re-derive "already posted" from the live channel before choosing anything.
    # The ledger and the DB both go stale in the cloud; the channel does not.
    if skip_seen and not _RECONCILED:
        _RECONCILED = True
        reconcile_with_channel(config)

    datasets = _datasets(config)
    random.shuffle(datasets)                    # vary which topic goes next
    already_posted = posted_ids() if skip_seen else set()
    # Datasets we know are live — the reference set for near-duplicate checks.
    posted_data = [d for _p, d in datasets if _post_id(d["title"]) in already_posted]
    seen_titles: set[str] = set()               # guard identical-title dup files
    stories: list[dict] = []
    for path, data in datasets:
        post_id = _post_id(data["title"])
        # Skip anything already posted per EITHER the durable ledger or the DB,
        # and never surface two datasets with the same title in one run.
        if skip_seen and (post_id in already_posted or db.post_already_seen(post_id)):
            continue
        # Same subject re-worded, or the same objects re-ranked, still reads as
        # duplicate content to YouTube — block it even though the title differs.
        if skip_seen:
            why = is_near_duplicate(data, posted_data)
            if why:
                log.info("[ranking] near-duplicate skipped (%s): %s", why, path.name)
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
    from agentdrop_common import load_config

    # `python -m sourcing.ranking_source reconcile` re-derives the posted-ledger
    # from the live channel (run it any time the ledger looks behind).
    if len(sys.argv) > 1 and sys.argv[1] == "reconcile":
        cfg = load_config()
        cfg.setdefault("ranking", {})
        print(f"marked {reconcile_with_channel(cfg)} newly-recognized dataset(s).")
        sys.exit(0)

    # `python -m sourcing.ranking_source mark "Top 5 ..." [youtube_id]` records a
    # (manual) upload into the durable posted-ledger so it's never posted again.
    if len(sys.argv) > 2 and sys.argv[1] == "mark":
        import datetime
        mark_posted(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "",
                    datetime.date.today().isoformat(), "manual")
        print(f"marked posted: {sys.argv[2]}")
        sys.exit(0)

    db.init_db()
    cfg = load_config()
    cfg.setdefault("ranking", {})
    for s in fetch_stories(cfg, skip_seen=False):
        d = json.loads(s["body"])
        print(f"\n{s['post_id']}  [{s['subreddit']}]  {s['title']}")
        for it in d["items"]:
            print(f"   #{it['rank']} {it['name']:14} — {it['stat']}   (img: {it['query']})")
