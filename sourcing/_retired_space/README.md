# Retired: the space-ranking format

TopTierCosmos ran "Top 5 <space thing>" countdowns here from 2026-07-08 to
2026-09-15 — 70 videos, 69,240 views, a median of 1,003 views per video and a
best-ever 1,632. Retention was fine at 66%; what never happened was spread (14
shares and 36 comments across the whole run). The channel pivoted to ranked
wildlife clips because the lane had no audience to grow into, not because the
format was broken.

Everything space-specific is parked here and **nothing runs it**. Production,
restocking and dataset generation are all gated on `content_type`, which is now
`clipranking`, so this costs no API calls, no ElevenLabs credits and no compute.

## What's here
- `ranking_data/` — the 36 "Top 5" datasets
- `ranking_posted.json` — the durable ledger of which ones were uploaded

## Code still in place (inert)
`sourcing/ranking_source.py`, `sourcing/ranking_generate.py`,
`video/ranking_assemble.py`, `media/clip_source.py` and `main._produce_ranking`.
They're left where they are because they cost nothing while unused and because
`clip_ranking_source.py` still imports `channel_titles()` from
`ranking_source.py` — the two formats share one YouTube channel, so reconciling
against the live uploads list has to see both.

## To bring it back
Set `content_type: ranking` in config.yaml. The `ranking.dataset_dir` path
already points here, so nothing else needs changing.

## Note on the live videos
The 70 space videos are still public on the channel. Nothing here deletes them —
that's a separate decision, and worth thinking about, since a channel's back
catalogue is part of what the algorithm reads when deciding who to show new
uploads to.
