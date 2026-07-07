"""
Visual sourcing for the ranking channel — free, copyright-clean images.

v1 source = NASA's public-domain image library (images-api.nasa.gov): no API
key, gorgeous space visuals, all public domain. Given a search query it returns
the best image, downloaded + cached and cover-cropped to a vertical frame ready
for the Ken Burns renderer. Extensible to Pexels/Pixabay (CC0) later for
non-space topics.

Test:  python3 -m media.clip_source "Jupiter planet" "black hole"
"""

import hashlib
import json
import re
import ssl
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image

from agentdrop_common import setup_logging

log = setup_logging()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = PROJECT_ROOT / "media" / "clip_cache"     # gitignored; cloud re-fetches
NASA_SEARCH = "https://images-api.nasa.gov/search"

# Py3.14 urllib has no default CA bundle on macOS; use certifi (same fix as
# video/emoji_assets.py). Falls back to unverified — we only fetch public NASA.
try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _SSL_CTX = ssl._create_unverified_context()


def _get(url: str, timeout: int = 25) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "AgentDrop"})
    with urllib.request.urlopen(req, context=_SSL_CTX, timeout=timeout) as r:
        return r.read()


def _search(query: str, media_type: str = "image") -> list[dict]:
    url = f"{NASA_SEARCH}?" + urlencode({"q": query, "media_type": media_type})
    try:
        return json.loads(_get(url)).get("collection", {}).get("items", [])
    except Exception as e:
        log.warning("[nasa] search failed for %r: %s", query, e)
        return []


# NASA search loves returning rocket launches / crew photos / press events for
# space queries. Skip any result whose title screams "not the actual subject".
_BAD_TITLE = (
    "launch", "rocket", "crew", "astronaut", "cosmonaut", "patch", "portrait",
    "employee", "ceremony", "administrator", "spacex", "falcon", "atlas",
    "delta ", "booster", "kennedy space center", "johnson space center",
    "headquarters", "director", "press", "conference", "student", "teacher",
    "award", "logo", "building", "expedition", "panel", "meeting", "facility",
    "technician", "engineer", "assembl", "test ", "training", "ceremon",
)


def _title(item: dict) -> str:
    try:
        return (item.get("data") or [{}])[0].get("title", "")
    except Exception:
        return ""


def fetch_image(query: str, prefer: str | None = None) -> Path | None:
    """Return a cached local path to the best NASA image for `query` (or None).

    Filters out launch/crew/press photos and prefers results whose title matches
    `prefer` (the actual subject, e.g. the planet name), so we get the object —
    not a rocket named after it. Downloads the highest-res variant and caches.
    """
    key = hashlib.sha1(f"{query}|{prefer or ''}".encode("utf-8")).hexdigest()[:12]
    out = CACHE_DIR / f"{key}.jpg"
    if out.exists():
        return out

    items = _search(query)
    if not items:
        return None
    # Score candidates: drop bad titles, prefer subject-matching titles.
    import re as _re
    toks = [t for t in _re.split(r"\W+", (prefer or query).lower()) if len(t) > 2]
    best, best_score = None, -1
    for it in items[:20]:
        title = _title(it).lower()
        if any(b in title for b in _BAD_TITLE):
            continue
        score = sum(2 for t in toks if t in title)
        if score > best_score:
            best, best_score = it, score
    if best is None:
        best = items[0]                                # everything filtered -> fall back
    href = best["links"][0]["href"]                    # e.g. .../PIA15658~medium.jpg
    # Prefer the biggest variant; fall back down the ladder, then the raw href.
    candidates = [re.sub(r"~(thumb|small|medium|large|orig)\.jpg$",
                         f"~{s}.jpg", href) for s in ("orig", "large", "medium")]
    candidates.append(href)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    for url in candidates:
        try:
            out.write_bytes(_get(url))
            with Image.open(out) as im:               # validate it's a real image
                im.verify()
            return out
        except Exception:
            out.unlink(missing_ok=True)
            continue
    log.warning("[nasa] no usable image for %r", query)
    return None


def cover_image(path: Path, w: int = 1080, h: int = 1920) -> Image.Image:
    """Scale + center-crop an image to exactly WxH (fill, no letterbox)."""
    im = Image.open(path).convert("RGB")
    scale = max(w / im.width, h / im.height)
    nw, nh = round(im.width * scale), round(im.height * scale)
    im = im.resize((nw, nh), Image.LANCZOS)
    left, top = (nw - w) // 2, (nh - h) // 2
    return im.crop((left, top, left + w, top + h))


if __name__ == "__main__":
    for q in (sys.argv[1:] or ["Jupiter planet", "black hole", "Andromeda galaxy"]):
        p = fetch_image(q)
        if p:
            with Image.open(p) as im:
                print(f"  {q!r:32} -> {p.name}  {im.size}")
        else:
            print(f"  {q!r:32} -> NONE")
