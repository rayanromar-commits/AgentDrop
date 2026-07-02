"""
One-time maintenance: bring ALREADY-POSTED YouTube videos in line with the
per-video settings policy (set 2026-07-01):

  - description  -> BLANK
  - tags         -> NONE (removed)
  - categoryId   -> "24" (Entertainment)

It intentionally does NOT touch: the title, privacy status, made-for-kids, or
anything in the `status` part. (The "altered/synthetic content" disclosure is
NOT available in the Data API and must be set manually in YouTube Studio.)

Videos are discovered from the channel's real "uploads" playlist (the channel
is the source of truth, not our local DB, which can be out of sync).

Usage:
    python3 -m upload.backfill_youtube_settings          # DRY RUN: list only
    python3 -m upload.backfill_youtube_settings --apply   # actually update

--apply needs the youtube.force-ssl scope. If the current token predates it,
re-authorize once: delete token.json and re-run any upload/auth command to
trigger the browser consent, then run this with --apply.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdrop_common import setup_logging
from upload.youtube_upload import get_authenticated_service

log = setup_logging()

TARGET_CATEGORY_ID = "24"   # Entertainment


def list_uploaded_videos(youtube) -> list[dict]:
    """Return snippet dicts for every video on the authorized channel."""
    # 1) find this channel's "uploads" playlist id
    ch = youtube.channels().list(part="contentDetails", mine=True).execute()
    items = ch.get("items", [])
    if not items:
        raise RuntimeError("No channel found for the authorized account.")
    uploads_id = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]

    # 2) page through the uploads playlist to collect video ids
    video_ids, page_token = [], None
    while True:
        resp = youtube.playlistItems().list(
            part="contentDetails", playlistId=uploads_id,
            maxResults=50, pageToken=page_token,
        ).execute()
        for it in resp.get("items", []):
            video_ids.append(it["contentDetails"]["videoId"])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    # 3) fetch full snippets in batches of 50
    videos = []
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i + 50]
        resp = youtube.videos().list(
            part="snippet", id=",".join(batch)
        ).execute()
        videos.extend(resp.get("items", []))
    return videos


def _needs_update(snippet: dict) -> bool:
    return bool(
        snippet.get("description")
        or snippet.get("tags")
        or snippet.get("categoryId") != TARGET_CATEGORY_ID
    )


def main(apply: bool) -> None:
    youtube = get_authenticated_service()
    videos = list_uploaded_videos(youtube)
    log.info("Found %d video(s) on the channel.", len(videos))

    to_change = [v for v in videos if _needs_update(v["snippet"])]

    print(f"\n{'APPLYING' if apply else 'DRY RUN — no changes'} — "
          f"{len(to_change)} of {len(videos)} video(s) need updating:\n")
    for v in videos:
        s = v["snippet"]
        flags = []
        if s.get("description"):
            flags.append(f"desc({len(s['description'])} chars)")
        if s.get("tags"):
            flags.append(f"tags({len(s['tags'])})")
        if s.get("categoryId") != TARGET_CATEGORY_ID:
            flags.append(f"cat({s.get('categoryId')}->24)")
        status = ", ".join(flags) if flags else "already compliant"
        print(f"  [{v['id']}] {s.get('title','')[:60]!r}  ->  {status}")

    if not apply:
        print("\nDry run only. Re-run with --apply to make these changes.")
        return

    print()
    changed = 0
    for v in to_change:
        s = v["snippet"]
        # videos.update REPLACES the snippet; title + categoryId are required.
        # Omitting description/tags (sending blank/empty) is what clears them.
        new_snippet = {
            "title": s["title"],            # preserved exactly
            "categoryId": TARGET_CATEGORY_ID,
            "description": "",
            "tags": [],
        }
        # Preserve language fields if present (else they'd be cleared).
        for k in ("defaultLanguage", "defaultAudioLanguage"):
            if s.get(k):
                new_snippet[k] = s[k]
        try:
            youtube.videos().update(
                part="snippet", body={"id": v["id"], "snippet": new_snippet}
            ).execute()
            changed += 1
            log.info("Updated %s (%.40r)", v["id"], s.get("title", ""))
        except Exception as e:  # keep going; report at the end
            log.error("FAILED %s: %s", v["id"], e)

    print(f"\nDone. Updated {changed}/{len(to_change)} video(s).")


if __name__ == "__main__":
    main(apply="--apply" in sys.argv)
