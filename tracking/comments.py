"""
Read the channel's comments and work out which ones are actually telling us
something.

The pipeline has always counted comments (a number in video_stats) without ever
reading one. That number is a poor signal on its own: this format ASKS a
question at the end, so most replies are people answering it ("orca obviously"),
which says the CTA worked but nothing about the video's quality.

What this module does is separate the three kinds of comment that matter:

  * ANSWERS to the outro question — the CTA working. Counted, not read.
  * JOKES, banter, copypasta and SARCASM — the bulk of engagement on a Short,
    and worthless as feedback. "yeah bro the hippo is definitely gonna beat a
    crocodile 💀" is not a factual correction, it is a joke, and a naive
    sentiment pass reads it as a complaint.
  * REAL FEEDBACK — a genuine complaint, correction or request. This is the
    rare one and the only kind that should ever change what we make.

Sarcasm is the whole difficulty. It is literal-negative and intent-positive (or
the reverse), it carries emoji that mean the opposite of their face value (💀 =
"this is funny", not "this is dead"), and no lexicon handles it. So the
classification is done by Claude, which is given the video's title for context —
without knowing the video was a ranking of shark predators, "wrong order" is
unreadable.

Everything fails SAFE: no API key, no scope, no quota, a bad response — all
return empty and the caller carries on. Comment reading must never be able to
break a drop.

One-off:  python3 -m tracking.comments
"""

import json
import os
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from agentdrop_common import first_json, setup_logging
from database import db

log = setup_logging()

MODEL = "claude-opus-4-8"          # same model the generator uses
MAX_VIDEOS = 25                   # newest N uploads are where live comments are
PER_VIDEO = 40                    # top-level threads per video
MAX_CLASSIFY = 300                # hard cap on one classification batch

_CLASSIFY_PROMPT = """You are reading YouTube comments on a silent "ranked \
clips" Short and sorting real feedback from noise. Every video ranks five \
subjects and ends by asking the viewer "Which one was your #1?".

Classify EACH comment into exactly one `kind`:
- "answer"    — answering the closing question, or just naming a pick ("orca", \
"definitely the hippo"). The CTA worked; there is nothing to act on.
- "joke"      — banter, memes, copypasta, absurdity, roleplay, or SARCASM. On \
this surface most engagement is jokes, and sarcasm is the trap: it says the \
opposite of what it means, and its emoji do too. 💀 ☠️ 😭 🤡 mean "this is \
funny", not distress. "yeah the buffalo is totally taking that crocodile 💀" is \
a JOKE, not a correction. Exaggerated praise ("this changed my life") is a joke. \
Treat a comment as a joke unless it would still make sense said plainly.
- "praise"    — sincere approval. Pleasant, but not actionable.
- "feedback"  — a SINCERE complaint, correction or criticism: the footage \
doesn't match the label, a fact is wrong, the text is too fast to read, the \
music is too loud, the ranking order is defended seriously. This is the only \
kind that should change what we make.
- "request"   — asking for a specific future topic ("do deep sea next").
- "spam"      — self-promo, links, bots, unrelated.

For "feedback" and "request" only, also give:
- `issue`: at most 12 words saying what to fix or make, in plain terms.
- `severity`: "high" if it claims something is WRONG or misleading (wrong \
animal, wrong fact, unreadable), "low" for taste and preference.

Be conservative. A comment is only "feedback" when a reasonable person would \
read it as sincere. When torn between joke and feedback, choose joke — acting \
on a joke teaches the channel the wrong lesson, while missing one costs \
nothing. Sarcastic praise is "joke", never "praise".

Return ONLY JSON:
{{"comments": [{{"i": <index>, "kind": "...", "issue": "...", "severity": "..."}}]}}

The comments, each with the video it is on:
{listing}"""


def _service():
    from upload.youtube_upload import get_authenticated_service
    return get_authenticated_service()


def fetch_comments(max_videos: int = MAX_VIDEOS,
                   per_video: int = PER_VIDEO) -> list[dict]:
    """Recent top-level comments across the newest uploads.

    Returns [{youtube_id, title, author, text, likes}], newest videos first.
    Empty on any failure — including comments being disabled on a video, which
    is a per-video 403 and must not abort the rest.
    """
    rows = [r for r in db.videos_by_status("uploaded") if r["youtube_id"]]
    if not rows:
        log.info("[comments] no uploaded videos to read.")
        return []
    rows = rows[-max_videos:]
    try:
        youtube = _service()
    except Exception as e:
        log.warning("[comments] no YouTube service (%s).", e)
        return []

    out: list[dict] = []
    for row in rows:
        try:
            resp = youtube.commentThreads().list(
                part="snippet", videoId=row["youtube_id"],
                maxResults=min(per_video, 100), order="time",
                textFormat="plainText").execute()
        except Exception as e:
            # Disabled comments / removed video / quota — skip this one only.
            log.debug("[comments] %s unavailable (%s).", row["youtube_id"], e)
            continue
        for item in resp.get("items", []):
            sn = (item.get("snippet", {})
                      .get("topLevelComment", {})
                      .get("snippet", {}))
            text = (sn.get("textDisplay") or "").strip()
            if not text:
                continue
            out.append({
                "youtube_id": row["youtube_id"],
                "title": row["title"] or "",
                "author": sn.get("authorDisplayName", ""),
                "text": text[:400],
                "likes": int(sn.get("likeCount", 0) or 0),
            })
    log.info("[comments] read %d comment(s) across %d video(s).",
             len(out), len(rows))
    return out


def classify(comments: list[dict]) -> list[dict]:
    """Tag each comment with a `kind` (+ issue/severity for real feedback).

    Returns the same list with fields added. Unclassified on any failure, which
    callers treat as "no feedback found" rather than as an error.
    """
    if not comments:
        return []
    load_dotenv()
    if not os.getenv("ANTHROPIC_API_KEY"):
        log.warning("[comments] ANTHROPIC_API_KEY not set — cannot tell a joke "
                    "from a complaint, so nothing is classified.")
        return []
    try:
        import anthropic
    except ImportError:
        return []

    batch = comments[:MAX_CLASSIFY]
    listing = "\n".join(
        f'{i}. [on "{c["title"][:60]}"] {c["text"]}' for i, c in enumerate(batch))
    try:
        resp = anthropic.Anthropic().messages.create(
            model=MODEL, max_tokens=4000,
            output_config={"effort": "medium"},
            messages=[{"role": "user",
                       "content": _CLASSIFY_PROMPT.format(listing=listing)}])
        txt = "".join(b.text for b in resp.content if b.type == "text")
        m = re.search(r"\{.*\}", txt, re.S)
        if not m:
            log.warning("[comments] classifier returned no JSON.")
            return []
        tagged = first_json(m.group(0)).get("comments") or []
    except Exception as e:
        log.warning("[comments] classification failed (%s).", e)
        return []

    by_i = {t.get("i"): t for t in tagged if isinstance(t, dict)}
    out = []
    for i, c in enumerate(batch):
        t = by_i.get(i) or {}
        out.append({**c,
                    "kind": str(t.get("kind") or "unknown"),
                    "issue": str(t.get("issue") or ""),
                    "severity": str(t.get("severity") or "")})
    return out


def summarize(tagged: list[dict]) -> dict:
    """Roll classified comments into the signal the rest of the pipeline uses.

    {counts, feedback: [...], requests: [...], answer_rate}

    `answer_rate` — the share of comments that answer the closing question — is
    the one number here that measures the VIDEO rather than the audience: it is
    how well the CTA is working, and it is why the outro was rebuilt.
    """
    counts: dict[str, int] = {}
    feedback, requests = [], []
    for c in tagged:
        k = c.get("kind", "unknown")
        counts[k] = counts.get(k, 0) + 1
        if k == "feedback" and c.get("issue"):
            feedback.append({"issue": c["issue"], "severity": c.get("severity", ""),
                             "likes": c.get("likes", 0), "text": c["text"][:160],
                             "title": c.get("title", "")})
        elif k == "request" and c.get("issue"):
            requests.append({"issue": c["issue"], "likes": c.get("likes", 0)})
    total = sum(counts.values())
    # Most-liked first: on a Short, likes are the closest thing to other
    # viewers agreeing that a complaint is real.
    feedback.sort(key=lambda f: (f["severity"] == "high", f["likes"]), reverse=True)
    requests.sort(key=lambda r: r["likes"], reverse=True)
    return {
        "counts": counts,
        "total": total,
        "answer_rate": round(counts.get("answer", 0) / total, 3) if total else 0.0,
        "feedback": feedback[:12],
        "requests": requests[:12],
        "as_of": date.today().isoformat(),
    }


def read_and_summarize() -> dict:
    """fetch -> classify -> summarize, the whole pass. {} if unavailable."""
    tagged = classify(fetch_comments())
    if not tagged:
        return {}
    summary = summarize(tagged)
    log.info("[comments] %d classified: %s | %d real feedback, %d requests.",
             summary["total"],
             ", ".join(f"{k}={v}" for k, v in sorted(summary["counts"].items())),
             len(summary["feedback"]), len(summary["requests"]))
    return summary


if __name__ == "__main__":
    db.init_db()
    s = read_and_summarize()
    print(json.dumps(s, indent=2, ensure_ascii=False) if s else "no comment data")
