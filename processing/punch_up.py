"""
Retention beats — a LIGHT, additive pass over a story part's narration.

Storytime videos lose viewers in the flat stretches between the good parts.
This module asks Claude to insert a few SHORT connective/cliffhanger beats
("but it got worse", "here's the part nobody believes") that keep the viewer
locked in — WITHOUT changing, removing, or inventing any story facts.

Design (mirrors processing/hook.py):
  * Runs per PART (the cleaned story body, before the hook/title/"Part N"
    cues are added — those are never touched).
  * Cached in the DB by part_id, so re-renders / resumes never regenerate.
  * Fails SAFE: any problem (no API key, package missing, API error) OR an
    edit that fails the safety guard returns the ORIGINAL text unchanged —
    nothing breaks, no facts drift.

Safety guard: the edit is accepted only if it is purely additive in size —
its word count must be between 1.0x and 1.25x the original. Anything that
shortens the text (facts dropped) or balloons it (rewritten/invented) is
discarded in favor of the original.

Needs ANTHROPIC_API_KEY in .env (locally) or the environment (cloud).
Test it:  python3 -m processing.punch_up
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from agentdrop_common import setup_logging
from database import db

log = setup_logging()

DEFAULT_MODEL = "claude-opus-4-8"

# Accept an edit only if it stays within this growth band. Below 1.0x means
# words were dropped (facts lost); above this means it was rewritten/padded.
MIN_RATIO = 1.0
MAX_RATIO = 1.25

SYSTEM_PROMPT = """You are a retention editor for a short-form video channel \
that narrates real Reddit stories aloud over background footage. You are given \
the story body for ONE video (or one part of a longer story). Viewers already \
watch most of the way through; your job is to make them watch ALL the way through \
by smoothing the dull connective stretches and sharpening the build to each beat.

Your ONLY allowed edit is to INSERT a few short transition/tension beats between \
existing sentences — things like "but it got worse", "and that's when it clicked", \
"here's the part nobody believes", "she had no idea what was coming". These are \
tiny narrative nudges that pull the viewer to the next moment.

ABSOLUTE rules — breaking any one means your output is thrown away:
- Do NOT change, reword, reorder, or delete any of the original sentences. Keep \
them verbatim, in order. You may ONLY add short bridging phrases between them.
- Do NOT invent or imply facts, events, names, numbers, or outcomes that are not \
already in the story. The beats must be content-free ("it gets worse"), never new \
information.
- Add SPARINGLY — at most one short beat every few sentences. Over-narrating kills \
the story. If the body is already tight, add little or nothing.
- Keep it natural when read ALOUD. No headings, no emojis, no quotes around the \
text, no meta commentary.

Output ONLY the edited story body text. Nothing else — no preamble, no explanation."""


def _build_user_message(body: str) -> str:
    return (
        "Here is the story body. Insert a few short retention beats per the rules, "
        "keeping every original sentence verbatim. Return only the edited body.\n\n"
        f"{body}"
    )


def punch_up(part_id: str, body: str, config: dict) -> str:
    """Return the body with light retention beats, or the original unchanged.

    Caches by part_id so resumes / re-renders reuse the same wording for free.
    Always returns a usable narration string (never None): on any failure or a
    guard rejection it returns `body` untouched.
    """
    pcfg = (config or {}).get("punch_up", {})
    if not pcfg.get("enabled", False):
        return body

    cached = db.get_punch_up(part_id)
    if cached is not None:
        return cached or body  # "" cached = a prior decline -> original

    load_dotenv()
    if not os.getenv("ANTHROPIC_API_KEY"):
        log.warning("[punch_up] ANTHROPIC_API_KEY not set; narrating original.")
        return body

    try:
        import anthropic
    except ImportError:
        log.warning("[punch_up] 'anthropic' package not installed; original.")
        return body

    model = pcfg.get("model", DEFAULT_MODEL)
    try:
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=model,
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            output_config={"effort": "medium"},
            messages=[{"role": "user", "content": _build_user_message(body)}],
        )
    except Exception as e:  # never let an edit failure break production
        log.error("[punch_up] failed (%s); narrating original.", e)
        return body

    text = "".join(b.text for b in resp.content if b.type == "text").strip()

    # Safety guard: must be purely additive in size. Anything shorter (facts
    # dropped) or much longer (rewritten/invented) is rejected.
    orig_words = max(1, len(body.split()))
    ratio = len(text.split()) / orig_words
    if not text or ratio < MIN_RATIO or ratio > MAX_RATIO:
        log.info("[punch_up] %s rejected (ratio %.2f); narrating original.",
                 part_id, ratio)
        db.save_punch_up(part_id, "")  # remember the decline so we don't pay twice
        return body

    db.save_punch_up(part_id, text)
    log.info("[punch_up] %s edited (%d -> %d words).",
             part_id, orig_words, len(text.split()))
    return text


if __name__ == "__main__":
    # Quick manual test against a sample body (spends a fraction of a cent).
    from agentdrop_common import load_config

    db.init_db()
    cfg = load_config()
    cfg.setdefault("punch_up", {})["enabled"] = True
    sample_id = "_punchuptest"
    body = (
        "My coworker kept stealing my lunches from the office fridge. "
        "I labeled them, I hid them, nothing worked. So I made one last lunch "
        "just for him. The next morning he was very quiet at his desk. "
        "He never touched the fridge again."
    )
    # Force a fresh call for the test.
    db.save_punch_up(sample_id, "") if False else None
    out = punch_up(sample_id, body, cfg)
    print("\n--- ORIGINAL ---\n" + body)
    print("\n--- EDITED ---\n" + out)
