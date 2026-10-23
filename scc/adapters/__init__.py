from __future__ import annotations

from ..config import Config
from .base import Adapter
from .dryrun import DryRun
from .meta import Meta
from .tiktok import TikTok
from .upload_post import UploadPost
from .youtube import YouTube


def for_route(cfg: Config, platform: str, route: str) -> Adapter:
    if route == "dryrun":
        return DryRun()
    if route == "upload_post":
        return UploadPost(cfg)
    if platform == "youtube":
        return YouTube(cfg)
    if platform in ("instagram", "facebook"):
        return Meta(cfg)
    if platform == "tiktok":
        return TikTok(cfg)
    raise ValueError(f"no direct adapter for {platform}")
