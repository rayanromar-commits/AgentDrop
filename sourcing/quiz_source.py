"""
Quiz story source — emoji-riddle rounds ("Guess the X by emoji").

Drops into the same switchboard the other sources use: exposes
`fetch_stories(config, skip_seen)` returning the standard story-dict contract
(post_id, subreddit, title, body, score, over_18, word_count), so the rest of
the pipeline treats a quiz round-set like any other "story".

One "story" = one Short = a ROUND SET of N questions sampled from a category
dataset (e.g. sourcing/quiz_data/football.json). The questions travel as JSON
in the `body` field; video/quiz_assemble.py parses them to render the slides.
`subreddit` is repurposed as the category tag ("football") so existing
per-category stats/selection keep working.

Config (config.yaml):
  quiz:
    dataset: sourcing/quiz_data/football.json   # which category to pull
    questions_per_video: 8                       # clues per Short
    batch: 5                                      # candidate sets per fetch
"""

import hashlib
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdrop_common import setup_logging
from database import db

log = setup_logging()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATASETS = ["sourcing/quiz_data/football.json"]

# Rotating, curiosity-gap titles so every upload is different (identical titles
# read as duplicate content and get throttled — the StoryDropper lesson).
# {subj} = singular category (footballer/club/legend), {subjs} = plural.
TITLE_TEMPLATES = [
    # questions
    "Can you guess it? 🤔", "How many {subjs} can you name? ⚽",
    "Did you get them all right? ✅", "Can YOU beat this quiz? 🔥",
    "Do you actually know your {subjs}? 👀", "Real fan or fraud? 🤨",
    # challenges / difficulty
    "Only 1% can name these {subjs} 🤯", "Bet you can't get 5/5 😏",
    "Nobody gets #4… 👀", "Impossible {subj} quiz? 🧠",
    "Casuals get 0/5 😅", "This is harder than it looks 👀",
    "5/5 = certified genius 🏆", "Even legends struggle with this ⚽",
    # hooks / statements
    "These emojis broke everyone 🤯", "You'll rage at number 3 😤",
    "Guess all 5 or you're not a real fan 🔥", "#1 fooled everybody 😳",
    "The emojis say it all… 🤔", "Miss one and you fail 💀",
    # score / engagement
    "Comment your score 👇", "Drop your score below ⬇️",
    "What did you get? 👀", "Tag someone who'd fail this 😂",
    # emoji-guess framing
    "Guess the {subj} from the emojis ⚡", "5 {subjs}, 5 emojis — go! 🚀",
    "Name the {subj} in 3 seconds ⏱️", "Emoji {subj} challenge 🔥",
]


def _pick_title(category: str, seed: str) -> str:
    """Deterministic-but-varied title per video (seeded by the round-set id) —
    diverse structures so YouTube doesn't read repeated titles as duplicates."""
    subj = category
    subjs = category + "s"
    return random.Random(seed).choice(TITLE_TEMPLATES).format(subj=subj, subjs=subjs)


def _dataset_paths(config: dict) -> list[str]:
    """The list of quiz-type datasets to rotate through (config quiz.datasets),
    falling back to a single quiz.dataset, then the default."""
    q = config.get("quiz", {})
    if q.get("datasets"):
        return list(q["datasets"])
    if q.get("dataset"):
        return [q["dataset"]]
    return DEFAULT_DATASETS


def _load_dataset(config: dict) -> dict:
    """Load the NEXT quiz-type dataset in rotation, so consecutive videos differ
    (players -> clubs -> legends -> ...) — variety YouTube rewards over one
    format repeated. The rotation counter persists across restarts."""
    paths = _dataset_paths(config)
    idx = db.next_rotation_index("quiz_dataset") % len(paths)
    rel = paths[idx]
    path = (PROJECT_ROOT / rel).resolve()
    if not path.exists():
        raise FileNotFoundError(
            f"Quiz dataset not found: {path}. Check quiz.datasets in config.yaml."
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    if not data.get("questions"):
        raise ValueError(f"Quiz dataset {path.name} has no 'questions'.")
    return data


def _set_id(category: str, questions: list[dict]) -> str:
    """Deterministic id for a round set (order-independent) so re-runs and the
    skip-seen de-dup treat the same combination of answers as one video."""
    key = "|".join(sorted(q["answer"] for q in questions))
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]
    return f"quiz_{category}_{h}"


def fetch_stories(config: dict, skip_seen: bool = True) -> list[dict]:
    """Return candidate quiz round-sets as story dicts (freshest-first)."""
    data = _load_dataset(config)
    qcfg = config.get("quiz", {})
    per_video = int(qcfg.get("questions_per_video", 8))
    batch = int(qcfg.get("batch", 5))
    category = data.get("category", "quiz")
    prompt = data.get("prompt", "GUESS THE ANSWER")

    pool = data["questions"]
    if len(pool) < per_video:
        raise ValueError(
            f"Dataset '{category}' has {len(pool)} questions but "
            f"questions_per_video={per_video}. Add more or lower the setting."
        )

    rng = random.Random()
    stories: list[dict] = []
    attempts = 0
    seen_ids: set[str] = set()
    # Sample distinct, unseen round-sets until we have `batch` candidates.
    while len(stories) < batch and attempts < batch * 40:
        attempts += 1
        picks = rng.sample(pool, per_video)
        post_id = _set_id(category, picks)
        if post_id in seen_ids:
            continue
        if skip_seen and db.post_already_seen(post_id):
            continue
        seen_ids.add(post_id)
        body = json.dumps({"prompt": prompt, "questions": picks},
                          ensure_ascii=False)
        stories.append({
            "post_id": post_id,
            "subreddit": category,          # repurposed as the category tag
            "title": _pick_title(category, post_id),
            "body": body,                   # JSON payload for quiz_assemble
            "score": 0,
            "over_18": False,
            "word_count": per_video * 10,   # nominal; quiz path skips length filters
        })

    log.info("Quiz source: %d candidate '%s' round-set(s) of %d questions.",
             len(stories), category, per_video)
    return stories


if __name__ == "__main__":
    from agentdrop_common import load_config
    db.init_db()
    cfg = load_config()
    cfg.setdefault("quiz", {})
    out = fetch_stories(cfg)
    for s in out[:2]:
        payload = json.loads(s["body"])
        print(f"\n{s['post_id']}  [{s['subreddit']}]  {s['title']}")
        for q in payload["questions"]:
            print(f"   {q['emoji']}  ->  {q['answer']}   ({q['clue']})")
