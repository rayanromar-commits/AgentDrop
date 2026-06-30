"""
Step 3 — text cleaning and screening.

Two jobs:

  clean_text()   -> turn raw Reddit text into something that reads
                    naturally when spoken aloud (expand abbreviations,
                    remove "Edit:"/"Update:" notes, strip usernames,
                    subreddit mentions, links, and markdown clutter).

  screen_story() -> decide whether a story is allowed to be used,
                    returning (passed, reasons). Checks word count,
                    NSFW flag, profanity/slurs, and your blocklist.

Test it:   python3 processing/screen.py
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdrop_common import load_config, setup_logging

log = setup_logging()

# Common Reddit/internet abbreviations expanded for natural narration.
# Matched as whole words, case-insensitively.
ABBREVIATIONS = {
    # Deliberately CLEAN expansions: these keep narration broadcast-safe
    # and avoid tripping the profanity filter on normal subreddit lingo.
    "AITA": "Am I the jerk",
    "TIFU": "Today I messed up",
    "WIBTA": "Would I be the jerk",
    "NTA": "not the jerk",
    "YTA": "you're the jerk",
    "ESH": "everyone is in the wrong here",
    "NAH": "nobody is in the wrong here",
    "OP": "the original poster",
    "IMO": "in my opinion",
    "IMHO": "in my honest opinion",
    "TL;DR": "in short",
    "TLDR": "in short",
    "FWIW": "for what it's worth",
    "IIRC": "if I recall correctly",
    "AFAIK": "as far as I know",
    "DH": "dear husband",
    "DW": "dear wife",
    "SO": "significant other",
    "BF": "boyfriend",
    "GF": "girlfriend",
    "MIL": "mother in law",
    "FIL": "father in law",
}


def _expand_abbreviations(text: str) -> str:
    for abbr, full in ABBREVIATIONS.items():
        pattern = r"\b" + re.escape(abbr) + r"\b"
        text = re.sub(pattern, full, text, flags=re.IGNORECASE)
    return text


def clean_str(text: str) -> str:
    """Clean a raw string for narration (edit notes, links, markdown...)."""
    # Remove edit/update notes like "Edit:", "EDIT 2:", "Update:" and
    # everything up to the end of that line.
    text = re.sub(
        r"(?im)^\s*(edit|update|tl;?dr)\s*\d*\s*:.*$",
        "",
        text,
    )

    # Strip URLs and markdown links [text](url) -> text
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"www\.\S+", "", text)

    # Remove subreddit and user mentions: r/foo, /r/foo, u/bar, /u/bar
    text = re.sub(r"/?[ru]/\w+", "", text)

    # Stripping a mention/link before a comma leaves " ," — a dangling space
    # before punctuation. Collapse it so narration doesn't pause oddly and
    # captions never start on an orphan comma.
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)

    # Expand abbreviations into spoken words.
    text = _expand_abbreviations(text)

    # Strip leftover markdown symbols (*, _, #, >, backticks).
    text = re.sub(r"[*_#>`]+", "", text)

    # Collapse repeated whitespace/newlines into clean single spaces.
    text = re.sub(r"\s*\n\s*", " ", text)
    text = re.sub(r"[ \t]{2,}", " ", text)

    return text.strip()


def clean_text(title: str, body: str) -> str:
    """Narration-ready text from a raw title + body (title spoken first)."""
    return clean_str(f"{title}. {body}")


def _profanity_filter():
    """Lazily load better_profanity (only when needed)."""
    from better_profanity import profanity
    profanity.load_censor_words()
    return profanity


def screen_story(story: dict, config: dict) -> tuple[bool, list[str]]:
    """Return (passed, reasons). reasons lists why it failed (if any)."""
    reasons: list[str] = []

    # 1. NSFW flag from the source.
    if config.get("skip_nsfw", True) and story.get("over_18"):
        reasons.append("flagged NSFW by source")

    # Clean first so we screen what will actually be narrated.
    cleaned = clean_text(story["title"], story["body"])
    word_count = len(cleaned.split())

    # 2. Length window. With splitting enabled, long stories are allowed
    #    (they'll be cut into parts) up to words_per_part * max_parts.
    lo = config["min_word_count"]
    split_cfg = config.get("splitting", {})
    if split_cfg.get("enabled"):
        hi = split_cfg.get("words_per_part", 375) * split_cfg.get("max_parts", 8)
    else:
        hi = config["max_word_count"]
    if word_count < lo:
        reasons.append(f"too short ({word_count} < {lo} words)")
    elif word_count > hi:
        reasons.append(f"too long ({word_count} > {hi} words, even split)")

    # 3. Profanity / slurs.
    if config.get("reject_profanity", True):
        profanity = _profanity_filter()
        if profanity.contains_profanity(cleaned):
            reasons.append("contains profanity or slurs")

    # 4. Your custom blocklist (substring match so stems like
    #    "masturbat" also catch "masturbating"). Case-insensitive.
    lowered = cleaned.lower()
    for term in config.get("extra_blocklist", []) or []:
        if term.lower() in lowered:
            reasons.append(f"matched blocklist term: '{term}'")

    return (len(reasons) == 0, reasons)


if __name__ == "__main__":
    from sourcing.get_stories import fetch_stories
    from database import db

    db.init_db()
    cfg = load_config()
    stories = fetch_stories(cfg, skip_seen=False)

    log.info("Screening %d stories...\n", len(stories))
    for s in stories:
        passed, reasons = screen_story(s, cfg)
        verdict = "PASS ✅" if passed else "REJECT ❌"
        print(f"\n===== {verdict}  ({s['post_id']}) =====")
        print(f"TITLE: {s['title']}")
        if reasons:
            print("REASONS: " + "; ".join(reasons))
        print("\n--- cleaned narration text ---")
        print(clean_text(s["title"], s["body"]))
