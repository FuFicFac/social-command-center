"""Posting slots: pure-ish helpers for the calendar and "auto-schedule" features.

Slots come from config ([schedule] slots/days, in general.timezone). All instants returned
are timezone-aware UTC datetimes; "taken" comparisons use minute precision.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from . import config, db, media

MAX_HORIZON_DAYS = 3 * 366


def tzinfo(cfg: config.Config) -> ZoneInfo:
    return ZoneInfo(cfg.get("general", "timezone"))


def parse_slot(text: str) -> time:
    hh, _, mm = text.strip().partition(":")
    if not (hh.isdigit() and mm.isdigit() and len(mm) == 2 and 0 <= int(hh) <= 23
            and 0 <= int(mm) <= 59):
        raise ValueError(f"bad slot {text!r}; use HH:MM (24-hour)")
    return time(int(hh), int(mm))


def minute(dt: datetime) -> datetime:
    """Normalize to a UTC instant truncated to the minute (the 'same slot' key)."""
    return dt.astimezone(UTC).replace(second=0, microsecond=0)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def local_today(cfg: config.Config, now: datetime | None = None) -> date:
    return (now or datetime.now(UTC)).astimezone(tzinfo(cfg)).date()


def slots_between(cfg: config.Config, start_local_date: date, days: int) -> list[datetime]:
    """Every configured slot on `days` consecutive local dates from start (past ones included)."""
    tz = tzinfo(cfg)
    times = sorted({parse_slot(s) for s in cfg.slots()})
    wanted = {config.WEEKDAYS.index(d) for d in cfg.days()}
    out: set[datetime] = set()
    for i in range(max(0, days)):
        d = start_local_date + timedelta(days=i)
        if d.weekday() not in wanted:
            continue
        for t in times:
            # zoneinfo resolves a nonexistent (spring-forward) time forward and an ambiguous
            # (fall-back) one to its first occurrence; converting to UTC pins the instant.
            out.add(minute(datetime.combine(d, t, tzinfo=tz)))
    return sorted(out)


def taken_slots(con: sqlite3.Connection, exclude: set[str] | None = None) -> dict[datetime, str]:
    """{slot instant (minute, UTC): post_id} for every scheduled post."""
    out: dict[datetime, str] = {}
    for r in con.execute("SELECT id, scheduled_at FROM posts WHERE scheduled_at IS NOT NULL"):
        if exclude and r["id"] in exclude:
            continue
        try:
            out[minute(datetime.fromisoformat(r["scheduled_at"]))] = r["id"]
        except ValueError:
            continue
    return out


def _free_slots(cfg: config.Config, con: sqlite3.Connection, need: int, start: date | None,
                now: datetime, exclude: set[str]) -> list[datetime]:
    taken = taken_slots(con, exclude)
    cutoff = minute(now)
    day = start or local_today(cfg, now)
    free: list[datetime] = []
    span = 60
    while len(free) < need and (day - (start or local_today(cfg, now))).days < MAX_HORIZON_DAYS:
        for s in slots_between(cfg, day, span):
            if s > cutoff and s not in taken and s not in free:
                free.append(s)
        day += timedelta(days=span)
        if not cfg.slots() or not cfg.days():
            break
    return free


def auto_assign(con: sqlite3.Connection, cfg: config.Config, ids: list[str],
                start: date | None = None, approve: bool = False, dry_run: bool = False,
                now: datetime | None = None) -> tuple[dict[str, str], dict[str, str]]:
    """Fill the next free slots, in the order of `ids`. Returns (assigned, errors).

    Slots in the past or already held by another post are skipped. With approve=True each
    post must pass media preflight first; failures are reported and not scheduled.
    A post being re-slotted releases its own current slot. dry_run never writes.
    """
    now = now or datetime.now(UTC)
    ids = list(dict.fromkeys(ids))
    assigned: dict[str, str] = {}
    errors: dict[str, str] = {}
    ok_posts: list[dict[str, Any]] = []
    for pid in ids:
        post = db.get_post(con, pid)
        if not post or post["id"] != pid:
            errors[pid] = "no such post"
            continue
        b = db.bucket(post)
        if b == "posted":
            errors[pid] = "already posted"
            continue
        if b == "posting":
            errors[pid] = "post is in flight"
            continue
        if approve:
            if not [d for d in post["deliveries"].values() if d["status"] != "skipped"]:
                errors[pid] = "no platforms selected"
                continue
            issues = media.post_issues(post)
            if issues:
                errors[pid] = "; ".join(issues)
                continue
        ok_posts.append(post)

    free = _free_slots(cfg, con, len(ok_posts), start, now, {p["id"] for p in ok_posts})
    for post, slot in zip(ok_posts, free, strict=False):
        assigned[post["id"]] = iso(slot)
    for post in ok_posts[len(free):]:
        errors[post["id"]] = "no free slots (check schedule settings)"

    if not dry_run:
        for pid, at in assigned.items():
            fields: dict[str, Any] = {"scheduled_at": at}
            if approve and not db.get_post(con, pid)["approved_at"]:
                fields["approved_at"] = db.now()
            db.update_post(con, pid, **fields)
            db.log(con, "info", f"auto-scheduled for {at}" + (" and approved" if approve else ""),
                   pid)
    return assigned, errors


def slot_label(cfg: config.Config, dt: datetime) -> str:
    return dt.astimezone(tzinfo(cfg)).strftime("%a %b %-d %-I:%M %p")
