"""Instagram Reels + Facebook Page Reels via the Meta Graph API (free, direct).

Both upload the local file straight to rupload.facebook.com, so no public media
host is needed. A Meta app in Development mode works for accounts that are
admins of the app — i.e. EJ's own Page and Instagram — before any app review.
"""

from __future__ import annotations

import getpass
import time
from pathlib import Path
from typing import Any

from .. import media, secrets
from ..config import Config
from .base import Ambiguous, Logger, NotSubmitted, Result, check, client, commit

PERMISSIONS = ("pages_show_list, pages_read_engagement, pages_manage_posts, "
               "instagram_basic, instagram_content_publish, business_management")


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
    def connect(self) -> str:
        app_id = self.cfg.get("meta", "app_id")
        if not app_id:
            raise RuntimeError("Set meta.app_id first (scc config meta.app_id <id>).")
        app_secret = secrets.require("meta.app_secret")
        print("\nIn Graph API Explorer (developers.facebook.com/tools/explorer):")
        print(f"  1. Pick your app, choose 'Get User Access Token', tick: {PERMISSIONS}")
        print("  2. Generate, approve, then copy the token.\n")
        short = getpass.getpass("Paste the user token (hidden): ").strip()
        with client(30) as http:
            ll = check(http.get(f"{self.graph}/oauth/access_token", params={
                "grant_type": "fb_exchange_token", "client_id": app_id,
                "client_secret": app_secret, "fb_exchange_token": short,
            }), "long-lived token exchange")
            pages = check(http.get(f"{self.graph}/me/accounts", params={
                "fields": "id,name,access_token,instagram_business_account{id,username}",
                "access_token": ll["access_token"],
            }), "list pages").get("data", [])
        if not pages:
            raise RuntimeError("That token can't see any Facebook Pages. Tick pages_show_list "
                               "and pick your Page during consent.")
        want = self.cfg.get("meta", "page_id")
        page = next((p for p in pages if p["id"] == want), None)
        if page is None:
            if len(pages) == 1:
                page = pages[0]
            else:
                for i, p in enumerate(pages, 1):
                    ig = (p.get("instagram_business_account") or {}).get("username", "no IG")
                    print(f"  {i}. {p['name']} (IG: {ig})")
                page = pages[int(input("Which Page? ")) - 1]
        ig = page.get("instagram_business_account") or {}
        secrets.put("meta.page_token", page["access_token"])
        self.cfg.set("meta", "page_id", page["id"])
        self.cfg.set("meta", "ig_user_id", ig.get("id", ""))
        return f"Facebook Page '{page['name']}', Instagram @{ig.get('username', '— none linked —')}"

    def _token(self) -> str:
        return secrets.require("meta.page_token")

    # ---- publish ------------------------------------------------------------
    def publish(self, post: dict[str, Any], platforms: list[str], video: Path,
                log: Logger) -> dict[str, Result]:
        (platform,) = platforms
        if platform == "instagram":
            return {"instagram": self._instagram(post, video, log)}
        return {"facebook": self._facebook(post, video, log)}

    def _rupload(self, http: Any, url: str, video: Path, what: str) -> dict[str, Any]:
        size = video.stat().st_size
        with video.open("rb") as fh:
            return check(http.post(url, headers={"Authorization": f"OAuth {self._token()}",
                                                 "offset": "0", "file_size": str(size)},
                                   content=fh), what)

    def _instagram(self, post: dict[str, Any], video: Path, log: Logger) -> Result:
        ig = self.cfg.get("meta", "ig_user_id")
        c = media.copy_for(post, "instagram")
        tok = self._token()
        with client() as http:
            box = check(http.post(f"{self.graph}/{ig}/media", data={
                "media_type": "REELS", "upload_type": "resumable", "caption": c["caption"],
                "share_to_feed": "true" if self.cfg.get("meta", "share_reel_to_feed") else "false",
                "access_token": tok,
            }), "instagram container")
            cid = box["id"]
            log("info", f"instagram: uploading {video.name}")
            self._rupload(http, f"https://rupload.facebook.com/ig-api-upload/{self.v}/{cid}",
                          video, "instagram upload")
            for _ in range(60):  # up to ~10 min of processing
                st = http.get(f"{self.graph}/{cid}", params={
                    "fields": "status_code,status", "access_token": tok}).json()
                code = st.get("status_code")
                if code == "FINISHED":
                    break
                if code in ("ERROR", "EXPIRED"):
                    raise NotSubmitted(f"instagram processing {code}: {st.get('status')}")
                time.sleep(10)
            else:
                raise NotSubmitted("instagram: video still processing after 10 minutes")
            resp = commit(lambda: http.post(f"{self.graph}/{ig}/media_publish", data={
                "creation_id": cid, "access_token": tok}), "instagram publish")
            mid = resp.json().get("id")
            if not mid:
                raise Ambiguous(f"instagram publish returned no media id: {resp.text[:200]}")
            link = http.get(f"{self.graph}/{mid}", params={
                "fields": "permalink", "access_token": tok}).json().get("permalink")
        return Result("published", remote_id=mid, url=link)

    def _facebook(self, post: dict[str, Any], video: Path, log: Logger) -> Result:
        page = self.cfg.get("meta", "page_id")
        c = media.copy_for(post, "facebook")
        tok = self._token()
        with client() as http:
            start = check(http.post(f"{self.graph}/{page}/video_reels", data={
                "upload_phase": "start", "access_token": tok}), "facebook reel start")
            vid = start["video_id"]
            log("info", f"facebook: uploading {video.name}")
            self._rupload(http, start.get("upload_url")
                          or f"https://rupload.facebook.com/video-upload/{self.v}/{vid}",
                          video, "facebook upload")
            resp = commit(lambda: http.post(f"{self.graph}/{page}/video_reels", data={
                "upload_phase": "finish", "video_id": vid, "video_state": "PUBLISHED",
                "description": c["caption"], "access_token": tok,
            }), "facebook reel publish")
            if not resp.json().get("success", False):
                raise NotSubmitted(f"facebook finish refused: {resp.text[:300]}")
        return Result("processing", remote_id=vid, url=f"https://www.facebook.com/reel/{vid}")

    def poll(self, post: dict[str, Any], platform: str,
             delivery: dict[str, Any]) -> Result | None:
        if platform != "facebook" or not delivery.get("remote_id"):
            return None
        with client(30) as http:
            st = http.get(f"{self.graph}/{delivery['remote_id']}", params={
                "fields": "status", "access_token": self._token()}).json().get("status", {})
        vs = st.get("video_status")
        pub = (st.get("publishing_phase") or {}).get("status")
        if vs == "error" or pub == "error":
            return Result("failed", remote_id=delivery["remote_id"], url=delivery.get("url"),
                          error=f"facebook processing error: {st}")
        if vs == "ready" and pub in ("complete", None):
            return Result("published", remote_id=delivery["remote_id"], url=delivery.get("url"))
        return None
