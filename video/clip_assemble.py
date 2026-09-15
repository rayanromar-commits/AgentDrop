"""
Ranked-clip renderer — silent "Ranking ___ Moments" countdowns (clip channel).

Layout:
  * A real stock VIDEO CLIP fills the frame for each rank.
  * Title in the top band (below the iPhone Dynamic Island), persistent.
  * A compact 1->5 rank list on the left; entries reveal into their slot as the
    countdown runs, #1 always last.
  * Bottom band = the entry's name + a short label, slammed in on the cut.

There is NO narration. 8 of the 10 reference channels in this genre have none,
and the one that does is the worst performer per subscriber in the set — so this
renderer has no TTS, no word timings and no karaoke sync at all. The audio track is a
rights-cleared music bed from `clipranking.music_dir` over the clips' own sound.
Nothing is synthesised: an earlier build generated its own whoosh per reveal and
it sounded like suction.

Forked from video/ranking_assemble.py. The hard-won parts are kept verbatim:
overlays are pre-composited into ONE frame sequence so each segment hands ffmpeg
exactly TWO inputs (feeding it more OOM-killed Railway twice), and segments are
stitched with the concat demuxer.

    python3 -m video.clip_assemble
"""

import json
import random
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image, ImageDraw, ImageFont

from agentdrop_common import setup_logging
from media.video_source import (clip_hash, fetch_item_clip, probe_duration)

log = setup_logging()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FONT = PROJECT_ROOT / "video" / "fonts" / "DejaVuSans-Bold.ttf"
OUTPUT_DIR = PROJECT_ROOT / "output"

W, H = 1080, 1920
FPS = 30
YELLOW = (255, 213, 0)          # the rank list
WHITE = (250, 251, 255)         # the title
DIM = (180, 184, 200)           # unrevealed slots
GOLD = (255, 236, 120)          # the #1 slot once revealed

# Safe-zone layout (carried over from the ranking renderer — same phone frame).
ML, MR = 60, 120
TITLE_CY = 275                       # below the Dynamic Island
LIST_X = 70
LIST_YS = [560, 690, 820, 950, 1080]
NAME_CY = 1500                       # entry name, in the eye-line
LABEL_CY = 1590                      # short label under it

# Segment timing. No narration means durations are chosen, not measured, so the
# video lands on target exactly and needs no atempo pass (which would also chew
# up the music bed).
INTRO_DUR = 1.8
OUTRO_DUR = 1.5
MIN_ITEM_DUR = 2.6
OUTRO_TEXT = "Which one was your #1?"      # a question — this format lives on comments


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


def _scrim(img, top_h=380, bot_h=600):
    """Darken the top and bottom bands so title + captions stay legible over
    footage we don't control the exposure of."""
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    for y in range(top_h):
        d.line([(0, y), (W, y)], fill=(0, 0, 0, int(150 * (1 - y / top_h))))
    for y in range(H - bot_h, H):
        d.line([(0, y), (W, y)], fill=(0, 0, 0, int(175 * (y - (H - bot_h)) / bot_h)))
    img.alpha_composite(ov)


def _crop(full: Image.Image) -> tuple[Image.Image, int, int]:
    """Crop a full-frame overlay to its non-empty bbox; return (image, x, y).

    This is what keeps each ffmpeg overlay input a small box instead of a full
    1080x1920 frame — the fix for the Railway OOM. Do not remove it."""
    bbox = full.getbbox()
    if bbox is None:
        return full, 0, 0
    return full.crop(bbox), int(bbox[0]), int(bbox[1])


def _base_overlay(title, by_rank, revealed_ranks, cur_rank):
    """Static layer for a segment: scrim + title + the 1->5 rank list."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    _scrim(img)
    d = ImageDraw.Draw(img)

    ts = 68
    tlines = _wrap(d, title.upper(), W - ML - MR, ts)
    while len(tlines) > 2 and ts > 38:
        ts -= 4
        tlines = _wrap(d, title.upper(), W - ML - MR, ts)
    y = TITLE_CY - (len(tlines) - 1) * (ts + 12) // 2
    for ln in tlines:
        _text(d, (W // 2, y), ln, ts, fill=WHITE, anchor="mm", stroke=9)
        y += ts + 14

    for rank in range(1, 6):
        yy = LIST_YS[rank - 1]
        cur = rank == cur_rank
        shown = rank in revealed_ranks
        colour = (GOLD if rank == 1 else YELLOW) if shown else DIM
        _text(d, (LIST_X, yy), str(rank), 66 if cur else 58, fill=colour, stroke=8)
        it = by_rank.get(rank)
        if shown and it:
            nm = it["name"].upper()
            _text(d, (LIST_X + 92, yy), nm,
                  _fit(d, nm, 380, 50 if cur else 44), fill=colour, stroke=8)
        else:
            _text(d, (LIST_X + 92, yy), "—", 44, fill=DIM, stroke=8)
    return img


def _caption_layer(name: str, label: str, scale: float = 1.0) -> Image.Image:
    """Full-frame transparent layer with the entry's name + short label.

    `scale` drives the slam-in: the layer is drawn oversized for the first few
    frames of a cut, then settles to 1.0. Cheap substitute for a real keyframed
    animation, and it reads as deliberate on a fast cut."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    nsize = int(_fit(d, name.upper(), W - 2 * ML, 88) * scale)
    _text(d, (W // 2, NAME_CY), name.upper(), nsize, fill=WHITE,
          anchor="mm", stroke=10)
    if label:
        lsize = int(_fit(d, label, W - 2 * ML, 56) * scale)
        _text(d, (W // 2, LABEL_CY), label, lsize, fill=YELLOW,
              anchor="mm", stroke=8)
    return img


_TEMPLATE_WORDS = re.compile(
    r"^\s*ranking\s+(?:best|funniest|craziest|most\s+satisfying|wildest)\s+|"
    r"\s+(?:moments|fails)\s*$", re.I)


def _topic(title: str) -> str:
    """The ranked subject, with the genre template stripped off.

    'Ranking Most Satisfying Glass Blowing Moments' -> 'Glass Blowing'."""
    s = _TEMPLATE_WORDS.sub("", title or "")
    s = _TEMPLATE_WORDS.sub("", s)          # leading and trailing are separate matches
    return s.strip() or (title or "").strip()


def _title_card(title: str) -> Image.Image:
    """Intro layer — the title, big and centred, over the opening clip."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    size = 96
    lines = _wrap(d, title.upper(), W - 2 * ML, size)
    while len(lines) > 3 and size > 52:
        size -= 6
        lines = _wrap(d, title.upper(), W - 2 * ML, size)
    y = H // 2 - (len(lines) - 1) * (size + 16) // 2
    for ln in lines:
        _text(d, (W // 2, y), ln, size, fill=WHITE, anchor="mm", stroke=12)
        y += size + 18
    return img


def _outro_layer(text: str) -> Image.Image:
    """The closing call-to-action.

    Deliberately NOT centred in the frame: the outro keeps the completed 1-5
    list on screen (that's the satisfying payoff), and a mid-frame CTA lands
    straight on top of rank 4. It sits in the caption band instead, which is
    empty by then."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    size = _fit(d, text.upper(), W - 2 * ML, 82)
    _text(d, (W // 2, NAME_CY), text.upper(), size, fill=WHITE,
          anchor="mm", stroke=12)
    return img


MUSIC_EXTS = (".mp3", ".m4a", ".wav", ".aac", ".ogg", ".opus")


def _pick_music(cfg: dict, post_id: str, category: str) -> Path | None:
    """Choose this video's backing track.

    `music_dir` is a folder of rights-cleared tracks. Drop files straight in it,
    or group them in subfolders named after a category ("ocean", "extreme", …)
    and a matching video takes its track from there — a calm reef and a motocross
    crash want different energy.

    The track is picked deterministically from post_id, so a re-render of the
    same video keeps its music, but consecutive uploads don't share one.

    NOTE the deliberate choice of baked-in royalty-free audio over YouTube's own
    trending sounds: a trending sound can only be attached by hand in the mobile
    app (the upload API cannot select one at all), and a Short carrying a
    licensed track sends about half its revenue to music licensing instead of the
    Creator Pool. This costs neither.
    """
    mdir = cfg.get("music_dir")
    if mdir:
        root = (PROJECT_ROOT / mdir)
        pool: list[Path] = []
        # Datasets carry a GRANULAR category ("sharks", "jellyfish"), while the
        # music folders are named after the coarse groups ("ocean"), so try the
        # exact tag first and then the group it maps to. Without the second step
        # a folder would essentially never match.
        names = []
        cat = (category or "").strip().lower()
        if cat:
            names.append(cat.replace(" ", "_"))
            try:
                from sourcing.clip_ranking_generate import _category_group
                names.append(_category_group(cat))
            except Exception:
                pass
        for name in names:
            sub = root / name
            if sub.is_dir():
                pool = [f for f in sorted(sub.iterdir())
                        if f.suffix.lower() in MUSIC_EXTS]
                if pool:
                    break
        if not pool and root.is_dir():
            pool = [f for f in sorted(root.rglob("*"))
                    if f.suffix.lower() in MUSIC_EXTS]
        if pool:
            return random.Random(f"{post_id}:music").choice(pool)
        log.warning("[clip] music_dir %r has no audio files in it.", mdir)

    one = cfg.get("music")                     # single-track fallback
    if one:
        path = PROJECT_ROOT / one
        if path.exists():
            return path
        log.warning("[clip] clipranking.music %r not found.", one)
    return None


def _ffmpeg():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def _overlay_track(tmpd, idx, base_img, overlays, dur):
    """Pre-render the WHOLE overlay layer as a per-frame PNG sequence, so the
    segment ffmpeg needs exactly ONE overlay input. `overlays` = [(img,x,y,s,e)].

    Feeding ffmpeg one input per caption piece OOM-killed Railway twice; this is
    flat at two inputs regardless. Identical consecutive frames are hard-copied,
    so a mostly-static overlay costs almost nothing."""
    n = max(int(dur * FPS), 1)
    d = tmpd / f"ovseq{idx}"
    d.mkdir(parents=True, exist_ok=True)
    prev_sig, prev_path = None, None
    for f in range(n):
        t = f / FPS
        active = [(im, x, y) for (im, x, y, s, e) in overlays if s <= t < e]
        sig = tuple(id(im) for (im, _x, _y) in active)
        path = d / f"f{f:05d}.png"
        if sig == prev_sig and prev_path is not None:
            shutil.copyfile(prev_path, path)
        else:
            frame = base_img.copy()
            for (im, x, y) in active:
                frame.alpha_composite(im, (int(x), int(y)))
            frame.save(path)
            prev_sig, prev_path = sig, path
    return d / "f%05d.png"


def _segment(clip_path, start, dur, overlay_pattern, out_path, framing="cover"):
    """One ranked clip + a single pre-composited overlay sequence.

    Exactly TWO inputs — memory-flat on Railway, same discipline as the ranking
    renderer. The clip is looped so a source shorter than the slot still fills it.

    framing='cover' crops the clip to fill the 9:16 frame (right almost always,
    since we ask the stock APIs for portrait). framing='fit' scales it to the
    WIDTH and fills top/bottom with a blurred extension, for a wide shot whose
    action spans the full width and would be cut in half by a centre crop."""
    inputs = ["-stream_loop", "-1", "-ss", f"{start:.2f}", "-t", f"{dur:.2f}",
              "-i", str(clip_path),
              "-framerate", str(FPS), "-i", str(overlay_pattern)]
    if framing == "fit":
        bg = (f"[0:v]fps={FPS},split=2[fb][fs];"
              f"[fb]scale={W}:{H}:force_original_aspect_ratio=increase,"
              f"crop={W}:{H},boxblur=26:1[fbb];"
              f"[fs]scale={W}:{H}:force_original_aspect_ratio=decrease[fss];"
              f"[fbb][fss]overlay=(W-w)/2:(H-h)/2,setsar=1[bg]")
    else:
        bg = (f"[0:v]fps={FPS},scale={W}:{H}:force_original_aspect_ratio=increase,"
              f"crop={W}:{H},setsar=1[bg]")
    filt = [bg, "[bg][1:v]overlay=0:0[vv]", "[vv]format=yuv420p[v]"]
    subprocess.run([_ffmpeg(), "-y", *inputs, "-filter_complex", ";".join(filt),
                    "-map", "[v]", "-c:v", "libx264", "-preset", "veryfast",
                    "-pix_fmt", "yuv420p", "-r", str(FPS), "-t", f"{dur:.2f}",
                    "-an", "-threads", "2", str(out_path)],
                   check=True, capture_output=True)


def _segment_audio(ff, clip_path, start, dur, out_wav):
    """The clip's own audio for its excerpt, or silence when it has none.

    Kept quiet — it's texture under the SFX and music, and stock clips have wildly
    inconsistent levels, so it's normalised rather than trusted."""
    if clip_path:
        try:
            subprocess.run([ff, "-y", "-stream_loop", "-1",
                            "-ss", f"{start:.2f}", "-t", f"{dur:.2f}",
                            "-i", str(clip_path), "-vn",
                            "-af", "loudnorm=I=-24:TP=-3,volume=0.5,apad",
                            "-t", f"{dur:.2f}", "-ar", "44100", "-ac", "1",
                            "-c:a", "pcm_s16le", str(out_wav)],
                           check=True, capture_output=True)
            if out_wav.exists() and out_wav.stat().st_size > 1000:
                return
        except Exception:
            pass                      # no audio stream, or an odd codec
    subprocess.run([ff, "-y", "-f", "lavfi", "-t", f"{dur:.2f}",
                    "-i", "anullsrc=r=44100:cl=mono",
                    "-c:a", "pcm_s16le", str(out_wav)],
                   check=True, capture_output=True)


def render_clip_video(post_id, payload, config=None) -> Path:
    cfg = (config or {}).get("clipranking", {})
    title = payload["title"]
    by_rank = {it["rank"]: it for it in payload["items"]}
    OUTPUT_DIR.mkdir(exist_ok=True)
    ff = _ffmpeg()

    # "Ranking Most Satisfying Glass Blowing Moments" -> "Glass Blowing". The
    # clip fetcher needs this: entry names alone are ambiguous, and stock search
    # will cheerfully return the wrong sense of a word.
    subject = _topic(title)

    target = float(cfg.get("target_seconds", 25) or 25)
    item_dur = max(MIN_ITEM_DUR, (target - INTRO_DUR - OUTRO_DUR) / 5.0)

    # Reveal order: ranks 5-2 shuffled (seeded per video so it isn't the same
    # sequence every upload), but #1 is ALWAYS last — it's the payoff the whole
    # countdown exists for, and revealing it early kills the reason to stay.
    rest = [it for it in payload["items"] if it["rank"] != 1]
    random.Random(post_id).shuffle(rest)
    order = rest + [it for it in payload["items"] if it["rank"] == 1]

    # Source one clean clip per entry. `used` keeps two ranks from landing on the
    # same stock footage — a backdrop that doesn't change while the ranking moves
    # on reads as broken, and repeated stills risk duplicate-content suppression.
    #
    # Sourcing runs in RANK ORDER (#1 first), which is deliberately NOT the reveal
    # order. Every entry sourced adds to `used`, so whatever goes last picks from
    # the most-depleted pool — and #1 is the payoff the whole countdown builds to,
    # the one shot that must not fall back.
    used: set[str] = set()
    clips: dict[int, tuple[Path | None, str]] = {}
    for it in sorted(payload["items"], key=lambda x: x["rank"]):
        path, framing = fetch_item_clip(
            it.get("queries") or it.get("query"), prefer=it["name"],
            exclude=used, subject=subject,
            context=f"{it['name']} — {it.get('label', '')}".strip(" —"))
        if path:
            used.add(clip_hash(path))
            used.add(path.stem)          # the source id, so a re-search skips it
        else:
            log.warning("[clip] no clean clip for %r — entry will show the "
                        "opening shot instead.", it["name"])
        clips[it["rank"]] = (path, framing)

    found = [clips[r][0] for r in sorted(clips) if clips[r][0]]
    if not found:
        raise RuntimeError(f"no usable clips for {title!r} — nothing to render")
    # #1's clip leads the intro: it's the best shot we sourced, and the opening
    # second is what decides whether anyone stays.
    opener = clips.get(1, (None, ""))[0] or found[0]

    # plan entries: (clip, framing, dur, base_overlay, timed_overlays)
    plan = []
    intro_base = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    _scrim(intro_base, top_h=500, bot_h=500)
    plan.append((opener, "cover", INTRO_DUR, intro_base,
                 [(*_crop(_title_card(title)), 0.0, INTRO_DUR)]))

    revealed: set[int] = set()
    for it in order:
        revealed = revealed | {it["rank"]}
        path, framing = clips[it["rank"]]
        base = _base_overlay(title, by_rank, set(revealed), it["rank"])
        cap_big = _crop(_caption_layer(it["name"], it.get("label", ""), scale=1.14))
        cap = _crop(_caption_layer(it["name"], it.get("label", "")))
        timed = [(*cap_big, 0.0, 0.16), (*cap, 0.16, item_dur)]
        plan.append((path or opener, framing, item_dur, base, timed))

    outro_base = _base_overlay(title, by_rank, {1, 2, 3, 4, 5}, None)
    plan.append((found[-1], "cover", OUTRO_DUR, outro_base,
                 [(*_crop(_outro_layer(OUTRO_TEXT)), 0.0, OUTRO_DUR)]))

    with tempfile.TemporaryDirectory() as tmp:
        tmpd = Path(tmp)
        seg_files, durs, starts, seg_clips = [], [], [], []
        t = 0.0
        for i, (clip, framing, dur, base, timed) in enumerate(plan):
            # Excerpt from the clip's middle: stock footage routinely opens on a
            # fade or an establishing beat, so the front of a clip is the least
            # interesting part of it.
            #
            # The intro is the exception. It reuses #1's clip (the best shot we
            # sourced, and the opening second decides whether anyone stays), so
            # it takes the HEAD instead — otherwise the intro and the #1 reveal
            # play the identical few seconds and the payoff lands as a repeat.
            cdur = probe_duration(clip) if clip else 0.0
            if i == 0:
                start = 0.0
            else:
                start = max(0.0, (cdur - dur) / 2.0) if cdur > dur else 0.0
            seg = tmpd / f"seg{i}.mp4"
            try:
                pattern = _overlay_track(tmpd, i, base, timed, dur)
                _segment(clip, start, dur, pattern, seg, framing)
            except Exception as e:
                # Never let one segment sink the whole video — retry with the
                # static layer only, so the daily upload still happens.
                log.warning("[clip] segment %d failed (%s); retrying static-only.",
                            i, e)
                pattern = _overlay_track(tmpd, f"{i}s", base, [], dur)
                _segment(clip, start, dur, pattern, seg, framing)
            seg_files.append(seg); durs.append(dur); starts.append(t)
            seg_clips.append((clip, start)); t += dur
        total = t

        lst = tmpd / "list.txt"
        lst.write_text("".join(f"file '{s}'\n" for s in seg_files), encoding="utf-8")
        silent = tmpd / "silent.mp4"
        subprocess.run([ff, "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                        "-c", "copy", str(silent)], check=True, capture_output=True)

        # Audio is built PER SEGMENT and concatenated, so each clip's own sound
        # stays locked to its own pictures — the same discipline that stopped the
        # space channel's narration drifting away from its images.
        alist = []
        for i, ((clip, start), d) in enumerate(zip(seg_clips, durs)):
            aw = tmpd / f"a{i}.wav"
            _segment_audio(ff, clip, start, d, aw)
            alist.append(aw)
        alst = tmpd / "audiolist.txt"
        alst.write_text("".join(f"file '{a}'\n" for a in alist), encoding="utf-8")
        native = tmpd / "native.wav"
        subprocess.run([ff, "-y", "-f", "concat", "-safe", "0", "-i", str(alst),
                        "-c", "copy", str(native)], check=True, capture_output=True)

        # Music. There is deliberately no synthesised fallback and no
        # synthesised sound design: an earlier build generated its own whoosh on
        # every reveal (a half-second noise swell) and it read as a suction
        # noise, the same way the space channel's synth bed read as fake. If we
        # can't play real audio, we play none.
        music_path = _pick_music(cfg, post_id, payload.get("category", ""))
        if not music_path:
            log.warning("[clip] no music configured (clipranking.music_dir). This "
                        "format has no narration, so the video carries only its "
                        "clips' own audio — add rights-cleared tracks.")

        vol = float(cfg.get("music_volume", 0.55) or 0.55)
        full_audio = tmpd / "audio.wav"
        if music_path:
            log.info("[clip] music: %s", music_path.name)
            # The track is looped to cover the video, trimmed, levelled and faded
            # out; the clips' own audio sits underneath as texture.
            subprocess.run(
                [ff, "-y", "-i", str(native),
                 "-stream_loop", "-1", "-i", str(music_path),
                 "-filter_complex",
                 f"[1:a]atrim=0:{total:.2f},asetpts=N/SR/TB,"
                 f"loudnorm=I=-16:TP=-1.5,volume={vol:.2f},"
                 f"afade=t=out:st={max(total - 1.2, 0):.2f}:d=1.2[m];"
                 "[0:a][m]amix=inputs=2:duration=first:normalize=0[a]",
                 "-map", "[a]", "-t", f"{total:.2f}",
                 "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le",
                 str(full_audio)], check=True, capture_output=True)
        else:
            subprocess.run(
                [ff, "-y", "-i", str(native), "-map", "0:a",
                 "-t", f"{total:.2f}", "-ar", "44100", "-ac", "2",
                 "-c:a", "pcm_s16le", str(full_audio)],
                check=True, capture_output=True)

        out_path = OUTPUT_DIR / f"{post_id}.mp4"
        log.info("[clip] %d/%d entries on a real clip -> %s (%.1fs)",
                 len(found), len(payload["items"]), out_path.name, total)
        # Durations are chosen rather than measured, so the edit already lands on
        # target — no atempo pass (which would also chew up the music bed).
        subprocess.run([ff, "-y", "-i", str(silent), "-i", str(full_audio),
                        "-map", "0:v", "-map", "1:a", "-c:v", "copy",
                        "-c:a", "aac", "-b:a", "160k", "-shortest", str(out_path)],
                       check=True, capture_output=True)
    return out_path


if __name__ == "__main__":
    from agentdrop_common import load_config
    import glob
    files = sorted(glob.glob(str(PROJECT_ROOT / "sourcing" / "clip_ranking_data" / "*.json")))
    if not files:
        sys.exit("no datasets in sourcing/clip_ranking_data — run "
                 "python3 -m sourcing.clip_ranking_generate first")
    ds = json.loads(Path(files[0]).read_text(encoding="utf-8"))
    print("Rendered:", render_clip_video("clip_demo", ds, load_config()))
