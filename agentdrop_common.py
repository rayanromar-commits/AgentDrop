"""
Shared helpers used across AgentDrop modules:
  - load_config(): reads config.yaml into a Python dictionary
  - setup_logging(): configures clear, timestamped logs

Every other part of AgentDrop will import from here so we only define
these things once.
"""

import logging
import os
import sys
from pathlib import Path

import yaml

# The folder this file lives in = the project root.
PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"

# Where working files (audio, videos, review queue) are written. Locally
# this is the project root; in the cloud set AGENTDROP_DATA_DIR to the
# persistent volume (e.g. /data) so produced videos survive restarts
# between the production step and their later upload slot.
DATA_DIR = Path(os.getenv("AGENTDROP_DATA_DIR", PROJECT_ROOT))


def bootstrap_cloud_secrets() -> None:
    """In the cloud, recreate Google credential FILES from env vars.

    Railway/servers store secrets as environment variables, not files.
    If GOOGLE_CLIENT_SECRET_JSON / GOOGLE_TOKEN_JSON are set and the
    files are missing, write them so the rest of the code works
    unchanged. No-op locally where the files already exist.
    """
    mapping = {
        "GOOGLE_CLIENT_SECRET_JSON": PROJECT_ROOT / "client_secret.json",
        "GOOGLE_TOKEN_JSON": PROJECT_ROOT / "token.json",
    }
    for env_name, path in mapping.items():
        content = os.getenv(env_name)
        if content and not path.exists():
            path.write_text(content, encoding="utf-8")


def load_config(path: Path = CONFIG_PATH) -> dict:
    """Read config.yaml and return it as a dictionary."""
    if not path.exists():
        raise FileNotFoundError(
            f"Could not find config file at {path}. "
            "Make sure config.yaml is in the project folder."
        )
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """Set up logging that prints to the screen with timestamps."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-7s  %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    return logging.getLogger("agentdrop")
