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
import re
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
YELLOW = (255, 213, 0)          # the tier list
WHITE = (250, 251, 255)         # the title + caption base
NEON = (57, 255, 20)            # the word currently being spoken (karaoke)
DIM = (180, 184, 200)
CAP_SIZE = 74                   # caption font size — big, attention-grabbing
NARRATOR = {"id": "pNInz6obpgDQGcFmaJgB", "name": "Adam"}

# Intro/outro + fallback background. Picked PER VIDEO (seeded by post_id) so the
# backdrop is NOT the same starry image every upload — variety without leaving
# the space theme. All are famous, reliably-available NASA/Hubble targets.
BACKGROUNDS = [
    ("Carina Nebula", "nebula"), ("Orion Nebula", "nebula"),
    ("Eagle Nebula pillars of creation", "nebula"), ("Helix Nebula", "nebula"),
    ("Lagoon Nebula", "nebula"), ("Tarantula Nebula", "nebula"),
    ("Veil Nebula", "nebula"), ("colorful nebula", "nebula"),
    ("Hubble Ultra Deep Field galaxies", "galaxies"), ("Andromeda galaxy", "galaxy"),
    ("Milky Way star field", "stars"), ("Cosmic Cliffs Carina", "nebula"),
]

# Safe-zone layout.
ML, MR = 60, 120                     # left / right margins
TITLE_CY = 205                       # title center (below the Dynamic Island)
LIST_X = 70
LIST_YS = [560, 690, 820, 950, 1080]  # compact 1..5 rows, upper-left
CAP_CY = 1480                        # caption center — raised into the eye-line
                                     # (midway between the list end and the bottom)


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


def _scrim(img, top_h=300, bot_h=600):
    """Darken the top and bottom bands so title + captions stay legible."""
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    for y in range(top_h):
        d.line([(0, y), (W, y)], fill=(0, 0, 0, int(150 * (1 - y / top_h))))
    for y in range(H - bot_h, H):
        d.line([(0, y), (W, y)], fill=(0, 0, 0, int(175 * (y - (H - bot_h)) / bot_h)))
    img.alpha_composite(ov)


PAGE_LINES = 2                       # caption shows <=2 lines at a time, then pages


def _fill_times(times, seg_dur):
    """Replace any missing word timings with an even spread across the segment,
    so captions still page sensibly if the voiceover word data is absent."""
    n = len(times)
    if n == 0:
        return times
    span = max(seg_dur - 0.4, 0.4)
    even = [(0.2 + span * k / n, 0.2 + span * (k + 1) / n) for k in range(n)]
    return [t if (t and t[1] > t[0]) else even[k] for k, t in enumerate(times)]


def _paginate(d, words, size, max_w):
    """Group (word,start,end) tokens into pages of <=PAGE_LINES wrapped lines.
    Returns [[line, line], ...] where each line is a list of tokens."""
    space = d.textlength(" ", font=_font(size))
    lines, cur, cur_w = [], [], 0.0
    for tok in words:
        ww = d.textlength(tok[0], font=_font(size))
        if cur and cur_w + space + ww > max_w:
            lines.append(cur); cur, cur_w = [], 0.0
        cur.append(tok); cur_w += (space if cur_w else 0) + ww
    if cur:
        lines.append(cur)
    return [lines[i:i + PAGE_LINES] for i in range(0, len(lines), PAGE_LINES)]


def _caption_overlays(tmpd, prefix, caption, word_times, seg_dur, delay):
    """Build timed caption overlays that PAGE through long narration instead of
    truncating: for each page a white base (all its words) plus a neon highlight
    per word timed to the voice. Returns [(png_path, start, end), ...]."""
    words = caption.split()
    if not words:
        return []
    times = _fill_times(list(word_times)[:len(words)] +
                        [None] * (len(words) - len(word_times)), seg_dur)
    tokens = [(w, t[0], t[1]) for w, t in zip(words, times)]

    scratch = ImageDraw.Draw(Image.new("RGBA", (W, H)))
    pages = _paginate(scratch, tokens, CAP_SIZE, W - ML - MR)
    # Page display windows: each page stays up until the next page begins.
    starts = [pg[0][0][1] for pg in pages]             # first word start of page
    out = []
    for pi, pg in enumerate(pages):
        p_start = starts[pi]
        p_end = starts[pi + 1] if pi + 1 < len(pages) else seg_dur
        flat = [tok for ln in pg for tok in ln]
        yy = CAP_CY - (len(pg) - 1) * (CAP_SIZE + 12) // 2
        placed = []                                    # (word, cx, cy, start, end)
        for ln in pg:
            text = " ".join(t[0] for t in ln)
            x = (W - scratch.textlength(text, font=_font(CAP_SIZE))) / 2
            space = scratch.textlength(" ", font=_font(CAP_SIZE))
            for (w, ws, we) in ln:
                ww = scratch.textlength(w, font=_font(CAP_SIZE))
                placed.append((w, x + ww / 2, yy, ws, we))
                x += ww + space
            yy += CAP_SIZE + 12
        # White base for the whole page.
        base = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        bd = ImageDraw.Draw(base)
        for (w, cx, cy, _ws, _we) in placed:
            _text(bd, (cx, cy), w, CAP_SIZE, fill=WHITE, anchor="mm", stroke=8)
        bp = tmpd / f"{prefix}_base{pi}.png"
        base.save(bp)
        out.append((bp, max(p_start + delay, 0.0), p_end + delay))
        # Neon highlight per word, timed to the voice.
        for wi, (w, cx, cy, ws, we) in enumerate(placed):
            if we <= ws:
                continue
            wp = tmpd / f"{prefix}_w{pi}_{wi}.png"
            _word_png(w, cx, cy, CAP_SIZE).save(wp)
            out.append((wp, ws + delay, we + delay))
    return out


def _word_png(word, cx, cy, size=CAP_SIZE) -> Image.Image:
    """Full-frame transparent PNG with one word in NEON at (cx, cy)."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    stroke = 9 if size >= 60 else 8
    _text(ImageDraw.Draw(img), (cx, cy), word, size, fill=NEON,
          anchor="mm", stroke=stroke)
    return img


def _align_words(words, vo_words):
    """Map each displayed word (a plain string) to a (start,end) from the
    ElevenLabs word timestamps (matches by alphanumeric content, so punctuation
    lines up). Returns a list the same length as `words` (None where unknown)."""
    def key(s):
        return re.sub(r"[^a-z0-9]", "", s.lower())
    out, vi = [], 0
    for w in words:
        k = key(w)
        if not k or vi >= len(vo_words):
            out.append(None)
            continue
        start = vo_words[vi]["start"]
        end, acc = vo_words[vi]["end"], ""
        while vi < len(vo_words) and len(acc) < len(k):
            acc += key(vo_words[vi]["word"])
            end = vo_words[vi]["end"]
            vi += 1
        out.append((start, end))
    return out


def _overlay(title, by_rank, revealed_ranks, cur_rank):
    """Returns (overlay_image, title_word_positions). Title + tier list + scrim
    only — the bottom caption is drawn separately as timed, paginated overlays."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    _scrim(img)
    d = ImageDraw.Draw(img)

    # Title (top band): WHITE, bold, wrapped to <=2 lines (shrink if it overflows).
    ts = 68
    tlines = _wrap(d, title.upper(), W - ML - MR, ts)
    while len(tlines) > 2 and ts > 38:
        ts -= 4
        tlines = _wrap(d, title.upper(), W - ML - MR, ts)
    y = TITLE_CY - (len(tlines) - 1) * (ts + 12) // 2
    tfont = _font(ts)
    tspace = d.textlength(" ", font=tfont)
    title_words = []                     # per-word centres (for intro karaoke)
    for ln in tlines:
        _text(d, (W // 2, y), ln, ts, fill=WHITE, anchor="mm", stroke=9)
        x = (W - d.textlength(ln, font=tfont)) / 2
        for w in ln.split():
            ww = d.textlength(w, font=tfont)
            title_words.append((w, x + ww / 2, y, ts))
            x += ww + tspace
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

    return img, title_words


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


def _segment(image_path, overlay_png, word_overlays, dur, out_path):
    """Full-bleed Ken Burns image + the static overlay (title/list/white caption)
    + timed neon per-word highlights (karaoke). word_overlays = [(png, start, end)].

    Smooth zoom: input fed AT the output fps, and the zoom accumulates one small
    step per frame (d=1 with pzoom) so it never resets/judders."""
    frames = max(int(dur * FPS), 1)
    incr = round(0.12 / frames, 5)                  # ease from 1.0 -> ~1.12 over the clip
    inputs = ["-framerate", str(FPS), "-loop", "1", "-t", str(dur), "-i", str(image_path),
              "-loop", "1", "-t", str(dur), "-i", str(overlay_png)]
    filt = [f"[0:v]scale=2400:4267:force_original_aspect_ratio=increase,"
            f"crop=2400:4267,zoompan=z='min(max(pzoom,1.0)+{incr},1.13)':d=1:"
            f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}:fps={FPS}[bg]",
            "[bg][1:v]overlay=0:0[v0]"]
    prev = "[v0]"
    for k, (png, st, en) in enumerate(word_overlays):
        inputs += ["-loop", "1", "-t", str(dur), "-i", str(png)]
        lbl = f"[v{k + 1}]"
        filt.append(f"{prev}[{k + 2}:v]overlay=0:0:"
                    f"enable='between(t,{round(st, 3)},{round(en, 3)})'{lbl}")
        prev = lbl
    filt.append(f"{prev}format=yuv420p[v]")
    subprocess.run([_ffmpeg(), "-y", *inputs, "-filter_complex", ";".join(filt),
                    "-map", "[v]", "-c:v", "libx264", "-preset", "veryfast",
                    "-pix_fmt", "yuv420p", "-r", str(FPS), "-t", str(dur),
                    str(out_path)], check=True, capture_output=True)


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
    # Per-video background (varied, seeded by post_id) — NOT the same nebula every
    # upload. Used for the intro/outro and as the last-resort item fallback.
    bg_q, bg_p = random.Random(f"{post_id}:bg").choice(BACKGROUNDS)
    background = (fetch_image(bg_q, prefer=bg_p)
                  or fetch_image("colorful nebula", prefer="nebula"))
    # Intro: the voice reads the TITLE first, then the hook. The title karaokes
    # up top (it's already on screen); the hook is the bottom caption after it.
    plan = [(background, set(), None, hook, f"{title}. {hook}")]
    revealed = set()
    for it in order:
        revealed = revealed | {it["rank"]}
        # Real picture of THIS object: NASA first, then an accurate Wikipedia
        # image (fetch_image handles the web fallback) so items are never blank.
        img = fetch_image(it["query"], prefer=it["name"]) or background
        cap = f"{it['name']}. {it['stat']}."
        plan.append((img, set(revealed), it["rank"], cap, cap))
    plan.append((background, set(range(1, 6)), None, "Follow for more cosmic countdowns.",
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
            base_img, title_words = _overlay(title, by_rank, rev, cur)
            base_img.save(ov)

            vo_words = []
            if res:
                try:
                    vo_words = json.load(open(res["words"]))
                except Exception:
                    vo_words = []
            delay = 0.1 if i == 0 else 0.2              # matches the audio adelay
            timed = []                                  # (png, start, end) overlays

            # Bottom caption PAGES through the narration (never truncates); the
            # intro also karaokes the TITLE up top while it's being read.
            if i == 0:
                tw = [w for (w, _cx, _cy, _s) in title_words]
                all_times = _align_words(tw + cap.split(), vo_words)
                for k, ((w, cx, cy, size), tm) in enumerate(
                        zip(title_words, all_times[:len(tw)])):
                    if tm and tm[1] > tm[0]:
                        wp = tmpd / f"tw{i}_{k}.png"
                        _word_png(w, cx, cy, size).save(wp)
                        timed.append((wp, tm[0] + delay, tm[1] + delay))
                timed += _caption_overlays(tmpd, f"c{i}", cap,
                                           all_times[len(tw):], dur, delay)
            else:
                cap_times = _align_words(cap.split(), vo_words)
                timed += _caption_overlays(tmpd, f"c{i}", cap, cap_times, dur, delay)

            seg = tmpd / f"seg{i}.mp4"
            _segment(img, ov, timed, dur, seg)
            seg_files.append(seg); durs.append(dur); starts.append(t)
            vo_files.append(res["audio"] if res else None); t += dur
        total = t

        lst = tmpd / "list.txt"
        lst.write_text("".join(f"file '{s}'\n" for s in seg_files), encoding="utf-8")
        silent = tmpd / "silent.mp4"
        subprocess.run([ff, "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                        "-c", "copy", str(silent)], check=True, capture_output=True)

        # Build audio PER SEGMENT (each VO padded to its own segment length) and
        # concatenate, so narration stays perfectly locked to its image — no
        # timestamp drift between the zoom and the voice.
        alist = []
        for i, (vp, d) in enumerate(zip(vo_files, durs)):
            aw = tmpd / f"a{i}.wav"
            delay = 100 if i == 0 else 200
            if vp:
                subprocess.run([ff, "-y", "-i", str(vp), "-af",
                                f"volume=1.6,adelay={delay}|{delay},apad",
                                "-t", str(d), "-ar", "44100", "-ac", "1",
                                "-c:a", "pcm_s16le", str(aw)],
                               check=True, capture_output=True)
            else:
                subprocess.run([ff, "-y", "-f", "lavfi", "-t", str(d),
                                "-i", "anullsrc=r=44100:cl=mono",
                                "-c:a", "pcm_s16le", str(aw)],
                               check=True, capture_output=True)
            alist.append(aw)
        alst = tmpd / "audiolist.txt"
        alst.write_text("".join(f"file '{a}'\n" for a in alist), encoding="utf-8")
        full_audio = tmpd / "audio.wav"
        subprocess.run([ff, "-y", "-f", "concat", "-safe", "0", "-i", str(alst),
                        "-c", "copy", str(full_audio)], check=True, capture_output=True)

        # Tighten to ~target seconds by speeding up the EDIT (tempo up, pitch
        # preserved via atempo) — NOT by making the narrator re-speak faster.
        # Captions/karaoke are already baked into the frames, so a uniform
        # speed-up keeps everything in sync.
        target = float((config or {}).get("ranking", {}).get("target_seconds", 40) or 0)
        speed = min(total / target, 2.0) if target and total > target + 0.5 else 1.0

        out_path = OUTPUT_DIR / f"{post_id}.mp4"
        log.info("[ranking] %d VO clips -> %s (%.1fs -> ~%.1fs, speed x%.2f)",
                 sum(1 for v in vo_files if v), out_path.name, total, total / speed, speed)
        if speed > 1.005:
            subprocess.run([ff, "-y", "-i", str(silent), "-i", str(full_audio),
                            "-filter_complex",
                            f"[0:v]setpts=PTS/{speed:.5f}[v];[1:a]atempo={speed:.5f}[a]",
                            "-map", "[v]", "-map", "[a]",
                            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt",
                            "yuv420p", "-r", str(FPS), "-c:a", "aac", "-b:a", "160k",
                            "-shortest", str(out_path)], check=True, capture_output=True)
        else:
            subprocess.run([ff, "-y", "-i", str(silent), "-i", str(full_audio),
                            "-map", "0:v", "-map", "1:a", "-c:v", "copy",
                            "-c:a", "aac", "-b:a", "160k", "-shortest", str(out_path)],
                           check=True, capture_output=True)
    return out_path


if __name__ == "__main__":
    from agentdrop_common import load_config
    ds = json.loads((PROJECT_ROOT / "sourcing" / "ranking_data"
                     / "dangerous_planets.json").read_text(encoding="utf-8"))
    print("Rendered:", render_ranking_video("rank_demo_planets", ds, load_config()))
