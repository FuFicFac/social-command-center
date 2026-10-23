"""SQLite store: posts (one video + copy) fan out to deliveries (one per platform).

Delivery states:
  pending      waiting for the post to be approved and due
  uploading    an adapter is sending bytes right now
  processing   the platform accepted it; we're polling for the final result
  published    live (or in TikTok inbox, for inbox mode) — has url/remote_id
  failed       failed BEFORE the platform accepted anything; safe to retry
  needs_review interrupted mid-upload; may or may not have posted — a human checks
  skipped      removed from this post's targets
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id            TEXT PRIMARY KEY,
    video_path    TEXT NOT NULL,
    sha256        TEXT,
    title         TEXT NOT NULL DEFAULT '',
    caption       TEXT NOT NULL DEFAULT '',
    overrides     TEXT NOT NULL DEFAULT '{}',
    scheduled_at  TEXT,
    approved_at   TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS posts_video ON posts(video_path);
CREATE TABLE IF NOT EXISTS deliveries (
    id          TEXT PRIMARY KEY,
    post_id     TEXT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    platform    TEXT NOT NULL,
    route       TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',
    remote_id   TEXT,
    url         TEXT,
    error       TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    meta        TEXT NOT NULL DEFAULT '{}',
    updated_at  TEXT NOT NULL,
    UNIQUE(post_id, platform)
);
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at         TEXT NOT NULL,
    post_id    TEXT,
    platform   TEXT,
    level      TEXT NOT NULL,
    message    TEXT NOT NULL
);
"""

ACTIVE = ("uploading", "processing")
DONE = ("published", "private", "inbox", "simulated")


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    con.executescript(SCHEMA)
    _migrate(con)
    return con


def _migrate(con: sqlite3.Connection) -> None:
    cols = {r["name"] for r in con.execute("PRAGMA table_info(posts)")}
    if "duration" not in cols:
        try:
            con.execute("ALTER TABLE posts ADD COLUMN duration REAL")
        except sqlite3.OperationalError:  # another connection added it first
            pass


def bucket(post: dict[str, Any]) -> str:
    """Dashboard bucket: attention | posting | posted | scheduled | draft (first match wins)."""
    live = [d["status"] for d in post["deliveries"].values() if d["status"] != "skipped"]
    if any(s in ("failed", "needs_review") for s in live):
        return "attention"
    if any(s in ACTIVE for s in live):
        return "posting"
    if live and all(s in DONE for s in live):
        return "posted"
    return "scheduled" if post.get("approved_at") else "draft"


def log(con: sqlite3.Connection, level: str, message: str, post_id: str | None = None,
        platform: str | None = None) -> None:
    con.execute(
        "INSERT INTO events(at, post_id, platform, level, message) VALUES (?,?,?,?,?)",
        (now(), post_id, platform, level, message[:2000]),
    )


def create_post(con: sqlite3.Connection, video_path: str, sha256: str | None, title: str,
                caption: str, platforms: list[str], scheduled_at: str | None = None) -> str:
    pid = uuid.uuid4().hex[:10]
    ts = now()
    con.execute(
        "INSERT INTO posts(id, video_path, sha256, title, caption, scheduled_at, created_at,"
        " updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (pid, video_path, sha256, title, caption, scheduled_at, ts, ts),
    )
    set_targets(con, pid, platforms)
    log(con, "info", f"created post for {Path(video_path).name}", pid)
    return pid


def set_targets(con: sqlite3.Connection, post_id: str, platforms: list[str]) -> None:
    for p in config.PLATFORMS:
        row = con.execute(
            "SELECT status FROM deliveries WHERE post_id=? AND platform=?", (post_id, p)
        ).fetchone()
        wanted = p in platforms
        if wanted and row is None:
            con.execute(
                "INSERT INTO deliveries(id, post_id, platform, updated_at) VALUES (?,?,?,?)",
                (uuid.uuid4().hex[:12], post_id, p, now()),
            )
        elif wanted and row["status"] == "skipped":
            update_delivery(con, post_id, p, status="pending", error=None)
        elif not wanted and row is not None and row["status"] in ("pending", "failed"):
            update_delivery(con, post_id, p, status="skipped")


def update_post(con: sqlite3.Connection, post_id: str, **fields: Any) -> None:
    if "overrides" in fields and not isinstance(fields["overrides"], str):
        fields["overrides"] = json.dumps(fields["overrides"])
    fields["updated_at"] = now()
    cols = ", ".join(f"{k}=?" for k in fields)
    con.execute(f"UPDATE posts SET {cols} WHERE id=?", (*fields.values(), post_id))


def update_delivery(con: sqlite3.Connection, post_id: str, platform: str, **fields: Any) -> None:
    if "meta" in fields and not isinstance(fields["meta"], str):
        fields["meta"] = json.dumps(fields["meta"])
    fields["updated_at"] = now()
    cols = ", ".join(f"{k}=?" for k in fields)
    con.execute(
        f"UPDATE deliveries SET {cols} WHERE post_id=? AND platform=?",
        (*fields.values(), post_id, platform),
    )


def get_post(con: sqlite3.Connection, post_id: str) -> dict[str, Any] | None:
    row = con.execute("SELECT * FROM posts WHERE id=? OR id LIKE ?",
                      (post_id, f"{post_id}%")).fetchone()
    if row is None:
        return None
    post = dict(row)
    post["overrides"] = json.loads(post["overrides"] or "{}")
    post["deliveries"] = {
        d["platform"]: {**dict(d), "meta": json.loads(d["meta"] or "{}")}
        for d in con.execute("SELECT * FROM deliveries WHERE post_id=?", (post["id"],))
    }
    return post


def list_posts(con: sqlite3.Connection) -> list[dict[str, Any]]:
    ids = [r["id"] for r in con.execute("SELECT id FROM posts ORDER BY created_at DESC")]
    return [p for p in (get_post(con, i) for i in ids) if p]


def recent_events(con: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    return [dict(r) for r in con.execute(
        "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))]
