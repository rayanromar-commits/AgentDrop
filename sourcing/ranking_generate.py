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

TOPICS = [
    # Planets & solar system
    "most dangerous planets", "hottest planets", "strangest exoplanets ever found",
    "most extreme places in the solar system", "planets where it rains something bizarre",
    "biggest planets ever discovered", "most Earth-like exoplanets",
    "windiest planets", "planets with the wildest storms",
    # Moons
    "strangest moons", "moons that could hold alien life", "most volcanic moons",
    # Stars
    "biggest stars in the universe", "most powerful explosions in space",
    "strangest types of stars", "closest stars that could go supernova",
    # Galaxies & deep space
    "largest galaxies", "most beautiful nebulae", "biggest black holes",
    "most mysterious objects in space", "strangest signals from space",
    "coldest places in the universe", "brightest objects in the universe",
    "biggest structures in the universe",
    # Phenomena & events
    "deadliest cosmic events", "things that could end all life on Earth from space",
    "most terrifying facts about black holes", "fastest things in the universe",
    # Space history & people (NASA / public-domain imagery)
    "most important space missions in history",
    "people who changed how we see the universe",
    "greatest astronomers of all time", "most famous astronauts in history",
    "most iconic photos NASA ever took",
]

SYSTEM = """You write viral "Top 5" space-ranking YouTube Shorts. Return ONE JSON \
object (and nothing else) with this exact shape:

{
  "title": "Top 5 Most DANGEROUS Planets In The Universe",
  "hook": "Number one shouldn't even exist.",
  "category": "planets",
  "items": [
    {"rank": 5, "name": "...", "stat": "...", "query": "..."},
    ... exactly 5 items, ranks 5 down to 1 (1 = the most extreme / best) ...
  ]
}

Rules:
- title: SHORT, dramatic, superlative, ONE word in ALL CAPS (DANGEROUS, LARGEST, \
STRANGEST). Format "Top 5 ... ".
- hook: one punchy line teasing #1 (curiosity gap), spoken in the intro.
- stat: ONE vivid, FACTUALLY ACCURATE line for that object (a real number/detail).
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
    return data


def save(data: dict) -> Path | None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"{_slug(data['title'])}.json"
    if path.exists():
        return None                         # already have this one
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    topic = None if (arg in (None, "auto")) else arg
    for _ in range(n):
        t = topic or random.choice(TOPICS)
        d = generate_ranking(t)
        if not d:
            continue
        p = save(d)
        print(f"  {'saved ' + p.name if p else '(duplicate)':40} — {d['title']}")
        for it in sorted(d["items"], key=lambda x: -x["rank"]):
            print(f"       #{it['rank']} {it['name']:20} | {it['query']}")
