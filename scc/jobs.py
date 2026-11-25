"""In-process background work for the dashboard.

- one caption worker (FIFO) so Whisper/Claude never run twice at once
- a scheduler thread that calls engine.run_once every 60 seconds
- OAuth connect jobs (they block on the user's browser, so they live in threads)
- ad-hoc named jobs (publish-now, manual scheduler pass)

Every thread opens its own SQLite connection.
"""

from __future__ import annotations

import contextlib
import queue
import threading
from datetime import UTC, datetime
from pathlib import Path

from . import accounts, captions, config, db, engine, media

PLIST = (Path.home() / "Library/LaunchAgents"
         / f"{config.load().get('general', 'label_prefix')}.social-command-center.plist")
SCHEDULER_INTERVAL = 60

_lock = threading.Lock()

# ---------------------------------------------------------------- captions --
_queue: queue.Queue[str] = queue.Queue()
_queued: list[str] = []          # FIFO mirror of the queue, for display
_running: str | None = None
_worker: threading.Thread | None = None


def _caption_one(post_id: str) -> None:
    con = db.connect()
    try:
        post = db.get_post(con, post_id)
        if not post or post["id"] != post_id:
            return
        text = captions.transcribe(Path(post["video_path"]), post["sha256"] or post_id)
        fresh = db.get_post(con, post_id)
        if not fresh:  # removed while we were thinking
            return
        db.update_post(con, post_id,
                       **captions.to_post_fields(captions.draft(text, fresh["title"])))
        db.log(con, "info", "AI caption draft written", post_id)
    except Exception as e:  # never let one bad video stop the queue
        db.log(con, "error", f"caption draft failed: {e}", post_id)
    finally:
        con.close()


def _work() -> None:
    global _running
    while True:
        pid = _queue.get()
        with _lock:
            if pid in _queued:
                _queued.remove(pid)
            _running = pid
        try:
            with contextlib.suppress(Exception):  # even a failed log must not kill the worker
                _caption_one(pid)
        finally:
            with _lock:
                _running = None


def enqueue_captions(post_id: str) -> None:
    global _worker
    with _lock:
        if post_id == _running or post_id in _queued:
            raise ValueError("already queued for captions")
        _queued.append(post_id)
        _queue.put(post_id)
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_work, name="scc-captions", daemon=True)
            _worker.start()


def caption_queue() -> dict[str, object]:
    with _lock:
        return {"running": _running, "queued": list(_queued)}


# ------------------------------------------------------------ named jobs ----
_named: dict[str, str] = {}


def background(key: str, label: str, fn) -> None:
    """Run fn(con) in a thread; one job per key at a time."""
    with _lock:
        if key in _named:
            raise ValueError(f"already {_named[key]}")
        _named[key] = label

    def run() -> None:
        con = db.connect()
        try:
            fn(con)
        except Exception as e:
            db.log(con, "error", f"{label} failed: {e}", None if key.startswith("_") else key)
        finally:
            con.close()
            with _lock:
                _named.pop(key, None)

    threading.Thread(target=run, name=f"scc-{key}", daemon=True).start()


def post_jobs() -> dict[str, str]:
    """Per-post labels for the UI."""
    with _lock:
        out = {k: v for k, v in _named.items() if not k.startswith("_")}
        for pid in _queued:
            out[pid] = "queued for captions"
        if _running:
            out[_running] = "drafting captions"
    return out


# --------------------------------------------------------------- scheduler --
_sched = {"in_process": False, "last_run": None, "last_result": None}
_sched_stop = threading.Event()
_sched_thread: threading.Thread | None = None


def scheduler_pass(con=None) -> str:
    own = con is None
    con = con or db.connect()
    try:
        result = engine.run_once(con, config.load())
    except Exception as e:
        result = f"error: {e}"
    finally:
        if own:
            con.close()
    with _lock:
        _sched["last_run"] = datetime.now(UTC).isoformat(timespec="seconds")
        _sched["last_result"] = result
    return result


def publish_job(con) -> None:
    """Publish-now / manual run: wait out a concurrent pass instead of skipping."""
    for _ in range(60):
        if scheduler_pass(con) != "another run is in progress":
            return
        _sched_stop.wait(5)


def _tick() -> None:
    _sched_stop.wait(3)  # let the server come up, then run immediately
    while not _sched_stop.is_set():
        scheduler_pass()
        _sched_stop.wait(SCHEDULER_INTERVAL)


def start_scheduler() -> None:
    global _sched_thread
    if _sched_thread and _sched_thread.is_alive():
        return
    _sched_stop.clear()
    _sched["in_process"] = True
    _sched_thread = threading.Thread(target=_tick, name="scc-scheduler", daemon=True)
    _sched_thread.start()


def stop_scheduler() -> None:
    _sched_stop.set()
    _sched["in_process"] = False


def scheduler_state() -> dict[str, object]:
    with _lock:
        return {**_sched, "launchd_installed": PLIST.exists()}


def backfill_durations() -> None:
    """Cache ffprobe durations for posts that don't have one yet."""
    con = db.connect()
    try:
        for r in con.execute("SELECT id, video_path FROM posts WHERE duration IS NULL").fetchall():
            d = media.duration(r["video_path"])
            if d is not None:
                con.execute("UPDATE posts SET duration=? WHERE id=?", (d, r["id"]))
    finally:
        con.close()


# ---------------------------------------------------------------- connect ---
CONNECT: dict[str, str] = {"youtube": "idle", "tiktok": "idle"}


def start_connect(platform: str) -> None:
    from .adapters.tiktok import TikTok
    from .adapters.youtube import YouTube
    if platform not in CONNECT:
        raise ValueError("connect jobs exist for youtube and tiktok")
    with _lock:
        if CONNECT[platform] == "waiting for browser":
            raise ValueError("already waiting for the browser sign-in")
        CONNECT[platform] = "waiting for browser"

    def run() -> None:
        try:
            cfg = config.load()
            if platform == "youtube":
                ch = YouTube(cfg).connect()
                con = db.connect()
                try:  # each channel is its own account with its own Keychain item
                    accounts.upsert(con, conn="direct", profile=ch["id"], platform="youtube",
                                    platform_id=ch["id"], handle=ch["handle"] or None,
                                    name=ch["title"])
                finally:
                    con.close()
                who = ch["title"]
            else:
                who = TikTok(cfg).connect()
            msg = f"connected: {who}"
        except Exception as e:
            msg = f"error: {str(e)[:300]}"
        with _lock:
            CONNECT[platform] = msg

    threading.Thread(target=run, name=f"scc-connect-{platform}", daemon=True).start()


def connect_state() -> dict[str, str]:
    with _lock:
        return dict(CONNECT)
