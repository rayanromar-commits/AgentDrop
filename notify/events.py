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


def notify_low_stock(remaining: int, threshold: int) -> None:
    """Ping Slack the moment unused stories hit/cross the restock threshold.

    Fires at posting time (not just in the daily digest) so a restock can
    happen before the queue runs dry — repetitive/duplicate content is what
    gets a channel throttled. Sends one nudge per post while at or below the
    threshold, so the reminder escalates as the count keeps dropping.
    """
    if remaining > threshold:
        return
    tail = ("*out of stories* — produce/queue more now." if remaining <= 0
            else f"only *{remaining}* unused {'story' if remaining == 1 else 'stories'} left.")
    send_slack(f"📉 *Restock stories* — {tail}")
