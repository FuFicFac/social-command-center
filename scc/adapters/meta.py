"""Instagram Reels + Facebook Page Reels via the Meta Graph API (free, direct).

Both upload the local file straight to rupload.facebook.com, so no public media
host is needed. A Meta app in Development mode works for accounts that are
admins of the app — i.e. EJ's own Page and Instagram — before any app review.

Docs checked 2026-10-23:
  * developers.facebook.com/docs/video-api/guides/reels-publishing (Page Reels)
  * developers.facebook.com/docs/instagram-platform/instagram-api-with-facebook-login/content-publishing
  * developers.facebook.com/docs/facebook-login/guides/access-tokens/get-long-lived
A Page access token is what both flows use (Instagram Business account linked to the Page).
"""

from __future__ import annotations

import getpass
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .. import media, secrets
from ..config import Config
from .base import Ambiguous, Logger, NotSubmitted, Result, client, commit, file_body, pre, pre_resp

PERMISSIONS = ("pages_show_list, pages_read_engagement, pages_manage_posts, "
               "instagram_basic, instagram_content_publish, business_management")
POLL_SECONDS = 10
POLL_TRIES = 60  # ~10 minutes of Instagram processing


class Meta:
    name = "meta"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.v = cfg.get("meta", "graph_version")
        self.graph = f"https://graph.facebook.com/{self.v}"

    def readiness(self, platform: str) -> tuple[bool, str]:
        if not secrets.get("meta.page_token"):
            return False, "not signed in — run: scc connect meta"
        if platform == "instagram" and not self.cfg.get("meta", "ig_user_id"):
            return False, "no Instagram professional account linked to the Page"
        if not self.cfg.get("meta", "page_id"):
            return False, "no Facebook Page selected — run: scc connect meta"
        return True, "ready"

    # ---- connect ------------------------------------------------------------
    def list_pages(self, user_token: str) -> list[dict]:
        """Swap a (short-lived) user token for a long-lived one and list the Pages it can
        manage. Page tokens obtained this way do not expire. Raises RuntimeError."""
        app_id = self.cfg.get("meta", "app_id")
        if not app_id:
            raise RuntimeError("Set meta.app_id first (scc config meta.app_id <id>).")
        try:
            app_secret = secrets.require("meta.app_secret")
        except secrets.MissingSecret as e:
            raise RuntimeError(str(e)) from e
        try:
            with client(30) as http:
                ll = _graph_json(http.get(f"{self.graph}/oauth/access_token", params={
                    "grant_type": "fb_exchange_token", "client_id": app_id,
                    "client_secret": app_secret, "fb_exchange_token": user_token.strip()}),
                    "Meta rejected that token (expired? wrong app?)")
                token = ll.get("access_token")
                if not token:
                    raise RuntimeError("Meta returned no long-lived token.")
                pages = _graph_json(http.get(f"{self.graph}/me/accounts", params={
                    "fields": "id,name,access_token,instagram_business_account{id,username}",
                    "limit": "100", "access_token": token}),
                    "Meta would not list your Pages").get("data", [])
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"Could not reach Meta ({e.__class__.__name__}).") from e
        if not pages:
            raise RuntimeError("That token can't see any Facebook Pages. Tick pages_show_list "
                               "and pick your Page during consent.")
        out = []
        for p in pages:
            ig = p.get("instagram_business_account") or {}
            out.append({"id": p["id"], "name": p.get("name", ""),
                        "access_token": p.get("access_token", ""),
                        "ig_id": ig.get("id"), "ig_username": ig.get("username")})
        return out

    def use_page(self, page: dict) -> str:
        """Store the chosen Page's token + ids. The caller saves config."""
        secrets.put("meta.page_token", page["access_token"])
        self.cfg.set("meta", "page_id", page["id"])
        self.cfg.set("meta", "ig_user_id", page.get("ig_id") or "")
        ig = f"@{page['ig_username']}" if page.get("ig_username") else "— none linked —"
        return f"Facebook Page '{page['name']}', Instagram {ig}"

    def connect(self) -> str:
        print("\nIn Graph API Explorer (developers.facebook.com/tools/explorer):")
        print(f"  1. Pick your app, choose 'Get User Access Token', tick: {PERMISSIONS}")
        print("  2. Generate, approve, then copy the token.\n")
        pages = self.list_pages(getpass.getpass("Paste the user token (hidden): "))
        want = self.cfg.get("meta", "page_id")
        page = next((p for p in pages if p["id"] == want), None)
        if page is None:
            if len(pages) == 1:
                page = pages[0]
            else:
                for i, p in enumerate(pages, 1):
                    print(f"  {i}. {p['name']} (IG: {p['ig_username'] or 'no IG'})")
                page = pages[int(input("Which Page? ")) - 1]
        return self.use_page(page)

    def _token(self) -> str:
        try:
            return secrets.require("meta.page_token")
        except secrets.MissingSecret as e:
            raise NotSubmitted(f"Meta is not connected ({e}) — run: scc connect meta") from e

    def verify(self, platform: str) -> tuple[bool, str]:
        try:
            tok = secrets.get("meta.page_token")
            if not tok:
                return False, "not signed in — run: scc connect meta"
            with client(20) as http:
                if platform == "instagram":
                    ig = self.cfg.get("meta", "ig_user_id")
                    if not ig:
                        return False, "no Instagram professional account linked to the Page"
                    r = http.get(f"{self.graph}/{ig}", params={
                        "fields": "username", "access_token": tok})
                    key, label = "username", "Instagram @"
                else:
                    page = self.cfg.get("meta", "page_id")
                    if not page:
                        return False, "no Facebook Page selected — run: scc connect meta"
                    r = http.get(f"{self.graph}/{page}", params={
                        "fields": "name", "access_token": tok})
                    key, label = "name", "Facebook Page "
            if r.status_code >= 400:
                return False, _graph_error(r) + " — reconnect with: scc connect meta"
            return True, f"{label}{r.json().get(key, '?')}"
        except Exception as e:
            return False, e.__class__.__name__

    # ---- publish ------------------------------------------------------------
    def publish(self, post: dict[str, Any], platforms: list[str], video: Path,
                log: Logger) -> dict[str, Result]:
        (platform,) = platforms
        if platform == "instagram":
            return {"instagram": self._instagram(post, video, log)}
        return {"facebook": self._facebook(post, video, log)}

    def _rupload(self, http: Any, url: str, video: Path, what: str) -> None:
        """Binary upload to rupload.facebook.com. Pre-commit: nothing is public until the
        later publish call, so any failure here is NotSubmitted."""
        host = urlparse(url).hostname or ""
        if not (host == "rupload.facebook.com" or host.endswith(".facebook.com")):
            raise NotSubmitted(f"{what}: refusing to send the token to unexpected host {host!r}")
        size = video.stat().st_size
        body = pre(lambda: http.post(
            url, headers={"Authorization": f"OAuth {self._token()}", "offset": "0",
                          "file_size": str(size), "Content-Length": str(size)},
            content=file_body(video)), what)
        if body.get("success") is False:
            raise NotSubmitted(f"{what}: {str(body)[:300]}")

    def _instagram(self, post: dict[str, Any], video: Path, log: Logger) -> Result:
        ig = self.cfg.get("meta", "ig_user_id")
        c = media.copy_for(post, "instagram")
        tok = self._token()
        with client() as http:
            box = pre(lambda: http.post(f"{self.graph}/{ig}/media", data={
                "media_type": "REELS", "upload_type": "resumable", "caption": c["caption"],
                "share_to_feed": "true" if self.cfg.get("meta", "share_reel_to_feed") else "false",
                "access_token": tok,
            }), "instagram container")
            cid = box.get("id")
            if not cid:
                raise NotSubmitted(f"instagram container: no id in response {str(box)[:200]}")
            log("info", f"instagram: uploading {video.name}")
            self._rupload(http, f"https://rupload.facebook.com/ig-api-upload/{self.v}/{cid}",
                          video, "instagram upload")
            for attempt in range(POLL_TRIES):
                st = pre(lambda: http.get(f"{self.graph}/{cid}", params={
                    "fields": "status_code,status", "access_token": tok}),
                    "instagram processing status")
                code = st.get("status_code")
                if code == "FINISHED":
                    break
                if code in ("ERROR", "EXPIRED"):
                    raise NotSubmitted(f"instagram processing {code}: {st.get('status')}")
                if attempt < POLL_TRIES - 1:
                    time.sleep(POLL_SECONDS)
            else:
                raise NotSubmitted("instagram: video still processing after 10 minutes")
            resp = commit(lambda: http.post(f"{self.graph}/{ig}/media_publish", data={
                "creation_id": cid, "access_token": tok}), "instagram publish")
            try:
                mid = resp.json().get("id")
            except ValueError:
                mid = None
            if not mid:
                raise Ambiguous(f"instagram publish returned no media id: {resp.text[:200]}")
            link = None
            try:  # the post is live; a failed permalink lookup must not turn it into an error
                link = http.get(f"{self.graph}/{mid}", params={
                    "fields": "permalink", "access_token": tok}).json().get("permalink")
            except Exception:
                log("warn", "instagram: published, but could not fetch the permalink")
        return Result("published", remote_id=mid, url=link)

    def _facebook(self, post: dict[str, Any], video: Path, log: Logger) -> Result:
        page = self.cfg.get("meta", "page_id")
        c = media.copy_for(post, "facebook")
        tok = self._token()
        with client() as http:
            start = pre(lambda: http.post(f"{self.graph}/{page}/video_reels", data={
                "upload_phase": "start", "access_token": tok}), "facebook reel start")
            vid = start.get("video_id")
            if not vid:
                raise NotSubmitted(f"facebook reel start: no video_id in {str(start)[:200]}")
            log("info", f"facebook: uploading {video.name}")
            self._rupload(http, f"https://rupload.facebook.com/video-upload/{self.v}/{vid}",
                          video, "facebook upload")
            resp = commit(lambda: http.post(f"{self.graph}/{page}/video_reels", data={
                "upload_phase": "finish", "video_id": vid, "video_state": "PUBLISHED",
                "description": c["caption"], "access_token": tok,
            }), "facebook reel publish")
            try:
                ok = resp.json().get("success", False)
            except ValueError:
                ok = False
            if not ok:
                raise NotSubmitted(f"facebook finish refused: {resp.text[:300]}")
        return Result("processing", remote_id=vid, url=f"https://www.facebook.com/reel/{vid}")

    def poll(self, post: dict[str, Any], platform: str,
             delivery: dict[str, Any]) -> Result | None:
        if platform != "facebook" or not delivery.get("remote_id"):
            return None
        with client(30) as http:
            r = pre_resp(lambda: http.get(f"{self.graph}/{delivery['remote_id']}", params={
                "fields": "status", "access_token": self._token()}), "facebook reel status")
        st = r.json().get("status", {}) or {}
        vs = st.get("video_status")
        phase = st.get("publishing_phase") or {}
        pub = phase.get("status")
        if vs == "error" or pub == "error":
            return Result("failed", remote_id=delivery["remote_id"], url=delivery.get("url"),
                          error=f"facebook processing error: {st}")
        if vs == "ready" and (pub in ("complete", None) or phase.get("publish_status") == "published"):
            return Result("published", remote_id=delivery["remote_id"], url=delivery.get("url"))
        return None


def _graph_error(resp: Any) -> str:
    try:
        e = resp.json().get("error", {})
        return f"Meta HTTP {resp.status_code}: {e.get('message', '')[:200]}".strip()
    except Exception:
        return f"Meta HTTP {resp.status_code}"


def _graph_json(resp: Any, human: str) -> dict[str, Any]:
    if resp.status_code >= 400:
        raise RuntimeError(f"{human} ({_graph_error(resp)})")
    try:
        return resp.json()
    except ValueError as e:
        raise RuntimeError(f"{human} (non-JSON reply)") from e
