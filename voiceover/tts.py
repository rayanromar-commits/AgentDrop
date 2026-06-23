"""
Step 4 — AI voiceover (ElevenLabs).

Turns cleaned story text into:
  1. an MP3 audio file              -> voiceover/output/<id>.mp3
  2. word-level timing data (JSON)  -> voiceover/output/<id>.words.json

The timing JSON is a list of {"word", "start", "end"} (seconds) that
Step 6 uses to flash captions in sync with the narration.

We call ElevenLabs' "with-timestamps" endpoint, which returns the audio
plus per-CHARACTER timings; we group those characters into words here.

Test it (uses ~1 short's worth of your ElevenLabs quota):
    python3 voiceover/tts.py
"""

import base64
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests
from dotenv import load_dotenv

from agentdrop_common import DATA_DIR, load_config, setup_logging

log = setup_logging()

OUTPUT_DIR = DATA_DIR / "media" / "voiceover"
API_BASE = "https://api.elevenlabs.io/v1/text-to-speech"


def _get_api_key() -> str:
    load_dotenv()
    key = os.getenv("ELEVENLABS_API_KEY")
    if not key:
        raise RuntimeError(
            "Missing ELEVENLABS_API_KEY in .env. Add it, then retry."
        )
    return key


def _chars_to_words(chars: list[str], starts: list[float], ends: list[float]):
    """Group per-character timings into per-word timings."""
    words = []
    current = ""
    word_start = None
    word_end = None

    for ch, st, en in zip(chars, starts, ends):
        if ch.isspace():
            if current:
                words.append({"word": current, "start": word_start, "end": word_end})
                current = ""
                word_start = None
            continue
        if word_start is None:
            word_start = st
        current += ch
        word_end = en

    if current:
        words.append({"word": current, "start": word_start, "end": word_end})
    return words


def synthesize(text: str, post_id: str, config: dict) -> dict:
    """Generate audio + word timings for the given text. Returns paths."""
    OUTPUT_DIR.mkdir(exist_ok=True)
    vo = config["voiceover"]
    api_key = _get_api_key()

    url = f"{API_BASE}/{vo['voice_id']}/with-timestamps"
    headers = {"xi-api-key": api_key, "Content-Type": "application/json"}
    payload = {
        "text": text,
        "model_id": vo.get("model", "eleven_turbo_v2_5"),
        "voice_settings": {
            "stability": vo.get("stability", 0.5),
            "similarity_boost": vo.get("similarity_boost", 0.75),
        },
    }

    log.info("Requesting voiceover from ElevenLabs (%s)...", vo.get("voice_name"))
    resp = requests.post(url, headers=headers, json=payload, timeout=120)
    if resp.status_code != 200:
        raise RuntimeError(
            f"ElevenLabs error {resp.status_code}: {resp.text[:300]}"
        )

    data = resp.json()

    # Save the audio (returned as base64-encoded MP3).
    audio_path = OUTPUT_DIR / f"{post_id}.mp3"
    audio_path.write_bytes(base64.b64decode(data["audio_base64"]))

    # Build word timings from the character alignment.
    align = data.get("alignment") or {}
    words = _chars_to_words(
        align.get("characters", []),
        align.get("character_start_times_seconds", []),
        align.get("character_end_times_seconds", []),
    )
    words_path = OUTPUT_DIR / f"{post_id}.words.json"
    words_path.write_text(json.dumps(words, indent=2), encoding="utf-8")

    duration = words[-1]["end"] if words else 0
    log.info(
        "Saved audio (%.1fs, %d words) -> %s", duration, len(words), audio_path
    )
    return {"audio": audio_path, "words": words_path, "duration": duration}


if __name__ == "__main__":
    from sourcing.get_stories import fetch_stories
    from processing.screen import clean_text, screen_story
    from database import db

    db.init_db()
    cfg = load_config()
    stories = fetch_stories(cfg, skip_seen=False)

    # Find the first story that passes screening.
    chosen = None
    for s in stories:
        passed, _ = screen_story(s, cfg)
        if passed:
            chosen = s
            break

    if not chosen:
        log.error("No passing story found to narrate.")
        sys.exit(1)

    log.info("Narrating: %s", chosen["title"])
    text = clean_text(chosen["title"], chosen["body"])
    char_count = len(text)
    log.info("This will use about %d characters of your quota.", char_count)

    result = synthesize(text, chosen["post_id"], cfg)
    print(f"\nAudio : {result['audio']}")
    print(f"Words : {result['words']}")
    print(f"Length: {result['duration']:.1f} seconds")
    print("\nOpen the .mp3 to LISTEN before we continue.")
