"""Upload-Post (upload-post.com) — one API call fans out to every connected account.

This is the lane for TikTok: TikTok's own API won't make a personal-use app's posts
public, and Upload-Post is an audited app. One multipart upload covers every platform
routed here, so the video is only sent once.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from .. import media, secrets
from ..config import Config
from .base import Logger, NotSubmitted, Result, client, commit

API = "https://api.upload-post.com/api"
NAMES = {"youtube": "youtube", "instagram": "instagram", "facebook": "facebook",
         "tiktok": "tiktok"}


class UploadPost:
    name = "upload_post"

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def readiness(self, platform: str) -> tuple[bool, str]:
        if not secrets.get("upload_post.api_key"):
            return False, "no API key — run: scc secret set upload_post.api_key"
        if not self.cfg.get("upload_post", "user"):
            return False, "set upload_post.user (the profile name in Upload-Post)"
        if platform == "facebook" and not self.cfg.get("upload_post", "facebook_page_id"):
            return True, "ready (uses the profile's default Facebook Page)"
        return True, "ready"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Apikey {secrets.require('upload_post.api_key')}"}

    def publish(self, post: dict[str, Any], platforms: list[str], video: Path,
                log: Logger) -> dict[str, Result]:
        request_id = f"scc-{post['id']}-{uuid.uuid4().hex[:8]}"
        form: list[tuple[str, str]] = [
            ("user", self.cfg.get("upload_post", "user")),
            ("async_upload", "true"),
            ("request_id", request_id),
            ("external_id", f"scc-{post['id']}"),
        ]
        base = media.copy_for(post, platforms[0])
        form.append(("title", base["caption"] or base["title"]))
        for p in platforms:
            form.append(("platform[]", NAMES[p]))
            c = media.copy_for(post, p)
            if p == "youtube":
                form.append(("youtube_title", c["title"]))
                form.append(("youtube_description", c["caption"]))
            else:
                # TikTok / Instagram / Facebook use "title" as the post caption.
                form.append((f"{p}_title", c["caption"] or c["title"]))
            if p == "facebook":
                form.append(("facebook_description", c["caption"]))
                page = self.cfg.get("upload_post", "facebook_page_id")
                if page:
                    form.append(("facebook_page_id", page))
        log("info", f"upload-post: sending {video.name} to {', '.join(platforms)}")
        with client() as http, video.open("rb") as fh:
            resp = commit(
                lambda: http.post(f"{API}/upload", headers={**self._headers(),
                                  "Idempotency-Key": request_id},
                                  data=form, files={"video": (video.name, fh, "video/mp4")}),
                "upload-post upload",
            )
        body = resp.json()
        if not body.get("success", True) and not body.get("request_id"):
            raise NotSubmitted(f"upload-post refused: {str(body)[:300]}")
        rid = body.get("request_id") or request_id
        if body.get("results"):  # finished synchronously
            parsed = _parse_results(body["results"])
            return {p: parsed.get(NAMES[p], Result("processing", meta={"request_id": rid}))
                    for p in platforms}
        return {p: Result("processing", meta={"request_id": rid}) for p in platforms}

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
        r = parsed.get(NAMES[platform])
        if r is None:
            if body.get("status") == "failed":
                return Result("failed", error=body.get("message") or "upload-post reported failure")
            return None
        r.meta = {**delivery["meta"], **r.meta}
        return r if r.status != "processing" else None


def _parse_results(results: Any) -> dict[str, Result]:
    items: list[tuple[str, dict[str, Any]]] = []
    if isinstance(results, dict):
        items = [(k, v) for k, v in results.items() if isinstance(v, dict)]
    elif isinstance(results, list):
        items = [(r.get("platform", ""), r) for r in results if isinstance(r, dict)]
    out: dict[str, Result] = {}
    for platform, r in items:
        status = str(r.get("status") or "").lower()
        url = r.get("url") or r.get("post_url") or r.get("platform_post_url")
        rid = r.get("post_id") or r.get("publish_id") or r.get("video_id")
        if status in ("queued", "processing", "pending", "in_progress", "retryable"):
            out[platform] = Result("processing")
        elif status == "skipped":
            out[platform] = Result("failed", error=f"skipped: {r.get('skip_reason') or r.get('message')}")
        elif r.get("success") is True or status == "completed":
            out[platform] = Result("inbox" if r.get("fallback_to_inbox") else "published",
                                   remote_id=str(rid) if rid else None, url=url)
        elif r.get("success") is False or status == "failed":
            out[platform] = Result("failed", error=str(r.get("error") or r.get("message"))[:500])
        else:
            out[platform] = Result("processing")
    return out

