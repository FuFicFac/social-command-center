"""A stand-in for the Upload-Post API, for tests and the multi-account demonstration.

It speaks the same wire shapes the real adapter uses — profile listing, profile creation,
connect links, async uploads and status lookups — but keeps everything in memory. Nothing
leaves the machine. Install it with `install(fake, monkeypatch_or_None)`.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

FIELD = re.compile(rb'name="([^"]+)"\r\n\r\n([^\r]*)\r\n')


class FakeUploadPost:
    def __init__(self, limit: int = 5):
        self.limit = limit
        self.profiles: dict[str, dict[str, Any]] = {}
        self.jobs: dict[str, dict[str, Any]] = {}      # request_id -> job
        self.uploads: list[dict[str, Any]] = []        # one entry per accepted upload request
        self.failing: dict[tuple[str, str], str] = {}  # (profile, platform) -> error text
        self.posted: list[tuple[str, str, str]] = []   # (profile, platform, account id) that went live
        self.counter = 7000

    # ------------------------------------------------------------- set-up --
    def connect(self, profile: str, platform: str, account_id: str, handle: str,
                name: str | None = None, reauth: bool = False) -> None:
        """Connect (or replace) the account a profile holds for a platform."""
        self.profiles.setdefault(profile, {})[platform] = {
            "username": account_id, "handle": handle, "display_name": name or handle.lstrip("@"),
            "reauth_required": reauth}

    def disconnect(self, profile: str, platform: str) -> None:
        self.profiles.get(profile, {}).pop(platform, None)

    def listing(self) -> dict[str, Any]:
        """Like the real list endpoint: handles and names, but no platform IDs."""
        def brief(acct: Any) -> Any:
            return {k: v for k, v in acct.items() if k != "username"} if acct else ""
        return {"success": True, "plan": "basic", "limit": self.limit, "profiles": [
            {"username": name, "display_name": name, "social_accounts": {
                p: brief(social.get(p)) for p in ("tiktok", "instagram", "youtube", "facebook",
                                                  "linkedin", "x")}}
            for name, social in self.profiles.items()]}

    def detail(self, name: str) -> httpx.Response:
        """Like the real single-profile endpoint: `username` is the platform's own ID."""
        if name not in self.profiles:
            return httpx.Response(404, json={"success": False, "message": "Profile not found"})
        return httpx.Response(200, json={"success": True, "profile": {
            "username": name, "display_name": name, "social_accounts": dict(self.profiles[name])}})

    # ---------------------------------------------------------------- wire --
    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if request.headers.get("authorization") != "Apikey TEST-KEY":
            return httpx.Response(401, json={"message": "Invalid API key"})
        if path.endswith("/uploadposts/users") and method == "GET":
            return httpx.Response(200, json=self.listing())
        if path.endswith("/uploadposts/users") and method == "POST":
            name = _json(request).get("username")
            if name in self.profiles:
                return httpx.Response(409, json={"message": "Username already in use"})
            if len(self.profiles) >= self.limit:
                return httpx.Response(403, json={
                    "error_code": "PROFILE_LIMIT_REACHED", "profile_limit": self.limit,
                    "current_profiles": len(self.profiles)})
            self.profiles[name] = {}
            return httpx.Response(200, json={"success": True, "profile": {"username": name}})
        if "/uploadposts/users/" in path and method == "GET":
            return self.detail(path.rsplit("/", 1)[1])
        if path.endswith("/uploadposts/users/generate-jwt"):
            name = _json(request).get("username")
            return httpx.Response(200, json={
                "success": True, "duration": "48h",
                "access_url": f"https://app.upload-post.com/connect/{name}?token=simulated"})
        if path.endswith("/upload") and method == "POST":
            return self._upload(request)
        if path.endswith("/uploadposts/status"):
            job = self.jobs.get(request.url.params.get("request_id", ""))
            if not job:
                return httpx.Response(404, json={"status": "not_found"})
            return httpx.Response(200, json={"request_id": job["request_id"],
                                             "status": "completed", "results": job["results"]})
        return httpx.Response(404, json={"message": f"no route {method} {path}"})

    def _upload(self, request: httpx.Request) -> httpx.Response:
        fields: dict[str, list[str]] = {}
        for key, value in FIELD.findall(request.content):
            fields.setdefault(key.decode(), []).append(value.decode())
        profile = fields.get("user", [""])[0]
        platforms = fields.get("platform[]", [])
        request_id = request.headers.get("idempotency-key") or fields["request_id"][0]
        if request_id in self.jobs:  # the same request never posts twice
            return httpx.Response(200, json={"success": True, "request_id": request_id})
        if profile not in self.profiles:
            return httpx.Response(404, json={"success": False, "message": "Profile not found"})
        results = []
        for platform in platforms:
            acct = self.profiles[profile].get(platform)
            if not acct:
                results.append({"platform": platform, "skipped": True,
                                "skip_reason": "profile_platform_not_configured"})
            elif (profile, platform) in self.failing:
                results.append({"platform": platform, "success": False,
                                "error": self.failing[(profile, platform)]})
            else:
                self.counter += 1
                self.posted.append((profile, platform, acct["username"]))
                results.append({"platform": platform, "success": True,
                                "platform_post_id": str(self.counter),
                                "post_url": _permalink(platform, acct["handle"], self.counter)})
        self.jobs[request_id] = {"request_id": request_id, "results": results}
        self.uploads.append({"profile": profile, "platforms": platforms, "request_id": request_id,
                             "title": fields.get("title", [""])[0]})
        return httpx.Response(200, json={"success": True, "request_id": request_id,
                                         "total_platforms": len(platforms),
                                         "message": "Upload initiated"})


def _json(request: httpx.Request) -> dict[str, Any]:
    import json
    try:
        data = json.loads(request.content or b"{}")
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def _permalink(platform: str, handle: str, n: int) -> str:
    h = handle.lstrip("@")
    return {"tiktok": f"https://www.tiktok.com/@{h}/video/{n}",
            "instagram": f"https://www.instagram.com/reel/SIM{n}/",
            "youtube": f"https://youtube.com/shorts/SIM{n}",
            "x": f"https://x.com/{h}/status/{n}"}.get(platform, f"https://example.test/{platform}/{n}")


def install(fake: FakeUploadPost, monkeypatch: Any = None) -> None:
    """Point every adapter's HTTP client at the fake and supply a throwaway API key.
    With no monkeypatch (a script), the modules are patched for the life of the process."""
    from scc import secrets
    from scc.adapters import base, upload_post, youtube

    def fake_client(timeout: float = 60) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(fake), follow_redirects=False)

    store = {"upload_post.api_key": "TEST-KEY"}

    def require(name: str) -> str:
        if name not in store:
            raise secrets.MissingSecret(f"Missing Keychain secret '{name}'")
        return store[name]

    patches = [(mod, "client", fake_client) for mod in (base, upload_post, youtube)]
    patches += [(secrets, "get", store.get), (secrets, "put", store.__setitem__),
                (secrets, "require", require), (secrets, "delete", lambda n: store.pop(n, None))]
    for target, name, value in patches:
        if monkeypatch is not None:
            monkeypatch.setattr(target, name, value)
        else:
            setattr(target, name, value)
    fake.store = store
