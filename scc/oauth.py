"""Loopback OAuth helper: open the consent page, catch the redirect on 127.0.0.1."""

from __future__ import annotations

import base64
import hashlib
import html
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse


def pkce_pair(hex_challenge: bool = False) -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    digest = hashlib.sha256(verifier.encode()).digest()
    if hex_challenge:  # TikTok desktop flavour
        return verifier, digest.hex()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def authorize(auth_url: str, params: dict[str, str], port: int, path: str = "/callback",
              host: str = "127.0.0.1", timeout: int = 300) -> dict[str, str]:
    """Run the browser consent flow and return the redirect's query params.

    `params` must already include redirect_uri; a random `state` is added and verified.
    """
    state = secrets.token_urlsafe(24)
    result: dict[str, str] = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path != path:
                self.send_response(404)
                self.end_headers()
                return
            q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            ok = q.get("state") == state and "code" in q
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            msg = ("Connected. You can close this tab and go back to the terminal." if ok
                   else f"Authorization failed: {q.get('error_description') or q.get('error') or 'state mismatch'}")
            self.wfile.write(f"<h2 style='font-family:sans-serif'>{html.escape(msg)}</h2>".encode())
            if ok:
                result.update(q)
            else:
                result["error"] = q.get("error_description") or q.get("error") or "state mismatch"
            done.set()

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer((host, port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"{auth_url}?{urlencode({**params, 'state': state})}"
    print(f"\nOpening your browser to sign in. If it doesn't open, visit:\n{url}\n")
    webbrowser.open(url)
    try:
        if not done.wait(timeout):
            raise TimeoutError("No response from the browser within 5 minutes.")
    finally:
        server.shutdown()
    if "error" in result:
        raise RuntimeError(result["error"])
    return result
