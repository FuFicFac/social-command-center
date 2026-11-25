"""Upload-Post (upload-post.com) — one API call fans out to every connected account.

This is the lane for TikTok: TikTok's own API won't make a personal-use app's posts
public, and Upload-Post is an audited app. One multipart upload covers every platform
routed here, so the video is only sent once.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from .. import media, secrets
from ..config import Config
from .base import Logger, NotSubmitted, Result, client, commit

API = "https://api.upload-post.com/api"


class UploadPost:
    name = "upload_post"

    def __init__(self, cfg: Config, profile: str | None = None):
        self.cfg = cfg
        # One API key covers every profile; each profile holds one account per platform.
        self.profile = profile or cfg.get("upload_post", "user")

    def readiness(self, platform: str) -> tuple[bool, str]:
        if not secrets.get("upload_post.api_key"):
            return False, "no API key — run: scc secret set upload_post.api_key"
        if not self.profile:
            return False, "set upload_post.user (the profile name in Upload-Post)"
        if platform == "facebook" and not self._facebook_page():
            return True, "ready (uses the profile's default Facebook Page)"
        return True, "ready"

    def _facebook_page(self) -> str:
        # The saved Page belongs to the original profile; other profiles use their own Page.
        if self.profile != self.cfg.get("upload_post", "user"):
            return ""
        return self.cfg.get("upload_post", "facebook_page_id") or ""

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """A read or setup request. Nothing here publishes, so any failure is NotSubmitted."""
        try:
            with client(20) as http:
                r = http.request(method, f"{API}{path}", headers=self._headers(), json=body)
        except httpx.HTTPError as e:
            raise NotSubmitted(f"Upload-Post is unreachable ({e.__class__.__name__})") from e
        try:
            data = r.json()
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        if r.status_code == 401:
            raise NotSubmitted("Upload-Post rejected the API key (401)")
        if r.status_code >= 400:
            code = data.get("error_code")
            if code == "PROFILE_LIMIT_REACHED":
                raise NotSubmitted(
                    f"Your Upload-Post plan allows {data.get('profile_limit', 'no more')} "
                    "profiles and they are all in use.")
            raise NotSubmitted(str(data.get("message") or data.get("error")
                                   or f"Upload-Post HTTP {r.status_code}")[:300]
                               + (" (404)" if r.status_code == 404 else ""))
        return data

    def profile_detail(self, name: str | None = None) -> dict[str, Any] | None:
        """One profile with each account's stable platform ID (`username`), or None if the
        profile does not exist. The list endpoint leaves those IDs out, so this is the
        call that identifies an account."""
        try:
            data = self._call("GET", f"/uploadposts/users/{quote(name or self.profile, safe='')}")
        except NotSubmitted as e:
            if "not found" in str(e).lower() or "404" in str(e):
                return None
            raise
        profile = data.get("profile")
        return profile if isinstance(profile, dict) else None

    def list_profiles(self) -> dict[str, Any]:
        """{"profiles": [{username, display_name, social_accounts}], "limit", "plan"} with
        each profile's accounts read from its detail, so they carry stable IDs."""
        data = self._call("GET", "/uploadposts/users")
        profiles = []
        for p in data.get("profiles") or []:
            detail = self.profile_detail(str(p.get("username") or "")) if p.get("username") else None
            profiles.append({**p, **({"social_accounts": detail.get("social_accounts") or {}}
                                     if detail else {})})
        return {"profiles": profiles, "limit": data.get("limit"), "plan": data.get("plan")}

    def create_profile(self, name: str) -> None:
        self._call("POST", "/uploadposts/users", {"username": name})

    def connect_link(self, platforms: list[str] | None = None) -> str:
        """A one-time Upload-Post page where you sign in to this profile's social accounts."""
        body: dict[str, Any] = {"username": self.profile}
        if platforms:
            body["platforms"] = platforms
        url = self._call("POST", "/uploadposts/users/generate-jwt", body).get("access_url")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise NotSubmitted("Upload-Post returned no connection link")
        return url

    def current_accounts(self) -> dict[str, Any] | None:
        """What this profile is connected to right now, or None if the profile is gone."""
        detail = self.profile_detail()
        return (detail.get("social_accounts") or {}) if detail else None

    def _headers(self) -> dict[str, str]:
        try:
            return {"Authorization": f"Apikey {secrets.require('upload_post.api_key')}"}
        except secrets.MissingSecret as e:
            raise NotSubmitted(f"Upload-Post is not connected ({e}) — run: scc connect "
                               "upload-post") from e

    def verify(self, platform: str | None) -> tuple[bool, str]:
        """GET /uploadposts/users lists profiles (401 = bad key); confirm ours exists."""
        try:
            user = self.profile
            if not user:
                return False, "set upload_post.user (the profile name in Upload-Post)"
            with client(20) as http:
                r = http.get(f"{API}/uploadposts/users", headers=self._headers())
            if r.status_code == 401:
                return False, "Upload-Post rejected the API key (401)"
            if r.status_code >= 400:
                return False, f"Upload-Post HTTP {r.status_code}"
            profiles = r.json().get("profiles") or []
            mine = next((p for p in profiles if p.get("username") == user), None)
            if mine is None:
                names = ", ".join(str(p.get("username")) for p in profiles) or "none"
                return False, f"no Upload-Post profile named {user!r} (found: {names})"
            connected = sorted(k for k, v in (mine.get("social_accounts") or {}).items() if v)
            if platform and platform not in connected:
                return False, f"{platform} is not connected to profile '{user}'"
            return True, f"profile '{user}' — connected: {', '.join(connected) or 'nothing yet'}"
        except Exception as e:
            return False, str(e) if isinstance(e, NotSubmitted) else e.__class__.__name__

    def publish(self, post: dict[str, Any], platforms: list[str], video: Path,
                log: Logger, *, request_id: str | None = None) -> dict[str, Result]:
        request_id = request_id or f"scc-{post['id']}-{uuid.uuid4().hex}"
        form: list[tuple[str, str]] = [
            ("user", self.profile),
            ("async_upload", "true"),
            ("request_id", request_id),
            ("external_id", f"scc-{post['id']}"),
        ]
        base = media.copy_for(post, platforms[0])
        form.append(("title", base["caption"] or base["title"]))
        for p in platforms:
            form.append(("platform[]", p))
            c = media.copy_for(post, p)
            if p == "youtube":
                form.append(("youtube_title", c["title"]))
                form.append(("youtube_description", c["caption"]))
            elif p == "linkedin":
                form.append(("linkedin_title", c["title"]))
                form.append(("linkedin_description", c["caption"]))
                form.append(("target_linkedin_page_id", "personal"))
            else:
                form.append((f"{p}_title", c["caption"] or c["title"]))
            if p == "instagram":
                form.append(("media_type", "REELS"))
                form.append(("share_to_feed",
                             "true" if self.cfg.get("meta", "share_reel_to_feed") else "false"))
            if p == "facebook":
                form.append(("facebook_media_type", "REELS"))
                form.append(("facebook_description", c["caption"]))
                page = self._facebook_page()
                if page:
                    form.append(("facebook_page_id", page))
        log("info", f"upload-post: sending {video.name} to {', '.join(platforms)} "
                    f"on profile '{self.profile}'")
        with client() as http, video.open("rb") as fh:
            resp = commit(
                lambda: http.post(f"{API}/upload", headers={**self._headers(),
                                  "Idempotency-Key": request_id},
                                  data=_as_fields(form), files={"video": (video.name, fh, "video/mp4")}),
                "upload-post upload",
            )
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        if body.get("success") is False and not body.get("request_id"):
            raise NotSubmitted(f"upload-post refused: {str(body)[:300]}")
        rid = body.get("request_id") or request_id
        parsed = _parse_results(body.get("results"))
        results = {p: parsed.get(p, Result("processing")) for p in platforms}
        for result in results.values():
            result.meta["request_id"] = rid
        return results

    def poll(self, post: dict[str, Any], platform: str,
             delivery: dict[str, Any]) -> Result | None:
        rid = delivery["meta"].get("request_id")
        if not rid:
            return None
        with client(30) as http:
            resp = http.get(f"{API}/uploadposts/status", params={"request_id": rid},
                            headers=self._headers())
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            return None
        body = resp.json()
        parsed = _parse_results(body.get("results") or [])
        r = parsed.get(platform)
        if r is None:
            if body.get("status") == "failed":
                return Result("needs_review", error=body.get("message") or
                              "Request failed without a result for this destination")
            return None
        r.meta = {**delivery["meta"], **r.meta}
        return r if r.status != "processing" else None


def _as_fields(pairs: list[tuple[str, str]]) -> dict[str, list[str]]:
    """httpx wants a mapping for multipart fields (a list of tuples is read as raw content);
    repeated keys such as platform[] become lists."""
    out: dict[str, list[str]] = {}
    for k, v in pairs:
        out.setdefault(k, []).append(v)
    return out


def _parse_results(results: Any) -> dict[str, Result]:
    items: list[tuple[str, dict[str, Any]]] = []
    if isinstance(results, dict):
        items = [(k, v) for k, v in results.items() if isinstance(v, dict)]
    elif isinstance(results, list):
        items = [(r.get("platform", ""), r) for r in results if isinstance(r, dict)]
    out: dict[str, Result] = {}
    for platform, r in items:
        status = str(r.get("status") or "").lower()
        msg = str(r.get("message") or "").strip().lower()
        url = r.get("url") or r.get("post_url") or r.get("platform_post_url")
        rid = (r.get("platform_post_id") or r.get("post_id") or r.get("publish_id")
               or r.get("video_id") or r.get("video_reel_id"))
        if r.get("skipped") or status == "skipped":
            out[platform] = Result("failed", error=f"skipped: {r.get('skip_reason') or r.get('message')}")
        elif status in ("queued", "processing", "pending", "in_progress", "retryable") \
                or (not url and not rid and msg in ("queued", "processing", "pending")):
            out[platform] = Result("processing")
        elif r.get("success") is True or status == "completed":
            out[platform] = Result("inbox" if r.get("fallback_to_inbox") else "published",
                                   remote_id=str(rid) if rid else None, url=url)
        elif r.get("success") is False or status == "failed":
            out[platform] = Result("failed", error=str(r.get("error") or r.get("message"))[:500])
        else:
            out[platform] = Result("processing")
    return out
