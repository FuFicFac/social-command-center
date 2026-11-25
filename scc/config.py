"""Non-secret configuration (config.toml) and paths.

Secrets never live here — they go in the macOS Keychain (see secrets.py).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PLATFORMS = ("youtube", "instagram", "facebook", "tiktok")
ROUTES = ("direct", "upload_post", "dryrun")

ROOT = Path(os.environ.get("SCC_HOME", Path(__file__).resolve().parent.parent))
CONFIG_PATH = ROOT / "config.toml"
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "scc.sqlite3"
LOCK_PATH = DATA_DIR / "run.lock"
SHORTS_READY = Path("~/Movies/Shorts Ready")

DEFAULTS: dict[str, dict[str, Any]] = {
    # Where each platform's posts go. dryrun = log what would happen, touch nothing.
    "routing": {p: "dryrun" for p in PLATFORMS},
    "general": {"timezone": "America/Los_Angeles", "shorts_folder": str(SHORTS_READY),
                "auto_captions": True},
    # Posting slots (local time, general.timezone) — stored as comma strings in config.toml.
    "schedule": {"slots": "09:00,17:00", "days": "mon,tue,wed,thu,fri,sat,sun"},
    "youtube": {
        "uploads_paused": False,
        "client_id": "",
        "privacy": "public",
        "category_id": "28",
        "made_for_kids": False,
        "add_shorts_tag": True,
    },
    "meta": {
        "app_id": "",
        "page_id": "",
        "ig_user_id": "",
        "graph_version": "v25.0",
        "share_reel_to_feed": True,
    },
    "tiktok": {
        "client_key": "",
        # inbox  = video lands in the TikTok app's inbox; you tap Post (works without audit)
        # direct = posts straight to the profile; public only once TikTok audits the app
        "mode": "inbox",
        "privacy_level": "SELF_ONLY",
        "redirect_port": 8790,
    },
    "fini": {"auto_approve_new_shorts": False, "alist_upload_post_user": ""},
    "upload_post": {"user": "", "facebook_page_id": ""},
}


WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass
class Config:
    data: dict[str, dict[str, Any]]

    def get(self, section: str, key: str) -> Any:
        return self.data.get(section, {}).get(key, DEFAULTS.get(section, {}).get(key))

    def route(self, platform: str) -> str:
        r = self.get("routing", platform) or "dryrun"
        if r not in ROUTES:
            raise ValueError(f"routing.{platform} = {r!r}; must be one of {ROUTES}")
        return r

    def slots(self) -> list[str]:
        return _csv(self.get("schedule", "slots"))

    def days(self) -> list[str]:
        return [d for d in _csv(self.get("schedule", "days")) if d in WEEKDAYS]

    def set(self, section: str, key: str, value: Any) -> None:
        self.data.setdefault(section, {})[key] = value


def _csv(value: Any) -> list[str]:
    if isinstance(value, list | tuple):
        return [str(v).strip() for v in value if str(v).strip()]
    return [v.strip() for v in str(value or "").split(",") if v.strip()]


def load(path: Path | None = None) -> Config:
    path = path or CONFIG_PATH  # resolved at call time so tests/tools can repoint it
    merged = {s: dict(v) for s, v in DEFAULTS.items()}
    if path.exists():
        with path.open("rb") as fh:
            for section, values in tomllib.load(fh).items():
                if isinstance(values, dict):
                    merged.setdefault(section, {}).update(values)
    return Config(merged)


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int | float):
        return str(v)
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def save(cfg: Config, path: Path | None = None) -> None:
    path = path or CONFIG_PATH
    lines = [
        "# Social Command Center config. Secrets are NOT stored here (macOS Keychain).",
        "# routing values: direct | upload_post | dryrun",
        "",
    ]
    for section, values in cfg.data.items():
        lines.append(f"[{section}]")
        for k, v in values.items():
            lines.append(f"{k} = {_toml_value(v)}")
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text("\n".join(lines))
    tmp.replace(path)  # atomic: a crash mid-write can't leave a half config
