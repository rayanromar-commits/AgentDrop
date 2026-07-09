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
import time
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
WIKI_API = "https://en.wikipedia.org/w/api.php"       # article lead-image fallback
COMMONS_API = "https://commons.wikimedia.org/w/api.php"  # real-photo file search

# Py3.14 urllib has no default CA bundle on macOS; use certifi (same fix as
# video/emoji_assets.py). Falls back to unverified — we only fetch public NASA.
try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _SSL_CTX = ssl._create_unverified_context()


# A descriptive UA — Wikipedia's API rejects/limits generic agents.
_UA = "AgentDrop/1.0 (educational space ranking video generator)"


def _get(url: str, timeout: int = 25, retries: int = 2) -> bytes:
    """GET with a couple of retries — several image fetches fire per video in
    quick succession, and a transient blip must not drop an item to a fallback."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    last = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, context=_SSL_CTX, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            last = e
            if attempt < retries:
                time.sleep(0.6 * (attempt + 1))
    raise last


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


def _download_valid(urls: list[str], out: Path) -> bool:
    """Try each URL; keep the first that downloads as a real raster image."""
    for url in urls:
        try:
            out.write_bytes(_get(url))
            with Image.open(out) as im:                # validate it's a real image
                im.verify()
            return True
        except Exception:
            out.unlink(missing_ok=True)
            continue
    return False


def _fetch_nasa(query: str, prefer: str | None, out: Path) -> bool:
    """Best public-domain NASA image for `query` -> `out`. Returns success."""
    items = _search(query)
    if not items:
        return False
    # Score candidates: drop bad titles, prefer subject-matching titles.
    toks = [t for t in re.split(r"\W+", (prefer or query).lower()) if len(t) > 2]
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
    urls = [re.sub(r"~(thumb|small|medium|large|orig)\.jpg$", f"~{s}.jpg", href)
            for s in ("orig", "large", "medium")] + [href]
    return _download_valid(urls, out)


# Filenames that are NOT a photo of the object — locator maps, charts, spectra,
# orbit/position diagrams, size-comparison graphics. We want the actual picture.
_BAD_FILE = (
    "map", "locator", "chart", "diagram", "constellation", "iau", "spectrum",
    "light curve", "lightcurve", "orbit", "position", "starmap", "atlas",
    "schematic", "graph", "plot", "logo", "icon", ".svg",
)


def _is_photo_name(name: str) -> bool:
    n = name.lower()
    if not any(n.endswith(e) for e in (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp")):
        return False
    norm = re.sub(r"[_%\-]+", " ", n)                 # underscores/dashes -> spaces
    # Reject non-photos (maps/charts) AND the "named-after" trap: Antares the
    # rocket, Mercury the mission, etc. share the object's name.
    return not any(b in norm for b in _BAD_FILE) and \
        not any(b in norm for b in _BAD_TITLE)


def _commons_photos(query: str, limit: int = 15) -> list[str]:
    """Wikimedia Commons file search -> raster-photo thumbnail URLs (maps/charts
    filtered out), best-match first. Commons content is copyright-clean."""
    params = {
        "action": "query", "format": "json", "formatversion": "2",
        "generator": "search", "gsrsearch": query, "gsrlimit": str(limit),
        "gsrnamespace": "6",                       # File: namespace
        "prop": "imageinfo", "iiprop": "url|mime", "iiurlwidth": "2000",
    }
    try:
        data = json.loads(_get(f"{COMMONS_API}?" + urlencode(params)))
    except Exception as e:
        log.warning("[web] commons search failed for %r: %s", query, e)
        return []
    pages = sorted((data.get("query") or {}).get("pages") or [],
                   key=lambda p: p.get("index", 999))
    urls = []
    for p in pages:
        title = p.get("title", "")               # e.g. "File:Betelgeuse ALMA.jpg"
        info = (p.get("imageinfo") or [{}])[0]
        if not _is_photo_name(title):
            continue
        if "image" not in (info.get("mime") or "image"):
            continue
        url = info.get("thumburl") or info.get("url")
        if url:
            urls.append(url)
    return urls


def _wiki_pageimage(query: str) -> str | None:
    """Lead image of the best-matching Wikipedia article — used only if its
    filename looks like a real photo (not a locator map)."""
    params = {
        "action": "query", "format": "json", "formatversion": "2",
        "generator": "search", "gsrsearch": query, "gsrlimit": "3",
        "gsrnamespace": "0", "prop": "pageimages", "piprop": "thumbnail|name",
        "pithumbsize": "2000",
    }
    try:
        data = json.loads(_get(f"{WIKI_API}?" + urlencode(params)))
    except Exception:
        return None
    pages = sorted((data.get("query") or {}).get("pages") or [],
                   key=lambda p: p.get("index", 999))
    for p in pages:
        src = (p.get("thumbnail") or {}).get("source")
        name = p.get("pageimage", "") or (src or "")
        if src and _is_photo_name(name):
            return src
    return None


def _fetch_web(query: str, out: Path) -> bool:
    """Accurate web-image fallback so an item is never left blank. Prefers a real
    PHOTO of the object from Wikimedia Commons (maps/charts/diagrams filtered
    out); falls back to the Wikipedia article lead image. Copyright-clean."""
    candidates = _commons_photos(query)
    pageimg = _wiki_pageimage(query)
    if pageimg:
        candidates.append(pageimg)
    for url in candidates:
        if _download_valid([url], out):
            log.info("[web] used photo for %r -> %s", query, url.rsplit("/", 1)[-1])
            return True
    return False


def fetch_image(query: str, prefer: str | None = None,
                allow_web: bool = True) -> Path | None:
    """Return a cached local path to an accurate image for `query` (or None).

    Order: (1) NASA public-domain library — filtered to the real subject, not a
    rocket named after it; (2) if NASA has nothing usable, an accurate Wikipedia
    lead image so the item is NEVER left blank (stars/exoplanets rarely have NASA
    photos). `prefer` (the object's name) drives both the NASA scoring and the
    web search. Set allow_web=False to force NASA-only.
    """
    key = hashlib.sha1(f"{query}|{prefer or ''}".encode("utf-8")).hexdigest()[:12]
    out = CACHE_DIR / f"{key}.jpg"
    if out.exists():
        return out
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if _fetch_nasa(query, prefer, out):
        return out
    if allow_web and _fetch_web(prefer or query, out):
        return out
    log.warning("[img] no usable image for %r (prefer=%r)", query, prefer)
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
