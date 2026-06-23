"""
Generate YouTube title / description / hashtags from a story.

Includes a credit line to the source subreddit (see COPYRIGHT_NOTES.md)
and #Shorts so YouTube treats it as a Short.
"""

import re

# Generic tags that help discovery for this niche.
BASE_TAGS = ["Shorts", "reddit", "redditstories", "storytime", "redditreadings"]

# Map subreddits to a couple of extra relevant tags.
SUBREDDIT_TAGS = {
    "AmItheAsshole": ["aita", "amitheasshole"],
    "tifu": ["tifu"],
    "nosleep": ["nosleep", "scarystories", "horrorstories"],
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
    """Return {title, description, tags} for a story."""
    sub = story.get("subreddit", "")
    title = _clean_title(story["title"])

    # YouTube titles benefit from a #Shorts tag in the title itself.
    yt_title = f"{title} #Shorts"

    tags = BASE_TAGS + SUBREDDIT_TAGS.get(sub, [])
    hashtags = " ".join(f"#{re.sub(r'[^A-Za-z0-9]', '', t)}" for t in tags[:6])

    credit = f"Story from r/{sub} — credit to the original poster." if sub else ""
    description = (
        f"{title}\n\n"
        f"{credit}\n\n"
        f"{hashtags}"
    ).strip()

    return {
        "title": yt_title,
        "description": description,
        "tags": ", ".join(tags),
    }
