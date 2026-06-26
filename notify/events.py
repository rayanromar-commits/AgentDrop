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
