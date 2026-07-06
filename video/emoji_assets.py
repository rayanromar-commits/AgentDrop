"""
Emoji → crisp image assets for the quiz renderer.

Splits an emoji clue string (e.g. "🐐🇦🇷🚀") into individual emoji, loads each
as a high-res OpenMoji PNG (618x618, CC-BY-SA 4.0), and composites them into a
single horizontal row image. Handles multi-codepoint emoji (flags, ZWJ / tag
sequences) that Pillow's font path can't render.

Assets are cached under video/emoji_assets/ and fetched on first use. For cloud
runs with no network, pre-fetch the active dataset's emoji with
`python3 -m video.emoji_assets prefetch sourcing/quiz_data/football.json` and
commit the cache dir.

OpenMoji attribution: emoji graphics by https://openmoji.org (CC-BY-SA 4.0).
"""

import ssl
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image
import emoji as emoji_lib

from agentdrop_common import setup_logging

log = setup_logging()

# Python 3.14's urllib has no default CA bundle on macOS, so HTTPS verify fails.
# Use certifi's bundle when available; fall back to unverified (we're only
# fetching public CC-licensed emoji PNGs, never user data).
try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:  # certifi missing
    _SSL_CTX = ssl._create_unverified_context()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = PROJECT_ROOT / "video" / "emoji_assets"
OPENMOJI_URL = ("https://raw.githubusercontent.com/hfg-gmuend/openmoji/"
                "master/color/618x618/{}")


def split_emoji(text: str) -> list[str]:
    """Break a string into its individual emoji (order preserved)."""
    return [d["emoji"] for d in emoji_lib.emoji_list(text)]


def _filename(e: str) -> str:
    """OpenMoji filename for one emoji: uppercase hex codepoints joined by '-',
    dropping the FE0F variation selector (OpenMoji's naming convention)."""
    cps = [ord(c) for c in e if ord(c) != 0xFE0F]
    return "-".join(f"{cp:04X}" for cp in cps) + ".png"


def load_emoji_image(e: str) -> Image.Image | None:
    """Return an RGBA image for one emoji (cache-or-fetch), or None on failure."""
    fn = _filename(e)
    path = CACHE_DIR / fn
    if not path.exists():
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            req = urllib.request.Request(OPENMOJI_URL.format(fn),
                                         headers={"User-Agent": "AgentDrop"})
            with urllib.request.urlopen(req, context=_SSL_CTX, timeout=20) as r:
                data = r.read()
            path.write_bytes(data)
        except Exception as ex:
            log.warning("[emoji] fetch failed for %s (%s): %s", e, fn, ex)
            return None
    try:
        return Image.open(path).convert("RGBA")
    except Exception as ex:
        log.warning("[emoji] open failed for %s: %s", fn, ex)
        return None


def render_emoji_row(text: str, height: int, gap: int | None = None
                     ) -> Image.Image | None:
    """Composite a clue's emoji into one horizontal RGBA strip `height` px tall."""
    imgs = [im for im in (load_emoji_image(e) for e in split_emoji(text))
            if im is not None]
    if not imgs:
        return None
    imgs = [im.resize((height, height), Image.LANCZOS) for im in imgs]
    gap = gap if gap is not None else int(height * 0.10)
    total_w = sum(im.width for im in imgs) + gap * (len(imgs) - 1)
    row = Image.new("RGBA", (total_w, height), (0, 0, 0, 0))
    x = 0
    for im in imgs:
        row.alpha_composite(im, (x, 0))
        x += im.width + gap
    return row


def prefetch_dataset(dataset_path: str) -> int:
    """Fetch + cache every emoji used in a quiz dataset (for offline/cloud)."""
    import json
    data = json.loads(Path(dataset_path).read_text(encoding="utf-8"))
    seen, n = set(), 0
    for q in data.get("questions", []):
        for e in split_emoji(q.get("emoji", "")):
            if e in seen:
                continue
            seen.add(e)
            if load_emoji_image(e) is not None:
                n += 1
    log.info("[emoji] prefetched %d/%d unique emoji from %s into %s",
             n, len(seen), Path(dataset_path).name, CACHE_DIR)
    return n


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "prefetch":
        prefetch_dataset(sys.argv[2])
    else:
        print("usage: python3 -m video.emoji_assets prefetch <dataset.json>")
