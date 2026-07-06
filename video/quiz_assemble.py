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
BG = (16, 18, 26)
WHITE = (238, 241, 248)
MUTED = (120, 126, 142)
ACCENT = (255, 205, 60)
GREEN = (86, 204, 128)

# Frame durations (seconds).
INTRO_SEC = 1.3
COUNT_SEC = 0.7        # per countdown number (3, 2, 1)
REVEAL_SEC = 1.7
OUTRO_SEC = 2.2


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_PATH), size)


def _center(d: ImageDraw.ImageDraw, y: int, text: str, font, fill) -> None:
    w = d.textbbox((0, 0), text, font=font)[2]
    d.text(((W - w) / 2, y), text, font=font, fill=fill)


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
    if row.width > W - 120:                      # scale down wide multi-emoji rows
        scale = (W - 120) / row.width
        row = row.resize((int(row.width * scale), int(row.height * scale)),
                         Image.LANCZOS)
    img.paste(row, ((W - row.width) // 2, cy - row.height // 2), row)


def _intro_frame(prompt: str, total: int, handle: str) -> Image.Image:
    img, d = _base(handle)
    _center(d, 720, prompt, _font(84), WHITE)
    _center(d, 840, "by emoji", _font(64), ACCENT)
    _center(d, 1040, f"{total} rounds — how many can you get?", _font(46), MUTED)
    return img


def _question_frame(prompt, emoji_str, count, idx, total, handle) -> Image.Image:
    img, d = _base(handle)
    _center(d, 210, f"{idx}/{total}", _font(54), MUTED)
    _center(d, 300, prompt, _font(66), WHITE)
    _paste_emoji(img, emoji_str, 880, 440)
    cx, cy, r = W // 2, 1360, 120
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=ACCENT, width=10)
    _center(d, cy - 92, str(count), _font(140), ACCENT)
    return img


def _reveal_frame(prompt, emoji_str, answer, clue, idx, total, handle) -> Image.Image:
    img, d = _base(handle)
    _center(d, 210, f"{idx}/{total}", _font(54), MUTED)
    _center(d, 300, prompt, _font(66), WHITE)
    _paste_emoji(img, emoji_str, 820, 360)
    _center(d, 1180, answer.upper(), _font(96), GREEN)
    if clue:
        _center(d, 1320, clue, _font(50), MUTED)
    return img


def _outro_frame(handle: str) -> Image.Image:
    img, d = _base(handle)
    _center(d, 780, "How many did", _font(80), WHITE)
    _center(d, 880, "you get?", _font(80), WHITE)
    _center(d, 1050, "Comment your score 👇", _font(56), ACCENT)
    _center(d, 1180, "Follow for daily quizzes", _font(48), MUTED)
    return img


def _ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"                            # rely on PATH (e.g. Railway)


def _synth_audio(plan: list[tuple[float, str]], path: Path, sr: int = 44100) -> None:
    """Synthesize a click-track wav timed to the frame plan (list of (dur, kind)).

    A short tick on every countdown second and a two-note ding on each reveal —
    the quiz-show feel — generated from sine tones so it's fully rights-clean.
    (Background music is left to a bundled/user-provided bed; see config.)
    """
    total = sum(d for d, _ in plan)
    n = int(total * sr)
    buf = [0.0] * n

    def tone(t0: float, freq: float, dur: float, amp: float) -> None:
        start = int(t0 * sr)
        for k in range(int(dur * sr)):
            i = start + k
            if 0 <= i < n:
                env = math.exp(-(k / sr) * 9.0)     # fast percussive decay
                buf[i] += amp * env * math.sin(2 * math.pi * freq * (k / sr))

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

        audio_path = tmpd / "audio.wav"
        _synth_audio([(dur, kind) for _, dur, kind in frames], audio_path)

        out_path = OUTPUT_DIR / f"{post_id}.mp4"
        cmd = [
            _ffmpeg_exe(), "-y",
            "-f", "concat", "-safe", "0", "-i", str(listfile),
            "-i", str(audio_path),
            "-vf", f"fps={FPS},format=yuv420p",
            "-c:v", "libx264", "-preset", "veryfast",
            "-c:a", "aac", "-b:a", "128k", "-shortest",
            "-movflags", "+faststart", str(out_path),
        ]
        log.info("[quiz] rendering %d frames (+audio) -> %s", len(frames), out_path.name)
        subprocess.run(cmd, check=True, capture_output=True)
    return out_path


if __name__ == "__main__":
    # Standalone: render a sample round from the football dataset.
    ds = json.loads((PROJECT_ROOT / "sourcing" / "quiz_data" / "football.json")
                    .read_text(encoding="utf-8"))
    payload = {"prompt": ds["prompt"], "questions": ds["questions"][:6]}
    out = render_quiz_video("quiz_demo_football", payload)
    print("Rendered:", out)
