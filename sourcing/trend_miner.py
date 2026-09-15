"""
Trend miner — find what's ACTUALLY working on YouTube in our niche.

Until now the clip channel's topics came from a list hand-written in
clip_ranking_generate.py, weighted only by our own engagement. That means the
channel could only ever learn from its own history — and a channel stuck at
1,000 views per video has very little to teach itself.

This searches YouTube for recent, high-performing Shorts in the niche and hands
the winners to the generator, so new topics are chosen from evidence rather than
from a list someone guessed at.

Uses the YouTube Data API we already authenticate for. Quota: search.list costs
100 units and videos.list costs 1, against a 10,000/day default — a refresh is
~15 searches, so about 15% of a day's quota, and results are cached for a week.

    python3 -m sourcing.trend_miner            # show what's trending now
    python3 -m sourcing.trend_miner refresh    # force a re-fetch
"""

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdrop_common import setup_logging

log = setup_logging()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE = PROJECT_ROOT / "sourcing" / "trend_cache.json"
CACHE_DAYS = 7          # a week-old read of "what's working" is still current
LOOKBACK_DAYS = 90      # only count recent uploads, not all-time monuments
MIN_VIEWS = 50_000      # below this it isn't evidence of anything

# Seed searches. These describe the LANE, not individual videos — the miner's job
# is to discover which subjects inside the lane are pulling views right now, so
# the seeds stay broad and the results supply the specifics.
SEEDS = [
    "ranking deadliest animals shorts",
    "most dangerous animals shorts",
    "deadliest predator shorts",
    "most venomous animal shorts",
    "strongest bite force animal shorts",
    "fastest animal shorts",
    "biggest animal ever shorts",
    "weirdest animal shorts",
    "rarest animal shorts",
    "apex predator shorts",
    "deep sea creature shorts",
    "animal survival shorts",
]

# Words that carry no subject information — stripped when distilling a title
# down to the thing it is actually about.
_NOISE = {
    "the", "a", "an", "of", "in", "on", "at", "to", "for", "and", "or", "is",
    "are", "was", "were", "this", "that", "it", "its", "you", "your", "my",
    "i", "we", "he", "she", "they", "most", "top", "best", "worst", "ranking",
    "ranked", "rank", "shorts", "short", "video", "youtube", "subscribe",
    "like", "follow", "part", "ever", "world", "earth", "vs", "new", "real",
    "crazy", "insane", "amazing", "incredible", "unbelievable", "shocking",
    "facts", "fact", "did", "know", "what", "why", "how", "when", "which",
}


# A broad search inevitably drags in gaming, animation and non-English uploads
# that share a keyword. They're real videos with real views, but they say nothing
# about what OUR channel should make, and left in they poison the topic list.
_OFF_TOPIC = (
    "roblox", "minecraft", "fortnite", "gta", "gameplay", "anime", "cartoon",
    "animation", "meme", "troll", "edit", "tiktok compilation", "movie",
    "trailer", "reaction", "prank", "asmr", "song", "lyrics", "gacha",
)


# The niche is wildlife — animals, predators, the deep sea. A broad search also
# returns genuinely popular videos about floods, fishing trips and travel that
# happen to be "dangerous", and one of those ("how to survive a flood") got
# through and produced a "Ranking Most Dangerous Floods" dataset. A mined topic
# has to be about a CREATURE to be usable.
_NICHE_WORDS = (
    "animal", "creature", "predator", "shark", "snake", "spider", "fish",
    "whale", "octopus", "squid", "jellyfish", "sea", "ocean", "deep",
    "venom", "poison", "bite", "insect", "bug", "scorpion", "crocodile",
    "alligator", "bear", "lion", "tiger", "wolf", "cat", "dog", "bird",
    "eagle", "owl", "reptile", "lizard", "frog", "beast", "species",
    "wildlife", "dinosaur", "prehistoric", "extinct", "hunter", "prey",
    "claw", "fang", "jaw", "tentacle", "abyss", "trench", "reef", "gorilla",
    "monkey", "ape", "elephant", "rhino", "hippo", "orca", "dolphin", "ray",
    "eel", "worm", "bat", "rodent", "mammal", "amphibian", "parasite",
)


# The comedy lane — "funniest dog moments", "weird cats but funny" — is about
# animals and performs enormously, but it runs on scraped user clips of peak
# comic moments that no stock library holds. Mining it would hand the generator
# topics we structurally cannot serve, and we'd ship calm stock animals under a
# title promising laughs.
_UNSERVABLE = (
    "funny", "funniest", "hilarious", "fail", "prank", "moment", "reaction",
    "caught on", "gone wrong", "tiktok", "vlog", "unboxing", "challenge",
    # "Animal Hospital" is a Roblox game whose clips flood any animal search;
    # the word "roblox" often falls out when a title is distilled, so catch the
    # game's own vocabulary too.
    "hospital", "anomaly", "animal company", "obby", "lore",
)


def on_niche(text: str) -> bool:
    """Is this subject about a creature, and something we can actually film?

    Applied to MINED topics before they reach the generator. Without it a
    popular flood-survival vlog becomes a wildlife channel's video topic, and a
    Roblox animal-hospital game becomes a wildlife ranking."""
    t = (text or "").lower()
    if any(w in t for w in _OFF_TOPIC):
        return False                       # gaming/animation keyword collision
    if any(w in t for w in _UNSERVABLE):
        return False                       # real, popular, and closed to us
    return any(w in t for w in _NICHE_WORDS)


def _relevant(title: str) -> bool:
    """Is this title usable as evidence for what we should make?

    Rejects off-topic keyword collisions and anything not predominantly written
    in the Latin alphabet — a 15M-view Arabic or Thai Short is a real result,
    but it tells us nothing we can act on for an English channel."""
    t = title.lower()
    if any(w in t for w in _OFF_TOPIC):
        return False
    letters = [c for c in title if c.isalpha()]
    if not letters:
        return False
    latin = sum(1 for c in letters if c.isascii())
    return latin / len(letters) >= 0.7


def _service():
    from upload.youtube_upload import get_authenticated_service
    return get_authenticated_service()


def _load_cache() -> dict:
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _fresh(cache: dict) -> bool:
    ts = cache.get("fetched_at")
    if not ts:
        return False
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(ts)
    except Exception:
        return False
    return age < timedelta(days=CACHE_DAYS)


def fetch_trending(seeds: list[str] | None = None,
                   lookback_days: int = LOOKBACK_DAYS) -> list[dict]:
    """Top recent Shorts across the seed searches, newest-performing first.

    Returns [{title, views, channel, video_id}]. Returns [] on any API failure —
    the generator must still work when this is unavailable."""
    seeds = seeds or SEEDS
    after = (datetime.now(timezone.utc)
             - timedelta(days=lookback_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        yt = _service()
    except Exception as e:
        log.warning("[trends] YouTube API unavailable (%s); skipping.", e)
        return []

    ids: dict[str, str] = {}                    # video_id -> seed that found it
    for seed in seeds:
        try:
            r = yt.search().list(part="id", q=seed, type="video",
                                 videoDuration="short", order="viewCount",
                                 publishedAfter=after, maxResults=20).execute()
        except Exception as e:
            log.warning("[trends] search failed for %r: %s", seed, e)
            continue
        for it in r.get("items", []):
            vid = it.get("id", {}).get("videoId")
            if vid:
                ids.setdefault(vid, seed)

    if not ids:
        return []

    rows: list[dict] = []
    all_ids = list(ids)
    for i in range(0, len(all_ids), 50):
        chunk = all_ids[i:i + 50]
        try:
            r = yt.videos().list(part="snippet,statistics",
                                 id=",".join(chunk)).execute()
        except Exception as e:
            log.warning("[trends] stats failed: %s", e)
            continue
        for v in r.get("items", []):
            views = int(v.get("statistics", {}).get("viewCount", 0))
            if views < MIN_VIEWS:
                continue
            title = v["snippet"]["title"]
            if not _relevant(title):
                continue
            rows.append({
                "title": title,
                "views": views,
                "channel": v["snippet"]["channelTitle"],
                "video_id": v["id"],
                "seed": ids.get(v["id"], ""),
            })
    rows.sort(key=lambda r: -r["views"])
    return rows


def _subject(title: str) -> str:
    """Distil a title down to the subject it's about.

    'Ranking The DEADLIEST Snakes In The World 🐍 #shorts' -> 'deadliest snakes'
    Emoji, hashtags and hype words carry no subject, so they go."""
    t = re.sub(r"#\w+", " ", title)
    t = re.sub(r"[^\w\s]", " ", t)                       # emoji + punctuation
    words = [w.lower() for w in t.split() if len(w) > 2]
    keep = [w for w in words if w not in _NOISE and not w.isdigit()]
    return " ".join(keep[:5])


def trending_topics(limit: int = 30, refresh: bool = False) -> list[str]:
    """Short subject phrases, ordered by how well they're performing.

    Cached for a week so a daily production run doesn't spend quota or wait on
    the network. Returns [] if the API is unreachable and nothing is cached —
    callers fall back to their own topic list."""
    cache = _load_cache()
    if not refresh and _fresh(cache) and cache.get("rows"):
        rows = cache["rows"]
    else:
        rows = fetch_trending()
        if rows:
            try:
                CACHE.write_text(json.dumps(
                    {"fetched_at": datetime.now(timezone.utc).isoformat(),
                     "rows": rows[:200]}, indent=2, ensure_ascii=False),
                    encoding="utf-8")
            except Exception as e:
                log.warning("[trends] could not write cache: %s", e)
        elif cache.get("rows"):
            log.info("[trends] fetch failed; using the cached read.")
            rows = cache["rows"]                 # stale beats nothing

    seen, out = set(), []
    for r in rows:
        s = _subject(r["title"])
        if len(s.split()) < 2:
            continue
        key = frozenset(s.split())
        if key in seen:
            continue
        if not on_niche(s):
            continue                       # popular, but not about a creature
        seen.add(key)
        out.append(s)
        if len(out) >= limit:
            break
    return out


def winning_titles(limit: int = 25, refresh: bool = False) -> list[dict]:
    """The raw top performers — title + views — to show the generator what a
    working video in this lane actually looks like."""
    cache = _load_cache()
    if not refresh and _fresh(cache) and cache.get("rows"):
        rows = cache["rows"]
    else:
        rows = fetch_trending() or cache.get("rows", [])
    return rows[:limit]


if __name__ == "__main__":
    refresh = len(sys.argv) > 1 and sys.argv[1] == "refresh"
    rows = winning_titles(25, refresh=refresh)
    if not rows:
        print("no trend data (API unavailable and no cache)")
        sys.exit(1)
    print(f"{len(rows)} top performers in the niche:\n")
    for r in rows:
        print(f"  {r['views']:>10,}  {r['channel'][:22]:24} {r['title'][:62]}")
    print("\ndistilled subjects:")
    for s in trending_topics(20):
        print("  -", s)
