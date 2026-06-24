"""
Compress background footage to the size AgentDrop actually renders at.

Why: shorts render at 1080x1920 and the narration audio comes from TTS,
so 4K clips with audio are wasted bytes — they bloat the Railway volume
and slow every render. This downscales each clip to 1080x1920 (cover +
center-crop, matching video/assemble.py), re-encodes H.264, and DROPS the
audio track. Output goes to ./footage_compressed/.

Run it whenever you add new clips:
    source venv/bin/activate
    python3 tools/compress_footage.py

Then upload the contents of footage_compressed/ to your cloud volume.
"""

import subprocess
import sys
from pathlib import Path

import imageio_ffmpeg

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "footage"
DST = ROOT / "footage_compressed"
W, H, FPS = 1080, 1920, 30
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
VF = (
    f"scale={W}:{H}:force_original_aspect_ratio=increase,"
    f"crop={W}:{H},setsar=1,fps={FPS}"
)


def main() -> None:
    DST.mkdir(exist_ok=True)
    clips = sorted(p for p in SRC.iterdir() if p.suffix.lower() in VIDEO_EXTS)
    if not clips:
        print(f"No footage found in {SRC}")
        sys.exit(1)

    print(f"Compressing {len(clips)} clip(s) -> {DST}\n")
    for i, src in enumerate(clips, 1):
        out = DST / (src.stem + ".mp4")
        if out.exists():
            print(f"[{i}/{len(clips)}] skip (already done): {out.name}")
            continue
        print(f"[{i}/{len(clips)}] {src.name} ...", flush=True)
        cmd = [
            FFMPEG, "-y", "-i", str(src),
            "-vf", VF,
            "-an",                       # drop audio — TTS provides it
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p",
            str(out),
        ]
        r = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE, text=True)
        if r.returncode != 0:
            print(f"   FAILED: {r.stderr[-400:]}")
            continue
        in_mb = src.stat().st_size / 1e6
        out_mb = out.stat().st_size / 1e6
        print(f"   {in_mb:.0f}MB -> {out_mb:.0f}MB")

    total = sum(p.stat().st_size for p in DST.glob("*.mp4")) / 1e6
    print(f"\nDone. footage_compressed/ total: {total:.0f}MB "
          f"({len(list(DST.glob('*.mp4')))} clips)")


if __name__ == "__main__":
    main()
