"""Video probing, hashing, per-platform copy resolution and preflight checks."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class Probe:
    duration: float
    width: int
    height: int
    size: int
    vcodec: str
    has_audio: bool

    @property
    def vertical(self) -> bool:
        return self.height > self.width


def probe(path: str | Path) -> Probe:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
         str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    info = json.loads(out)
    v = next((s for s in info["streams"] if s.get("codec_type") == "video"), {})
    return Probe(
        duration=float(info["format"].get("duration", 0)),
        width=int(v.get("width", 0)),
        height=int(v.get("height", 0)),
        size=Path(path).stat().st_size,
        vcodec=v.get("codec_name", ""),
        has_audio=any(s.get("codec_type") == "audio" for s in info["streams"]),
    )


def sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def title_from_filename(path: str | Path) -> str:
    stem = Path(path).stem
    stem = stem.split("__", 1)[-1]  # drop "project__" prefix used in Shorts Ready
    words = [w for w in stem.replace("_", "-").split("-") if w]
    while words and (words[0].lower() in {"short", "s1", "s2", "s3", "s4", "s5"} or words[0].isdigit()
                     or (words[0][:1] == "s" and words[0][1:].isdigit())):
        words.pop(0)
    text = " ".join(words) or stem
    return text[:1].upper() + text[1:]


def copy_for(post: dict[str, Any], platform: str) -> dict[str, str]:
    """Resolve the title/caption a platform will actually receive."""
    o = (post.get("overrides") or {}).get(platform, {})
    title = (o.get("title") or post.get("title") or "").strip()
    caption = (o.get("caption") or post.get("caption") or "").strip()
    return {"title": title, "caption": caption}


# (min_seconds, max_seconds, max_bytes, caption_limit, title_limit)
LIMITS = {
    "youtube": (1, 180, 256 * 2**30, 5000, 100),
    "instagram": (3, 900, 300 * 2**20, 2200, 0),
    "facebook": (3, 90, 1 * 2**30, 63206, 0),
    "tiktok": (3, 600, 4 * 2**30, 2200, 0),
}


def preflight(post: dict[str, Any], platform: str, p: Probe) -> list[str]:
    """Return blocking problems for this platform (empty list = good to go)."""
    lo, hi, max_bytes, cap_limit, title_limit = LIMITS[platform]
    problems: list[str] = []
    if not p.vertical:
        problems.append(f"video is {p.width}x{p.height}, not vertical 9:16")
    if p.duration < lo or p.duration > hi:
        problems.append(f"duration {p.duration:.1f}s outside {platform} short range {lo}-{hi}s")
    if p.size > max_bytes:
        problems.append(f"file is {p.size / 2**20:.0f} MB, over {platform} limit")
    c = copy_for(post, platform)
    if platform == "youtube":
        if not c["title"]:
            problems.append("YouTube needs a title")
        elif len(c["title"]) > title_limit:
            problems.append(f"YouTube title is {len(c['title'])} chars (max {title_limit})")
        if "<" in c["title"] or ">" in c["title"]:
            problems.append("YouTube titles can't contain < or >")
    if len(c["caption"]) > cap_limit:
        problems.append(f"caption is {len(c['caption'])} chars (max {cap_limit} on {platform})")
    if platform in ("instagram", "tiktok", "facebook") and not c["caption"]:
        problems.append(f"{platform} caption is empty")
    return problems


def post_issues(post: dict[str, Any], probe_result: Probe | None = None) -> list[str]:
    """Everything blocking approval, across the post's live (pending/failed) deliveries."""
    p = Path(post["video_path"])
    if not p.exists():
        return [f"video missing: {p}"]
    live = [pf for pf, d in post["deliveries"].items() if d["status"] in ("pending", "failed")]
    if not live:
        return []
    try:
        pr = probe_result or probe(p)
    except Exception as e:
        return [f"could not read video: {type(e).__name__}"]
    return [f"{pf}: {x}" for pf in live for x in preflight(post, pf, pr)]


def duration(path: str | Path) -> float | None:
    """Duration in seconds via ffprobe, or None if unreadable."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
             "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=20, check=True).stdout.strip()
        return round(float(out), 2)
    except Exception:
        return None


def thumbnail(video: str | Path, out: Path) -> bool:
    """Write a 360px-wide poster frame (≈1s in; falls back to frame 0 for very short clips)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".part.jpg")
    for seek in ("1", "0"):
        tmp.unlink(missing_ok=True)
        try:
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-ss", seek, "-i", str(video), "-frames:v", "1",
                 "-vf", "scale=360:-2", str(tmp)],
                capture_output=True, timeout=45, check=True)
        except (subprocess.SubprocessError, OSError):
            continue
        if tmp.exists() and tmp.stat().st_size > 0:
            tmp.replace(out)
            return True
    tmp.unlink(missing_ok=True)
    return False
