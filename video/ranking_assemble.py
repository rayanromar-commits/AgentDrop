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
import subprocess
import sys
import tempfile
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
RANK_SEC = 3.6
INTRO_SEC = 3.2
OUTRO_SEC = 2.6
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

        out_path = OUTPUT_DIR / f"{post_id}.mp4"
        log.info("[ranking] crossfading %d segments -> %s", len(segs), out_path.name)
        _xfade_concat(segs, durs, out_path)
    return out_path


if __name__ == "__main__":
    ds = json.loads((PROJECT_ROOT / "sourcing" / "ranking_data"
                     / "dangerous_planets.json").read_text(encoding="utf-8"))
    print("Rendered:", render_ranking_video("rank_demo_planets", ds))
