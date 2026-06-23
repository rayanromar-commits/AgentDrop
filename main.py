"""
AgentDrop — entry point + orchestrator + scheduler.

Commands:
  python3 main.py            show current config
  python3 main.py produce    make ONE video and put it in the review queue
  python3 main.py upload      upload the next APPROVED video
  python3 main.py stats       refresh + print performance stats
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
    """Source -> screen -> rank -> narrate -> assemble -> queue. Spends TTS."""
    from sourcing.get_stories import fetch_stories
    from processing.screen import screen_story, clean_text
    from processing.rank import rank_stories
    from voiceover.tts import synthesize
    from video.assemble import assemble_video
    from review.queue import submit_video

    db.init_db()

    # Gather fresh, unseen, passing stories.
    stories = fetch_stories(config, skip_seen=True)
    passing = [s for s in stories if screen_story(s, config)[0]]
    if not passing:
        log.warning("No new passing stories available right now.")
        return None

    # --- SAFEGUARD 1: daily video cap ---
    sg = config.get("safeguards", {})
    max_per_day = sg.get("max_videos_per_day", 4)
    produced_today = db.videos_produced_today()
    if produced_today >= max_per_day:
        log.warning("Daily cap reached (%d/%d videos). Skipping production.",
                    produced_today, max_per_day)
        return None

    ranked = rank_stories(passing)

    # Optional: bias toward subreddits that perform well.
    if config.get("use_performance_weighting"):
        perf = db.subreddit_performance()
        if perf:
            max_v = max((d["avg_views"] for d in perf.values()), default=0) or 1
            for s in ranked:
                boost = perf.get(s["subreddit"], {}).get("avg_views", 0) / max_v
                s["captivation_score"] += boost * 3.0  # up to +3
            ranked.sort(key=lambda s: s["captivation_score"], reverse=True)

    story = ranked[0]
    log.info("Selected (score %.2f): %s", story["captivation_score"], story["title"])

    # Narrate (spends ElevenLabs credits).
    text = clean_text(story["title"], story["body"])

    # --- SAFEGUARD 2: monthly TTS character budget ---
    char_count = len(text)
    budget = sg.get("monthly_tts_char_budget", 110000)
    used = db.tts_chars_this_month()
    if used + char_count > budget:
        log.warning(
            "TTS monthly budget would be exceeded (%d + %d > %d). "
            "Skipping to protect spend.", used, char_count, budget)
        return None

    synthesize(text, story["post_id"], config)
    db.record_tts_usage(story["post_id"], char_count)

    # Build the video and queue it.
    video_path = assemble_video(story["post_id"], config)
    result = submit_video(story, video_path, config)

    # Mark the source post used so it's never repeated.
    db.save_post(
        post_id=story["post_id"], subreddit=story["subreddit"],
        title=story["title"], body=story["body"],
        score=story.get("score", 0), word_count=story.get("word_count", 0),
        status="used",
    )
    log.info("Produced video -> %s (status: %s)", result["path"], result["status"])
    return result


def upload_next_approved(config: dict):
    """Upload one approved video, if any."""
    from upload.youtube_upload import upload_video
    db.init_db()
    approved = db.videos_by_status("approved")
    if not approved:
        log.info("No approved videos to upload.")
        return None
    return upload_video(approved[0], config)


def refresh_performance(config: dict) -> None:
    from tracking.stats import refresh_stats, print_report
    db.init_db()
    refresh_stats()
    print_report()


def start_scheduler(config: dict) -> None:
    """Run AgentDrop continuously on the configured schedule."""
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(config.get("timezone", "America/New_York"))
    sched = BlockingScheduler(timezone=tz)
    times = config["upload"]["upload_times"]

    # Produce a fresh batch each day, a bit before the first upload time.
    first_hh = int(times[0].split(":")[0])
    prod_hour = (first_hh - 1) % 24
    n_per_day = config["upload"]["videos_per_day"]

    def production_job():
        log.info("[scheduler] Daily production: up to %d video(s).", n_per_day)
        for _ in range(n_per_day):
            try:
                # produce_one_video returns None when a safeguard or lack
                # of stories stops it — no point continuing the batch then.
                if produce_one_video(config) is None:
                    log.info("[scheduler] production stopped early (cap/budget/no stories).")
                    break
            except Exception as e:  # keep the scheduler alive on errors
                log.error("[scheduler] production error: %s", e)

    sched.add_job(production_job, CronTrigger(hour=prod_hour, minute=0),
                  id="produce", name="daily production")

    # Upload one approved video at each configured time.
    for t in times:
        hh, mm = (int(x) for x in t.split(":"))
        sched.add_job(lambda: upload_next_approved(config),
                      CronTrigger(hour=hh, minute=mm),
                      id=f"upload_{t}", name=f"upload at {t}")

    # Refresh performance stats every 6 hours.
    sched.add_job(lambda: refresh_performance(config),
                  CronTrigger(hour="*/6"), id="stats", name="stats refresh")

    log.info("Scheduler started. Production at %02d:00; uploads at %s; "
             "stats every 6h. Approval mode: %s. Ctrl+C to stop.",
             prod_hour, ", ".join(times), config["approval_mode"])
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
    elif cmd == "stats":
        refresh_performance(config)
    elif cmd == "schedule":
        start_scheduler(config)
    else:
        log.error("Unknown command '%s'. Use: show | produce | upload | stats | schedule", cmd)


if __name__ == "__main__":
    main()
