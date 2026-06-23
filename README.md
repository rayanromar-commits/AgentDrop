# AgentDrop

Autonomously turns Reddit stories into narrated vertical YouTube Shorts
(gameplay / calming background) and uploads them on a schedule.

## Pipeline
1. **Sourcing** — pull popular Reddit stories (PRAW).
2. **Screening** — length / NSFW / content filter + clean text for narration.
3. **Voiceover** — text-to-speech with word timing for captions.
4. **Video** — vertical 1080x1920 mp4: background clip + voiceover + captions (ffmpeg).
5. **Review** — manual approval queue (toggleable to auto).
6. **Upload** — YouTube Data API, scheduled.
7. **Tracking** — pull view/like stats; weight selection toward winners.

## Project layout
```
config.yaml          <- edit this to control behavior
main.py              <- entry point + (later) scheduler
agentdrop_common.py  <- shared config + logging helpers
sourcing/            Reddit scraping
processing/          text cleaning + screening
voiceover/           text-to-speech
video/               ffmpeg assembly + captions
footage/             YOUR background clip library (rights-cleared)
review/              approval queue (pending/approved/rejected)
upload/              YouTube API
tracking/            performance stats
database/            SQLite db (used posts, video status, stats)
```

## Setup (Step 1)
1. Have Python 3 installed (`python3 --version`).
2. (Recommended) create a virtual environment:
   ```
   python3 -m venv venv
   source venv/bin/activate
   ```
3. Install Step-1 dependencies:
   ```
   pip3 install -r requirements.txt
   ```
4. Run it:
   ```
   python3 main.py
   ```
   You should see your config printed back to you.

## Build status
- [x] Step 1 — scaffold + config
- [ ] Step 2 — Reddit sourcing
- [ ] Step 3 — text cleaning + screening
- [ ] Step 4 — voiceover
- [ ] Step 5 — footage library
- [ ] Step 6 — video assembly
- [ ] Step 7 — review queue
- [ ] Step 8 — YouTube upload
- [ ] Step 9 — scheduling + tracking
- [ ] Step 10 — auto mode

## Copyright / monetization notes
See `COPYRIGHT_NOTES.md` (added at the footage step) before going public.
