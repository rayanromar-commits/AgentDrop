"""
Send messages to Slack via an Incoming Webhook.

Set up once: create a Slack app -> Incoming Webhooks -> add to a channel,
then put the webhook URL in your environment:

    SLACK_WEBHOOK_URL=https://hooks.slack.com/services/XXX/YYY/ZZZ

(in .env locally, and as a Railway variable in the cloud)
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests
from dotenv import load_dotenv

from agentdrop_common import setup_logging

log = setup_logging()


def send_slack(text: str) -> bool:
    """Post a Slack message. Returns True on success.

    No-op (logs a warning) if SLACK_WEBHOOK_URL isn't configured, so the
    rest of the pipeline keeps running even when notifications aren't set up.
    """
    load_dotenv()
    url = os.getenv("SLACK_WEBHOOK_URL")
    if not url:
        log.warning("SLACK_WEBHOOK_URL not set — skipping Slack notification.")
        return False

    resp = requests.post(url, json={"text": text}, timeout=20)
    if resp.status_code != 200:
        log.error("Slack webhook failed (%s): %s", resp.status_code, resp.text[:200])
        return False
    return True
