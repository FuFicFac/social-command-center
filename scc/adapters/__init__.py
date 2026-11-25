from __future__ import annotations

from ..config import Config
from .base import Adapter
from .dryrun import DryRun
from .meta import Meta
from .tiktok import TikTok
from .upload_post import UploadPost
from .youtube import YouTube


def for_route(cfg: Config, platform: str, route: str, profile: str | None = None) -> Adapter:
    """`profile` names the connection a selected account lives on: the Upload-Post profile,
    or the YouTube channel ID for a direct sign-in. None means the single legacy connection."""
    if route == "dryrun":
        return DryRun()
    if route == "upload_post":
        return UploadPost(cfg, profile)
    if platform == "youtube":
        return YouTube(cfg, profile)
    if platform in ("instagram", "facebook"):
        return Meta(cfg)
    if platform == "tiktok":
        return TikTok(cfg)
    raise ValueError(f"no direct adapter for {platform}")
