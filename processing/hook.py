"""
Hook generation — write a scroll-stopping opening LINE for each story.

The raw Reddit title is a summary; it rarely stops the scroll. This module
asks Claude (Opus 4.8) to write ONE spoken opening line engineered to grab
the first 1-2 seconds — a curiosity gap, an in-medias-res jolt, or the single
weirdest detail in the story, aimed at THIS subreddit's audience.

Design:
  * ONE hook per STORY (not per part) so a multi-part series opens
    consistently.
  * Cached in the DB by post_id, so re-renders / resumes never regenerate
    (no double spend, identical wording on retries).
  * Fails SAFE: any problem (no API key, package missing, API error, or a
    low-confidence "SKIP" from the model) returns None, and the pipeline
    falls back to the title-first opening — nothing breaks.

Needs ANTHROPIC_API_KEY in .env (locally) or the environment (cloud).
Test it:  python3 -m processing.hook
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from agentdrop_common import setup_logging
from database import db

log = setup_logging()

# Smartest model — quality is the whole point of the hook. At 3 videos/day
# this costs roughly half a cent per video. Override in config.yaml (hook.model).
DEFAULT_MODEL = "claude-opus-4-8"

# Body longer than this is trimmed before sending (keeps cost down; the hook
# only needs the gist + the juiciest details, not the full text).
MAX_BODY_CHARS = 3500

SYSTEM_PROMPT = """You are the hook writer for a viral short-form video channel \
(YouTube Shorts / TikTok) that narrates real Reddit stories over background \
footage. Your ONE job: write the first line the narrator speaks — the cold open \
that decides whether someone keeps watching or swipes away in the next 1.5 seconds.

What a great hook does:
- Drops the viewer mid-action or mid-shock (in medias res), or opens a curiosity \
gap the brain CANNOT leave unfinished.
- Leads with the single weirdest, most specific, most visceral detail in the story \
— the thing that makes someone go "wait, WHAT?" and stop their thumb.
- Sounds like a real person blurting out the craziest part first, NOT a narrator \
politely introducing a topic.
- Is tuned to the audience of the subreddit it came from (pettiness, dread, \
betrayal, sweet revenge, secondhand rage — whatever that crowd shows up for).

Hard rules:
- ONE sentence, ideally 6-16 words. It is read ALOUD, so it must sound natural \
spoken — no clunky phrasing.
- NEVER summarize the plot. NEVER start with "This is a story about", "So", \
"Here's", "Imagine", "Picture this", "POV", or the story's own title. No hashtags, \
no emojis, no surrounding quotes.
- Be bold and a little unhinged. Weird, blunt and polarizing beats safe and smooth \
— do NOT sand off the edges. Lean into the dark/taboo/uncomfortable angle when the \
story has one.
- Two lines you must not cross, because they get the video DELETED (which defeats \
the entire point): do not invent facts that aren't in the story, and do not use \
slurs or sexually explicit wording. Edgy tone = yes. Bannable content = no.

If — and ONLY if — you genuinely cannot write something that beats the story's own \
title, reply with exactly: SKIP

Output ONLY the hook line (or SKIP). Nothing else — no preamble, no explanation."""


def _build_user_message(title: str, body: str, subreddit: str) -> str:
    body = (body or "").strip()
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + " […]"
    return (
        f"Subreddit: r/{subreddit} (write the hook for THAT audience).\n\n"
        f"Story title: {title}\n\n"
        f"Story:\n{body}\n\n"
        "Write the cold-open hook line now."
    )


def generate_hook(post_id: str, title: str, body: str, subreddit: str,
                  config: dict) -> str | None:
    """Return a punchy spoken hook line for the story, or None to fall back.

    Caches by post_id so resumes / re-renders reuse the same line for free.
    Returns None (and the caller opens with the title instead) whenever the
    feature is off, the key is missing, the call fails, or the model SKIPs.
    """
    hcfg = (config or {}).get("hook", {})
    if not hcfg.get("enabled", False):
        return None

    cached = db.get_hook(post_id)
    if cached is not None:
        return cached or None  # "" cached = a prior SKIP -> fall back

    load_dotenv()
    if not os.getenv("ANTHROPIC_API_KEY"):
        log.warning("[hook] ANTHROPIC_API_KEY not set; opening with the title.")
        return None

    try:
        import anthropic
    except ImportError:
        log.warning("[hook] 'anthropic' package not installed; opening with title.")
        return None

    model = hcfg.get("model", DEFAULT_MODEL)
    try:
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=model,
            max_tokens=200,
            system=SYSTEM_PROMPT,
            output_config={"effort": "medium"},
            messages=[{
                "role": "user",
                "content": _build_user_message(title, body, subreddit),
            }],
        )
    except Exception as e:  # never let a hook failure break production
        log.error("[hook] generation failed (%s); opening with the title.", e)
        return None

    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    # The model occasionally wraps the line in quotes despite instructions.
    text = text.strip().strip('"').strip("'").strip()

    if not text or text.upper() == "SKIP":
        log.info("[hook] model declined for %s; opening with the title.", post_id)
        db.save_hook(post_id, "")  # remember the SKIP so we don't pay twice
        return None

    # Guard against a rambling response — a hook is one short sentence.
    if len(text.split()) > 40:
        log.warning("[hook] response too long for %s; opening with the title.",
                    post_id)
        return None

    db.save_hook(post_id, text)
    log.info("[hook] %s -> %r", post_id, text)
    return text


if __name__ == "__main__":
    # Quick manual test against a sample story (spends a fraction of a cent).
    from agentdrop_common import load_config

    db.init_db()
    cfg = load_config()
    cfg.setdefault("hook", {})["enabled"] = True
    sample = {
        "post_id": "_hooktest",
        "title": "My new neighbor keeps leaving notes on my car",
        "body": (
            "It started friendly. Then the notes got specific — they knew "
            "when I left for work, what I wore, the name of my dog who died "
            "last year. I never told anyone that name."
        ),
        "subreddit": "LetsNotMeet",
    }
    # Clear any cached test hook so we actually call the model.
    db.save_hook(sample["post_id"], "") if False else None
    hook = generate_hook(sample["post_id"], sample["title"], sample["body"],
                         sample["subreddit"], cfg)
    print("\nHOOK:", hook if hook else "(none — would open with the title)")
