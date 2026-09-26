"""
Why this channel is stuck at ~1,000 views, and what to change tomorrow.

Every video lands in the same band. That is the Shorts "seed test": a video is
shown to a small pool, and it graduates to a bigger one only on the signals
YouTube names as Shorts ranking inputs — the share of viewers who CHOSE to keep
watching, average view duration, average % viewed, and engagement. A video can
fail that test in exactly three ways, and they need opposite fixes:

  HOOK       people are served it and swipe immediately.
  RETENTION  they start, then leave part-way.
  SPREAD     they watch it all and do nothing — no like, no share, no comment.

Telling them apart is the whole job here, because "views are flat" looks
identical in all three cases. The evidence:

  engaged_ratio = engagedViews / views
      Since 2026-03-31 `views` counts every start however brief, and the old
      definition became `engagedViews`. Their ratio is the closest the API gets
      to Studio's "viewed vs swiped away", which has no API metric at all.
  hook_hold     audienceWatchRatio in the first ~5% of the video.
  avg_view_pct  completion.
  shares / comments / likes per 1,000 views.

Measured 2026-09-25, and the reason this module exists: the ranked-clip videos
run 20-35% engaged views while the channel's older space videos ran 52-60% on
the same audience. Completion is FINE (62-85%). So the binding constraint is
the first second, not the edit and not the subject.

The adjuster is deliberately timid. Five data points a day will happily support
any conclusion you like, so: one knob at a time, hard limits per knob, a
minimum sample before anything moves, and every change written down with the
number that caused it. `learning.auto_adjust: false` stops it dead.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdrop_common import post_id_prefix, setup_logging
from database import db

log = setup_logging()

# --- thresholds ---------------------------------------------------------------
# Deliberately round numbers, not tuned constants: they are decision boundaries
# for "which of three problems do we have", not a model of the algorithm.
GOOD_ENGAGED_RATIO = 0.45      # below this, the video is being skipped
GOOD_COMPLETION = 55.0         # avg_view_pct; below this they leave part-way
GOOD_SHARES_PER_1K = 0.5       # the spread signal this channel has never had
MIN_VIDEOS = 3                 # fewer than this is noise, not evidence

# --- the knobs the agent may turn ---------------------------------------------
# name -> (default, min, max, which diagnosis moves it, step)
KNOBS: dict[str, dict] = {
    "target_seconds": {
        "default": 20.0, "min": 15.0, "max": 24.0, "step": -2.0,
        "fixes": "RETENTION",
        "why": "a shorter cut finishes before the point people were leaving",
    },
    "min_best_quality": {
        "default": 4.0, "min": 3.0, "max": 5.0, "step": 1.0,
        "fixes": "HOOK",
        "why": "demand a more arresting opening shot before a list may ship",
    },
    "share_cta": {
        "default": 1.0, "min": 0.0, "max": 1.0, "step": 1.0,
        "fixes": "SPREAD",
        "why": "ask for the share outright, since nobody is sharing unprompted",
    },
}


def _rows(prefix: str, limit: int) -> list[dict]:
    """Latest stats snapshot per video for this content format, newest first."""
    conn = db.get_connection()
    try:
        cur = conn.execute(
            """
            SELECT s.* FROM video_stats s
            JOIN (SELECT post_id, MAX(id) AS top FROM video_stats
                  GROUP BY post_id) last
              ON last.top = s.id
            WHERE s.post_id LIKE ? AND s.views > 0
            ORDER BY s.id DESC LIMIT ?
            """,
            (f"{prefix}%", limit),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


# The ranked-clip format's titles all start this way, and nothing else on the
# channel does — the old space videos are plain statements. That one word is how
# we tell our own format's videos apart on a channel that carries both.
LIVE_TITLE_PREFIX = "Ranking "


def _live_rows(limit: int) -> list[dict]:
    """Recent videos of this format, read from the CHANNEL rather than the DB.

    The DB is not a reliable record here: Railway's filesystem is ephemeral, so
    a redeploy wipes video_stats and the agent would wake up with no memory of
    how anything performed. The channel always knows. Returns [] on any API
    problem, and the caller falls back to whatever the DB has.
    """
    try:
        from tracking.analytics import fetch_retention, fetch_video_analytics
        from upload.youtube_upload import get_authenticated_service
        svc = get_authenticated_service()
        ch = svc.channels().list(part="contentDetails", mine=True).execute()
        pl = ch["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
        items = svc.playlistItems().list(
            part="contentDetails,snippet", playlistId=pl,
            maxResults=min(50, limit * 3)).execute().get("items", [])
    except Exception as e:
        log.info("[jail] live channel read failed (%s); using the DB.", e)
        return []

    wanted = [i for i in items
              if str(i["snippet"].get("title", "")).startswith(LIVE_TITLE_PREFIX)][:limit]
    ids = [i["contentDetails"]["videoId"] for i in wanted]
    if not ids:
        return []
    try:
        stats = svc.videos().list(part="statistics,snippet",
                                  id=",".join(ids)).execute().get("items", [])
        analytics = fetch_video_analytics(ids)
    except Exception as e:
        log.info("[jail] live stats read failed (%s); using the DB.", e)
        return []

    rows = []
    for v in stats:
        st, a = v.get("statistics", {}), analytics.get(v["id"], {})
        ret = fetch_retention(v["id"]) or {}
        rows.append({
            "post_id": v["id"], "youtube_id": v["id"],
            "views": int(st.get("viewCount", 0) or 0),
            "likes": int(st.get("likeCount", 0) or 0),
            "comments": int(st.get("commentCount", 0) or 0),
            "engaged_views": a.get("engaged_views"),
            "avg_view_pct": a.get("avg_view_pct"),
            "shares": a.get("shares"),
            "hook_hold": ret.get("hook_hold"),
            "loop_ratio": ret.get("loop_ratio"),
        })
    return [r for r in rows if r["views"] > 0]


def _avg(vals: list[float]) -> float | None:
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def measure(config: dict | None = None, limit: int = 10) -> dict:
    """Average the signals over the most recent videos of the current format."""
    prefix = post_id_prefix(config)
    source = "channel"
    rows = _live_rows(limit)
    if len(rows) < MIN_VIDEOS:
        db_rows = _rows(prefix, limit)
        if len(db_rows) > len(rows):
            rows, source = db_rows, "database"
    if not rows:
        return {"n": 0, "prefix": prefix, "source": source}

    views = sum(r["views"] or 0 for r in rows) or 1
    engaged = [(r["engaged_views"] / r["views"])
               for r in rows if r.get("engaged_views") and r["views"]]
    return {
        "n": len(rows),
        "prefix": prefix,
        "source": source,
        "views_avg": views / len(rows),
        "engaged_ratio": _avg(engaged),
        "hook_hold": _avg([r.get("hook_hold") for r in rows]),
        "loop_ratio": _avg([r.get("loop_ratio") for r in rows]),
        "completion": _avg([r.get("avg_view_pct") for r in rows]),
        "shares_per_1k": 1000.0 * sum(r.get("shares") or 0 for r in rows) / views,
        "comments_per_1k": 1000.0 * sum(r.get("comments") or 0 for r in rows) / views,
        "likes_per_1k": 1000.0 * sum(r.get("likes") or 0 for r in rows) / views,
    }


def diagnose(config: dict | None = None, limit: int = 10) -> dict:
    """Which of the three failures is binding, with the numbers behind it.

    Order matters: a video nobody watches past the first second cannot be fixed
    by asking for shares, so HOOK is tested first and SPREAD last.
    """
    m = measure(config, limit)
    if m.get("n", 0) < MIN_VIDEOS:
        return {**m, "diagnosis": "UNKNOWN",
                "evidence": f"only {m.get('n', 0)} measured video(s); "
                            f"need {MIN_VIDEOS}"}

    er, comp = m.get("engaged_ratio"), m.get("completion")
    if er is not None and er < GOOD_ENGAGED_RATIO:
        return {**m, "diagnosis": "HOOK",
                "evidence": f"only {er:.0%} of views engaged (want "
                            f"{GOOD_ENGAGED_RATIO:.0%}+) — served and swiped"}
    if comp is not None and comp < GOOD_COMPLETION:
        return {**m, "diagnosis": "RETENTION",
                "evidence": f"completion {comp:.0f}% (want "
                            f"{GOOD_COMPLETION:.0f}%+) — they leave part-way"}
    if m.get("shares_per_1k", 0) < GOOD_SHARES_PER_1K:
        return {**m, "diagnosis": "SPREAD",
                "evidence": f"{m['shares_per_1k']:.2f} shares per 1k views and "
                            f"{m['comments_per_1k']:.2f} comments — watched, "
                            f"then nothing"}
    return {**m, "diagnosis": "HEALTHY",
            "evidence": "no single bottleneck stands out"}


# --- knobs --------------------------------------------------------------------

def current_knobs() -> dict:
    """The knob values in force, falling back to their defaults."""
    out = {k: v["default"] for k, v in KNOBS.items()}
    try:
        saved = json.loads(db.get_meta("jail_knobs") or "{}")
    except Exception:
        saved = {}
    for k, v in (saved.items() if isinstance(saved, dict) else []):
        if k in out:
            try:
                out[k] = float(v)
            except (TypeError, ValueError):
                pass
    return out


def _clamp(name: str, value: float) -> float:
    spec = KNOBS[name]
    return max(spec["min"], min(spec["max"], value))


def decide(diag: dict, knobs: dict | None = None) -> dict | None:
    """The single knob to turn for this diagnosis, or None to leave it alone.

    Returns {knob, old, new, reason}. None when there is nothing to do: too
    little evidence, a healthy channel, or the relevant knob already at its
    limit — in which case the problem is the content, not a setting, and
    pretending otherwise would just add noise.
    """
    knobs = knobs or current_knobs()
    d = diag.get("diagnosis")
    if d in (None, "UNKNOWN", "HEALTHY"):
        return None
    for name, spec in KNOBS.items():
        if spec["fixes"] != d:
            continue
        old = knobs.get(name, spec["default"])
        new = _clamp(name, old + spec["step"])
        if abs(new - old) < 1e-9:
            log.info("[jail] %s says turn %s, but it is already at its limit "
                     "(%.1f) — this needs better content, not a setting.",
                     d, name, old)
            return None
        return {"knob": name, "old": old, "new": new,
                "reason": f"{d}: {diag.get('evidence', '')} — {spec['why']}"}
    return None


def apply(config: dict | None = None, limit: int = 10) -> dict:
    """Diagnose, turn at most one knob, and report what happened.

    Called from the daily learn pass. Writes nothing when auto-adjust is off;
    the diagnosis is still returned, so the briefing and the digest can say
    what is wrong even when the agent is not allowed to act on it.
    """
    diag = diagnose(config, limit)
    knobs = current_knobs()
    out = {"diagnosis": diag, "knobs": knobs, "change": None}

    auto = (config or {}).get("learning", {}).get("auto_adjust", True)
    change = decide(diag, knobs)
    if not change:
        log.info("[jail] %s — %s. No knob turned.", diag.get("diagnosis"),
                 diag.get("evidence", ""))
        return out
    if not auto:
        log.info("[jail] %s — would turn %s %.1f -> %.1f, but "
                 "learning.auto_adjust is off.", diag.get("diagnosis"),
                 change["knob"], change["old"], change["new"])
        out["change"] = {**change, "applied": False}
        return out

    knobs[change["knob"]] = change["new"]
    try:
        db.set_meta("jail_knobs", json.dumps(knobs))
        db.set_meta("jail_last_change", json.dumps(change))
    except Exception as e:
        log.warning("[jail] could not persist knobs (%s); change not kept.", e)
        out["change"] = {**change, "applied": False}
        return out

    log.info("[jail] %s -> turning %s from %.1f to %.1f (%s)",
             diag.get("diagnosis"), change["knob"], change["old"],
             change["new"], change["reason"])
    out["knobs"] = knobs
    out["change"] = {**change, "applied": True}
    return out


def knobs_for_render(config: dict) -> dict:
    """`clipranking` config with the learned knobs folded in.

    Keeps the knobs in ONE place: production asks for this instead of reading
    config directly, so a knob the learner moved actually reaches the renderer.
    """
    cfg = dict(config.get("clipranking", {}) or {})
    k = current_knobs()
    cfg["target_seconds"] = k.get("target_seconds", cfg.get("target_seconds", 20))
    cfg["min_best_quality"] = k.get("min_best_quality", 4)
    cfg["share_cta"] = bool(k.get("share_cta", 1.0))
    return cfg


if __name__ == "__main__":
    from agentdrop_common import load_config
    db.init_db()
    cfg = load_config()
    d = diagnose(cfg)
    print(f"\ndiagnosis: {d.get('diagnosis')} — {d.get('evidence')}")
    for key in ("n", "views_avg", "engaged_ratio", "hook_hold", "loop_ratio",
                "completion", "shares_per_1k", "comments_per_1k", "likes_per_1k"):
        v = d.get(key)
        print(f"  {key:16} {v if v is None else round(float(v), 3)}")
    print(f"\nknobs: {current_knobs()}")
    ch = decide(d)
    print(f"would change: {ch}")
