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

# In the cloud, point FOOTAGE_DIR at a persistent volume holding your
# clips (e.g. /data/footage). Locally it defaults to ./footage.
FOOTAGE_DIR = Path(os.getenv("FOOTAGE_DIR", Path(__file__).resolve().parent.parent / "footage"))
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}


def list_clips() -> list[Path]:
    """Return all usable video files in the footage folder."""
    FOOTAGE_DIR.mkdir(exist_ok=True)
    return sorted(
        p for p in FOOTAGE_DIR.iterdir()
        if p.suffix.lower() in VIDEO_EXTENSIONS
    )


def pick_clip(rng: random.Random | None = None) -> Path:
    """Pick a random clip. Raises if the library is empty."""
    clips = list_clips()
    if not clips:
        raise FileNotFoundError(
            f"No footage found in {FOOTAGE_DIR}. Add at least one "
            "rights-cleared video file (see COPYRIGHT_NOTES.md)."
        )
    rng = rng or random
    return rng.choice(clips)


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
