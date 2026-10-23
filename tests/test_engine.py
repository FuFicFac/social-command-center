from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scc import config, db, engine, media
from scc.adapters import base
from scc.adapters.tiktok import chunk_plan
from scc.adapters.upload_post import _parse_results


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "LOCK_PATH", tmp_path / "run.lock")
    con = db.connect(tmp_path / "t.sqlite3")
    video = tmp_path / "proj__short-2-books-to-the-desk.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=108x192:d=5",
                    "-f", "lavfi", "-i", "anullsrc", "-shortest", "-c:v", "libx264",
                    "-c:a", "aac", str(video)], check=True)
    cfg = config.Config({s: dict(v) for s, v in config.DEFAULTS.items()})
    return con, cfg, video


def make_post(con, video, **kw):
    pid = db.create_post(con, str(video), media.sha256(video), kw.get("title", "Books to the desk"),
                         kw.get("caption", "A caption #ai"), kw.get("to", list(config.PLATFORMS)))
    db.update_post(con, pid, approved_at=db.now())
    return pid


class Fake:
    name = "fake"

    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    def readiness(self, platform):
        return True, "ok"

    def publish(self, post, platforms, video, log):
        self.calls.append(platforms)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return {p: base.Result(self.outcome, remote_id="r1", url="https://x/1") for p in platforms}

    def poll(self, post, platform, delivery):
        return base.Result("published", remote_id="r1", url="https://x/1")


def use(monkeypatch, fake):
    monkeypatch.setattr(engine, "for_route", lambda cfg, p, r: fake)


def statuses(con, pid):
    return {p: d["status"] for p, d in db.get_post(con, pid)["deliveries"].items()}


def test_dryrun_end_to_end(env):
    con, cfg, video = env
    pid = make_post(con, video)
    engine.run_once(con, cfg)
    assert set(statuses(con, pid).values()) == {"simulated"}


def test_drafts_never_post(env, monkeypatch):
    con, cfg, video = env
    fake = Fake("published")
    use(monkeypatch, fake)
    pid = make_post(con, video)
    db.update_post(con, pid, approved_at=None)
    engine.run_once(con, cfg)
    assert fake.calls == []
    assert set(statuses(con, pid).values()) == {"pending"}


def test_published_is_never_resent(env, monkeypatch):
    con, cfg, video = env
    fake = Fake("published")
    use(monkeypatch, fake)
    pid = make_post(con, video)
    engine.run_once(con, cfg)
    engine.run_once(con, cfg)
    assert sum(len(c) for c in fake.calls) == 4  # each platform exactly once
    assert set(statuses(con, pid).values()) == {"published"}


def test_ambiguous_goes_to_review_not_retry(env, monkeypatch):
    con, cfg, video = env
    fake = Fake(base.Ambiguous("timeout during publish"))
    use(monkeypatch, fake)
    pid = make_post(con, video, to=["youtube"])
    engine.run_once(con, cfg)
    engine.run_once(con, cfg)
    assert len(fake.calls) == 1
    assert statuses(con, pid)["youtube"] == "needs_review"


def test_not_submitted_retries_then_stops(env, monkeypatch):
    con, cfg, video = env
    fake = Fake(base.NotSubmitted("400 bad"))
    use(monkeypatch, fake)
    pid = make_post(con, video, to=["instagram"])
    for _ in range(6):
        engine.run_once(con, cfg)
    assert len(fake.calls) == engine.MAX_AUTO_RETRIES
    assert statuses(con, pid)["instagram"] == "failed"


def test_interrupted_upload_flagged(env):
    con, cfg, video = env
    pid = make_post(con, video, to=["tiktok"])
    db.update_delivery(con, pid, "tiktok", status="uploading")
    engine.run_once(con, cfg)
    assert statuses(con, pid)["tiktok"] == "needs_review"


def test_changed_video_blocks_and_unapproves(env, monkeypatch):
    con, cfg, video = env
    fake = Fake("published")
    use(monkeypatch, fake)
    pid = make_post(con, video, to=["youtube"])
    with video.open("ab") as fh:
        fh.write(b"\0")
    engine.run_once(con, cfg)
    assert fake.calls == []
    assert db.get_post(con, pid)["approved_at"] is None


def test_preflight_blocks_bad_copy(env, monkeypatch):
    con, cfg, video = env
    fake = Fake("published")
    use(monkeypatch, fake)
    pid = make_post(con, video, title="x" * 101, caption="", to=["youtube", "tiktok"])
    engine.run_once(con, cfg)
    post = db.get_post(con, pid)
    assert fake.calls == []
    assert "title" in post["deliveries"]["youtube"]["error"]
    assert "caption is empty" in post["deliveries"]["tiktok"]["error"]


def test_upload_post_is_one_call_for_many(env, monkeypatch):
    con, cfg, video = env
    cfg.set("routing", "tiktok", "upload_post")
    cfg.set("routing", "instagram", "upload_post")
    fake = Fake("processing")
    use(monkeypatch, fake)
    make_post(con, video, to=["tiktok", "instagram"])
    engine.run_once(con, cfg)
    assert fake.calls == [["instagram", "tiktok"]] or fake.calls == [["tiktok", "instagram"]]


def test_scheduled_waits(env, monkeypatch):
    con, cfg, video = env
    fake = Fake("published")
    use(monkeypatch, fake)
    pid = make_post(con, video, to=["youtube"])
    db.update_post(con, pid, scheduled_at="2999-01-01T00:00:00+00:00")
    engine.run_once(con, cfg)
    assert fake.calls == []


def test_chunk_plan():
    assert chunk_plan(40 * 2**20) == (40 * 2**20, 1)
    size = 150 * 2**20 + 123
    chunk, n = chunk_plan(size)
    assert chunk * n <= size and size - chunk * (n - 1) < 2 * chunk


def test_title_from_filename():
    assert media.title_from_filename(Path("why-ai-matters-0928__short-1-the-same-day-model-drop.mp4")) \
        == "The same day model drop"
    assert media.title_from_filename(Path("wam-0930b-arc__s4-grandma.mp4")) == "Grandma"
    assert media.title_from_filename(Path("x__01-lost-ideas.mp4")) == "Lost ideas"


def test_upload_post_result_parsing():
    r = _parse_results([
        {"platform": "tiktok", "success": True, "post_url": "https://t/1"},
        {"platform": "instagram", "status": "processing"},
        {"platform": "facebook", "success": False, "message": "token expired"},
    ])
    assert r["tiktok"].status == "published" and r["tiktok"].url == "https://t/1"
    assert r["instagram"].status == "processing"
    assert r["facebook"].status == "failed" and "token" in r["facebook"].error
