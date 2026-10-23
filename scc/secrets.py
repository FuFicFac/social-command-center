"""Secrets live in the macOS Keychain under service 'social-command-center'.

Nothing secret is ever written to config.toml, the database, logs, or stdout.
"""

from __future__ import annotations

import keyring

SERVICE = "social-command-center"

KNOWN = {
    "youtube.client_secret": "Google OAuth client secret (Desktop app)",
    "youtube.refresh_token": "set by `scc connect youtube`",
    "meta.app_secret": "Meta app secret (App settings → Basic)",
    "meta.page_token": "set by `scc connect meta` (non-expiring Page token)",
    "tiktok.client_secret": "TikTok app client secret",
    "tiktok.refresh_token": "set by `scc connect tiktok`",
    "upload_post.api_key": "Upload-Post API key (app.upload-post.com → API Keys)",
}


def get(name: str) -> str | None:
    return keyring.get_password(SERVICE, name)


def put(name: str, value: str) -> None:
    keyring.set_password(SERVICE, name, value)


def delete(name: str) -> None:
    try:
        keyring.delete_password(SERVICE, name)
    except keyring.errors.PasswordDeleteError:
        pass


def require(name: str) -> str:
    v = get(name)
    if not v:
        hint = KNOWN.get(name, "")
        raise MissingSecret(f"Missing Keychain secret '{name}' ({hint}). Run: scc secret set {name}")
    return v


class MissingSecret(RuntimeError):
    pass
