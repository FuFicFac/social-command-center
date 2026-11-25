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

import httpx

from .. import media, oauth, secrets
from ..config import Config
from .base import (
    Ambiguous,
    Logger,
    NotSubmitted,
    Result,
    check,
    client,
    commit,
    file_body,
    pre,
    pre_resp,
)

LEGACY_TOKEN = "youtube.refresh_token"


def token_name(channel: str | None) -> str:
    return f"{LEGACY_TOKEN}.{channel}" if channel else LEGACY_TOKEN


def channel_info(item: dict[str, Any]) -> dict[str, str]:
    snippet = item.get("snippet") or {}
    return {"id": str(item["id"]), "title": snippet.get("title") or "",
            "handle": snippet.get("customUrl") or ""}


AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"
SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"
UPLOAD = "https://www.googleapis.com/upload/youtube/v3/videos"
VIDEOS = "https://www.googleapis.com/youtube/v3/videos"
CHANNELS = "https://www.googleapis.com/youtube/v3/channels"


class YouTube:
    name = "youtube"

    def __init__(self, cfg: Config, channel: str | None = None):
        self.cfg = cfg
        # Each channel's sign-in has its own Keychain item, so signing in to a second
        # channel can never replace the first. None = the original single sign-in.
        self.channel = channel
        self.token_name = token_name(channel)
        self._token: tuple[str, float] | None = None

    # ---- auth -------------------------------------------------------------
    def readiness(self, platform: str) -> tuple[bool, str]:
        if not self.cfg.get("youtube", "client_id"):
            return False, "set youtube.client_id — see SETUP.md § YouTube"
        if not secrets.get("youtube.client_secret"):
            return False, "run: scc secret set youtube.client_secret"
        if not secrets.get(self.token_name):
            return False, "not signed in — run: scc connect youtube"
        return True, "ready (uploads stay Private until the API audit clears)"

    def connect(self, port: int = 8789) -> dict[str, str]:
        """Sign in and store the refresh token under the channel it belongs to.
        Returns {"id", "title", "handle"} for that channel."""
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
            ch = http.get(CHANNELS, params={"part": "snippet", "mine": "true"},
                          headers={"Authorization": f"Bearer {tok['access_token']}"}).json()
        items = ch.get("items") or []
        if not items or not items[0].get("id"):
            raise RuntimeError("That Google account has no YouTube channel; nothing was saved.")
        channel = channel_info(items[0])
        secrets.put(token_name(channel["id"]), tok["refresh_token"])
        return channel

    def _access_token(self) -> str:
        if self._token and self._token[1] > time.time() + 60:
            return self._token[0]
        try:
            with client(30) as http:
                tok = pre(lambda: http.post(TOKEN, data={
                    "client_id": self.cfg.get("youtube", "client_id"),
                    "client_secret": secrets.require("youtube.client_secret"),
                    "refresh_token": secrets.require(self.token_name),
                    "grant_type": "refresh_token",
                }), "google token refresh")
        except secrets.MissingSecret as e:
            raise NotSubmitted(f"YouTube is not connected ({e}) — run: scc connect youtube") from e
        except NotSubmitted as e:
            raise NotSubmitted(f"YouTube sign-in failed — reconnect with: scc connect youtube "
                               f"({e})") from e
        if not tok.get("access_token"):
            raise NotSubmitted("YouTube sign-in failed — reconnect with: scc connect youtube")
        self._token = (tok["access_token"], time.time() + int(tok.get("expires_in", 3600)))
        return self._token[0]

    def current_channel(self) -> dict[str, str] | None:
        """The channel this sign-in controls right now (None if it has none)."""
        try:
            with client(20) as http:
                r = http.get(CHANNELS, params={"part": "snippet", "mine": "true"},
                             headers={"Authorization": f"Bearer {self._access_token()}"})
        except httpx.HTTPError as e:
            raise NotSubmitted(f"YouTube is unreachable ({e.__class__.__name__})") from e
        if r.status_code >= 400:
            raise NotSubmitted(f"YouTube API HTTP {r.status_code}")
        items = r.json().get("items") or []
        return channel_info(items[0]) if items and items[0].get("id") else None

    def verify(self, platform: str) -> tuple[bool, str]:
        try:
            with client(20) as http:
                r = http.get(CHANNELS, params={"part": "snippet", "mine": "true"},
                             headers={"Authorization": f"Bearer {self._access_token()}"})
            if r.status_code >= 400:
                return False, f"YouTube API HTTP {r.status_code}: {r.text[:200]}"
            items = r.json().get("items") or []
            if not items:
                return False, "signed in, but this Google account has no YouTube channel"
            return True, f"channel '{items[0]['snippet']['title']}'"
        except Exception as e:  # never raise, never leak
            return False, str(e) if isinstance(e, NotSubmitted) else f"{e.__class__.__name__}"

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
            init = pre_resp(lambda: http.post(
                UPLOAD, params={"uploadType": "resumable", "part": "snippet,status"},
                headers={**auth, "Content-Type": "application/json; charset=UTF-8",
                         "X-Upload-Content-Length": str(size),
                         "X-Upload-Content-Type": "video/mp4"},
                content=json.dumps(body)), "youtube upload session")
            session = init.headers.get("Location")
            if not session:
                raise NotSubmitted("youtube gave no upload session URL")
            log("info", f"youtube: uploading {video.name} ({size / 2**20:.0f} MB)")
            # The final PUT is the commit. Content-Length is explicit so httpx streams the
            # file from disk instead of chunk-encoding it.
            resp = commit(lambda: http.put(session, headers={**auth, "Content-Type": "video/mp4",
                                                             "Content-Length": str(size)},
                                           content=file_body(video)), "youtube upload")
        if resp.status_code not in (200, 201):  # e.g. 308: bytes landed but the video isn't created
            raise Ambiguous(f"youtube upload: unexpected HTTP {resp.status_code} on final PUT — "
                            "check YouTube Studio before retrying")
        try:
            vid = resp.json()
            vid_id = vid["id"]
        except (ValueError, KeyError, TypeError) as e:
            raise Ambiguous("youtube upload finished but returned no video id — "
                            "check YouTube Studio before retrying") from e
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
        pd = items[0].get("processingDetails", {})
        meta = {**delivery["meta"], "privacy": st.get("privacyStatus"),
                "upload_status": st.get("uploadStatus")}
        up = st.get("uploadStatus")
        if up in ("rejected", "failed", "deleted") or pd.get("processingStatus") in ("failed", "terminated"):
            why = (st.get("rejectionReason") or st.get("failureReason")
                   or pd.get("processingFailureReason") or pd.get("processingStatus"))
            return Result("failed", remote_id=vid, url=delivery.get("url"),
                          error=f"YouTube {up}: {why}", meta=meta)
        if up == "processed":
            locked = st.get("privacyStatus") != self.cfg.get("youtube", "privacy")
            return Result("private" if locked else "published", remote_id=vid,
                          url=delivery.get("url"), meta=meta)
        return None
