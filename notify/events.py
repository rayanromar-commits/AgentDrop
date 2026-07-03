"""One-off Slack pings for pipeline events (uploads, failures)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from notify.slack import send_slack


def notify_posted(platform: str, title: str, link: str = "") -> None:
    """Ping Slack that a video went out to a platform."""
    title = (title or "").replace(" #Shorts", "")[:80]
    send_slack(f"✅ *Posted to {platform}:* {title}\n{link}".rstrip())


def notify_failed(stage: str, detail: str) -> None:
    """Ping Slack that part of the pipeline failed (e.g. a rejected upload)."""
    send_slack(f"⚠️ *{stage} failed* — {detail}")


def notify_low_stock(status: dict, min_days: float) -> None:
    """Ping Slack when the queue's RUNWAY drops to/below `min_days`.

    Measured in UPLOADS/days, not stories — a story fans out into ~3 videos, so
    upload runway is the truer signal. `status` is sourcing.manual_source.
    restock_status(): {stories, uploads, uploads_per_day, days_runway}. Fires at
    posting time (not just the daily digest) so a restock can happen before the
    queue runs dry; one nudge per post while at/below the threshold, so it
    escalates as the runway keeps shrinking.
    """
    if status["days_runway"] > min_days:
        return
    if status["uploads"] <= 0:
        tail = "*out of uploads* — produce/queue more stories now."
    else:
        tail = (f"only *{status['uploads']}* uploads (~{status['days_runway']} "
                f"days) left across {status['stories']} stories.")
    send_slack(f"📉 *Restock stories* — {tail}")
