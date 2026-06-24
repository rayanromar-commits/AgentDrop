"""
Step 5 — footage library.

Manages the background clips in the /footage folder. Each finished
video gets a randomly chosen clip (rotating backgrounds helps with
YouTube's "repetitive content" rule).

IMPORTANT: only put clips here that you have the right to use
commercially. See COPYRIGHT_NOTES.md for safe sources.

Test it (lists what you've got):  python3 video/footage_library.py
"""

import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdrop_common import setup_logging

log = setup_logging()

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _has_clips(d: Path) -> bool:
    return d.is_dir() and any(
        p.suffix.lower() in VIDEO_EXTENSIONS for p in d.iterdir()
    )


def footage_dir() -> Path:
    """Decide which folder to read background clips from.

    Priority:
      1. $FOOTAGE_DIR (e.g. a cloud volume) — if set AND it holds clips.
      2. ./footage_compressed — the small, render-ready clips committed to
         the repo (1080x1920, audio stripped). This is what the cloud and
         normal local runs use.
      3. ./footage — raw/original masters (often large 4K source files).
    Falls back to whichever exists with clips; defaults to footage_compressed.
    """
    env = os.getenv("FOOTAGE_DIR")
    compressed = PROJECT_ROOT / "footage_compressed"
    raw = PROJECT_ROOT / "footage"

    if env and _has_clips(Path(env)):
        return Path(env)
    if _has_clips(compressed):
        return compressed
    if _has_clips(raw):
        return raw
    # Nothing has clips yet — return the preferred location so the empty-
    # library error message points somewhere sensible.
    return Path(env) if env else compressed


# Kept for backward compatibility / logging. Resolved at import time.
FOOTAGE_DIR = footage_dir()


def list_clips() -> list[Path]:
    """Return all usable video files in the active footage folder."""
    d = footage_dir()
    d.mkdir(exist_ok=True)
    return sorted(
        p for p in d.iterdir()
        if p.suffix.lower() in VIDEO_EXTENSIONS
    )


def pick_clip(rng: random.Random | None = None) -> Path:
    """Pick a random clip. Raises if the library is empty."""
    clips = list_clips()
    if not clips:
        raise FileNotFoundError(
            f"No footage found in {footage_dir()}. Add at least one "
            "rights-cleared video file (see COPYRIGHT_NOTES.md)."
        )
    rng = rng or random
    return rng.choice(clips)


def next_clip() -> Path:
    """Return the next clip in round-robin order.

    Unlike a random pick, this cycles through EVERY file in the footage
    folder in turn, so all clips you add get used (no clip is skipped by
    chance). Just drop new .mp4s into the folder — they're picked up
    automatically. The counter persists across restarts (cloud-safe).
    """
    # Imported lazily so this module has no hard dependency on the DB
    # when used purely for listing.
    from database import db

    clips = list_clips()
    if not clips:
        raise FileNotFoundError(
            f"No footage found in {footage_dir()}. Add at least one "
            "rights-cleared video file (see COPYRIGHT_NOTES.md)."
        )
    idx = db.next_rotation_index("footage") % len(clips)
    return clips[idx]


if __name__ == "__main__":
    clips = list_clips()
    if not clips:
        log.warning(
            "Footage library is EMPTY (%s).\n"
            "Download rights-cleared clips and drop them in there. "
            "See COPYRIGHT_NOTES.md for safe sources.",
            FOOTAGE_DIR,
        )
    else:
        log.info("Found %d clip(s) in your footage library:", len(clips))
        for c in clips:
            size_mb = c.stat().st_size / (1024 * 1024)
            print(f"  - {c.name}  ({size_mb:.1f} MB)")
        print(f"\nRandom pick for next video: {pick_clip().name}")
