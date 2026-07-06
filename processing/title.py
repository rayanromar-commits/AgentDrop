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
import re
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


SERIES_SYSTEM_PROMPT = """You are the title writer for a viral short-form video \
channel (YouTube Shorts) that narrates real Reddit stories (AITA, petty revenge, \
entitled people, confessions). A long story is being posted as a SERIES of N \
sequential clips (Part 1, Part 2, ...). Your job: write N titles — ONE per part — \
that are DISTINCT from each other, so the parts do NOT look like duplicate \
re-uploads (identical titles get the later parts suppressed to 0 views).

Each title must:
- Be a SHORT, EXAGGERATED, superlative line that makes someone tap: superlatives \
("most", "worst", "craziest", "-est", "ever"), ONE word in ALL CAPS (INSANE, \
ENTITLED, PETTY, SAVAGE), a clear side to take, a curiosity gap that does NOT \
resolve the story.
- Reflect a DIFFERENT beat/angle of the story from the other parts — escalation, \
the twist, the payback, the fallout — so the N titles read as different videos, \
not one title with a number bolted on.
- Be 4-9 words, at most ~60 characters, Title Case with exactly ONE all-caps \
power word, ending in . ! or ?.

Hard rules:
- Do NOT number them or write "Part 1/2/3" — the app appends part numbers itself.
- NO hashtags, NO emojis, NO surrounding quotes.
- Do NOT invent facts not in the story. Exaggerated FRAMING only.
- No slurs, nothing sexually explicit.

Output EXACTLY N lines, one title per line, in part order. Nothing else — no \
numbering, no preamble, no blank lines."""


def generate_series_titles(base_id: str, title: str, body: str, subreddit: str,
                           n: int, config: dict) -> list[str] | None:
    """Return N distinct titles (one per part) for a multi-part story.

    Differentiates a series so its parts don't share one headline (the
    identical-title signal that helps land later parts in the 0-view jail).
    Cached per part_id (``{base_id}_pI``) in the titles table so re-renders /
    resumes never regenerate. Returns None (caller falls back to
    ``base title (Part i/n)``) whenever the feature is off, the key is missing,
    the call fails, or the result is unusable — nothing breaks.
    """
    if n <= 1:
        return None
    tcfg = (config or {}).get("title", {})
    if not tcfg.get("enabled", False):
        return None

    # Cache hit: every part already has a stored title.
    cached = [db.get_title(f"{base_id}_p{i}") for i in range(1, n + 1)]
    if all(c for c in cached):  # non-None and non-empty for every part
        return cached  # type: ignore[return-value]

    load_dotenv()
    if not os.getenv("ANTHROPIC_API_KEY"):
        log.warning("[title] no API key; series falls back to a shared title.")
        return None
    try:
        import anthropic
    except ImportError:
        log.warning("[title] 'anthropic' not installed; shared series title.")
        return None

    body = (body or "").strip()
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + " […]"
    user = (
        f"Subreddit: r/{subreddit}.\n\n"
        f"Original (boring) title: {title}\n\n"
        f"Full story (it will be split into {n} sequential parts):\n{body}\n\n"
        f"Write exactly {n} distinct titles now, one per line, in part order."
    )

    model = tcfg.get("model", DEFAULT_MODEL)
    try:
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=model,
            max_tokens=400,
            system=SERIES_SYSTEM_PROMPT,
            output_config={"effort": "medium"},
            messages=[{"role": "user", "content": user}],
        )
    except Exception as e:
        log.error("[title] series generation failed (%s); shared title.", e)
        return None

    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    lines = [ln.strip().strip('"').strip("'").strip()
             for ln in text.splitlines() if ln.strip()]
    # Strip any leading "1." / "1)" / "- " the model may add despite the rules.
    lines = [re.sub(r"^\s*(?:\d+[.)]|-|\*)\s*", "", ln).strip() for ln in lines]
    lines = [ln.replace("#Shorts", "").replace("#shorts", "").strip()
             for ln in lines]

    # Must be exactly N usable, distinct titles — else fall back safely.
    ok = (len(lines) == n
          and all(ln and "#" not in ln and len(ln) <= MAX_TITLE_CHARS
                  for ln in lines)
          and len({ln.lower() for ln in lines}) == n)
    if not ok:
        log.warning("[title] unusable series titles for %s (%d lines); "
                    "shared title.", base_id, len(lines))
        return None

    for i, ln in enumerate(lines, 1):
        db.save_title(f"{base_id}_p{i}", ln)
    log.info("[title] %s -> %d distinct part titles", base_id, n)
    return lines


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
