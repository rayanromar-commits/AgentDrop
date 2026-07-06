"""
Quiz video assembly — the emoji-riddle slide/timer renderer.

Takes a quiz round-set (parsed from a quiz_source story's JSON body) and renders
a vertical 1080x1920 Short: an intro, then per question a countdown (3-2-1) over
the emoji clue, then the answer reveal, then an outro CTA. Slides are composited
with Pillow (emoji via video/emoji_assets.py) and stitched into an mp4 with the
bundled ffmpeg (imageio-ffmpeg). No TTS — this replaces the narration path.

Standalone test (renders a sample from the football dataset):
    python3 -m video.quiz_assemble
"""

import json
import math
import struct
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image, ImageDraw, ImageFont

from agentdrop_common import setup_logging
from video.emoji_assets import render_emoji_row

log = setup_logging()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FONT_PATH = PROJECT_ROOT / "video" / "fonts" / "DejaVuSans-Bold.ttf"
OUTPUT_DIR = PROJECT_ROOT / "output"

W, H = 1080, 1920
FPS = 30
# Bright, vibrant palette (light background, bold ink, punchy accents).
BG = (248, 249, 252)       # near-white background
INK = (18, 22, 38)         # primary dark text
MUTED = (110, 116, 132)    # secondary/labels
ACCENT = (37, 99, 235)     # vibrant blue (countdown, CTA)
GREEN = (5, 150, 105)      # vibrant green (answer reveal)

# Frame durations (seconds).
INTRO_SEC = 1.3
COUNT_SEC = 1.0        # per countdown number (3, 2, 1) -> a true 3-second timer
REVEAL_SEC = 1.7
OUTRO_SEC = 2.2
SIDE_MARGIN = 90       # min clear space each side so text never touches the edge


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_PATH), size)


def _center(d: ImageDraw.ImageDraw, y: int, text: str, font, fill) -> None:
    w = d.textbbox((0, 0), text, font=font)[2]
    d.text(((W - w) / 2, y), text, font=font, fill=fill)


def _fit_font(d: ImageDraw.ImageDraw, text: str, max_w: int, size: int,
              min_size: int = 30) -> ImageFont.FreeTypeFont:
    """Largest font <= `size` whose `text` fits in `max_w` (so long prompts /
    player names never clip, whatever the dataset)."""
    while size > min_size:
        f = _font(size)
        if d.textbbox((0, 0), text, font=f)[2] <= max_w:
            return f
        size -= 4
    return _font(min_size)


def _center_fit(d: ImageDraw.ImageDraw, y: int, text: str, size: int, fill,
                max_w: int = W - 2 * SIDE_MARGIN) -> None:
    _center(d, y, text, _fit_font(d, text, max_w, size), fill)


def _base(handle: str) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    _center(d, H - 150, handle, _font(46), MUTED)
    return img, d


def _paste_emoji(img: Image.Image, emoji_str: str, cy: int, box: int) -> None:
    """Composite the clue's emoji row centered on cy, capped to a box height/width."""
    row = render_emoji_row(emoji_str, box)
    if row is None:
        return
    max_w = W - 2 * SIDE_MARGIN
    if row.width > max_w:                         # scale down wide multi-emoji rows
        scale = max_w / row.width
        row = row.resize((int(row.width * scale), int(row.height * scale)),
                         Image.LANCZOS)
    img.paste(row, ((W - row.width) // 2, cy - row.height // 2), row)


def _intro_frame(prompt: str, total: int, handle: str) -> Image.Image:
    img, d = _base(handle)
    _center_fit(d, 720, prompt, 84, INK)
    _center(d, 840, "by emoji", _font(64), ACCENT)
    _center_fit(d, 1040, f"{total} rounds — how many can you get?", 46, MUTED)
    return img


def _question_frame(prompt, emoji_str, count, idx, total, handle) -> Image.Image:
    img, d = _base(handle)
    _center(d, 210, f"{idx}/{total}", _font(54), MUTED)
    _center_fit(d, 300, prompt, 66, INK)
    _paste_emoji(img, emoji_str, 880, 440)
    cx, cy, r = W // 2, 1360, 120
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=ACCENT, width=10)
    _center(d, cy - 92, str(count), _font(140), ACCENT)
    return img


def _reveal_frame(prompt, emoji_str, answer, clue, idx, total, handle) -> Image.Image:
    img, d = _base(handle)
    _center(d, 210, f"{idx}/{total}", _font(54), MUTED)
    _center_fit(d, 300, prompt, 66, INK)
    _paste_emoji(img, emoji_str, 820, 360)
    _center_fit(d, 1180, answer.upper(), 96, GREEN)
    if clue:
        _center_fit(d, 1320, clue, 50, MUTED)
    return img


def _outro_frame(handle: str) -> Image.Image:
    img, d = _base(handle)
    _center(d, 760, "How many did", _font(80), INK)
    _center(d, 860, "you get?", _font(80), INK)
    _center(d, 1040, "Comment your score", _font(56), ACCENT)
    cx, y = W // 2, 1150                            # down-chevron (no emoji glyph)
    d.polygon([(cx - 34, y), (cx + 34, y), (cx, y + 42)], fill=ACCENT)
    _center(d, 1250, "Follow for daily quizzes", _font(48), MUTED)
    return img


def _ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"                            # rely on PATH (e.g. Railway)


def _synth_audio(plan: list[tuple[float, str]], path: Path, sr: int = 44100,
                 bed: bool = True) -> None:
    """Synthesize a wav timed to the frame plan (list of (dur, kind)).

    A tick on every countdown second and a two-note ding on each reveal, over a
    soft looping "trivia" arpeggio bed — the quiz-show feel — all generated from
    sine tones so it's fully rights-clean (no Content-ID risk). Set bed=False
    when an external music track is mixed in instead (see config quiz.music).
    """
    total = sum(d for d, _ in plan)
    n = int(total * sr)
    buf = [0.0] * n

    def tone(t0: float, freq: float, dur: float, amp: float,
             decay: float = 9.0) -> None:
        start = int(t0 * sr)
        for k in range(int(dur * sr)):
            i = start + k
            if 0 <= i < n:
                env = math.exp(-(k / sr) * decay)
                buf[i] += amp * env * math.sin(2 * math.pi * freq * (k / sr))

    # Soft looping trivia bed (bright major arpeggio), low volume under the SFX.
    if bed:
        notes = [523.25, 659.25, 783.99, 659.25]    # C5 E5 G5 E5
        step, i, tt = 0.32, 0, 0.0
        while tt < total:
            tone(tt, notes[i % len(notes)], step * 0.95, 0.07, decay=4.5)
            i += 1
            tt += step

    t = 0.0
    for dur, kind in plan:
        if kind == "count":
            tone(t, 1180, 0.09, 0.55)               # crisp tick
        elif kind == "reveal":
            tone(t, 660, 0.14, 0.42)                # ding (two notes)
            tone(t + 0.13, 990, 0.24, 0.42)
        t += dur

    with wave.open(str(path), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b"".join(
            struct.pack("<h", int(max(-1.0, min(1.0, s)) * 32767)) for s in buf))


def render_quiz_video(post_id: str, payload: dict, config: dict | None = None,
                      handle: str = "@FootyEmoji") -> Path:
    """Render one quiz Short from a payload {prompt, questions:[{emoji,answer,clue}]}."""
    prompt = payload.get("prompt", "GUESS THE ANSWER")
    questions = payload["questions"]
    total = len(questions)
    OUTPUT_DIR.mkdir(exist_ok=True)

    # Each frame: (image, duration_sec, kind) — kind drives the audio track.
    frames: list[tuple[Image.Image, float, str]] = [
        (_intro_frame(prompt, total, handle), INTRO_SEC, "intro")]
    for i, q in enumerate(questions, 1):
        emoji_str, answer, clue = q["emoji"], q["answer"], q.get("clue", "")
        for c in (3, 2, 1):
            frames.append((_question_frame(prompt, emoji_str, c, i, total, handle),
                           COUNT_SEC, "count"))
        frames.append((_reveal_frame(prompt, emoji_str, answer, clue, i, total, handle),
                       REVEAL_SEC, "reveal"))
    frames.append((_outro_frame(handle), OUTRO_SEC, "outro"))

    with tempfile.TemporaryDirectory() as tmp:
        tmpd = Path(tmp)
        concat_lines = []
        for n, (frame, dur, _kind) in enumerate(frames):
            fp = tmpd / f"f{n:04d}.png"
            frame.save(fp)
            concat_lines.append(f"file '{fp}'")
            concat_lines.append(f"duration {dur}")
        concat_lines.append(f"file '{tmpd / f'f{len(frames)-1:04d}.png'}'")  # last held
        listfile = tmpd / "frames.txt"
        listfile.write_text("\n".join(concat_lines), encoding="utf-8")

        # Optional real music track (config quiz.music, relative to repo root).
        music_rel = (config or {}).get("quiz", {}).get("music")
        music_path = (PROJECT_ROOT / music_rel) if music_rel else None
        use_ext = bool(music_path and music_path.exists())

        audio_path = tmpd / "audio.wav"
        _synth_audio([(dur, kind) for _, dur, kind in frames], audio_path,
                     bed=not use_ext)

        out_path = OUTPUT_DIR / f"{post_id}.mp4"
        ff = _ffmpeg_exe()
        if use_ext:
            # Mix the looped external track (quiet) under the tick/ding SFX.
            cmd = [
                ff, "-y",
                "-f", "concat", "-safe", "0", "-i", str(listfile),
                "-stream_loop", "-1", "-i", str(music_path),
                "-i", str(audio_path),
                "-filter_complex",
                f"[0:v]fps={FPS},format=yuv420p[v];[1:a]volume=0.22[m];"
                "[m][2:a]amix=inputs=2:duration=first:normalize=0[a]",
                "-map", "[v]", "-map", "[a]",
                "-c:v", "libx264", "-preset", "veryfast",
                "-c:a", "aac", "-b:a", "128k", "-shortest",
                "-movflags", "+faststart", str(out_path),
            ]
        else:
            cmd = [
                ff, "-y",
                "-f", "concat", "-safe", "0", "-i", str(listfile),
                "-i", str(audio_path),
                "-vf", f"fps={FPS},format=yuv420p",
                "-c:v", "libx264", "-preset", "veryfast",
                "-c:a", "aac", "-b:a", "128k", "-shortest",
                "-movflags", "+faststart", str(out_path),
            ]
        log.info("[quiz] rendering %d frames (+audio%s) -> %s",
                 len(frames), " +music" if use_ext else "", out_path.name)
        subprocess.run(cmd, check=True, capture_output=True)
    return out_path


if __name__ == "__main__":
    # Standalone: render a sample round from the football dataset.
    ds = json.loads((PROJECT_ROOT / "sourcing" / "quiz_data" / "football.json")
                    .read_text(encoding="utf-8"))
    payload = {"prompt": ds["prompt"], "questions": ds["questions"][:6]}
    out = render_quiz_video("quiz_demo_football", payload)
    print("Rendered:", out)
