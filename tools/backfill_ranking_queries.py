"""
Backfill image search terms on existing ranking datasets.

Datasets written before 2026-07-28 carry ONE search term per item, and for
unphotographable subjects that term described the absence ("galaxy void", "CMB
cold spot") — which returns nothing usable, so those items shipped as generic
nebula backdrops. This asks Claude for the 3-term `queries` list the current
generator produces (see ranking_generate.SYSTEM) and writes it into each dataset
in place. Items that already have `queries` are left alone.

    python3 -m tools.backfill_ranking_queries            # every dataset
    python3 -m tools.backfill_ranking_queries --dry-run
    python3 -m tools.backfill_ranking_queries top_5_emptiest_places_in_the_universe.json
"""

import json
import os
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

SYSTEM = """You choose image-search terms for a cinematic "Top 5" space video. \
For each ranked item you get its name and the on-screen fact.

Return ONE JSON object: {"items": [{"name": "<exact name given>", \
"queries": ["...", "...", "..."]}, ...]} — one entry per item, same order.

Each `queries` list is EXACTLY 3 image-search terms, BEST FIRST. They're searched \
against the NASA image library, then Wikimedia/Wikipedia, and a vision model picks \
the best clean result. The job of the list is to GUARANTEE at least one term \
returns a spectacular, real, watermark-free image — an item where all three fail \
falls back to a generic nebula and the video looks cheap.
- Term 1: the specific object with its imaging context ("Jupiter Cassini", \
"Hubble NGC 1300", "Europa Galileo", "<name> nebula Webb").
- Term 2: a different wording or instrument for the same object.
- Term 3: a guaranteed-photogenic fallback that still fits the fact on screen.
- Never repeat a term; never give three near-identical ones.

CRITICAL — if the item cannot be photographed (a void, empty space, a force, a \
speed, a distance, a concept), do NOT write terms describing the absence \
("galaxy void", "cold spot", "empty region"): they return nothing. Name REAL, \
heavily-imaged objects or scenes that legitimately illustrate it — a named galaxy \
or cluster in that region, a Hubble/Webb deep field, a large-scale-structure \
visualization, a relevant nebula.
Avoid anything that returns rockets, crews, logos, charts or maps.

Output ONLY the JSON object."""


def new_queries(data: dict) -> dict[str, list[str]] | None:
    """{item name -> 3 search terms} from Claude, or None on failure."""
    load_dotenv()
    if not os.getenv("ANTHROPIC_API_KEY"):
        log.warning("[backfill] ANTHROPIC_API_KEY not set.")
        return None
    import anthropic

    listing = "\n".join(f"- {it['name']}: {it.get('stat', '')}"
                        for it in sorted(data["items"], key=lambda x: -x["rank"]))
    user = f"Video: {data['title']}\nItems:\n{listing}\n\nWrite the JSON now."
    try:
        resp = anthropic.Anthropic().messages.create(
            model=MODEL, max_tokens=1200, system=SYSTEM,
            output_config={"effort": "medium"},
            messages=[{"role": "user", "content": user}])
        txt = "".join(b.text for b in resp.content if b.type == "text")
        out = json.loads(re.search(r"\{.*\}", txt, re.S).group(0))
    except Exception as e:
        log.error("[backfill] %s: %s", data["title"], e)
        return None
    return {i["name"]: [q for q in i.get("queries", []) if isinstance(q, str) and q.strip()]
            for i in out.get("items", []) if i.get("name")}


def backfill(path: Path, dry_run: bool = False) -> bool:
    data = json.loads(path.read_text(encoding="utf-8"))
    if all(it.get("queries") for it in data.get("items", [])):
        log.info("[backfill] %s already has queries; skipping.", path.name)
        return False
    mapping = new_queries(data)
    if not mapping:
        return False
    changed = False
    for it in data["items"]:
        qs = mapping.get(it["name"]) or []
        if len(qs) < 2:                      # a single term is what we're fixing
            log.warning("[backfill] %s / %s: got %d term(s); keeping the old one.",
                        path.name, it["name"], len(qs))
            qs = [q for q in ([it.get("query")] + qs) if q]
        it["queries"] = list(dict.fromkeys(qs))
        it["query"] = it["queries"][0]       # kept in sync for older readers
        changed = True
        print(f"   #{it['rank']} {it['name'][:26]:28} {it['queries']}")
    if changed and not dry_run:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                        encoding="utf-8")
    return changed


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    dry = "--dry-run" in sys.argv
    paths = [DATA_DIR / a for a in args] if args else sorted(DATA_DIR.glob("*.json"))
    done = 0
    for p in paths:
        print(f"\n{p.name}")
        if backfill(p, dry):
            done += 1
    print(f"\n{'would update' if dry else 'updated'} {done} dataset(s).")
