"""The scheduler: publish approved, due posts and follow up on in-flight ones.

Safe to run every minute from launchd; a file lock stops overlapping runs.
"""

from __future__ import annotations

import copy
import fcntl
import sqlite3
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import config, db, media
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
    """An 'uploading' row at startup means a run died mid-upload. Don't guess."""
    for row in con.execute("SELECT post_id, platform FROM deliveries WHERE status='uploading'"):
        db.update_delivery(con, row["post_id"], row["platform"], status="needs_review",
                           error="run was interrupted mid-upload — check the platform, then "
                                 "`scc mark` it published or `scc retry` it")
        db.log(con, "warn", "interrupted upload flagged for review", row["post_id"], row["platform"])


def _apply(con: sqlite3.Connection, post_id: str, platform: str, r: Result,
           prior_meta: dict[str, Any] | None = None) -> None:
    db.update_delivery(con, post_id, platform, status=r.status, remote_id=r.remote_id,
                       url=r.url, error=r.error, meta={**(prior_meta or {}), **r.meta})
    level = "error" if r.status == "failed" else "info"
    db.log(con, level, f"{r.status}" + (f" {r.url}" if r.url else "")
           + (f" — {r.error}" if r.error else ""), post_id, platform)


def sendable(post: dict[str, Any]) -> list[str]:
    return [p for p, d in post["deliveries"].items()
            if d["status"] == "pending"
            or (d["status"] == "failed" and d["meta"].get("retryable")
                and d["attempts"] < MAX_AUTO_RETRIES)]


def publish_post(con: sqlite3.Connection, cfg: config.Config, post: dict[str, Any],
                 platforms: list[str] | None = None) -> None:
    video = Path(post["video_path"])
    targets = sendable(post)
    if platforms is not None:
        targets = [p for p in targets if p in platforms]
    if not targets:
        return
    if not video.exists():
        for p in targets:
            _apply(con, post["id"], p, Result("failed", error=f"video missing: {video}"))
        return
    if post.get("sha256") and media.sha256(video) != post["sha256"]:
        for p in targets:
            _apply(con, post["id"], p, Result(
                "failed", error="video changed since it was queued — re-approve to post the new cut"))
        db.update_post(con, post["id"], approved_at=None)
        return
    probe = media.probe(video)

    intake = con.execute("SELECT brand FROM fini_items WHERE post_id=? AND phase='queued'",
                         (post["id"],)).fetchone()
    if intake and intake["brand"] == "alist":
        cfg = config.Config(copy.deepcopy(cfg.data))
        cfg.set("upload_post", "user", cfg.get("fini", "alist_upload_post_user"))
        cfg.set("upload_post", "facebook_page_id", "")
    groups: dict[tuple[str, int], list[str]] = defaultdict(list)
    for p in targets:
        problems = media.preflight(post, p, probe)
        if problems:
            _apply(con, post["id"], p, Result("failed", error="; ".join(problems)))
            continue
        route = cfg.route(p)
        if intake and (route == "dryrun" or (intake["brand"] == "alist" and route == "direct")):
            msg = ("Choose a live route in Connections & Settings" if route == "dryrun"
                   else "Select Upload-Post and the A-List profile to deliver to this brand")
            if post["deliveries"][p]["error"] != msg:
                db.update_delivery(con, post["id"], p, error=msg)
            continue
        adapter = for_route(cfg, p, route)
        ok, why = adapter.readiness(p)
        if not ok:  # stay pending; it goes out on the first run after you connect
            msg = f"waiting on setup ({route}): {why}"
            if post["deliveries"][p]["error"] != msg:
                db.update_delivery(con, post["id"], p, error=msg)
                db.log(con, "warn", msg, post["id"], p)
            continue
        # upload_post and dryrun take every platform in one call; direct adapters one each.
        key = (route, 0) if route in ("upload_post", "dryrun") else (f"{route}:{p}", 0)
        groups[key].append(p)

    for (route_key, _), plats in groups.items():
        route = route_key.split(":")[0]
        adapter = for_route(cfg, plats[0], route)
        for p in plats:
            db.update_delivery(con, post["id"], p, status="uploading", route=route,
                               attempts=post["deliveries"][p]["attempts"] + 1, error=None)

        def log(level: str, msg: str, _plats: list[str] = plats) -> None:
            db.log(con, level, msg, post["id"], ",".join(_plats))

        try:
            results = adapter.publish(post, plats, video, log)
        except NotSubmitted as e:
            for p in plats:
                _apply(con, post["id"], p, Result("failed", error=str(e), meta={"retryable": True}))
            continue
        except Ambiguous as e:
            for p in plats:
                _apply(con, post["id"], p, Result("needs_review", error=str(e)))
            continue
        except Exception as e:  # unknown failure mid-flight: never guess, flag it
            for p in plats:
                _apply(con, post["id"], p, Result("needs_review", error=f"{type(e).__name__}: {e}"))
            continue
        for p in plats:
            _apply(con, post["id"], p, results.get(p) or Result("needs_review",
                                                                 error="adapter returned nothing"))


def poll_in_flight(con: sqlite3.Connection, cfg: config.Config) -> None:
    cutoff = datetime.now(UTC) - timedelta(days=INBOX_FOLLOW_DAYS)
    rows = con.execute(
        f"SELECT post_id, platform FROM deliveries WHERE status IN ({','.join('?' * len(POLLING))})",
        POLLING).fetchall()
    for row in rows:
        post = db.get_post(con, row["post_id"])
        if not post:
            continue
        d = post["deliveries"][row["platform"]]
        if d["status"] == "inbox" and datetime.fromisoformat(d["updated_at"]) < cutoff:
            continue
        adapter = for_route(cfg, row["platform"], d["route"] or "dryrun")
        try:
            r = adapter.poll(post, row["platform"], d)
        except Exception as e:  # polling is best-effort; try again next run
            db.log(con, "warn", f"status check failed: {e}", post["id"], row["platform"])
            continue
        if r and (r.status != d["status"] or r.url != d["url"]):
            _apply(con, post["id"], row["platform"], r, d["meta"])


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
