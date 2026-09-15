"""
Ranked-clip dataset generator — Claude writes new "Ranking ___ Moments" lists.

The clipranking format (2026-09-15 pivot) is the silent, stock-footage cousin of
the space ranking channel: 5 real video clips, each slotted into an on-screen
rank, no narration. So a dataset here carries no `hook` and no `yt_titles` — the
title is the genre template and the only spoken word is none.

    python3 -m sourcing.clip_ranking_generate                  # 1 auto topic
    python3 -m sourcing.clip_ranking_generate "shark moments"
    python3 -m sourcing.clip_ranking_generate auto 5           # 5 auto topics
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
DATA_DIR = PROJECT_ROOT / "sourcing" / "clip_ranking_data"
MODEL = "claude-opus-4-8"

# Subjects are chosen for STOCK FOOTAGE STRENGTH, not for comedy.
#
# The reference channels (Xiro 323M, Mugi 158M, Polar 116M) run on scraped
# user-generated "peak moments" — a chicken screaming, a trampoline fail. No
# stock library contains those, and we are not scraping (see the plan: that is
# both a copyright and a reused-content exposure). So we play to where licensed
# footage is genuinely spectacular: wildlife, water, slow motion, natural
# phenomena, extreme sport, satisfying process shots.
#
# Groups map to the per-`category` engagement the channel measures, so
# generation leans toward whatever actually earns shares. Add rows freely.
TOPIC_GROUPS: dict[str, list[str]] = {
    "ocean": [
        "shark moments", "whale moments", "dolphin moments", "jellyfish moments",
        "octopus moments", "coral reef moments", "sea turtle moments",
        "underwater predator moments", "deep sea creature moments",
        "crashing wave moments", "surfing wipeout moments",
    ],
    "wildlife": [
        "big cat moments", "wolf moments", "bear moments", "elephant moments",
        "monkey moments", "fox moments", "deer moments", "snake moments",
        "lizard moments", "insect close up moments", "spider moments",
        "predator hunting moments", "baby animal moments", "herd stampede moments",
    ],
    "birds": [
        "eagle moments", "owl moments", "hummingbird moments", "penguin moments",
        "flamingo moments", "parrot moments", "bird of prey diving moments",
        "flock murmuration moments",
    ],
    "pets": [
        "dog moments", "cat moments", "puppy moments", "kitten moments",
        "dogs swimming moments", "cats jumping moments", "rabbit moments",
        "horse moments", "farm animal moments",
    ],
    "nature": [
        "volcano moments", "lightning strike moments", "tornado moments",
        "avalanche moments", "waterfall moments", "aurora moments",
        "desert storm moments", "glacier calving moments", "wildfire moments",
        "sunset timelapse moments",
    ],
    "extreme": [
        "skydiving moments", "surfing moments", "snowboarding moments",
        "mountain biking moments", "rock climbing moments", "parkour moments",
        "motocross moments", "wingsuit moments", "skateboarding moments",
        "cliff diving moments",
    ],
    "satisfying": [
        "slow motion water moments", "paint mixing moments", "glass blowing moments",
        "welding sparks moments", "domino moments", "ink in water moments",
        "macro food moments", "fire in slow motion moments",
        "bubble popping moments", "sand cutting moments",
    ],
}

TOPICS = [t for group in TOPIC_GROUPS.values() for t in group]

# Measured dataset `category` (Claude's granular tag) -> coarse group above.
CATEGORY_TO_GROUP: dict[str, str] = {
    "ocean": "ocean", "sea": "ocean", "underwater": "ocean", "marine": "ocean",
    "sharks": "ocean", "whales": "ocean", "waves": "ocean", "surfing": "ocean",
    "wildlife": "wildlife", "animals": "wildlife", "predators": "wildlife",
    "big cats": "wildlife", "reptiles": "wildlife", "insects": "wildlife",
    "birds": "birds", "raptors": "birds",
    "pets": "pets", "dogs": "pets", "cats": "pets", "farm": "pets",
    "nature": "nature", "weather": "nature", "geology": "nature",
    "storms": "nature", "landscapes": "nature",
    "extreme": "extreme", "sports": "extreme", "extreme sports": "extreme",
    "stunts": "extreme",
    "satisfying": "satisfying", "macro": "satisfying", "slow motion": "satisfying",
    "process": "satisfying",
}


def _category_group(category: str) -> str:
    """Map a measured dataset category onto one of TOPIC_GROUPS' coarse buckets."""
    c = (category or "").strip().lower()
    if c in CATEGORY_TO_GROUP:
        return CATEGORY_TO_GROUP[c]
    for key, group in CATEGORY_TO_GROUP.items():      # substring fallback
        if key in c or c in key:
            return group
    for group in TOPIC_GROUPS:                        # group name literally present
        if group in c:
            return group
    return "wildlife"


SYSTEM = """You write faceless "ranked clip" YouTube Shorts for a channel that \
ranks five real STOCK VIDEO CLIPS against each other. There is NO narrator and \
NO voiceover — every word you write is read silently on screen, so it must be \
short enough to take in at a glance.

Return ONE JSON object (and nothing else) with this exact shape:

{
  "title": "Ranking Best Shark Moments",
  "category": "sharks",
  "items": [
    {"rank": 5, "name": "Reef Shark", "label": "cruising the shallows",
     "queries": ["...", "...", "..."]},
    ... exactly 5 items, ranks 5 down to 1 (1 = the most spectacular) ...
  ]
}

Rules:
- title: MUST follow the genre template exactly — \
"Ranking {Best|Funniest|Craziest|Most Satisfying|Wildest} <TOPIC> {Moments|Fails}". \
No colons, no numbers, no emoji, no extra clauses. This is a house format, not a \
place to be creative: "Ranking Best Shark Moments", "Ranking Craziest Volcano \
Moments", "Ranking Most Satisfying Slow Motion Water Moments".
- category: ONE short lowercase tag for the subject ("sharks", "volcanoes", \
"big cats"). The channel measures engagement per category.
- name: the thing being ranked, 1-3 words, TITLE CASE. It goes on screen in the \
rank slot, so it must be instantly readable ("Great White", "Lava Fountain", \
"Snow Leopard").
- label: a SHORT on-screen caption for that clip — AT MOST 5 WORDS, a fragment, \
no full sentence, no final period. It is read in about a second while the clip \
plays. Good: "breaching clean out of water", "40 mph in open ocean". Bad: \
"The great white shark is capable of breaching entirely out of the water."
- RANK ORDER MATTERS. #1 must be the single most visually spectacular of the \
five — it is the payoff the viewer stays for, and it is always revealed last. \
Rank on how jaw-dropping the FOOTAGE is, not on facts or size.
- queries: EXACTLY 3 STOCK VIDEO search terms for this item, BEST FIRST. These \
are searched on Pexels/Pixabay, which are libraries of professional stock \
footage — NOT a viral clip archive. So write terms that a stock library \
actually contains.
    * Term 1 = the subject plus the shot you want: "great white shark \
breaching slow motion", "lava fountain erupting at night".
    * Term 2 = a DIFFERENT wording or angle for the same subject: "shark \
swimming underwater close up".
    * Term 3 = a BROADER, guaranteed-to-return fallback that still fits the \
label: "shark underwater".
    * Prefer terms that suggest motion ("running", "erupting", "diving", \
"slow motion", "aerial") — a static clip looks like a photo and kills the format.
    * NEVER write a term for a specific viral incident, a named person, a meme, \
a branded event, or anything user-generated. Stock libraries have none of it.
    * Never repeat a term, and never give three near-identical ones.
- All five items must be VISUALLY DISTINCT from each other — five near-identical \
shots make the ranking meaningless.
- Everything must be real. Do not invent species or fake claims.

Output ONLY the JSON object — no prose, no code fences."""

# The template the title must match — enforced after generation, because the
# genre format IS the product here and a drifting title breaks the channel's
# recognisability.
_TITLE_RE = re.compile(
    r"^Ranking (Best|Funniest|Craziest|Most Satisfying|Wildest) .+ (Moments|Fails)$")


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:48]


def generate_clip_ranking(topic: str | None = None,
                          avoid: list[str] | None = None) -> dict | None:
    """Ask Claude for one ranked-clip list. Returns a validated dict, or None.

    `avoid` = titles the channel already holds; shown to the model so it doesn't
    re-cover a subject under new wording (is_near_duplicate is the backstop).
    """
    load_dotenv()
    if not os.getenv("ANTHROPIC_API_KEY"):
        log.warning("[clip-gen] ANTHROPIC_API_KEY not set.")
        return None
    try:
        import anthropic
    except ImportError:
        log.warning("[clip-gen] 'anthropic' not installed.")
        return None

    user = (f"Topic: {topic}." if topic else
            "Invent a fresh, visually spectacular ranked-clip topic.")
    if avoid:
        user += ("\n\nThe channel has ALREADY published these lists — your list "
                 "must cover a genuinely different subject, not a re-worded one, "
                 "and must not repeat their headline subjects:\n"
                 + "\n".join(f"- {t}" for t in avoid[-40:]))
    user += " Write the JSON now."
    try:
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=MODEL, max_tokens=1500, system=SYSTEM,
            output_config={"effort": "medium"},
            messages=[{"role": "user", "content": user}])
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
    except Exception as e:
        log.error("[clip-gen] generation failed: %s", e)
        return None

    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        log.warning("[clip-gen] no JSON in response.")
        return None
    try:
        data = json.loads(m.group(0))
    except Exception as e:
        log.warning("[clip-gen] bad JSON: %s", e)
        return None

    return _validate(data)


def _validate(data: dict) -> dict | None:
    """Enforce the dataset contract. Returns the normalized dict, or None."""
    title = (data.get("title") or "").strip()
    items = data.get("items")
    if not title or not isinstance(items, list) or len(items) != 5:
        log.warning("[clip-gen] invalid shape for %r.", title)
        return None
    if not _TITLE_RE.match(title):
        # The house format is the point — a drifting title is a reject, not a
        # warning, so the channel stays recognisable upload to upload.
        log.warning("[clip-gen] title off-template, skipped: %r", title)
        return None
    data["title"] = title
    data["category"] = (data.get("category") or "").strip().lower() or "wildlife"

    ranks = set()
    for it in items:
        if not all(k in it for k in ("rank", "name")):
            log.warning("[clip-gen] item missing fields in %r.", title)
            return None
        try:
            it["rank"] = int(it["rank"])
        except Exception:
            log.warning("[clip-gen] non-numeric rank in %r.", title)
            return None
        ranks.add(it["rank"])
        it["name"] = str(it["name"]).strip()

        # No narrator: the label is read silently in about a second, so a long
        # one is worse than none. Trim rather than reject.
        label = str(it.get("label") or it.get("stat") or "").strip().rstrip(".")
        words = label.split()
        if len(words) > 5:
            label = " ".join(words[:5])
        it["label"] = label
        it.pop("stat", None)

        # Normalize search terms: keep `queries` (the pool the fetcher searches)
        # and `query` (first term) in sync, matching the ranking dataset contract
        # so shared code reading either field behaves the same.
        qs = it.get("queries")
        if isinstance(qs, str):
            qs = [qs]
        qs = [q.strip() for q in (qs or []) if isinstance(q, str) and q.strip()]
        if not qs and it.get("query"):
            qs = [str(it["query"]).strip()]
        if not qs:
            log.warning("[clip-gen] item %r has no clip query.", it.get("name"))
            return None
        it["queries"] = list(dict.fromkeys(qs))
        it["query"] = it["queries"][0]

    if ranks != {1, 2, 3, 4, 5}:
        log.warning("[clip-gen] ranks are not 1-5 in %r: %s", title, sorted(ranks))
        return None
    return data


def save(data: dict) -> Path | None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"{_slug(data['title'])}.json"
    if path.exists():
        return None                          # already have this one
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def _group_weights(perf: dict | None) -> dict[str, float]:
    """Per-group selection weight from measured per-category performance.

    Same shape as the space channel's version: each group's score is the mean
    composite score of the categories mapping to it, shrunk toward the global
    mean by sample size so one lucky video can't dominate, then blended with an
    exploration floor so a quiet group is never starved. Uniform with no data.
    """
    groups = list(TOPIC_GROUPS)
    if not perf:
        return {g: 1.0 for g in groups}

    agg: dict[str, list[tuple[float, int]]] = {g: [] for g in groups}
    for cat, d in perf.items():
        agg[_category_group(cat)].append((d.get("score", 0.0), d.get("n", 1)))

    all_scores = [d.get("score", 0.0) for d in perf.values()]
    global_mean = (sum(all_scores) / len(all_scores)) if all_scores else 0.0
    prior = 1.5                               # pseudo-count for shrinkage

    weights: dict[str, float] = {}
    for g in groups:
        rows = agg[g]
        n = sum(r[1] for r in rows)
        raw = sum(r[0] * r[1] for r in rows) / n if n else global_mean
        weights[g] = (n * raw + prior * global_mean) / (n + prior)

    lo = min(weights.values())
    span = (max(weights.values()) - lo) or 1.0
    return {g: 0.35 + (w - lo) / span for g, w in weights.items()}    # floor 0.35


def pick_topics(n: int, perf: dict | None = None,
                exclude: set[str] | None = None,
                seed: str | None = None) -> list[str]:
    """Choose ``n`` distinct topics, biased toward high-performing groups."""
    rng = random.Random(seed)
    exclude = set(exclude or ())
    weights = _group_weights(perf)
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

    Returns the paths actually written. Never raises on a single-topic failure —
    it logs and continues, because this runs inside the daily production job.
    """
    from sourcing.clip_ranking_source import is_near_duplicate

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    existing_slugs = {p.stem for p in DATA_DIR.glob("*.json")}
    existing: list[dict] = []
    for p in DATA_DIR.glob("*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if d.get("title") and d.get("items"):
            existing.append(d)

    saved: list[Path] = []
    tried: set[str] = set()
    while len(saved) < n:
        need = n - len(saved)
        topics = pick_topics(need + 3, perf=perf, exclude=tried, seed=seed)
        if not topics:
            log.warning("[clip-gen] topic pool exhausted after %d new dataset(s).",
                        len(saved))
            break
        progressed = False
        for t in topics:
            if len(saved) >= n:
                break
            tried.add(t)
            d = generate_clip_ranking(t, avoid=[e["title"] for e in existing])
            if not d:
                continue
            if _slug(d["title"]) in existing_slugs:
                log.info("[clip-gen] duplicate title skipped: %s", d["title"])
                continue
            why = is_near_duplicate(d, existing)
            if why:
                log.info("[clip-gen] near-duplicate skipped (%s): %s", why, d["title"])
                continue
            p = save(d)
            if p:
                existing_slugs.add(p.stem)
                existing.append(d)
                saved.append(p)
                progressed = True
                log.info("[clip-gen] saved %s — %s", p.name, d["title"])
        if not progressed and len(tried) >= len(TOPICS):
            break
    return saved


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    if arg in (None, "auto"):
        perf = None
        try:
            from database import db
            db.init_db()
            perf = db.subreddit_performance()
        except Exception as e:
            log.info("[clip-gen] no performance data (%s); generating uniformly.", e)
        paths = generate_batch(n, perf=perf)
        print(f"generated {len(paths)} new dataset(s).")
    else:
        for _ in range(n):
            d = generate_clip_ranking(arg)
            if not d:
                continue
            p = save(d)
            print(f"  {'saved ' + p.name if p else '(duplicate)':40} — {d['title']}")
            for it in sorted(d["items"], key=lambda x: -x["rank"]):
                print(f"       #{it['rank']} {it['name']:18} | {it['label']:28} | {it['query']}")
