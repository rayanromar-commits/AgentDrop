"""
Ranking list generator — Claude writes new "Top 5 ___" space rankings.

Turns the content ideation into a background job: given a topic (or an invented
one) Claude returns a dramatic, factually-accurate Top-5 with a NASA image query
per item, validated and saved into sourcing/ranking_data/. Run it occasionally
(or on a schedule) to keep the channel stocked.

    python3 -m sourcing.ranking_generate                 # 1 auto topic
    python3 -m sourcing.ranking_generate "biggest black holes"
    python3 -m sourcing.ranking_generate auto 5          # 5 auto topics
"""

import json
import os
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from agentdrop_common import setup_logging

log = setup_logging()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "sourcing" / "ranking_data"
MODEL = "claude-opus-4-8"

# The channel is COSMOS-locked (NASA public-domain image moat), so every topic
# stays in space — but the SUBJECT TYPES and ANGLES are deliberately spread as
# wide as possible. Repetitive/samey topics + titles are what got the old story
# channel suppressed, so breadth here is the product, not a nice-to-have. Keep
# adding rows; the generator picks one per run (performance-weighted, see
# pick_topics).
#
# Topics are grouped so the generator can LEARN which kinds of rankings pull
# engagement: each group maps (via CATEGORY_TO_GROUP) to the per-`category`
# performance the channel measures, and generation leans toward winning groups
# while still exploring quiet ones. Add rows freely; a new topic just needs to
# live under the group it belongs to.
TOPIC_GROUPS: dict[str, list[str]] = {
    "planets": [
        "most dangerous planets", "hottest planets", "coldest planets",
        "strangest exoplanets ever found", "most extreme places in the solar system",
        "planets where it rains something bizarre", "biggest planets ever discovered",
        "most Earth-like exoplanets", "windiest planets", "planets with the wildest storms",
        "planets that shouldn't exist", "loneliest rogue planets drifting in the dark",
        "planets with the longest and shortest days", "most toxic atmospheres in space",
    ],
    "moons": [
        "strangest moons", "moons that could hold alien life", "most volcanic moons",
        "moons with hidden underground oceans", "moons weirder than any planet",
    ],
    "stars": [
        "biggest stars in the universe", "strangest types of stars",
        "closest stars that could go supernova", "most powerful explosions in space",
        "oldest stars in the universe", "stars that break the laws of physics",
        "brightest stars you can actually see", "stars that are already dead",
    ],
    "galaxies": [
        "largest galaxies", "most beautiful nebulae", "biggest black holes",
        "most mysterious objects in space", "strangest signals from space",
        "coldest places in the universe", "brightest objects in the universe",
        "biggest structures in the universe", "emptiest places in the universe",
        "most colorful things in space", "galaxies on a collision course",
    ],
    "phenomena": [
        "deadliest cosmic events", "things that could end all life on Earth from space",
        "most terrifying facts about black holes", "fastest things in the universe",
        "loudest events in the universe", "most powerful forces in the universe",
        "weirdest things Einstein was right about", "cosmic events you could actually survive",
        "slowest processes in the universe", "biggest numbers in astronomy",
    ],
    "history": [
        "most important space missions in history", "people who changed how we see the universe",
        "greatest astronomers of all time", "most famous astronauts in history",
        "most iconic photos NASA ever took", "riskiest moments in spaceflight",
        "most incredible things left on other worlds", "wildest spacecraft ever built",
        "space missions that failed spectacularly", "longest space journeys ever made",
    ],
    "mysteries": [
        "unsolved mysteries of the universe", "cosmic coincidences that seem impossible",
        "things scientists still can't explain about space",
        "what the universe will look like in a trillion years",
        "places in space that would kill you instantly", "most Earth-sized surprises in space",
        "space facts that sound fake but are true", "smallest things in the universe",
    ],
}

# Flattened view (back-compat for callers that just want a random topic).
TOPICS = [t for group in TOPIC_GROUPS.values() for t in group]

# The channel measures performance per dataset `category` (Claude's granular tag,
# e.g. "cosmic voids", "spaceflight events"). Fold those into the coarse groups
# above so a group inherits the engagement of every category it has produced.
# Anything unmatched falls back to a keyword scan, then to "mysteries".
CATEGORY_TO_GROUP: dict[str, str] = {
    "planets": "planets", "solar system": "planets", "extreme places": "planets",
    "exoplanets": "planets",
    "moons": "moons",
    "stars": "stars", "cosmic explosions": "stars",
    "galaxies": "galaxies", "black holes": "galaxies", "nebulae": "galaxies",
    "signals": "galaxies", "cosmic structures": "galaxies", "cosmic voids": "galaxies",
    "photos": "history", "missions": "history", "scientists": "history",
    "spacecraft": "history", "spaceflight events": "history", "astronauts": "history",
    "cosmic events": "phenomena", "events": "phenomena", "phenomena": "phenomena",
    "forces": "phenomena",
}


def _category_group(category: str) -> str:
    """Map a measured dataset category onto one of TOPIC_GROUPS' coarse buckets."""
    c = (category or "").strip().lower()
    if c in CATEGORY_TO_GROUP:
        return CATEGORY_TO_GROUP[c]
    for key, group in CATEGORY_TO_GROUP.items():   # substring fallback
        if key in c or c in key:
            return group
    for group in TOPIC_GROUPS:                      # group name literally present
        if group in c:
            return group
    return "mysteries"

SYSTEM = """You write viral "Top 5" space-ranking YouTube Shorts. Return ONE JSON \
object (and nothing else) with this exact shape:

{
  "title": "Top 5 Most DANGEROUS Planets In The Universe",
  "yt_titles": [
    "These 5 Planets Would Kill You Instantly",
    "Why Does Planet #1 Even Exist?? 😳",
    "Ranking the Deadliest Planets in the Universe",
    "I Didn't Know Space Was This Terrifying",
    "5 Planets NASA Wishes You Knew About"
  ],
  "hook": "Number one shouldn't even exist.",
  "category": "planets",
  "items": [
    {"rank": 5, "name": "...", "stat": "...", "query": "..."},
    ... exactly 5 items, ranks 5 down to 1 (1 = the most extreme / best) ...
  ]
}

LENGTH MATTERS. The whole script is read aloud, then the edit is sped up a LITTLE \
(never more than 1.35x, pitch preserved) to land the Short at ~30-33 seconds. So \
write TIGHT but not bare: include the one vivid fact/number for each item, cut \
filler and wind-up, and NEVER repeat the same detail in two places (the hook and \
#1 must not restate the same fact). Aim for one clean, information-dense line per \
item — enough to be interesting, short enough that a gentle speed-up keeps it \
comfortably under ~33 seconds.

Rules:
- title: the ON-SCREEN title. SHORT, dramatic, superlative, ONE word in ALL CAPS \
(DANGEROUS, LARGEST, STRANGEST). Format "Top 5 ... ".
- yt_titles: EXACTLY 5 YouTube titles for THIS video, each WILDLY different from \
the others and from the on-screen title. This is the single most important field \
for the channel — repetitive titles get a channel suppressed. Vary EVERYTHING: \
some a question, some a bold claim, some a curiosity gap, some first-person, some \
number-led; vary length; most with NO emoji (at most one has an emoji). NEVER just \
reword "Top 5 X" five times. Make each one a title a different creator might write.
- hook: ONE short line (AT MOST 12 words) teasing the ACTUAL #1 (curiosity gap), \
spoken in the intro. It MUST be factually accurate and specifically about YOUR #1 \
— a real, verifiable detail of THAT object. NEVER borrow a fact from a different \
object or invent one (e.g. do not say "rains molten glass" unless #1 truly does). \
Don't name #1 — tease it. Do NOT reuse #1's exact stat here; tease a DIFFERENT \
angle so the payoff isn't spoiled. Write the hook AFTER you've chosen #1.
- stat: ONE punchy line, ABOUT 8-11 words (never more than ~12). Lead with the \
number or the vivid detail; cut articles and connective filler ("that", "which", \
"making it", "into a region"). Keep it factually ACCURATE (the real number/detail). \
Examples of the right length: "462°C — hot enough to melt lead in seconds", \
"4.3 million Suns crammed inside Mercury's tiny orbit", \
"Rains molten glass, blown sideways at 5,400 mph".
- query: a NASA image-library search term that reliably returns a clear image of \
THAT object. Use mission-specific terms:
    * Solar-system planets/moons: "Jupiter Cassini", "Venus Mariner 10", \
"Saturn Cassini", "Neptune Voyager", "Europa Galileo", "Mars Viking".
    * Exoplanets: "<name> exoplanet" or "hot jupiter exoplanet artist concept".
    * Galaxies/nebulae/black holes: "Hubble <name>", "<name> nebula", \
"black hole accretion disk".
  Avoid generic terms that return rockets or crews.
- Everything must be real. Do not invent objects or fake facts.

Output ONLY the JSON object — no prose, no code fences."""


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:48]


def generate_ranking(topic: str | None = None) -> dict | None:
    """Ask Claude for one ranking list. Returns a validated dict, or None."""
    load_dotenv()
    if not os.getenv("ANTHROPIC_API_KEY"):
        log.warning("[rank-gen] ANTHROPIC_API_KEY not set.")
        return None
    try:
        import anthropic
    except ImportError:
        log.warning("[rank-gen] 'anthropic' not installed.")
        return None

    user = (f"Topic: {topic}." if topic else
            "Invent a fresh, high-curiosity space Top-5 topic.") + \
        " Write the JSON now."
    try:
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=MODEL, max_tokens=1500, system=SYSTEM,
            output_config={"effort": "medium"},
            messages=[{"role": "user", "content": user}])
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
    except Exception as e:
        log.error("[rank-gen] generation failed: %s", e)
        return None

    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        log.warning("[rank-gen] no JSON in response.")
        return None
    try:
        data = json.loads(m.group(0))
    except Exception as e:
        log.warning("[rank-gen] bad JSON: %s", e)
        return None

    # Validate.
    items = data.get("items")
    if not data.get("title") or not isinstance(items, list) or len(items) != 5:
        log.warning("[rank-gen] invalid shape for %r.", data.get("title"))
        return None
    for it in items:
        if not all(k in it for k in ("rank", "name", "stat", "query")):
            log.warning("[rank-gen] item missing fields.")
            return None
    # Normalize YouTube title variants: keep only distinct non-empty strings.
    variants = [t.strip() for t in data.get("yt_titles", [])
                if isinstance(t, str) and t.strip()]
    seen, deduped = set(), []
    for t in variants:
        if t.lower() not in seen:
            seen.add(t.lower())
            deduped.append(t)
    data["yt_titles"] = deduped
    if len(deduped) < 3:
        log.warning("[rank-gen] only %d title variant(s) for %r (want 5).",
                    len(deduped), data.get("title"))
    return data


def save(data: dict) -> Path | None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"{_slug(data['title'])}.json"
    if path.exists():
        return None                         # already have this one
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def _group_weights(perf: dict | None) -> dict[str, float]:
    """Per-group selection weight from measured per-category performance.

    Mirrors main._apply_ranking_performance_weight: each group's score is the
    mean composite score of the categories that map to it, shrunk toward the
    global mean by sample size so a single lucky video can't dominate. Every
    group keeps a floor weight (exploration) so quiet/unproven groups still get
    generated rather than starved. Uniform weights when there's no data yet.
    """
    groups = list(TOPIC_GROUPS)
    if not perf:
        return {g: 1.0 for g in groups}

    # Collect (score, n) per group from the measured categories.
    agg: dict[str, list[tuple[float, int]]] = {g: [] for g in groups}
    for cat, d in perf.items():
        agg[_category_group(cat)].append((d.get("score", 0.0), d.get("n", 1)))

    all_scores = [s for d in perf.values() for s in [d.get("score", 0.0)]]
    global_mean = (sum(all_scores) / len(all_scores)) if all_scores else 0.0
    prior = 1.5                              # pseudo-count for shrinkage

    weights: dict[str, float] = {}
    for g in groups:
        rows = agg[g]
        n = sum(r[1] for r in rows)
        raw = sum(r[0] * r[1] for r in rows) / n if n else global_mean
        # Shrink toward the global mean by sample size.
        adj = (n * raw + prior * global_mean) / (n + prior)
        weights[g] = adj
    # Normalize to positives, then blend with an exploration floor so no group
    # ever hits zero probability.
    lo = min(weights.values())
    span = (max(weights.values()) - lo) or 1.0
    return {g: 0.35 + (w - lo) / span for g, w in weights.items()}   # floor 0.35


def pick_topics(n: int, perf: dict | None = None,
                exclude: set[str] | None = None,
                seed: str | None = None) -> list[str]:
    """Choose ``n`` distinct topics, biased toward high-performing groups.

    Groups are sampled without replacement in proportion to `_group_weights`;
    within a chosen group a random unused topic is taken. `exclude` holds topic
    strings already built/queued so we don't re-roll them.
    """
    rng = random.Random(seed)
    exclude = set(exclude or ())
    weights = _group_weights(perf)
    # Available (group, topic) pairs, minus anything excluded.
    avail: dict[str, list[str]] = {
        g: [t for t in ts if t not in exclude] for g, ts in TOPIC_GROUPS.items()
    }
    picked: list[str] = []
    while len(picked) < n and any(avail.values()):
        live = [g for g, ts in avail.items() if ts]
        w = [weights.get(g, 0.35) for g in live]
        g = rng.choices(live, weights=w, k=1)[0]
        t = rng.choice(avail[g])
        avail[g].remove(t)
        picked.append(t)
    return picked


def generate_batch(n: int, perf: dict | None = None,
                   seed: str | None = None) -> list[Path]:
    """Generate up to ``n`` NEW datasets, performance-weighted, skipping dups.

    Returns the paths actually written. Re-rolls fresh topics if a generated
    title collides with one already on disk, so ``n`` reflects new files, not
    attempts. Never raises on a single-topic failure — it logs and continues.
    """
    existing_slugs = {p.stem for p in DATA_DIR.glob("*.json")}
    saved: list[Path] = []
    # Seed the exclusion set from the coarse topic that produced each existing
    # slug where we can tell, so we prefer genuinely new subjects.
    tried: set[str] = set()
    while len(saved) < n:
        need = n - len(saved)
        topics = pick_topics(need + 3, perf=perf, exclude=tried, seed=seed)
        if not topics:
            log.warning("[rank-gen] topic pool exhausted after %d new dataset(s).",
                        len(saved))
            break
        progressed = False
        for t in topics:
            if len(saved) >= n:
                break
            tried.add(t)
            d = generate_ranking(t)
            if not d:
                continue
            if _slug(d["title"]) in existing_slugs:
                log.info("[rank-gen] duplicate title skipped: %s", d["title"])
                continue
            p = save(d)
            if p:
                existing_slugs.add(p.stem)
                saved.append(p)
                progressed = True
                log.info("[rank-gen] saved %s — %s", p.name, d["title"])
        if not progressed and len(tried) >= len(TOPICS):
            break
    return saved


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    if arg in (None, "auto"):
        # Performance-weighted batch (uses the channel's own engagement data
        # when the DB is reachable; falls back to uniform when it isn't).
        perf = None
        try:
            from database import db
            db.init_db()
            perf = db.subreddit_performance()
        except Exception as e:
            log.info("[rank-gen] no performance data (%s); generating uniformly.", e)
        paths = generate_batch(n, perf=perf)
        print(f"generated {len(paths)} new dataset(s).")
    else:
        for _ in range(n):
            d = generate_ranking(arg)
            if not d:
                continue
            p = save(d)
            print(f"  {'saved ' + p.name if p else '(duplicate)':40} — {d['title']}")
            for it in sorted(d["items"], key=lambda x: -x["rank"]):
                print(f"       #{it['rank']} {it['name']:20} | {it['query']}")
