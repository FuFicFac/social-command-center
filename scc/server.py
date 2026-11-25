"""Local dashboard. Binds to 127.0.0.1 only; every write needs the per-launch token.

The wire format is specified in docs/API.md. Handlers never do long work inline: captions,
publishing, the scheduler and OAuth sign-ins all run in threads (see jobs.py), and every
request opens its own SQLite connection.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets as pysecrets
import subprocess
import threading
import time
import webbrowser
from datetime import UTC, date, datetime
from difflib import SequenceMatcher
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import config, db, jobs, media, schedule
from . import secrets as keychain
from .adapters import for_route
from .cli import import_folder, parse_when

STATIC = Path(__file__).parent / "static"
SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
TOKEN = pysecrets.token_urlsafe(24)
CSP = ("default-src 'self'; img-src 'self' data:; media-src 'self'; "
       "style-src 'self' 'unsafe-inline'; script-src 'self'")
CONTENT_TYPES = {
    ".js": "text/javascript; charset=utf-8", ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".html": "text/html; charset=utf-8",
    ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".ico": "image/x-icon", ".json": "application/json", ".woff2": "font/woff2",
    ".woff": "font/woff", ".map": "application/json", ".txt": "text/plain; charset=utf-8",
}
MAX_BODY = 1 << 20
META_PAGE_TTL = 600
SECRET_FIELDS = ("api_key", "app_secret", "client_secret", "user_token")

_cfg_lock = threading.RLock()      # serializes config.toml read-modify-write
_thumb_lock = threading.Lock()
_meta_lock = threading.Lock()
_meta_pages: dict[str, Any] = {"expires": 0.0, "pages": {}}


class Fail(Exception):
    def __init__(self, message: str, code: int = 400, issues: list[str] | None = None):
        super().__init__(message)
        self.code, self.message, self.issues = code, message, issues


def scrub(text: str, secret_values: list[str]) -> str:
    for v in secret_values:
        if v and len(v) >= 4:
            text = text.replace(v, "***")
    return text


# ------------------------------------------------------------------- state --
def settings_view(cfg: config.Config) -> dict[str, Any]:
    return {
        "timezone": cfg.get("general", "timezone"),
        "shorts_folder": cfg.get("general", "shorts_folder"),
        "schedule": {"slots": cfg.slots(), "days": cfg.days()},
        "routing": {p: cfg.route(p) for p in config.PLATFORMS},
        "tiktok_mode": cfg.get("tiktok", "mode"),
    }


def connections_view(cfg: config.Config) -> dict[str, Any]:
    """Non-secret connection facts so the UI can show what is already saved."""
    def has(name: str) -> bool:
        return bool(keychain.get(name))

    return {
        "youtube": {"client_id": cfg.get("youtube", "client_id"),
                    "has_client_secret": has("youtube.client_secret"),
                    "signed_in": has("youtube.refresh_token")},
        "meta": {"app_id": cfg.get("meta", "app_id"), "has_app_secret": has("meta.app_secret"),
                 "page_id": cfg.get("meta", "page_id"), "ig_user_id": cfg.get("meta", "ig_user_id"),
                 "signed_in": has("meta.page_token")},
        "tiktok": {"client_key": cfg.get("tiktok", "client_key"),
                   "mode": cfg.get("tiktok", "mode"),
                   "has_client_secret": has("tiktok.client_secret"),
                   "signed_in": has("tiktok.refresh_token")},
        "upload_post": {"user": cfg.get("upload_post", "user"), "has_api_key": has("upload_post.api_key")},
    }


def slug(path: str) -> str:
    return re.sub(r"[^a-z0-9]", "", media.title_from_filename(path).lower())


_SIM_CACHE: dict[tuple[str, str], float] = {}
SAME_CONTENT = 0.5  # transcript similarity; re-cuts score 0.67-0.96, unrelated shorts < 0.3


def _transcript_words(sha: str | None) -> list[str] | None:
    if not sha:
        return None
    f = config.DATA_DIR / "transcripts" / f"{sha}.txt"
    return f.read_text().lower().split() if f.exists() else None


def duplicates(posts: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Same spoken content (cached transcripts) — or, lacking transcripts, the same title slug."""
    words = {p["id"]: _transcript_words(p.get("sha256")) for p in posts}
    slugs = {p["id"]: slug(p["video_path"]) for p in posts}
    out: dict[str, list[str]] = {p["id"]: [] for p in posts}
    for i, a in enumerate(posts):
        for b in posts[i + 1:]:
            wa, wb = words[a["id"]], words[b["id"]]
            if wa and wb:
                key = tuple(sorted((a.get("sha256") or a["id"], b.get("sha256") or b["id"])))
                if key not in _SIM_CACHE:
                    _SIM_CACHE[key] = SequenceMatcher(None, wa, wb, autojunk=False).ratio()
                same = _SIM_CACHE[key] >= SAME_CONTENT
            else:
                same = bool(slugs[a["id"]]) and slugs[a["id"]] == slugs[b["id"]]
            if same:
                out[a["id"]].append(b["id"])
                out[b["id"]].append(a["id"])
    return out


def present_posts(con, posts: list[dict[str, Any]] | None = None,
                  fill_durations: int = 8) -> list[dict[str, Any]]:
    posts = db.list_posts(con) if posts is None else posts
    dupes = duplicates(posts)
    out = []
    for p in posts:
        dur = p.get("duration")
        if dur is None and fill_durations > 0 and Path(p["video_path"]).exists():
            fill_durations -= 1
            dur = media.duration(p["video_path"])
            if dur is not None:
                con.execute("UPDATE posts SET duration=? WHERE id=?", (dur, p["id"]))
        deliveries = {}
        for plat in config.PLATFORMS:
            d = p["deliveries"].get(plat)
            deliveries[plat] = (
                {"status": d["status"], "route": d["route"], "url": d["url"], "error": d["error"],
                 "attempts": d["attempts"], "meta": d["meta"], "updated_at": d["updated_at"]}
                if d else {"status": "skipped", "route": None, "url": None, "error": None,
                           "attempts": 0, "meta": {}, "updated_at": p["updated_at"]})
        out.append({
            "id": p["id"], "video_path": p["video_path"], "filename": Path(p["video_path"]).name,
            "title": p["title"], "caption": p["caption"],
            "overrides": {k: {a: b for a, b in o.items() if b}
                          for k, o in p["overrides"].items() if any(o.values())},
            "scheduled_at": p["scheduled_at"], "approved_at": p["approved_at"],
            "created_at": p["created_at"], "updated_at": p["updated_at"],
            "duration": dur,
            "duplicate_of": dupes.get(p["id"], []),
            "bucket": db.bucket(p), "deliveries": deliveries,
        })
    return out


def state() -> dict[str, Any]:
    cfg = config.load()
    con = db.connect()
    try:
        platforms = {}
        for p in config.PLATFORMS:
            route = cfg.route(p)
            configured = {r: for_route(cfg, p, r).readiness(p)[0] for r in ("direct", "upload_post")}
            ok, why = for_route(cfg, p, route).readiness(p)
            platforms[p] = {"route": route, "ready": ok, "detail": why, "configured": configured}
        posts = present_posts(con)
        counts = {"drafts": 0, "needs_captions": 0, "scheduled": 0, "posted": 0, "attention": 0}
        for p in posts:
            b = p["bucket"]
            if b == "draft":
                counts["drafts"] += 1
                counts["needs_captions"] += not p["caption"].strip()
            elif b in counts:
                counts[b] += 1
        return {
            "settings": settings_view(cfg), "platforms": platforms, "connections": connections_view(cfg),
            "posts": posts, "jobs": jobs.post_jobs(), "caption_queue": jobs.caption_queue(),
            "connect": jobs.connect_state(), "scheduler": jobs.scheduler_state(),
            "events": db.recent_events(con, 60), "counts": counts,
        }
    finally:
        con.close()


def slots_view(query: dict[str, list[str]]) -> dict[str, Any]:
    cfg = config.load()
    try:
        start = date.fromisoformat(query["from"][0]) if query.get("from") else None
        days = int(query["days"][0]) if query.get("days") else 28
    except ValueError as e:
        raise Fail(f"bad query: {e}") from e
    days = max(1, min(days, 120))
    start = start or schedule.local_today(cfg)
    con = db.connect()
    try:
        taken = schedule.taken_slots(con)
    finally:
        con.close()
    now = datetime.now(UTC)
    return {"slots": [{"at": schedule.iso(s), "local": schedule.slot_label(cfg, s),
                       "post_id": taken.get(s)}
                      for s in schedule.slots_between(cfg, start, days) if s > now]}


# -------------------------------------------------------------- operations --
def need_post(con, post_id: str) -> dict[str, Any]:
    post = db.get_post(con, post_id) if re.fullmatch(r"[0-9a-f]{10}", str(post_id)) else None
    if not post or post["id"] != post_id:
        raise Fail("no such post", 404)
    return post


def in_flight(post: dict[str, Any]) -> bool:
    return any(d["status"] in db.ACTIVE for d in post["deliveries"].values())


def op_approve(con, pid: str) -> None:
    post = need_post(con, pid)
    issues = media.post_issues(post)
    if issues:
        raise Fail("fix first", 400, issues)
    db.update_post(con, pid, approved_at=db.now())
    db.log(con, "info", "approved in dashboard", pid)


def op_unapprove(con, pid: str) -> None:
    need_post(con, pid)
    db.update_post(con, pid, approved_at=None)


def op_remove(con, pid: str) -> None:
    if in_flight(need_post(con, pid)):
        raise Fail("still in flight", 409)
    con.execute("DELETE FROM posts WHERE id=?", (pid,))


def op_targets(con, pid: str, targets: Any) -> None:
    if not isinstance(targets, list) or any(t not in config.PLATFORMS for t in targets):
        raise Fail(f"targets must be a list of {list(config.PLATFORMS)}")
    need_post(con, pid)
    db.set_targets(con, pid, targets)


def op_unschedule(con, pid: str) -> None:
    # Approved + no time means "post now", so taking a post off the calendar also
    # returns it to draft — unscheduling must never trigger a post.
    need_post(con, pid)
    db.update_post(con, pid, scheduled_at=None, approved_at=None)


def op_captions(con, pid: str) -> None:
    need_post(con, pid)
    try:
        jobs.enqueue_captions(pid)
    except ValueError as e:
        raise Fail(str(e), 409) from e


def op_update(con, cfg, pid: str, body: dict[str, Any]) -> bool:
    post = need_post(con, pid)
    fields: dict[str, Any] = {}
    for k in ("title", "caption"):
        if k in body:
            fields[k] = str(body[k])
    if "overrides" in body:
        if not isinstance(body["overrides"], dict):
            raise Fail("overrides must be an object")
        fields["overrides"] = {
            p: {k: str(v) for k, v in o.items() if k in ("title", "caption") and v}
            for p, o in body["overrides"].items() if p in config.PLATFORMS and isinstance(o, dict)}
    if "scheduled_at" in body:
        try:
            fields["scheduled_at"] = parse_when(body["scheduled_at"] or None, cfg)
        except ValueError as e:
            raise Fail(f"bad scheduled_at: {e}") from e
    changed = {k for k in ("title", "caption", "overrides") if k in fields
               and fields[k] != (post[k] if k != "overrides" else (post["overrides"] or {}))}
    unapproved = bool(post["approved_at"] and changed)
    if unapproved:
        fields["approved_at"] = None
    if fields:
        db.update_post(con, pid, **fields)
    if "targets" in body:
        op_targets(con, pid, body["targets"])
    return unapproved


def op_publish(con, pid: str, body: dict[str, Any]) -> None:
    post = need_post(con, pid)
    if body.get("confirm") != "post":
        raise Fail("confirmation required")
    for p, d in post["deliveries"].items():
        if d["status"] == "failed":
            db.update_delivery(con, pid, p, status="pending", attempts=0)
    db.update_post(con, pid, approved_at=db.now(), scheduled_at=None)
    db.log(con, "info", "publish-now from dashboard", pid)
    try:
        jobs.background(pid, "publishing", jobs.publish_job)
    except ValueError as e:
        raise Fail(str(e), 409) from e


def op_retry(con, pid: str, body: dict[str, Any]) -> None:
    post = need_post(con, pid)
    plats = body.get("platforms") or []
    if not isinstance(plats, list):
        raise Fail("platforms must be a list")
    chosen = [p for p in plats if p in post["deliveries"]]
    if any(post["deliveries"][p]["status"] in db.ACTIVE for p in chosen):
        raise Fail("still in flight — wait for it to settle", 409)
    for p in chosen:
        db.update_delivery(con, pid, p, status="pending", error=None, attempts=0,
                           remote_id=None, url=None, meta={})


BULK = {
    "captions": lambda con, pid, body: op_captions(con, pid),
    "approve": lambda con, pid, body: op_approve(con, pid),
    "unapprove": lambda con, pid, body: op_unapprove(con, pid),
    "remove": lambda con, pid, body: op_remove(con, pid),
    "targets": lambda con, pid, body: op_targets(con, pid, body.get("targets")),
    "unschedule": lambda con, pid, body: op_unschedule(con, pid),
}


def op_bulk(con, body: dict[str, Any]) -> dict[str, str]:
    action, ids = body.get("action"), body.get("ids")
    if action not in BULK:
        raise Fail(f"action must be one of {list(BULK)}")
    if not isinstance(ids, list):
        raise Fail("ids must be a list")
    results: dict[str, str] = {}
    for pid in ids:
        try:
            BULK[action](con, str(pid), body)
            results[str(pid)] = "ok"
        except Fail as e:
            results[str(pid)] = "; ".join([e.message, *(e.issues or [])])
        except Exception as e:
            results[str(pid)] = f"{type(e).__name__}: {e}"
    return results


def op_schedule_auto(con, cfg, body: dict[str, Any]) -> dict[str, Any]:
    ids = body.get("ids")
    if not isinstance(ids, list):
        raise Fail("ids must be a list")
    start = None
    if body.get("start"):
        try:
            start = date.fromisoformat(str(body["start"]))
        except ValueError as e:
            raise Fail("start must be YYYY-MM-DD") from e
    assigned, errors = schedule.auto_assign(
        con, cfg, [str(i) for i in ids], start, bool(body.get("approve")), bool(body.get("dry_run")))
    return {"ok": True, "assigned": assigned, "errors": errors}


def op_schedule_clear(con, body: dict[str, Any]) -> dict[str, Any]:
    ids = body.get("ids")
    if not isinstance(ids, list):
        raise Fail("ids must be a list")
    results = {}
    for pid in ids:
        try:
            op_unschedule(con, str(pid))
            results[str(pid)] = "ok"
        except Fail as e:
            results[str(pid)] = e.message
    return {"ok": True, "results": results}


# ---------------------------------------------------------------- settings --
def apply_settings(body: dict[str, Any]) -> dict[str, Any]:
    with _cfg_lock:
        cfg = config.load()
        issues: list[str] = []
        new: list[tuple[str, str, Any]] = []
        if "timezone" in body:
            try:
                ZoneInfo(str(body["timezone"]))
                new.append(("general", "timezone", str(body["timezone"])))
            except (ZoneInfoNotFoundError, ValueError, OSError):
                issues.append(f"unknown timezone {body['timezone']!r}")
        if "shorts_folder" in body:
            folder = Path(str(body["shorts_folder"])).expanduser()
            if folder.is_dir():
                new.append(("general", "shorts_folder", str(folder)))
            else:
                issues.append(f"shorts_folder does not exist: {folder}")
        sched = body.get("schedule")
        if sched is not None:
            if not isinstance(sched, dict):
                issues.append("schedule must be an object")
            else:
                if "slots" in sched:
                    try:
                        slots = config._csv(sched["slots"])
                        times = sorted({schedule.parse_slot(s) for s in slots})
                        if not times:
                            raise ValueError("need at least one slot")
                        new.append(("schedule", "slots", ",".join(t.strftime("%H:%M") for t in times)))
                    except ValueError as e:
                        issues.append(str(e))
                if "days" in sched:
                    days = [d.lower() for d in config._csv(sched["days"])]
                    bad = [d for d in days if d not in config.WEEKDAYS]
                    if bad or not days:
                        issues.append(f"days must be a non-empty subset of {list(config.WEEKDAYS)}")
                    else:
                        new.append(("schedule", "days",
                                    ",".join(d for d in config.WEEKDAYS if d in days)))
        routing = body.get("routing")
        if routing is not None:
            if not isinstance(routing, dict):
                issues.append("routing must be an object")
            else:
                for p, r in routing.items():
                    if p not in config.PLATFORMS:
                        issues.append(f"unknown platform {p!r}")
                    elif r not in config.ROUTES:
                        issues.append(f"route for {p} must be one of {list(config.ROUTES)}")
                    else:
                        new.append(("routing", p, r))
        if "tiktok_mode" in body:
            if body["tiktok_mode"] in ("inbox", "direct"):
                new.append(("tiktok", "mode", body["tiktok_mode"]))
            else:
                issues.append("tiktok_mode must be inbox or direct")
        if issues:
            raise Fail(issues[0], 400, issues)
        for section, key, value in new:
            cfg.set(section, key, value)
        config.save(cfg)
        return settings_view(cfg)


# ------------------------------------------------------------- connections --
def _text(body: dict[str, Any], key: str) -> str:
    v = body.get(key)
    return v.strip() if isinstance(v, str) else ""


def _save_cfg(section: str, **values: Any) -> None:
    with _cfg_lock:
        cfg = config.load()
        for k, v in values.items():
            cfg.set(section, k, v)
        config.save(cfg)


def verify_platform(cfg: config.Config, platform: str) -> tuple[bool, str]:
    route = cfg.route(platform)
    if route == "dryrun":
        return True, "dry run"
    adapter = for_route(cfg, platform, route)
    ok, why = adapter.readiness(platform)
    if not ok:
        return False, why
    fn = getattr(adapter, "verify", None)
    if fn is None:
        return True, why
    try:
        return fn(platform)
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:300]}"


def disconnect(platform: str, route: str) -> list[str]:
    if platform not in config.PLATFORMS:
        raise Fail("unknown platform")
    if route == "upload_post":
        names = ["upload_post.api_key"]
    elif route == "direct":
        names = [{"youtube": "youtube.refresh_token", "instagram": "meta.page_token",
                  "facebook": "meta.page_token", "tiktok": "tiktok.refresh_token"}[platform]]
    else:
        raise Fail("route must be direct or upload_post")
    for n in names:
        keychain.delete(n)
    return names


def meta_remember(pages: list[dict[str, Any]]) -> None:
    with _meta_lock:
        _meta_pages["expires"] = time.monotonic() + META_PAGE_TTL
        _meta_pages["pages"] = {str(p["id"]): p for p in pages}


def meta_page(page_id: str) -> dict[str, Any]:
    with _meta_lock:
        if time.monotonic() > _meta_pages["expires"] or page_id not in _meta_pages["pages"]:
            raise Fail("page list expired or page unknown — paste the token again")
        return _meta_pages["pages"][page_id]


def run_script(name: str) -> dict[str, Any]:
    try:
        r = subprocess.run(["sh", str(SCRIPTS / name)], check=False, capture_output=True, text=True,
                           timeout=30, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": f"{name} timed out after 30s"}
    return {"ok": r.returncode == 0, "output": (r.stdout + r.stderr).strip()}


def connect_post(path: str, body: dict[str, Any]) -> dict[str, Any] | None:
    """Connection endpoints. Returns None when the path isn't one of them."""
    with _cfg_lock:
        cfg = config.load()
    if path == "/api/connect/upload_post":
        if key := _text(body, "api_key"):
            keychain.put("upload_post.api_key", key)
        if user := _text(body, "user"):
            _save_cfg("upload_post", user=user)
        cfg = config.load()
        ok, why = upload_post_verify(cfg)
        return {"ok": ok, "detail": why}
    if path == "/api/connect/meta/app":
        if aid := _text(body, "app_id"):
            _save_cfg("meta", app_id=aid)
        if sec := _text(body, "app_secret"):
            keychain.put("meta.app_secret", sec)
        return {"ok": True}
    if path == "/api/connect/meta/token":
        token = _text(body, "user_token")
        if not token:
            raise Fail("user_token required")
        from .adapters.meta import Meta
        fn = getattr(Meta(cfg), "list_pages", None)
        if fn is None:
            raise Fail("this build can't list Pages yet", 501)
        try:
            pages = fn(token)
        except Exception as e:
            raise Fail(scrub(f"Meta rejected the token: {str(e)[:300]}", [token])) from None
        meta_remember(pages)
        return {"ok": True, "pages": [{"id": str(p["id"]), "name": p.get("name"),
                                       "ig_username": p.get("ig_username")} for p in pages]}
    if path == "/api/connect/meta/page":
        page = meta_page(_text(body, "page_id"))
        from .adapters.meta import Meta
        with _cfg_lock:
            cfg = config.load()
            detail = Meta(cfg).use_page(page)
            config.save(cfg)
        return {"ok": True, "detail": detail}
    if path == "/api/connect/youtube/client":
        if cid := _text(body, "client_id"):
            _save_cfg("youtube", client_id=cid)
        if sec := _text(body, "client_secret"):
            keychain.put("youtube.client_secret", sec)
        return {"ok": True}
    if path == "/api/connect/tiktok/client":
        mode = _text(body, "mode")
        if mode and mode not in ("inbox", "direct"):
            raise Fail("mode must be inbox or direct")
        if key := _text(body, "client_key"):
            _save_cfg("tiktok", client_key=key)
        if mode:
            _save_cfg("tiktok", mode=mode)
        if sec := _text(body, "client_secret"):
            keychain.put("tiktok.client_secret", sec)
        return {"ok": True}
    if path in ("/api/connect/youtube/start", "/api/connect/tiktok/start"):
        try:
            jobs.start_connect(path.split("/")[3])
        except ValueError as e:
            raise Fail(str(e), 409) from e
        return {"ok": True}
    if path == "/api/connect/verify":
        platform = _text(body, "platform")
        if platform not in config.PLATFORMS:
            raise Fail("unknown platform")
        ok, detail = verify_platform(cfg, platform)
        return {"ok": ok, "detail": detail}
    if path == "/api/disconnect":
        names = disconnect(_text(body, "platform"), _text(body, "route"))
        return {"ok": True, "removed": names}
    return None


def upload_post_verify(cfg: config.Config) -> tuple[bool, str]:
    from .adapters.upload_post import UploadPost
    adapter = UploadPost(cfg)
    ok, why = adapter.readiness("youtube")
    if not ok:
        return False, why
    fn = getattr(adapter, "verify", None)
    if fn is None:
        return True, why
    try:
        return fn("youtube")
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:300]}"


# ----------------------------------------------------------------- handler --
class Handler(BaseHTTPRequestHandler):
    server_version = "scc"

    # -- plumbing ---------------------------------------------------------
    def _host_ok(self) -> bool:
        port = self.server.server_address[1]
        return self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _json(self, obj: Any, code: int = 200) -> None:
        body = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, data: bytes, ctype: str, cache: str = "no-store",
               extra: dict[str, str] | None = None) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise Fail("request too large", 413)
        body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        if not isinstance(body, dict):
            raise Fail("JSON body must be an object")
        return body

    def log_message(self, *args: object) -> None:
        pass

    # -- GET ----------------------------------------------------------------
    def do_GET(self) -> None:
        try:
            if not self._host_ok():
                return self._json({"error": "bad host"}, 403)
            url = urlsplit(self.path)
            path = url.path
            if path in ("/", "/index.html"):
                html = (STATIC / "index.html").read_text().replace("__SCC_TOKEN__", TOKEN)
                return self._bytes(html.encode(), "text/html; charset=utf-8",
                                   extra={"Content-Security-Policy": CSP})
            if path == "/api/shorts-inventory":
                from .inventory import listing
                con = db.connect()
                try:
                    return self._json({"items": listing(con)})
                finally:
                    con.close()
            if path == "/api/state":
                return self._json(state())
            if path == "/api/slots":
                return self._json(slots_view(parse_qs(url.query)))
            if m := re.fullmatch(r"/static/([A-Za-z0-9._-]+)", path):
                return self._static(m.group(1))
            if m := re.fullmatch(r"/media/([0-9a-f]{10})", path):
                return self._media(m.group(1))
            if m := re.fullmatch(r"/thumb/([0-9a-f]{10})\.jpg", path):
                return self._thumb(m.group(1))
            self._json({"error": "not found"}, 404)
        except Fail as e:
            self._json({"error": e.message, **({"issues": e.issues} if e.issues else {})}, e.code)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def _static(self, name: str) -> None:
        f = (STATIC / name).resolve()
        if f.parent != STATIC.resolve() or not f.is_file():
            return self._json({"error": "not found"}, 404)
        ctype = CONTENT_TYPES.get(f.suffix.lower(), "application/octet-stream")
        self._bytes(f.read_bytes(), ctype)

    def _post_video(self, post_id: str) -> tuple[dict[str, Any], Path] | None:
        con = db.connect()
        try:
            post = db.get_post(con, post_id)
        finally:
            con.close()
        if not post or post["id"] != post_id or not Path(post["video_path"]).exists():
            self._json({"error": "not found"}, 404)
            return None
        return post, Path(post["video_path"])

    def _thumb(self, post_id: str) -> None:
        found = self._post_video(post_id)
        if not found:
            return
        post, video = found
        key = post["sha256"] or hashlib.sha256(str(video).encode()).hexdigest()
        out = config.DATA_DIR / "thumbs" / f"{key}.jpg"
        if not out.exists():
            with _thumb_lock:  # one ffmpeg at a time; others reuse the result
                if not out.exists() and not media.thumbnail(video, out):
                    return self._json({"error": "could not render a poster frame"}, 404)
        self._bytes(out.read_bytes(), "image/jpeg", cache="private, max-age=86400")

    def _media(self, post_id: str) -> None:
        found = self._post_video(post_id)
        if not found:
            return
        path = found[1]
        size = path.stat().st_size
        start, end = 0, size - 1
        rng = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
        if rng:
            if rng.group(1):
                start = int(rng.group(1))
                end = int(rng.group(2)) if rng.group(2) else size - 1
            elif rng.group(2):
                start = max(0, size - int(rng.group(2)))
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        with path.open("rb") as fh:
            fh.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    chunk = fh.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    # -- POST ---------------------------------------------------------------
    def do_POST(self) -> None:
        if not self._host_ok() or not hmac.compare_digest(
                self.headers.get("X-SCC-Token") or "", TOKEN):
            return self._json({"error": "forbidden"}, 403)
        body: dict[str, Any] = {}
        try:
            body = self._body()
            result = self._route_post(urlsplit(self.path).path, body)
            if result is None:
                return self._json({"error": "not found"}, 404)
            return self._json(result)
        except Fail as e:
            return self._json({"error": e.message, **({"issues": e.issues} if e.issues else {})},
                              e.code)
        except (ValueError, KeyError) as e:  # includes bad JSON
            return self._json({"error": scrub(str(e), self._secrets(body))}, 400)
        except Exception as e:
            return self._json({"error": scrub(f"{type(e).__name__}: {e}", self._secrets(body))}, 500)

    @staticmethod
    def _secrets(body: dict[str, Any]) -> list[str]:
        return [v for k, v in body.items() if k in SECRET_FIELDS and isinstance(v, str)]

    def _route_post(self, path: str, body: dict[str, Any]) -> dict[str, Any] | None:
        if path.startswith(("/api/connect/", "/api/disconnect")):
            return connect_post(path, body)
        con = db.connect()
        try:
            return self._route_data(con, path, body)
        finally:
            con.close()

    def _route_data(self, con, path: str, body: dict[str, Any]) -> dict[str, Any] | None:
        cfg = config.load()
        if m := re.fullmatch(r"/api/shorts-inventory/([a-f0-9]{64})/prepare", path):
            from .inventory import prepare_for_social
            return prepare_for_social(con, m.group(1))
        if path == "/api/import":
            folder = Path(cfg.get("general", "shorts_folder")).expanduser()
            if not folder.is_dir():
                raise Fail(f"shorts folder not found: {folder}")
            r = import_folder(con, cfg, folder, list(config.PLATFORMS))
            for pid in r["added"] if cfg.get("general", "auto_captions") else []:
                # new shorts get AI caption drafts automatically
                try:
                    jobs.enqueue_captions(pid)
                except ValueError:
                    pass
            return {"ok": True, "imported": len(r["added"]), "updated": len(r["updated"])}
        if path == "/api/run":
            try:
                jobs.background("_scheduler", "running scheduler", jobs.publish_job)
            except ValueError as e:
                raise Fail(str(e), 409) from e
            return {"ok": True}
        if path == "/api/bulk":
            return {"ok": True, "results": op_bulk(con, body)}
        if path == "/api/schedule/auto":
            return op_schedule_auto(con, cfg, body)
        if path == "/api/schedule/clear":
            return op_schedule_clear(con, body)
        if path == "/api/settings":
            return {"ok": True, "settings": apply_settings(body)}
        if path == "/api/scheduler/install":
            return run_script("install-scheduler.sh")
        if path == "/api/scheduler/uninstall":
            return run_script("uninstall-scheduler.sh")
        m = re.fullmatch(r"/api/posts/([0-9a-f]{10})(?:/(\w+))?", path)
        if not m:
            return None
        pid, action = m.group(1), m.group(2) or "update"
        def post_view() -> dict[str, Any]:
            return next(p for p in present_posts(con, fill_durations=0) if p["id"] == pid)

        if action == "update":
            unapproved = op_update(con, cfg, pid, body)
            out = {"ok": True, "post": post_view()}
            if unapproved:
                out["unapproved"] = True
            return out
        if action == "approve":
            op_approve(con, pid)
            return {"ok": True, "post": post_view()}
        if action == "unapprove":
            op_unapprove(con, pid)
            return {"ok": True, "post": post_view()}
        if action == "publish":
            op_publish(con, pid, body)
            return {"ok": True}
        if action == "captions":
            op_captions(con, pid)
            return {"ok": True}
        if action == "retry":
            op_retry(con, pid, body)
            return {"ok": True, "post": post_view()}
        if action == "remove":
            op_remove(con, pid)
            return {"ok": True}
        return None


def make_server(port: int = 8787) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    return httpd


def serve(port: int = 8787, open_browser: bool = True) -> None:
    httpd = make_server(port)
    url = f"http://127.0.0.1:{port}/"
    print(f"Social Command Center → {url}  (Ctrl-C to stop)")
    jobs.start_scheduler()
    threading.Thread(target=jobs.backfill_durations, name="scc-durations", daemon=True).start()
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        jobs.stop_scheduler()

