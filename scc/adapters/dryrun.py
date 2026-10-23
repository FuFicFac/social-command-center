from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import media
from .base import Logger, Result


class DryRun:
    """Shows exactly what would be sent. Touches no network."""

    name = "dryrun"

    def readiness(self, platform: str) -> tuple[bool, str]:
        return True, "dry run — nothing is posted"

    def verify(self, platform: str) -> tuple[bool, str]:
        return True, "dry run"

    def publish(self, post: dict[str, Any], platforms: list[str], video: Path,
                log: Logger) -> dict[str, Result]:
        out = {}
        for p in platforms:
            c = media.copy_for(post, p)
            log("info", f"[dry run] {p}: would post {video.name} | title={c['title']!r} "
                        f"| caption={c['caption'][:80]!r}")
            out[p] = Result("simulated", remote_id="dryrun", url=None,
                            meta={"dry_run": True})
        return out

    def poll(self, post: dict[str, Any], platform: str,
             delivery: dict[str, Any]) -> Result | None:
        return None
