"""
Compose and send the once-a-day performance digest to Slack.

Contents:
  - channel totals (subscribers / views / videos) with 1d / 7d / 30d deltas
  - top 3 videos by views (title + link)
  - how many unused manual stories remain, with a restock warning

Test on demand:  python3 main.py digest
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdrop_common import setup_logging
from database import db
from notify.slack import send_slack
from sourcing.manual_source import unused_story_count
from tracking.stats import fetch_channel_stats, refresh_stats

log = setup_logging()


def _fmt_delta(d) -> str:
    """Format a single delta value: '—' if unknown, else '+1,234' / '-5'."""
    if d is None:
        return "—"
    return f"{d:+,}"


def _delta_str(metrics: dict, field: str) -> str:
    """'(+12 today · +80 7d · +300 30d)' for one metric."""
    parts = []
    for label in ("1d", "7d", "30d"):
        window = metrics["deltas"].get(label)
        val = None if window is None else window[field]
        suffix = {"1d": "today", "7d": "7d", "30d": "30d"}[label]
        parts.append(f"{_fmt_delta(val)} {suffix}")
    return "(" + " · ".join(parts) + ")"


def build_digest(config: dict) -> str:
    """Gather metrics and return the Slack message text."""
    ncfg = config.get("notifications", {})
    top_n = ncfg.get("top_n", 3)
    restock_threshold = ncfg.get("restock_threshold", 5)

    # Refresh the snapshots the digest reads from.
    refresh_stats()             # per-video views/likes/comments
    fetch_channel_stats()       # channel subscribers/views/videos

    today = datetime.now().strftime("%a %b %-d, %Y")
    lines = [f"📊 *AgentDrop Daily Digest* — {today}", ""]

    metrics = db.channel_metrics()
    if metrics is None:
        lines.append("_No channel stats available yet._")
    else:
        lines.append(
            f"*Subscribers:* {metrics['subscribers']:,}  "
            f"{_delta_str(metrics, 'subscribers')}"
        )
        lines.append(
            f"*Total views:* {metrics['views']:,}  "
            f"{_delta_str(metrics, 'views')}"
        )
        lines.append(
            f"*Videos:* {metrics['videos']:,}  "
            f"{_delta_str(metrics, 'videos')}"
        )

    top = db.top_videos(top_n)
    if top:
        lines += ["", f"*Top {len(top)} videos:*"]
        for i, v in enumerate(top, 1):
            title = (v["title"] or "(untitled)")[:60]
            link = (f"https://youtube.com/watch?v={v['youtube_id']}"
                    if v["youtube_id"] else "")
            lines.append(f"{i}. {title} — {v['views']:,} views  {link}".rstrip())

    unused = unused_story_count()
    lines += ["", f"*Unused stories:* {unused}"]
    if unused <= restock_threshold:
        lines.append(f"⚠️ *Restock soon* — only {unused} stories left to produce.")

    return "\n".join(lines)


def send_daily_digest(config: dict) -> bool:
    """Build the digest and send it to Slack."""
    text = build_digest(config)
    ok = send_slack(text)
    if ok:
        log.info("Daily digest sent to Slack.")
    return ok
