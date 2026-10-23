"""Adapter contract shared by every route.

The one rule that matters: never double-post. Each adapter knows the single
"commit" request that makes a post real on its platform. Failures before that
request (or a clean 4xx rejection of it) raise NotSubmitted — safe to retry.
A timeout or 5xx *during* the commit raises Ambiguous — a human checks first.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx

Logger = Callable[[str, str], None]  # (level, message)


@dataclass
class Result:
    status: str  # published | processing | failed
    remote_id: str | None = None
    url: str | None = None
    error: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


class NotSubmitted(RuntimeError):
    """Nothing went live. Safe to retry."""


class Ambiguous(RuntimeError):
    """The commit request may or may not have landed. Do not auto-retry."""


class Adapter(Protocol):
    name: str

    def readiness(self, platform: str) -> tuple[bool, str]: ...

    def verify(self, platform: str) -> tuple[bool, str]: ...

    def publish(self, post: dict[str, Any], platforms: list[str], video: Path,
                log: Logger) -> dict[str, Result]: ...

    def poll(self, post: dict[str, Any], platform: str,
             delivery: dict[str, Any]) -> Result | None: ...


def check(resp: httpx.Response, what: str) -> dict[str, Any]:
    """Raise NotSubmitted on any non-2xx in a pre-commit step; return JSON."""
    if resp.status_code >= 400:
        raise NotSubmitted(f"{what}: HTTP {resp.status_code} {_short(resp)}")
    try:
        return resp.json()
    except ValueError:
        return {}


def pre_resp(send: Callable[[], httpx.Response], what: str) -> httpx.Response:
    """A pre-commit request: any transport error or non-2xx means nothing went live."""
    try:
        resp = send()
    except httpx.HTTPError as e:
        raise NotSubmitted(f"{what}: {e.__class__.__name__} before anything was published") from e
    check(resp, what)
    return resp


def pre(send: Callable[[], httpx.Response], what: str) -> dict[str, Any]:
    """Like pre_resp, but returns the parsed JSON body."""
    return check(pre_resp(send, what), what)


def commit(send: Callable[[], httpx.Response], what: str) -> httpx.Response:
    """Run the request that makes a post real, classifying failures safely."""
    try:
        resp = send()
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:  # never reached the server
        raise NotSubmitted(f"{what}: could not connect ({e.__class__.__name__})") from e
    except httpx.TransportError as e:  # timeout/reset/protocol error after bytes may have left
        raise Ambiguous(f"{what}: connection dropped during publish ({e.__class__.__name__})") from e
    if resp.status_code >= 500:
        raise Ambiguous(f"{what}: HTTP {resp.status_code} during publish {_short(resp)}")
    if resp.status_code >= 400:
        raise NotSubmitted(f"{what}: HTTP {resp.status_code} {_short(resp)}")
    return resp


def file_body(path: Path, chunk: int = 1 << 20) -> Iterator[bytes]:
    """Stream a file in 1 MB pieces. Pair with an explicit Content-Length header:
    httpx would otherwise iterate a binary file object line by line and send it
    with chunked transfer-encoding, which upload endpoints often reject."""
    with path.open("rb") as fh:
        while block := fh.read(chunk):
            yield block


def _short(resp: httpx.Response) -> str:
    return resp.text[:400].replace("\n", " ")


def client(timeout: float = 60) -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(timeout, read=600, write=600), follow_redirects=False)
