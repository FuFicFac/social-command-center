"""The scheduler: publish approved, due posts and follow up on in-flight ones.

Safe to run every minute from launchd; a file lock stops overlapping runs.
"""

from __future__ import annotations

import fcntl
import sqlite3
import uuid
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import accounts, config, db, media
from .adapters import for_route
from .adapters.base import Ambiguous, NotSubmitted, Result

MAX_AUTO_RETRIES = 3
# Statuses the scheduler keeps polling until they settle.
POLLING = ("processing", "inbox")
INBOX_FOLLOW_DAYS = 7


@contextmanager
def run_lock() -> Iterator[bool]:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(config.LOCK_PATH, "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def is_due(post: dict[str, Any], at: datetime | None = None) -> bool:
    if not post.get("approved_at"):
        return False
    if not post.get("scheduled_at"):
        return True
    return datetime.fromisoformat(post["scheduled_at"]) <= (at or datetime.now(UTC))


def recover_interrupted(con: sqlite3.Connection) -> None:
    """An interrupted upload may already have reached the destination."""
    rows = con.execute("SELECT post_id, platform, account_id, account_handle, account_name "
                       "FROM deliveries WHERE status='uploading'").fetchall()
    for row in rows:
        key = db.delivery_key(row["platform"], row["account_id"])
        db.update_delivery(con, row["post_id"], key, status="needs_review",
                           error="Interrupted during upload. Reconcile the request and account history.")
        db.log(con, "warn", "interrupted upload flagged for review", row["post_id"],
               row["platform"], db.account_label(row) or None)


def _apply(con: sqlite3.Connection, post: dict[str, Any], key: str, r: Result,
           prior_meta: dict[str, Any] | None = None) -> None:
    d = post["deliveries"][key]
    db.update_delivery(con, post["id"], key, status=r.status, remote_id=r.remote_id,
                       url=r.url, error=r.error, meta={**(prior_meta or {}), **r.meta})
    level = "error" if r.status == "failed" else "info"
    db.log(con, level, f"{r.status}" + (f" {r.url}" if r.url else "")
           + (f" — {r.error}" if r.error else ""), post["id"], d["platform"],
           db.account_label(d) or None)


def _note(con: sqlite3.Connection, post: dict[str, Any], key: str, msg: str,
          warn: bool = False) -> None:
    """Leave a delivery waiting, saying why. Logged once, not every minute."""
    d = post["deliveries"][key]
    if d["error"] != msg:
        db.update_delivery(con, post["id"], key, error=msg)
        if warn:
            db.log(con, "warn", msg, post["id"], d["platform"], db.account_label(d) or None)


def _hold(con: sqlite3.Connection, post: dict[str, Any], key: str, msg: str) -> None:
    """The approved account can't be confirmed. Nothing is sent; a person decides."""
    d = post["deliveries"][key]
    db.update_delivery(con, post["id"], key, status="needs_review", error=msg,
                       meta={**d["meta"], "account_hold": msg})
    db.log(con, "warn", f"held for review — {msg}", post["id"], d["platform"],
           db.account_label(d) or None)


def sendable(post: dict[str, Any]) -> list[str]:
    return [k for k, d in post["deliveries"].items()
            if d["status"] == "pending"
            or (d["status"] == "failed" and d["meta"].get("retryable")
                and d["attempts"] < MAX_AUTO_RETRIES)]


def _receipt_block(con: sqlite3.Connection, post: dict[str, Any],
                   d: dict[str, Any]) -> tuple[sqlite3.Row | None, str | None]:
    """(duplicate receipt, hold reason). Duplicates are per account: the same file may go to
    a second TikTok, never twice to the same one."""
    rows = con.execute("SELECT * FROM upload_receipts WHERE sha256=? AND platform=?",
                       (post.get("sha256"), d["platform"])).fetchall()
    if not d["account_id"]:  # no account selected: any earlier upload to the platform counts
        return (rows[0] if rows else None), None
    same = next((r for r in rows if r["account_ref"] == d["account_ref"]), None)
    if same:
        return same, None
    if any(r["account_ref"] == "" for r in rows):
        return None, (f"An earlier {d['platform']} upload of this file has no confirmed account. "
                      "Confirm where it went under Connections & Settings → Upload history.")
    return None, None


def _live_problems(con: sqlite3.Connection, adapter: Any, route: str, profile: str,
                   group: list[dict[str, Any]]) -> dict[str, str]:
    """Ask the connection what it holds right now and compare with what was approved.
    What the connection says today outranks anything remembered from an earlier look."""
    out: dict[str, str] = {}
    if route == "upload_post" and hasattr(adapter, "current_accounts"):
        social = adapter.current_accounts()
        accounts.sync_profile(con, profile, None, social or {})  # keep the stored list in step
        where = f"Upload-Post profile '{profile}'"
        for d in group:
            current = accounts.read_social(d["platform"], (social or {}).get(d["platform"]))
            if msg := accounts.live_problem(d, current, where):
                out[d["key"]] = msg
    elif route == "direct" and hasattr(adapter, "current_channel"):
        ch = adapter.current_channel()
        current = {"platform_id": ch["id"], "handle": ch["handle"], "name": ch["title"]} if ch else None
        for d in group:
            if msg := accounts.live_problem(d, current, "This YouTube sign-in"):
                out[d["key"]] = msg
    else:  # this connection can't be asked; go by the last verified state
        for d in group:
            if msg := accounts.problem(d, accounts.get(con, d["account_id"])):
                out[d["key"]] = msg
    return out


def publish_post(con: sqlite3.Connection, cfg: config.Config, post: dict[str, Any],
                 only: list[str] | None = None) -> None:
    """Send every due delivery of one post. `only` limits it to delivery keys or platforms."""
    video = Path(post["video_path"])
    targets = sendable(post)
    if only is not None:
        targets = [k for k in targets if k in only or post["deliveries"][k]["platform"] in only]
    for key in targets.copy():
        delivery = post["deliveries"][key]
        platform = delivery["platform"]
        prior, reason = _receipt_block(con, post, delivery)
        if prior:
            reason = f"Existing {accounts.describe(delivery)} upload recorded ({prior['status']})."
            db.update_delivery(con, post["id"], key, status="skipped", error=reason,
                               url=prior["url"] or delivery["url"],
                               remote_id=prior["remote_id"] or delivery["remote_id"])
            db.log(con, "info", f"not sent again — {reason}", post["id"], platform,
                   db.account_label(delivery) or None)
        elif reason:
            pass
        elif platform == "youtube" and cfg.get("youtube", "uploads_paused"):
            reason = "YouTube uploads are paused."
        elif delivery["meta"].get("history_unverified"):
            reason = "Check this account's posting history before delivery."
        if reason:
            if not prior:
                _note(con, post, key, reason)
            targets.remove(key)
    if not targets:
        return
    if not video.exists():
        for k in targets:
            _apply(con, post, k, Result("failed", error=f"video missing: {video}"))
        return
    if post.get("sha256") and media.sha256(video) != post["sha256"]:
        for k in targets:
            _apply(con, post, k, Result(
                "failed", error="video changed since it was queued — re-approve to post the new cut"))
        db.update_post(con, post["id"], approved_at=None)
        return
    probe = media.probe(video)

    intake = con.execute("SELECT brand FROM fini_items WHERE post_id=? AND phase='queued'",
                         (post["id"],)).fetchone()
    # (route, connection) -> deliveries. One Upload-Post request per profile carries only the
    # platforms selected on that profile; a profile holds one account per platform.
    groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    for key in targets:
        d = post["deliveries"][key]
        p = d["platform"]
        problems = media.preflight(post, p, probe)
        if problems:
            _apply(con, post, key, Result("failed", error="; ".join(problems)))
            continue
        setting = cfg.route(p)
        if setting == "dryrun":
            if intake:
                _note(con, post, key, "Choose a live route in Connections & Settings")
                continue
            groups[("dryrun", d["profile"] or d["account_id"])].append(key)
            continue
        if d["account_id"]:
            acct = accounts.get(con, d["account_id"])
            if acct is None or acct["platform_id"] != d["account_ref"]:
                _hold(con, post, key, accounts.problem(d, acct))
                continue
            route, profile = acct["conn"], acct["profile"]
        else:
            route, profile = setting, ""
            if route == "upload_post" or p == "youtube":
                _note(con, post, key,
                      f"Choose which {p} account receives this in the editor.", warn=True)
                continue
        adapter = for_route(cfg, p, route, profile or None)
        ok, why = adapter.readiness(p)
        if not ok:  # stay pending; it goes out on the first run after you connect
            _note(con, post, key, f"waiting on setup ({route}): {why}", warn=True)
            continue
        # upload_post takes every platform of one profile in one call; direct adapters one each.
        groups[(route, profile if route == "upload_post" else key)].append(key)

    for (route, group), keys in groups.items():
        first = post["deliveries"][keys[0]]
        profile = (first["profile"] or None) if route != "dryrun" and first["account_id"] else None
        adapter = for_route(cfg, first["platform"], route, profile)
        if profile:
            try:
                held = _live_problems(con, adapter, route, profile,
                                      [post["deliveries"][k] for k in keys])
            except NotSubmitted as e:  # could not ask; nothing was sent, so try again later
                for k in keys:
                    d = post["deliveries"][k]
                    db.update_delivery(con, post["id"], k, attempts=d["attempts"] + 1)
                    _apply(con, post, k, Result("failed", error=str(e), meta={"retryable": True}),
                           d["meta"])
                continue
            for k, msg in held.items():
                _hold(con, post, k, msg)
            keys = [k for k in keys if k not in held]
            if not keys:
                continue
        request = ({"request_id": f"scc-{post['id']}-{uuid.uuid4().hex}"}
                   if route == "upload_post" else {})
        plats = [post["deliveries"][k]["platform"] for k in keys]
        by_platform = dict(zip(plats, keys, strict=True))
        metadata = {}
        for k in keys:
            d = post["deliveries"][k]
            metadata[k] = {**d["meta"], **request}
            metadata[k].pop("retryable", None)
            metadata[k].pop("account_hold", None)
            if route == "upload_post":
                metadata[k]["profile"] = profile or cfg.get("upload_post", "user")
            db.update_delivery(con, post["id"], k, status="uploading", route=route,
                               attempts=d["attempts"] + 1, error=None, meta=metadata[k])

        def log(level: str, msg: str, _keys: list[str] = keys,
                _plats: list[str] = plats) -> None:
            names = [db.account_label(post["deliveries"][k]) for k in _keys]
            db.log(con, level, msg, post["id"], ",".join(dict.fromkeys(_plats)),
                   ", ".join(n for n in names if n) or None)

        try:
            results = adapter.publish(post, plats, video, log, **request)
        except NotSubmitted as e:
            for k in keys:
                _apply(con, post, k, Result("failed", error=str(e), meta={"retryable": True}),
                       metadata[k])
            continue
        except Ambiguous as e:
            for k in keys:
                _apply(con, post, k, Result("needs_review", error=str(e)), metadata[k])
            continue
        except Exception as e:  # unknown failure mid-flight: never guess, flag it
            for k in keys:
                _apply(con, post, k, Result("needs_review", error=f"{type(e).__name__}: {e}"),
                       metadata[k])
            continue
        for p, k in by_platform.items():
            _apply(con, post, k, results.get(p) or Result("needs_review",
                                                          error="adapter returned nothing"),
                   metadata[k])


def poll_in_flight(con: sqlite3.Connection, cfg: config.Config) -> None:
    cutoff = datetime.now(UTC) - timedelta(days=INBOX_FOLLOW_DAYS)
    rows = con.execute(
        f"SELECT post_id, platform, account_id FROM deliveries WHERE status IN ({','.join('?' * len(POLLING))}) "
        "OR (status='needs_review' AND route='upload_post' AND json_extract(meta,'$.request_id') IS NOT NULL)",
        POLLING).fetchall()
    for row in rows:
        post = db.get_post(con, row["post_id"])
        if not post:
            continue
        key = db.delivery_key(row["platform"], row["account_id"])
        d = post["deliveries"][key]
        if d["status"] == "inbox" and datetime.fromisoformat(d["updated_at"]) < cutoff:
            continue
        # Status checks go back to the connection the delivery was sent through.
        adapter = for_route(cfg, row["platform"], d["route"] or "dryrun",
                            (d["profile"] or None) if d["account_id"] else None)
        try:
            r = adapter.poll(post, row["platform"], d)
        except Exception as e:  # polling is best-effort; try again next run
            db.log(con, "warn", f"status check failed: {e}", post["id"], row["platform"],
                   db.account_label(d) or None)
            continue
        if r and (r.status != d["status"] or r.url != d["url"]):
            # An operator can reconcile a destination while the HTTP lookup is in flight.
            with con:
                con.execute("BEGIN IMMEDIATE")
                current = db.get_post(con, row["post_id"])
                if current and current["deliveries"].get(key) == d:
                    _apply(con, post, key, r, d["meta"])


def run_once(con: sqlite3.Connection | None = None, cfg: config.Config | None = None) -> str:
    con = con or db.connect()
    cfg = cfg or config.load()
    with run_lock() as got:
        if not got:
            return "another run is in progress"
        recover_interrupted(con)
        published = 0
        for post in db.list_posts(con):
            if is_due(post) and sendable(post):
                publish_post(con, cfg, post)
                published += 1
        poll_in_flight(con, cfg)
    return f"processed {published} due post(s)"
