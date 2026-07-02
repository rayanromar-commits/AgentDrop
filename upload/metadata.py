"""
Generate YouTube + TikTok metadata from a story.

YouTube (per 2026-07-01 channel policy) gets a short title, a BLANK
description, and NO tags — source credit is burned on-screen as an
"r/subreddit" caption instead (see video/assemble.py, COPYRIGHT_NOTES.md).
TikTok still uses inline hashtags in its caption.
"""

import re

# Map subreddits to a couple of extra relevant tags (TikTok captions only).
SUBREDDIT_TAGS = {
    "AmItheAsshole": ["aita", "amitheasshole"],
    "tifu": ["tifu"],
    "nosleep": ["nosleep", "scarystories", "horrorstories"],
    "LetsNotMeet": ["letsnotmeet", "scarystories", "truescary", "creepy"],
    "shortscarystories": ["scarystories", "horror", "creepypasta"],
    "confession": ["confession", "confessions", "storytime"],
    "pettyrevenge": ["pettyrevenge", "revenge"],
    "ProRevenge": ["prorevenge", "revenge"],
    "MaliciousCompliance": ["maliciouscompliance"],
    "entitledparents": ["entitledparents", "entitled"],
}


def _clean_title(title: str, max_len: int = 95) -> str:
    """Tidy a story title into a YouTube title (<=100 chars)."""
    t = title.strip()
    if len(t) > max_len:
        t = t[:max_len].rsplit(" ", 1)[0] + "..."
    return t


def generate_metadata(story: dict) -> dict:
    """Return {title, description, tags} for a story.

    Channel policy (set 2026-07-01): short punchy titles, NO hashtags, a
    completely BLANK description, and NO tags. YouTube's recommendation
    engine classifies Shorts by the video itself (vertical + <3min), so we
    feed it zero metadata to bias against and let it categorize naturally.
    Source credit lives as an on-screen "r/subreddit" caption in the video,
    not in the description. Category is forced to Entertainment via config.
    """
    title = _clean_title(story["title"])

    return {
        "title": title,       # short & sweet, no #Shorts / no hashtags
        "description": "",     # intentionally blank — no metadata to bias YouTube
        "tags": "",            # no tags at all
    }


# TikTok's caption (the API "title" field) allows up to 2200 chars, with
# hashtags written inline in the caption itself.
TIKTOK_MAX_CAPTION = 2200
# Discovery hashtags TikTok viewers expect, prepended to the niche tags.
TIKTOK_BASE_TAGS = ["fyp", "foryou", "storytime", "reddit", "redditstories"]


def generate_tiktok_caption(story: dict) -> str:
    """Build a TikTok caption (text + inline hashtags) within TikTok's limit."""
    sub = story.get("subreddit", "")
    title = _clean_title(story["title"], max_len=150)

    tags = TIKTOK_BASE_TAGS + SUBREDDIT_TAGS.get(sub, [])
    # De-dupe while preserving order, cap the count so captions stay clean.
    seen, ordered = set(), []
    for t in tags:
        key = re.sub(r"[^A-Za-z0-9]", "", t).lower()
        if key and key not in seen:
            seen.add(key)
            ordered.append(key)
    hashtags = " ".join(f"#{t}" for t in ordered[:8])

    caption = f"{title}\n\n{hashtags}".strip()
    if len(caption) > TIKTOK_MAX_CAPTION:
        caption = caption[:TIKTOK_MAX_CAPTION].rstrip()
    return caption
