"""
SQLite database for AgentDrop.

SQLite is a tiny database that lives in a single file (agentdrop.db) —
no server to install. We use it to remember which Reddit posts we've
already used, the status of each video, and (later) performance stats.

This module creates the tables if they don't exist and provides small
helper functions the rest of AgentDrop calls.
"""

import os
import sqlite3
from pathlib import Path

# Default to a file next to this module. In the cloud, set DB_PATH to a
# location on a persistent volume (e.g. /data/agentdrop.db) so it
# survives redeploys.
DB_PATH = Path(os.getenv("DB_PATH", Path(__file__).resolve().parent / "agentdrop.db"))


def get_connection() -> sqlite3.Connection:
    """Open a connection to the database file (creates it if missing)."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # lets us access columns by name
    return conn


def init_db() -> None:
    """Create tables if they don't already exist. Safe to call anytime."""
    conn = get_connection()
    with conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS posts (
                post_id      TEXT PRIMARY KEY,   -- Reddit's unique id
                subreddit    TEXT NOT NULL,
                title        TEXT NOT NULL,
                body         TEXT,
                score        INTEGER,
                word_count   INTEGER,
                status       TEXT DEFAULT 'sourced',  -- sourced|used|skipped
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        # One row per produced video, tracking it through the pipeline.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS videos (
                post_id      TEXT PRIMARY KEY,
                subreddit    TEXT,
                title        TEXT,        -- proposed YouTube title
                description  TEXT,        -- proposed description
                tags         TEXT,        -- comma-separated hashtags
                file_path    TEXT,        -- where the .mp4 currently lives
                status       TEXT DEFAULT 'pending',
                              -- pending|approved|rejected|uploaded
                youtube_id   TEXT,        -- filled after upload
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        # TTS spend tracking — every voiceover records its character count
        # so we can enforce a monthly budget safeguard.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tts_usage (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id     TEXT,
                chars       INTEGER,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        # Round-robin counters (e.g. which voice / footage clip is next).
        # Persisted so rotation survives restarts in the cloud.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rotation_state (
                key    TEXT PRIMARY KEY,
                value  INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        # Performance snapshots over time (one row per stats fetch).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS video_stats (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id     TEXT,
                youtube_id  TEXT,
                subreddit   TEXT,
                views       INTEGER,
                likes       INTEGER,
                comments    INTEGER,
                fetched_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    conn.close()


def next_rotation_index(key: str) -> int:
    """Return a steadily-incrementing counter for round-robin rotation.

    First call for a given key returns 0, then 1, 2, 3 ... The caller
    takes this modulo the number of choices (voices, footage clips) to
    pick the next one. Persisted so rotation continues across restarts.
    """
    conn = get_connection()
    with conn:
        # Self-create so callers don't depend on init_db() running first.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS rotation_state "
            "(key TEXT PRIMARY KEY, value INTEGER NOT NULL DEFAULT 0)"
        )
        conn.execute(
            "INSERT INTO rotation_state (key, value) VALUES (?, 0) "
            "ON CONFLICT(key) DO UPDATE SET value = value + 1",
            (key,),
        )
        row = conn.execute(
            "SELECT value FROM rotation_state WHERE key = ?", (key,)
        ).fetchone()
    conn.close()
    return int(row["value"])


def record_stats(post_id, youtube_id, subreddit, views, likes, comments):
    """Save a performance snapshot for an uploaded video."""
    conn = get_connection()
    with conn:
        conn.execute(
            """
            INSERT INTO video_stats
                (post_id, youtube_id, subreddit, views, likes, comments)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (post_id, youtube_id, subreddit, views, likes, comments),
        )
    conn.close()


def video_performance() -> list[dict]:
    """Per-video performance from each video's latest snapshot.

    Returns one dict per uploaded video with an age-normalized ``score``:
      views_per_day = latest views / days since the video was first tracked
      engagement    = (likes + comments) / views
      score         = views_per_day * (1 + 5 * engagement)

    Age comes from the EARLIEST stats snapshot (a good proxy for upload
    time, since stats are polled every 6h). Normalizing by age stops older
    videos from looking "better" just because they've had more time to rack
    up views; engagement nudges the score toward stories people react to.
    """
    conn = get_connection()
    rows = conn.execute(
        """
        WITH latest AS (
            SELECT post_id, subreddit, views, likes, comments,
                   ROW_NUMBER() OVER (
                       PARTITION BY post_id ORDER BY fetched_at DESC) AS rn
            FROM video_stats
        ),
        firstseen AS (
            SELECT post_id, MIN(fetched_at) AS first_at
            FROM video_stats GROUP BY post_id
        )
        SELECT l.post_id, l.subreddit, l.views, l.likes, l.comments,
               (julianday('now') - julianday(f.first_at)) AS age_days
        FROM latest l
        JOIN firstseen f ON l.post_id = f.post_id
        WHERE l.rn = 1
        """
    ).fetchall()
    conn.close()

    out = []
    for r in rows:
        views = r["views"] or 0
        likes = r["likes"] or 0
        comments = r["comments"] or 0
        # Floor age so a just-uploaded video can't show an infinite rate.
        age_days = max(float(r["age_days"] or 0), 0.5)
        views_per_day = views / age_days
        engagement = (likes + comments) / max(views, 1)
        score = views_per_day * (1 + 5 * engagement)
        out.append({
            "post_id": r["post_id"], "subreddit": r["subreddit"],
            "views": views, "likes": likes, "comments": comments,
            "age_days": age_days, "views_per_day": views_per_day,
            "engagement_rate": engagement, "score": score,
        })
    return out


def subreddit_performance() -> dict:
    """Aggregate per-video performance into per-subreddit averages.

    Each subreddit gets: n (sample size), avg_views, avg_views_per_day,
    engagement_rate, and the composite ``score`` the producer uses to bias
    story selection. ``score`` is age-normalized, so it reflects momentum
    rather than just how long a video has been live.
    """
    by_sub: dict[str, list] = {}
    for v in video_performance():
        by_sub.setdefault(v["subreddit"], []).append(v)

    out = {}
    for sub, vs in by_sub.items():
        n = len(vs)
        out[sub] = {
            "n": n,
            "avg_views": sum(v["views"] for v in vs) / n,
            "avg_views_per_day": sum(v["views_per_day"] for v in vs) / n,
            "engagement_rate": sum(v["engagement_rate"] for v in vs) / n,
            "score": sum(v["score"] for v in vs) / n,
        }
    return out


def upsert_video(post_id, subreddit, title, description, tags, file_path, status="pending"):
    """Insert or update a video row."""
    conn = get_connection()
    with conn:
        conn.execute(
            """
            INSERT INTO videos
                (post_id, subreddit, title, description, tags, file_path, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(post_id) DO UPDATE SET
                subreddit=excluded.subreddit, title=excluded.title,
                description=excluded.description, tags=excluded.tags,
                file_path=excluded.file_path, status=excluded.status
            """,
            (post_id, subreddit, title, description, tags, file_path, status),
        )
    conn.close()


def video_exists(post_id: str) -> bool:
    """True if a video row already exists (used to resume multi-part series)."""
    conn = get_connection()
    row = conn.execute(
        "SELECT 1 FROM videos WHERE post_id = ?", (post_id,)
    ).fetchone()
    conn.close()
    return row is not None


def videos_by_status(status: str) -> list:
    conn = get_connection()
    # created_at orders across stories; post_id is a tiebreaker so parts
    # of the same story made in the same second still upload p1, p2, p3...
    rows = conn.execute(
        "SELECT * FROM videos WHERE status = ? ORDER BY created_at, post_id",
        (status,),
    ).fetchall()
    conn.close()
    return rows


def set_video_status(post_id: str, status: str, file_path: str | None = None,
                     youtube_id: str | None = None) -> None:
    conn = get_connection()
    with conn:
        if file_path is not None:
            conn.execute("UPDATE videos SET file_path=? WHERE post_id=?",
                         (file_path, post_id))
        if youtube_id is not None:
            conn.execute("UPDATE videos SET youtube_id=? WHERE post_id=?",
                         (youtube_id, post_id))
        conn.execute("UPDATE videos SET status=? WHERE post_id=?",
                     (status, post_id))
    conn.close()


def post_already_seen(post_id: str) -> bool:
    """Return True if we've recorded this Reddit post before."""
    conn = get_connection()
    row = conn.execute(
        "SELECT 1 FROM posts WHERE post_id = ?", (post_id,)
    ).fetchone()
    conn.close()
    return row is not None


def save_post(
    post_id: str,
    subreddit: str,
    title: str,
    body: str,
    score: int,
    word_count: int,
    status: str = "sourced",
) -> None:
    """Insert a post record. Ignores duplicates (same post_id)."""
    conn = get_connection()
    with conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO posts
                (post_id, subreddit, title, body, score, word_count, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (post_id, subreddit, title, body, score, word_count, status),
        )
    conn.close()


def record_tts_usage(post_id: str, chars: int) -> None:
    """Log characters sent to the TTS API (for the monthly budget guard)."""
    conn = get_connection()
    with conn:
        conn.execute(
            "INSERT INTO tts_usage (post_id, chars) VALUES (?, ?)",
            (post_id, chars),
        )
    conn.close()


def tts_chars_this_month() -> int:
    """Total TTS characters used since the start of the current month."""
    conn = get_connection()
    row = conn.execute(
        """
        SELECT COALESCE(SUM(chars), 0) AS total FROM tts_usage
        WHERE strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now')
        """
    ).fetchone()
    conn.close()
    return int(row["total"])


def videos_produced_today() -> int:
    """How many videos were created today (for the daily-cap guard)."""
    conn = get_connection()
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM videos WHERE date(created_at) = date('now')"
    ).fetchone()
    conn.close()
    return int(row["n"])


if __name__ == "__main__":
    # Running this file directly sets up the database.
    init_db()
    print(f"Database ready at {DB_PATH}")
