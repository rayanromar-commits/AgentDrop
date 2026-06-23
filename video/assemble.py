"""
Step 6 — video assembly (ffmpeg).

Takes a background clip + the voiceover MP3 + the word-timing JSON and
produces a vertical 1080x1920 MP4 with word-synced captions burned in.

Pipeline:
  background clip  --crop/scale-->  1080x1920, looped to cover narration
  + voiceover audio
  + captions (.ass subtitles built from the word timings)
  =>  video/output/<id>.mp4

We locate ffmpeg from the bundled imageio-ffmpeg package, so no system
install is needed.

Test it:  python3 video/assemble.py
"""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdrop_common import load_config, setup_logging
from video.footage_library import pick_clip

log = setup_logging()

OUTPUT_DIR = Path(__file__).resolve().parent / "output"
VOICE_DIR = Path(__file__).resolve().parent.parent / "voiceover" / "output"


def _ffmpeg_exe() -> str:
    """Find an ffmpeg binary (bundled one preferred)."""
    import shutil
    system = shutil.which("ffmpeg")
    if system:
        return system
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _fmt_time(seconds: float) -> str:
    """Format seconds as ASS time  H:MM:SS.cs"""
    if seconds is None:
        seconds = 0.0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int(round((seconds - int(seconds)) * 100))
    if cs == 100:  # rounding edge
        cs = 99
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def build_captions_ass(words: list[dict], config: dict, out_path: Path) -> None:
    """Write an .ass subtitle file with word-synced, Shorts-style captions."""
    cap = config["captions"]
    vid = config["video"]
    n = max(1, int(cap.get("words_per_caption", 3)))

    # ASS alignment: 5 = middle-center, 2 = bottom-center.
    alignment = 5 if cap.get("position", "center") == "center" else 2

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {vid['width']}
PlayResY: {vid['height']}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{cap.get('font','Arial')},{cap.get('font_size',90)},&H00FFFFFF,&H00000000,&H00000000,1,0,1,{cap.get('outline_width',6)},0,{alignment},60,60,0,1

[Events]
Format: Layer, Start, End, Style, MarginL, MarginR, MarginV, Effect, Text
"""

    lines = [header]
    # Group words into small chunks shown together.
    for i in range(0, len(words), n):
        chunk = words[i:i + n]
        start = chunk[0]["start"]
        end = chunk[-1]["end"]
        text = " ".join(w["word"] for w in chunk)
        # Escape ASS-special characters.
        text = text.replace("\\", "\\\\").replace("{", "(").replace("}", ")")
        lines.append(
            f"Dialogue: 0,{_fmt_time(start)},{_fmt_time(end)},Caption,,0,0,0,,{text}"
        )

    out_path.write_text("\n".join(lines), encoding="utf-8")


def assemble_video(post_id: str, config: dict) -> Path:
    """Build the final MP4 for a story whose voiceover already exists."""
    OUTPUT_DIR.mkdir(exist_ok=True)

    audio_path = VOICE_DIR / f"{post_id}.mp3"
    words_path = VOICE_DIR / f"{post_id}.words.json"
    if not audio_path.exists() or not words_path.exists():
        raise FileNotFoundError(
            f"Missing voiceover for {post_id}. Run the voiceover step first."
        )

    words = json.loads(words_path.read_text(encoding="utf-8"))

    # Captions file written into the output dir; we run ffmpeg from there
    # so the subtitles filter can reference it by a simple filename
    # (avoids fussy path-escaping in the filtergraph).
    ass_path = OUTPUT_DIR / f"{post_id}.ass"
    build_captions_ass(words, config, ass_path)

    bg_clip = pick_clip()
    log.info("Using background clip: %s", bg_clip.name)

    vid = config["video"]
    w, h, fps = vid["width"], vid["height"], vid["fps"]
    out_path = OUTPUT_DIR / f"{post_id}.mp4"

    # scale to cover -> crop center -> set fps -> burn captions
    vf = (
        f"scale={w}:{h}:force_original_aspect_ratio=increase,"
        f"crop={w}:{h},setsar=1,fps={fps},"
        f"subtitles={ass_path.name}"
    )

    cmd = [
        _ffmpeg_exe(), "-y",
        "-stream_loop", "-1", "-i", str(bg_clip),  # loop bg as needed
        "-i", str(audio_path),                      # voiceover
        "-vf", vf,
        "-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",                                # stop at narration end
        out_path.name,
    ]

    log.info("Running ffmpeg to build the video (this can take a minute)...")
    result = subprocess.run(cmd, cwd=OUTPUT_DIR, capture_output=True, text=True)
    if result.returncode != 0:
        log.error("ffmpeg failed:\n%s", result.stderr[-1500:])
        raise RuntimeError("ffmpeg failed — see log above.")

    log.info("Done! Final video: %s", out_path)
    return out_path


if __name__ == "__main__":
    from sourcing.get_stories import fetch_stories
    from processing.screen import screen_story
    from database import db

    db.init_db()
    cfg = load_config()

    # Use the first passing story that already has a voiceover generated.
    stories = fetch_stories(cfg, skip_seen=False)
    chosen = None
    for s in stories:
        passed, _ = screen_story(s, cfg)
        if not passed:
            continue
        if (VOICE_DIR / f"{s['post_id']}.mp3").exists():
            chosen = s
            break

    if not chosen:
        log.error(
            "No story with an existing voiceover found. Run "
            "'python3 voiceover/tts.py' first to narrate one."
        )
        sys.exit(1)

    log.info("Assembling video for: %s", chosen["title"])
    final = assemble_video(chosen["post_id"], cfg)
    print(f"\nFinished video: {final}")
    print("Open it with:  open '%s'" % final)
