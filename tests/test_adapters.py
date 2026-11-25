"""Adapter request-sequence and never-double-post tests (httpx.MockTransport, no network)."""

from __future__ import annotations

import json
import subprocess
from urllib.parse import parse_qs

import httpx
import pytest

from scc import config, secrets
from scc.adapters import base, meta, tiktok, upload_post, youtube
from scc.adapters.base import Ambiguous, NotSubmitted
from scc.adapters.dryrun import DryRun
from scc.adapters.meta import Meta
from scc.adapters.tiktok import TikTok, chunk_plan
from scc.adapters.upload_post import UploadPost
from scc.adapters.youtube import YouTube

MB = 2**20
POST = {"id": 7, "title": "Books to the desk", "caption": "A caption #ai", "overrides": {}}


# ---------------------------------------------------------------- fixtures ----
class Net:
    """Scripted HTTP world. Routes match on method + URL substring; first match wins."""

    def __init__(self):
        self.routes: list[tuple[str, str, object]] = []
        self.calls: list[httpx.Request] = []

    def on(self, method, contains, response):
        """response: httpx.Response, a callable(request) -> Response, a list (consumed in
        order, last one repeats), or an Exception to raise."""
        self.routes.append((method, contains, response))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        for method, contains, resp in self.routes:
            if request.method == method and contains in str(request.url):
                if isinstance(resp, list):
                    resp = resp.pop(0) if len(resp) > 1 else resp[0]
                if isinstance(resp, Exception):
                    raise resp
                return resp(request) if callable(resp) else resp
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    def to(self, contains: str) -> list[httpx.Request]:
        return [c for c in self.calls if contains in str(c.url)]

    def urls(self) -> list[str]:
        return [f"{c.method} {str(c.url).split('?')[0]}" for c in self.calls]


def J(data, status=200, headers=None):
    return httpx.Response(status, json=data, headers=headers)


def form(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


@pytest.fixture
def store(monkeypatch):
    kv: dict[str, str] = {}
    monkeypatch.setattr(secrets, "get", kv.get)
    monkeypatch.setattr(secrets, "put", kv.__setitem__)

    def require(name):
        if not kv.get(name):
            raise secrets.MissingSecret(f"Missing Keychain secret '{name}'")
        return kv[name]

    monkeypatch.setattr(secrets, "require", require)
    return kv


@pytest.fixture
def net(monkeypatch):
    n = Net()

    def fake_client(timeout: float = 60):
        return httpx.Client(transport=httpx.MockTransport(n), follow_redirects=False)

    for mod in (base, youtube, meta, tiktok, upload_post):
        if hasattr(mod, "client"):
            monkeypatch.setattr(mod, "client", fake_client)
    monkeypatch.setattr(meta.time, "sleep", lambda s: None)
    return n


@pytest.fixture(scope="session")
def mp4(tmp_path_factory):
    path = tmp_path_factory.mktemp("vid") / "short.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=108x192:d=2",
                    "-f", "lavfi", "-i", "anullsrc", "-shortest", "-c:v", "libx264",
                    "-c:a", "aac", str(path)], check=True)
    return path


def make_cfg(**sections) -> config.Config:
    cfg = config.Config({s: dict(v) for s, v in config.DEFAULTS.items()})
    for section, values in sections.items():
        cfg.data[section].update(values)
    return cfg


def log(level, msg):
    pass


# ---------------------------------------------------------------- base --------
def test_file_body_streams_with_content_length(mp4, net):
    net.on("PUT", "up.example", J({}))
    with httpx.Client(transport=httpx.MockTransport(net)) as http:
        http.put("https://up.example/x", headers={"Content-Length": str(mp4.stat().st_size)},
                 content=base.file_body(mp4))
    req = net.calls[0]
    assert req.headers["content-length"] == str(mp4.stat().st_size)
    assert "transfer-encoding" not in req.headers
    assert req.content == mp4.read_bytes()


def test_commit_classification():
    ok = lambda: httpx.Response(200)
    assert base.commit(ok, "x").status_code == 200

    def boom(exc):
        def send():
            raise exc
        return send

    with pytest.raises(Ambiguous):
        base.commit(boom(httpx.ReadTimeout("t")), "x")
    with pytest.raises(Ambiguous):
        base.commit(boom(httpx.RemoteProtocolError("p")), "x")
    with pytest.raises(NotSubmitted):  # never reached the server
        base.commit(boom(httpx.ConnectError("c")), "x")
    with pytest.raises(Ambiguous):
        base.commit(lambda: httpx.Response(503), "x")
    with pytest.raises(NotSubmitted):
        base.commit(lambda: httpx.Response(400), "x")
    with pytest.raises(NotSubmitted):
        base.pre(boom(httpx.ReadTimeout("t")), "x")  # pre-commit timeouts are safe


def test_dryrun_verify():
    assert DryRun().verify("youtube") == (True, "dry run")


# ---------------------------------------------------------------- YouTube -----
@pytest.fixture
def yt(store, net):
    store.update({"youtube.client_secret": "cs", "youtube.refresh_token": "rt"})
    net.on("POST", "oauth2.googleapis.com/token", J({"access_token": "at", "expires_in": 3600}))
    return YouTube(make_cfg(youtube={"client_id": "cid"})), net


def yt_session(net, put):
    net.on("POST", "upload/youtube/v3/videos",
           httpx.Response(200, headers={"Location": "https://upload.example/session1"}))
    net.on("PUT", "upload.example/session1", put)


def test_youtube_happy_path_and_private_lock(yt, mp4):
    adapter, net = yt
    yt_session(net, J({"id": "VID1", "status": {"privacyStatus": "private",
                                                "uploadStatus": "uploaded"}}, 201))
    res = adapter.publish(POST, ["youtube"], mp4, log)["youtube"]
    assert res.status == "processing" and res.remote_id == "VID1"
    assert res.url == "https://youtube.com/shorts/VID1"
    assert "Private" in res.meta["note"]  # asked for public, got private
    init, put = net.to("upload/youtube/v3/videos")[0], net.to("session1")[0]
    assert init.url.params["uploadType"] == "resumable"
    assert init.url.params["part"] == "snippet,status"
    size = str(mp4.stat().st_size)
    assert init.headers["x-upload-content-length"] == size
    assert init.headers["x-upload-content-type"] == "video/mp4"
    assert init.headers["authorization"] == "Bearer at"
    body = json.loads(init.content)
    assert body["snippet"]["title"] == "Books to the desk"
    assert "#Shorts" in body["snippet"]["description"]
    assert body["status"]["selfDeclaredMadeForKids"] is False
    assert put.headers["content-length"] == size and "transfer-encoding" not in put.headers
    assert put.content == mp4.read_bytes()


def test_youtube_commit_timeout_is_ambiguous(yt, mp4):
    adapter, net = yt
    yt_session(net, httpx.ReadTimeout("slow"))
    with pytest.raises(Ambiguous):
        adapter.publish(POST, ["youtube"], mp4, log)


def test_youtube_commit_5xx_and_308_ambiguous(yt, mp4):
    adapter, net = yt
    yt_session(net, httpx.Response(503))
    with pytest.raises(Ambiguous):
        adapter.publish(POST, ["youtube"], mp4, log)
    adapter2, net2 = yt
    net2.routes.clear()
    net2.on("POST", "oauth2.googleapis.com/token", J({"access_token": "at"}))
    yt_session(net2, httpx.Response(308))
    with pytest.raises(Ambiguous):
        YouTube(adapter2.cfg).publish(POST, ["youtube"], mp4, log)


def test_youtube_precommit_400_not_submitted(yt, mp4):
    adapter, net = yt
    net.on("POST", "upload/youtube/v3/videos", J({"error": "bad"}, 400))
    with pytest.raises(NotSubmitted):
        adapter.publish(POST, ["youtube"], mp4, log)
    assert not net.to("session1")


def test_youtube_precommit_network_error_not_submitted(yt, mp4):
    adapter, net = yt
    net.on("POST", "upload/youtube/v3/videos", httpx.ReadTimeout("x"))
    with pytest.raises(NotSubmitted):
        adapter.publish(POST, ["youtube"], mp4, log)


def test_youtube_commit_400_not_submitted(yt, mp4):
    adapter, net = yt
    yt_session(net, J({"error": {"errors": [{"reason": "quotaExceeded"}]}}, 403))
    with pytest.raises(NotSubmitted):
        adapter.publish(POST, ["youtube"], mp4, log)


def test_youtube_refresh_failure_says_reconnect(store, net, mp4):
    store.update({"youtube.client_secret": "cs", "youtube.refresh_token": "rt"})
    net.on("POST", "oauth2.googleapis.com/token", J({"error": "invalid_grant"}, 400))
    with pytest.raises(NotSubmitted, match="scc connect youtube"):
        YouTube(make_cfg(youtube={"client_id": "c"})).publish(POST, ["youtube"], mp4, log)


def test_youtube_missing_secret_not_submitted(store, net, mp4):
    with pytest.raises(NotSubmitted, match="scc connect youtube"):
        YouTube(make_cfg(youtube={"client_id": "c"})).publish(POST, ["youtube"], mp4, log)


def test_youtube_poll_mapping(yt):
    adapter, net = yt
    delivery = {"remote_id": "V", "url": "https://youtube.com/shorts/V", "meta": {}}

    def videos(status, pd=None):
        return J({"items": [{"status": status, "processingDetails": pd or {}}]})

    net.on("GET", "youtube/v3/videos",
           [videos({"uploadStatus": "processed", "privacyStatus": "private"})])
    r = adapter.poll(POST, "youtube", delivery)
    assert r.status == "private" and r.url == delivery["url"]
    net.routes.pop()
    net.on("GET", "youtube/v3/videos",
           videos({"uploadStatus": "processed", "privacyStatus": "public"}))
    assert adapter.poll(POST, "youtube", delivery).status == "published"
    net.routes.pop()
    net.on("GET", "youtube/v3/videos", videos({"uploadStatus": "uploaded"}))
    assert adapter.poll(POST, "youtube", delivery) is None
    net.routes.pop()
    net.on("GET", "youtube/v3/videos",
           videos({"uploadStatus": "rejected", "rejectionReason": "duplicate"}))
    r = adapter.poll(POST, "youtube", delivery)
    assert r.status == "failed" and "duplicate" in r.error
    net.routes.pop()
    net.on("GET", "youtube/v3/videos",
           videos({"uploadStatus": "uploaded"}, {"processingStatus": "failed",
                                                 "processingFailureReason": "transcodeFailed"}))
    assert "transcodeFailed" in adapter.poll(POST, "youtube", delivery).error


def test_youtube_verify(yt):
    adapter, net = yt
    net.on("GET", "youtube/v3/channels", J({"items": [{"snippet": {"title": "EJ Channel"}}]}))
    ok, msg = adapter.verify("youtube")
    assert ok and "EJ Channel" in msg
    assert net.to("youtube/v3/channels")[0].url.params["mine"] == "true"
    net.routes.pop()
    net.on("GET", "youtube/v3/channels", J({"items": []}))
    assert adapter.verify("youtube")[0] is False
    net.routes.pop()
    net.on("GET", "youtube/v3/channels", J({"error": {"message": "nope"}}, 401))
    ok, msg = adapter.verify("youtube")
    assert not ok and "401" in msg


def test_youtube_verify_not_connected_never_raises(store, net):
    ok, msg = YouTube(make_cfg()).verify("youtube")
    assert not ok and "scc connect youtube" in msg


def test_youtube_readiness_no_network(store, net):
    ok, _ = YouTube(make_cfg()).readiness("youtube")
    assert not ok and not net.calls


# ---------------------------------------------------------------- Meta --------
@pytest.fixture
def mt(store, net):
    store["meta.page_token"] = "ptok"
    cfg = make_cfg(meta={"page_id": "PAGE", "ig_user_id": "IG"})
    return Meta(cfg), net


def test_instagram_happy_path(mt, mp4):
    adapter, net = mt
    net.on("POST", "/IG/media_publish", J({"id": "MEDIA1"}))
    net.on("POST", "/IG/media", J({"id": "CONT1"}))
    net.on("POST", "rupload.facebook.com", J({"success": True, "message": "Upload successful."}))
    net.on("GET", "/CONT1", [J({"status_code": "IN_PROGRESS"}), J({"status_code": "FINISHED"})])
    net.on("GET", "/MEDIA1", J({"permalink": "https://instagram.com/reel/abc/"}))
    res = adapter.publish(POST, ["instagram"], mp4, log)["instagram"]
    assert res.status == "published" and res.remote_id == "MEDIA1"
    assert res.url == "https://instagram.com/reel/abc/"
    assert net.urls()[:3] == [
        "POST https://graph.facebook.com/v25.0/IG/media",
        "POST https://rupload.facebook.com/ig-api-upload/v25.0/CONT1",
        "GET https://graph.facebook.com/v25.0/CONT1",
    ]
    f = form(net.to("/IG/media")[0])
    assert f["media_type"] == "REELS" and f["upload_type"] == "resumable"
    assert f["caption"] == "A caption #ai" and f["share_to_feed"] == "true"
    up = net.to("rupload.facebook.com")[0]
    assert up.headers["authorization"] == "OAuth ptok"
    assert up.headers["offset"] == "0" and up.headers["file_size"] == str(mp4.stat().st_size)
    assert up.headers["content-length"] == str(mp4.stat().st_size)
    assert "transfer-encoding" not in up.headers
    assert up.content == mp4.read_bytes()
    assert form(net.to("media_publish")[0])["creation_id"] == "CONT1"


def ig_ready(net, publish):
    net.on("POST", "/IG/media_publish", publish)
    net.on("POST", "/IG/media", J({"id": "CONT1"}))
    net.on("POST", "rupload.facebook.com", J({"success": True}))
    net.on("GET", "/CONT1", J({"status_code": "FINISHED"}))


def test_instagram_publish_timeout_ambiguous_and_5xx(mt, mp4):
    adapter, net = mt
    ig_ready(net, httpx.ReadTimeout("slow"))
    with pytest.raises(Ambiguous):
        adapter.publish(POST, ["instagram"], mp4, log)
    net.routes.clear()
    ig_ready(net, httpx.Response(500, text="boom"))
    with pytest.raises(Ambiguous):
        adapter.publish(POST, ["instagram"], mp4, log)


def test_instagram_publish_400_not_submitted(mt, mp4):
    adapter, net = mt
    ig_ready(net, J({"error": {"message": "The media is not ready"}}, 400))
    with pytest.raises(NotSubmitted):
        adapter.publish(POST, ["instagram"], mp4, log)


def test_instagram_container_400_not_submitted(mt, mp4):
    adapter, net = mt
    net.on("POST", "/IG/media", J({"error": {"message": "bad"}}, 400))
    with pytest.raises(NotSubmitted):
        adapter.publish(POST, ["instagram"], mp4, log)
    assert not net.to("media_publish")


def test_instagram_upload_failure_not_submitted(mt, mp4):
    adapter, net = mt
    net.on("POST", "/IG/media", J({"id": "CONT1"}))
    net.on("POST", "rupload.facebook.com", J({"debug_info": {"message": "x"}}, 400))
    with pytest.raises(NotSubmitted):
        adapter.publish(POST, ["instagram"], mp4, log)
    assert not net.to("media_publish")


def test_instagram_processing_error_includes_status_text(mt, mp4):
    adapter, net = mt
    net.on("POST", "/IG/media", J({"id": "CONT1"}))
    net.on("POST", "rupload.facebook.com", J({"success": True}))
    net.on("GET", "/CONT1", J({"status_code": "ERROR", "status": "Error: aspect ratio 2207026"}))
    with pytest.raises(NotSubmitted, match="aspect ratio 2207026"):
        adapter.publish(POST, ["instagram"], mp4, log)
    assert not net.to("media_publish")


def test_instagram_permalink_failure_does_not_lose_the_post(mt, mp4):
    adapter, net = mt
    ig_ready(net, J({"id": "MEDIA1"}))
    net.on("GET", "/MEDIA1", httpx.Response(500, text="not json"))
    res = adapter.publish(POST, ["instagram"], mp4, log)["instagram"]
    assert res.status == "published" and res.url is None


def test_meta_not_connected_not_submitted(store, net, mp4):
    with pytest.raises(NotSubmitted, match="scc connect meta"):
        Meta(make_cfg(meta={"page_id": "P"}))._facebook(POST, mp4, log)


def test_facebook_happy_path(mt, mp4):
    adapter, net = mt
    net.on("POST", "/PAGE/video_reels", [
        J({"video_id": "VID9", "upload_url": "https://rupload.facebook.com/video-upload/v25.0/VID9"}),
        J({"success": True}),
    ])
    net.on("POST", "rupload.facebook.com", J({"success": True}))
    res = adapter.publish(POST, ["facebook"], mp4, log)["facebook"]
    assert res.status == "processing" and res.remote_id == "VID9"
    assert res.url == "https://www.facebook.com/reel/VID9"
    assert net.urls() == [
        "POST https://graph.facebook.com/v25.0/PAGE/video_reels",
        "POST https://rupload.facebook.com/video-upload/v25.0/VID9",
        "POST https://graph.facebook.com/v25.0/PAGE/video_reels",
    ]
    start, up, finish = net.calls
    assert form(start)["upload_phase"] == "start"
    assert up.headers["authorization"] == "OAuth ptok"
    assert up.headers["file_size"] == str(mp4.stat().st_size) and up.headers["offset"] == "0"
    f = form(finish)
    assert (f["upload_phase"], f["video_id"], f["video_state"]) == ("finish", "VID9", "PUBLISHED")
    assert f["description"] == "A caption #ai"


def fb_ready(net, finish):
    net.on("POST", "/PAGE/video_reels", [J({"video_id": "VID9"}), finish])
    net.on("POST", "rupload.facebook.com", J({"success": True}))


def test_facebook_finish_timeout_ambiguous(mt, mp4):
    adapter, net = mt
    fb_ready(net, httpx.ReadTimeout("slow"))
    with pytest.raises(Ambiguous):
        adapter.publish(POST, ["facebook"], mp4, log)


def test_facebook_finish_400_not_submitted(mt, mp4):
    adapter, net = mt
    fb_ready(net, J({"error": {"message": "(#100) bad"}}, 400))
    with pytest.raises(NotSubmitted):
        adapter.publish(POST, ["facebook"], mp4, log)


def test_facebook_finish_success_false_not_submitted(mt, mp4):
    adapter, net = mt
    fb_ready(net, J({"success": False}))
    with pytest.raises(NotSubmitted):
        adapter.publish(POST, ["facebook"], mp4, log)


def test_facebook_start_400_not_submitted(mt, mp4):
    adapter, net = mt
    net.on("POST", "/PAGE/video_reels", J({"error": {"message": "no perm"}}, 403))
    with pytest.raises(NotSubmitted):
        adapter.publish(POST, ["facebook"], mp4, log)
    assert len(net.calls) == 1


def test_facebook_poll_mapping(mt):
    adapter, net = mt
    d = {"remote_id": "VID9", "url": "https://www.facebook.com/reel/VID9", "meta": {}}

    def status(**s):
        return J({"status": s})

    net.on("GET", "/VID9", status(video_status="ready", publishing_phase={"status": "complete"}))
    assert adapter.poll(POST, "facebook", d).status == "published"
    assert net.to("/VID9")[0].url.params["fields"] == "status"
    net.routes.pop()
    net.on("GET", "/VID9", status(video_status="processing"))
    assert adapter.poll(POST, "facebook", d) is None
    net.routes.pop()
    net.on("GET", "/VID9", status(video_status="error"))
    assert adapter.poll(POST, "facebook", d).status == "failed"
    assert adapter.poll(POST, "instagram", d) is None  # IG is final at publish time


def test_meta_verify(mt):
    adapter, net = mt
    net.on("GET", "/PAGE", J({"name": "My Page", "id": "PAGE"}))
    net.on("GET", "/IG", J({"username": "creator", "id": "IG"}))
    assert adapter.verify("facebook") == (True, "Facebook Page My Page")
    assert adapter.verify("instagram") == (True, "Instagram @creator")
    assert net.to("/PAGE")[0].url.params["fields"] == "name"
    net.routes.clear()
    net.on("GET", "/PAGE", J({"error": {"message": "Invalid OAuth access token"}}, 400))
    ok, msg = adapter.verify("facebook")
    assert not ok and "scc connect meta" in msg and "ptok" not in msg


def test_meta_verify_not_connected(store, net):
    ok, msg = Meta(make_cfg()).verify("facebook")
    assert not ok and "scc connect meta" in msg and not net.calls


def test_meta_list_pages_and_use_page(store, net):
    store["meta.app_secret"] = "shh"
    cfg = make_cfg(meta={"app_id": "APP"})
    adapter = Meta(cfg)
    net.on("GET", "oauth/access_token", J({"access_token": "LONG", "expires_in": 5184000}))
    net.on("GET", "me/accounts", J({"data": [
        {"id": "P1", "name": "Page One", "access_token": "PT1",
         "instagram_business_account": {"id": "IG1", "username": "one"}},
        {"id": "P2", "name": "Page Two", "access_token": "PT2"},
    ]}))
    pages = adapter.list_pages("SHORT")
    assert pages == [
        {"id": "P1", "name": "Page One", "access_token": "PT1", "ig_id": "IG1", "ig_username": "one"},
        {"id": "P2", "name": "Page Two", "access_token": "PT2", "ig_id": None, "ig_username": None},
    ]
    ex = net.to("oauth/access_token")[0].url.params
    assert ex["grant_type"] == "fb_exchange_token" and ex["client_id"] == "APP"
    assert ex["client_secret"] == "shh" and ex["fb_exchange_token"] == "SHORT"
    acc = net.to("me/accounts")[0].url.params
    assert acc["access_token"] == "LONG"
    assert acc["fields"] == "id,name,access_token,instagram_business_account{id,username}"

    assert adapter.use_page(pages[0]) == "Facebook Page 'Page One', Instagram @one"
    assert store["meta.page_token"] == "PT1"
    assert cfg.get("meta", "page_id") == "P1" and cfg.get("meta", "ig_user_id") == "IG1"
    assert "none linked" in adapter.use_page(pages[1])
    assert cfg.get("meta", "ig_user_id") == ""


def test_meta_list_pages_errors_are_human_and_clean(store, net):
    with pytest.raises(RuntimeError, match="meta.app_id"):
        Meta(make_cfg()).list_pages("SHORT")
    adapter = Meta(make_cfg(meta={"app_id": "APP"}))
    with pytest.raises(RuntimeError, match="app_secret"):
        adapter.list_pages("SHORT")
    store["meta.app_secret"] = "shh"
    net.on("GET", "oauth/access_token", J({"error": {"message": "Session has expired"}}, 400))
    with pytest.raises(RuntimeError) as e:
        adapter.list_pages("SHORTSECRET")
    assert "SHORTSECRET" not in str(e.value) and "shh" not in str(e.value)
    net.routes.clear()
    net.on("GET", "oauth/access_token", J({"access_token": "LONG"}))
    net.on("GET", "me/accounts", J({"data": []}))
    with pytest.raises(RuntimeError, match="any Facebook Pages"):
        adapter.list_pages("SHORT")
    net.routes.clear()
    net.on("GET", "oauth/access_token", httpx.ConnectError("down"))
    with pytest.raises(RuntimeError, match="reach Meta"):
        adapter.list_pages("SHORT")


# ---------------------------------------------------------------- TikTok ------
@pytest.fixture
def tt(store, net):
    store.update({"tiktok.client_secret": "cs", "tiktok.refresh_token": "rt0"})
    net.on("POST", "oauth/token", J({"access_token": "tat", "refresh_token": "rt1",
                                      "expires_in": 86400}))
    return TikTok(make_cfg(tiktok={"client_key": "ck"})), net


def tt_init(net, **extra):
    net.on("POST", "inbox/video/init", J({"data": {"publish_id": "PUB1",
                                                  "upload_url": "https://up.tiktok.example/u1"},
                                           "error": {"code": "ok"}}))


def test_tiktok_chunk_plan():
    assert chunk_plan(40 * MB) == (40 * MB, 1)
    assert chunk_plan(3 * MB) == (3 * MB, 1)
    assert chunk_plan(64 * MB) == (64 * MB, 1)
    assert chunk_plan(150 * MB) == (10 * MB, 15)
    for size in (64 * MB + 1, 65 * MB, 150 * MB + 7, 300 * MB + 123, 4 * 2**30):
        chunk, count = chunk_plan(size)
        assert 5 * MB <= chunk <= 64 * MB and 1 <= count <= 1000
        last = size - (count - 1) * chunk
        assert chunk <= last <= 128 * MB and count == size // chunk


def test_tiktok_inbox_happy_path(tt, mp4):
    adapter, net = tt
    tt_init(net)
    net.on("PUT", "up.tiktok.example", httpx.Response(201))
    res = adapter.publish(POST, ["tiktok"], mp4, log)["tiktok"]
    assert res.status == "processing" and res.remote_id == "PUB1"
    assert res.meta["inbox"] is True and res.meta["caption_to_paste"] == "A caption #ai"
    size = mp4.stat().st_size
    init = net.to("inbox/video/init")[0]
    assert init.headers["authorization"] == "Bearer tat"
    assert json.loads(init.content) == {"source_info": {
        "source": "FILE_UPLOAD", "video_size": size, "chunk_size": size, "total_chunk_count": 1}}
    put = net.to("up.tiktok.example")[0]
    assert put.headers["content-range"] == f"bytes 0-{size - 1}/{size}"
    assert put.headers["content-length"] == str(size) and put.headers["content-type"] == "video/mp4"
    assert put.content == mp4.read_bytes()
    assert tt[0]._token is not None


def test_tiktok_refresh_token_rotation_is_saved(tt, store):
    adapter, _ = tt
    assert adapter._access_token() == "tat"
    assert store["tiktok.refresh_token"] == "rt1"


def test_tiktok_multichunk_upload(tt, tmp_path):
    adapter, net = tt
    big = tmp_path / "big.mp4"
    with big.open("wb") as fh:
        fh.truncate(66 * MB)  # sparse: 6 chunks, last = 16 MB
    tt_init(net)
    net.on("PUT", "up.tiktok.example", lambda r: httpx.Response(
        201 if r.headers["content-range"].endswith(f"{66 * MB - 1}/{66 * MB}") else 206))
    adapter.publish(POST, ["tiktok"], big, log)
    puts = net.to("up.tiktok.example")
    assert len(puts) == 6
    init = json.loads(net.to("inbox/video/init")[0].content)["source_info"]
    assert init["chunk_size"] == 10 * MB and init["total_chunk_count"] == 6
    ranges = [p.headers["content-range"] for p in puts]
    assert ranges[0] == f"bytes 0-{10 * MB - 1}/{66 * MB}"
    assert ranges[1] == f"bytes {10 * MB}-{20 * MB - 1}/{66 * MB}"
    assert ranges[-1] == f"bytes {50 * MB}-{66 * MB - 1}/{66 * MB}"
    assert [int(p.headers["content-length"]) for p in puts] == [10 * MB] * 5 + [16 * MB]


def test_tiktok_final_chunk_timeout_ambiguous(tt, mp4):
    adapter, net = tt
    tt_init(net)
    net.on("PUT", "up.tiktok.example", httpx.ReadTimeout("slow"))
    with pytest.raises(Ambiguous):
        adapter.publish(POST, ["tiktok"], mp4, log)


def test_tiktok_nonfinal_chunk_failure_not_submitted(tt, tmp_path):
    adapter, net = tt
    big = tmp_path / "big.mp4"
    with big.open("wb") as fh:
        fh.truncate(66 * MB)
    tt_init(net)
    net.on("PUT", "up.tiktok.example", httpx.Response(500))
    with pytest.raises(NotSubmitted):
        adapter.publish(POST, ["tiktok"], big, log)
    assert len(net.to("up.tiktok.example")) == 1  # stopped at the first bad chunk


def test_tiktok_final_chunk_400_not_submitted(tt, mp4):
    adapter, net = tt
    tt_init(net)
    net.on("PUT", "up.tiktok.example", httpx.Response(416))
    with pytest.raises(NotSubmitted):
        adapter.publish(POST, ["tiktok"], mp4, log)


def test_tiktok_init_errors_not_submitted(tt, mp4):
    adapter, net = tt
    net.on("POST", "inbox/video/init", J({"error": {"code": "spam_risk_too_many_pending_share",
                                                    "message": "limit"}}, 403))
    with pytest.raises(NotSubmitted, match="HTTP 403"):
        adapter.publish(POST, ["tiktok"], mp4, log)
    net.routes.pop()
    net.on("POST", "inbox/video/init", J({"data": {}, "error": {"code": "invalid_param",
                                                                 "message": "bad"}}))
    with pytest.raises(NotSubmitted, match="invalid_param"):
        adapter.publish(POST, ["tiktok"], mp4, log)
    assert not net.to("up.tiktok.example")


def test_tiktok_direct_post_queries_creator_info(store, net, mp4):
    store.update({"tiktok.client_secret": "cs", "tiktok.refresh_token": "rt0"})
    net.on("POST", "oauth/token", J({"access_token": "tat", "refresh_token": "rt1"}))
    net.on("POST", "creator_info/query", J({"data": {"privacy_level_options": [
        "PUBLIC_TO_EVERYONE", "SELF_ONLY"]}, "error": {"code": "ok"}}))
    net.on("POST", "publish/video/init", J({"data": {"publish_id": "P2",
                                                    "upload_url": "https://up.tiktok.example/u"},
                                             "error": {"code": "ok"}}))
    net.on("PUT", "up.tiktok.example", httpx.Response(201))
    adapter = TikTok(make_cfg(tiktok={"client_key": "ck", "mode": "direct",
                                      "privacy_level": "SELF_ONLY"}))
    res = adapter.publish(POST, ["tiktok"], mp4, log)["tiktok"]
    assert res.remote_id == "P2" and res.meta["inbox"] is False
    order = [u.split("/v2/")[-1] for u in net.urls()]
    assert order.index("post/publish/creator_info/query/") < order.index("post/publish/video/init/")
    post_info = json.loads(net.to("publish/video/init")[0].content)["post_info"]
    assert post_info == {"title": "A caption #ai", "privacy_level": "SELF_ONLY"}


def test_tiktok_direct_post_privacy_mismatch_not_submitted(store, net, mp4):
    store.update({"tiktok.client_secret": "cs", "tiktok.refresh_token": "rt0"})
    net.on("POST", "oauth/token", J({"access_token": "tat"}))
    net.on("POST", "creator_info/query", J({"data": {"privacy_level_options": ["SELF_ONLY"]},
                                            "error": {"code": "ok"}}))
    adapter = TikTok(make_cfg(tiktok={"client_key": "ck", "mode": "direct",
                                      "privacy_level": "PUBLIC_TO_EVERYONE"}))
    with pytest.raises(NotSubmitted, match="SELF_ONLY"):
        adapter.publish(POST, ["tiktok"], mp4, log)
    assert not net.to("video/init")


def test_tiktok_refresh_failure_says_reconnect(store, net, mp4):
    store.update({"tiktok.client_secret": "cs", "tiktok.refresh_token": "rt0"})
    net.on("POST", "oauth/token", J({"error": "invalid_grant"}, 400))
    with pytest.raises(NotSubmitted, match="scc connect tiktok"):
        TikTok(make_cfg(tiktok={"client_key": "ck"})).publish(POST, ["tiktok"], mp4, log)


def test_tiktok_poll_mapping(tt):
    adapter, net = tt
    d = {"remote_id": "PUB1", "url": None, "meta": {"inbox": True}}

    def status(**data):
        net.routes[:] = [r for r in net.routes if "status/fetch" not in r[1]]
        net.on("POST", "status/fetch", J({"data": data, "error": {"code": "ok"}}))

    status(status="SEND_TO_USER_INBOX")
    r = adapter.poll(POST, "tiktok", d)
    assert r.status == "inbox" and "inbox" in r.meta["note"]
    assert json.loads(net.to("status/fetch")[0].content) == {"publish_id": "PUB1"}
    status(status="PUBLISH_COMPLETE", publicaly_available_post_id=[7123456789])  # TikTok's spelling
    r = adapter.poll(POST, "tiktok", d)
    assert r.status == "published" and r.url.endswith("/video/7123456789")
    status(status="PUBLISH_COMPLETE")
    assert adapter.poll(POST, "tiktok", d).status == "published"
    status(status="PROCESSING_UPLOAD")
    assert adapter.poll(POST, "tiktok", d) is None
    status(status="FAILED", fail_reason="file_format_check_failed")
    r = adapter.poll(POST, "tiktok", d)
    assert r.status == "failed" and "file_format_check_failed" in r.error


def test_tiktok_verify(tt):
    adapter, net = tt
    net.on("GET", "user/info", J({"data": {"user": {"display_name": "Creator"}},
                                  "error": {"code": "ok", "message": ""}}))
    ok, msg = adapter.verify("tiktok")
    assert ok and "Creator" in msg
    assert net.to("user/info")[0].url.params["fields"] == "display_name"
    net.routes.pop()
    net.on("GET", "user/info", J({"error": {"code": "access_token_invalid", "message": "x"}}, 401))
    ok, msg = adapter.verify("tiktok")
    assert not ok and "access_token_invalid" in msg


def test_tiktok_pkce_is_hex_sha256():
    from scc import oauth
    verifier, challenge = oauth.pkce_pair(hex_challenge=True)
    import hashlib
    assert challenge == hashlib.sha256(verifier.encode()).hexdigest()
    assert 43 <= len(verifier) <= 128
    v2, c2 = oauth.pkce_pair()
    assert "=" not in c2 and len(c2) == 43 and v2 != verifier


# ---------------------------------------------------------------- Upload-Post -
@pytest.fixture
def up(store, net):
    store["upload_post.api_key"] = "KEY"
    cfg = make_cfg(upload_post={"user": "creator", "facebook_page_id": "PG"})
    return UploadPost(cfg), net


def test_upload_post_happy_path(up, mp4):
    adapter, net = up
    net.on("POST", "api.upload-post.com/api/upload",
           J({"success": True, "request_id": "REQ1", "total_platforms": 4}))
    out = adapter.publish(POST, ["youtube", "instagram", "facebook", "tiktok"], mp4, log)
    assert set(out) == {"youtube", "instagram", "facebook", "tiktok"}
    assert all(r.status == "processing" and r.meta["request_id"] == "REQ1" for r in out.values())
    req = net.calls[0]
    assert req.headers["authorization"] == "Apikey KEY"
    assert req.headers["idempotency-key"].startswith("scc-7-")
    assert req.headers["content-type"].startswith("multipart/form-data")
    assert req.headers["content-length"] == str(len(req.content))
    assert "transfer-encoding" not in req.headers
    body = req.content
    for needle in (b'name="user"', b"creator", b'name="async_upload"', b'name="platform[]"',
                   b'name="youtube_title"', b'name="youtube_description"',
                   b'name="instagram_title"', b'name="tiktok_title"',
                   b'name="facebook_title"', b'name="facebook_page_id"', b"PG",
                   b'name="facebook_media_type"', b'name="video"; filename="short.mp4"',
                   b"A caption #ai"):
        assert needle in body, needle
    assert body.count(b'name="platform[]"') == 4
    assert mp4.read_bytes() in body


def test_upload_post_commit_timeout_ambiguous(up, mp4):
    adapter, net = up
    net.on("POST", "/api/upload", httpx.ReadTimeout("slow"))
    with pytest.raises(Ambiguous):
        adapter.publish(POST, ["tiktok"], mp4, log)


def test_upload_post_errors_not_submitted(up, mp4):
    adapter, net = up
    net.on("POST", "/api/upload", J({"message": "bad key"}, 401))
    with pytest.raises(NotSubmitted):
        adapter.publish(POST, ["tiktok"], mp4, log)
    net.routes.clear()
    net.on("POST", "/api/upload", J({"success": False, "message": "no"}, 200))
    with pytest.raises(NotSubmitted):
        adapter.publish(POST, ["tiktok"], mp4, log)


def test_upload_post_500_ambiguous_and_missing_key(up, store, net, mp4):
    adapter, net = up
    net.on("POST", "/api/upload", httpx.Response(502))
    with pytest.raises(Ambiguous):
        adapter.publish(POST, ["tiktok"], mp4, log)
    store.pop("upload_post.api_key")
    with pytest.raises(NotSubmitted, match="upload-post"):
        adapter.publish(POST, ["tiktok"], mp4, log)


def test_upload_post_sync_results(up, mp4):
    adapter, net = up
    net.on("POST", "/api/upload", J({"success": True, "results": {
        "tiktok": {"success": True, "url": "https://tiktok.com/@e/video/1", "publish_id": "p1"},
        "instagram": {"success": False, "error": "token expired"}}}))
    out = adapter.publish(POST, ["tiktok", "instagram"], mp4, log)
    assert out["tiktok"].status == "published" and out["tiktok"].url.endswith("/video/1")
    assert out["instagram"].status == "failed" and "token expired" in out["instagram"].error


def test_upload_post_poll_mapping(up):
    adapter, net = up
    d = {"meta": {"request_id": "REQ1"}}

    def status(body, code=200):
        net.routes[:] = []
        net.on("GET", "uploadposts/status", J(body, code))

    status({"status": "in_progress", "results": [
        {"platform": "tiktok", "success": True, "message": "Queued"}]})
    assert adapter.poll(POST, "tiktok", d) is None
    assert net.to("uploadposts/status")[0].url.params["request_id"] == "REQ1"
    status({"status": "completed", "results": [
        {"platform": "tiktok", "success": True, "url": "https://tiktok.com/x", "post_id": "9"},
        {"platform": "facebook", "success": True, "fallback_to_inbox": True}]})
    r = adapter.poll(POST, "tiktok", d)
    assert r.status == "published" and r.url == "https://tiktok.com/x" and r.remote_id == "9"
    assert r.meta["request_id"] == "REQ1"
    assert adapter.poll(POST, "facebook", d).status == "inbox"
    status({"status": "completed", "results": [
        {"platform": "youtube", "success": False, "error": "quota"}]})
    assert adapter.poll(POST, "youtube", d).status == "failed"
    status({"status": "completed", "results": [
        {"platform": "instagram", "skipped": True, "skip_reason": "profile_platform_not_configured"}]})
    assert "not_configured" in adapter.poll(POST, "instagram", d).error
    status({"status": "not_found"}, 404)
    assert adapter.poll(POST, "tiktok", d) is None


def test_upload_post_verify(up):
    adapter, net = up
    net.on("GET", "uploadposts/users", J({"success": True, "profiles": [
        {"username": "other", "social_accounts": {}},
        {"username": "creator", "social_accounts": {"tiktok": {"display_name": "e"},
                                                    "instagram": "", "youtube": None}}]}))
    ok, msg = adapter.verify("tiktok")
    assert ok and "tiktok" in msg and "instagram" not in msg
    assert net.calls[0].headers["authorization"] == "Apikey KEY"
    net.routes.clear()
    net.on("GET", "uploadposts/users", J({"profiles": [{"username": "other"}]}))
    ok, msg = adapter.verify("tiktok")
    assert not ok and "creator" in msg and "other" in msg
    net.routes.clear()
    net.on("GET", "uploadposts/users", J({"error": "x"}, 401))
    ok, msg = adapter.verify("tiktok")
    assert not ok and "401" in msg and "KEY" not in msg


def test_upload_post_verify_without_key_never_raises(store, net):
    ok, _ = UploadPost(make_cfg(upload_post={"user": "u"})).verify("tiktok")
    assert not ok and not net.calls


def test_timeout_reconciles_linkedin_and_x_from_one_upload(tmp_path, monkeypatch, up):
    from scc import accounts, db, engine, media, server

    adapter, net = up
    cfg = adapter.cfg
    con = db.connect(tmp_path / "queue.sqlite3")
    video = tmp_path / "review.mp4"
    video.write_bytes(b"local video fixture")
    platforms = ["linkedin", "x"]
    pid = db.create_post(con, str(video), media.sha256(video), "Video title", "Default", platforms)
    copies = {"linkedin": {"caption": "A practical takeaway"}, "x": {"caption": "A short hook"}}
    db.update_post(con, pid, overrides=copies, approved_at=db.now())
    monkeypatch.setattr(media, "probe", lambda _: media.Probe(39.4, 1080, 1920, 100000, "h264", True))
    for p in platforms:
        cfg.set("routing", p, "upload_post")
    engine.publish_post(con, cfg, db.get_post(con, pid))
    assert not net.calls  # approval alone does not establish account history
    social = {"linkedin": {"username": "li-1", "handle": "creator", "display_name": "Creator"},
              "x": {"username": "x-1", "handle": "@creator", "display_name": "Creator"}}
    net.on("GET", "/uploadposts/users/creator", J({"profile": {
        "username": "creator", "social_accounts": social}}))
    accounts.sync_profiles(con, [{"username": "creator", "social_accounts": social}])
    keys = {}
    for a in accounts.list_accounts(con):
        db.rebind(con, pid, a["platform"], a["id"])
        keys[a["platform"]] = f"{a['platform']}:{a['id']}"
        db.update_delivery(con, pid, keys[a["platform"]], meta={"history_checked_at": db.now()})
    db.update_post(con, pid, approved_at=db.now())
    net.on("POST", "/api/upload", httpx.ReadTimeout("accepted, but the reply was lost"))
    engine.publish_post(con, cfg, db.get_post(con, pid))
    req, = [c for c in net.calls if c.method == "POST"]  # one request for the one profile
    assert req.content.count(b'name="video";') == 1
    for field, value in [("platform[]", "linkedin"), ("platform[]", "x"),
                         ("linkedin_title", "Video title"), ("target_linkedin_page_id", "personal"),
                         ("linkedin_description", "A practical takeaway"), ("x_title", "A short hook")]:
        assert f'name="{field}"\r\n\r\n{value}\r\n'.encode() in req.content
    request_id = req.headers["idempotency-key"]
    for d in db.get_post(con, pid)["deliveries"].values():
        assert d["status"] == "needs_review" and d["meta"]["request_id"] == request_id
        assert db.retry_blocked(d)
    results = [{"platform": "linkedin", "status": "completed", "platform_post_id": "urn:li:ugcPost:1",
                "post_url": "https://www.linkedin.com/feed/update/urn:li:ugcPost:1/"}]
    net.on("GET", "/uploadposts/status", J({"status": "in_progress", "results": results}))
    engine.poll_in_flight(con, cfg)
    assert db.get_post(con, pid)["deliveries"][keys["x"]]["status"] == "needs_review"
    net.routes.clear()
    results.append({"platform": "x", "success": True, "platform_post_id": "2",
                    "post_url": "https://x.com/example/status/2"})
    net.on("GET", "/uploadposts/status", J({"status": "completed", "results": results}))
    engine.poll_in_flight(con, cfg)
    engine.publish_post(con, cfg, db.get_post(con, pid))
    shown = server.present_posts(con, fill_durations=0)[0]["deliveries"]
    receipts = con.execute("SELECT platform,remote_id,url,account_ref FROM upload_receipts").fetchall()
    assert len(receipts) == 2 and {r["account_ref"] for r in receipts} == {"li-1", "x-1"}
    for p, remote_id, url, _ in receipts:
        assert shown[p]["remote_id"] == remote_id and shown[p]["url"] == url
        assert shown[p]["status"] == "published" and shown[p]["meta"]["history_checked_at"]
    assert shown["x"]["remote_id"] == "2" and shown["linkedin"]["remote_id"] == "urn:li:ugcPost:1"
    assert all(r.url.params["request_id"] == request_id for r in net.to("/uploadposts/status"))


def test_upload_post_readiness_no_network(store, net):
    UploadPost(make_cfg()).readiness("tiktok")
    TikTok(make_cfg()).readiness("tiktok")
    Meta(make_cfg()).readiness("facebook")
    assert not net.calls
