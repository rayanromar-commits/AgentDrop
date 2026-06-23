"""
SQLite database for AgentDrop.

SQLite is a tiny database that lives in a single file (agentdrop.db) —
no server to install. We use it to remember which Reddit posts we've
already used, the status of each video, and (later) performance stats.

This module creates the tables if they don't exist and provides small
helper functions the rest of AgentDrop calls.
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "agentdrop.db"


def get_connection() -> sqlite3.Connection:
    """Open a connection to the database file (creates it if missing)."""
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


if __name__ == "__main__":
    # Running this file directly sets up the database.
    init_db()
    print(f"Database ready at {DB_PATH}")
