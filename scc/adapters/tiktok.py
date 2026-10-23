"""TikTok Content Posting API (direct lane).

Reality (checked 2026-10-23, developers.tiktok.com content-sharing guidelines):
  * Unaudited apps can only post SELF_ONLY (private), max 5 users/day.
  * TikTok lists "a utility tool to help upload contents to the account(s) you or
    your team manages" as NOT acceptable for audit — so a personal app won't get
    public Direct Post.
So the direct lane defaults to `inbox` mode: the video lands in the TikTok app's
inbox and EJ taps Post (sounds/trends/caption from the phone). For true hands-off
public TikTok posting, route tiktok = "upload_post".
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .. import media, oauth, secrets
from ..config import Config
from .base import Logger, NotSubmitted, Result, check, client, commit

AUTH = "https://www.tiktok.com/v2/auth/authorize/"
API = "https://open.tiktokapis.com/v2"
MIN_CHUNK, MAX_CHUNK = 5 * 2**20, 64 * 2**20


def chunk_plan(size: int) -> tuple[int, int]:
    """(chunk_size, total_chunk_count) per TikTok's media transfer rules."""
    if size <= MAX_CHUNK:
        return size, 1
    chunk = 10 * 2**20
    return chunk, size // chunk  # final chunk absorbs the remainder (< 2 chunks)


class TikTok:
    name = "tiktok"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._token: tuple[str, float] | None = None

    def readiness(self, platform: str) -> tuple[bool, str]:
        if not self.cfg.get("tiktok", "client_key"):
            return False, "set tiktok.client_key — see SETUP.md § TikTok"
        if not secrets.get("tiktok.client_secret"):
            return False, "run: scc secret set tiktok.client_secret"
        if not secrets.get("tiktok.refresh_token"):
            return False, "not signed in — run: scc connect tiktok"
        mode = self.cfg.get("tiktok", "mode")
        return True, ("ready — lands in your TikTok inbox; tap Post in the app" if mode == "inbox"
                      else f"ready — direct post as {self.cfg.get('tiktok', 'privacy_level')}")

    def _scopes(self) -> str:
        return "user.info.basic," + ("video.upload" if self.cfg.get("tiktok", "mode") == "inbox"
                                     else "video.publish")

    def connect(self) -> str:
        key = self.cfg.get("tiktok", "client_key")
        csec = secrets.require("tiktok.client_secret")
        port = int(self.cfg.get("tiktok", "redirect_port"))
        redirect = f"http://localhost:{port}/callback/"
        verifier, challenge = oauth.pkce_pair(hex_challenge=True)
        q = oauth.authorize(AUTH, {
            "client_key": key, "response_type": "code", "scope": self._scopes(),
            "redirect_uri": redirect, "code_challenge": challenge,
            "code_challenge_method": "S256",
        }, port, path="/callback/", host="localhost")
        with client(30) as http:
            tok = check(http.post(f"{API}/oauth/token/", data={
                "client_key": key, "client_secret": csec, "code": q["code"],
                "grant_type": "authorization_code", "redirect_uri": redirect,
                "code_verifier": verifier,
            }), "tiktok token exchange")
            if "refresh_token" not in tok:
                raise RuntimeError(f"TikTok token exchange failed: {tok}")
            secrets.put("tiktok.refresh_token", tok["refresh_token"])
            me = http.get(f"{API}/user/info/", params={"fields": "display_name"},
                          headers={"Authorization": f"Bearer {tok['access_token']}"}).json()
        return (me.get("data", {}).get("user", {}) or {}).get("display_name", "TikTok account")

    def _access_token(self) -> str:
        if self._token and self._token[1] > time.time() + 60:
            return self._token[0]
        with client(30) as http:
            tok = check(http.post(f"{API}/oauth/token/", data={
                "client_key": self.cfg.get("tiktok", "client_key"),
                "client_secret": secrets.require("tiktok.client_secret"),
                "grant_type": "refresh_token",
                "refresh_token": secrets.require("tiktok.refresh_token"),
            }), "tiktok token refresh")
        if tok.get("refresh_token"):
            secrets.put("tiktok.refresh_token", tok["refresh_token"])
        self._token = (tok["access_token"], time.time() + int(tok.get("expires_in", 86400)))
        return self._token[0]

    def publish(self, post: dict[str, Any], platforms: list[str], video: Path,
                log: Logger) -> dict[str, Result]:
        size = video.stat().st_size
        chunk, count = chunk_plan(size)
        source = {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk,
                  "total_chunk_count": count}
        auth = {"Authorization": f"Bearer {self._access_token()}",
                "Content-Type": "application/json; charset=UTF-8"}
        inbox = self.cfg.get("tiktok", "mode") == "inbox"
        with client() as http:
            if inbox:
                url = f"{API}/post/publish/inbox/video/init/"
                payload: dict[str, Any] = {"source_info": source}
            else:
                url = f"{API}/post/publish/video/init/"
                payload = {"source_info": source, "post_info": {
                    "title": media.copy_for(post, "tiktok")["caption"],
                    "privacy_level": self.cfg.get("tiktok", "privacy_level"),
                }}
            init = check(http.post(url, headers=auth, json=payload), "tiktok init")
            err = init.get("error", {})
            if err.get("code") not in (None, "ok"):
                raise NotSubmitted(f"tiktok init: {err.get('code')} {err.get('message')}")
            data = init["data"]
            log("info", f"tiktok: uploading {video.name} in {count} chunk(s)")
            with video.open("rb") as fh:
                for i in range(count):
                    start = i * chunk
                    end = size - 1 if i == count - 1 else start + chunk - 1
                    fh.seek(start)
                    body = fh.read(end - start + 1)
                    headers = {"Content-Type": "video/mp4", "Content-Length": str(len(body)),
                               "Content-Range": f"bytes {start}-{end}/{size}"}
                    if i == count - 1:  # the final chunk completes the post
                        commit(lambda h=headers, b=body: http.put(data["upload_url"], headers=h, content=b),
                               "tiktok upload")
                    else:
                        check(http.put(data["upload_url"], headers=headers, content=body),
                              f"tiktok chunk {i + 1}/{count}")
        caption = media.copy_for(post, "tiktok")["caption"]
        return {"tiktok": Result("processing", remote_id=data["publish_id"],
                                 meta={"inbox": inbox, "caption_to_paste": caption if inbox else None})}

    def poll(self, post: dict[str, Any], platform: str,
             delivery: dict[str, Any]) -> Result | None:
        pid = delivery.get("remote_id")
        if not pid:
            return None
        with client(30) as http:
            r = http.post(f"{API}/post/publish/status/fetch/", json={"publish_id": pid},
                          headers={"Authorization": f"Bearer {self._access_token()}",
                                   "Content-Type": "application/json; charset=UTF-8"})
        if r.status_code >= 400:
            return None
        d = r.json().get("data", {})
        status = d.get("status")
        if status == "FAILED":
            return Result("failed", remote_id=pid, error=f"tiktok: {d.get('fail_reason')}",
                          meta=delivery["meta"])
        if status in ("SEND_TO_USER_INBOX", "PUBLISH_COMPLETE"):
            ids = d.get("publicaly_available_post_id") or d.get("publicly_available_post_id") or []
            url = f"https://www.tiktok.com/video/{ids[0]}" if ids else None
            if status == "SEND_TO_USER_INBOX":
                return Result("inbox", remote_id=pid, meta={
                    **delivery["meta"], "tiktok_status": status,
                    "note": "waiting in your TikTok inbox — open the app and tap Post"})
            return Result("published", remote_id=pid, url=url,
                          meta={**delivery["meta"], "tiktok_status": status})
        return None

