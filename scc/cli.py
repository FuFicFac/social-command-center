"""scc — Social Command Center CLI."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import captions, config, db, engine, media, schedule, secrets
from .adapters import for_route
from .adapters.meta import Meta
from .adapters.tiktok import TikTok
from .adapters.youtube import YouTube

ICON = {"pending": "·", "uploading": "↑", "processing": "…", "published": "✓", "private": "🔒",
        "inbox": "📥", "simulated": "◌", "failed": "✗", "needs_review": "⚠", "skipped": "–"}


def parse_when(text: str | None, cfg: config.Config) -> str | None:
    if not text or text == "now":
        return None
    tz = ZoneInfo(cfg.get("general", "timezone"))
    dt = datetime.fromisoformat(text.replace(" ", "T")) if "T" in text or ":" in text \
        else datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def local(ts: str | None, cfg: config.Config) -> str:
    if not ts:
        return "asap"
    return datetime.fromisoformat(ts).astimezone(ZoneInfo(cfg.get("general", "timezone"))) \
        .strftime("%a %b %d %-I:%M %p")


def platforms_arg(text: str | None) -> list[str]:
    if not text or text == "all":
        return list(config.PLATFORMS)
    out = [p.strip() for p in text.split(",") if p.strip()]
    bad = [p for p in out if p not in config.PLATFORMS]
    if bad:
        sys.exit(f"unknown platform(s): {bad}; choose from {config.PLATFORMS}")
    return out


def need_post(con, pid: str) -> dict:
    post = db.get_post(con, pid)
    if not post:
        sys.exit(f"no post {pid!r}")
    return post


def print_post(post: dict, cfg: config.Config, full: bool = False) -> None:
    state = "approved" if post["approved_at"] else "draft"
    print(f"{post['id']}  [{state}, {local(post['scheduled_at'], cfg)}]  "
          f"{Path(post['video_path']).name}")
    print(f"    title: {post['title'] or '—'}")
    if full:
        print(f"    caption: {post['caption'] or '—'}")
        for p, o in post["overrides"].items():
            for k, v in o.items():
                print(f"    {p}.{k}: {v}")
    for p, d in sorted(post["deliveries"].items()):
        if d["status"] == "skipped":
            continue
        extra = d["url"] or d["error"] or d["meta"].get("note") or ""
        print(f"    {ICON.get(d['status'], '?')} {p:<9} {d['status']:<12} "
              f"{d['route'] or cfg.route(p):<11} {extra}")


# ---------------------------------------------------------------- commands --
def cmd_status(a, cfg, con):
    print("Platform   Route        Ready  Detail")
    for p in config.PLATFORMS:
        route = cfg.route(p)
        ok, why = for_route(cfg, p, route).readiness(p)
        print(f"{p:<10} {route:<12} {'yes' if ok else 'NO ':<6} {why}")
    print("\nKeychain secrets:")
    for name in secrets.KNOWN:
        print(f"  {'✓' if secrets.get(name) else '·'} {name}")
    counts: dict[str, int] = {}
    for post in db.list_posts(con):
        for d in post["deliveries"].values():
            counts[d["status"]] = counts.get(d["status"], 0) + 1
    print("\nDeliveries:", ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "none yet")


def cmd_config(a, cfg, con):
    section, _, key = a.key.partition(".")
    if not key:
        sys.exit("use section.key, e.g. meta.app_id")
    value = {"true": True, "false": False}.get(a.value.lower(), a.value)
    cfg.set(section, key, value)
    config.save(cfg)
    print(f"{section}.{key} = {value!r}")


def cmd_route(a, cfg, con):
    if a.route not in config.ROUTES:
        sys.exit(f"route must be one of {config.ROUTES}")
    for p in platforms_arg(a.platforms):
        cfg.set("routing", p, a.route)
    config.save(cfg)
    cmd_status(a, cfg, con)


def cmd_secret(a, cfg, con):
    if a.action == "list":
        for name, hint in secrets.KNOWN.items():
            print(f"{'✓' if secrets.get(name) else '·'} {name:<24} {hint}")
    elif a.action == "set":
        value = getpass.getpass(f"{a.name} (hidden): ").strip()
        if not value:
            sys.exit("empty — nothing saved")
        secrets.put(a.name, value)
        print(f"saved {a.name} to Keychain")
    elif a.action == "clear":
        secrets.delete(a.name)
        print(f"removed {a.name}")


def cmd_connect(a, cfg, con):
    if a.platform == "youtube":
        who = YouTube(cfg).connect()
    elif a.platform == "meta":
        who = Meta(cfg).connect()
        config.save(cfg)
    elif a.platform == "tiktok":
        who = TikTok(cfg).connect()
    elif a.platform == "upload-post":
        if not secrets.get("upload_post.api_key"):
            secrets.put("upload_post.api_key", getpass.getpass("Upload-Post API key (hidden): "))
        if not cfg.get("upload_post", "user"):
            cfg.set("upload_post", "user", input("Upload-Post profile name: ").strip())
            config.save(cfg)
        who = f"profile {cfg.get('upload_post', 'user')}"
    else:
        sys.exit("connect youtube | meta | tiktok | upload-post")
    print(f"Connected: {who}")


def _add(con, cfg, path: Path, title: str | None, caption: str | None, to: list[str],
         at: str | None) -> str | None:
    path = path.expanduser().resolve()
    if not path.exists():
        sys.exit(f"no such file: {path}")
    if con.execute("SELECT 1 FROM posts WHERE video_path=?", (str(path),)).fetchone():
        return None
    pid = db.create_post(con, str(path), media.sha256(path),
                         title or media.title_from_filename(path), caption or "", to,
                         parse_when(at, cfg))
    dur = media.duration(path)
    if dur is not None:
        con.execute("UPDATE posts SET duration=? WHERE id=?", (dur, pid))
    return pid


def cmd_add(a, cfg, con):
    pid = _add(con, cfg, Path(a.video), a.title, a.caption, platforms_arg(a.to), a.at)
    if not pid:
        sys.exit("that video is already queued (scc list)")
    if a.ai:
        _caption_one(con, pid)
    print_post(need_post(con, pid), cfg, full=True)
    print("\nDraft saved. Review it, then: scc approve", pid)


def cmd_import(a, cfg, con):
    folder = Path(a.folder or cfg.get("general", "shorts_folder"))
    added = 0
    for f in sorted(folder.glob("*.mp4")):
        if _add(con, cfg, f, None, None, platforms_arg(a.to), None):
            added += 1
    print(f"imported {added} new short(s) from {folder} as drafts")


def _caption_one(con, pid: str) -> None:
    post = need_post(con, pid)
    print(f"  transcribing + drafting captions for {Path(post['video_path']).name} …", flush=True)
    text = captions.transcribe(Path(post["video_path"]), post["sha256"] or post["id"])
    fields = captions.to_post_fields(captions.draft(text, post["title"]))
    db.update_post(con, post["id"], **fields)
    db.log(con, "info", "AI caption draft written", post["id"])


def cmd_captions(a, cfg, con):
    posts = [need_post(con, a.id)] if a.id else [
        p for p in db.list_posts(con) if not p["caption"] and not p["approved_at"]]
    for p in posts:
        try:
            _caption_one(con, p["id"])
        except Exception as e:
            print(f"  ✗ {p['id']}: {e}")
    print(f"drafted captions for {len(posts)} post(s)")


def cmd_edit(a, cfg, con):
    post = need_post(con, a.id)
    fields: dict = {}
    if a.title is not None:
        fields["title"] = a.title
    if a.caption is not None:
        fields["caption"] = a.caption
    if a.set:
        o = post["overrides"]
        for item in a.set:
            key, _, val = item.partition("=")
            plat, _, field = key.partition(".")
            o.setdefault(plat, {})[field or "caption"] = val
        fields["overrides"] = o
    if a.at is not None:
        fields["scheduled_at"] = parse_when(a.at, cfg)
    if fields:
        if post["approved_at"] and any(k in fields for k in ("title", "caption", "overrides")):
            fields["approved_at"] = None
            print("copy changed — approval cleared; re-approve when happy")
        db.update_post(con, post["id"], **fields)
    if a.to:
        db.set_targets(con, post["id"], platforms_arg(a.to))
    print_post(need_post(con, post["id"]), cfg, full=True)


def preflight_report(post: dict, cfg) -> list[str]:
    return media.post_issues(post)


def cmd_approve(a, cfg, con):
    post = need_post(con, a.id)
    issues = preflight_report(post, cfg)
    if issues:
        sys.exit("can't approve yet:\n  " + "\n  ".join(issues))
    fields = {"approved_at": db.now()}
    if a.at:
        fields["scheduled_at"] = parse_when(a.at, cfg)
    db.update_post(con, post["id"], **fields)
    db.log(con, "info", "approved", post["id"])
    post = need_post(con, post["id"])
    print_post(post, cfg)
    print(f"\napproved — goes out {local(post['scheduled_at'], cfg)} on the next scheduler run")


def cmd_unapprove(a, cfg, con):
    db.update_post(con, need_post(con, a.id)["id"], approved_at=None)
    print("back to draft")


def cmd_publish(a, cfg, con):
    post = need_post(con, a.id)
    issues = preflight_report(post, cfg)
    if issues:
        sys.exit("blocked:\n  " + "\n  ".join(issues))
    print_post(post, cfg, full=True)
    live = [p for p, d in post["deliveries"].items()
            if d["status"] in ("pending", "failed") and cfg.route(p) != "dryrun"]
    if live and not a.yes:
        ans = input(f"\nPost this LIVE to {', '.join(live)} now? Type 'post' to confirm: ")
        if ans.strip().lower() != "post":
            sys.exit("cancelled")
    for p, d in post["deliveries"].items():
        if d["status"] == "failed":
            db.update_delivery(con, post["id"], p, status="pending", attempts=0)
    db.update_post(con, post["id"], approved_at=db.now(), scheduled_at=None)
    print(engine.run_once(con, cfg))
    print_post(need_post(con, post["id"]), cfg)


def cmd_list(a, cfg, con):
    posts = db.list_posts(con)
    for post in posts:
        print_post(post, cfg)
    if not posts:
        print("queue is empty — scc import  or  scc add VIDEO")


def cmd_show(a, cfg, con):
    post = need_post(con, a.id)
    print_post(post, cfg, full=True)
    for e in con.execute("SELECT * FROM events WHERE post_id=? ORDER BY id", (post["id"],)):
        print(f"    {e['at']} {e['level']:<5} {e['platform'] or '':<10} {e['message']}")


def cmd_run(a, cfg, con):
    print(engine.run_once(con, cfg))


def cmd_daemon(a, cfg, con):
    print("scheduler running every 60s — Ctrl-C to stop")
    while True:
        print(datetime.now().strftime("%H:%M:%S"), engine.run_once(con, config.load()), flush=True)
        time.sleep(60)


def cmd_retry(a, cfg, con):
    post = need_post(con, a.id)
    for p in platforms_arg(a.platforms):
        if p in post["deliveries"] and post["deliveries"][p]["status"] not in ("skipped",):
            db.update_delivery(con, post["id"], p, status="pending", error=None, attempts=0,
                               remote_id=None, url=None, meta={})
            print(f"{p}: reset to pending")


def cmd_mark(a, cfg, con):
    post = need_post(con, a.id)
    db.update_delivery(con, post["id"], a.platform, status=a.status, url=a.url, error=None)
    db.log(con, "info", f"manually marked {a.status}", post["id"], a.platform)
    print_post(need_post(con, post["id"]), cfg)


def cmd_remove(a, cfg, con):
    post = need_post(con, a.id)
    if any(d["status"] in ("uploading", "processing") for d in post["deliveries"].values()):
        sys.exit("still in flight; wait for it to settle")
    con.execute("DELETE FROM posts WHERE id=?", (post["id"],))
    print("removed from queue (the video file is untouched)")


def cmd_slots(a, cfg, con):
    taken = schedule.taken_slots(con)
    now = datetime.now(UTC)
    n = 0
    for s in schedule.slots_between(cfg, schedule.local_today(cfg), a.days):
        if s <= now:
            continue
        pid = taken.get(s)
        print(f"{schedule.slot_label(cfg, s):<22} {pid or 'free'}")
        n += pid is None
    print(f"\n{n} free slot(s) in the next {a.days} day(s)  "
          f"(slots {','.join(cfg.slots())}; days {','.join(cfg.days())})")


def cmd_schedule(a, cfg, con):
    if a.action != "auto":
        sys.exit("schedule auto ID... [--start YYYY-MM-DD] [--approve] [--dry-run]")
    ids = [need_post(con, i)["id"] for i in a.ids]
    start = date.fromisoformat(a.start) if a.start else None
    assigned, errors = schedule.auto_assign(con, cfg, ids, start, a.approve, a.dry_run)
    for pid, at in assigned.items():
        print(f"{pid}  {local(at, cfg)}" + ("  (dry run)" if a.dry_run else ""))
    for pid, msg in errors.items():
        print(f"{pid}  ✗ {msg}")
    if not assigned and not errors:
        print("nothing to do")


def cmd_scheduler(a, cfg, con):
    import subprocess

    from .jobs import PLIST
    if a.action == "status":
        print(f"launchd agent: {'installed' if PLIST.exists() else 'not installed'} ({PLIST})")
        return
    script = Path(__file__).resolve().parent.parent / "scripts" / f"{a.action}-scheduler.sh"
    r = subprocess.run(["sh", str(script)], check=False, capture_output=True, text=True, timeout=30)
    print((r.stdout + r.stderr).strip())
    if r.returncode:
        sys.exit(r.returncode)


def cmd_serve(a, cfg, con):
    from .server import serve
    serve(a.port, open_browser=not a.no_open)


def cmd_dump(a, cfg, con):
    print(json.dumps(db.list_posts(con), indent=2))


def build() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="scc", description="Post shorts everywhere at once.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(fn=fn)
        return sp

    add("status", cmd_status, "connections, routes and queue counts")
    sp = add("config", cmd_config, "set a config value, e.g. scc config meta.app_id 123")
    sp.add_argument("key")
    sp.add_argument("value")
    sp = add("route", cmd_route, "route platforms: scc route upload_post tiktok")
    sp.add_argument("route")
    sp.add_argument("platforms", nargs="?", default="all")
    sp = add("secret", cmd_secret, "manage Keychain secrets")
    sp.add_argument("action", choices=["list", "set", "clear"])
    sp.add_argument("name", nargs="?", choices=list(secrets.KNOWN))
    sp = add("connect", cmd_connect, "sign in: youtube | meta | tiktok | upload-post")
    sp.add_argument("platform")
    sp = add("add", cmd_add, "queue a video as a draft")
    sp.add_argument("video")
    sp.add_argument("--title")
    sp.add_argument("--caption")
    sp.add_argument("--to", help="comma list or 'all' (default)")
    sp.add_argument("--at", help="'2026-10-24 09:00' (Pacific) or 'now'")
    sp.add_argument("--ai", action="store_true", help="draft captions with Whisper + Claude")
    sp = add("import", cmd_import, "queue every new MP4 in Shorts Ready as drafts")
    sp.add_argument("folder", nargs="?")
    sp.add_argument("--to")
    sp = add("captions", cmd_captions, "AI-draft captions (one id, or every uncaptioned draft)")
    sp.add_argument("id", nargs="?")
    sp = add("edit", cmd_edit, "change copy, targets or time")
    sp.add_argument("id")
    sp.add_argument("--title")
    sp.add_argument("--caption")
    sp.add_argument("--set", action="append", help="platform.field=text, e.g. tiktok.caption=hi")
    sp.add_argument("--to")
    sp.add_argument("--at")
    sp = add("approve", cmd_approve, "approve a draft (optionally schedule it)")
    sp.add_argument("id")
    sp.add_argument("--at")
    sp = add("unapprove", cmd_unapprove, "pull a post back to draft")
    sp.add_argument("id")
    sp = add("publish", cmd_publish, "approve + post right now (asks to confirm)")
    sp.add_argument("id")
    sp.add_argument("--yes", action="store_true")
    add("list", cmd_list, "show the queue")
    sp = add("show", cmd_show, "one post with its event log")
    sp.add_argument("id")
    add("run", cmd_run, "one scheduler pass (launchd calls this every minute)")
    add("daemon", cmd_daemon, "run the scheduler in the foreground")
    sp = add("retry", cmd_retry, "reset platform deliveries to pending")
    sp.add_argument("id")
    sp.add_argument("platforms", nargs="?", default="all")
    sp = add("mark", cmd_mark, "record what you verified by hand")
    sp.add_argument("id")
    sp.add_argument("platform", choices=config.PLATFORMS)
    sp.add_argument("status", choices=["published", "private", "failed", "skipped"])
    sp.add_argument("--url")
    sp = add("remove", cmd_remove, "drop a post from the queue")
    sp.add_argument("id")
    sp = add("slots", cmd_slots, "upcoming posting slots and who holds them")
    sp.add_argument("--days", type=int, default=14)
    sp = add("schedule", cmd_schedule, "schedule auto ID... : fill the next free slots")
    sp.add_argument("action", choices=["auto"])
    sp.add_argument("ids", nargs="+")
    sp.add_argument("--start", help="YYYY-MM-DD (local); default today")
    sp.add_argument("--approve", action="store_true", help="approve too (runs preflight)")
    sp.add_argument("--dry-run", action="store_true", help="show the plan, save nothing")
    sp = add("scheduler", cmd_scheduler, "launchd agent: install | uninstall | status")
    sp.add_argument("action", choices=["install", "uninstall", "status"])
    sp = add("serve", cmd_serve, "open the dashboard")
    sp.add_argument("--port", type=int, default=8787)
    sp.add_argument("--no-open", action="store_true")
    add("dump", cmd_dump, "queue as JSON")
    return ap


def main(argv: list[str] | None = None) -> None:
    a = build().parse_args(argv)
    cfg = config.load()
    con = db.connect()
    try:
        a.fn(a, cfg, con)
    except secrets.MissingSecret as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
