"""
Ranking renderer — full-bleed "Top 5" tier-list countdowns (space channel).

Layout (safe zones per the spec):
  * NASA image = full-bleed background, Ken Burns motion.
  * Title in the top ~240px band (pushed down to clear the iPhone Dynamic Island).
  * A COMPACT 1->5 tier list overlaid on the left; items reveal in RANDOM order
    and drop into their correct rank slot.
  * Bottom ~380px band = captions (the current narration line).
Full ElevenLabs voiceover reads/explains every item; a mood-matched music bed
(ominous / angelic / epic / mysterious, chosen from the title) sits underneath,
in a phone-audible register.

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
YELLOW = (255, 213, 0)
DIM = (180, 184, 200)
NARRATOR = {"id": "pNInz6obpgDQGcFmaJgB", "name": "Adam"}

# Safe-zone layout.
ML, MR = 60, 120                     # left / right margins
TITLE_CY = 205                       # title center (below the Dynamic Island)
LIST_X = 70
LIST_YS = [560, 690, 820, 950, 1080]  # compact 1..5 rows, upper-left
CAP_CY = 1660                        # caption band center (bottom ~380px)


def _font(sz):
    return ImageFont.truetype(str(FONT), sz)


def _text(d, xy, text, size, fill=YELLOW, anchor="lm", stroke=6):
    d.text(xy, text, font=_font(size), fill=fill, anchor=anchor,
           stroke_width=stroke, stroke_fill=(0, 0, 0))


def _fit(d, text, max_w, size, min_size=26):
    while size > min_size and d.textlength(text, font=_font(size)) > max_w:
        size -= 3
    return size


def _wrap(d, text, max_w, size):
    lines, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if d.textlength(t, font=_font(size)) <= max_w:
            cur = t
        else:
            lines.append(cur); cur = w
    if cur:
        lines.append(cur)
    return lines


def _scrim(img, top_h=300, bot_h=460):
    """Darken the top and bottom bands so title + captions stay legible."""
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    for y in range(top_h):
        d.line([(0, y), (W, y)], fill=(0, 0, 0, int(150 * (1 - y / top_h))))
    for y in range(H - bot_h, H):
        d.line([(0, y), (W, y)], fill=(0, 0, 0, int(175 * (y - (H - bot_h)) / bot_h)))
    img.alpha_composite(ov)


def _overlay(title, by_rank, revealed_ranks, cur_rank, caption) -> Image.Image:
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    _scrim(img)
    d = ImageDraw.Draw(img)

    # Title (top band): big, wrapped to <=2 lines (shrink only if it overflows).
    ts = 62
    tlines = _wrap(d, title.upper(), W - ML - MR, ts)
    while len(tlines) > 2 and ts > 36:
        ts -= 4
        tlines = _wrap(d, title.upper(), W - ML - MR, ts)
    y = TITLE_CY - (len(tlines) - 1) * (ts + 12) // 2
    for ln in tlines:
        _text(d, (W // 2, y), ln, ts, anchor="mm", stroke=7)
        y += ts + 14

    # Compact tier list (left) — text sits directly on the image (no panel).
    for rank in range(1, 6):
        yy = LIST_YS[rank - 1]
        cur = rank == cur_rank
        _text(d, (LIST_X, yy), str(rank), 66 if cur else 58,
              fill=YELLOW if rank in revealed_ranks else DIM, stroke=8)
        it = by_rank.get(rank)
        if rank in revealed_ranks and it:
            nm = it["name"].upper()
            _text(d, (LIST_X + 92, yy), nm, _fit(d, nm, 380, 50 if cur else 44), stroke=8)
        else:
            _text(d, (LIST_X + 92, yy), "—", 44, fill=DIM, stroke=8)

    # Caption (bottom band).
    if caption:
        cs = 56
        clines = _wrap(d, caption, W - ML - MR, cs)[:3]
        yy = CAP_CY - (len(clines) - 1) * (cs + 6) // 2
        for ln in clines:
            _text(d, (W // 2, yy), ln, cs, anchor="mm", stroke=7)
            yy += cs + 10
    return img


def _mood(title: str) -> str:
    t = title.lower()
    if any(w in t for w in ("dangerous", "deadl", "terrifying", "scary", "killer",
                            "destroy", "violent", "hostile", "worst")):
        return "ominous"
    if any(w in t for w in ("beautiful", "stunning", "gorgeous", "breathtaking",
                            "serene", "peaceful", "colorful", "amazing")):
        return "angelic"
    if any(w in t for w in ("biggest", "largest", "most massive", "most powerful",
                            "greatest", "strongest", "epic", "extreme")):
        return "epic"
    if any(w in t for w in ("strangest", "mysterious", "weird", "bizarre",
                            "unexplained", "signals", "secret")):
        return "mysterious"
    return "cinematic"


# mood -> (chord in a PHONE-AUDIBLE register, shimmer freq, shimmer amp)
_MOODS = {
    "ominous":    ([146.8, 174.6, 220.0, 293.7], 440.0, 0.03),   # D minor
    "angelic":    ([261.6, 329.6, 392.0, 523.3], 1046.5, 0.05),  # C major bright
    "epic":       ([130.8, 196.0, 261.6, 329.6], 523.3, 0.04),   # C major power
    "mysterious": ([146.8, 196.0, 220.0, 293.7], 587.3, 0.035),  # suspended
    "cinematic":  ([220.0, 261.6, 329.6, 440.0], 880.0, 0.04),
}


def _synth_bed(total, reveal_times, transition_times, mood, path, sr=44100):
    n = int(total * sr)
    buf = [0.0] * n
    chord, shf, sha = _MOODS.get(mood, _MOODS["cinematic"])
    amps = [0.10, 0.09, 0.075, 0.055]                 # audible, still a bed
    for k in range(n):
        env = min(1.0, k / (sr * 2.0)) * min(1.0, (n - k) / (sr * 1.5))
        s = sum(a * math.sin(2 * math.pi * f * (k / sr)) for f, a in zip(amps, chord))
        s += sha * math.sin(2 * math.pi * shf * (k / sr)) * (
            0.5 + 0.5 * math.sin(2 * math.pi * 0.15 * (k / sr)))
        buf[k] += s * env

    def imp(t0):
        st = int(t0 * sr)
        for k in range(int(0.4 * sr)):
            i = st + k
            if 0 <= i < n:
                e = math.exp(-(k / sr) * 12)
                buf[i] += 0.35 * e * math.sin(2 * math.pi * 130 * (k / sr))

    def wh(t0):
        st = int((t0 - 0.25) * sr)
        for k in range(int(0.5 * sr)):
            i = st + k
            if 0 <= i < n:
                buf[i] += 0.1 * math.sin(math.pi * k / (0.5 * sr)) \
                    * (random.random() * 2 - 1)

    for t in transition_times:
        wh(t)
    for t in reveal_times:
        imp(t)
    with wave.open(str(path), "w") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(b"".join(
            struct.pack("<h", int(max(-1.0, min(1.0, s)) * 32767)) for s in buf))


def _ffmpeg():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def _segment(image_path, overlay_png, dur, out_path):
    """Full-bleed Ken Burns image + the title/list/caption overlay."""
    frames = int(dur * FPS)
    vf = (f"[0:v]scale=2160:3840:force_original_aspect_ratio=increase,"
          f"crop=2160:3840,zoompan=z='min(zoom+0.0011,1.12)':d={frames}:"
          f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}:fps={FPS}[bg];"
          f"[bg][1:v]overlay=0:0,format=yuv420p[v]")
    subprocess.run([_ffmpeg(), "-y",
                    "-loop", "1", "-t", str(dur), "-i", str(image_path),
                    "-loop", "1", "-t", str(dur), "-i", str(overlay_png),
                    "-filter_complex", vf, "-map", "[v]",
                    "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                    "-t", str(dur), str(out_path)], check=True, capture_output=True)


def render_ranking_video(post_id, payload, config=None) -> Path:
    title = payload["title"]
    hook = payload.get("hook", "")
    by_rank = {it["rank"]: it for it in payload["items"]}
    OUTPUT_DIR.mkdir(exist_ok=True)
    ff = _ffmpeg()
    from voiceover.tts import synthesize

    def vo(text, tag):
        try:
            return synthesize(text, f"{post_id}_{tag}", config or {}, voice=NARRATOR)
        except Exception as e:
            log.warning("[ranking] VO '%s' failed: %s", tag, e)
            return None

    # RANDOM reveal order (deterministic per video); each drops into its slot.
    order = list(payload["items"])
    random.Random(post_id).shuffle(order)

    # plan entries: (image, revealed_ranks, cur_rank, caption, voiceover_text).
    # Caption stays clean (name + fact); the voiceover adds "Number N" for clarity.
    nebula = fetch_image("colorful nebula", prefer="nebula")
    plan = [(nebula, set(), None, hook or title, f"{title}. {hook}")]
    revealed = set()
    for it in order:
        revealed = revealed | {it["rank"]}
        img = fetch_image(it["query"], prefer=it["name"]) or nebula
        cap = f"{it['name']}. {it['stat']}."
        plan.append((img, set(revealed), it["rank"], cap, f"Number {it['rank']}. {cap}"))
    plan.append((nebula, set(range(1, 6)), None, "Follow for more cosmic countdowns.",
                 "Follow for more cosmic countdowns."))

    with tempfile.TemporaryDirectory() as tmp:
        tmpd = Path(tmp)
        seg_files, durs, vo_files, starts = [], [], [], []
        t = 0.0
        for i, (img, rev, cur, cap, votext) in enumerate(plan):
            res = vo(votext, f"vo{i}")
            dur = round((float(res["duration"]) if res else 2.5)
                        + (0.3 if cur is not None else 0.5), 2)
            ov = tmpd / f"ov{i}.png"
            _overlay(title, by_rank, rev, cur, cap).save(ov)
            seg = tmpd / f"seg{i}.mp4"
            _segment(img, ov, dur, seg)
            seg_files.append(seg); durs.append(dur); starts.append(t)
            vo_files.append(res["audio"] if res else None); t += dur
        total = t

        lst = tmpd / "list.txt"
        lst.write_text("".join(f"file '{s}'\n" for s in seg_files), encoding="utf-8")
        silent = tmpd / "silent.mp4"
        subprocess.run([ff, "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                        "-c", "copy", str(silent)], check=True, capture_output=True)

        # Voiceover only — music bed removed per feedback (a real track can be
        # mixed back later via config ranking.music).
        inputs = ["-i", str(silent)]
        filt, labels, idx = [], [], 1
        for i, vp in enumerate(vo_files):
            if not vp:
                continue
            inputs += ["-i", str(vp)]
            ms = int((starts[i] + (0.1 if i == 0 else 0.2)) * 1000)
            filt.append(f"[{idx}:a]volume=1.6,adelay={ms}|{ms}[a{idx}]")
            labels.append(f"[a{idx}]"); idx += 1
        filt.append("".join(labels) +
                    f"amix=inputs={len(labels)}:normalize=0:duration=first[a]")
        out_path = OUTPUT_DIR / f"{post_id}.mp4"
        log.info("[ranking] %d VO clips (no music) -> %s", idx - 1, out_path.name)
        subprocess.run([ff, "-y", *inputs, "-filter_complex", ";".join(filt),
                        "-map", "0:v", "-map", "[a]", "-c:v", "copy",
                        "-c:a", "aac", "-b:a", "160k", "-shortest", str(out_path)],
                       check=True, capture_output=True)
    return out_path


if __name__ == "__main__":
    from agentdrop_common import load_config
    ds = json.loads((PROJECT_ROOT / "sourcing" / "ranking_data"
                     / "dangerous_planets.json").read_text(encoding="utf-8"))
    print("Rendered:", render_ranking_video("rank_demo_planets", ds, load_config()))
