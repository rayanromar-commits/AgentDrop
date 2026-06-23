"""
Shared helpers used across AgentDrop modules:
  - load_config(): reads config.yaml into a Python dictionary
  - setup_logging(): configures clear, timestamped logs

Every other part of AgentDrop will import from here so we only define
these things once.
"""

import logging
import sys
from pathlib import Path

import yaml

# The folder this file lives in = the project root.
PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"


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
