"""Multi-account publishing: brands, per-account destinations, duplicates, retries, migration.

Everything runs against FakeUploadPost (tests/fake_upload_post.py) — two test brands with
mocked accounts, simulated publishing, no network and no real Keychain.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest
from fake_upload_post import FakeUploadPost, install

from scc import accounts, config, db, engine, media, server
from scc.adapters import youtube

PAST = "2020-01-01T00:00:00+00:00"


@pytest.fixture(scope="session")
def clip(tmp_path_factory):
    path = tmp_path_factory.mktemp("clip") / "trailer-reaction.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=108x192:d=5",
                    "-f", "lavfi", "-i", "anullsrc", "-shortest", "-c:v", "libx264",
                    "-c:a", "aac", str(path)], check=True)
    return path


@pytest.fixture
def world(tmp_path, monkeypatch, clip):
    """Two test brands, each with its own Upload-Post profile and mocked accounts."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "LOCK_PATH", tmp_path / "run.lock")
    fake = FakeUploadPost()
    install(fake, monkeypatch)
    fake.connect("alpha-profile", "tiktok", "tt-alpha-001", "@alpha_tok")
    fake.connect("alpha-profile", "instagram", "ig-alpha-001", "@alpha.gram")
    fake.connect("alpha-profile", "youtube", "UCalpha0001", "@AlphaChannel", "Alpha Channel")
    fake.connect("beta-profile", "tiktok", "tt-beta-001", "@beta_tok")
    fake.connect("beta-profile", "instagram", "ig-beta-001", "@beta.gram")
    con = db.connect(tmp_path / "t.sqlite3")
    cfg = config.Config({s: dict(v) for s, v in config.DEFAULTS.items()})
    for p in ("tiktok", "instagram", "youtube"):
        cfg.set("routing", p, "upload_post")
    cfg.set("upload_post", "user", "alpha-profile")
    alpha = accounts.save_brand(con, "Test Brand Alpha")
    beta = accounts.save_brand(con, "Test Brand Beta")
    server.discover(con, cfg)
    server.accounts_post(con, cfg, "/api/accounts/map", {"profile": "alpha-profile", "brand_id": alpha})
    server.accounts_post(con, cfg, "/api/accounts/map", {"profile": "beta-profile", "brand_id": beta})
    video = tmp_path / "trailer-reaction.mp4"
    shutil.copyfile(clip, video)

    class W:
        pass

    w = W()
    w.con, w.cfg, w.fake, w.video, w.alpha, w.beta, w.tmp = con, cfg, fake, video, alpha, beta, tmp_path
    return w


def acct(w, handle):
    return next(a for a in accounts.list_accounts(w.con) if a["handle"] == handle)


def make(w, destinations, brand=None, video=None, approve=True, when=None):
    """A post whose account history is already checked, sent to exactly these handles."""
    video = video or w.video
    pid = db.create_post(w.con, str(video), media.sha256(video), "Trailer reaction",
                         "A caption #movies", [], brand=brand)
    db.set_destinations(w.con, pid, [(acct(w, h)["platform"], acct(w, h)["id"]) for h in destinations])
    for key in db.get_post(w.con, pid)["deliveries"]:
        db.update_delivery(w.con, pid, key, meta={"history_checked_at": db.now()})
    if approve:
        db.update_post(w.con, pid, approved_at=db.now(), scheduled_at=when)
    return pid


def by_handle(w, pid):
    return {d["account_handle"]: d for d in db.get_post(w.con, pid)["deliveries"].values()}


def api(w, path, body):
    return server.Handler._route_data(None, w.con, path, body)


# ------------------------------------------------------- discovery & mapping --
def test_discovery_stores_stable_ids_handles_and_brands(world):
    w = world
    found = {(a["profile"], a["platform"]): a for a in accounts.list_accounts(w.con)}
    tok = found[("alpha-profile", "tiktok")]
    assert (tok["platform_id"], tok["handle"], tok["brand_name"]) == (
        "tt-alpha-001", "@alpha_tok", "Test Brand Alpha")
    assert found[("beta-profile", "tiktok")]["brand_name"] == "Test Brand Beta"
    assert found[("alpha-profile", "youtube")]["name"] == "Alpha Channel"
    assert all(a["status"] == "verified" and a["is_default"] for a in found.values())
    view = server.accounts_view(w.con)
    assert view["upload_post_plan"]["limit"] == 5
    assert {p["name"] for p in view["profiles"]} == {"alpha-profile", "beta-profile"}


def test_create_profile_and_connect_link_use_the_plan(world):
    w = world
    out = api(w, "/api/accounts/profile", {"profile": "gamma-profile"})
    assert out["ok"] and "gamma-profile" in w.fake.profiles
    link = api(w, "/api/accounts/link", {"profile": "gamma-profile", "platforms": ["tiktok"]})
    assert link["url"].startswith("https://app.upload-post.com/connect/gamma-profile")
    w.fake.limit = 3
    with pytest.raises(server.Fail, match="allows 3 profiles"):
        api(w, "/api/accounts/profile", {"profile": "delta-profile"})
    with pytest.raises(server.Fail, match="2–40"):
        api(w, "/api/accounts/profile", {"profile": "no spaces allowed"})


def test_brand_suggests_its_accounts_and_the_editor_sees_handles(world):
    w = world
    pid = db.create_post(w.con, str(w.video), media.sha256(w.video), "Reaction", "cap",
                         ["tiktok", "instagram", "youtube"], brand=w.beta)
    shown = next(p for p in server.present_posts(w.con, fill_durations=0) if p["id"] == pid)
    assert shown["brand_name"] == "Test Brand Beta"
    dest = {d["platform"]: d for d in shown["destinations"]}
    assert dest["tiktok"]["account"] == "@beta_tok" and dest["tiktok"]["brand"] == "Test Brand Beta"
    assert dest["instagram"]["account"] == "@beta.gram"
    assert dest["youtube"]["account"] == "" and dest["youtube"]["account_id"] == ""  # Beta has none
    assert shown["deliveries"]["tiktok"]["accounts"] == ["@beta_tok"]
    # Changing the brand re-suggests, and the stable ID is what gets stored.
    db.set_brand(w.con, pid, w.alpha)
    d = by_handle(w, pid)
    assert d["@alpha_tok"]["account_ref"] == "tt-alpha-001" and d["@AlphaChannel"]["status"] == "pending"
    assert "@beta_tok" not in d or d["@beta_tok"]["status"] == "skipped"


def test_changing_destinations_clears_approval(world):
    w = world
    pid = make(w, ["@alpha_tok", "@alpha.gram"])
    assert db.get_post(w.con, pid)["approved_at"]
    # Remove Instagram, keep the brand's TikTok, deliberately add a second TikTok.
    unapproved = server.op_update(w.con, w.cfg, pid, {"destinations": [
        {"platform": "tiktok", "account_id": acct(w, "@alpha_tok")["id"]},
        {"platform": "tiktok", "account_id": acct(w, "@beta_tok")["id"]}]})
    post = db.get_post(w.con, pid)
    assert unapproved and post["approved_at"] is None
    d = by_handle(w, pid)
    assert d["@alpha.gram"]["status"] == "skipped"
    assert d["@alpha_tok"]["status"] == d["@beta_tok"]["status"] == "pending"
    db.update_post(w.con, pid, approved_at=db.now())
    assert not server.op_update(w.con, w.cfg, pid, {"title": "Trailer reaction"})  # nothing changed
    assert db.get_post(w.con, pid)["approved_at"]
    with pytest.raises(server.Fail):  # an Instagram account can't be a TikTok destination
        server.op_update(w.con, w.cfg, pid, {"destinations": [
            {"platform": "tiktok", "account_id": acct(w, "@alpha.gram")["id"]}]})


# ----------------------------------------------------------------- publishing --
def test_one_clip_reaches_two_tiktoks_grouped_by_profile(world):
    w = world
    pid = make(w, ["@alpha_tok", "@alpha.gram", "@beta_tok"])
    engine.run_once(w.con, w.cfg)
    sent = sorted((u["profile"], sorted(u["platforms"])) for u in w.fake.uploads)
    # One request per profile, carrying only the platforms selected on that profile.
    assert sent == [("alpha-profile", ["instagram", "tiktok"]), ("beta-profile", ["tiktok"])]
    assert sorted(w.fake.posted) == [("alpha-profile", "instagram", "ig-alpha-001"),
                                     ("alpha-profile", "tiktok", "tt-alpha-001"),
                                     ("beta-profile", "tiktok", "tt-beta-001")]
    d = by_handle(w, pid)
    assert {x["status"] for x in d.values()} == {"published"}
    assert "@alpha_tok" in d["@alpha_tok"]["url"] and "@beta_tok" in d["@beta_tok"]["url"]
    refs = {r["account_ref"] for r in w.con.execute(
        "SELECT account_ref FROM upload_receipts WHERE platform='tiktok'")}
    assert refs == {"tt-alpha-001", "tt-beta-001"}
    events = [(e["platform"], e["account"]) for e in db.recent_events(w.con)
              if e["message"].startswith("published")]
    assert ("tiktok", "@beta_tok") in events and ("instagram", "@alpha.gram") in events


def test_same_file_to_the_same_tiktok_is_blocked_but_another_tiktok_is_allowed(world):
    w = world
    make(w, ["@alpha_tok"])
    engine.run_once(w.con, w.cfg)
    assert len(w.fake.uploads) == 1
    again = w.tmp / "same-clip-renamed.mp4"
    shutil.copyfile(w.video, again)
    pid = make(w, ["@alpha_tok", "@beta_tok"], video=again)
    engine.run_once(w.con, w.cfg)
    d = by_handle(w, pid)
    assert d["@alpha_tok"]["status"] == "skipped"
    assert "Existing tiktok @alpha_tok upload recorded" in d["@alpha_tok"]["error"]
    assert d["@alpha_tok"]["url"].startswith("https://www.tiktok.com/@alpha_tok/")  # the original link
    assert d["@beta_tok"]["status"] == "published"
    assert [u["profile"] for u in w.fake.uploads] == ["alpha-profile", "beta-profile"]
    assert w.fake.posted.count(("alpha-profile", "tiktok", "tt-alpha-001")) == 1


def test_partial_failure_retries_only_the_outstanding_account(world):
    w = world
    w.fake.failing[("beta-profile", "tiktok")] = "TikTok rejected the video"
    pid = make(w, ["@alpha_tok", "@beta_tok"])
    engine.run_once(w.con, w.cfg)
    d = by_handle(w, pid)
    assert d["@alpha_tok"]["status"] == "published" and d["@beta_tok"]["status"] == "failed"
    assert "TikTok rejected" in d["@beta_tok"]["error"]
    engine.run_once(w.con, w.cfg)                      # nothing is resent on its own
    assert len(w.fake.uploads) == 2
    # You check the account, confirm nothing landed, and approve again.
    del w.fake.failing[("beta-profile", "tiktok")]
    api(w, f"/api/posts/{pid}/history", {"key": d["@beta_tok"]["key"], "decision": "absent"})
    assert db.get_post(w.con, pid)["approved_at"] is None
    server.op_approve(w.con, pid)
    engine.run_once(w.con, w.cfg)
    assert [(u["profile"], u["platforms"]) for u in w.fake.uploads[2:]] == [("beta-profile", ["tiktok"])]
    assert w.fake.posted.count(("alpha-profile", "tiktok", "tt-alpha-001")) == 1
    assert w.fake.posted.count(("beta-profile", "tiktok", "tt-beta-001")) == 1
    assert {x["status"] for x in by_handle(w, pid).values()} == {"published"}


def test_unreachable_service_retries_only_what_was_not_sent(world, monkeypatch):
    w = world
    pid = make(w, ["@alpha_tok", "@beta_tok"])
    real = w.fake._upload

    def flaky(request):
        if b"beta-profile" in request.content:
            raise __import__("httpx").ConnectError("offline")
        return real(request)

    monkeypatch.setattr(w.fake, "_upload", flaky)
    engine.run_once(w.con, w.cfg)
    d = by_handle(w, pid)
    assert d["@alpha_tok"]["status"] == "published"
    assert d["@beta_tok"]["status"] == "failed" and d["@beta_tok"]["meta"]["retryable"]
    monkeypatch.setattr(w.fake, "_upload", real)
    engine.run_once(w.con, w.cfg)                      # the scheduler retries by itself
    assert [u["profile"] for u in w.fake.uploads] == ["alpha-profile", "beta-profile"]
    assert by_handle(w, pid)["@beta_tok"]["status"] == "published"


# ------------------------------------------------- changes after scheduling --
def test_profile_reconnected_to_another_account_after_scheduling_is_held(world):
    w = world
    pid = make(w, ["@alpha_tok", "@alpha.gram"], when=PAST)
    approved_id = by_handle(w, pid)["@alpha_tok"]["account_ref"]
    # Someone reconnects the profile's TikTok to a different account after approval.
    w.fake.connect("alpha-profile", "tiktok", "tt-intruder-999", "@someone_else")
    engine.run_once(w.con, w.cfg)
    d = by_handle(w, pid)
    assert d["@alpha_tok"]["status"] == "needs_review"
    assert "@someone_else" in d["@alpha_tok"]["error"] and "@alpha_tok" in d["@alpha_tok"]["error"]
    assert d["@alpha.gram"]["status"] == "published"   # the untouched account still goes out
    assert [u["platforms"] for u in w.fake.uploads] == [["instagram"]]
    assert not any(p[2] == "tt-intruder-999" for p in w.fake.posted)
    assert acct(w, "@alpha_tok")["status"] == "missing"
    assert db.bucket(db.get_post(w.con, pid)) == "attention"
    # Reconnect the approved account, retry that one destination, and it goes to the right ID.
    w.fake.connect("alpha-profile", "tiktok", approved_id, "@alpha_tok")
    server.op_retry(w.con, pid, {"keys": [d["@alpha_tok"]["key"]]})
    engine.run_once(w.con, w.cfg)
    assert by_handle(w, pid)["@alpha_tok"]["status"] == "published"
    assert ("alpha-profile", "tiktok", approved_id) in w.fake.posted


def test_changing_a_brand_default_never_redirects_an_approved_post(world):
    w = world
    pid = make(w, ["@alpha_tok"], brand=w.alpha, when=PAST)
    # Beta's TikTok becomes Alpha's suggested TikTok after the post was approved.
    accounts.map_account(w.con, acct(w, "@beta_tok")["id"], w.alpha, default=True)
    assert db.default_account(w.con, w.alpha, "tiktok") == acct(w, "@beta_tok")["id"]
    post = db.get_post(w.con, pid)
    assert post["approved_at"] and list(by_handle(w, pid)) == ["@alpha_tok"]
    engine.run_once(w.con, w.cfg)
    assert w.fake.posted == [("alpha-profile", "tiktok", "tt-alpha-001")]


def test_expired_or_removed_account_is_held(world):
    w = world
    first = make(w, ["@alpha_tok"], when=PAST)
    w.fake.connect("alpha-profile", "tiktok", "tt-alpha-001", "@alpha_tok", reauth=True)
    engine.run_once(w.con, w.cfg)
    d = by_handle(w, first)["@alpha_tok"]
    assert d["status"] == "needs_review" and "sign-in expired" in d["error"]
    other = w.tmp / "second.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=108x192:d=4",
                    "-f", "lavfi", "-i", "anullsrc", "-shortest", "-c:v", "libx264",
                    "-c:a", "aac", str(other)], check=True)
    second = make(w, ["@beta.gram"], video=other, when=PAST)
    w.fake.disconnect("beta-profile", "instagram")
    engine.run_once(w.con, w.cfg)
    d = by_handle(w, second)["@beta.gram"]
    assert d["status"] == "needs_review" and "no instagram account connected" in d["error"]
    assert w.fake.uploads == []
    # A held destination can simply be deselected in the editor.
    server.op_update(w.con, w.cfg, second, {"destinations": []})
    assert by_handle(w, second)["@beta.gram"]["status"] == "skipped"


# ------------------------------------------------------------------ migration --
def v1_database(path: Path, sha: str) -> None:
    """A version-1 database holding what the live one holds: four live Upload-Post receipts
    from one request, a confirmed external YouTube upload and a private Studio upload."""
    con = sqlite3.connect(path, isolation_level=None)
    con.executescript((Path(__file__).parent / "v1_schema.sql").read_text())
    now = "2026-11-25T03:01:16+00:00"
    posts = [("d810df8044", sha, "Type @ChatGPT Inside Your Notes and Watch What Happens"),
             ("5472240620", "b" * 64, "My First AI Agent"), ("a8d34d21a8", "c" * 64, "Wobbly dog"),
             ("c0620b13d4", "d" * 64, "Twilight to Bond")]
    for pid, digest, title in posts:
        con.execute("INSERT INTO posts(id,video_path,sha256,title,caption,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?)", (pid, f"/clips/{pid}.mp4", digest, title, "cap", now, now))
    con.execute("INSERT INTO fini_items(path,stamp,observed,phase,brand,post_id) VALUES "
                "('/x.mp4','[]',0,'queued','alist','c0620b13d4')")
    meta = json.dumps({"request_id": "scc-d810df8044-live-7d4dff8c0b96", "profile": "default",
                       "history_checked_at": now, "history_unverified": False})
    live = [("instagram", "18149287912559765", "https://www.instagram.com/reel/DgJhEVRiOTe/"),
            ("tiktok", "7711938774011317518", "https://www.tiktok.com/@creator_tok/video/7711938774011317518"),
            ("linkedin", "urn:li:ugcPost:7531190354113466368",
             "https://www.linkedin.com/feed/update/urn:li:ugcPost:7531190354113466368/"),
            ("x", "2125424667007561855", "https://x.com/creator/status/2125424667007561855")]
    rows = [("d810df8044", p, "upload_post", "pending", None, None, meta) for p, _, _ in live]
    rows += [("d810df8044", "facebook", None, "skipped", None, None, "{}"),
             ("5472240620", "youtube", "external", "pending", None, None, '{"source":"audio match"}'),
             ("a8d34d21a8", "youtube", "studio", "pending", None, None, '{"source":"private Studio staging"}'),
             ("c0620b13d4", "tiktok", None, "pending", None, None, '{"history_unverified":true}')]
    for n, (pid, platform, route, status, rid, url, m) in enumerate(rows):
        con.execute("INSERT INTO deliveries(id,post_id,platform,route,status,remote_id,url,meta,"
                    "updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (f"d{n:011d}", pid, platform, route, status, rid, url, m, now))
    # The version-1 trigger writes the receipts, exactly as it did in production.
    for platform, rid, url in live:
        con.execute("UPDATE deliveries SET status='published', remote_id=?, url=? WHERE "
                    "post_id='d810df8044' AND platform=?", (rid, url, platform))
    con.execute("UPDATE deliveries SET status='published', remote_id='RHxSTCEim_8', "
                "url='https://www.youtube.com/shorts/RHxSTCEim_8' WHERE post_id='5472240620'")
    con.execute("UPDATE deliveries SET status='private', remote_id='8ao0tawmtDM', "
                "url='https://studio.youtube.com/video/8ao0tawmtDM/edit' WHERE post_id='a8d34d21a8'")
    con.execute("UPDATE posts SET scheduled_at='2026-12-01T17:00:00+00:00' WHERE id='c0620b13d4'")
    assert con.execute("SELECT count(*) FROM upload_receipts").fetchone()[0] == 6
    con.close()


def test_existing_receipts_survive_migration_and_attach_only_when_provable(world):
    w = world
    path = w.tmp / "v1.sqlite3"
    sha = media.sha256(w.video)
    v1_database(path, sha)
    before = sqlite3.connect(path).execute(
        "SELECT sha256,platform,post_id,status,remote_id,url FROM upload_receipts ORDER BY 1,2").fetchall()
    con = db.connect(path)
    after = [tuple(r) for r in con.execute(
        "SELECT sha256,platform,post_id,status,remote_id,url FROM upload_receipts ORDER BY 1,2")]
    assert after == before                                  # every receipt and permalink kept
    assert all(r["review"] == 1 and r["account_ref"] == "" for r in
               con.execute("SELECT review, account_ref FROM upload_receipts"))
    post = db.get_post(con, "c0620b13d4")
    assert post["brand"] == "alist" and post["scheduled_at"] == "2026-12-01T17:00:00+00:00"
    assert post["deliveries"]["tiktok"]["status"] == "pending"   # drafts and schedules untouched
    assert con.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    db.connect(path).close()                                # migrating twice changes nothing
    assert con.execute("SELECT count(*) FROM upload_receipts").fetchone()[0] == 6

    # Discover the profile those posts went through.
    profile = {"username": "default", "social_accounts": {
        "tiktok": {"username": "tt-open-id-1", "handle": "@creator_tok", "display_name": "Creator"},
        "instagram": {"username": "17840000000000001", "display_name": "Creator"},
        "linkedin": {"username": "li-member-1", "display_name": "Creator"},
        "x": {"username": "x-user-1", "handle": "creator"},
        "youtube": {"username": "UCcreator0001", "handle": "@CreatorChannel"}}}
    accounts.sync_profiles(con, [profile])
    got = {r["platform"]: r for r in con.execute(
        "SELECT * FROM upload_receipts WHERE sha256=?", (sha,))}
    assert got["tiktok"]["account_ref"] == "tt-open-id-1" and "permalink" in got["tiktok"]["evidence"]
    assert got["x"]["account_ref"] == "x-user-1" and "permalink" in got["x"]["evidence"]
    assert got["instagram"]["account_ref"] == "17840000000000001"
    assert "profile 'default'" in got["instagram"]["evidence"]
    assert got["linkedin"]["account_ref"] == "li-member-1"
    assert all(r["review"] == 0 for r in got.values())
    assert got["instagram"]["url"] == "https://www.instagram.com/reel/DgJhEVRiOTe/"
    done = db.get_post(con, "d810df8044")["deliveries"]
    assert {d["account_ref"] for d in done.values() if d["status"] == "published"} == {
        "tt-open-id-1", "17840000000000001", "li-member-1", "x-user-1"}
    # YouTube history names no channel anywhere, so it stays flagged rather than guessed.
    review = accounts.review_receipts(con)
    assert sorted(r["post_id"] for r in review) == ["5472240620", "a8d34d21a8"]
    assert {r["url"] for r in review} == {"https://www.youtube.com/shorts/RHxSTCEim_8",
                                          "https://studio.youtube.com/video/8ao0tawmtDM/edit"}

    # The same file is blocked on the proven TikTok and allowed on a different one.
    accounts.sync_profile(con, "second", None, {"tiktok": {"username": "tt-open-id-2",
                                                            "handle": "@second_tok"}})
    clip2 = w.tmp / "again.mp4"
    shutil.copyfile(w.video, clip2)
    ids = {a["handle"]: a["id"] for a in accounts.list_accounts(con)}
    pid = db.create_post(con, str(clip2), sha, "Again", "cap", [])
    db.set_destinations(con, pid, [("tiktok", ids["@creator_tok"]), ("tiktok", ids["@second_tok"])])
    for key in db.get_post(con, pid)["deliveries"]:
        db.update_delivery(con, pid, key, meta={"history_checked_at": db.now()})
    db.update_post(con, pid, approved_at=db.now())
    w.fake.connect("default", "tiktok", "tt-open-id-1", "@creator_tok")
    w.fake.connect("second", "tiktok", "tt-open-id-2", "@second_tok")
    engine.publish_post(con, w.cfg, db.get_post(con, pid))
    d = {x["account_handle"]: x for x in db.get_post(con, pid)["deliveries"].values()}
    assert d["@creator_tok"]["status"] == "skipped"
    assert d["@creator_tok"]["url"] == "https://www.tiktok.com/@creator_tok/video/7711938774011317518"
    assert d["@second_tok"]["status"] in ("processing", "published")
    assert [u["profile"] for u in w.fake.uploads] == ["second"]


def test_unconfirmed_history_holds_a_new_delivery_until_you_assign_it(world):
    w = world
    path = w.tmp / "v1.sqlite3"
    v1_database(path, media.sha256(w.video))
    con = db.connect(path)
    accounts.sync_profile(con, "default", None, {"youtube": {"username": "UCcreator0001",
                                                              "handle": "@CreatorChannel"}})
    channel = accounts.list_accounts(con)[0]
    other = w.tmp / "yt.mp4"
    shutil.copyfile(w.video, other)
    con.execute("UPDATE posts SET sha256=? WHERE id='5472240620'", (media.sha256(other),))
    con.execute("UPDATE upload_receipts SET sha256=? WHERE post_id='5472240620'", (media.sha256(other),))
    pid = db.create_post(con, str(other), media.sha256(other), "Reupload", "cap", [])
    db.set_destinations(con, pid, [("youtube", channel["id"])])
    key = f"youtube:{channel['id']}"
    db.update_delivery(con, pid, key, meta={"history_checked_at": db.now()})
    db.update_post(con, pid, approved_at=db.now())
    engine.publish_post(con, w.cfg, db.get_post(con, pid))
    held = db.get_post(con, pid)["deliveries"][key]
    assert held["status"] == "pending" and "no confirmed account" in held["error"]
    assert w.fake.uploads == []
    review = accounts.review_receipts(con)
    item = next(r for r in review if r["post_id"] == "5472240620")
    server.accounts_post(con, w.cfg, "/api/receipts/assign", {
        "items": [{"sha256": item["sha256"], "platform": "youtube"}], "account_id": channel["id"]})
    receipt = con.execute("SELECT * FROM upload_receipts WHERE post_id='5472240620'").fetchone()
    assert receipt["account_ref"] == "UCcreator0001" and receipt["review"] == 0
    assert receipt["url"] == "https://www.youtube.com/shorts/RHxSTCEim_8"
    engine.publish_post(con, w.cfg, db.get_post(con, pid))   # now it is a known duplicate
    assert db.get_post(con, pid)["deliveries"][key]["status"] == "skipped"


def test_account_first_known_by_name_keeps_its_history_when_its_id_arrives(world):
    w = world
    path = w.tmp / "v1.sqlite3"
    sha = media.sha256(w.video)
    v1_database(path, sha)
    con = db.connect(path)
    # First look: the service gives a handle but no platform ID.
    accounts.sync_profiles(con, [{"username": "default", "social_accounts": {
        "tiktok": {"handle": "creator_tok", "display_name": "Creator"},
        "linkedin": {"handle": "Creator Name", "display_name": "Creator Name"}}}])
    before = {a["platform"]: a for a in accounts.list_accounts(con)}
    assert before["tiktok"]["platform_id"] == "handle:creator_tok"
    assert before["linkedin"]["label"] == "Creator Name"          # a display name gets no "@"
    assert con.execute("SELECT account_ref FROM upload_receipts WHERE sha256=? AND platform="
                       "'tiktok'", (sha,)).fetchone()[0] == "handle:creator_tok"
    # Later the real ID is available: same account row, history follows it.
    accounts.sync_profiles(con, [{"username": "default", "social_accounts": {
        "tiktok": {"username": "tt-open-id-1", "handle": "creator_tok", "display_name": "Creator"},
        "linkedin": {"username": "li-1", "handle": "Creator Name", "display_name": "Creator Name"}}}])
    after = {a["platform"]: a for a in accounts.list_accounts(con)}
    assert after["tiktok"]["id"] == before["tiktok"]["id"] and len(after) == 2
    assert after["tiktok"]["platform_id"] == "tt-open-id-1" and after["tiktok"]["status"] == "verified"
    receipt = con.execute("SELECT * FROM upload_receipts WHERE sha256=? AND platform='tiktok'",
                          (sha,)).fetchone()
    assert receipt["account_ref"] == "tt-open-id-1" and receipt["review"] == 0
    assert receipt["url"] == "https://www.tiktok.com/@creator_tok/video/7711938774011317518"
    done = [d for d in db.get_post(con, "d810df8044")["deliveries"].values() if d["platform"] == "tiktok"]
    assert [d["account_ref"] for d in done] == ["tt-open-id-1"]


# ------------------------------------------------------- direct YouTube OAuth --
def test_each_youtube_channel_keeps_its_own_keychain_item(world, monkeypatch):
    w = world
    w.fake.store["youtube.client_secret"] = "CSEC"
    w.cfg.set("youtube", "client_id", "CID")
    signins = iter([("REFRESH-ONE", "UCfirst", "First Channel", "@first"),
                    ("REFRESH-TWO", "UCsecond", "Second Channel", "@second")])
    current: dict[str, tuple] = {}

    class Http:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, data=None, **kw):
            current["v"] = next(signins)
            return __import__("httpx").Response(200, json={
                "access_token": "AT", "refresh_token": current["v"][0]},
                request=__import__("httpx").Request("POST", url))

        def get(self, url, **kw):
            _, cid, title, handle = current["v"]
            return __import__("httpx").Response(200, json={"items": [{
                "id": cid, "snippet": {"title": title, "customUrl": handle}}]})

    monkeypatch.setattr(youtube, "client", lambda timeout=60: Http())
    monkeypatch.setattr(youtube.oauth, "authorize", lambda *a, **k: {"code": "CODE"})
    first = youtube.YouTube(w.cfg).connect()
    second = youtube.YouTube(w.cfg).connect()
    assert (first["id"], second["id"]) == ("UCfirst", "UCsecond")
    assert w.fake.store["youtube.refresh_token.UCfirst"] == "REFRESH-ONE"   # not overwritten
    assert w.fake.store["youtube.refresh_token.UCsecond"] == "REFRESH-TWO"
    assert "youtube.refresh_token" not in w.fake.store
    assert youtube.YouTube(w.cfg, "UCfirst").token_name == "youtube.refresh_token.UCfirst"
    assert youtube.YouTube(w.cfg, "UCfirst").readiness("youtube")[0]
    assert not youtube.YouTube(w.cfg, "UCthird").readiness("youtube")[0]
