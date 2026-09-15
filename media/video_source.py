"""
Stock VIDEO sourcing for the ranked-clip channel — licensed, copyright-clean.

Sources are Pexels (primary) and Pixabay (fallback): both are free, allow
commercial use, and require no attribution. That is a deliberate departure from
how the reference channels in this genre operate — they harvest other creators'
clips and list a takedown address — which is both a copyright and a YouTube
reused-content exposure we are not putting on a working channel.

Given a search term this returns the best CLEAN portrait-ish clip, downloaded and
cached, plus a framing hint for the renderer. Selection reuses the same idea as
the image pipeline: gather several candidates, extract one representative frame
from each, and let a vision model pick.

Needs PEXELS_API_KEY (free, instant: https://www.pexels.com/api/).
Optional PIXABAY_API_KEY for the fallback source.

Test:  python3 -m media.video_source "great white shark breaching"
"""

import base64
import hashlib
import io
import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
from PIL import Image

from agentdrop_common import setup_logging

log = setup_logging()

JUDGE_MODEL = "claude-sonnet-5"        # vision model that picks the best clip

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = PROJECT_ROOT / "media" / "video_cache"     # gitignored; cloud re-fetches
PEXELS_SEARCH = "https://api.pexels.com/videos/search"
PIXABAY_SEARCH = "https://pixabay.com/api/videos/"

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _SSL_CTX = ssl._create_unverified_context()

_UA = "AgentDrop/1.0 (ranked clip video generator)"

# Pexels allows 200 requests/hour. One video is ~15 searches plus downloads, so
# we're far inside it at one upload a day — but space hits anyway so a burst
# never trips a limit, exactly as the image fetcher learned to do with Wikimedia.
_MIN_GAP = 0.4
_LAST_HIT: dict[str, float] = {}

# Clip selection constraints.
MIN_HEIGHT = 720          # below this a 1080x1920 frame is a heavy upscale
MIN_DURATION = 3.0        # shorter than one rank slot is useless
MAX_DURATION = 120.0      # huge files cost download time for one 4s excerpt
CANDIDATES = 6            # clips shown to the judge


def _get(url: str, timeout: int = 40, retries: int = 3,
         headers: dict | None = None) -> bytes:
    """GET with retries and per-host spacing.

    Same discipline as media/clip_source._get: several fetches fire per video in
    quick succession and a transient blip must not silently drop an item to a
    fallback, while a 429 must back off rather than burn its retries at speed."""
    host = urllib.parse.urlparse(url).netloc
    req = urllib.request.Request(url, headers={"User-Agent": _UA, **(headers or {})})
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


def _ffmpeg() -> str:
    """ffmpeg binary — system first, bundled imageio-ffmpeg as fallback."""
    from shutil import which
    exe = which("ffmpeg")
    if exe:
        return exe
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


# --- sources -----------------------------------------------------------------

def _best_file(files: list[dict]) -> dict | None:
    """The best downloadable rendition of one Pexels video.

    Prefers PORTRAIT (the frame is 9:16, so a vertical source fills it without
    throwing away most of the picture), then the tallest that isn't absurd."""
    usable = [f for f in files
              if f.get("link") and (f.get("height") or 0) >= MIN_HEIGHT]
    if not usable:
        return None
    portrait = [f for f in usable if (f.get("height") or 0) > (f.get("width") or 0)]
    pool = portrait or usable
    # Cap the rendition we pull: a 4K master costs a long download for a 4s
    # excerpt that gets scaled to 1080 wide anyway.
    pool.sort(key=lambda f: (abs((f.get("height") or 0) - 1920), -(f.get("height") or 0)))
    return pool[0]


def _search_pexels(query: str, n: int) -> list[dict]:
    """Candidate clips from Pexels. Returns [] when unconfigured or on error."""
    load_dotenv()
    key = os.getenv("PEXELS_API_KEY")
    if not key:
        log.warning("[clips] PEXELS_API_KEY not set — cannot source footage.")
        return []
    # Ask for noticeably more than we need: the duration and height filters
    # below reject roughly half of what comes back, and a judge choosing between
    # two clips is barely choosing at all.
    url = f"{PEXELS_SEARCH}?" + urlencode({
        "query": query, "per_page": max(n * 3, 15), "orientation": "portrait"})
    try:
        raw = _get(url, headers={"Authorization": key})
        data = json.loads(raw)
    except Exception as e:
        log.warning("[clips] pexels search failed for %r: %s", query, e)
        return []
    out = []
    for v in data.get("videos", []):
        dur = float(v.get("duration") or 0)
        if not (MIN_DURATION <= dur <= MAX_DURATION):
            continue
        f = _best_file(v.get("video_files") or [])
        if not f:
            continue
        # Pexels ships a strip of preview frames per video. Judging on one of
        # those instead of the file itself is the difference between pulling
        # ~30 clips per video (500MB, minutes) and pulling 5.
        pics = [pp.get("picture") for pp in (v.get("video_pictures") or [])
                if pp.get("picture")]
        poster = pics[len(pics) // 2] if pics else v.get("image")
        out.append({"src": "pexels", "url": f["link"], "dur": dur,
                    "w": f.get("width") or 0, "h": f.get("height") or 0,
                    "poster": poster, "id": f"pexels_{v.get('id')}"})
    return out


def _search_pixabay(query: str, n: int) -> list[dict]:
    """Candidate clips from Pixabay — the fallback when Pexels comes up short."""
    load_dotenv()
    key = os.getenv("PIXABAY_API_KEY")
    if not key:
        return []
    url = f"{PIXABAY_SEARCH}?" + urlencode({
        "key": key, "q": query, "per_page": max(3, n * 3), "video_type": "film"})
    try:
        data = json.loads(_get(url))
    except Exception as e:
        log.warning("[clips] pixabay search failed for %r: %s", query, e)
        return []
    out = []
    for v in data.get("hits", []):
        dur = float(v.get("duration") or 0)
        if not (MIN_DURATION <= dur <= MAX_DURATION):
            continue
        vids = v.get("videos") or {}
        best = None
        for size in ("large", "medium", "small"):
            f = vids.get(size) or {}
            if f.get("url") and (f.get("height") or 0) >= MIN_HEIGHT:
                best = f
                break
        if not best:
            continue
        out.append({"src": "pixabay", "url": best["url"], "dur": dur,
                    "w": best.get("width") or 0, "h": best.get("height") or 0,
                    "poster": best.get("thumbnail") or v.get("userImageURL"),
                    "id": f"pixabay_{v.get('id')}"})
    return out


# --- download + frame extraction ---------------------------------------------

def _download(cand: dict) -> Path | None:
    """Fetch one candidate clip into the cache. Returns its path, or None."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out = CACHE_DIR / f"{cand['id']}.mp4"
    if out.exists() and out.stat().st_size > 0:
        return out
    try:
        data = _get(cand["url"])
    except Exception as e:
        log.warning("[clips] download failed (%s): %s", cand["url"][:60], e)
        return None
    if len(data) < 10_000:                       # an error page, not a video
        return None
    out.write_bytes(data)
    return out


def poster_frame(path: Path, at: float | None = None) -> Path | None:
    """Extract ONE representative frame, for the vision judge.

    Taken from the clip's midpoint by default: stock clips routinely open on a
    fade or an establishing beat, so frame 0 is a poor sample of what the
    viewer will actually see in a 4-second excerpt."""
    path = Path(path)
    # Always written into the cache, never beside the source: the source may be
    # a committed directory (footage_compressed/) that must not collect stray
    # JPEGs, and the cache is gitignored and re-derivable.
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    jpg = CACHE_DIR / f"{path.stem}.frame.jpg"
    if jpg.exists() and jpg.stat().st_size > 0:
        return jpg
    dur = probe_duration(path)
    ts = at if at is not None else max(0.0, (dur or 4.0) / 2.0)
    cmd = [_ffmpeg(), "-y", "-ss", f"{ts:.2f}", "-i", str(path),
           "-frames:v", "1", "-q:v", "3", str(jpg)]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=90)
    except Exception as e:
        log.warning("[clips] frame extract failed for %s: %s", path.name, e)
        return None
    return jpg if jpg.exists() and jpg.stat().st_size > 0 else None


def probe_duration(path: Path) -> float:
    """Clip duration in seconds (0.0 if it can't be read)."""
    cmd = [_ffmpeg(), "-i", str(path)]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=60)
        m = re.search(rb"Duration: (\d+):(\d+):(\d+\.?\d*)", r.stderr)
        if m:
            h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
            return h * 3600 + mi * 60 + s
    except Exception:
        pass
    return 0.0


def clip_hash(path: Path) -> str:
    """Content hash of a clip — lets a caller tell two sources apart even when
    two different search terms resolve to the same stock footage."""
    h = hashlib.sha1()
    with open(path, "rb") as f:
        # Clips run to tens of MB; the head is plenty to identify one, and
        # hashing the whole file per candidate would dominate render time.
        h.update(f.read(2_000_000))
    return h.hexdigest()


def _download_poster(cand: dict) -> Path | None:
    """Fetch a candidate's preview frame (a few KB) for the judge to look at."""
    if not cand.get("poster"):
        return None
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out = CACHE_DIR / f"{cand['id']}.poster.jpg"
    if out.exists() and out.stat().st_size > 0:
        return out
    try:
        data = _get(cand["poster"], timeout=20)
    except Exception as e:
        log.warning("[clips] poster fetch failed (%s): %s", cand["id"], e)
        return None
    if len(data) < 500:
        return None
    out.write_bytes(data)
    return out


def prune_cache(max_bytes: int = 1_500_000_000) -> int:
    """Keep the clip cache under a size cap, oldest first.

    Railway's disk is ephemeral and small; one video can pull hundreds of MB of
    footage, so an unbounded cache eventually fills it and the daily render dies
    with no obvious cause. Returns the bytes freed."""
    try:
        files = [(f, f.stat()) for f in CACHE_DIR.glob("*") if f.is_file()]
    except Exception:
        return 0
    total = sum(st.st_size for _f, st in files)
    if total <= max_bytes:
        return 0
    files.sort(key=lambda t: t[1].st_mtime)          # oldest first
    freed = 0
    for f, st in files:
        if total - freed <= max_bytes:
            break
        try:
            f.unlink()
            freed += st.st_size
        except Exception:
            pass
    if freed:
        log.info("[clips] pruned %.0f MB from the clip cache.", freed / 1e6)
    return freed


# --- vision judge -------------------------------------------------------------

def _b64(path: Path, max_side: int = 1024) -> str:
    im = Image.open(path).convert("RGB")
    if max(im.size) > max_side:
        im.thumbnail((max_side, max_side), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


_JUDGE_PROMPT = """You are choosing ONE stock video clip to fill the screen for a \
single entry in a fast, silent "ranked clips" YouTube Short. The entry is: \
**{name}**.{context}

Each image below is a frame taken from the MIDDLE of a candidate clip. Pick the \
clip whose frame promises the most watchable few seconds.

A good pick is:
- clearly and obviously about {name} — a viewer must recognise it instantly, with \
no caption to help them,
- VISUALLY STRIKING: dramatic, close, well-lit, high contrast. This format lives \
or dies on the footage; a dull clip is worse than a slightly less literal one,
- CLEAN: absolutely NO watermark, logo, stock-agency mark, channel name, URL, \
social handle, or overlaid text of any kind,
- full-frame and sharp, not a soft or tiny source,
- suggestive of MOTION (an animal moving, water breaking, something erupting). A \
static locked-off shot reads as a still photo and kills the format.

Reject (best: -1) ONLY if every candidate is watermarked, has overlaid text, is \
plainly the WRONG subject, or is too low-quality to fill a phone screen. A \
rejection drops this entry to a generic backdrop, so it is a last resort.

Also decide framing for the winner in a tall 9:16 phone frame:
- "cover" = the shot already fills a vertical frame, or the subject is central \
enough that cropping the sides loses nothing. This is usually right.
- "fit"   = the important action spans the full WIDTH of a wide shot, so cropping \
to full-bleed would cut it in half — fit it to the width instead.

Return ONLY JSON, with `reason` at most 15 words:
{{"best": <index or -1>, "framing": "cover"|"fit", "reason": "..."}}"""


def _judge(name: str, cand: list[dict], context: str = "") -> tuple[int, str]:
    """Claude-vision pick: (best_index or -1, framing).

    Any failure returns (0, 'cover') so the pipeline degrades to the first
    candidate rather than breaking the day's video."""
    load_dotenv()
    if not os.getenv("ANTHROPIC_API_KEY"):
        return 0, "cover"
    try:
        import anthropic
    except ImportError:
        return 0, "cover"
    ctx = f"\nThe on-screen caption for this clip reads: \"{context}\"" if context else ""
    content = [{"type": "text",
                "text": _JUDGE_PROMPT.format(name=name, context=ctx)}]
    for i, c in enumerate(cand):
        content.append({"type": "text", "text":
                        f"Candidate {i} (source: {c['src']}, {c['w']}x{c['h']}px, "
                        f"{c['dur']:.0f}s):"})
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": "image/jpeg", "data": _b64(c["frame"])}})
    try:
        resp = anthropic.Anthropic().messages.create(
            model=JUDGE_MODEL, max_tokens=800,
            messages=[{"role": "user", "content": content}])
        txt = "".join(b.text for b in resp.content if b.type == "text")
        m = re.search(r"\{.*\}", txt, re.S)
        if not m:
            log.warning("[clip-judge] no JSON for %r; using first candidate.", name)
            return 0, "cover"
        data = json.loads(m.group(0))
    except Exception as e:
        log.warning("[clip-judge] failed (%s); using first candidate.", e)
        return 0, "cover"
    best = data.get("best", 0)
    framing = "fit" if data.get("framing") == "fit" else "cover"
    log.info("[clip-judge] %s -> best=%s framing=%s (%s)", name, best, framing,
             str(data.get("reason", ""))[:80])
    if best is None or not isinstance(best, int) or best < 0 or best >= len(cand):
        return -1, framing
    return best, framing


# --- public API ---------------------------------------------------------------

def _queries(query) -> list[str]:
    if isinstance(query, str):
        return [query]
    return [q for q in (query or []) if isinstance(q, str) and q.strip()]


def fetch_item_clip(query, prefer: str | None = None,
                    exclude: set[str] | None = None,
                    context: str = "") -> tuple[Path | None, str]:
    """Best CLEAN stock clip for one ranked item + its framing hint.

    `query`   — one search term or the dataset's list of them (best first).
    `prefer`  — the item's display name; the judge's subject, and the last-resort
                search term.
    `exclude` — clip ids AND content hashes already used in THIS video, so two
                ranks never show the same footage (a backdrop that doesn't change
                while the ranking moves on reads as broken).
    `context` — the on-screen caption, so the judge knows what the clip must show.

    Candidates are judged on their PREVIEW FRAME and only the winner is
    downloaded. Judging on downloaded files instead meant ~30 clips (500MB) per
    video; this pulls five.

    Returns (path, framing), or (None, 'cover') when nothing usable was found.
    """
    exclude = set(exclude or ())
    terms = _queries(query)
    if not terms:
        return None, "cover"

    # A dataset's terms describe the IDEAL shot ("great white breaching clean out
    # of the water") and stock libraries often simply do not have it. Falling back
    # to the bare subject gets real footage of the right animal, which beats
    # dropping the entry to an unrelated backdrop by a wide margin.
    if prefer and prefer.lower() not in {t.lower() for t in terms}:
        terms = terms + [prefer]

    def gather() -> list[dict]:
        cands: list[dict] = []
        seen: set[str] = set()
        for term in terms:
            if len(cands) >= CANDIDATES:
                break
            for c in (_search_pexels(term, CANDIDATES) or
                      _search_pixabay(term, CANDIDATES)):
                if len(cands) >= CANDIDATES:
                    break
                if c["id"] in seen or c["id"] in exclude:
                    continue
                seen.add(c["id"])
                poster = _download_poster(c)
                if not poster:
                    continue
                cands.append({**c, "frame": poster})
        return cands

    cands = gather()
    if not cands:
        log.warning("[clips] no usable candidate for %r (terms: %s)",
                    prefer or terms[0], "; ".join(terms))
        return None, "cover"

    idx, framing = _judge(prefer or terms[0], cands, context=context)
    if idx < 0:
        log.warning("[clips] judge rejected every candidate for %r.",
                    prefer or terms[0])
        return None, framing

    # Download the winner; if its content collides with footage another rank is
    # already using (the same stock clip republished under two ids), step down
    # the list rather than repeating a shot.
    for c in [cands[idx]] + [c for i, c in enumerate(cands) if i != idx]:
        path = _download(c)
        if not path:
            continue
        if clip_hash(path) in exclude:
            log.info("[clips] %s duplicates footage already used; trying another.",
                     c["id"])
            continue
        prune_cache()
        return path, framing

    log.warning("[clips] every candidate for %r failed to download.",
                prefer or terms[0])
    return None, framing


if __name__ == "__main__":
    terms = sys.argv[1:] or ["great white shark breaching"]
    path, framing = fetch_item_clip(terms, prefer=terms[0])
    if path:
        print(f"OK  {path}  ({probe_duration(path):.1f}s, framing={framing})")
    else:
        print("no usable clip found")
