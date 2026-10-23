from __future__ import annotations

import http.client
import json
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scc import captions, config, db, jobs, media, server
from scc import secrets as keychain


def make_video(path: Path, seconds: float = 5, size: str = "108x192") -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    f"color=c=black:s={size}:d={seconds}", "-f", "lavfi", "-i", "anullsrc",
                    "-shortest", "-c:v", "libx264", "-c:a", "aac", str(path)], check=True)


class Client:
    def __init__(self, port: int):
        self.port = port

    def req(self, method, path, body=None, token=True, host=None, headers=None, raw=False):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        h = {"Host": host or f"127.0.0.1:{self.port}", **(headers or {})}
        if token:
            h["X-SCC-Token"] = server.TOKEN
        data = None
        if body is not None:
            data = json.dumps(body)
            h["Content-Type"] = "application/json"
        c.request(method, path, data, h)
        r = c.getresponse()
        payload = r.read()
        is_json = r.getheader("Content-Type", "").startswith("application/json")
        out = payload if raw or not is_json else json.loads(payload)
        c.close()
        return r.status, out, r

    def get(self, path, **kw):
        return self.req("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.req("POST", path, {} if body is None else body, **kw)


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "scc.sqlite3")
    monkeypatch.setattr(config, "LOCK_PATH", tmp_path / "data" / "run.lock")
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.toml")
    shorts = tmp_path / "shorts"
    shorts.mkdir()
    monkeypatch.setitem(config.DEFAULTS["general"], "shorts_folder", str(shorts))
    store: dict[str, str] = {}
    monkeypatch.setattr(keychain, "get", store.get)
    monkeypatch.setattr(keychain, "put", store.__setitem__)
    monkeypatch.setattr(keychain, "delete", lambda n: store.pop(n, None))
    monkeypatch.setattr(jobs, "PLIST", tmp_path / "nope.plist")
    httpd = server.make_server(0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    c = Client(httpd.server_address[1])
    c.shorts, c.store, c.tmp = shorts, store, tmp_path
    yield c
    httpd.shutdown()
    httpd.server_close()


def add_video(app, name, **kw):
    make_video(app.shorts / f"{name}.mp4", **kw)


def import_all(app):
    s, out, _ = app.post("/api/import")
    assert s == 200
    return out["imported"]


def state(app):
    return app.get("/api/state")[1]


def posts(app):
    return {p["filename"]: p for p in state(app)["posts"]}


def wait_for(fn, tries=100):
    for _ in range(tries):
        if fn():
            return True
        time.sleep(0.1)
    return False


def test_auth_and_host(app):
    assert app.post("/api/import", token=False)[0] == 403
    assert app.post("/api/import", host="evil.example:80")[0] == 403
    assert app.get("/api/state", host="evil.example:80")[0] == 403
    assert app.get("/api/state")[0] == 200


def test_index_injects_token_and_csp(app):
    s, body, r = app.get("/", raw=True)
    assert s == 200
    assert server.TOKEN.encode() in body and b"__SCC_TOKEN__" not in body
    csp = r.getheader("Content-Security-Policy")
    assert "script-src 'self'" in csp and "script-src 'self' 'unsafe-inline'" not in csp


def test_static_serving_and_traversal(app, monkeypatch, tmp_path):
    static = tmp_path / "static"
    static.mkdir()
    (static / "app.js").write_text("1")
    (static / "styles.css").write_text("a{}")
    (tmp_path / "secret.txt").write_text("nope")
    monkeypatch.setattr(server, "STATIC", static)
    s, _, r = app.get("/static/app.js", raw=True)
    assert s == 200 and r.getheader("Content-Type").startswith("text/javascript")
    ctype = app.get("/static/styles.css", raw=True)[2].getheader("Content-Type")
    assert ctype.startswith("text/css")
    for bad in ("/static/..", "/static/../secret.txt", "/static/%2e%2e%2fsecret.txt",
                "/static/..%2fsecret.txt", "/static/nonexistent.js", "/static//etc/passwd",
                "/static/sub/app.js"):
        assert app.get(bad, raw=True)[0] == 404, bad


def test_state_shape_and_import(app):
    add_video(app, "proj__short-1-alpha")
    add_video(app, "proj__short-2-beta")
    assert import_all(app) == 2
    assert import_all(app) == 0
    st = state(app)
    assert {"settings", "platforms", "posts", "jobs", "caption_queue", "connect", "scheduler",
            "events", "counts"} <= set(st)
    assert st["settings"]["schedule"] == {
        "slots": ["09:00", "17:00"], "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}
    assert st["settings"]["tiktok_mode"] == "inbox"
    yt = st["platforms"]["youtube"]
    assert yt["route"] == "dryrun" and yt["ready"]
    assert yt["configured"] == {"direct": False, "upload_post": False}
    assert st["counts"] == {"drafts": 2, "needs_captions": 2, "scheduled": 0, "posted": 0,
                            "attention": 0}
    assert st["caption_queue"] == {"running": None, "queued": []}
    assert st["scheduler"]["launchd_installed"] is False
    p = next(x for x in st["posts"] if x["filename"] == "proj__short-1-alpha.mp4")
    assert {"id", "video_path", "filename", "title", "caption", "overrides", "scheduled_at",
            "approved_at", "created_at", "updated_at", "duration", "duplicate_of", "bucket",
            "deliveries"} <= set(p)
    assert p["bucket"] == "draft" and p["title"] == "Alpha"
    assert 4 < p["duration"] < 6
    assert set(p["deliveries"]) == {"youtube", "instagram", "facebook", "tiktok"}


def test_duplicates_and_targets(app):
    add_video(app, "a__short-1-the-books")
    add_video(app, "b__s3-The_Books")
    add_video(app, "c__short-1-other")
    import_all(app)
    ps = posts(app)
    a, b, c = (ps[f"{n}.mp4"] for n in ("a__short-1-the-books", "b__s3-The_Books",
                                         "c__short-1-other"))
    assert a["duplicate_of"] == [b["id"]] and b["duplicate_of"] == [a["id"]]
    assert c["duplicate_of"] == []
    out = app.post(f"/api/posts/{c['id']}", {"targets": ["youtube"]})[1]
    assert out["post"]["deliveries"]["tiktok"]["status"] == "skipped"


def test_edit_approve_unapprove_flow(app):
    add_video(app, "x__short-1-flow")
    import_all(app)
    pid = next(iter(posts(app).values()))["id"]
    s, out, _ = app.post(f"/api/posts/{pid}/approve")
    assert s == 400 and out["error"] and any("caption is empty" in i for i in out["issues"])
    out = app.post(f"/api/posts/{pid}", {"caption": "hello #ai", "title": "Flow"})[1]
    assert out["ok"] and "unapproved" not in out
    assert app.post(f"/api/posts/{pid}/approve")[0] == 200
    assert posts(app)["x__short-1-flow.mp4"]["bucket"] == "scheduled"
    out = app.post(f"/api/posts/{pid}", {"caption": "changed"})[1]
    assert out["unapproved"] is True and out["post"]["approved_at"] is None
    assert app.post(f"/api/posts/{pid}", {"scheduled_at": "garbage"})[0] == 400
    out = app.post(f"/api/posts/{pid}", {"scheduled_at": "2030-01-01T10:00:00+00:00"})[1]
    assert out["post"]["scheduled_at"] == "2030-01-01T10:00:00+00:00"
    assert app.post("/api/posts/0000000000/approve")[0] == 404
    assert app.post(f"/api/posts/{pid}/publish", {})[0] == 400  # confirm required


def test_bulk_results_per_id(app):
    add_video(app, "a__short-1-one")
    add_video(app, "b__short-1-two")
    import_all(app)
    ps = posts(app)
    a, b = ps["a__short-1-one.mp4"]["id"], ps["b__short-1-two.mp4"]["id"]
    app.post(f"/api/posts/{a}", {"caption": "ok caption"})
    r = app.post("/api/bulk", {"action": "approve", "ids": [a, b, "ffffffffff"]})[1]["results"]
    assert r[a] == "ok" and "caption is empty" in r[b] and r["ffffffffff"] == "no such post"
    assert app.post("/api/bulk", {"action": "unapprove", "ids": [a]})[1]["results"][a] == "ok"
    res = app.post("/api/bulk", {"action": "targets", "ids": [a], "targets": ["tiktok"]})[1]
    assert res["results"][a] == "ok"
    res = app.post("/api/bulk", {"action": "targets", "ids": [a], "targets": ["nope"]})[1]
    assert res["results"][a] != "ok"
    assert app.post("/api/bulk", {"action": "wat", "ids": []})[0] == 400
    out = app.post("/api/bulk", {"action": "remove", "ids": [b]})[1]
    assert out["results"][b] == "ok" and b not in {p["id"] for p in state(app)["posts"]}


def test_caption_queue_is_sequential(app, monkeypatch):
    for n in ("a", "b", "c"):
        add_video(app, f"{n}__short-1-{n}{n}")
    import_all(app)
    ps = posts(app)
    ids = [ps[f"{n}__short-1-{n}{n}.mp4"]["id"] for n in ("a", "b", "c")]
    active = {"now": 0, "max": 0}
    lock = threading.Lock()
    release = threading.Event()

    def fake_transcribe(video, sha):
        with lock:
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
        release.wait(5)
        return "words"

    def fake_draft(text, hint):
        with lock:
            active["now"] -= 1
        if hint == "Bb":
            raise RuntimeError("claude exploded")
        return {"title": "AI " + hint, "caption": "cap #x", "tiktok": "tt #x"}

    monkeypatch.setattr(captions, "transcribe", fake_transcribe)
    monkeypatch.setattr(captions, "draft", fake_draft)
    out = app.post("/api/bulk", {"action": "captions", "ids": ids})[1]["results"]
    assert set(out.values()) == {"ok"}
    assert wait_for(lambda: state(app)["caption_queue"]["running"] == ids[0])
    st = state(app)
    assert st["caption_queue"]["queued"] == ids[1:]
    assert st["jobs"][ids[0]] == "drafting captions" and st["jobs"][ids[1]] == "queued for captions"
    assert app.post(f"/api/posts/{ids[1]}/captions")[0] == 409  # already queued
    release.set()
    assert wait_for(lambda: not state(app)["jobs"])
    st = state(app)
    assert active["max"] == 1
    by_id = {p["id"]: p for p in st["posts"]}
    assert by_id[ids[0]]["title"] == "AI Aa"
    assert by_id[ids[0]]["overrides"]["tiktok"]["caption"] == "tt #x"
    assert by_id[ids[1]]["caption"] == ""  # the failed one
    assert any(e["level"] == "error" and "claude exploded" in e["message"] for e in st["events"])
    assert by_id[ids[2]]["title"] == "AI Cc"  # worker survived the failure


def test_slots_and_schedule_auto_end_to_end(app):
    for n in ("a", "b", "c"):
        add_video(app, f"{n}__short-1-{n}{n}")
    import_all(app)
    ps = posts(app)
    ids = [ps[f"{n}__short-1-{n}{n}.mp4"]["id"] for n in ("a", "b", "c")]
    for i in ids[:2]:
        app.post(f"/api/posts/{i}", {"caption": "cap #ai"})
    slots = app.get("/api/slots?days=3")[1]["slots"]
    assert slots and all(s["post_id"] is None for s in slots)
    assert {"at", "local", "post_id"} == set(slots[0])
    assert app.get("/api/slots?days=999")[0] == 200
    assert app.get("/api/slots?from=bad")[0] == 400

    plan = app.post("/api/schedule/auto", {"ids": ids, "approve": True, "dry_run": True})[1]
    assert plan["ok"] and list(plan["assigned"]) == ids[:2] and ids[2] in plan["errors"]
    assert all(p["scheduled_at"] is None for p in state(app)["posts"])  # nothing saved

    real = app.post("/api/schedule/auto", {"ids": ids, "approve": True, "dry_run": False})[1]
    assert real["assigned"] == plan["assigned"]
    st = {p["id"]: p for p in state(app)["posts"]}
    assert st[ids[0]]["scheduled_at"] == real["assigned"][ids[0]]
    assert st[ids[0]]["bucket"] == "scheduled"
    assert st[ids[2]]["scheduled_at"] is None
    taken = {s["at"]: s["post_id"] for s in app.get("/api/slots?days=10")[1]["slots"]}
    assert taken[real["assigned"][ids[0]]] == ids[0]

    # a second pass for the third post lands after the taken ones
    app.post(f"/api/posts/{ids[2]}", {"caption": "cap #ai"})
    out = app.post("/api/schedule/auto", {"ids": [ids[2]], "approve": False})[1]
    assert out["assigned"][ids[2]] > max(real["assigned"].values())

    assert app.post("/api/schedule/clear", {"ids": [ids[0]]})[1]["ok"]
    assert {p["id"]: p for p in state(app)["posts"]}[ids[0]]["scheduled_at"] is None
    assert app.post("/api/schedule/auto", {"ids": ids, "start": "nope"})[0] == 400


def test_settings_validation_and_persistence(app):
    for bad in ({"schedule": {"slots": ["25:00"]}}, {"schedule": {"slots": []}},
                {"schedule": {"days": ["funday"]}}, {"schedule": {"days": []}},
                {"timezone": "Mars/Base"}, {"routing": {"youtube": "carrier-pigeon"}},
                {"routing": {"myspace": "direct"}}, {"tiktok_mode": "yolo"},
                {"shorts_folder": "/definitely/not/here"}):
        s, out, _ = app.post("/api/settings", bad)
        assert s == 400 and out["error"], bad
    assert not (app.tmp / "config.toml").exists()  # rejected input never reaches disk
    s, out, _ = app.post("/api/settings", {
        "timezone": "America/New_York",
        "schedule": {"slots": ["17:00", "9:30", "09:30"], "days": ["sat", "mon"]},
        "routing": {"youtube": "upload_post"}, "tiktok_mode": "direct",
        "shorts_folder": str(app.shorts)})
    assert s == 200
    st = out["settings"]
    assert st["timezone"] == "America/New_York"
    assert st["schedule"] == {"slots": ["09:30", "17:00"], "days": ["mon", "sat"]}
    assert st["routing"]["youtube"] == "upload_post" and st["tiktok_mode"] == "direct"
    assert state(app)["settings"] == st
    toml = (app.tmp / "config.toml").read_text()
    assert 'slots = "09:30,17:00"' in toml and 'days = "mon,sat"' in toml


def test_secrets_never_in_state_and_connections(app):
    vals = ["sk-upload-SECRET-1", "app-SECRET-2", "yt-SECRET-3", "tt-SECRET-4"]
    assert app.post("/api/connect/upload_post", {"api_key": vals[0], "user": "creator"})[0] == 200
    app.post("/api/connect/meta/app", {"app_id": "1234", "app_secret": vals[1]})
    app.post("/api/connect/youtube/client", {"client_id": "yt-id", "client_secret": vals[2]})
    app.post("/api/connect/tiktok/client",
             {"client_key": "tt-key", "client_secret": vals[3], "mode": "direct"})
    assert app.store["upload_post.api_key"] == vals[0]
    # empty secret = keep existing
    app.post("/api/connect/upload_post", {"api_key": "", "user": "creator2"})
    assert app.store["upload_post.api_key"] == vals[0]
    app.store["youtube.refresh_token"] = "REFRESH-SECRET-5"
    raw = app.get("/api/state", raw=True)[1].decode()
    for v in [*vals, "REFRESH-SECRET-5"]:
        assert v not in raw
    st = json.loads(raw)
    assert st["connections"]["upload_post"] == {"user": "creator2", "has_api_key": True}
    assert st["connections"]["tiktok"]["mode"] == "direct"
    assert st["settings"]["tiktok_mode"] == "direct"
    assert st["platforms"]["youtube"]["configured"]["upload_post"] is True
    assert app.post("/api/connect/tiktok/client", {"mode": "nope"})[0] == 400
    assert "SECRET" not in (app.tmp / "config.toml").read_text()


def test_verify_and_disconnect(app, monkeypatch):
    out = app.post("/api/connect/verify", {"platform": "youtube"})[1]
    assert out == {"ok": True, "detail": "dry run"}
    app.post("/api/settings", {"routing": {"youtube": "direct", "instagram": "upload_post"}})
    out = app.post("/api/connect/verify", {"platform": "youtube"})[1]
    assert out["ok"] is False and "client_id" in out["detail"]  # not configured → no network

    from scc.adapters.upload_post import UploadPost
    app.store["upload_post.api_key"] = "KEY-abcdef"
    app.post("/api/connect/upload_post", {"user": "me"})
    monkeypatch.setattr(UploadPost, "verify", lambda self, platform: (True, "profile me ok"),
                        raising=False)
    out = app.post("/api/connect/verify", {"platform": "instagram"})[1]
    assert out == {"ok": True, "detail": "profile me ok"}

    app.store.update({"youtube.refresh_token": "r", "meta.page_token": "p",
                      "tiktok.refresh_token": "t"})
    assert app.post("/api/disconnect", {"platform": "youtube", "route": "direct"})[0] == 200
    assert "youtube.refresh_token" not in app.store
    app.post("/api/disconnect", {"platform": "facebook", "route": "direct"})
    assert "meta.page_token" not in app.store
    app.post("/api/disconnect", {"platform": "tiktok", "route": "upload_post"})
    assert "upload_post.api_key" not in app.store and "tiktok.refresh_token" in app.store
    assert app.post("/api/disconnect", {"platform": "tiktok", "route": "dryrun"})[0] == 400


def test_meta_token_page_flow(app, monkeypatch):
    from scc.adapters.meta import Meta
    pages = [{"id": "111", "name": "Why AI", "access_token": "PAGE-TOKEN-SECRET", "ig_id": "9",
              "ig_username": "whyai"},
             {"id": "222", "name": "Other", "access_token": "P2", "ig_id": None,
              "ig_username": None}]
    monkeypatch.setattr(Meta, "list_pages", lambda self, tok: pages, raising=False)
    used = {}

    def use_page(self, page):
        used.update(page)
        self.cfg.set("meta", "page_id", page["id"])
        return f"Page {page['name']}"

    monkeypatch.setattr(Meta, "use_page", use_page, raising=False)
    s, raw, _ = app.req("POST", "/api/connect/meta/token", {"user_token": "USER-TOKEN-XYZ"},
                        raw=True)
    assert s == 200 and b"PAGE-TOKEN-SECRET" not in raw and b"USER-TOKEN-XYZ" not in raw
    assert json.loads(raw)["pages"][0] == {"id": "111", "name": "Why AI", "ig_username": "whyai"}
    assert app.post("/api/connect/meta/page", {"page_id": "999"})[0] == 400
    out = app.post("/api/connect/meta/page", {"page_id": "111"})[1]
    assert out == {"ok": True, "detail": "Page Why AI"}
    assert used["access_token"] == "PAGE-TOKEN-SECRET"
    assert state(app)["connections"]["meta"]["page_id"] == "111"
    server._meta_pages["expires"] = time.monotonic() - 1  # expiry
    assert app.post("/api/connect/meta/page", {"page_id": "111"})[0] == 400

    def boom(self, tok):
        raise RuntimeError(f"HTTP 400 for token {tok}")

    monkeypatch.setattr(Meta, "list_pages", boom, raising=False)
    s, raw, _ = app.req("POST", "/api/connect/meta/token", {"user_token": "USER-TOKEN-XYZ"},
                        raw=True)
    assert s == 400 and b"USER-TOKEN-XYZ" not in raw


def test_connect_job_states(app, monkeypatch):
    from scc.adapters.youtube import YouTube
    gate = threading.Event()

    def fake_connect(self, port=8789):
        gate.wait(5)
        return "My Channel"

    monkeypatch.setattr(YouTube, "connect", fake_connect)
    assert app.post("/api/connect/youtube/start")[0] == 200
    assert state(app)["connect"]["youtube"] == "waiting for browser"
    assert app.post("/api/connect/youtube/start")[0] == 409
    gate.set()
    assert wait_for(lambda: state(app)["connect"]["youtube"].startswith("connected"), 50)
    assert state(app)["connect"]["youtube"] == "connected: My Channel"

    def failing(self, port=8789):
        raise RuntimeError("no client id")

    monkeypatch.setattr(YouTube, "connect", failing)
    app.post("/api/connect/youtube/start")
    assert wait_for(lambda: state(app)["connect"]["youtube"].startswith("error"), 50)
    assert state(app)["connect"]["youtube"] == "error: no client id"


def test_scheduler_endpoints_and_run(app, monkeypatch, tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "install-scheduler.sh").write_text("echo installed\n")
    (scripts / "uninstall-scheduler.sh").write_text("echo removed; exit 3\n")
    monkeypatch.setattr(server, "SCRIPTS", scripts)
    assert app.post("/api/scheduler/install")[1] == {"ok": True, "output": "installed"}
    assert app.post("/api/scheduler/uninstall")[1] == {"ok": False, "output": "removed"}
    assert app.post("/api/run")[0] == 200
    assert wait_for(lambda: state(app)["scheduler"]["last_run"], 50)
    sch = state(app)["scheduler"]
    assert sch["last_result"].startswith("processed") and sch["in_process"] is False


def test_scheduler_pass_skips_future_and_posts_due(app):
    add_video(app, "z__short-1-zed")
    import_all(app)
    pid = next(iter(posts(app).values()))["id"]
    app.post(f"/api/posts/{pid}", {"caption": "cap #ai"})
    future = (datetime.now(UTC) + timedelta(days=2)).isoformat()
    app.post(f"/api/posts/{pid}", {"scheduled_at": future})
    app.post(f"/api/posts/{pid}/approve")
    jobs.scheduler_pass()
    assert posts(app)["z__short-1-zed.mp4"]["bucket"] == "scheduled"  # not due, nothing sent
    app.post("/api/schedule/clear", {"ids": [pid]})
    jobs.scheduler_pass()
    assert posts(app)["z__short-1-zed.mp4"]["bucket"] == "draft"  # unscheduling never posts
    app.post(f"/api/posts/{pid}/approve")  # approved with no time = post asap
    jobs.scheduler_pass()
    assert posts(app)["z__short-1-zed.mp4"]["bucket"] == "posted"  # dryrun → simulated


def test_publish_now_and_remove_guard(app):
    add_video(app, "p__short-1-pub")
    import_all(app)
    pid = next(iter(posts(app).values()))["id"]
    app.post(f"/api/posts/{pid}", {"caption": "cap #ai"})
    assert app.post(f"/api/posts/{pid}/publish", {"confirm": "post"})[0] == 200
    assert wait_for(lambda: posts(app)["p__short-1-pub.mp4"]["bucket"] == "posted")
    con = db.connect()
    db.update_delivery(con, pid, "youtube", status="uploading")
    assert app.post(f"/api/posts/{pid}/remove")[0] == 409
    assert app.post(f"/api/posts/{pid}/retry", {"platforms": ["youtube"]})[0] == 409
    db.update_delivery(con, pid, "youtube", status="failed")
    assert app.post(f"/api/posts/{pid}/retry", {"platforms": ["youtube"]})[0] == 200
    assert app.post(f"/api/posts/{pid}/remove")[0] == 200


def test_media_range_and_thumb(app):
    add_video(app, "m__short-1-media")
    add_video(app, "tiny__short-1-tiny", seconds=0.4)
    import_all(app)
    ps = posts(app)
    pid = ps["m__short-1-media.mp4"]["id"]
    size = Path(ps["m__short-1-media.mp4"]["video_path"]).stat().st_size
    s, body, r = app.get(f"/media/{pid}", headers={"Range": "bytes=0-99"}, raw=True)
    assert s == 206 and len(body) == 100 and r.getheader("Content-Range") == f"bytes 0-99/{size}"
    s, body, r = app.get(f"/thumb/{pid}.jpg", raw=True)
    assert s == 200 and body[:3] == b"\xff\xd8\xff" and r.getheader("Content-Type") == "image/jpeg"
    sha = media.sha256(ps["m__short-1-media.mp4"]["video_path"])
    assert (config.DATA_DIR / "thumbs" / f"{sha}.jpg").exists()
    tid = ps["tiny__short-1-tiny.mp4"]["id"]  # very short clip falls back to frame 0
    s, body, _ = app.get(f"/thumb/{tid}.jpg", raw=True)
    assert s == 200 and body[:3] == b"\xff\xd8\xff"
    assert app.get("/thumb/ffffffffff.jpg")[0] == 404
    assert app.get("/media/ffffffffff")[0] == 404


def test_duplicates_use_transcripts_then_slugs(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    (tmp_path / "transcripts").mkdir()
    words = "so how many ai ideas did you lose this week because they had no home " * 3
    (tmp_path / "transcripts" / "a.txt").write_text(words)
    (tmp_path / "transcripts" / "b.txt").write_text(words + " anyway subscribe")
    (tmp_path / "transcripts" / "c.txt").write_text("completely different talk about blender " * 5)
    mk = lambda i, sha, name: {"id": i, "sha256": sha, "video_path": f"/x/{name}.mp4"}
    posts = [mk("1", "a", "p__short-1-lost-ideas"), mk("2", "b", "q__01-how-many-ideas"),
             mk("3", "c", "r__short-1-lost-ideas"),               # same slug, different talk
             mk("4", None, "s__s2-notion"), mk("5", None, "t__short-3-notion")]  # no transcripts
    d = server.duplicates(posts)
    assert d["1"] == ["2"] and d["2"] == ["1"]
    assert d["3"] == []
    assert d["4"] == ["5"]
