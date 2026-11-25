"""SQLite store: posts (one video + copy) fan out to deliveries (one per destination account).

A destination is a platform plus the account that receives it. A delivery keeps a snapshot
of that account (stable platform ID, handle, brand, connection) from the moment it was
selected, so a later change of defaults or a reconnected profile can never redirect it.

Delivery states:
  pending      waiting for the post to be approved and due
  uploading    an adapter is sending bytes right now
  processing   the platform accepted it; we're polling for the final result
  published    live (or in TikTok inbox, for inbox mode) — has url/remote_id
  failed       failed BEFORE the platform accepted anything; safe to retry
  needs_review interrupted mid-upload, or its account no longer matches — a human checks
  skipped      removed from this post's targets

Delivery keys: "tiktok" for a delivery with no account selected, "tiktok:<account id>"
for one bound to an account. `post["deliveries"]` is keyed by that string.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import config

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS fini_items (
    id INTEGER PRIMARY KEY, path TEXT NOT NULL, stamp TEXT NOT NULL,
    observed REAL NOT NULL, phase TEXT NOT NULL DEFAULT 'settling', hash TEXT,
    kind TEXT, brand TEXT, package TEXT, post_id TEXT, receipt TEXT,
    origin TEXT NOT NULL DEFAULT 'drop', retry_after REAL NOT NULL DEFAULT 0,
    message TEXT, UNIQUE(path,stamp)
);
CREATE INDEX IF NOT EXISTS fini_hash ON fini_items(hash);
CREATE INDEX IF NOT EXISTS fini_post ON fini_items(post_id);
CREATE TABLE IF NOT EXISTS shorts_inventory (
    hash TEXT NOT NULL, path TEXT NOT NULL UNIQUE, seconds REAL NOT NULL, dimensions TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS inventory_hash ON shorts_inventory(hash);
CREATE TABLE IF NOT EXISTS brands (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS profiles (
    name TEXT PRIMARY KEY, display_name TEXT, present INTEGER NOT NULL DEFAULT 1, seen_at TEXT
);
CREATE TABLE IF NOT EXISTS accounts (
    id           TEXT PRIMARY KEY,
    platform     TEXT NOT NULL,
    platform_id  TEXT NOT NULL,          -- the platform's own stable ID for the account
    handle       TEXT,
    name         TEXT,
    conn         TEXT NOT NULL,          -- upload_post | direct
    profile      TEXT NOT NULL,          -- Upload-Post profile, or the direct credential's ID
    brand_id     TEXT REFERENCES brands(id) ON DELETE SET NULL,
    is_default   INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'verified',   -- verified | expired | missing
    detail       TEXT,
    verified_at  TEXT,
    created_at   TEXT NOT NULL,
    UNIQUE(conn, profile, platform, platform_id)
);
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
    updated_at    TEXT NOT NULL,
    duration      REAL,
    brand         TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS posts_video ON posts(video_path);
CREATE TABLE IF NOT EXISTS deliveries (
    id             TEXT PRIMARY KEY,
    post_id        TEXT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    platform       TEXT NOT NULL,
    route          TEXT,
    status         TEXT NOT NULL DEFAULT 'pending',
    remote_id      TEXT,
    url            TEXT,
    error          TEXT,
    attempts       INTEGER NOT NULL DEFAULT 0,
    meta           TEXT NOT NULL DEFAULT '{}',
    updated_at     TEXT NOT NULL,
    account_id     TEXT NOT NULL DEFAULT '',   -- '' = no account selected
    account_ref    TEXT NOT NULL DEFAULT '',   -- the account's stable platform ID when selected
    account_handle TEXT,
    account_name   TEXT,
    brand_id       TEXT,
    profile        TEXT,
    UNIQUE(post_id, platform, account_id)
);
CREATE TABLE IF NOT EXISTS upload_receipts (
    sha256 TEXT NOT NULL, platform TEXT NOT NULL, account_ref TEXT NOT NULL DEFAULT '',
    post_id TEXT NOT NULL, status TEXT NOT NULL, remote_id TEXT, url TEXT,
    recorded_at TEXT NOT NULL, account_id TEXT, account_handle TEXT,
    review INTEGER NOT NULL DEFAULT 0,   -- 1 = which account received it is not confirmed
    evidence TEXT,
    PRIMARY KEY(sha256, platform, account_ref)
);
DROP TRIGGER IF EXISTS record_upload_receipt;
CREATE TRIGGER IF NOT EXISTS record_upload_receipt_v2 AFTER UPDATE ON deliveries
WHEN (NEW.status IN ('published','private','inbox','processing') OR
      (NEW.status='skipped' AND json_extract(NEW.meta,'$.previously_uploaded')=1)) AND
     coalesce(NEW.route,'') != 'dryrun' AND coalesce(NEW.remote_id,'') != 'dryrun'
BEGIN
    INSERT INTO upload_receipts(sha256,platform,account_ref,post_id,status,remote_id,url,
                                recorded_at,account_id,account_handle,evidence)
    SELECT sha256,NEW.platform,NEW.account_ref,NEW.post_id,NEW.status,NEW.remote_id,NEW.url,
           NEW.updated_at,nullif(NEW.account_id,''),NEW.account_handle,
           CASE WHEN NEW.account_ref != '' THEN 'recorded when it was delivered' END
    FROM posts WHERE id=NEW.post_id AND sha256 IS NOT NULL
    ON CONFLICT(sha256,platform,account_ref) DO UPDATE SET
        status=excluded.status, remote_id=coalesce(excluded.remote_id,upload_receipts.remote_id),
        url=coalesce(excluded.url,upload_receipts.url)
    WHERE upload_receipts.post_id=excluded.post_id;
END;
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at         TEXT NOT NULL,
    post_id    TEXT,
    platform   TEXT,
    level      TEXT NOT NULL,
    message    TEXT NOT NULL,
    account    TEXT
);
"""

DELIVERY_COLS = ("id,post_id,platform,route,status,remote_id,url,error,attempts,meta,updated_at")

ACTIVE = ("uploading", "processing")
DONE = ("published", "private", "inbox", "simulated")


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode = WAL")
    _migrate(con)
    con.execute("PRAGMA foreign_keys = ON")
    return con


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}


def _migrate(con: sqlite3.Connection) -> None:
    """Bring any earlier database to the current shape, then apply the idempotent schema.

    Version 2 adds destination accounts. Nothing is discarded: every delivery and receipt row
    is copied across. Receipts written before accounts existed keep an empty account and are
    flagged for review until their destination is proven (see accounts.prove_receipts).
    """
    if con.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
        return
    con.execute("PRAGMA foreign_keys = OFF")  # table rebuilds below; no-op inside a transaction
    con.execute("BEGIN IMMEDIATE")
    try:
        if con.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:  # lost the race?
            if "post_id" in (cols := _columns(con, "deliveries")) and "account_id" not in cols:
                con.execute("DROP TRIGGER IF EXISTS record_upload_receipt")
                con.execute("ALTER TABLE deliveries RENAME TO deliveries_v1")
                con.execute("ALTER TABLE upload_receipts RENAME TO upload_receipts_v1")
            for column in ("duration REAL", "brand TEXT"):
                if (cols := _columns(con, "posts")) and column.split()[0] not in cols:
                    con.execute(f"ALTER TABLE posts ADD COLUMN {column}")
            if (cols := _columns(con, "events")) and "account" not in cols:
                con.execute("ALTER TABLE events ADD COLUMN account TEXT")
            for statement in _statements(SCHEMA):
                con.execute(statement)
            if _columns(con, "deliveries_v1"):
                con.execute(f"INSERT INTO deliveries({DELIVERY_COLS}) "
                            f"SELECT {DELIVERY_COLS} FROM deliveries_v1")
                con.execute(
                    "INSERT INTO upload_receipts(sha256,platform,post_id,status,remote_id,url,"
                    "recorded_at,review) SELECT sha256,platform,post_id,status,remote_id,url,"
                    "recorded_at,1 FROM upload_receipts_v1")
                con.execute("DROP TABLE deliveries_v1")
                con.execute("DROP TABLE upload_receipts_v1")
                # Posts that came through intake already know their brand.
                con.execute(
                    "UPDATE posts SET brand=(SELECT brand FROM fini_items f WHERE f.post_id="
                    "posts.id AND f.brand IN ('wam','alist') ORDER BY f.id DESC LIMIT 1) "
                    "WHERE brand IS NULL")
            for brand_id, name in config.DEFAULT_BRANDS:
                con.execute("INSERT OR IGNORE INTO brands(id,name,created_at) VALUES (?,?,?)",
                            (brand_id, name, now()))
            con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise


def _statements(script: str) -> list[str]:
    """Split a schema script so it can run inside one transaction (executescript commits)."""
    out, buf = [], ""
    for line in script.splitlines():
        buf += line + "\n"
        if sqlite3.complete_statement(buf):
            out.append(buf.strip())
            buf = ""
    return [s for s in out if s]


def delivery_key(platform: str, account_id: str | None) -> str:
    return f"{platform}:{account_id}" if account_id else platform


def split_key(key: str) -> tuple[str, str]:
    platform, _, account_id = key.partition(":")
    return platform, account_id


def account_label(row: Any) -> str:
    """'@handle' / channel name for a delivery or account row; '' when none is selected."""
    cols = set(row.keys())  # works for sqlite rows and dicts alike
    handle = (row["account_handle"] if "account_handle" in cols else row["handle"]) or ""
    name = (row["account_name"] if "account_name" in cols else row["name"]) or ""
    if handle and " " not in handle.strip():   # a real @handle, not a display name
        return handle if handle.startswith("@") else f"@{handle}"
    return handle or name


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
        platform: str | None = None, account: str | None = None) -> None:
    con.execute(
        "INSERT INTO events(at, post_id, platform, level, message, account) VALUES (?,?,?,?,?,?)",
        (now(), post_id, platform, level, message[:2000], account),
    )


def create_post(con: sqlite3.Connection, video_path: str, sha256: str | None, title: str,
                caption: str, platforms: list[str], scheduled_at: str | None = None,
                brand: str | None = None) -> str:
    pid = uuid.uuid4().hex[:10]
    ts = now()
    con.execute(
        "INSERT INTO posts(id, video_path, sha256, title, caption, scheduled_at, created_at,"
        " updated_at, brand) VALUES (?,?,?,?,?,?,?,?,?)",
        (pid, video_path, sha256, title, caption, scheduled_at, ts, ts, brand),
    )
    set_targets(con, pid, platforms)
    log(con, "info", f"created post for {Path(video_path).name}", pid)
    return pid


def default_account(con: sqlite3.Connection, brand: str | None, platform: str) -> str:
    """The account a brand suggests for a platform ('' when it has none)."""
    if not brand:
        return ""
    row = con.execute(
        "SELECT id FROM accounts WHERE brand_id=? AND platform=? AND status != 'missing' "
        "ORDER BY is_default DESC, created_at LIMIT 1", (brand, platform)).fetchone()
    return row["id"] if row else ""


def set_targets(con: sqlite3.Connection, post_id: str, platforms: list[str]) -> None:
    """Choose destinations by platform. Selected accounts are kept; a newly added platform
    takes the post's brand account when there is one, otherwise no account yet."""
    post = get_post(con, post_id)
    wanted: list[tuple[str, str]] = []
    for p in config.PLATFORMS:
        if p not in platforms:
            continue
        rows = [d for d in post["deliveries"].values() if d["platform"] == p]
        live = [d for d in rows if d["status"] != "skipped"]
        if live:
            wanted += [(p, d["account_id"]) for d in live]
        else:
            suggested = default_account(con, post.get("brand"), p)
            ids = [d["account_id"] for d in rows]
            wanted.append((p, suggested if suggested or not ids or "" in ids else ids[0]))
    set_destinations(con, post_id, wanted)


def _snapshot(con: sqlite3.Connection, platform: str, account_id: str) -> dict[str, Any]:
    if not account_id:
        return {"account_id": "", "account_ref": "", "account_handle": None,
                "account_name": None, "brand_id": None, "profile": None}
    acct = con.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    if acct is None or acct["platform"] != platform:
        raise ValueError(f"no {platform} account {account_id!r}")
    return {"account_id": acct["id"], "account_ref": acct["platform_id"],
            "account_handle": acct["handle"], "account_name": acct["name"],
            "brand_id": acct["brand_id"], "profile": acct["profile"]}


def removable(delivery: dict[str, Any]) -> bool:
    """Nothing was ever sent for this delivery, so the destination can still be dropped."""
    if delivery["status"] in ("pending", "failed"):
        return True
    return (delivery["status"] == "needs_review" and bool(delivery["meta"].get("account_hold"))
            and not delivery["remote_id"] and not delivery["meta"].get("request_id"))


def set_destinations(con: sqlite3.Connection, post_id: str,
                     wanted: list[tuple[str, str]]) -> bool:
    """Make the post's destinations exactly `wanted` [(platform, account_id or '')].

    Deliveries that already went out are never touched. Any change clears the approval:
    an approval covers only the destinations that were on screen when it was given.
    """
    post = get_post(con, post_id)
    have = {(d["platform"], d["account_id"]): d for d in post["deliveries"].values()}
    want = list(dict.fromkeys((p, a or "") for p, a in wanted))
    changed = False
    for (platform, account_id), d in have.items():
        if (platform, account_id) not in want and d["status"] != "skipped" and removable(d):
            update_delivery(con, post_id, d["key"], status="skipped", error=None)
            changed = True
    for platform, account_id in want:
        d = have.get((platform, account_id))
        if d is None:
            snap = _snapshot(con, platform, account_id)
            con.execute(
                "INSERT INTO deliveries(id,post_id,platform,updated_at,meta,account_id,"
                "account_ref,account_handle,account_name,brand_id,profile) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex[:12], post_id, platform, now(), '{"history_unverified":true}',
                 *snap.values()))
            changed = True
        elif d["status"] == "skipped":
            if d["meta"].get("previously_uploaded"):
                continue
            meta = dict(d["meta"])
            if not meta.get("history_checked_at"):
                meta["history_unverified"] = True
            update_delivery(con, post_id, d["key"], status="pending", error=None, meta=meta)
            changed = True
    if changed:
        con.execute("UPDATE posts SET approved_at=NULL, updated_at=? WHERE id=? AND "
                    "approved_at IS NOT NULL", (now(), post_id))
    return changed


def rebind(con: sqlite3.Connection, post_id: str, key: str, account_id: str) -> bool:
    """Point an unsent delivery at another account (or at none). Clears the approval."""
    post = get_post(con, post_id)
    d = post["deliveries"].get(key)
    if d is None or not removable(d) or d["account_id"] == (account_id or ""):
        return False
    if delivery_key(d["platform"], account_id) in post["deliveries"]:
        if d["status"] != "skipped":      # that destination already exists; drop this one
            update_delivery(con, post_id, key, status="skipped", error=None)
    else:
        snap = _snapshot(con, d["platform"], account_id or "")
        meta = {k: v for k, v in d["meta"].items() if k != "account_hold"}
        con.execute(
            "UPDATE deliveries SET account_id=?,account_ref=?,account_handle=?,account_name=?,"
            "brand_id=?,profile=?,status='pending',error=NULL,meta=?,updated_at=? WHERE id=?",
            (*snap.values(), json.dumps(meta), now(), d["id"]))
    con.execute("UPDATE posts SET approved_at=NULL, updated_at=? WHERE id=?", (now(), post_id))
    return True


def set_brand(con: sqlite3.Connection, post_id: str, brand: str | None) -> None:
    """Record the post's brand and re-suggest that brand's accounts for unsent deliveries."""
    post = get_post(con, post_id)
    if brand and not con.execute("SELECT 1 FROM brands WHERE id=?", (brand,)).fetchone():
        raise ValueError(f"no brand {brand!r}")
    if post.get("brand") != brand:
        update_post(con, post_id, brand=brand)
    for key, d in post["deliveries"].items():
        if d["status"] == "skipped" or not removable(d):
            continue
        rebind(con, post_id, key, default_account(con, brand, d["platform"]))


def fill_brand_defaults(con: sqlite3.Connection, brand: str) -> int:
    """Give a brand's unsent deliveries that have no account the brand's account. Deliveries
    that already name an account are left exactly as they are."""
    rows = con.execute(
        "SELECT d.post_id, d.platform FROM deliveries d JOIN posts p ON p.id=d.post_id "
        "WHERE p.brand=? AND d.account_id='' AND d.status IN ('pending','failed')",
        (brand,)).fetchall()
    filled = 0
    for row in rows:
        account_id = default_account(con, brand, row["platform"])
        if account_id and rebind(con, row["post_id"], row["platform"], account_id):
            filled += 1
    return filled


def update_post(con: sqlite3.Connection, post_id: str, **fields: Any) -> None:
    if "overrides" in fields and not isinstance(fields["overrides"], str):
        fields["overrides"] = json.dumps(fields["overrides"])
    fields["updated_at"] = now()
    cols = ", ".join(f"{k}=?" for k in fields)
    con.execute(f"UPDATE posts SET {cols} WHERE id=?", (*fields.values(), post_id))


def update_delivery(con: sqlite3.Connection, post_id: str, key: str, **fields: Any) -> None:
    if "meta" in fields and not isinstance(fields["meta"], str):
        fields["meta"] = json.dumps(fields["meta"])
    fields["updated_at"] = now()
    cols = ", ".join(f"{k}=?" for k in fields)
    platform, account_id = split_key(key)
    con.execute(
        f"UPDATE deliveries SET {cols} WHERE post_id=? AND platform=? AND account_id=?",
        (*fields.values(), post_id, platform, account_id),
    )


def get_post(con: sqlite3.Connection, post_id: str) -> dict[str, Any] | None:
    row = con.execute("SELECT * FROM posts WHERE id=? OR id LIKE ?",
                      (post_id, f"{post_id}%")).fetchone()
    if row is None:
        return None
    post = dict(row)
    post["overrides"] = json.loads(post["overrides"] or "{}")
    post["deliveries"] = {}
    for d in con.execute("SELECT * FROM deliveries WHERE post_id=? ORDER BY rowid",
                         (post["id"],)):
        key = delivery_key(d["platform"], d["account_id"])
        post["deliveries"][key] = {**dict(d), "key": key, "meta": json.loads(d["meta"] or "{}")}
    return post


def keys_for(post: dict[str, Any], names: list[str]) -> list[str]:
    """Expand delivery keys and bare platform names to the post's matching delivery keys."""
    out: list[str] = []
    for name in names:
        out += [k for k, d in post["deliveries"].items() if k == name or d["platform"] == name]
    return list(dict.fromkeys(out))


def needs_request_check(delivery: dict[str, Any]) -> bool:
    return (delivery["route"] == "upload_post" and bool(delivery["meta"].get("request_id"))
            and (delivery["status"] == "needs_review"
                 or (delivery["status"] == "failed" and not delivery["meta"].get("retryable"))))


def retry_blocked(delivery: dict[str, Any]) -> bool:
    meta = delivery["meta"]
    return (delivery["status"] in ("published", "private", "inbox", "processing", "uploading")
            or needs_request_check(delivery)
            or bool(delivery["remote_id"] and delivery["remote_id"] != "dryrun")
            or bool(meta.get("hold") or meta.get("history_unverified") or meta.get("previously_uploaded")))


def list_posts(con: sqlite3.Connection) -> list[dict[str, Any]]:
    ids = [r["id"] for r in con.execute("SELECT id FROM posts ORDER BY created_at DESC")]
    return [p for p in (get_post(con, i) for i in ids) if p]


def recent_events(con: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    return [dict(r) for r in con.execute(
        "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))]
