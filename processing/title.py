"""
Title generation — write an EXAGGERATED, superlative YouTube title per story.

The raw Reddit title is a flat summary ("Airport no pickup") — boring, and the
title is a top-3 CTR/discovery lever (it's what shows on the homepage shelf).
This module asks Claude to rewrite it as a punchy, superlative, side-to-take
line that makes people tap ("Her Most ENTITLED Airport Demand Yet").

Design (mirrors processing/hook.py):
  * ONE title per STORY (not per part), cached in the DB by post_id so a
    multi-part series shares the same base title and re-renders never
    regenerate. The caller appends "(Part i/n)".
  * Fails SAFE: any problem (no API key, package missing, API error, or a
    low-confidence "SKIP") returns None, and the caller falls back to the
    cleaned raw story title — nothing breaks.

Honors the channel's video-settings policy: SHORT (homepage surfacing), caps +
punctuation, NO hashtags, no #Shorts, no emojis, no quotes.

Needs ANTHROPIC_API_KEY in .env (locally) or the environment (cloud).
Test it:  python3 -m processing.title
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from agentdrop_common import setup_logging
from database import db

log = setup_logging()

# Smartest model — the title is a top CTR lever. ~half a cent per story.
DEFAULT_MODEL = "claude-opus-4-8"

# Body longer than this is trimmed before sending (the title only needs the
# gist + the juiciest conflict, not the full text).
MAX_BODY_CHARS = 3500

# Hard length guard: a title longer than this is rejected (won't surface on the
# homepage shelf). Kept tight so titles stay punchy.
MAX_TITLE_CHARS = 70

SYSTEM_PROMPT = """You are the title writer for a viral short-form video channel \
(YouTube Shorts) that narrates real Reddit stories (AITA, petty revenge, \
entitled people, confessions). Your ONE job: rewrite the story's boring title \
as a SHORT, EXAGGERATED, superlative title that makes someone stop and tap.

What a great title does:
- Uses SUPERLATIVE, over-the-top framing: "most", "worst", "craziest", \
"pettiest", "-est" words, "ever", "of all time", "you'll ever see". Make it \
sound like the single most extreme version of this story that exists.
- Puts ONE word in ALL CAPS for punch (e.g. INSANE, ENTITLED, PETTY, SAVAGE).
- Frames a side to take — someone is clearly wrong, someone got destroyed, a \
line got crossed — so viewers itch to weigh in.
- Teases the conflict WITHOUT resolving it (curiosity gap). Do NOT give away \
the ending.

Hard rules:
- SHORT: aim for 4-9 words, at most ~60 characters. It must fit the homepage \
shelf. Shorter and punchier is better.
- Use normal Title Case with exactly ONE all-caps power word, and end with \
punctuation (. ! or ?) — this channel's style.
- NO hashtags, NO "#Shorts", NO emojis, NO surrounding quotes, NO "Part 1".
- Do NOT invent facts that aren't in the story. Exaggerated FRAMING is fine \
("her most entitled demand"); inventing events that didn't happen is not — it \
gets the video flagged.
- Do not use slurs or sexually explicit wording.

If — and ONLY if — you genuinely cannot beat the story's own title, reply with \
exactly: SKIP

Output ONLY the title line (or SKIP). Nothing else — no preamble, no quotes."""


def _build_user_message(title: str, body: str, subreddit: str) -> str:
    body = (body or "").strip()
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + " […]"
    return (
        f"Subreddit: r/{subreddit}.\n\n"
        f"Original (boring) title: {title}\n\n"
        f"Story:\n{body}\n\n"
        "Write the exaggerated, superlative title now."
    )


def generate_title(post_id: str, title: str, body: str, subreddit: str,
                   config: dict) -> str | None:
    """Return a punchy superlative title for the story, or None to fall back.

    Caches by post_id so a series / resumes reuse the same base title for free.
    Returns None (caller uses the raw title) whenever the feature is off, the
    key is missing, the call fails, the model SKIPs, or the result is unusable.
    """
    tcfg = (config or {}).get("title", {})
    if not tcfg.get("enabled", False):
        return None

    cached = db.get_title(post_id)
    if cached is not None:
        return cached or None  # "" cached = a prior SKIP -> fall back

    load_dotenv()
    if not os.getenv("ANTHROPIC_API_KEY"):
        log.warning("[title] ANTHROPIC_API_KEY not set; using the raw title.")
        return None

    try:
        import anthropic
    except ImportError:
        log.warning("[title] 'anthropic' package not installed; raw title.")
        return None

    model = tcfg.get("model", DEFAULT_MODEL)
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
    except Exception as e:  # never let a title failure break production
        log.error("[title] generation failed (%s); using the raw title.", e)
        return None

    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    # Strip stray quotes / a trailing #Shorts the model may add despite rules.
    text = text.strip().strip('"').strip("'").strip()
    text = text.replace("#Shorts", "").replace("#shorts", "").strip()

    if not text or text.upper() == "SKIP":
        log.info("[title] model declined for %s; using the raw title.", post_id)
        db.save_title(post_id, "")  # remember the SKIP so we don't pay twice
        return None

    # Guards: single line, no hashtags leaked, within the length cap.
    if "\n" in text or "#" in text or len(text) > MAX_TITLE_CHARS:
        log.warning("[title] unusable result for %s (%r); using the raw title.",
                    post_id, text[:80])
        db.save_title(post_id, "")
        return None

    db.save_title(post_id, text)
    log.info("[title] %s -> %r", post_id, text)
    return text


if __name__ == "__main__":
    # Quick manual test against a sample story (spends a fraction of a cent).
    from agentdrop_common import load_config

    db.init_db()
    cfg = load_config()
    cfg.setdefault("title", {})["enabled"] = True
    samples = [
        {
            "post_id": "_titletest_airport",
            "title": "Airport no pickup",
            "subreddit": "AmItheAsshole",
            "body": (
                "My partner and two kids went on vacation with her sister. I "
                "stayed behind for work. She was furious I didn't drive 45 "
                "minutes to the airport at midnight to help with the luggage "
                "and car seats. Am I the asshole for not driving down?"
            ),
        },
    ]
    for s in samples:
        db.save_title(s["post_id"], "")  # clear cache path? no-op; force below
    for s in samples:
        # Force a fresh call by deleting any cached row first.
        conn = db.get_connection()
        with conn:
            conn.execute("DELETE FROM titles WHERE post_id = ?", (s["post_id"],))
        conn.close()
        out = generate_title(s["post_id"], s["title"], s["body"], s["subreddit"], cfg)
        print(f"\nRAW  : {s['title']}")
        print(f"TITLE: {out if out else '(none — would use raw title)'}")
