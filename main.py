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


def produce_one_video(config: dict):
    """Source -> screen -> rank -> narrate -> assemble -> queue.

    Produces ALL parts of the top-ranked story (one video for short
    stories, several "Part N" videos for long ones). Returns a list of
    produced video results, or None if nothing was made. Spends TTS.
    """
    from sourcing.get_stories import fetch_stories
    from processing.screen import screen_story, clean_str
    from processing.rank import rank_stories
    from processing.split import num_parts, split_text
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

    # Speak the TITLE at the start of every part (with a "Part N" cue for
    # multi-part series, so each video has context for new viewers).
    body_chunks = split_text(cbody, n)
    chunks = []
    for i, bc in enumerate(body_chunks, 1):
        if n == 1:
            chunks.append(f"{ctitle}. {bc}")
        else:
            chunks.append(f"{ctitle}. Part {i}. {bc}")

    log.info("Selected (score %.2f): %s  [%d part(s)]",
             story["captivation_score"], story["title"], n)

    budget = sg.get("monthly_tts_char_budget", 110000)
    base_id = story["post_id"]
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
        video_path = assemble_video(part_id, config)

        part_title = story["title"] if n == 1 else f"{story['title']} (Part {i}/{n})"
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


def upload_next_approved(config: dict):
    """Upload the oldest video not yet on YouTube whose file still exists."""
    from pathlib import Path
    from upload.youtube_upload import upload_video
    from notify.events import notify_posted, notify_failed
    db.init_db()
    for row in db.videos_missing_platform("youtube"):
        if not Path(row["file_path"]).exists():
            log.warning("Approved video file missing (%s); marking 'missing' "
                        "and skipping.", row["file_path"])
            db.set_video_status(row["post_id"], "missing")
            continue
        try:
            vid = upload_video(row, config)
            notify_posted("YouTube", row["title"],
                          f"https://youtube.com/watch?v={vid}")
            return vid
        except Exception as e:
            log.error("[youtube] upload failed for %s: %s", row["post_id"], e)
            notify_failed("YouTube upload", f"{row['post_id']}: {e}")
            return None
    log.info("No videos waiting for YouTube upload.")
    return None


def upload_next_tiktok(config: dict):
    """Post the oldest video not yet on TikTok (its own schedule)."""
    from pathlib import Path
    from upload.tiktok_upload import upload_video_tiktok
    from notify.events import notify_posted, notify_failed
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

    def production_job():
        log.info("[scheduler] Production run (target buffer: %d queued videos).",
                 n_per_day)
        while True:
            # Stop once enough videos are queued for the day's uploads.
            backlog = (len(db.videos_by_status("approved"))
                       + len(db.videos_by_status("pending")))
            if backlog >= n_per_day:
                log.info("[scheduler] %d videos queued (>= %d); production done.",
                         backlog, n_per_day)
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

    # Upload one approved video at each configured time.
    for t in times:
        hh, mm = (int(x) for x in t.split(":"))
        sched.add_job(lambda: upload_next_approved(config),
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
