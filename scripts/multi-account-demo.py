"""Prove multi-account publishing on a COPY of the database with simulated publishing.

    SCC_HOME=/some/empty/folder python scripts/multi-account-demo.py prove --db data/scc.sqlite3
    SCC_HOME=/some/empty/folder python scripts/multi-account-demo.py serve --port 8799

`prove` copies the given database into $SCC_HOME, migrates the copy and walks through every
multi-account behaviour against a stand-in for Upload-Post (tests/fake_upload_post.py).
`serve` opens the dashboard on that same copy with the same stand-in, so you can click
through it. Neither touches the real database, the Keychain, or the network: the script
refuses to run unless SCC_HOME points somewhere other than the real installation.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOME = Path(os.environ.get("SCC_HOME", "")).resolve() if os.environ.get("SCC_HOME") else None
if HOME is None or HOME == REPO or (HOME / "scc").exists():
    sys.exit("Set SCC_HOME to an empty scratch folder first — this never runs on the real data.")
sys.path[:0] = [str(REPO), str(REPO / "tests")]

from fake_upload_post import FakeUploadPost, install

from scc import accounts, config, db, engine, media, server

RECEIPT_COLS = "sha256,platform,post_id,status,remote_id,url,recorded_at"


def say(text: str = "") -> None:
    print(text, flush=True)


def check(label: str, ok: bool) -> None:
    say(f"   {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        sys.exit(f"FAILED: {label}")


def saved_handle(platform: str, fallback: str) -> str:
    """The handle named in an existing permalink for this platform, so the mocked 'default'
    profile matches the links already stored in the copied database."""
    path = HOME / "data" / "scc.sqlite3"
    if path.exists():
        con = sqlite3.connect(path)
        urls = [r[0] for r in con.execute(
            "SELECT url FROM upload_receipts WHERE platform=? AND url IS NOT NULL", (platform,))]
        con.close()
        for url in urls:
            if m := accounts.HANDLE_URL[platform].search(url):
                return "@" + m.group(1).lstrip("@")
    return fallback


def the_fake() -> FakeUploadPost:
    """Two test brands with mocked accounts, plus the existing 'default' profile."""
    fake = FakeUploadPost(limit=5)
    # The profile already in use, with mocked IDs.
    fake.connect("default", "tiktok", "mock-open-id-default-tiktok", saved_handle("tiktok", "@main_tiktok"), "Why AI Matters")
    fake.connect("default", "instagram", "17841400000000001", "@whyaimatters.mock", "Why AI Matters")
    fake.connect("default", "linkedin", "mock-linkedin-member", "", "Why AI Matters (LinkedIn)")
    fake.connect("default", "x", "mock-x-user", saved_handle("x", "@main_x"))
    fake.connect("default", "youtube", "UCmockWhyAIMatters0000000", "@WhyAIMattersMock", "Why AI Matters")
    fake.connect("test-alpha", "tiktok", "mock-open-id-alpha-tiktok", "@alpha_trailers", "Alpha Trailers")
    fake.connect("test-alpha", "instagram", "17841400000000002", "@alpha.trailers", "Alpha Trailers")
    fake.connect("test-alpha", "youtube", "UCmockAlphaChannel0000000", "@AlphaTrailers", "Alpha Trailers")
    fake.connect("test-beta", "tiktok", "mock-open-id-beta-tiktok", "@beta_tutorials", "Beta Tutorials")
    fake.connect("test-beta", "instagram", "17841400000000003", "@beta.tutorials", "Beta Tutorials")
    install(fake)
    return fake


def demo_config() -> config.Config:
    """The real settings, with TikTok and Instagram pointed at the simulated service."""
    cfg = config.load()
    for platform in ("tiktok", "instagram"):
        cfg.set("routing", platform, "upload_post")
    cfg.set("general", "shorts_folder", str(HOME / "shorts"))
    config.save(cfg)
    return cfg


def clip(name: str, colour: str) -> Path:
    path = HOME / "shorts" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"color=c={colour}:s=1080x1920:d=6",
                        "-f", "lavfi", "-i", "anullsrc", "-shortest", "-c:v", "libx264", "-pix_fmt",
                        "yuv420p", "-c:a", "aac", str(path)], check=True)
    return path


def handles(con, pid: str) -> dict[str, dict]:
    return {d["account_handle"] or d["key"]: d for d in db.get_post(con, pid)["deliveries"].values()}


def new_post(con, video: Path, title: str, brand: str, platforms: list[str]) -> str:
    return db.create_post(con, str(video), media.sha256(video), title,
                          "Simulated caption for the multi-account demonstration.", platforms,
                          brand=brand)


def ready(con, pid: str, when: str | None = None) -> None:
    """What you do in the editor: confirm the account history, then approve."""
    for key, d in db.get_post(con, pid)["deliveries"].items():
        if d["status"] == "pending":
            db.update_delivery(con, pid, key, meta={**d["meta"], "history_unverified": False,
                                                    "history_checked_at": db.now()})
    db.update_post(con, pid, approved_at=db.now(), scheduled_at=when)


def prepare(source: Path) -> None:
    (HOME / "data").mkdir(parents=True, exist_ok=True)
    target = HOME / "data" / "scc.sqlite3"
    for stale in HOME.glob("data/scc.sqlite3*"):
        stale.unlink()
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    src.backup(dst)  # a consistent copy even while the dashboard is running
    src.close()
    dst.close()
    if (REPO / "config.toml").exists():
        shutil.copyfile(REPO / "config.toml", HOME / "config.toml")
    elif os.environ.get("SCC_DEMO_CONFIG"):
        shutil.copyfile(os.environ["SCC_DEMO_CONFIG"], HOME / "config.toml")


def prove(source: Path) -> None:
    prepare(source)
    path = HOME / "data" / "scc.sqlite3"
    raw = sqlite3.connect(path)
    raw.row_factory = sqlite3.Row
    before = {
        "receipts": [tuple(r) for r in raw.execute(f"SELECT {RECEIPT_COLS} FROM upload_receipts ORDER BY 1,2")],
        "posts": [tuple(r) for r in raw.execute("SELECT id,scheduled_at,approved_at,title,caption FROM posts ORDER BY id")],
        "deliveries": [tuple(r) for r in raw.execute("SELECT post_id,platform,status,remote_id,url FROM deliveries ORDER BY 1,2")],
        "events": raw.execute("SELECT count(*) FROM events").fetchone()[0],
    }
    live_title = "Type @ChatGPT Inside Your Notes and Watch What Happens"
    raw.close()

    say("1. MIGRATE THE COPY")
    con = db.connect(path)
    after = [tuple(r) for r in con.execute(f"SELECT {RECEIPT_COLS} FROM upload_receipts ORDER BY 1,2")]
    check(f"all {len(before['receipts'])} upload receipts kept with identical IDs and permalinks", after == before["receipts"])
    check(f"all {len(before['posts'])} posts keep their copy, schedule and approval",
          [tuple(r) for r in con.execute("SELECT id,scheduled_at,approved_at,title,caption FROM posts ORDER BY id")] == before["posts"])
    check(f"all {len(before['deliveries'])} deliveries keep their status and links",
          [tuple(r) for r in con.execute("SELECT post_id,platform,status,remote_id,url FROM deliveries ORDER BY 1,2")] == before["deliveries"])
    check("activity history kept", con.execute("SELECT count(*) FROM events").fetchone()[0] == before["events"])
    check("every old receipt starts flagged, with no account guessed",
          all(r["review"] == 1 and r["account_ref"] == "" for r in con.execute("SELECT * FROM upload_receipts")))
    cfg = demo_config()
    check("YouTube upload pause is still on", bool(cfg.get("youtube", "uploads_paused")))
    check("new Shorts are still not auto-approved", cfg.get("fini", "auto_approve_new_shorts") is False)

    say("\n2. DISCOVER PROFILES AND PROVE WHERE OLD UPLOADS WENT")
    fake = the_fake()
    result = server.discover(con, cfg)
    say(f"   discovered: {result['detail']}; {result['proven']} receipts matched, {result['review']} left for review")
    live = [r for r in con.execute(
        "SELECT r.*, p.title FROM upload_receipts r JOIN posts p ON p.id=r.post_id WHERE p.title=?", (live_title,))]
    for r in live:
        say(f"   {r['platform']:<10} {r['account_handle'] or '?':<26} {r['url']}")
        say(f"              evidence: {r['evidence']}")
    check(f"the four live posts for “{live_title}” are attached to accounts", len(live) == 4 and all(r["review"] == 0 and r["account_ref"] for r in live))
    check("their permalinks are unchanged", {r["url"] for r in live} == {b[5] for b in before["receipts"] if b[2] == live[0]["post_id"]})
    review = accounts.review_receipts(con)
    say(f"   left for review ({len(review)}): " + ", ".join(sorted({r['platform'] for r in review})) + " uploads whose saved data names no channel")
    check("uncertain history is flagged, not guessed", all(r["platform"] == "youtube" for r in review) and len(review) == len(before["receipts"]) - 4)

    say("\n3. TWO TEST BRANDS, MOCKED ACCOUNTS, REAL HANDLES IN THE EDITOR")
    alpha = accounts.save_brand(con, "Test Brand Alpha (trailers)")
    beta = accounts.save_brand(con, "Test Brand Beta (tutorials)")
    server.accounts_post(con, cfg, "/api/accounts/map", {"profile": "test-alpha", "brand_id": alpha})
    server.accounts_post(con, cfg, "/api/accounts/map", {"profile": "test-beta", "brand_id": beta})
    by_handle = {a["handle"]: a for a in accounts.list_accounts(con)}
    main_tok = next(a["handle"] for a in accounts.list_accounts(con)
                    if a["profile"] == "default" and a["platform"] == "tiktok")
    trailer = new_post(con, clip("trailer-reaction-demo.mp4", "navy"), "Trailer reaction (demo)", alpha, ["tiktok", "instagram", "youtube"])
    tutorial = new_post(con, clip("ai-tutorial-demo.mp4", "darkgreen"), "AI tutorial (demo)", beta, ["tiktok", "instagram"])
    for pid in (trailer, tutorial):
        shown = next(p for p in server.present_posts(con, fill_durations=0) if p["id"] == pid)
        say(f"   {shown['title']} → brand: {shown['brand_name']}")
        for d in shown["destinations"]:
            say(f"      {d['platform']:<10} {d['account'] or '(no account)':<18} platform ID {db.get_post(con, pid)['deliveries'][d['key']]['account_ref']}")
    shown = {d["platform"]: d["account"] for d in next(p for p in server.present_posts(con, fill_durations=0) if p["id"] == trailer)["destinations"]}
    check("trailer reaction is suggested Alpha's accounts", shown == {"tiktok": "@alpha_trailers", "instagram": "@alpha.trailers", "youtube": "@AlphaTrailers"})
    shown = {d["platform"]: d["account"] for d in next(p for p in server.present_posts(con, fill_durations=0) if p["id"] == tutorial)["destinations"]}
    check("AI tutorial is suggested Beta's accounts", shown == {"tiktok": "@beta_tutorials", "instagram": "@beta.tutorials"})

    say("\n4. ONE CLIP TO TWO DIFFERENT TIKTOKS (Instagram removed, second TikTok added)")
    ready(con, trailer)
    unapproved = server.op_update(con, cfg, trailer, {"destinations": [
        {"platform": "tiktok", "account_id": by_handle["@alpha_trailers"]["id"]},
        {"platform": "tiktok", "account_id": by_handle["@beta_tutorials"]["id"]},
        {"platform": "youtube", "account_id": by_handle["@AlphaTrailers"]["id"]}]})
    check("changing destinations cleared the approval", unapproved and not db.get_post(con, trailer)["approved_at"])
    ready(con, trailer)
    engine.run_once(con, cfg)
    for u in fake.uploads:
        say(f"   simulated request → profile {u['profile']:<11} platforms {u['platforms']}")
    check("one request per profile, each with only its selected platform",
          sorted((u["profile"], u["platforms"]) for u in fake.uploads) == [("test-alpha", ["tiktok"]), ("test-beta", ["tiktok"])])
    d = handles(con, trailer)
    for h in ("@alpha_trailers", "@beta_tutorials"):
        say(f"   {h:<18} {d[h]['status']:<10} {d[h]['url']}")
    check("both TikToks received it", d["@alpha_trailers"]["status"] == d["@beta_tutorials"]["status"] == "published")
    check("Instagram was not sent", d["@alpha.trailers"]["status"] == "skipped" and not any(p[1] == "instagram" for p in fake.posted))
    check("YouTube stayed held by the upload pause", d["@AlphaTrailers"]["status"] == "pending" and "paused" in d["@AlphaTrailers"]["error"])

    say("\n5. THE SAME FILE TO THE SAME TIKTOK IS BLOCKED; A DIFFERENT TIKTOK IS ALLOWED")
    copy = HOME / "shorts" / "trailer-reaction-demo-renamed.mp4"
    shutil.copyfile(clip("trailer-reaction-demo.mp4", "navy"), copy)
    again = new_post(con, copy, "Same trailer file, renamed (demo)", alpha, ["tiktok"])
    db.set_destinations(con, again, [("tiktok", by_handle["@alpha_trailers"]["id"]), ("tiktok", by_handle[main_tok]["id"])])
    ready(con, again)
    sent = len(fake.uploads)
    engine.run_once(con, cfg)
    d = handles(con, again)
    say(f"   @alpha_trailers   {d['@alpha_trailers']['status']:<10} {d['@alpha_trailers']['error']}")
    say(f"   {main_tok:<17} {d[main_tok]['status']:<10} {d[main_tok]['url']}")
    check("duplicate to the same TikTok was blocked and keeps the original link",
          d["@alpha_trailers"]["status"] == "skipped" and "@alpha_trailers" in (d["@alpha_trailers"]["url"] or ""))
    check("a different TikTok, chosen deliberately, received it", d[main_tok]["status"] == "published")
    check("exactly one new request, to the other profile", [u["profile"] for u in fake.uploads[sent:]] == ["default"])
    check("Alpha's TikTok has this file exactly once", fake.posted.count(("test-alpha", "tiktok", "mock-open-id-alpha-tiktok")) == 1)

    say("\n6. A PARTIAL FAILURE RETRIES ONLY THE OUTSTANDING ACCOUNT")
    fake.failing[("test-beta", "tiktok")] = "TikTok rejected the upload (simulated)"
    partial = new_post(con, clip("partial-failure-demo.mp4", "maroon"), "Partial failure (demo)", alpha, ["tiktok"])
    db.set_destinations(con, partial, [("tiktok", by_handle["@alpha_trailers"]["id"]), ("tiktok", by_handle["@beta_tutorials"]["id"])])
    ready(con, partial)
    sent = len(fake.uploads)
    engine.run_once(con, cfg)
    d = handles(con, partial)
    say(f"   first run:  @alpha_trailers {d['@alpha_trailers']['status']}, @beta_tutorials {d['@beta_tutorials']['status']} — {d['@beta_tutorials']['error']}")
    check("one account live, the other failed", d["@alpha_trailers"]["status"] == "published" and d["@beta_tutorials"]["status"] == "failed")
    del fake.failing[("test-beta", "tiktok")]
    server.Handler._route_data(None, con, f"/api/posts/{partial}/history", {"key": d["@beta_tutorials"]["key"], "decision": "absent"})
    server.op_approve(con, partial)
    engine.run_once(con, cfg)
    retried = [(u["profile"], u["platforms"]) for u in fake.uploads[sent + 2:]]
    say(f"   retry sent: {retried}")
    check("the retry went only to the outstanding account", retried == [("test-beta", ["tiktok"])])
    posted = [p for p in fake.posted if p[2] == "mock-open-id-alpha-tiktok"]
    check("the account that already had it was not sent it again", len(posted) == 2)  # trailer + this clip
    check("both are live now", {x["status"] for x in handles(con, partial).values()} == {"published"})

    say("\n7. A PROFILE CHANGE AFTER SCHEDULING NEVER REDIRECTS THE POST")
    sched = new_post(con, clip("scheduled-demo.mp4", "purple"), "Scheduled before a profile change (demo)", beta, ["tiktok", "instagram"])
    ready(con, sched, when="2020-01-01T00:00:00+00:00")
    approved_ref = handles(con, sched)["@beta_tutorials"]["account_ref"]
    accounts.map_account(con, by_handle["@alpha_trailers"]["id"], beta, default=True)   # the default changes
    fake.connect("test-beta", "tiktok", "mock-open-id-someone-else", "@someone_else")   # the profile is reconnected
    sent = len(fake.uploads)
    engine.run_once(con, cfg)
    d = handles(con, sched)
    say(f"   @beta_tutorials  {d['@beta_tutorials']['status']} — {d['@beta_tutorials']['error']}")
    say(f"   @beta.tutorials  {d['@beta.tutorials']['status']}")
    check("the changed account was held for review", d["@beta_tutorials"]["status"] == "needs_review")
    check("nothing went to the replacement account or to the new default",
          not any(p[2] in ("mock-open-id-someone-else",) for p in fake.posted)
          and [u["platforms"] for u in fake.uploads[sent:]] == [["instagram"]])
    check("the delivery still names the approved account ID", d["@beta_tutorials"]["account_ref"] == approved_ref)
    fake.connect("test-beta", "tiktok", approved_ref, "@beta_tutorials")                # reconnect the right one
    server.op_retry(con, sched, {"keys": [d["@beta_tutorials"]["key"]]})
    engine.run_once(con, cfg)
    check("after reconnecting the approved account, it posts there", handles(con, sched)["@beta_tutorials"]["status"] == "published"
          and fake.posted[-1] == ("test-beta", "tiktok", approved_ref))
    accounts.map_account(con, by_handle["@alpha_trailers"]["id"], alpha, default=True)   # put the demo mapping back

    say("\n8. ACTIVITY HISTORY NAMES THE ACCOUNT")
    for e in [e for e in db.recent_events(con, 200) if e["account"]][:6]:
        say(f"   {e['platform']:<10} {e['account']:<28} {e['message'][:70]}")
    say("\nALL CHECKS PASSED — on a copy; the real database, Keychain and accounts were not touched.")
    con.close()


def serve(port: int) -> None:
    if not (HOME / "data" / "scc.sqlite3").exists():
        sys.exit("Run `prove` first so there is a migrated copy to look at.")
    the_fake()
    con = db.connect()
    server.discover(con, config.load())
    con.close()
    server.serve(port=port, open_browser=False)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prove")
    p.add_argument("--db", required=True, type=Path, help="database to copy (it is only read)")
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=8799)
    args = ap.parse_args()
    prove(args.db.resolve()) if args.cmd == "prove" else serve(args.port)
