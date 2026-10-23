from __future__ import annotations

import subprocess
from datetime import UTC, date, datetime

import pytest

from scc import config, db, media, schedule


def cfg_with(tz="America/Los_Angeles", slots="09:00,17:00", days="mon,tue,wed,thu,fri,sat,sun"):
    cfg = config.Config({s: dict(v) for s, v in config.DEFAULTS.items()})
    cfg.set("general", "timezone", tz)
    cfg.set("schedule", "slots", slots)
    cfg.set("schedule", "days", days)
    return cfg


def make_video(path, seconds=5, size="108x192"):
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"color=c=black:s={size}:d={seconds}",
                    "-f", "lavfi", "-i", "anullsrc", "-shortest", "-c:v", "libx264", "-c:a", "aac",
                    str(path)], check=True)


@pytest.fixture
def env(tmp_path):
    con = db.connect(tmp_path / "t.sqlite3")
    return con, cfg_with(), tmp_path


def post(con, tmp_path, name, caption="cap #ai", seconds=5, size="108x192", to=None):
    v = tmp_path / f"{name}.mp4"
    make_video(v, seconds, size)
    return db.create_post(con, str(v), media.sha256(v), name, caption, to or ["youtube", "tiktok"])


NOW = datetime(2026, 10, 20, 17, 30, tzinfo=UTC)  # 10:30 PDT: the 09:00 slot is already past


def test_slots_basic_utc_conversion():
    got = schedule.slots_between(cfg_with(), date(2026, 10, 24), 1)
    assert [s.isoformat() for s in got] == ["2026-10-24T16:00:00+00:00", "2026-10-25T00:00:00+00:00"]


def test_slots_respect_days():
    cfg = cfg_with(days="mon,wed")
    got = schedule.slots_between(cfg, date(2026, 10, 19), 7)  # Mon..Sun
    assert len(got) == 4
    assert {s.astimezone(schedule.tzinfo(cfg)).weekday() for s in got} == {0, 2}


def test_slots_dst_fall_back_boundary():
    # 2026-11-01 is the day PDT -> PST. 09:00 local is 16:00 UTC before, 17:00 UTC after.
    got = schedule.slots_between(cfg_with(), date(2026, 10, 31), 3)
    assert [s.isoformat() for s in got] == [
        "2026-10-31T16:00:00+00:00", "2026-11-01T00:00:00+00:00",   # Oct 31 09:00 / 17:00 PDT
        "2026-11-01T17:00:00+00:00", "2026-11-02T01:00:00+00:00",   # Nov 1 09:00 / 17:00 PST
        "2026-11-02T17:00:00+00:00", "2026-11-03T01:00:00+00:00",
    ]


def test_slots_dst_spring_forward_gap():
    # 02:30 doesn't exist on 2026-03-08 in LA; we must still produce one valid instant.
    got = schedule.slots_between(cfg_with(slots="02:30"), date(2026, 3, 7), 3)
    assert len(got) == 3 and len(set(got)) == 3


def test_slots_dedup_and_sorted():
    got = schedule.slots_between(cfg_with(slots="17:00,09:00,09:00"), date(2026, 10, 24), 1)
    assert len(got) == 2 and got == sorted(got)


def test_parse_slot_rejects_bad():
    for bad in ("25:00", "9:5", "ab:cd", "12", "12:60"):
        with pytest.raises(ValueError):
            schedule.parse_slot(bad)


def test_auto_assign_order_and_taken_skip(env):
    con, cfg, tmp = env
    a, b, c = (post(con, tmp, n) for n in ("aaa", "bbb", "ccc"))
    # b sits on the first slot after NOW (Oct 20 17:00 PDT = 00:00Z Oct 21)
    db.update_post(con, b, scheduled_at="2026-10-21T00:00:00+00:00")
    assigned, errors = schedule.auto_assign(con, cfg, [a, c], now=NOW)
    assert errors == {}
    assert list(assigned) == [a, c]
    assert assigned[a] == "2026-10-21T16:00:00+00:00"   # taken 17:00 skipped
    assert assigned[c] == "2026-10-22T00:00:00+00:00"
    assert db.get_post(con, a)["scheduled_at"] == assigned[a]
    assert db.get_post(con, a)["approved_at"] is None


def test_taken_compares_at_minute_precision(env):
    con, cfg, tmp = env
    a, b = post(con, tmp, "aaa"), post(con, tmp, "bbb")
    db.update_post(con, b, scheduled_at="2026-10-21T00:00:30+00:00")  # seconds off the slot
    assigned, _ = schedule.auto_assign(con, cfg, [a], now=NOW)
    assert assigned[a] == "2026-10-21T16:00:00+00:00"


def test_skips_past_slots(env):
    con, cfg, tmp = env
    a = post(con, tmp, "aaa")
    late = datetime(2026, 10, 21, 0, 30, tzinfo=UTC)  # just after Oct 20 17:00 PDT
    assigned, _ = schedule.auto_assign(con, cfg, [a], now=late)
    assert assigned[a] == "2026-10-21T16:00:00+00:00"


def test_dry_run_does_not_save(env):
    con, cfg, tmp = env
    a = post(con, tmp, "aaa")
    assigned, errors = schedule.auto_assign(con, cfg, [a], approve=True, dry_run=True, now=NOW)
    assert a in assigned and errors == {}
    p = db.get_post(con, a)
    assert p["scheduled_at"] is None and p["approved_at"] is None


def test_start_date_in_future(env):
    con, cfg, tmp = env
    a = post(con, tmp, "aaa")
    assigned, _ = schedule.auto_assign(con, cfg, [a], start=date(2026, 12, 1), now=NOW)
    assert assigned[a] == "2026-12-01T17:00:00+00:00"  # 09:00 PST


def test_approve_preflight_failure_not_scheduled(env):
    con, cfg, tmp = env
    good = post(con, tmp, "good")
    nocap = post(con, tmp, "nocap", caption="")       # tiktok caption empty
    landscape = post(con, tmp, "wide", size="192x108")
    assigned, errors = schedule.auto_assign(con, cfg, [nocap, good, landscape], approve=True,
                                            now=NOW)
    assert list(assigned) == [good]
    assert assigned[good] == "2026-10-21T00:00:00+00:00"  # failures didn't burn a slot
    assert "caption is empty" in errors[nocap]
    assert "not vertical" in errors[landscape]
    assert db.get_post(con, good)["approved_at"]
    assert db.get_post(con, nocap)["scheduled_at"] is None
    assert db.get_post(con, nocap)["approved_at"] is None


def test_already_posted_and_unknown(env):
    con, cfg, tmp = env
    a = post(con, tmp, "aaa")
    for plat in ("youtube", "tiktok"):
        db.update_delivery(con, a, plat, status="published")
    assigned, errors = schedule.auto_assign(con, cfg, [a, "deadbeef00"], now=NOW)
    assert assigned == {}
    assert errors[a] == "already posted"
    assert errors["deadbeef00"] == "no such post"


def test_reslot_releases_own_slot(env):
    con, cfg, tmp = env
    a = post(con, tmp, "aaa")
    db.update_post(con, a, scheduled_at="2026-10-21T00:00:00+00:00")
    assigned, _ = schedule.auto_assign(con, cfg, [a], now=NOW)
    assert assigned[a] == "2026-10-21T00:00:00+00:00"


def test_no_slots_configured(env):
    con, _, tmp = env
    a = post(con, tmp, "aaa")
    cfg = cfg_with(days="")
    _, errors = schedule.auto_assign(con, cfg, [a], now=NOW)
    assert "no free slots" in errors[a]


def test_bucket_rules():
    d = lambda s: {"status": s}
    base = {"approved_at": None}
    assert db.bucket({**base, "deliveries": {"a": d("pending")}}) == "draft"
    assert db.bucket({"approved_at": "x", "deliveries": {"a": d("pending")}}) == "scheduled"
    assert db.bucket({**base, "deliveries": {"a": d("failed"), "b": d("uploading")}}) == "attention"
    assert db.bucket({**base, "deliveries": {"a": d("processing"), "b": d("skipped")}}) == "posting"
    assert db.bucket({**base, "deliveries": {"a": d("inbox"), "b": d("skipped")}}) == "posted"
