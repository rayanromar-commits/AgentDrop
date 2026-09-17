"""
The learning loop — everything the channel knows, in one place, refreshed daily.

Before this, "learning" meant one number: a per-category score derived from our
own views. That is a slow and narrow teacher. A channel averaging 1,000 views
needs roughly forever to learn anything from itself, and category is only one of
the choices a video makes.

This pulls four independent sources into a single snapshot:

  1. OUR OWN RESULTS, per category and — new — per TITLE SUPERLATIVE. The house
     template forces one of nine words ("Deadliest", "Weirdest", ...), and that
     word is a real editorial choice we were never measuring.
  2. WHAT THE AUDIENCE SAYS — comment feedback and topic requests, with jokes
     and sarcasm filtered out (tracking/comments.py).
  3. WHAT BEATS ITS OWN CHANNEL among the ten reference channels
     (sourcing/rival_miner.py) — evidence about subject and framing that does
     not have to wait for our own sample to grow.
  4. WHAT IS TRENDING in the wider lane (sourcing/trend_miner.py).

The snapshot is written to disk AND summarised into a short briefing that is
injected into the generator's prompt, so tomorrow's topics are chosen with
today's evidence. Every source is independently optional: whatever is reachable
contributes, whatever is not is left out, and an empty snapshot changes nothing.

    python3 -m tracking.insights           # build + print today's snapshot
    python3 -m tracking.insights show      # print the last saved one
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdrop_common import post_id_prefix, setup_logging
from database import db

log = setup_logging()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = PROJECT_ROOT / "tracking" / "insights.json"

SUPERLATIVES = ["deadliest", "most dangerous", "most venomous", "strongest",
                "biggest", "fastest", "weirdest", "rarest", "scariest"]

MIN_SAMPLE = 3          # below this our own numbers are noise, not signal


def _superlative(title: str) -> str:
    low = (title or "").lower()
    for sup in sorted(SUPERLATIVES, key=len, reverse=True):
        if sup in low:
            return sup
    return ""


def own_superlative_performance(prefix: str = "") -> dict:
    """Our own score per title superlative: {sup: {n, score, avg_views}}.

    Same composite score the category ranker uses (views/day, engagement,
    completion), grouped by the template word instead of the subject. With a
    daily upload this reaches a usable sample in a couple of weeks, and until
    then callers gate on `n`.
    """
    titles = {r["post_id"]: (r["title"] or "")
              for r in db.videos_by_status("uploaded")}
    agg: dict[str, list[dict]] = {}
    for v in db.video_performance(prefix=prefix or None):
        sup = _superlative(titles.get(v["post_id"], ""))
        if sup:
            agg.setdefault(sup, []).append(v)
    out = {}
    for sup, vs in agg.items():
        n = len(vs)
        out[sup] = {
            "n": n,
            "score": round(sum(v["score"] for v in vs) / n, 2),
            "avg_views": round(sum(v["views"] for v in vs) / n, 1),
            "avg_shares": round(sum(v["shares"] for v in vs) / n, 2),
        }
    return out


def build(config: dict | None = None, refresh_rivals: bool = True) -> dict:
    """Gather every source into one snapshot and save it.

    Never raises: each source is wrapped, because this runs on a schedule and a
    failure here must not touch production.
    """
    config = config or {}
    lcfg = config.get("learning", {}) or {}
    snap: dict = {"built_at": datetime.now(timezone.utc).isoformat()}

    prefix = post_id_prefix(config)
    try:
        snap["categories"] = db.subreddit_performance(prefix=prefix)
    except Exception as e:
        log.warning("[insights] own category stats unavailable: %s", e)
        snap["categories"] = {}

    try:
        snap["superlatives"] = own_superlative_performance(prefix=prefix)
    except Exception as e:
        log.warning("[insights] own superlative stats unavailable: %s", e)
        snap["superlatives"] = {}

    snap["comments"] = {}
    if lcfg.get("read_comments", True):
        try:
            from tracking.comments import read_and_summarize
            snap["comments"] = read_and_summarize()
        except Exception as e:
            log.warning("[insights] comments unavailable: %s", e)

    snap["rivals"] = {}
    if lcfg.get("mine_rivals", True):
        try:
            from sourcing import rival_miner
            mult = lcfg.get("rival_outlier_multiple")
            if mult:
                rival_miner.OUTLIER_MULT = float(mult)
            rivals = rival_miner.fetch_rivals(refresh=refresh_rivals)
            # Mark which breakouts our stock-footage pipeline could actually
            # build, so the briefing can quote only those.
            keep = set(rival_miner.winning_subjects(limit=40))
            outliers = rivals.get("outliers", [])[:25]
            for o in outliers:
                o["servable"] = o.get("subject") in keep
            snap["rivals"] = {
                "outliers": outliers,
                "superlatives": rival_miner.superlative_scores(refresh=False),
                "channels": rivals.get("channels", []),
            }
        except Exception as e:
            log.warning("[insights] rival mining unavailable: %s", e)

    try:
        from sourcing.trend_miner import winning_titles
        snap["trending"] = winning_titles(limit=15)
    except Exception as e:
        log.warning("[insights] trend mining unavailable: %s", e)
        snap["trending"] = []

    try:
        SNAPSHOT.write_text(json.dumps(snap, indent=2, ensure_ascii=False),
                            encoding="utf-8")
    except Exception as e:
        log.warning("[insights] could not save snapshot: %s", e)
    try:
        db.set_meta("insights_built_at", snap["built_at"])
    except Exception:
        pass

    log.info("[insights] built: %d categories, %d superlatives, %d rival "
             "outlier(s), %d trending, comments=%s",
             len(snap.get("categories", {})), len(snap.get("superlatives", {})),
             len(snap.get("rivals", {}).get("outliers", [])),
             len(snap.get("trending", [])),
             snap.get("comments", {}).get("total", 0))
    return snap


def load() -> dict:
    """The last saved snapshot, or {} if there isn't one."""
    try:
        return json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    except Exception:
        return {}


def best_superlatives(snap: dict | None = None) -> list[str]:
    """Template superlatives worth favouring, best first.

    Blends our own measured score with the rivals' outperformance, because
    early on we have almost no data of our own and they have plenty. Our own
    numbers only enter once a superlative has MIN_SAMPLE videos behind it.
    """
    snap = snap if snap is not None else load()
    own = snap.get("superlatives", {}) or {}
    rival = (snap.get("rivals", {}) or {}).get("superlatives", {}) or {}
    if not own and not rival:
        return []

    own_scores = {k: v["score"] for k, v in own.items()
                  if v.get("n", 0) >= MIN_SAMPLE}
    def norm(d):
        if not d:
            return {}
        top = max(d.values()) or 1.0
        return {k: v / top for k, v in d.items()}
    on, rn = norm(own_scores), norm(rival)
    keys = set(on) | set(rn)
    # Our own audience outranks the rivals' where we actually have a sample.
    blended = {k: 0.65 * on.get(k, rn.get(k, 0)) + 0.35 * rn.get(k, on.get(k, 0))
               for k in keys}
    return [k for k, _ in sorted(blended.items(), key=lambda kv: -kv[1])]


def briefing(snap: dict | None = None, max_chars: int = 2600) -> str:
    """A short evidence block for the generator's prompt.

    Deliberately written as EVIDENCE, not instructions: the same discipline the
    trend miner uses. The model is told what happened and what viewers asked
    for, and left to choose a subject — a list of commands here would collapse
    topic variety within a week.
    """
    snap = snap if snap is not None else load()
    if not snap:
        return ""
    parts: list[str] = []

    sups = best_superlatives(snap)
    if sups:
        own = snap.get("superlatives", {})
        detail = ", ".join(
            f"{s}" + (f" (ours: {own[s]['n']} videos, score {own[s]['score']})"
                      if s in own else "")
            for s in sups[:5])
        parts.append("Framings currently travelling furthest, best first: "
                     + detail + ".")

    cats = snap.get("categories", {}) or {}
    ranked = sorted(((c, d) for c, d in cats.items() if d.get("n", 0) >= 2),
                    key=lambda kv: -kv[1].get("score", 0))
    if ranked:
        top = ", ".join(f"{c} (score {d['score']:.1f}, n={d['n']})"
                        for c, d in ranked[:5])
        parts.append(f"Our own best-performing categories so far: {top}.")
        if len(ranked) > 5:
            worst = ", ".join(c for c, _ in ranked[-3:])
            parts.append(f"Our weakest so far: {worst}.")

    # Only outliers we could actually BUILD. Unfiltered, this list is almost
    # entirely scraped-UGC comedy ("Ranking Best Cats Stealing The Spotlight",
    # 135x) — real evidence about a lane that is closed to a stock-footage
    # channel, and pointing the generator at it just produces topics we have to
    # serve with calm stock under a title promising chaos.
    outliers = [o for o in (snap.get("rivals", {}) or {}).get("outliers", [])
                if o.get("servable")]
    if outliers:
        lines = "; ".join(f"{o['title'][:70]} ({o['multiple']}x its channel's "
                          f"median)" for o in outliers[:8])
        parts.append("On directly comparable channels, these recent videos beat "
                     "their OWN channel's median by the widest margin — read "
                     "them for what KIND of subject is landing, do not remake "
                     "one: " + lines + ".")

    comments = snap.get("comments", {}) or {}
    reqs = comments.get("requests", [])
    if reqs:
        parts.append("Viewers have asked for: "
                     + "; ".join(r["issue"] for r in reqs[:6]) + ".")
    fb = [f for f in comments.get("feedback", []) if f.get("severity") == "high"]
    if fb:
        parts.append("Complaints worth avoiding in future lists: "
                     + "; ".join(f["issue"] for f in fb[:5]) + ".")

    return ("\n\n".join(parts))[:max_chars]


def digest_lines(snap: dict | None = None) -> list[str]:
    """Slack digest section — what the channel learned today."""
    snap = snap if snap is not None else load()
    if not snap:
        return []
    out = ["", "*What the agent learned:*"]
    sups = best_superlatives(snap)
    if sups:
        out.append(f"• Best framings: {', '.join(sups[:3])}")
    c = snap.get("comments", {}) or {}
    if c.get("total"):
        counts = c.get("counts", {})
        out.append(
            f"• Comments: {c['total']} read — {counts.get('answer', 0)} answered "
            f"the CTA ({c.get('answer_rate', 0) * 100:.0f}%), "
            f"{counts.get('joke', 0)} jokes, {len(c.get('feedback', []))} real "
            f"feedback, {len(c.get('requests', []))} requests")
        for f in c.get("feedback", [])[:3]:
            out.append(f"   ⚠️ {f['issue']} — \"{f['text'][:70]}\"")
        for r in c.get("requests", [])[:3]:
            out.append(f"   💡 asked for: {r['issue']}")
    riv = [o for o in (snap.get("rivals", {}) or {}).get("outliers", [])
           if o.get("servable")]
    if riv:
        out.append(f"• Rival breakouts: {riv[0]['title'][:60]} "
                   f"({riv[0]['multiple']}x their median)")
    return out


if __name__ == "__main__":
    db.init_db()
    if len(sys.argv) > 1 and sys.argv[1] == "show":
        snap = load()
    else:
        snap = build()
    print(json.dumps({k: v for k, v in snap.items() if k != "trending"},
                     indent=2, ensure_ascii=False)[:4000])
    print("\n--- generator briefing ---\n")
    print(briefing(snap) or "(no evidence yet)")
