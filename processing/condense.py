"""
Condense — shorten an over-length story so it fits the part cap.

Videos are capped at N parts (config: splitting.max_parts) of ~words_per_part
words each, i.e. a hard word ceiling. Stories over that ceiling used to be
SKIPPED. Instead, this asks Claude to tighten the story down to the ceiling
while keeping the whole arc — setup, conflict, and (crucially) the payoff/ending
— so a good-but-long story still gets made instead of thrown away.

This is a real rewrite (unlike voice_direction, which may only add tags), so it
can only shorten stories the user chose — it never invents. Guardrails keep it
honest: the result must actually be shorter, fit the ceiling, and not be gutted
to a stub.

Design (mirrors processing/punch_up.py):
  * Runs per STORY (before splitting), cached in the DB by post_id so
    re-renders / resumes reuse the same condensed text without paying again.
  * Fails SAFE: any problem (no API key, package missing, API error) OR a
    guard rejection returns the ORIGINAL body — which then may still be skipped
    if it's over the ceiling (we tried).

Needs ANTHROPIC_API_KEY in .env (locally) or the environment (cloud).
Test it:  python3 -m processing.condense
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

# Reject a "condense" that guts the story below this fraction of the target —
# that usually means the model dropped whole beats instead of tightening.
MIN_KEEP_FRACTION = 0.55


SYSTEM_PROMPT = """You are a story editor for a short-form video channel that \
narrates real Reddit stories (AITA, petty revenge, entitled people, \
confessions). You are given ONE story that is a bit too long. Your job: tighten \
it so it fits a hard length limit, WITHOUT losing the story.

How to shorten:
- Cut filler, throat-clearing, repetition, tangents, over-explaining, and \
meta-asides ("edit:", "for context", update/clarification paragraphs). Merge \
wordy sentences into tighter ones.
- KEEP the full arc: the setup, the conflict/turn, and especially the PAYOFF or \
ending — never cut the resolution. Keep the specific, vivid details that make \
the story land.
- Keep it in the original narrator's first-person voice and tone. It must read \
naturally aloud.

ABSOLUTE rules — breaking any one means your output is thrown away:
- Do NOT invent or change facts, names, numbers, events, or the outcome. Only \
REMOVE and TIGHTEN what's already there.
- Do NOT add commentary, headings, labels, quotes around the text, or a title. \
Output ONLY the shortened story body.
- Stay at or under the word limit you are given."""


def _build_user_message(body: str, target_words: int) -> str:
    return (
        f"Shorten this story to AT MOST {target_words} words (aim a little "
        f"under). Keep the whole arc and the ending; cut only filler. Return "
        f"only the shortened body.\n\n{body}"
    )


def condense_body(post_id: str, title: str, body: str,
                  ceiling_total_words: int, config: dict) -> str:
    """Return a shortened body so that title+body fits `ceiling_total_words`.

    Caches by post_id. Returns the ORIGINAL body unchanged on any failure or if
    the shortened version fails the safety guards (in which case the caller may
    still skip the story for being too long).
    """
    ccfg = (config or {}).get("condense", {})
    if not ccfg.get("enabled", False):
        return body

    cached = db.get_condensed(post_id)
    if cached is not None:
        return cached or body  # "" cached = a prior decline -> original

    load_dotenv()
    if not os.getenv("ANTHROPIC_API_KEY"):
        log.warning("[condense] ANTHROPIC_API_KEY not set; leaving body as-is.")
        return body

    try:
        import anthropic
    except ImportError:
        log.warning("[condense] 'anthropic' package not installed; body as-is.")
        return body

    # The REAL constraint is that title+body fits the ceiling. We tell the model
    # a tighter target (extra margin) because models don't hit word counts
    # exactly, and we accept anything that actually fits.
    title_words = len(title.split())
    max_body = max(60, ceiling_total_words - title_words)   # true limit
    # Models overshoot tight word targets by ~15-20%, so ASK for ~20% under the
    # true limit; the overshoot then still lands under it.
    target_body = max(60, int(max_body * 0.80))
    orig_words = len(body.split())
    if orig_words <= max_body:
        return body  # already fits; nothing to do

    model = ccfg.get("model", DEFAULT_MODEL)

    def _call(messages: list) -> str:
        resp = anthropic.Anthropic().messages.create(
            model=model, max_tokens=1500, system=SYSTEM_PROMPT,
            output_config={"effort": "medium"}, messages=messages,
        )
        return "".join(b.text for b in resp.content if b.type == "text").strip()

    # Up to 2 attempts: if the first is still over the true limit, send it back
    # with the exact overage so the model tightens further.
    messages = [{"role": "user", "content": _build_user_message(body, target_body)}]
    text = ""
    try:
        for attempt in range(3):
            text = _call(messages)
            n = len(text.split())
            if n <= max_body:
                break
            log.info("[condense] %s attempt %d over (%d > %d); retrying.",
                     post_id, attempt + 1, n, max_body)
            messages += [
                {"role": "assistant", "content": text},
                {"role": "user", "content":
                    f"That is still {n} words — over the hard limit of {max_body}. "
                    f"Cut it to at most {target_body} words. Keep the setup, the "
                    f"conflict, and the ending; remove more detail from the "
                    f"middle."},
            ]
    except Exception as e:  # never let a condense failure break production
        log.error("[condense] failed (%s); leaving body as-is.", e)
        return body

    new_words = len(text.split())
    # Guards: non-empty, actually fits the ceiling, and not gutted to a stub.
    if not text or new_words > max_body or new_words < target_body * MIN_KEEP_FRACTION:
        log.info("[condense] %s rejected (%d words, limit %d); body as-is.",
                 post_id, new_words, max_body)
        db.save_condensed(post_id, "")
        return body

    db.save_condensed(post_id, text)
    log.info("[condense] %s condensed %d -> %d words (limit %d).",
             post_id, orig_words, new_words, max_body)
    return text


if __name__ == "__main__":
    from agentdrop_common import load_config

    db.init_db()
    cfg = load_config()
    cfg.setdefault("condense", {})["enabled"] = True
    sample_id = "_condensetest"
    conn = db.get_connection()
    with conn:
        conn.execute("DELETE FROM condensed WHERE post_id = ?", (sample_id,))
    conn.close()
    title = "AITA for taking my backpack back"
    body = (
        "I need your opinion. " * 3 +
        ("I hung a backpack in a shared closet at work for a whole school year "
         "and kept forgetting to take it home. When a room was cleared for "
         "renovations, I finally went to grab it and it was gone. I asked in "
         "the group chat and got no reply. Later I found it in the closet full "
         "of someone else's stuff, identified by a keychain I'd left inside. I "
         "dumped their things in a neat pile and took my backpack home. It "
         "turned out a cleaner had been using it because her own bag broke, and "
         "she sent me a long message calling me disrespectful, especially since "
         "I'm a teacher. I didn't know whose things they were and was rushing to "
         "my kid's appointment. ") * 4
    )
    ceiling = cfg["splitting"]["words_per_part"] * cfg["splitting"]["max_parts"]
    out = condense_body(sample_id, title, body, ceiling, cfg)
    print(f"\nORIGINAL words: {len(body.split())}")
    print(f"CONDENSED words: {len(out.split())}  (ceiling {ceiling})")
    print("\n--- CONDENSED ---\n" + out)
