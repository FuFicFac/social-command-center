"""Local dashboard. Binds to 127.0.0.1 only; every write needs the per-launch token."""

from __future__ import annotations

import json
import re
import secrets as pysecrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import captions, config, db, engine, media
from .adapters import for_route

STATIC = Path(__file__).parent / "static"
TOKEN = pysecrets.token_urlsafe(24)
JOBS: dict[str, str] = {}  # post_id -> running job label
_jobs_lock = threading.Lock()


def state() -> dict[str, Any]:
    cfg = config.load()
    con = db.connect()
    platforms = {}
    for p in config.PLATFORMS:
        route = cfg.route(p)
        ok, why = for_route(cfg, p, route).readiness(p)
        platforms[p] = {"route": route, "ready": ok, "detail": why}
    return {"posts": db.list_posts(con), "platforms": platforms, "jobs": dict(JOBS),
            "events": db.recent_events(con, 40), "timezone": cfg.get("general", "timezone")}


def background(post_id: str, label: str, fn) -> None:
    with _jobs_lock:
        if post_id in JOBS:
            raise ValueError(f"already {JOBS[post_id]}")
        JOBS[post_id] = label

    def run() -> None:
        con = db.connect()
        try:
            fn(con)
        except Exception as e:
            db.log(con, "error", f"{label} failed: {e}", post_id)
        finally:
            with _jobs_lock:
                JOBS.pop(post_id, None)

    threading.Thread(target=run, daemon=True).start()


def _caption_job(post_id: str):
    def go(con):
        post = db.get_post(con, post_id)
        text = captions.transcribe(Path(post["video_path"]), post["sha256"] or post_id)
        db.update_post(con, post_id, **captions.to_post_fields(captions.draft(text, post["title"])))
        db.log(con, "info", "AI caption draft written", post_id)
    return go


def _publish_job(con):
    import time
    for _ in range(60):  # wait out a concurrent launchd pass rather than skipping
        if engine.run_once(con, config.load()) != "another run is in progress":
            return
        time.sleep(5)


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

    def _body(self) -> dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def log_message(self, *args: object) -> None:
        pass

    # -- GET ----------------------------------------------------------------
    def do_GET(self) -> None:
        if not self._host_ok():
            return self._json({"error": "bad host"}, 403)
        if self.path in ("/", "/index.html"):
            html = (STATIC / "index.html").read_text().replace("__SCC_TOKEN__", TOKEN)
            body = html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                             "script-src 'self' 'unsafe-inline'; media-src 'self'; img-src 'self' data:")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/api/state":
            return self._json(state())
        m = re.fullmatch(r"/media/([0-9a-f]{10})", self.path)
        if m:
            return self._media(m.group(1))
        self._json({"error": "not found"}, 404)

    def _media(self, post_id: str) -> None:
        post = db.get_post(db.connect(), post_id)
        if not post or post["id"] != post_id or not Path(post["video_path"]).exists():
            return self._json({"error": "not found"}, 404)
        path = Path(post["video_path"])
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
        if not self._host_ok() or self.headers.get("X-SCC-Token") != TOKEN:
            return self._json({"error": "forbidden"}, 403)
        try:
            return self._route_post(self._body())
        except (ValueError, KeyError) as e:
            return self._json({"error": str(e)}, 400)

    def _route_post(self, body: dict[str, Any]) -> None:
        con = db.connect()
        cfg = config.load()
        if self.path == "/api/import":
            from .cli import _add
            folder = Path(cfg.get("general", "shorts_folder"))
            n = sum(1 for f in sorted(folder.glob("*.mp4"))
                    if _add(con, cfg, f, None, None, list(config.PLATFORMS), None))
            return self._json({"ok": True, "imported": n})
        if self.path == "/api/run":
            background("_scheduler", "running scheduler", _publish_job)
            return self._json({"ok": True})
        m = re.fullmatch(r"/api/posts/([0-9a-f]{10})(?:/(\w+))?", self.path)
        if not m:
            return self._json({"error": "not found"}, 404)
        post = db.get_post(con, m.group(1))
        if not post or post["id"] != m.group(1):
            return self._json({"error": "no such post"}, 404)
        action = m.group(2) or "update"
        pid = post["id"]

        if action == "update":
            fields: dict[str, Any] = {}
            for k in ("title", "caption"):
                if k in body:
                    fields[k] = str(body[k])
            if "overrides" in body:
                fields["overrides"] = {
                    p: {k: str(v) for k, v in o.items() if k in ("title", "caption") and v}
                    for p, o in body["overrides"].items() if p in config.PLATFORMS}
            if "scheduled_at" in body:
                from .cli import parse_when
                fields["scheduled_at"] = parse_when(body["scheduled_at"] or None, cfg)
            if post["approved_at"] and {"title", "caption", "overrides"} & fields.keys():
                fields["approved_at"] = None
            if fields:
                db.update_post(con, pid, **fields)
            if "targets" in body:
                db.set_targets(con, pid, [p for p in body["targets"] if p in config.PLATFORMS])
            return self._json({"ok": True, "post": db.get_post(con, pid)})
        if action == "approve":
            probe = media.probe(post["video_path"])
            issues = [f"{p}: {x}" for p, d in post["deliveries"].items()
                      if d["status"] in ("pending", "failed")
                      for x in media.preflight(post, p, probe)]
            if issues:
                return self._json({"error": "fix first", "issues": issues}, 400)
            db.update_post(con, pid, approved_at=db.now())
            db.log(con, "info", "approved in dashboard", pid)
            return self._json({"ok": True})
        if action == "unapprove":
            db.update_post(con, pid, approved_at=None)
            return self._json({"ok": True})
        if action == "publish":
            if body.get("confirm") != "post":
                return self._json({"error": "confirmation required"}, 400)
            for p, d in post["deliveries"].items():
                if d["status"] == "failed":
                    db.update_delivery(con, pid, p, status="pending", attempts=0)
            db.update_post(con, pid, approved_at=db.now(), scheduled_at=None)
            db.log(con, "info", "publish-now from dashboard", pid)
            background(pid, "publishing", _publish_job)
            return self._json({"ok": True})
        if action == "captions":
            background(pid, "drafting captions", _caption_job(pid))
            return self._json({"ok": True})
        if action == "retry":
            for p in body.get("platforms") or []:
                if p in post["deliveries"]:
                    db.update_delivery(con, pid, p, status="pending", error=None, attempts=0,
                                       remote_id=None, url=None, meta={})
            return self._json({"ok": True})
        if action == "remove":
            if any(d["status"] in ("uploading", "processing") for d in post["deliveries"].values()):
                return self._json({"error": "still in flight"}, 409)
            con.execute("DELETE FROM posts WHERE id=?", (pid,))
            return self._json({"ok": True})
        return self._json({"error": "unknown action"}, 404)


def serve(port: int = 8787, open_browser: bool = True) -> None:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"Social Command Center → {url}  (Ctrl-C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
