"""
AgentDrop — entry point + orchestrator + scheduler.

Commands:
  python3 main.py            show current config
  python3 main.py produce    make ONE video and put it in the review queue
  python3 main.py upload      upload the next APPROVED video to YouTube
  python3 main.py tiktok      post the next video to TikTok
  python3 main.py stats       refresh + print performance stats
  python3 main.py digest      send the daily Slack performance digest now
  python3 main.py schedule    run continuously on your configured schedule

NOTE: 'produce' calls the TTS API and so SPENDS ElevenLabs credits.
'schedule' will do this automatically on a timer — only run it when you
intend AgentDrop to operate (and spend) on its own.
"""

import sys

from agentdrop_common import bootstrap_cloud_secrets, load_config, setup_logging
from database import db

log = setup_logging()


def show_config(config: dict) -> None:
    log.info("Subreddits     : %s", ", ".join(config["subreddits"]))
    log.info("Story source   : %s", config.get("story_source"))
    log.info("Approval mode  : %s", config["approval_mode"])
    log.info("Videos/day     : %s at %s", config["upload"]["videos_per_day"],
             ", ".join(config["upload"]["upload_times"]))
    log.info("Upload privacy : %s", config["upload"]["privacy_status"])


def _apply_ranking_performance_weight(candidates: list[dict], config: dict) -> None:
    """Reorder ranking candidates in place, favoring high-performing categories.

    Mirrors the story pipeline's bias (see produce_one_video): each category's
    age-normalized composite score (views/day + engagement + completion) is
    shrunk toward the global mean by sample size, so one lucky video can't
    dominate while data is thin. Every candidate then gets a random exploration
    base PLUS a bounded, category-scaled boost — the random base stands in for
    the crowd-upvote signal ranking lists don't have, keeping newer/unseen
    categories in play. No-op when there's no performance data yet, so the
    shuffled order from the source survives cold-start.

    Ranking items carry their category in the ``subreddit`` field, which is
    exactly what db.subreddit_performance() groups by — so the same aggregation
    the story channel learns from applies here unchanged.
    """
    import random

    perf = db.subreddit_performance()
    if not perf:
        return

    pcfg = config.get("performance", {})
    max_boost = pcfg.get("boost", 3.0)      # max points the category signal adds
    prior = pcfg.get("prior_weight", 1.5)   # pseudo-count for shrinkage

    scores = [d["score"] for d in perf.values()]
    global_mean = sum(scores) / len(scores)
    # Bayesian-style shrink toward the mean by sample size (small n -> trust
    # the mean more), matching produce_one_video.
    adj = {
        cat: (d["n"] * d["score"] + prior * global_mean) / (d["n"] + prior)
        for cat, d in perf.items()
    }
    max_s = max(adj.values()) or 1.0

    rng = random.Random()

    def _sel(c):
        # Unseen categories get the (shrunk) global mean, so they're explored
        # rather than starved. Random base = exploration; category term =
        # exploitation, both capped by the same `boost` knob.
        cat_score = adj.get(c["subreddit"], global_mean)
        return rng.random() * max_boost + (cat_score / max_s) * max_boost

    candidates.sort(key=_sel, reverse=True)

    top = candidates[0]
    log.info("[ranking] performance-weighted pick: category=%s (adj score %.2f) — %s",
             top["subreddit"], adj.get(top["subreddit"], global_mean), top["title"])


def _produce_ranking(config: dict):
    """Produce ONE cinematic 'Top 5' ranking Short (content_type=ranking).

    Pulls an unused ranking list, renders it with the Ken Burns / crossfade /
    voiceover renderer over NASA images, and queues it. Single video, no split.
    """
    import json as _json
    from sourcing.ranking_source import (fetch_stories as rank_fetch,
                                          youtube_title, mark_posted)
    from video.ranking_assemble import render_ranking_video
    from review.queue import submit_video

    db.init_db()
    sg = config.get("safeguards", {})
    max_per_day = sg.get("max_videos_per_day", 4)
    if db.videos_produced_today() >= max_per_day:
        log.warning("Daily cap reached (%d videos). Skipping ranking production.",
                    max_per_day)
        return None

    candidates = rank_fetch(config, skip_seen=True)
    if not candidates:
        log.warning("No fresh ranking lists available (dataset exhausted?).")
        return None

    # Close the learning loop: bias the pick toward categories the channel's
    # own view/retention/share data says are winning. Off -> shuffled order.
    if config.get("use_performance_weighting"):
        _apply_ranking_performance_weight(candidates, config)

    item = candidates[0]
    payload = _json.loads(item["body"])   # on-screen title stays payload["title"]
    log.info("Producing ranking Short %s: %s", item["post_id"], item["title"])
    video_path = render_ranking_video(item["post_id"], payload, config)
    # Give YouTube a VARIED title (metadata) so daily uploads don't look duplicate.
    # Prefers the wildly-distinct variants Claude baked into the dataset.
    item["title"] = youtube_title(item["title"], item["post_id"],
                                  payload.get("yt_titles"))
    result = submit_video(item, video_path, config)
    db.save_post(post_id=item["post_id"], subreddit=item["subreddit"],
                 title=item["title"], body=item["body"], score=0,
                 word_count=item.get("word_count", 0), status="used")
    # Also record in the durable committed ledger — the reliable anti-duplicate
    # store that survives DB resets/redeploys (keyed on the on-screen title).
    mark_posted(payload["title"], date=__import__("datetime").date.today().isoformat(),
                note="auto")
    log.info("Produced ranking -> %s (%s)", result["path"], result["status"])
    return [result]


def produce_one_video(config: dict):
    """Source -> screen -> rank -> narrate -> assemble -> queue.

    Produces ALL parts of the top-ranked story (one video for short
    stories, several "Part N" videos for long ones). Returns a list of
    produced video results, or None if nothing was made. Spends TTS.
    """
    if config.get("content_type", "story") == "ranking":
        return _produce_ranking(config)

    from sourcing.get_stories import fetch_stories
    from processing.screen import screen_story, clean_str
    from processing.rank import rank_stories
    from processing.split import num_parts, split_text
    from processing.hook import generate_hook
    from processing.title import generate_title, generate_series_titles
    from processing.punch_up import punch_up
    from processing.voice_direction import add_voice_direction
    from processing.condense import condense_body
    from voiceover.tts import synthesize, choose_voice
    from video.assemble import assemble_video
    from review.queue import submit_video

    db.init_db()

    # --- SAFEGUARD 1: daily video cap (checked before starting a story) ---
    sg = config.get("safeguards", {})
    max_per_day = sg.get("max_videos_per_day", 4)
    if db.videos_produced_today() >= max_per_day:
        log.warning("Daily cap reached (%d videos). Skipping production.",
                    max_per_day)
        return None

    # Gather fresh, unseen, passing stories.
    stories = fetch_stories(config, skip_seen=True)
    passing = [s for s in stories if screen_story(s, config)[0]]
    if not passing:
        log.warning("No new passing stories available right now.")
        return None

    ranked = rank_stories(passing)

    # Optional: bias toward subreddits that perform well. Uses the
    # age-normalized composite score (views/day + engagement), with
    # shrinkage toward the global mean so one lucky video doesn't dominate
    # while we still have only a handful of data points per subreddit.
    if config.get("use_performance_weighting"):
        perf = db.subreddit_performance()
        if perf:
            pcfg = config.get("performance", {})
            max_boost = pcfg.get("boost", 3.0)      # max points added to a score
            prior = pcfg.get("prior_weight", 1.5)   # pseudo-count for shrinkage

            scores = [d["score"] for d in perf.values()]
            global_mean = sum(scores) / len(scores)

            # Bayesian-style shrink: blend each subreddit toward the mean by
            # its sample size (small n -> trust the mean more).
            adj = {
                sub: (d["n"] * d["score"] + prior * global_mean) / (d["n"] + prior)
                for sub, d in perf.items()
            }
            max_s = max(adj.values()) or 1
            for s in ranked:
                # Unseen subreddits get the (shrunk) average, not zero, so
                # they're still explored rather than starved.
                sub_score = adj.get(s["subreddit"], global_mean)
                s["captivation_score"] += (sub_score / max_s) * max_boost
            ranked.sort(key=lambda s: s["captivation_score"], reverse=True)

    story = ranked[0]
    ctitle = clean_str(story["title"])
    cbody = clean_str(story["body"])
    words = len(f"{ctitle} {cbody}".split())

    # Decide how many parts this story becomes.
    split_cfg = config.get("splitting", {})
    if split_cfg.get("enabled"):
        # If the story is over the part cap's word ceiling, tighten it to fit
        # (Claude shortens it, keeping the arc) instead of skipping it. Cached;
        # fails safe to the original body (which may then still be skipped).
        ceiling = split_cfg.get("words_per_part", 375) * split_cfg.get("max_parts", 8)
        if words > ceiling:
            cbody = condense_body(story["post_id"], ctitle, cbody, ceiling, config)
            words = len(f"{ctitle} {cbody}".split())
        n = num_parts(words, split_cfg.get("words_per_part", 375),
                      split_cfg.get("max_parts", 8))
        if n is None:
            log.warning("Story too long even to split (%d words); skipping.", words)
            db.save_post(post_id=story["post_id"], subreddit=story["subreddit"],
                         title=story["title"], body=story["body"],
                         score=story.get("score", 0),
                         word_count=story.get("word_count", 0), status="skipped")
            return None
    else:
        n = 1

    # Write an AI cold-open hook (one per story, cached). It opens the FIRST
    # part only; because captions are synced to the narration, the hook is
    # both spoken and shown on screen. Falls back to None (title-first) on
    # any problem — see processing/hook.py.
    hook_line = generate_hook(story["post_id"], ctitle, cbody,
                              story["subreddit"], config)

    # Write an EXAGGERATED, superlative YouTube title (one per story, cached).
    # This is the DISPLAY title only — it drives homepage CTR and is NEVER
    # spoken; the narration keeps using the real story title (ctitle) for
    # context. Falls back to the raw title on any problem — see
    # processing/title.py.
    ai_title = generate_title(story["post_id"], ctitle, cbody,
                              story["subreddit"], config)
    display_title = ai_title or story["title"]

    # For a multi-part series, write a DISTINCT title per part (one API call)
    # so siblings don't share one headline — an identical title across parts is
    # a duplicate signal that helps land the later parts in the 0-view jail.
    # Falls back to the shared "base (Part i/n)" title on any problem.
    part_titles = None
    if n > 1:
        part_titles = generate_series_titles(story["post_id"], ctitle, cbody,
                                             story["subreddit"], n, config)

    def _opener(text: str, rest: str) -> str:
        """Join an opening line to the rest, avoiding doubled punctuation."""
        text = text.strip()
        sep = " " if text[-1:] in ".!?" else ". "
        return f"{text}{sep}{rest}"

    # A fixed closing call-to-action, spoken + shown on screen on the LAST
    # part only, to turn watchers into commenters/sharers (the engagement
    # signal that pushes a video past the algorithm's first test audience).
    outro_cfg = config.get("outro", {})
    outro_text = (outro_cfg.get("text", "") or "").strip() \
        if outro_cfg.get("enabled") else ""

    base_id = story["post_id"]

    # Build the spoken text per part. Part 1 leads with the hook (then the
    # title for context on multi-part series); later parts keep the "Part N"
    # cue so new viewers still have context. The body of each part first goes
    # through a light retention-beat pass (punch_up), then a voice-direction
    # pass that inserts ElevenLabs v3 audio tags ([sighs] etc.); the
    # hook/title/"Part N" cues are added AFTER, so they're never touched.
    body_chunks = split_text(cbody, n)
    chunks = []
    for i, bc in enumerate(body_chunks, 1):
        part_id = base_id if n == 1 else f"{base_id}_p{i}"
        bc = punch_up(part_id, bc, config)
        bc = add_voice_direction(part_id, bc, config)
        if n == 1:
            # Single video: the hook REPLACES the title as the opener.
            text = _opener(hook_line, bc) if hook_line else f"{ctitle}. {bc}"
        elif i == 1:
            # First of a series: hook, then the title for context, then Part 1.
            lead = _opener(hook_line, ctitle) if hook_line else ctitle
            text = f"{lead}. Part {i}. {bc}"
        else:
            text = f"{ctitle}. Part {i}. {bc}"
        # CTA goes on the final part only (never repeated across a series).
        if outro_text and i == n:
            text = f"{text} {outro_text}"
        chunks.append(text)

    log.info("Selected (score %.2f): %s  [%d part(s)]",
             story["captivation_score"], story["title"], n)

    budget = sg.get("monthly_tts_char_budget", 110000)
    results = []
    completed_all = True

    # Pick ONE voice for this whole story so a multi-part series keeps the
    # same narrator; the next story rotates to a different voice.
    voice = choose_voice(config)
    log.info("Narrator for this story: %s", voice.get("name"))

    for i, chunk in enumerate(chunks, 1):
        part_id = base_id if n == 1 else f"{base_id}_p{i}"

        # Resume support: skip parts already produced in a prior run.
        if db.video_exists(part_id):
            continue

        # --- SAFEGUARD 2: monthly TTS budget (hard money wall) ---
        char_count = len(chunk)
        used = db.tts_chars_this_month()
        if used + char_count > budget:
            log.warning("TTS budget reached (%d + %d > %d). Stopping at part %d; "
                        "will resume later.", used, char_count, budget, i)
            completed_all = False
            break

        synthesize(chunk, part_id, config, voice=voice)
        db.record_tts_usage(part_id, char_count)
        video_path = assemble_video(part_id, config, subreddit=story.get("subreddit"))

        if n == 1:
            part_title = display_title
        elif part_titles:
            # Distinct per-part title; still tag the part number for the viewer.
            part_title = f"{part_titles[i - 1]} (Part {i}/{n})"
        else:
            part_title = f"{display_title} (Part {i}/{n})"
        part_story = {**story, "post_id": part_id, "title": part_title, "body": chunk}
        result = submit_video(part_story, video_path, config)
        results.append(result)
        log.info("Produced part %d/%d -> %s (%s)",
                 i, n, result["path"], result["status"])

    # Mark the source post used only once every part is done.
    if completed_all:
        db.save_post(
            post_id=base_id, subreddit=story["subreddit"],
            title=story["title"], body=story["body"],
            score=story.get("score", 0), word_count=story.get("word_count", 0),
            status="used",
        )
    return results or None


# A multi-part series must not put two of its parts on YouTube within this
# many hours. At 3 uploads/day (~5-10h apart) this pushes each subsequent part
# to the NEXT day, so siblings never cluster into a same-day burst — the
# pattern YouTube reads as duplicate/repetitive content and drops to 0 views.
SERIES_SPACING_HOURS = 20


def upload_next_approved(config: dict):
    """Upload the oldest eligible video not yet on YouTube.

    "Eligible" adds a series-spacing guard on top of oldest-first: a part is
    skipped if another part of the SAME story was posted to YouTube within the
    last SERIES_SPACING_HOURS. If every waiting video is a too-recent sibling,
    we post nothing this slot (the parts wait for tomorrow) rather than firing
    a duplicate burst.
    """
    from pathlib import Path
    from upload.youtube_upload import upload_video
    from notify.events import notify_posted, notify_failed, notify_low_stock
    from sourcing.manual_source import archive_story, restock_status
    db.init_db()
    held_for_spacing = 0
    for row in db.videos_missing_platform("youtube"):
        if not Path(row["file_path"]).exists():
            log.warning("Approved video file missing (%s); marking 'missing' "
                        "and skipping.", row["file_path"])
            db.set_video_status(row["post_id"], "missing")
            continue
        # Series spacing: hold this part if a sibling was posted very recently.
        base = db.base_story_id(row["post_id"])
        if db.youtube_sibling_uploaded_since(base, SERIES_SPACING_HOURS):
            held_for_spacing += 1
            continue
        try:
            vid = upload_video(row, config)
            notify_posted("YouTube", row["title"],
                          f"https://youtube.com/watch?v={vid}")
            # Retire the source script so it's never reused (avoids the
            # repetitive-content penalties that throttle a channel). If a
            # story was actually retired, nudge Slack when stock runs low.
            if archive_story(row["post_id"]):
                min_days = config.get("notifications", {}).get(
                    "restock_min_days", 4)
                notify_low_stock(restock_status(config), min_days)
            return vid
        except Exception as e:
            log.error("[youtube] upload failed for %s: %s", row["post_id"], e)
            notify_failed("YouTube upload", f"{row['post_id']}: {e}")
            return None
    if held_for_spacing:
        log.info("No eligible video: %d queued part(s) held so series siblings "
                 "stay >%dh apart (they post on a later day).",
                 held_for_spacing, SERIES_SPACING_HOURS)
    else:
        log.info("No videos waiting for YouTube upload.")
    return None


def upload_next_tiktok(config: dict):
    """Post the oldest video not yet on TikTok (its own schedule)."""
    from pathlib import Path
    from upload.tiktok_upload import upload_video_tiktok
    from notify.events import notify_posted, notify_failed, notify_low_stock
    from sourcing.manual_source import archive_story, restock_status
    db.init_db()
    if not config.get("tiktok", {}).get("enabled"):
        log.info("TikTok disabled in config; skipping.")
        return None
    for row in db.videos_missing_platform("tiktok"):
        if not Path(row["file_path"]).exists():
            continue
        try:
            pid = upload_video_tiktok(row, config)
            mode = config["tiktok"].get("mode", "inbox")
            where = "TikTok drafts" if mode == "inbox" else "TikTok"
            notify_posted(where, row["title"])
            # Idempotent: no-op if YouTube already archived this story.
            if archive_story(row["post_id"]):
                min_days = config.get("notifications", {}).get(
                    "restock_min_days", 4)
                notify_low_stock(restock_status(config), min_days)
            return pid
        except Exception as e:
            log.error("[tiktok] upload failed for %s: %s", row["post_id"], e)
            notify_failed("TikTok upload", f"{row['post_id']}: {e}")
            return None
    log.info("No videos waiting for TikTok.")
    return None


def refresh_performance(config: dict) -> None:
    from tracking.stats import refresh_stats, print_report
    db.init_db()
    refresh_stats()
    print_report()


def send_digest(config: dict) -> None:
    """Build + send the daily Slack performance digest."""
    from notify.digest import send_daily_digest
    db.init_db()
    send_daily_digest(config)


def start_scheduler(config: dict) -> None:
    """Run AgentDrop continuously on the configured schedule."""
    from datetime import datetime
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger
    from zoneinfo import ZoneInfo

    # Ensure the DB exists and one-time migrations run at startup (e.g. the
    # TikTok backlog-skip), before any scheduled upload fires.
    db.init_db()

    tz_name = config.get("timezone", "America/New_York")
    tz = ZoneInfo(tz_name)
    # Diagnostic: confirm in the logs which timezone is actually active.
    log.info("Scheduler timezone resolved to: %s | local time now: %s",
             tz_name, datetime.now(tz).strftime("%Y-%m-%d %H:%M %Z"))
    sched = BlockingScheduler(timezone=tz)
    times = config["upload"]["upload_times"]

    # Produce a fresh batch each day, a bit before the first upload time.
    first_hh = int(times[0].split(":")[0])
    prod_hour = (first_hh - 1) % 24
    n_per_day = config["upload"]["videos_per_day"]

    # Optional start date — the agent stays idle until this date, so a manually
    # scheduled first post isn't doubled up by the automation.
    from datetime import date
    _start = config.get("start_date")

    def _active_today() -> bool:
        if not _start:
            return True
        try:
            sd = date.fromisoformat(str(_start))
        except Exception:
            return True
        if datetime.now(tz).date() < sd:
            log.info("[scheduler] before start_date %s — idle today.", sd)
            return False
        return True

    def production_job():
        if not _active_today():
            return
        log.info("[scheduler] Production run (target buffer: %d queued videos).",
                 n_per_day)
        # Pull the freshest performance data BEFORE ranking so story selection
        # always uses the most up-to-date completion / shares / views available
        # (not just whatever the last 6-hourly refresh happened to leave behind).
        # Fail-safe: a stats hiccup must never block production.
        try:
            refresh_performance(config)
        except Exception as e:
            log.warning("[scheduler] pre-production stats refresh failed (%s); "
                        "ranking on last known data.", e)
        while True:
            # Stop once enough DISTINCT stories are queued — not enough videos.
            # The series-spacing rule posts at most one part per story per day,
            # so a full day of uploads needs n_per_day *different* stories in
            # the buffer, each contributing one part. Counting distinct stories
            # (a 3-part story counts as 1) keeps production making fresh stories
            # until the buffer can feed a diverse, non-clustered upload day.
            queued_stories = db.distinct_queued_stories()
            if queued_stories >= n_per_day:
                log.info("[scheduler] %d distinct stories queued (>= %d); "
                         "production done.", queued_stories, n_per_day)
                break
            try:
                res = produce_one_video(config)
            except Exception as e:  # keep the scheduler alive on errors
                log.error("[scheduler] production error: %s", e)
                break
            if not res:
                log.info("[scheduler] production stopped (cap/budget/no stories).")
                break

    # NOTE: each CronTrigger MUST be given timezone=tz explicitly. APScheduler
    # does not apply the scheduler's timezone to a pre-built trigger, so an
    # untagged CronTrigger captures the container's local zone (UTC on Railway)
    # and fires hours off from the configured America/Chicago times.
    sched.add_job(production_job, CronTrigger(hour=prod_hour, minute=0, timezone=tz),
                  id="produce", name="daily production")

    # Upload one approved video at each configured time (idle before start_date).
    def upload_job():
        if _active_today():
            upload_next_approved(config)
    for t in times:
        hh, mm = (int(x) for x in t.split(":"))
        sched.add_job(upload_job,
                      CronTrigger(hour=hh, minute=mm, timezone=tz),
                      id=f"upload_{t}", name=f"upload at {t}")

    # Refresh performance stats every 6 hours.
    sched.add_job(lambda: refresh_performance(config),
                  CronTrigger(hour="*/6", timezone=tz), id="stats", name="stats refresh")

    # TikTok cross-posting on its OWN schedule (independent of YouTube times).
    tcfg = config.get("tiktok", {})
    if tcfg.get("enabled"):
        for t in tcfg.get("post_times", []):
            th, tm = (int(x) for x in t.split(":"))
            sched.add_job(lambda: upload_next_tiktok(config),
                          CronTrigger(hour=th, minute=tm, timezone=tz),
                          id=f"tiktok_{t}", name=f"tiktok post at {t}")

    # Daily Slack digest (channel totals + deltas + top videos + restock signal).
    ncfg = config.get("notifications", {})
    digest_time = ncfg.get("digest_time", "20:00")
    if ncfg.get("enabled"):
        dh, dm = (int(x) for x in digest_time.split(":"))
        sched.add_job(lambda: send_digest(config),
                      CronTrigger(hour=dh, minute=dm, timezone=tz),
                      id="digest", name="daily digest")

    log.info("Scheduler started. Production at %02d:00; uploads at %s; "
             "stats every 6h; digest at %s. Approval mode: %s. Ctrl+C to stop.",
             prod_hour, ", ".join(times),
             digest_time if ncfg.get("enabled") else "off",
             config["approval_mode"])
    if config["approval_mode"] == "manual":
        log.info("Manual mode: videos are produced into the review queue but "
                 "NOT uploaded until you approve them (python3 -m review.review).")
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler stopped.")


def main() -> None:
    bootstrap_cloud_secrets()  # recreate Google cred files from env (cloud)
    config = load_config()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "show"

    if cmd == "show":
        show_config(config)
    elif cmd == "produce":
        produce_one_video(config)
    elif cmd == "upload":
        upload_next_approved(config)
    elif cmd == "tiktok":
        upload_next_tiktok(config)
    elif cmd == "stats":
        refresh_performance(config)
    elif cmd == "digest":
        send_digest(config)
    elif cmd == "schedule":
        start_scheduler(config)
    else:
        log.error("Unknown command '%s'. Use: show | produce | upload | tiktok | "
                  "stats | digest | schedule", cmd)


if __name__ == "__main__":
    main()
