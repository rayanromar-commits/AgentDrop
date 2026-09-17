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

Env: RUN_NOW=1 makes 'schedule' do one catch-up produce+upload at startup
(for recovering a missed cloud slot), then follow the normal schedule.

NOTE: 'produce' calls the TTS API and so SPENDS ElevenLabs credits.
'schedule' will do this automatically on a timer — only run it when you
intend AgentDrop to operate (and spend) on its own.
"""

import sys

from agentdrop_common import bootstrap_cloud_secrets, load_config, setup_logging
from database import db

log = setup_logging()


def show_config(config: dict) -> None:
    ctype = config.get("content_type", "clipranking")
    log.info("Content type   : %s", ctype)
    log.info("Datasets       : %s", config.get(ctype, {}).get("dataset_dir", "-"))
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


def _produce_clipranking(config: dict):
    """Produce ONE silent ranked-clip Short (content_type=clipranking).

    Pulls an unused ranked-clip list, renders it over licensed stock footage with
    no narration, and queues it. Single video, no split. Deliberately a sibling of
    _produce_ranking rather than a rewrite of it: the space format stays intact on
    disk so the pivot is revertible with a one-line config change.
    """
    import json as _json
    from sourcing.clip_ranking_source import (fetch_stories as clip_fetch,
                                              youtube_title, mark_posted)
    from video.clip_assemble import render_clip_video
    from review.queue import submit_video

    db.init_db()
    sg = config.get("safeguards", {})
    max_per_day = sg.get("max_videos_per_day", 4)
    if db.videos_produced_today() >= max_per_day:
        log.warning("Daily cap reached (%d videos). Skipping clip production.",
                    max_per_day)
        return None

    candidates = clip_fetch(config, skip_seen=True)
    if not candidates:
        log.warning("No fresh ranked-clip lists available (dataset exhausted?).")
        return None

    # Same learning loop as the space channel: bias the pick toward the
    # categories the channel's own engagement data says are winning.
    if config.get("use_performance_weighting"):
        _apply_ranking_performance_weight(candidates, config)

    item = candidates[0]
    payload = _json.loads(item["body"])
    log.info("Producing ranked-clip Short %s: %s", item["post_id"], item["title"])
    video_path = render_clip_video(item["post_id"], payload, config)
    # The genre template IS the YouTube title here — no variant rotation.
    item["title"] = youtube_title(item["title"], item["post_id"])
    result = submit_video(item, video_path, config)
    db.save_post(post_id=item["post_id"], subreddit=item["subreddit"],
                 title=item["title"], body=item["body"], score=0,
                 word_count=item.get("word_count", 0), status="used")
    mark_posted(payload["title"], date=__import__("datetime").date.today().isoformat(),
                note="auto")
    log.info("Produced ranked clip -> %s (%s)", result["path"], result["status"])
    return [result]


def restock_ranking_datasets(config: dict) -> int:
    """Keep the ranking dataset buffer stocked so production never runs dry.

    Counts how many FRESH (unposted) ranking lists remain; if that's below
    ``ranking.autorefill.min_buffer`` it generates new datasets — biased toward
    the topic groups the channel's own engagement data says are winning — up to
    ``target``. Returns the number of new datasets written. A no-op when the
    buffer is healthy, when disabled, or for the story channel; never raises
    (a refill hiccup must not block the day's production).
    """
    ctype = config.get("content_type", "story")
    if ctype not in ("ranking", "clipranking"):
        return 0
    acfg = config.get(ctype, {}).get("autorefill", {})
    if not acfg.get("enabled", True):
        return 0
    min_buffer = int(acfg.get("min_buffer", 4))
    target = int(acfg.get("target", 12))

    try:
        if ctype == "clipranking":
            from sourcing.clip_ranking_source import fetch_stories as rank_fetch
            from sourcing import clip_ranking_generate as rgen
        else:
            from sourcing.ranking_source import fetch_stories as rank_fetch
            from sourcing import ranking_generate as rgen

        db.init_db()
        fresh = len(rank_fetch(config, skip_seen=True))
        if fresh >= min_buffer:
            log.info("[restock] %d fresh ranking list(s) >= min_buffer %d; no refill.",
                     fresh, min_buffer)
            return 0
        need = max(target - fresh, 0)
        log.info("[restock] only %d fresh list(s) (< %d); generating %d "
                 "performance-weighted dataset(s)...", fresh, min_buffer, need)
        try:
            perf = db.subreddit_performance()
        except Exception:
            perf = None
        paths = rgen.generate_batch(need, perf=perf)
        log.info("[restock] wrote %d new ranking dataset(s).", len(paths))
        return len(paths)
    except Exception as e:
        log.error("[restock] dataset refill failed (%s); continuing with what "
                  "exists.", e)
        return 0


def produce_one_video(config: dict):
    """Produce one video for the configured content type.

    The Reddit-story pipeline that used to live here was removed on 2026-09-15
    along with the StoryDropper and FootyEmoji channels. Only the ranking
    formats remain: `clipranking` (live) and `ranking` (the retired space
    format, kept so the pivot is reversible — see
    sourcing/_retired_space/README.md).
    """
    ctype = config.get("content_type", "clipranking")
    if ctype == "clipranking":
        return _produce_clipranking(config)
    if ctype == "ranking":
        return _produce_ranking(config)
    log.error("Unknown content_type %r — nothing to produce. Use 'clipranking' "
              "or 'ranking'.", ctype)
    return None

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
    from sourcing.ledger import archive_story, restock_status
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
            # The dataset was already retired in the posted ledger at produce
            # time, so this only backfills the YouTube id onto that entry. The
            # low-stock nudge is unconditional: the runway is what matters at
            # posting time, not who happened to retire the list.
            archive_story(row["post_id"], config, youtube_id=vid)
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
    from sourcing.ledger import restock_status
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
        # Auto-refill the ranking dataset pool FIRST so a production run can
        # never start empty (this is what silently killed the 2026-07-22 drop).
        # No-op for the story channel and when the buffer is already healthy.
        restock_ranking_datasets(config)
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

    # One-shot catch-up: set RUN_NOW=1 in the environment to produce + upload
    # ONCE at startup, then carry on with the normal schedule. This is how a
    # missed slot gets recovered in the cloud, where there is no shell to run
    # `python main.py produce` in. Guarded by a dated marker so a container
    # restart can't post twice — but REMOVE THE VARIABLE once it has run: a
    # redeploy wipes the marker along with the rest of the SQLite file.
    import os
    if os.getenv("RUN_NOW", "").strip().lower() in ("1", "true", "yes"):
        today = datetime.now(tz).date().isoformat()
        if db.get_meta("run_now_done") == today:
            log.info("[scheduler] RUN_NOW set but already ran today (%s); "
                     "skipping. Remove RUN_NOW from the environment.", today)
        else:
            log.info("[scheduler] RUN_NOW set — running one catch-up "
                     "production + upload now.")
            try:
                production_job()
                upload_job()
                db.set_meta("run_now_done", today)
            except Exception as e:
                log.error("[scheduler] RUN_NOW catch-up failed: %s", e)
            log.info("[scheduler] RUN_NOW catch-up finished — remove RUN_NOW "
                     "so the next redeploy doesn't post an extra video.")

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
    elif cmd == "restock":
        restock_ranking_datasets(config)
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
        log.error("Unknown command '%s'. Use: show | produce | restock | upload | "
                  "tiktok | stats | digest | schedule", cmd)


if __name__ == "__main__":
    main()
