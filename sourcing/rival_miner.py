"""
Mine the reference channels — the ten ranked-clip channels this format was
copied from — for what is working RIGHT NOW.

This is a different question from the one trend_miner asks. trend_miner searches
YouTube by keyword and finds whatever is big in the lane; it cannot tell a video
that blew up from a video on a channel where everything is big. This module
reads a fixed set of DIRECT COMPETITORS and asks the sharper question:

    which of their videos beat THEIR OWN median, and what are those about?

That baseline is the whole point. Xiro's median video does numbers we will not
see for a year, so raw views say nothing except "they are bigger than us". A
video at 6x its own channel's median, on a channel that posts the same format we
do, is evidence about the SUBJECT and the FRAMING — the two things we actually
choose each day.

Quota is deliberately cheap: channels.list + playlistItems.list + videos.list
cost 1 unit each, so a full pass over ten channels is ~40 units against a
10,000/day budget. trend_miner's search.list calls cost 100 EACH. Results cache
for a week.

Fails SAFE in every direction: no API, no quota, a renamed channel, a parse
failure — all return empty, and the generator falls back to its own topic pool.

    python3 -m sourcing.rival_miner            # show the current outliers
    python3 -m sourcing.rival_miner refresh    # force a re-fetch
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
CACHE = PROJECT_ROOT / "sourcing" / "rival_cache.json"
CACHE_DAYS = 7
PER_CHANNEL = 50          # newest N uploads per channel — one playlist page
LOOKBACK_DAYS = 180       # a subject that worked 6 months ago still informs
OUTLIER_MULT = 2.5        # "beat its own channel" threshold, x channel median

# The ten channels this format was studied from (see the clipranking pivot).
# Handles where we have them, raw channel ids where the handle is ambiguous.
RIVALS = [
    {"handle": "@AuraShorts67"},
    {"handle": "@mugiranks"},
    {"handle": "@Rankexa123"},
    {"handle": "@RankZilla23"},
    {"handle": "@Ryth"},
    {"handle": "@CallMeRanks"},
    {"handle": "@Rankedflow-z4g"},
    {"id": "UC4PPAK9dvRGPHyaedI5diMw", "name": "Xiro Ranks"},
    {"id": "UCACeuchFOKELIEVrWKIR7fQ", "name": "LaylaRanks"},
    {"id": "UCnqtwXNVh-EzU8fYcxGhSnA", "name": "Polar Ranks"},
]

# The superlatives our own title template can use. Mining which ones appear in
# rivals' outliers tells us which FRAMING travels, independently of subject.
SUPERLATIVES = ["deadliest", "most dangerous", "most venomous", "strongest",
                "biggest", "fastest", "weirdest", "rarest", "scariest"]

_NOISE = {
    "the", "a", "an", "of", "in", "on", "at", "to", "for", "and", "or", "is",
    "vs", "shorts", "short", "ranking", "ranked", "rank", "tier", "list",
    "top", "best", "worst", "you", "your", "that", "this", "with", "what",
    "which", "who", "are", "was", "were", "part", "ep", "episode",
}


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
        return (datetime.now(timezone.utc)
                - datetime.fromisoformat(ts)) < timedelta(days=CACHE_DAYS)
    except Exception:
        return False


def _median(xs: list[int]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    mid = len(s) // 2
    return float(s[mid]) if len(s) % 2 else (s[mid - 1] + s[mid]) / 2.0


def _resolve(yt, rival: dict) -> tuple[str, str] | None:
    """(channel_id, uploads_playlist_id) for one rival, or None."""
    try:
        if rival.get("id"):
            r = yt.channels().list(part="contentDetails,snippet",
                                   id=rival["id"]).execute()
        else:
            r = yt.channels().list(part="contentDetails,snippet",
                                   forHandle=rival["handle"]).execute()
        items = r.get("items") or []
        if not items:
            log.info("[rivals] %s not found (renamed or deleted?).",
                     rival.get("handle") or rival.get("id"))
            return None
        ch = items[0]
        uploads = (ch["contentDetails"]["relatedPlaylists"]["uploads"])
        return ch["snippet"]["title"], uploads
    except Exception as e:
        log.warning("[rivals] resolve failed for %s: %s",
                    rival.get("handle") or rival.get("id"), e)
        return None


def _channel_videos(yt, uploads_playlist: str) -> list[dict]:
    """Recent uploads on one channel with view counts."""
    try:
        r = yt.playlistItems().list(part="contentDetails",
                                    playlistId=uploads_playlist,
                                    maxResults=PER_CHANNEL).execute()
    except Exception as e:
        log.warning("[rivals] playlist read failed: %s", e)
        return []
    ids, cutoff = [], (datetime.now(timezone.utc)
                       - timedelta(days=LOOKBACK_DAYS))
    for it in r.get("items", []):
        cd = it.get("contentDetails", {})
        vid = cd.get("videoId")
        published = cd.get("videoPublishedAt")
        if not vid:
            continue
        if published:
            try:
                if datetime.fromisoformat(published.replace("Z", "+00:00")) < cutoff:
                    continue
            except Exception:
                pass
        ids.append(vid)
    if not ids:
        return []

    out = []
    for i in range(0, len(ids), 50):
        try:
            r = yt.videos().list(part="snippet,statistics",
                                 id=",".join(ids[i:i + 50])).execute()
        except Exception as e:
            log.warning("[rivals] stats failed: %s", e)
            continue
        for v in r.get("items", []):
            out.append({
                "video_id": v["id"],
                "title": v["snippet"]["title"],
                "views": int(v.get("statistics", {}).get("viewCount", 0) or 0),
                "published": v["snippet"].get("publishedAt", ""),
            })
    return out


def _subject(title: str) -> str:
    """The subject a rival title is about, template words stripped."""
    t = re.sub(r"#\w+", " ", title)
    t = re.sub(r"[^\w\s]", " ", t)
    words = [w.lower() for w in t.split() if len(w) > 2]
    keep = [w for w in words if w not in _NOISE and not w.isdigit()]
    return " ".join(keep[:5])


def _superlative(title: str) -> str:
    """Which of our template superlatives this title uses, if any."""
    low = title.lower()
    for sup in sorted(SUPERLATIVES, key=len, reverse=True):
        if sup in low:
            return sup
    return ""


def fetch_rivals(refresh: bool = False) -> dict:
    """Per-channel medians plus the videos that beat them.

    {fetched_at, channels: [{name, median, n}], outliers: [{title, views,
     multiple, channel, subject, superlative}]}
    """
    cache = _load_cache()
    if not refresh and _fresh(cache):
        return cache

    try:
        yt = _service()
    except Exception as e:
        log.warning("[rivals] YouTube API unavailable (%s).", e)
        return cache or {}

    channels, outliers = [], []
    for rival in RIVALS:
        resolved = _resolve(yt, rival)
        if not resolved:
            continue
        name, uploads = resolved
        vids = _channel_videos(yt, uploads)
        if len(vids) < 5:               # too thin for a meaningful median
            continue
        med = _median([v["views"] for v in vids])
        channels.append({"name": name, "median": med, "n": len(vids)})
        if med <= 0:
            continue
        for v in vids:
            mult = v["views"] / med
            if mult >= OUTLIER_MULT:
                outliers.append({
                    "title": v["title"],
                    "views": v["views"],
                    "multiple": round(mult, 1),
                    "channel": name,
                    "subject": _subject(v["title"]),
                    "superlative": _superlative(v["title"]),
                    "published": v["published"],
                })

    # Rank by how far a video beat its OWN channel, not by raw views — that is
    # the whole reason this module exists.
    outliers.sort(key=lambda o: -o["multiple"])
    result = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "channels": channels,
        "outliers": outliers[:60],
    }
    try:
        CACHE.write_text(json.dumps(result, indent=2, ensure_ascii=False),
                         encoding="utf-8")
    except Exception as e:
        log.warning("[rivals] could not write cache: %s", e)
    log.info("[rivals] %d channel(s) read, %d outlier(s) above %.1fx their own "
             "median.", len(channels), len(outliers), OUTLIER_MULT)
    return result


def winning_subjects(limit: int = 20, refresh: bool = False) -> list[str]:
    """Subjects that beat their own channel's median AND that we can film.

    The servability filter is not optional here, it is the point. A live run of
    this miner returned, in order: "Best Cats Stealing The Spotlight" (135x),
    "Best Tree Cutting Moments" (98x), "Funniest Chicken Scream" (33x). Those
    are real results and they are structurally closed to us — they run on
    scraped user clips of peak comic moments that no stock library holds. Handed
    to the generator unfiltered they would produce topics we can only serve with
    calm stock footage under a title promising chaos, which is the same
    caption-does-not-match-the-screen failure in a different coat.

    `on_niche` (shared with trend_miner) keeps what is left: creature subjects
    stock can actually supply — "Weirdest Sea Creatures", "Best Shark Moments".
    """
    from sourcing.trend_miner import servable

    data = fetch_rivals(refresh=refresh)
    seen, cands = set(), []
    for o in data.get("outliers", []):
        sub = o.get("subject", "").strip()
        if sub and sub not in seen:
            seen.add(sub)
            cands.append(sub)
    out = servable(cands)[:limit]
    log.info("[rivals] %d breakout subject(s) -> %d servable by stock footage.",
             len(cands), len(out))
    return out


def superlative_scores(refresh: bool = False) -> dict[str, float]:
    """Mean outperformance multiple per title superlative.

    Answers "does 'Deadliest' travel further than 'Weirdest'" using rivals'
    data rather than waiting for our own channel to gather a sample.
    """
    data = fetch_rivals(refresh=refresh)
    agg: dict[str, list[float]] = {}
    for o in data.get("outliers", []):
        sup = o.get("superlative")
        if sup:
            agg.setdefault(sup, []).append(float(o.get("multiple", 0)))
    return {k: round(sum(v) / len(v), 2) for k, v in agg.items() if v}


if __name__ == "__main__":
    refresh = len(sys.argv) > 1 and sys.argv[1] == "refresh"
    d = fetch_rivals(refresh=refresh)
    print(f"channels read: {len(d.get('channels', []))}")
    for c in d.get("channels", []):
        print(f"  {c['name']:<24} median {c['median']:>12,.0f}  (n={c['n']})")
    print("\ntop outliers (x their own channel median):")
    for o in d.get("outliers", [])[:20]:
        print(f"  {o['multiple']:>5.1f}x  {o['views']:>12,}  [{o['channel']}] "
              f"{o['title'][:70]}")
    print("\nservable subjects handed to the generator:")
    for sub in winning_subjects(15):
        print(f"  {sub}")
    sup = superlative_scores()
    if sup:
        print("\nsuperlative performance:")
        for k, v in sorted(sup.items(), key=lambda kv: -kv[1]):
            print(f"  {k:<16} {v}x")
