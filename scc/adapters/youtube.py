"""YouTube Shorts via the YouTube Data API v3 (resumable upload).

Heads-up: until the Google Cloud project passes YouTube's API compliance audit,
YouTube force-locks API uploads to Private no matter what we ask for. The upload
still succeeds; flip it to Public in Studio, or file the audit (free) once.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .. import media, oauth, secrets
from ..config import Config
from .base import Logger, NotSubmitted, Result, check, client, commit

AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"
SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"
UPLOAD = "https://www.googleapis.com/upload/youtube/v3/videos"
VIDEOS = "https://www.googleapis.com/youtube/v3/videos"


class YouTube:
    name = "youtube"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._token: tuple[str, float] | None = None

    # ---- auth -------------------------------------------------------------
    def readiness(self, platform: str) -> tuple[bool, str]:
        if not self.cfg.get("youtube", "client_id"):
            return False, "set youtube.client_id — see SETUP.md § YouTube"
        if not secrets.get("youtube.client_secret"):
            return False, "run: scc secret set youtube.client_secret"
        if not secrets.get("youtube.refresh_token"):
            return False, "not signed in — run: scc connect youtube"
        return True, "ready (uploads stay Private until the API audit clears)"

    def connect(self, port: int = 8789) -> str:
        cid = self.cfg.get("youtube", "client_id")
        csec = secrets.require("youtube.client_secret")
        redirect = f"http://127.0.0.1:{port}/callback"
        verifier, challenge = oauth.pkce_pair()
        q = oauth.authorize(AUTH, {
            "client_id": cid, "redirect_uri": redirect, "response_type": "code",
            "scope": SCOPES, "access_type": "offline", "prompt": "consent",
            "code_challenge": challenge, "code_challenge_method": "S256",
        }, port)
        with client(30) as http:
            tok = check(http.post(TOKEN, data={
                "client_id": cid, "client_secret": csec, "code": q["code"],
                "code_verifier": verifier, "grant_type": "authorization_code",
                "redirect_uri": redirect,
            }), "google token exchange")
            if not tok.get("refresh_token"):
                raise RuntimeError("Google returned no refresh token; remove the app's access at "
                                   "myaccount.google.com/permissions and connect again.")
            secrets.put("youtube.refresh_token", tok["refresh_token"])
            ch = http.get("https://www.googleapis.com/youtube/v3/channels",
                          params={"part": "snippet", "mine": "true"},
                          headers={"Authorization": f"Bearer {tok['access_token']}"}).json()
        items = ch.get("items") or []
        return items[0]["snippet"]["title"] if items else "(no channel found on this account)"

    def _access_token(self) -> str:
        if self._token and self._token[1] > time.time() + 60:
            return self._token[0]
        with client(30) as http:
            tok = check(http.post(TOKEN, data={
                "client_id": self.cfg.get("youtube", "client_id"),
                "client_secret": secrets.require("youtube.client_secret"),
                "refresh_token": secrets.require("youtube.refresh_token"),
                "grant_type": "refresh_token",
            }), "google token refresh")
        self._token = (tok["access_token"], time.time() + int(tok.get("expires_in", 3600)))
        return self._token[0]

    # ---- publish ----------------------------------------------------------
    def publish(self, post: dict[str, Any], platforms: list[str], video: Path,
                log: Logger) -> dict[str, Result]:
        c = media.copy_for(post, "youtube")
        title, desc = c["title"], c["caption"]
        if self.cfg.get("youtube", "add_shorts_tag") and "#shorts" not in (title + desc).lower():
            desc = (desc + "\n\n#Shorts").strip()
        body = {
            "snippet": {"title": title, "description": desc,
                        "categoryId": str(self.cfg.get("youtube", "category_id"))},
            "status": {"privacyStatus": self.cfg.get("youtube", "privacy"),
                       "selfDeclaredMadeForKids": bool(self.cfg.get("youtube", "made_for_kids"))},
        }
        size = video.stat().st_size
        auth = {"Authorization": f"Bearer {self._access_token()}"}
        with client() as http:
            init = http.post(UPLOAD, params={"uploadType": "resumable", "part": "snippet,status"},
                             headers={**auth, "Content-Type": "application/json; charset=UTF-8",
                                      "X-Upload-Content-Length": str(size),
                                      "X-Upload-Content-Type": "video/mp4"},
                             content=json.dumps(body))
            check(init, "youtube upload session")
            session = init.headers.get("Location")
            if not session:
                raise NotSubmitted("youtube gave no upload session URL")
            log("info", f"youtube: uploading {video.name} ({size / 2**20:.0f} MB)")
            with video.open("rb") as fh:
                resp = commit(lambda: http.put(session, headers={**auth, "Content-Type": "video/mp4",
                                                                 "Content-Length": str(size)},
                                               content=fh), "youtube upload")
        vid = resp.json()
        vid_id = vid["id"]
        privacy = vid.get("status", {}).get("privacyStatus")
        meta = {"privacy": privacy, "upload_status": vid.get("status", {}).get("uploadStatus")}
        wanted = self.cfg.get("youtube", "privacy")
        if privacy and privacy != wanted:
            meta["note"] = (f"YouTube set this video to {privacy} (asked for {wanted}). "
                            "Unaudited API projects are locked to Private — flip it in Studio.")
            log("warn", meta["note"])
        return {"youtube": Result("processing", remote_id=vid_id,
                                  url=f"https://youtube.com/shorts/{vid_id}", meta=meta)}

    def poll(self, post: dict[str, Any], platform: str,
             delivery: dict[str, Any]) -> Result | None:
        vid = delivery.get("remote_id")
        if not vid:
            return None
        with client(30) as http:
            r = http.get(VIDEOS, params={"part": "status,processingDetails", "id": vid},
                         headers={"Authorization": f"Bearer {self._access_token()}"})
        if r.status_code >= 400:
            return None
        items = r.json().get("items") or []
        if not items:
            return None
        st = items[0].get("status", {})
        meta = {**delivery["meta"], "privacy": st.get("privacyStatus"),
                "upload_status": st.get("uploadStatus")}
        if st.get("uploadStatus") in ("rejected", "failed", "deleted"):
            return Result("failed", remote_id=vid, url=delivery.get("url"),
                          error=f"YouTube {st.get('uploadStatus')}: "
                                f"{st.get('rejectionReason') or st.get('failureReason')}", meta=meta)
        if st.get("uploadStatus") == "processed":
            locked = st.get("privacyStatus") != self.cfg.get("youtube", "privacy")
            return Result("private" if locked else "published", remote_id=vid, url=delivery.get("url"), meta=meta)
        return None
