"""
Visual sourcing for the ranking channel — free, copyright-clean images.

v1 source = NASA's public-domain image library (images-api.nasa.gov): no API
key, gorgeous space visuals, all public domain. Given a search query it returns
the best image, downloaded + cached and cover-cropped to a vertical frame ready
for the Ken Burns renderer. Extensible to Pexels/Pixabay (CC0) later for
non-space topics.

Test:  python3 -m media.clip_source "Jupiter planet" "black hole"
"""

import base64
import hashlib
import io
import json
import os
import re
import shutil
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterable
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
from PIL import Image

from agentdrop_common import setup_logging

log = setup_logging()

JUDGE_MODEL = "claude-sonnet-5"       # vision model that picks the best clean image

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

# Wikimedia hard-rate-limits (429) requests for ORIGINAL files on
# upload.wikimedia.org and 400s any thumbnail width outside its standard list —
# so ask the API for a standard width and use the /thumb/ URL it hands back.
# The renderer supersamples to 1620x2880 and Ken Burns zooms a further 1.13x, so
# a 1280-wide thumb arrives visibly soft; ask for 2560 (files smaller than that
# come back as the original, which is what we want anyway).
THUMB_W = 2560

# Quality floor for a candidate image. Below MIN_LONG_SIDE the frame is a heavy
# upscale and looks blurry on a phone; GOOD_LONG_SIDE is what we actually want,
# and resolution breaks ties between otherwise-equal candidates.
MIN_LONG_SIDE = 900
GOOD_LONG_SIDE = 1800


def _thumb(info: dict) -> str | None:
    """The /thumb/ URL from an imageinfo blob. MediaWiki returns the ORIGINAL as
    `thumburl` when it won't render the requested width; that URL gets 429'd, so
    only a real thumb counts."""
    url = info.get("thumburl")
    return url if url and "/thumb/" in url else None


_LAST_HIT: dict[str, float] = {}      # host -> when we last hit it
_MIN_GAP = 0.5                        # min seconds between hits on one host


def _get(url: str, timeout: int = 25, retries: int = 3) -> bytes:
    """GET with retries — several image fetches fire per video in quick
    succession, and a transient blip must not drop an item to a fallback.

    Wikimedia 429s a burst of requests from one client, which silently cost us
    whole items, so hits on a host are spaced out and a 429 backs off (honouring
    Retry-After) rather than burning its retries at full speed."""
    host = urllib.parse.urlparse(url).netloc
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    last = None
    for attempt in range(retries + 1):
        gap = _MIN_GAP - (time.monotonic() - _LAST_HIT.get(host, 0.0))
        if gap > 0:
            time.sleep(gap)
        _LAST_HIT[host] = time.monotonic()
        try:
            with urllib.request.urlopen(req, context=_SSL_CTX, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = e
            if attempt >= retries:
                break
            if e.code == 429:
                try:
                    wait = float(e.headers.get("Retry-After") or 0)
                except ValueError:
                    wait = 0.0
                time.sleep(min(max(wait, 1.5 * (attempt + 1)), 10.0))
            else:
                time.sleep(0.6 * (attempt + 1))
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


def _blocked(terms: Iterable[str]) -> tuple[str, ...]:
    """_BAD_TITLE minus anything the caller is actually asking for.

    The blanket list exists because a query like "Mercury" otherwise returns the
    Mercury PROGRAM. But the channel also runs spaceflight-history lists where
    the rocket, the crew or the spacecraft IS the subject — blocking those words
    unconditionally left items like "Apollo 13" or "Soyuz T-10-1" with nothing to
    show. So a term is only banned when the search isn't about it."""
    want = " ".join(t.lower() for t in terms if t)
    return tuple(b for b in _BAD_TITLE if b.strip() not in want)


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
    bad = _blocked((prefer, query))
    best, best_score = None, -1
    for it in items[:20]:
        title = _title(it).lower()
        if any(b in title for b in bad):
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


def _is_photo_name(name: str, query: str = "") -> bool:
    n = name.lower()
    if not any(n.endswith(e) for e in (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp")):
        return False
    norm = re.sub(r"[_%\-]+", " ", n)                 # underscores/dashes -> spaces
    # Reject non-photos (maps/charts) AND the "named-after" trap: Antares the
    # rocket, Mercury the mission, etc. share the object's name — unless the
    # search is FOR that thing (a spaceflight-history item wants the spacecraft).
    return not any(b in norm for b in _BAD_FILE) and \
        not any(b in norm for b in _blocked((query,)))


def _commons_photos(query: str, limit: int = 15,
                    width: int = THUMB_W) -> list[str]:
    """Wikimedia Commons file search -> raster-photo image URLs (maps/charts
    filtered out), best-match first. Commons content is copyright-clean.

    Asks for a big thumbnail (THUMB_W) rather than the 1280 this used to take —
    1280 lands soft once the renderer supersamples and zooms. MediaWiki won't
    render a thumb wider than the file itself and hands back the ORIGINAL
    instead; that's fine to use when the original is already big enough, and
    beats downscaling to a standard rung for no reason."""
    params = {
        "action": "query", "format": "json", "formatversion": "2",
        "generator": "search", "gsrsearch": query, "gsrlimit": str(limit),
        "gsrnamespace": "6",                       # File: namespace
        "prop": "imageinfo", "iiprop": "url|mime|size", "iiurlwidth": str(width),
    }
    try:
        data = json.loads(_get(f"{COMMONS_API}?" + urlencode(params)))
    except Exception as e:
        log.warning("[web] commons search failed for %r: %s", query, e)
        return []
    pages = sorted((data.get("query") or {}).get("pages") or [],
                   key=lambda p: p.get("index", 999))
    urls: list[str] = []
    for p in pages:
        title = p.get("title", "")               # e.g. "File:Betelgeuse ALMA.jpg"
        info = (p.get("imageinfo") or [{}])[0]
        if not _is_photo_name(title, query):
            continue
        if "image" not in (info.get("mime") or "image"):
            continue
        url = _thumb(info)
        if not url and int(info.get("width") or 0) >= MIN_LONG_SIDE:
            url = info.get("thumburl") or info.get("url")   # the original itself
        if url and url not in urls:
            urls.append(url)
    return urls


def _wiki_pageimage(query: str) -> str | None:
    """Lead image of the best-matching Wikipedia article — used only if its
    filename looks like a real photo (not a locator map)."""
    params = {
        "action": "query", "format": "json", "formatversion": "2",
        "generator": "search", "gsrsearch": query, "gsrlimit": "3",
        "gsrnamespace": "0", "prop": "pageimages", "piprop": "thumbnail|name",
        "pithumbsize": str(THUMB_W),
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
        if src and _is_photo_name(name, query):
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


def _nasa_urls(query: str, prefer: str | None, n: int = 3) -> list[str]:
    """Top NASA candidate image URLs (subject-scored, launch/crew filtered).

    Asks for ~orig (NASA's full-resolution master). The ~large rung this used to
    request 403s on a lot of assets, which silently dropped EVERY NASA candidate
    for those items and left the judge choosing between one or two Commons
    thumbs; ~orig serves fine and is typically well under 2MB."""
    items = _search(query)
    if not items:
        return []
    toks = [t for t in re.split(r"\W+", (prefer or query).lower()) if len(t) > 2]
    bad = _blocked((prefer, query))
    scored = []
    for it in items[:20]:
        title = _title(it).lower()
        if any(b in title for b in bad):
            continue
        href = (it.get("links") or [{}])[0].get("href")
        if href:
            scored.append((sum(2 for t in toks if t in title), href))
    scored.sort(key=lambda x: -x[0])
    return [re.sub(r"~(thumb|small|medium|large|orig)\.jpg$", "~orig.jpg", h)
            for _, h in scored[:n]]


def _dims(path: Path) -> tuple[int, int]:
    try:
        with Image.open(path) as im:
            return im.size
    except Exception:
        return (0, 0)


def _queries(query) -> list[str]:
    """Normalize a dataset item's search term(s) into an ordered query list."""
    if isinstance(query, str):
        return [query]
    return [q for q in (query or []) if isinstance(q, str) and q.strip()]


def _gather_candidates(queries: list[str], prefer: str | None, key: str,
                       limit: int = 8) -> list[dict]:
    """Download candidate images (NASA first, then Wikimedia, then the Wikipedia
    article image) across EVERY query for the judge to choose between.

    Datasets now carry several search terms per item, best-first, because one
    term is a single point of failure: on 2026-07-28 three of five items came
    back with 1-4 unusable candidates and shipped as generic nebula backdrops.
    Every term is searched and the pooled candidates are judged together, so a
    weak first term costs quality instead of costing the image entirely.

    Commons/Wikipedia are searched with BOTH the bare name AND the mission-context
    query, so a generic name (e.g. "Io" -> an Io-moth butterfly) is disambiguated
    by the richer query (e.g. "Io Galileo"). The judge then rejects any strays.
    Anything under MIN_LONG_SIDE is dropped before judging — too small to fill
    the frame without visible blur."""
    terms = list(dict.fromkeys([t for t in ([prefer] + queries) if t]))
    srcs: list[tuple[str, str]] = []
    for q in queries:
        srcs += [("NASA", u) for u in _nasa_urls(q, prefer, 3)]
    com: list[str] = []
    for term in terms:
        com += _commons_photos(term, 4)
    srcs += [("Wikimedia", u) for u in dict.fromkeys(com)]
    for term in terms:
        wp = _wiki_pageimage(term)
        if wp:
            srcs.append(("Wikipedia", wp))

    seen, cand, small = set(), [], []
    for lbl, u in srcs:
        if u in seen:
            continue
        seen.add(u)
        # Index by DOWNLOAD, not by kept-count: two rejects sharing a filename
        # would clobber each other.
        p = CACHE_DIR / f"{key}_c{len(seen)}.jpg"
        # NASA serves some assets at one size rung and 403s the others, so walk
        # down the ladder rather than losing the candidate.
        ladder = [u] if lbl != "NASA" else [
            re.sub(r"~(thumb|small|medium|large|orig)\.jpg$", f"~{s}.jpg", u)
            for s in ("orig", "large", "medium")]
        if not _download_valid(ladder, p):
            continue
        w, h = _dims(p)
        entry = {"src": lbl, "url": u, "path": p, "w": w, "h": h}
        if max(w, h) < MIN_LONG_SIDE:
            log.info("[img] set aside %s candidate (%dx%d, under the floor)",
                     lbl, w, h)
            small.append(entry)
            continue
        cand.append(entry)
        if len(cand) >= limit:
            break

    if not cand and small:
        # A soft image of the right subject still beats an unrelated backdrop —
        # take the largest of the rejects rather than dropping the item.
        small.sort(key=lambda c: -max(c["w"], c["h"]))
        cand = small[:1]
        log.info("[img] all candidates were under %dpx; keeping the largest "
                 "(%dx%d).", MIN_LONG_SIDE, cand[0]["w"], cand[0]["h"])
        small = small[1:]
    for c in small:
        c["path"].unlink(missing_ok=True)
    # Sharpest first, so the judge's shortlist leads with frame-filling images
    # and a tie between equally-good depictions resolves to the better one.
    cand.sort(key=lambda c: -min(max(c["w"], c["h"]) / GOOD_LONG_SIDE, 1.0))
    return cand


def _b64(path: Path, max_side: int = 1024) -> str:
    im = Image.open(path).convert("RGB")
    if max(im.size) > max_side:
        im.thumbnail((max_side, max_side), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


_JUDGE_PROMPT = """You are curating the full-screen background image for one item \
in a cinematic "Top 5" space video. The item is: **{name}**.{context}

Choose the SINGLE best candidate image below. A good image is:
- a striking, clear visual for {name} — a real photo/telescope image, or a \
faithful artist rendering when no photo exists,
- CLEAN: absolutely NO watermark, logo, channel name, URL, social handle, or \
overlaid text/labels of any kind,
- not a star-chart, locator map, graph, or diagram,
- high quality (sharp and large, not a soft little thumbnail).

IMPORTANT — abstract subjects. Many items CANNOT be photographed directly: cosmic \
voids, regions of empty space, forces, distances, speeds, eras. For those, the \
right answer is the best SCIENTIFICALLY APPROPRIATE illustration — a deep-field or \
galaxy-survey image, a large-scale-structure visualization, a relevant galaxy or \
nebula, an artist's impression of the phenomenon. Judge those on whether they'd \
look right and cinematic on screen under that caption. Do NOT reject a candidate \
merely because the literal object isn't identifiable in it; an evocative, on-theme \
space image always beats returning nothing.

Reject (best: -1) ONLY if every candidate is watermarked, has overlaid text, is a \
chart/diagram, is a plainly WRONG subject (a rocket, a crowd, an unrelated animal \
or object), or is too low-quality to fill a phone screen. Returning -1 drops this \
item to a generic backdrop, so use it as a genuine last resort.
Prefer NASA/public-domain when quality is comparable, and prefer the higher \
resolution when two candidates are otherwise equal.

Also decide framing for the winner in a tall 9:16 phone frame:
- "fit"  = the object is roughly round/centred with empty space around it, so \
cropping it to full-bleed would slice its edges — fit it to the WIDTH instead.
- "cover" = the image already fills the frame edge-to-edge (surface texture, a \
nebula/field, or the object bleeds off-frame), so full-bleed is fine.

Return ONLY JSON, with `reason` at most 15 words:
{{"best": <index or -1>, "framing": "fit"|"cover", "reason": "..."}}"""


def _judge(name: str, cand: list[dict], context: str = "") -> tuple[int, str]:
    """Claude-vision pick: (best_index or -1, framing). Any failure -> (0,'cover')
    so the pipeline degrades gracefully to the first candidate, never breaks."""
    load_dotenv()
    if not os.getenv("ANTHROPIC_API_KEY"):
        return 0, "cover"
    try:
        import anthropic
    except ImportError:
        return 0, "cover"
    ctx = f"\nThe narration over this shot says: \"{context}\"" if context else ""
    content = [{"type": "text",
                "text": _JUDGE_PROMPT.format(name=name, context=ctx)}]
    for i, c in enumerate(cand):
        # Resolution is invisible in the downscaled thumbnail the judge sees, so
        # state it — it's how "sharp enough to fill the frame" gets weighed.
        content.append({"type": "text", "text":
                        f"Candidate {i} (source: {c['src']}, {c['w']}x{c['h']}px):"})
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": "image/jpeg", "data": _b64(c["path"])}})
    try:
        resp = anthropic.Anthropic().messages.create(
            # Roomy cap: at 400 a wordy verdict over 8 candidates truncated
            # mid-JSON, which silently demoted the pick to candidate 0.
            model=JUDGE_MODEL, max_tokens=800,
            messages=[{"role": "user", "content": content}])
        txt = "".join(b.text for b in resp.content if b.type == "text")
        m = re.search(r"\{.*\}", txt, re.S)
        if not m:                       # no JSON at all -> keep the best-res pick
            log.warning("[judge] no JSON in reply for %r; using first candidate.", name)
            return 0, "cover"
        data = json.loads(m.group(0))
    except Exception as e:
        log.warning("[judge] failed (%s); using first candidate.", e)
        return 0, "cover"
    best = data.get("best", 0)
    framing = "fit" if data.get("framing") == "fit" else "cover"
    log.info("[judge] %s -> best=%s framing=%s (%s)", name, best, framing,
             str(data.get("reason", ""))[:80])
    if best is None or not isinstance(best, int) or best < 0 or best >= len(cand):
        return -1, framing
    return best, framing


def image_hash(path: Path) -> str:
    """Content hash of an image file — lets a caller tell two sources apart even
    when they resolve to the same picture."""
    return hashlib.sha1(Path(path).read_bytes()).hexdigest()


def fetch_item_visual(query, prefer: str | None = None,
                      exclude: set[str] | None = None,
                      context: str = "") -> tuple[Path | None, str]:
    """Best CLEAN image of a ranked object + its framing hint ('fit'|'cover').

    `query` is a search term or an ordered LIST of them (datasets carry several
    per item so an unphotographable subject still gets a strong on-theme visual).
    Candidates are pooled across every term (NASA -> Wikimedia -> Wikipedia),
    filtered to a resolution that fills the frame without upscaling, and Claude
    vision picks the best watermark-free depiction and decides framing.
    `context` (the narration line) tells the judge what the shot has to convey.
    Returns (None,'cover') only when nothing usable exists, so the renderer can
    fall back to a backdrop.
    `exclude` = image_hash()es already used by earlier items; those candidates are
    dropped BEFORE judging, so neighbouring ranks whose best match is the same
    picture (e.g. two large-scale-structure entries) each get their own.
    Cached per (queries,prefer,exclude): image at <key>.jpg, framing at <key>.framing.
    """
    queries = _queries(query)
    name = prefer or (queries[0] if queries else "")
    exclude = set(exclude or ())
    ex = hashlib.sha1("|".join(sorted(exclude)).encode()).hexdigest()[:8] if exclude else ""
    key = hashlib.sha1(
        f"item|{'|'.join(queries)}|{prefer or ''}|{ex}".encode()).hexdigest()[:12]
    out = CACHE_DIR / f"{key}.jpg"
    fr = CACHE_DIR / f"{key}.framing"
    if out.exists() and fr.exists():
        return out, fr.read_text(encoding="utf-8").strip() or "cover"
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    cand = _gather_candidates(queries, prefer, key)
    if exclude:
        keep = [c for c in cand if image_hash(c["path"]) not in exclude]
        for c in cand:
            if c not in keep:
                c["path"].unlink(missing_ok=True)
        cand = keep
    if not cand:
        log.warning("[item] no candidates for %r (queries=%s)", name, queries)
        return None, "cover"
    best, framing = _judge(name, cand, context)
    if best < 0:
        log.warning("[item] judge rejected all %d candidate(s) for %r", len(cand), name)
        for c in cand:
            c["path"].unlink(missing_ok=True)
        return None, "cover"
    win = cand[best]
    log.info("[item] %r -> %s %dx%d (%d candidate(s))", name, win["src"],
             win["w"], win["h"], len(cand))
    shutil.copy(win["path"], out)
    fr.write_text(framing, encoding="utf-8")
    for c in cand:                                        # tidy the rejected temps
        c["path"].unlink(missing_ok=True)
    return out, framing


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
