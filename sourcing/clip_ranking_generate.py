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
from itertools import zip_longest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from agentdrop_common import first_json, setup_logging

log = setup_logging()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "sourcing" / "clip_ranking_data"
MODEL = "claude-opus-4-8"

# THE NICHE: dangerous and extreme wildlife, led by the ocean.
#
# Chosen from measured evidence, not taste. The space channel failed because
# nobody had built an audience in its lane at any size — that looked like an
# open goal and actually meant the audience wasn't there, and 70 videos capped
# at ~1,000 views each proved it. So the test for a lane is whether big numbers
# already happen in it:
#   * Ocean Pulse: 23,200 subscribers and a 54,000,000-view video. A SMALL
#     channel broke out — the algorithm doesn't gate this lane on channel size.
#   * "Why Deep Sea Creatures Are Giant" did 37M, then a near-copy did 9.8M.
#     The TOPIC carries it, not the creator, which is what makes it enterable.
#   * "Biggest Animal Ever Known" 24M, "Speed Kings of the Animal Kingdom" 8.3M,
#     "Top 3 Most Dangerous Snakes in America" 6.5M.
#   * 1 Minute Animals sustains 2.88M subscribers across 1,400 videos, so the
#     lane has the depth to feed a daily upload for years. Space ran dry at 24.
#
# It is also the lane licensed stock actually serves: sharks, jellyfish, snakes,
# predators and deep-sea footage are abundant, whereas the "funny animal moments"
# lane that dominates this format runs on scraped user clips we won't touch.
#
# And the framing carries the argument axis we're missing — people argue about
# what's deadliest. Beautiful scenery, which stock also does well, measured at
# 1K-36K views: nobody shares a nice sunset. Danger travels; pretty doesn't.
TOPIC_GROUPS: dict[str, list[str]] = {
    "deep_sea": [
        "deep sea creatures", "creatures of the mariana trench",
        "bioluminescent sea creatures", "anglerfish and deep sea hunters",
        "giant squid and colossal squid", "deep sea gigantism",
        "creatures that live without sunlight", "hydrothermal vent creatures",
        "deep sea creatures that look alien", "the deepest living fish",
        "monsters of the midnight zone", "transparent sea creatures",
    ],
    "sharks": [
        "most dangerous sharks", "biggest sharks ever", "fastest sharks",
        "strangest sharks", "prehistoric sharks", "sharks with the strongest bite",
        "deep sea sharks", "sharks that hunt in packs",
        "sharks people never see coming", "rarest sharks alive",
    ],
    "ocean_predators": [
        "deadliest ocean predators", "orcas and what they hunt",
        "most venomous sea creatures", "box jellyfish and deadly jellyfish",
        "sea snakes and ocean venom", "predators that hunt sharks",
        "most aggressive fish", "electric and shocking sea creatures",
        "ocean ambush predators", "creatures that can kill a diver",
    ],
    "venomous": [
        "most venomous snakes", "deadliest spiders", "most venomous insects",
        "poison dart frogs and toxic amphibians", "deadliest scorpions",
        "venomous animals of australia", "snakes with the fastest strike",
        "animals whose venom has no antivenom", "most painful stings",
        "tiny animals that can kill you",
    ],
    "apex_predators": [
        "deadliest land predators", "big cats ranked by power",
        "strongest bite force in the animal kingdom", "deadliest pack hunters",
        "most dangerous animals in africa", "apex predators of the arctic",
        "bears ranked by danger", "crocodiles and alligators ranked",
        "predators that hunt humans", "deadliest animals in the amazon",
    ],
    "extremes": [
        "fastest animals on earth", "strongest animals for their size",
        "biggest animals that ever lived", "longest living animals",
        "animals that survive the impossible", "highest flying animals",
        "animals with the best senses", "deadliest animals by body count",
        "animals with the most extreme migrations", "loudest animals",
        "animals that can regenerate", "coldest and hottest surviving animals",
    ],
    "weird_rare": [
        "weirdest animals alive", "rarest animals on earth",
        "animals that shouldn't exist", "animals with bizarre defenses",
        "creatures discovered recently", "animals that look prehistoric",
        "animals with impossible abilities", "ugliest animals in the ocean",
        "animals that change their body", "nearly extinct creatures",
    ],
}

TOPICS = [t for group in TOPIC_GROUPS.values() for t in group]

# Measured dataset `category` (Claude's granular tag) -> coarse group above.
CATEGORY_TO_GROUP: dict[str, str] = {
    "deep sea": "deep_sea", "deep_sea": "deep_sea", "abyss": "deep_sea",
    "mariana trench": "deep_sea", "bioluminescence": "deep_sea",
    "anglerfish": "deep_sea", "squid": "deep_sea", "octopus": "deep_sea",
    "sharks": "sharks", "shark": "sharks", "megalodon": "sharks",
    "ocean predators": "ocean_predators", "orcas": "ocean_predators",
    "whales": "ocean_predators", "jellyfish": "ocean_predators",
    "sea snakes": "ocean_predators", "marine": "ocean_predators",
    "ocean": "ocean_predators", "sea": "ocean_predators", "fish": "ocean_predators",
    "venomous": "venomous", "venom": "venomous", "snakes": "venomous",
    "spiders": "venomous", "scorpions": "venomous", "insects": "venomous",
    "frogs": "venomous", "toxic": "venomous", "stings": "venomous",
    "apex predators": "apex_predators", "predators": "apex_predators",
    "big cats": "apex_predators", "bears": "apex_predators",
    "crocodiles": "apex_predators", "wolves": "apex_predators",
    "land predators": "apex_predators", "africa": "apex_predators",
    "extremes": "extremes", "speed": "extremes", "strength": "extremes",
    "size": "extremes", "senses": "extremes", "longevity": "extremes",
    "migration": "extremes", "records": "extremes",
    "weird": "weird_rare", "rare": "weird_rare", "strange": "weird_rare",
    "bizarre": "weird_rare", "prehistoric": "weird_rare",
    "endangered": "weird_rare", "discoveries": "weird_rare",
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
    return "apex_predators"


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
"Ranking {Deadliest|Most Dangerous|Most Venomous|Strongest|Biggest|Fastest|\
Weirdest|Rarest|Scariest} <TOPIC>". No colons, no numbers, no emoji, no extra \
clauses. This is a house format, not a place to be creative: "Ranking Deadliest \
Ocean Predators", "Ranking Most Venomous Snakes", "Ranking Weirdest Deep Sea \
Creatures".
  The superlative is doing real work, so do NOT soften it. Titles built on \
"Best ... Moments" or on beauty measure 1,000-36,000 views in this lane; titles \
built on danger, size and strangeness measure 200,000-54,000,000. People share \
what unsettles them, not what is pretty. Pick the superlative that is TRUE of \
your list and lean on it.
- category: ONE short lowercase tag for the subject ("sharks", "volcanoes", \
"big cats"). The channel measures engagement per category.
- name: the thing being ranked, 1-3 words, TITLE CASE. It goes on screen in the \
rank slot, so it must be instantly readable ("Great White", "Lava Fountain", \
"Snow Leopard").
- label: a SHORT on-screen caption for that clip — AT MOST 5 WORDS, a fragment, \
no full sentence, no final period. It is read in about a second while the clip \
plays. Good: "largest fish in the ocean", "40 mph in open water". Bad: \
"The great white shark is capable of breaching entirely out of the water."
- RANK ORDER MATTERS. #1 must be the single most visually spectacular of the \
five — it is the payoff the viewer stays for, and it is always revealed last. \
Rank on how jaw-dropping the FOOTAGE is, not on facts or size. But rank on \
footage that STOCK LIBRARIES ACTUALLY HAVE: a whale shark gliding past divers \
is a real stock clip, a great white breaching clean out of the water is not, so \
the second one must never be your #1.
- THE LABEL MUST STAY TRUE OF ORDINARY GOOD FOOTAGE of that subject. This is the \
most common way these videos fail: the label promises a rare peak moment, the \
clip shows the animal calmly swimming, and the two visibly contradict each \
other on screen. Describe what the subject IS or is typically doing, not a \
once-in-a-decade action. Good: "largest fish in the ocean", "hunts in packs \
after dark", "wingspan wider than a person". Bad: "breaching clean out of \
water", "catching prey mid-air", "attacking the camera" — nobody has that on a \
stock library.
- queries: EXACTLY 3 STOCK VIDEO search terms for this item, BEST FIRST. These \
are searched on Pexels/Pixabay, which are libraries of professional stock \
footage — NOT a viral clip archive. So write terms that a stock library \
actually contains.
    * EVERY term must contain the TOPIC's own words. A stock library returns the
wrong sense of a bare word without them — in a glass-blowing video the entry \
"Breath Inflate" searched alone returned a fizzy drink in a drinking glass. So \
write "glass blowing breath inflate", never "breath inflate".
    * Term 1 = the subject plus the shot you want: "great white shark \
breaching slow motion", "lava fountain erupting at night".
    * Term 2 = a DIFFERENT wording or angle for the same subject: "shark \
swimming underwater close up".
    * Term 3 = THE BARE SUBJECT NAME and nothing else ("whale shark", \
"volcano"). This is the safety net that guarantees the entry gets real footage \
of the right thing when the specific shots come up empty, so it must stay \
plain — no adjectives, no action, no camera direction.
    * Prefer terms that suggest motion ("running", "erupting", "diving", \
"slow motion", "aerial") — a static clip looks like a photo and kills the format.
    * NEVER write a term for a specific viral incident, a named person, a meme, \
a branded event, or anything user-generated. Stock libraries have none of it.
    * Never repeat a term, and never give three near-identical ones.
- All five items must be VISUALLY DISTINCT from each other — five near-identical \
shots make the ranking meaningless.
- Each item must be a CONCRETE, SEARCHABLE THING, not a stage of one process. \
Five species, five places, five objects: good. Five steps of a single procedure \
("Molten Gather", "Shaping Roll", "Breath Inflate"): bad — they are not separate \
subjects, stock has no clip of any one of them, and they look identical on \
screen. If a topic is a PROCESS, rank the different THINGS it produces or the \
different FORMS it takes instead.
- Everything must be real. Do not invent species or fake claims.
- EVERY ITEM MUST BE FILMABLE, AND ALREADY FILMED. The channel shows real stock \
footage of the thing named on screen; there is no illustration, no recreation \
and no substitute. Before you write an item, apply this test: has a camera \
actually pointed at this, and would a stock library plausibly hold that clip? \
If not, it cannot be on the list. Excluded outright:
    * EXTINCT and PREHISTORIC subjects — megalodon, titanoboa, any dinosaur, any \
species known only from fossils. This is the single worst failure this format \
has: asked for prehistoric sharks, the pipeline found whale-shark footage and \
put "Megalodon" over it. Nothing has ever filmed a megalodon. A whole TOPIC \
about the extinct past is therefore banned, not just the individual item.
    * MYTHICAL or speculative subjects — cryptids, monsters, aliens.
    * Anything too small or too remote to film — microscopic organisms, bacteria, \
distant exoplanets, the inside of the Earth.
    * ABSTRACT ideas with no single object to point at ("evolution", "extinction").
    * Anything that exists mainly as an artist's reconstruction, a diagram or a \
CGI render rather than as real recorded footage.
- EACH ITEM MUST BE TELLABLE APART ON SCREEN. A viewer sees a few silent seconds \
with a name over it, so an item that looks like a generic member of its family \
is a failure even when footage exists: a named individual animal, a subspecies \
that is visually identical to its cousin, or a fish that reads as "just a fish". \
Rank things a stranger could distinguish at a glance.

Output ONLY the JSON object — no prose, no code fences."""

# The template the title must match — enforced after generation, because the
# genre format IS the product here and a drifting title breaks the channel's
# recognisability.
_TITLE_RE = re.compile(
    r"^Ranking (Deadliest|Most Dangerous|Most Venomous|Strongest|Biggest|"
    r"Fastest|Weirdest|Rarest|Scariest) \S.*$")


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:48]


def generate_clip_ranking(topic: str | None = None,
                          avoid: list[str] | None = None,
                          trending: list[dict] | None = None) -> dict | None:
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
    if trending:
        # Show the model what is ACTUALLY pulling views in this lane right now.
        # It is evidence, not a menu: the searches drag in gaming clips, vlogs
        # and non-English uploads that merely share a keyword, so the model is
        # told to read through them for the underlying subject rather than copy
        # any single title.
        lines = "\n".join(f"- {t['views']:,} views: {t['title'][:80]}"
                           for t in trending[:20])
        user += ("\n\nFor reference, these Shorts are performing well in this "
                 "lane right now:\n" + lines +
                 "\n\nUse these to judge WHAT KIND of subject is landing — the "
                 "angles, the level of danger or strangeness, how specific the "
                 "hook is. Ignore any that are off-topic (gaming, vlogs, "
                 "personal channels, other languages); they came from a keyword "
                 "collision. Do NOT copy a title or remake a specific video.")
    # Everything the channel has learned — our own results per category and per
    # title framing, what viewers actually asked for in the comments, and which
    # videos beat their OWN channel's median on directly comparable channels.
    # Presented as evidence, not orders: told what to make, the model converges
    # on one subject within a week.
    try:
        from tracking.insights import briefing
        brief = briefing()
    except Exception:
        brief = ""
    if brief:
        user += ("\n\nWhat this channel has learned so far — weigh it, don't "
                 "obey it, and do not repeat a subject already covered:\n" + brief)

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
        data = first_json(m.group(0))
    except Exception as e:
        log.warning("[clip-gen] bad JSON: %s", e)
        return None

    return _validate(data)


# Words that give away a subject nothing has ever filmed. A cheap pre-filter in
# front of the model-based audit below: it costs nothing and catches the exact
# failure that shipped ("Ranking Deadliest Prehistoric Sharks" -> whale-shark
# footage captioned "Megalodon").
_UNFILMABLE_RE = re.compile(
    r"\b(prehistoric|extinct|dinosaur|fossil|ancient|mythical|mythological|"
    r"legendary|cryptid|monster|alien|microscopic|bacteria|virus|"
    r"megalodon|titanoboa|mammoth|sabertooth|saber-tooth|trilobite|"
    r"\w+saurus|\w+raptor|\w+odon|\w+ceratops)\b", re.I)

_AUDIT_PROMPT = """You are the last check before a ranked-clip video is built \
from this list. The pipeline will search a STOCK FOOTAGE library (Pexels, \
Pixabay) for each item and put that clip on screen under the item's name. There \
is no illustration and no recreation — whatever it finds is presented to viewers \
as the real thing.

For each item, answer one question: does real recorded video of THIS EXACT \
SUBJECT plausibly exist in a stock library?

Answer "no" when the subject is extinct or prehistoric, mythical, microscopic, \
an abstract idea, a specific named individual or event, or anything that exists \
only as an artist's reconstruction or CGI. Answer "no" as well when footage of \
the subject exists but would be indistinguishable on screen from a generic \
member of its family, because a viewer cannot be shown a random look-alike under \
a confident caption.

Be strict. A "yes" means we publish a clip under that name. Say no when unsure.

Return ONLY JSON: {{"unfilmable": [{{"name": "...", "why": "<8 words"}}]}} — an \
empty list if every item is fine.

The list is titled "{title}". The items are:
{items}"""


_SUBSTITUTE_PROMPT = """A ranked-clip video is being built from this list, but \
one entry cannot be used: no stock footage exists that actually shows it, so \
keeping it would mean captioning the wrong animal.

List title: "{title}"
Entries: {items}
The entry to replace: "{failed}" (rank {rank})

Name ONE replacement that:
- genuinely belongs on this list — it must be TRUE that it fits the title's \
superlative, not merely plausible,
- is NOT already on the list and is not a near-twin of anything on it,
- is EASY to find in a professional stock footage library, and is recognisable \
on screen without a caption. Prefer a well-known, frequently filmed subject \
over an obscure or deep-sea one; that is exactly why the last one failed,
- is not extinct, mythical, microscopic or abstract.

Also write its on-screen caption (AT MOST 5 words, a fragment, no period, true \
of ORDINARY footage of it — not a rare peak moment) and 3 stock search terms, \
best first, EVERY one containing the topic's own words, with the third being \
the bare subject name alone.

Return ONLY JSON:
{{"name": "...", "label": "...", "why": "<why it truly belongs, <=12 words>", \
"queries": ["...", "...", "..."]}}"""

_VERIFY_SUB_PROMPT = """Answer one factual question, with no regard for what \
anyone wants the answer to be.

Would "{name}" be a DEFENSIBLE entry at rank {rank} of 5 on a list titled \
"{title}", where rank 1 is the most extreme?

Say no if the claim is simply false — if it is the wrong kind of thing for that \
title, or if it belongs on the opposite side of it. A sea lion is not one of \
the deadliest predators that hunt sharks; it is what sharks eat. Say no if it \
is a stretch you would have to argue for.

Return ONLY JSON: {{"ok": true|false, "why": "<=12 words"}}"""


def _substitute_is_true(title: str, name: str, rank) -> bool:
    """Second opinion on whether a replacement actually belongs on the list.

    The first call is asked to produce something, and a model asked to produce
    something produces something: a live run replaced an unsourceable Orca on
    "Deadliest Predators That Hunt Sharks" with a SEA LION — at rank 1, the
    payoff slot. Sea lions are what sharks eat. Asking a separate, fresh call
    the plain factual question, with no pressure to fill a slot, catches that.
    """
    try:
        import anthropic
    except ImportError:
        return False
    try:
        resp = anthropic.Anthropic().messages.create(
            model=MODEL, max_tokens=400,
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": _VERIFY_SUB_PROMPT.format(
                name=name, rank=rank, title=title)}])
        txt = "".join(b.text for b in resp.content if b.type == "text")
        m = re.search(r"\{.*\}", txt, re.S)
        if not m:
            return False
        data = first_json(m.group(0))
    except Exception as e:
        log.warning("[clip-gen] substitute verification failed (%s); rejecting.", e)
        return False
    ok = bool(data.get("ok"))
    if not ok:
        log.info("[clip-gen] substitute %r rejected as untrue for %r: %s",
                 name, title, str(data.get("why", ""))[:60])
    return ok


def substitute_item(data: dict, failed_name: str,
                    avoid: list[str] | None = None) -> dict | None:
    """A replacement entry for one that has no sourceable footage.

    A list is worth saving. When a single entry cannot be shown — a tiger shark
    whose stripes are in no stock clip, say — the honest options are to abandon
    the whole ranking or to rank a different animal that genuinely belongs on
    it. The second keeps the day's drop without ever putting a wrong clip under
    a caption, because the CAPTION CHANGES TOO: the video ends up ranking what
    it actually shows.

    `avoid` = replacements already tried for this slot that had no footage,
    so a second attempt asks for something different.

    Returns {name, label, queries} keeping the failed entry's rank, or None.
    """
    load_dotenv()
    if not os.getenv("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
    except ImportError:
        return None

    items = data.get("items", [])
    failed = next((it for it in items
                   if str(it.get("name", "")).lower() == failed_name.lower()), None)
    if not failed:
        return None
    # #1 is the payoff the whole countdown builds to and the list's strongest
    # claim. If THAT cannot be sourced honestly, the list is the wrong list —
    # swapping the headline act for whatever happens to be filmable is how a
    # sea lion ends up ranked the deadliest thing that hunts sharks.
    if failed.get("rank") == 1:
        log.info("[clip-gen] %r is rank 1 — not substituting the payoff slot.",
                 failed_name)
        return None
    listing = ", ".join(str(it.get("name", "")) for it in items)
    try:
        resp = anthropic.Anthropic().messages.create(
            model=MODEL, max_tokens=800,
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": _SUBSTITUTE_PROMPT.format(
                title=data.get("title", ""), items=listing,
                failed=failed_name, rank=failed.get("rank", ""))
                + (f"\n\nAlready tried, NO footage exists — do not suggest: "
                   f"{', '.join(avoid)}." if avoid else "")}])
        txt = "".join(b.text for b in resp.content if b.type == "text")
        m = re.search(r"\{.*\}", txt, re.S)
        if not m:
            return None
        sub = first_json(m.group(0))
    except Exception as e:
        log.warning("[clip-gen] substitution failed for %r: %s", failed_name, e)
        return None

    name = str(sub.get("name") or "").strip()
    if not name:
        return None
    existing = {str(it.get("name", "")).lower() for it in items}
    if name.lower() in existing or name.lower() in {a.lower() for a in avoid or []}:
        log.info("[clip-gen] substitute %r is already on the list; skipping.", name)
        return None
    if _UNFILMABLE_RE.search(name):
        log.info("[clip-gen] substitute %r is unfilmable; skipping.", name)
        return None
    if not _substitute_is_true(data.get("title", ""), name, failed.get("rank")):
        return None
    qs = [q.strip() for q in (sub.get("queries") or [])
          if isinstance(q, str) and q.strip()]
    if not qs:
        qs = [name]
    label = " ".join(str(sub.get("label") or "").strip().rstrip(".").split()[:5])
    return {"rank": failed.get("rank"), "name": name, "label": label,
            "queries": list(dict.fromkeys(qs)), "query": qs[0]}


def _unfilmable_items(data: dict) -> list[str]:
    """Names in `data` that stock footage cannot honestly show.

    Two passes: a keyword pre-filter, then Claude on what survives. The video
    pipeline refuses to render an entry it cannot source, so an unfilmable item
    does not produce a wrong clip — it wastes a whole day's list. Catching it at
    generation time is what keeps the buffer full of lists that can actually be
    built.
    """
    names = [str(it.get("name", "")) for it in data.get("items", [])]
    hits = [n for n in names
            if _UNFILMABLE_RE.search(n) or _UNFILMABLE_RE.search(data.get("title", ""))]
    if hits:
        return names if _UNFILMABLE_RE.search(data.get("title", "")) else hits

    try:
        import anthropic
    except ImportError:
        return []
    if not os.getenv("ANTHROPIC_API_KEY"):
        return []
    # Name quoted and separated from the caption, so the model echoes back a
    # name that matches an item instead of the whole line.
    listing = "\n".join(f'- "{n}"  (on-screen caption: {it.get("label", "")})'
                        for n, it in zip(names, data["items"]))
    try:
        resp = anthropic.Anthropic().messages.create(
            model=MODEL, max_tokens=500,
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": _AUDIT_PROMPT.format(
                title=data.get("title", ""), items=listing)}])
        txt = "".join(b.text for b in resp.content if b.type == "text")
        m = re.search(r"\{.*\}", txt, re.S)
        if not m:
            return []
        bad = first_json(m.group(0)).get("unfilmable") or []
    except Exception as e:
        log.warning("[clip-gen] filmability audit failed (%s); keeping list.", e)
        return []
    out = []
    for b in bad:
        if isinstance(b, dict) and b.get("name"):
            out.append(str(b["name"]))
            log.info("[clip-gen] unfilmable: %s (%s)", b["name"], b.get("why", ""))
    return out


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

    # Last gate: a list is only worth keeping if every entry can be SHOWN. One
    # unfilmable item is fatal, not fixable — the renderer refuses to caption
    # stand-in footage, so the list would be abandoned at production time.
    bad = _unfilmable_items(data)
    if bad:
        log.warning("[clip-gen] rejected %r — no real footage can exist for: %s",
                    title, ", ".join(bad))
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
                seed: str | None = None,
                mined: list[str] | None = None) -> list[str]:
    """Choose ``n`` distinct topics, biased toward high-performing groups.

    `mined` holds subjects the trend miner found doing well on YouTube right
    now. They're folded in as an extra pool so the channel isn't limited to the
    topics someone wrote down in advance — roughly a third of picks come from
    live evidence, the rest from the curated list, which keeps the channel on
    its niche even when a week's search results are noisy.
    """
    rng = random.Random(seed)
    exclude = set(exclude or ())
    weights = _group_weights(perf)
    avail: dict[str, list[str]] = {
        g: [t for t in ts if t not in exclude] for g, ts in TOPIC_GROUPS.items()
    }
    mined_pool = [m for m in (mined or []) if m not in exclude]
    picked: list[str] = []
    while len(picked) < n and (any(avail.values()) or mined_pool):
        if mined_pool and (not any(avail.values()) or rng.random() < 0.35):
            picked.append(mined_pool.pop(0))       # best-performing first
            continue
        live = [g for g, ts in avail.items() if ts]
        if not live:
            break
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

    # What's actually working in the niche right now. Cached for a week, and
    # empty if the API is unreachable — generation still runs off the curated
    # list in that case, it just stops learning from outside.
    trending, mined = [], []
    try:
        from sourcing.trend_miner import trending_topics, winning_titles
        trending = winning_titles(20)
        mined = trending_topics(20)
        if trending:
            log.info("[clip-gen] %d proven performers in the niche informing "
                     "this batch (top: %s).", len(trending),
                     trending[0]["title"][:50])
    except Exception as e:
        log.info("[clip-gen] trend data unavailable (%s); using the curated "
                 "topic list only.", e)

    # Subjects that beat their OWN channel's median on the ten reference
    # channels. A keyword search (above) finds what is big in the lane; this
    # finds what OVERPERFORMED for a channel shaped like ours, which is the
    # better signal about a subject and is not distorted by channel size.
    try:
        from sourcing.rival_miner import winning_subjects
        rivals = winning_subjects(15)
        if rivals:
            # Interleaved rather than appended, so the two sources alternate
            # instead of one monopolising the front of the pool.
            merged = []
            for a, b in zip_longest(mined, rivals):
                if a:
                    merged.append(a)
                if b:
                    merged.append(b)
            mined = merged
            log.info("[clip-gen] %d rival breakout subject(s) in the pool "
                     "(top: %s).", len(rivals), rivals[0][:50])
    except Exception as e:
        log.info("[clip-gen] rival data unavailable (%s).", e)

    saved: list[Path] = []
    tried: set[str] = set()
    while len(saved) < n:
        need = n - len(saved)
        topics = pick_topics(need + 3, perf=perf, exclude=tried, seed=seed,
                             mined=mined)
        if not topics:
            log.warning("[clip-gen] topic pool exhausted after %d new dataset(s).",
                        len(saved))
            break
        progressed = False
        for t in topics:
            if len(saved) >= n:
                break
            tried.add(t)
            d = generate_clip_ranking(t, avoid=[e["title"] for e in existing],
                                      trending=trending)
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
            perf = db.subreddit_performance(prefix="clip_")
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
