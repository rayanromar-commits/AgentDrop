"""
AgentDrop — entry point.

Right now (Step 1) this just proves the project is set up correctly:
it loads your config.yaml and prints a summary. As we build each
piece, this file will grow into the full pipeline + scheduler.

Run it with:   python3 main.py
"""

from agentdrop_common import load_config, setup_logging


def main() -> None:
    log = setup_logging()
    log.info("AgentDrop starting up...")

    config = load_config()

    log.info("Config loaded successfully. Current settings:")
    log.info("  Subreddits        : %s", ", ".join(config["subreddits"]))
    log.info(
        "  Story word range  : %s-%s words",
        config["min_word_count"],
        config["max_word_count"],
    )
    log.info("  Approval mode     : %s", config["approval_mode"])
    log.info(
        "  Videos per day    : %s at %s",
        config["upload"]["videos_per_day"],
        ", ".join(config["upload"]["upload_times"]),
    )
    log.info("  Upload privacy    : %s", config["upload"]["privacy_status"])

    log.info("Step 1 scaffold is working. Nothing else happens yet.")


if __name__ == "__main__":
    main()
