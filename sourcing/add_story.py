"""
Quick helper to add manual stories.

Paste a Reddit story and it creates the correctly-formatted .txt file in
sourcing/manual_stories/ for you. Add several in one sitting, then
optionally push them to GitHub so Railway picks them up automatically.

Run it:  python3 sourcing/add_story.py
"""

import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STORIES_DIR = Path(__file__).resolve().parent / "manual_stories"


def slugify(text: str, maxlen: int = 50) -> str:
    """Make a tidy, file-safe slug from a title."""
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if len(s) > maxlen:
        s = s[:maxlen].rsplit("-", 1)[0]
    return s or "story"


def unique_path(slug: str) -> Path:
    """Return a non-colliding file path for this slug."""
    path = STORIES_DIR / f"{slug}.txt"
    n = 2
    while path.exists():
        path = STORIES_DIR / f"{slug}-{n}.txt"
        n += 1
    return path


def read_multiline(prompt: str) -> str:
    """Read multiple lines until the user types END on its own line."""
    print(prompt)
    print("  (paste the text, then type  END  on its own line and press Enter)")
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        # Accept END / end / End on its own line (and Ctrl-D also works).
        if line.strip().upper() == "END":
            break
        lines.append(line)
    return "\n".join(lines).strip()


def add_one() -> Path | None:
    STORIES_DIR.mkdir(exist_ok=True)
    print("\n" + "=" * 55)
    subreddit = input("Subreddit (e.g. AmItheAsshole, no 'r/'): ").strip()
    title = input("Title: ").strip()
    if not title:
        print("No title given — skipping.")
        return None
    body = read_multiline("Body:")
    if not body:
        print("No body given — skipping.")
        return None

    path = unique_path(slugify(title))
    contents = f"{title}\n"
    if subreddit:
        contents += f"subreddit: {subreddit}\n"
    contents += f"{body}\n"
    path.write_text(contents, encoding="utf-8")

    word_count = len(body.split())
    print(f"\n✅ Saved {path.name}  ({word_count} words)")
    if word_count < 100 or word_count > 500:
        print(f"   ⚠️  Note: {word_count} words is outside the 100-500 target — "
              "the screener may skip it.")
    return path


def push_to_github(new_files: list) -> None:
    print("\nPushing new stories to GitHub (Railway will redeploy)...")
    try:
        subprocess.run(["git", "add", "sourcing/manual_stories"],
                       cwd=PROJECT_ROOT, check=True)
        subprocess.run(
            ["git", "commit", "-m", f"Add {len(new_files)} manual story(ies)"],
            cwd=PROJECT_ROOT, check=True)
        subprocess.run(["git", "push", "origin", "main"],
                       cwd=PROJECT_ROOT, check=True)
        print("✅ Pushed. Railway will pick up the new stories on redeploy.")
    except subprocess.CalledProcessError as e:
        print(f"⚠️  Git step failed: {e}. You can push manually later.")


def main() -> None:
    print("Add manual stories. Leave the Title blank to finish.")
    added = []
    while True:
        path = add_one()
        if path is None:
            # Empty title/body = user wants to stop.
            break
        added.append(path)
        again = input("\nAdd another? [y/N] ").strip().lower()
        if again != "y":
            break

    if not added:
        print("\nNothing added. Bye.")
        return

    print(f"\nAdded {len(added)} story(ies):")
    for p in added:
        print(f"  - {p.name}")

    choice = input("\nPush to GitHub/Railway now? [y/N] ").strip().lower()
    if choice == "y":
        push_to_github(added)
    else:
        print("Skipped push. Run it later with: git add -A && git commit && git push")


if __name__ == "__main__":
    main()
