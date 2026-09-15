# Music for the ranked-clip channel

Drop rights-cleared audio files in here (`.mp3`, `.m4a`, `.wav`, `.aac`, `.ogg`,
`.opus`). The renderer picks one per video, deterministically from the video's
id, so uploads don't repeat a track but a re-render keeps the one it had.

## Where to get tracks (free, commercial use, no attribution needed)

- **YouTube Audio Library** — studio.youtube.com > Audio library. Filter to
  "Attribution not required". Safest option: it is YouTube's own cleared catalog.
- **Pixabay Music** — pixabay.com/music. Free for commercial use.

## Optional: match the energy to the topic

Either drop everything in this folder, or make subfolders named after a dataset
category and the renderer will prefer a matching one:

    assets/music/ocean/        calm, wide, ambient
    assets/music/wildlife/     cinematic, tense
    assets/music/extreme/      fast, percussive
    assets/music/satisfying/   clean, rhythmic, lo-fi
    assets/music/nature/       epic, orchestral
    assets/music/birds/
    assets/music/pets/

Anything not matched falls back to whatever is in the root.

## Why not trending sounds

They'd suit this genre, but the YouTube upload API cannot attach one — picking a
sound is a mobile-app-only action, so it would break the automation. A Short
using a licensed track also sends roughly half its revenue to music licensing
rather than the Creator Pool. Baked-in royalty-free music costs neither.

A handful of tracks is enough to start; 8-10 is plenty of variety for a daily
upload.
