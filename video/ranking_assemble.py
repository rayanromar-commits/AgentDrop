"""
Cinematic ranking renderer — building "Top 5" tier-list countdowns.

Format: a title on top, a persistent 1->5 list that starts empty and fills in as
each item is revealed (countdown #5 -> #1), the current item's NASA image with
Ken Burns motion, big YELLOW outlined text, a full ElevenLabs voiceover reading
+ explaining every item, and a mood-matched music bed (ominous / angelic / epic
/ mysterious, chosen from the title).

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
BG = (8, 9, 16)
YELLOW = (255, 213, 0)
DIM = (95, 99, 120)
NARRATOR = {"id": "pNInz6obpgDQGcFmaJgB", "name": "Adam"}   # deep documentary voice

# Layout: title top, 1-5 list down the left, current image panel on the right.
TITLE_Y = 70
ROW_YS = [520, 776, 1032, 1288, 1544]          # rank 1..5, top to bottom
ROW_X = 60
NAME_X = 210
NAME_MAX_W = 400
PANEL = (600, 560, 430, 560)                    # x, y, w, h (image on the right)


def _font(sz):
    return ImageFont.truetype(str(FONT), sz)


def _text(d, xy, text, size, fill=YELLOW, anchor="lm", stroke=7):
    d.text(xy, text, font=_font(size), fill=fill, anchor=anchor,
           stroke_width=stroke, stroke_fill=(0, 0, 0))


def _fit(d, text, max_w, size, min_size=30):
    while size > min_size:
        if d.textlength(text, font=_font(size)) <= max_w:
            return size
        size -= 4
    return min_size


def _draw_title(d, title):
    """Big yellow outlined title, wrapped to <=2 lines, centered at top."""
    words = title.upper().split()
    line1, line2 = title.upper(), ""
    if d.textlength(title.upper(), font=_font(72)) > W - 90 and len(words) > 2:
        mid = len(words) // 2
        line1, line2 = " ".join(words[:mid]), " ".join(words[mid:])
    size = _fit(d, max(line1, line2, key=len), W - 90, 78)
    if line2:
        _text(d, (W // 2, TITLE_Y + 40), line1, size, anchor="mm")
        _text(d, (W // 2, TITLE_Y + 40 + size + 14), line2, size, anchor="mm")
    else:
        _text(d, (W // 2, TITLE_Y + 60), line1, size, anchor="mm")


def _overlay(title, by_rank, revealed_ranks, cur_rank) -> Image.Image:
    """Full-frame RGBA: title + the 1-5 list (filled for revealed ranks). The
    image-panel area is left transparent (the photo composites there)."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    _draw_title(d, title)
    for rank in range(1, 6):
        y = ROW_YS[rank - 1]
        cur = rank == cur_rank
        _text(d, (ROW_X, y), str(rank), 104 if cur else 92,
              fill=YELLOW if rank in revealed_ranks else DIM)
        it = by_rank.get(rank)
        if rank in revealed_ranks and it:
            name = it["name"].upper()
            _text(d, (NAME_X, y), name, _fit(d, name, NAME_MAX_W, 62 if cur else 54))
        else:
            _text(d, (NAME_X, y), "______", 54, fill=DIM)
    return img


def _mood(title: str) -> str:
    t = title.lower()
    if any(w in t for w in ("dangerous", "deadl", "terrifying", "scary",
                            "killer", "destroy", "violent", "hostile", "worst")):
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


# (mood -> low chord frequencies, shimmer freq, shimmer amp)
_MOODS = {
    "ominous":   ([36.7, 73.4, 87.3, 110.0], 220.0, 0.006),   # D minor, dark
    "angelic":   ([130.8, 164.8, 196.0, 261.6], 1046.5, 0.02),  # C major, bright
    "epic":      ([49.0, 65.4, 98.0, 130.8], 523.3, 0.012),    # power/major
    "mysterious": ([55.0, 73.4, 98.0, 146.8], 880.0, 0.01),    # suspended
    "cinematic": ([55.0, 82.4, 110.0, 164.8], 880.0, 0.012),
}


def _synth_bed(total, reveal_times, transition_times, mood, path, sr=44100):
    """Mood-matched music bed + whoosh (transitions) + impact (reveals)."""
    n = int(total * sr)
    buf = [0.0] * n
    chord, shf, sha = _MOODS.get(mood, _MOODS["cinematic"])
    amps = [0.05, 0.045, 0.04, 0.03]
    for k in range(n):
        env = min(1.0, k / (sr * 2.5)) * min(1.0, (n - k) / (sr * 1.5))
        s = sum(a * math.sin(2 * math.pi * f * (k / sr)) for f, a in zip(amps, chord))
        s += sha * math.sin(2 * math.pi * shf * (k / sr)) * (
            0.5 + 0.5 * math.sin(2 * math.pi * 0.15 * (k / sr)))
        buf[k] += s

    def imp(t0):
        st = int(t0 * sr)
        for k in range(int(0.45 * sr)):
            i = st + k
            if 0 <= i < n:
                e = math.exp(-(k / sr) * 11)
                buf[i] += 0.5 * e * math.sin(2 * math.pi * 68 * (k / sr)) \
                    + 0.1 * e * (random.random() * 2 - 1)

    def wh(t0):
        st = int((t0 - 0.28) * sr)
        for k in range(int(0.55 * sr)):
            i = st + k
            if 0 <= i < n:
                buf[i] += 0.12 * math.sin(math.pi * k / (0.55 * sr)) \
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
    """Black frame + Ken Burns image in the panel + the title/list overlay."""
    px, py, pw, ph = PANEL
    frames = int(dur * FPS)
    vf = (
        f"[1:v]scale={pw*2}:{ph*2}:force_original_aspect_ratio=increase,"
        f"crop={pw*2}:{ph*2},zoompan=z='min(zoom+0.0011,1.12)':d={frames}:"
        f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={pw}x{ph}:fps={FPS}[img];"
        f"[0:v][img]overlay={px}:{py}[b];[b][2:v]overlay=0:0,format=yuv420p[v]"
    )
    cmd = [_ffmpeg(), "-y",
           "-f", "lavfi", "-t", str(dur),
           "-i", f"color=c=0x{BG[0]:02x}{BG[1]:02x}{BG[2]:02x}:s={W}x{H}:r={FPS}",
           "-loop", "1", "-t", str(dur), "-i", str(image_path),
           "-loop", "1", "-t", str(dur), "-i", str(overlay_png),
           "-filter_complex", vf, "-map", "[v]",
           "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
           "-t", str(dur), str(out_path)]
    subprocess.run(cmd, check=True, capture_output=True)


def render_ranking_video(post_id, payload, config=None) -> Path:
    title = payload["title"]
    hook = payload.get("hook", "")
    items = sorted(payload["items"], key=lambda x: -x["rank"])   # #5 -> #1
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

    # Build the segment plan: (image, revealed_ranks, cur_rank, vo_text).
    nebula = fetch_image("colorful nebula", prefer="nebula")
    plan = [(nebula, set(), None, f"{title}. {hook}")]
    revealed = set()
    for it in items:
        revealed = revealed | {it["rank"]}
        img = fetch_image(it["query"], prefer=it["name"]) or nebula
        plan.append((img, set(revealed), it["rank"],
                     f"Number {it['rank']}. {it['name']}. {it['stat']}."))
    plan.append((by_rank[items[-1]["rank"]] and
                 fetch_image(items[-1]["query"], prefer=items[-1]["name"]) or nebula,
                 set(range(1, 6)), None,
                 "Which one shocked you the most? Follow for more cosmic countdowns."))

    with tempfile.TemporaryDirectory() as tmp:
        tmpd = Path(tmp)
        seg_files, durs, vo_files, starts = [], [], [], []
        t = 0.0
        for i, (img, rev, cur, votext) in enumerate(plan):
            res = vo(votext, f"vo{i}")
            vlen = float(res["duration"]) if res else 3.0
            pad = 0.7 if i == 0 else (0.9 if cur is not None else 1.0)
            dur = round(vlen + pad, 2)
            ov = tmpd / f"ov{i}.png"
            _overlay(title, by_rank, rev, cur).save(ov)
            seg = tmpd / f"seg{i}.mp4"
            _segment(img, ov, dur, seg)
            seg_files.append(seg); durs.append(dur)
            vo_files.append(res["audio"] if res else None)
            starts.append(t); t += dur
        total = t

        # Concatenate the (silent) segments.
        lst = tmpd / "list.txt"
        lst.write_text("".join(f"file '{s}'\n" for s in seg_files), encoding="utf-8")
        silent = tmpd / "silent.mp4"
        subprocess.run([ff, "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                        "-c", "copy", str(silent)], check=True, capture_output=True)

        # Mood music bed + SFX.
        bed = tmpd / "bed.wav"
        _synth_bed(total, starts[1:-1], starts, _mood(title), bed)

        # Mix: bed + each voiceover clip delayed to its segment start.
        inputs = ["-i", str(silent), "-i", str(bed)]
        filt = ["[1:a]volume=0.5[bed]"]
        mixlabels = ["[bed]"]
        idx = 2
        for i, vf_path in enumerate(vo_files):
            if not vf_path:
                continue
            inputs += ["-i", str(vf_path)]
            ms = int((starts[i] + (0.2 if i == 0 else 0.3)) * 1000)
            filt.append(f"[{idx}:a]volume=1.9,adelay={ms}|{ms}[a{idx}]")
            mixlabels.append(f"[a{idx}]")
            idx += 1
        filt.append("".join(mixlabels) +
                    f"amix=inputs={len(mixlabels)}:normalize=0:duration=first[a]")
        out_path = OUTPUT_DIR / f"{post_id}.mp4"
        log.info("[ranking] mixing %d voiceover clips + %s bed -> %s",
                 idx - 2, _mood(title), out_path.name)
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
