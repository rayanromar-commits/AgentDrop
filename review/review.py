"""
Step 7 — interactive review tool.

Walks you through every PENDING video one at a time. For each, it shows
the proposed title/description, lets you open and watch it, then asks
you to approve or reject.

Run it (from the project root):  python3 -m review.review

Commands at the prompt:
  o = open/play the video
  a = approve  (moves it to review/approved/, ready to upload)
  r = reject   (moves it to review/rejected/)
  s = skip     (leave it pending, decide later)
  q = quit
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdrop_common import setup_logging
from database import db
from review.queue import move_to_status

log = setup_logging()


def main() -> None:
    db.init_db()
    pending = db.videos_by_status("pending")

    if not pending:
        print("\nNo videos are waiting for review. 🎉")
        return

    print(f"\nYou have {len(pending)} video(s) to review.\n")

    for row in pending:
        print("=" * 60)
        print(f"TITLE      : {row['title']}")
        print(f"SUBREDDIT  : r/{row['subreddit']}")
        print(f"FILE       : {row['file_path']}")
        print("-" * 60)
        print("DESCRIPTION:")
        print(row["description"])
        print("=" * 60)

        while True:
            choice = input("[o]pen  [a]pprove  [r]eject  [s]kip  [q]uit > ").strip().lower()
            if choice == "o":
                subprocess.run(["open", row["file_path"]])
            elif choice == "a":
                move_to_status(row["post_id"], "approved")
                print("✅ Approved — moved to review/approved/\n")
                break
            elif choice == "r":
                move_to_status(row["post_id"], "rejected")
                print("❌ Rejected — moved to review/rejected/\n")
                break
            elif choice == "s":
                print("⏭  Skipped — still pending.\n")
                break
            elif choice == "q":
                print("Bye.")
                return
            else:
                print("Please type o, a, r, s, or q.")

    print("Review session complete.")


if __name__ == "__main__":
    main()
