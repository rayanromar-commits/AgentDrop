"""
Cinematic ranking renderer — "Top 5 ___" space countdowns.

Turns a ranking payload {title, hook, items:[{rank,name,stat,query}]} into a
premium vertical Short: NASA images with Ken Burns motion, bold animated
captions, crossfade transitions, an intro hook and an outro CTA. (Audio —
majestic music bed + whoosh/impact SFX + TTS voiceover — is layered in a second
pass; this module produces the moving picture.)

Standalone test (fetches NASA images + renders the dangerous-planets demo):
    python3 -m video.ranking_assemble
"""

import json
import math
import random
import struct
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image, ImageDraw, ImageFont

from agentdrop_common import setup_logging
from media.clip_source import fetch_image

log = setup_logging()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FONT = PROJECT_ROOT / "video" / "fonts" / "DejaVuSans-Bold.ttf"
OUTPUT_DIR = PROJECT_ROOT / "output"

W, H = 1080, 1920
FPS = 30
RANK_SEC = 6.0                    # per rank — lingering, cinematic (30-40s total)
INTRO_SEC = 5.5                   # fits the voiceover intro
OUTRO_SEC = 3.0
XFADE = 0.5                       # crossfade duration between segments

WHITE = (245, 247, 255)
MUTED = (176, 184, 204)
ACCENT = (120, 200, 255)          # cool cosmic blue
GOLD = (255, 209, 102)            # #1 highlight


def _font(sz):
    return ImageFont.truetype(str(FONT), sz)


def _fit(d, text, max_w, size, min_size=34):
    while size > min_size:
        f = _font(size)
        if d.textbbox((0, 0), text, font=f)[2] <= max_w:
            return f
        size -= 4
    return _font(min_size)


def _center(d, y, text, font, fill):
    w = d.textbbox((0, 0), text, font=font)[2]
    d.text(((W - w) / 2, y), text, font=font, fill=fill)


def _scrim(img, top_frac, strength=225):
    """Darken the lower part of a frame (transparent -> dark) for text legibility."""
    grad = Image.new("L", (1, H), 0)
    y0 = int(H * top_frac)
    for y in range(y0, H):
        grad.putpixel((0, y), int(strength * (y - y0) / (H - y0)))
    alpha = grad.resize((W, H))
    black = Image.new("RGBA", (W, H), (5, 7, 14, 255))
    black.putalpha(alpha)
    img.alpha_composite(black)


def _rank_caption(item, total) -> Image.Image:
    """Full-frame transparent overlay: rank badge + name + stat, bottom third."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    _scrim(img, 0.52)
    d = ImageDraw.Draw(img)
    is_one = item["rank"] == 1
    accent = GOLD if is_one else ACCENT
    _center(d, 1150, f"#{item['rank']}", _font(150), accent)
    _center(d, 1330, item["name"].upper(),
            _fit(d, item["name"].upper(), W - 140, 104), WHITE)
    _center(d, 1470, item["stat"], _fit(d, item["stat"], W - 130, 52), MUTED)
    return img


def _intro_caption(title, hook) -> Image.Image:
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    _scrim(img, 0.0, strength=150)          # gentle full darken so title pops
    d = ImageDraw.Draw(img)
    # Split "Top 5 ..." onto two lines around the number for punch.
    _center(d, 760, title.upper(),
            _fit(d, title.upper(), W - 120, 96), WHITE)
    if hook:
        _center(d, 1040, hook, _fit(d, hook, W - 140, 52), ACCENT)
    return img


def _outro_caption() -> Image.Image:
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    _scrim(img, 0.0, strength=170)
    d = ImageDraw.Draw(img)
    _center(d, 820, "Which one shocked you?", _font(70), WHITE)
    _center(d, 960, "Follow for more 🚀", _font(58), ACCENT)
    return img


def _ffmpeg():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def _kenburns_segment(image_path, caption_png, dur, out_path, zoom_in=True):
    """Render one segment: Ken Burns on the image + a fading-in caption overlay."""
    frames = int(dur * FPS)
    # Pre-scale to 2x cover so the zoom stays crisp, then zoompan to frame size.
    if zoom_in:
        z = "min(zoom+0.0012,1.14)"
    else:                                   # start zoomed, ease out
        z = "if(eq(on,0),1.14,max(zoom-0.0012,1.0))"
    vf = (
        f"[0:v]scale=2160:3840:force_original_aspect_ratio=increase,"
        f"crop=2160:3840,zoompan=z='{z}':d={frames}:"
        f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}:fps={FPS}[bg];"
        f"[1:v]format=rgba,fade=in:st=0:d=0.5:alpha=1[cap];"
        f"[bg][cap]overlay=0:0,format=yuv420p[v]"
    )
    cmd = [
        _ffmpeg(), "-y",
        "-loop", "1", "-t", str(dur), "-i", str(image_path),
        "-loop", "1", "-t", str(dur), "-i", str(caption_png),
        "-filter_complex", vf, "-map", "[v]",
        "-c:v", "libx264", "-preset", "veryfast", "-t", str(dur), str(out_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def _synth_ranking_audio(total, reveal_times, transition_times, path, sr=44100):
    """A cinematic space bed (low drone chord + shimmer) with a whoosh on each
    transition and a low impact/boom on each rank reveal — synth so it's
    rights-clean. A real majestic track can replace the bed via ranking.music."""
    n = int(total * sr)
    buf = [0.0] * n
    chord = [(55.0, 0.05), (82.4, 0.045), (110.0, 0.04), (164.8, 0.028)]  # A1/E2/A2/E3
    for k in range(n):
        env = min(1.0, k / (sr * 2.5)) * min(1.0, (n - k) / (sr * 1.5))    # swell in/out
        s = sum(a * math.sin(2 * math.pi * f * (k / sr)) for f, a in chord)
        s += 0.012 * math.sin(2 * math.pi * 880 * (k / sr)) * (
            0.5 + 0.5 * math.sin(2 * math.pi * 0.2 * (k / sr)))            # shimmer
        buf[k] += s

    def impact(t0):
        st = int(t0 * sr)
        for k in range(int(0.45 * sr)):
            i = st + k
            if 0 <= i < n:
                e = math.exp(-(k / sr) * 11)
                buf[i] += 0.6 * e * math.sin(2 * math.pi * 70 * (k / sr)) \
                    + 0.12 * e * (random.random() * 2 - 1)

    def whoosh(t0):
        st = int((t0 - 0.28) * sr)
        for k in range(int(0.55 * sr)):
            i = st + k
            if 0 <= i < n:
                buf[i] += 0.15 * math.sin(math.pi * k / (0.55 * sr)) \
                    * (random.random() * 2 - 1)

    for t in transition_times:
        whoosh(t)
    for t in reveal_times:
        impact(t)
    with wave.open(str(path), "w") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(b"".join(
            struct.pack("<h", int(max(-1.0, min(1.0, s)) * 32767)) for s in buf))


def _xfade_concat(segments, durs, out_path):
    """Crossfade-chain the segment clips into one video."""
    inputs = []
    for s in segments:
        inputs += ["-i", str(s)]
    # Build the xfade chain with cumulative offsets.
    filt, prev, running = [], "[0:v]", durs[0]
    for i in range(1, len(segments)):
        off = round(running - XFADE, 3)
        lbl = f"[x{i}]"
        filt.append(f"{prev}[{i}:v]xfade=transition=fade:duration={XFADE}:"
                    f"offset={off}{lbl}")
        prev = lbl
        running += durs[i] - XFADE
    cmd = [_ffmpeg(), "-y", *inputs,
           "-filter_complex", ";".join(filt), "-map", prev,
           "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
           "-movflags", "+faststart", str(out_path)]
    subprocess.run(cmd, check=True, capture_output=True)


def render_ranking_video(post_id, payload, config=None) -> Path:
    """Render the cinematic (silent) countdown video. Audio added in a 2nd pass."""
    title = payload["title"]
    hook = payload.get("hook", "")
    items = sorted(payload["items"], key=lambda x: -x["rank"])   # #5 -> #1
    OUTPUT_DIR.mkdir(exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        tmpd = Path(tmp)
        segs, durs = [], []

        # Intro over a nebula.
        nebula = (fetch_image("colorful nebula", prefer="nebula")
                  or fetch_image(items[-1]["query"], prefer=items[-1]["name"]))
        cap = tmpd / "cap_intro.png"; _intro_caption(title, hook).save(cap)
        seg = tmpd / "seg_intro.mp4"
        _kenburns_segment(nebula, cap, INTRO_SEC, seg, zoom_in=True)
        segs.append(seg); durs.append(INTRO_SEC)

        # Ranks #5 -> #1.
        for i, it in enumerate(items):
            img = fetch_image(it["query"], prefer=it["name"]) or nebula
            cap = tmpd / f"cap_{i}.png"; _rank_caption(it, len(items)).save(cap)
            seg = tmpd / f"seg_{i}.mp4"
            _kenburns_segment(img, cap, RANK_SEC, seg, zoom_in=(i % 2 == 0))
            segs.append(seg); durs.append(RANK_SEC)

        # Outro.
        cap = tmpd / "cap_outro.png"; _outro_caption().save(cap)
        seg = tmpd / "seg_outro.mp4"
        _kenburns_segment(nebula, cap, OUTRO_SEC, seg, zoom_in=False)
        segs.append(seg); durs.append(OUTRO_SEC)

        silent = tmpd / "silent.mp4"
        log.info("[ranking] crossfading %d segments...", len(segs))
        _xfade_concat(segs, durs, silent)

        # Segment timeline (matches _xfade_concat's cumulative offsets).
        offsets, run = [], durs[0]
        for i in range(1, len(durs)):
            offsets.append(round(run - XFADE, 3))
            run += durs[i] - XFADE
        total = run
        reveal_times = offsets[:len(items)]      # transition INTO each rank
        transition_times = offsets               # whoosh on every cut

        bed = tmpd / "bed.wav"
        _synth_ranking_audio(total, reveal_times, transition_times, bed)

        # Realistic voiceover intro (title + hook), unless disabled.
        vo_path = None
        if (config or {}).get("ranking", {}).get("voiceover", "intro") != "off":
            try:
                from voiceover.tts import synthesize
                narrator = {"id": "pNInz6obpgDQGcFmaJgB", "name": "Adam"}  # deep
                res = synthesize(f"{title}. {hook}", f"{post_id}_vo",
                                 config or {}, voice=narrator)
                vo_path = res["audio"]
            except Exception as e:
                log.warning("[ranking] voiceover failed (%s); music+SFX only.", e)

        out_path = OUTPUT_DIR / f"{post_id}.mp4"
        ff = _ffmpeg()
        if vo_path:
            cmd = [ff, "-y", "-i", str(silent), "-i", str(bed), "-i", str(vo_path),
                   "-filter_complex",
                   "[1:a]volume=1.0[b];[2:a]adelay=250|250,volume=1.8[v];"
                   "[b][v]amix=inputs=2:duration=first:normalize=0[a]",
                   "-map", "0:v", "-map", "[a]",
                   "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-shortest",
                   str(out_path)]
        else:
            cmd = [ff, "-y", "-i", str(silent), "-i", str(bed),
                   "-map", "0:v", "-map", "1:a",
                   "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-shortest",
                   str(out_path)]
        log.info("[ranking] mixing audio -> %s", out_path.name)
        subprocess.run(cmd, check=True, capture_output=True)
    return out_path


if __name__ == "__main__":
    from agentdrop_common import load_config
    ds = json.loads((PROJECT_ROOT / "sourcing" / "ranking_data"
                     / "dangerous_planets.json").read_text(encoding="utf-8"))
    print("Rendered:", render_ranking_video("rank_demo_planets", ds, load_config()))
