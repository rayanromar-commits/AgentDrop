"""
Voice direction — insert ElevenLabs v3 audio tags into a story part's narration.

Every competitor uses a flat, single-voice TTS read. ElevenLabs' v3 model can
PERFORM inline audio tags — [scoffs], [sighs], [whispers], [laughs], emphasis —
which makes the narration sound like a real person telling the story and lifts
completion (fewer swipes). This module asks Claude to sprinkle a FEW such tags
into the body at natural moments, WITHOUT touching a single word of the story.

The tags are heard in the audio but never shown on-screen: voiceover/tts.py
strips anything inside [...] from the caption word list (see _chars_to_words).

Design (mirrors processing/punch_up.py):
  * Runs per PART, right after punch_up, on the narration body.
  * Cached in the DB by part_id, so re-renders / resumes never regenerate.
  * Fails SAFE: any problem (no API key, package missing, API error) OR an edit
    that fails the safety guard returns the ORIGINAL text unchanged — nothing
    breaks, no words drift.

Safety guard (STRICTER than punch_up): strip every [tag] from the model's
output and require what remains to equal the input word-for-word. That proves
the model ONLY added bracketed tags and did not change, drop, reorder, or
invent any story words. We also cap the tag count so it can't over-perform.

Needs ANTHROPIC_API_KEY in .env (locally) or the environment (cloud).
Test it:  python3 -m processing.voice_direction
"""

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from agentdrop_common import setup_logging
from database import db

log = setup_logging()

DEFAULT_MODEL = "claude-opus-4-8"

# Reject an edit that adds more than this many tags (over-performed narration is
# worse than none). ~1 tag per 25-30 words reads naturally for a 120-word part.
MAX_TAGS = 8

_TAG_RE = re.compile(r"\[[^\]\[]*\]")

# The only tags we allow the model to use — a curated set the v3 model performs
# well for storytime. Kept tight so a hallucinated "[explosion]" can't slip in.
ALLOWED_TAGS = {
    "sighs", "scoffs", "laughs", "nervous laughter", "whispers", "exhales",
    "sarcastic", "annoyed", "excited", "hesitates", "pauses",
}

SYSTEM_PROMPT = """You are a voice director for a short-form video channel that \
narrates real Reddit stories aloud (ElevenLabs v3 text-to-speech). You are given \
the narration body for ONE video. Your job: make it sound like a real person \
telling the story by inserting a FEW inline audio tags the voice engine will \
perform — NOT by changing any words.

Audio tags are written in square brackets and inserted between words, e.g.:
  "I texted her back. [scoffs] She had the nerve to ask for more."

You may ONLY use these tags: [sighs] [scoffs] [laughs] [nervous laughter] \
[whispers] [exhales] [sarcastic] [annoyed] [excited] [hesitates] [pauses]

ABSOLUTE rules — breaking any one means your output is thrown away:
- Do NOT change, reword, reorder, add, or delete ANY word of the story. Keep \
every word exactly as given, in the same order. Your ONLY edit is inserting \
bracketed tags from the list above between existing words.
- Use tags SPARINGLY — at most one every 25-30 words, placed only where the \
emotion is obvious from the text. A short body might get just one or two. If \
nothing fits, add nothing.
- Do NOT invent tags outside the allowed list. Do NOT use tags for sound \
effects or anything not in the list.
- Output ONLY the story text with tags inserted. No headings, no quotes, no \
commentary."""


def _strip_tags(text: str) -> str:
    """Remove every [tag] and collapse the whitespace it leaves behind."""
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", text)).strip()


def _tags_in(text: str) -> list[str]:
    """Return the inner names of every [tag] in the text (lowercased)."""
    return [m.group(0)[1:-1].strip().lower() for m in _TAG_RE.finditer(text)]


def _build_user_message(body: str) -> str:
    return (
        "Here is the narration body. Insert a few allowed audio tags per the "
        "rules, keeping every word verbatim. Return only the tagged body.\n\n"
        f"{body}"
    )


def add_voice_direction(part_id: str, body: str, config: dict) -> str:
    """Return the body with a few audio tags inserted, or the original unchanged.

    Caches by part_id so resumes / re-renders reuse the same wording for free.
    Always returns a usable narration string (never None): on any failure or a
    guard rejection it returns `body` untouched.
    """
    vcfg = (config or {}).get("voice_direction", {})
    if not vcfg.get("enabled", False):
        return body

    cached = db.get_voice_direction(part_id)
    if cached is not None:
        return cached or body  # "" cached = a prior decline -> original

    load_dotenv()
    if not os.getenv("ANTHROPIC_API_KEY"):
        log.warning("[voice_direction] ANTHROPIC_API_KEY not set; plain narration.")
        return body

    try:
        import anthropic
    except ImportError:
        log.warning("[voice_direction] 'anthropic' package not installed; plain.")
        return body

    model = vcfg.get("model", DEFAULT_MODEL)
    try:
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=model,
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            output_config={"effort": "medium"},
            messages=[{"role": "user", "content": _build_user_message(body)}],
        )
    except Exception as e:  # never let a tagging failure break production
        log.error("[voice_direction] failed (%s); plain narration.", e)
        return body

    text = "".join(b.text for b in resp.content if b.type == "text").strip()

    # --- Safety guards ---
    # 1. Words must be IDENTICAL once tags are stripped (nothing changed/invented).
    if not text or _strip_tags(text) != body.strip():
        log.info("[voice_direction] %s rejected (words changed); plain narration.",
                 part_id)
        db.save_voice_direction(part_id, "")
        return body
    # 2. Only allowed tags, and not too many.
    tags = _tags_in(text)
    if len(tags) > MAX_TAGS or any(t not in ALLOWED_TAGS for t in tags):
        log.info("[voice_direction] %s rejected (%d tags, bad=%s); plain.",
                 part_id, len(tags), [t for t in tags if t not in ALLOWED_TAGS])
        db.save_voice_direction(part_id, "")
        return body

    db.save_voice_direction(part_id, text)
    log.info("[voice_direction] %s tagged (%d tags: %s).",
             part_id, len(tags), ", ".join(tags) or "none")
    return text


if __name__ == "__main__":
    # Quick manual test against a sample body (spends a fraction of a cent).
    from agentdrop_common import load_config

    db.init_db()
    cfg = load_config()
    cfg.setdefault("voice_direction", {})["enabled"] = True
    sample_id = "_voicedirtest"
    body = (
        "My coworker kept stealing my lunches from the office fridge. "
        "I labeled them, I hid them, nothing worked. So I made one last lunch "
        "just for him. The next morning he was very quiet at his desk. "
        "He never touched the fridge again."
    )
    db.save_voice_direction(sample_id, "") if False else None
    out = add_voice_direction(sample_id, body, cfg)
    print("\n--- ORIGINAL ---\n" + body)
    print("\n--- TAGGED ---\n" + out)
    print("\n--- WORDS-ONLY MATCH? ---")
    print(_strip_tags(out) == body.strip())
