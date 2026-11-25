"""AI caption drafts: local Whisper transcript → headless Claude → per-platform copy.

Drafts only. Nothing posts until a human approves the post.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from . import config

WHISPER_CANDIDATES = (
    shutil.which("whisper"),
    str(Path.home() / "Library/Python/3.9/bin/whisper"),
)

PROMPT = """You write social copy for short vertical videos{byline}
(AI for creators and regular people; voice: plain, curious, direct, no hype).

Here is the transcript of one short:
<transcript>
{transcript}
</transcript>
Working title (from the filename): {hint}

Return ONLY a JSON object with these keys:
  "title":     YouTube Shorts title, max 70 characters, curiosity-driven, no clickbait lies, no emoji
  "caption":   a 1-3 sentence caption that hooks in the first line, then 3-5 relevant hashtags
  "tiktok":    TikTok caption, punchier, max 150 characters incl. 3-4 hashtags
  "instagram": Instagram Reels caption, 1-3 short sentences + a soft call to follow, then 5-8 hashtags
  "facebook":  Facebook Reels caption, conversational 1-2 sentences, 1-3 hashtags
  "linkedin":  LinkedIn commentary with one concrete takeaway for someone at work
  "x":         one stand-alone X post under 280 characters, centered on the spoken hook
Rules: never invent facts not in the transcript;{name_rule}
no "In this video".
"""


def transcribe(video: Path, sha: str) -> str:
    cache = config.DATA_DIR / "transcripts" / f"{sha}.txt"
    if cache.exists():
        return cache.read_text()
    exe = next((w for w in WHISPER_CANDIDATES if w and Path(w).exists()), None)
    if not exe:
        raise RuntimeError("whisper CLI not found (pip install openai-whisper)")
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run([exe, str(video), "--model", "base.en", "--language", "en",
                        "--output_format", "txt", "--output_dir", tmp, "--fp16", "False",
                        "--verbose", "False"],
                       check=True, capture_output=True, timeout=900)
        text = next(Path(tmp).glob("*.txt")).read_text().strip()
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(text)
    return text


def prompt(transcript: str, hint: str, cfg: config.Config | None = None) -> str:
    """The caption request, signed with the creator's name when one is configured."""
    name = ((cfg or config.load()).get("general", "creator_name") or "").strip()
    return PROMPT.format(
        transcript=transcript[:6000], hint=hint,
        byline=(f" from {name}'s channel" if name else " for the channel") + ' "Why AI Matters"',
        name_rule=f' spell the name "{name}" if used;' if name else "")


def draft(transcript: str, hint: str) -> dict[str, Any]:
    claude = shutil.which("claude") or str(Path.home() / ".local/bin/claude")
    out = subprocess.run(
        [claude, "-p", prompt(transcript, hint),
         "--output-format", "json"],
        capture_output=True, text=True, timeout=300, check=True,
    ).stdout
    result = json.loads(out).get("result", out)
    start, end = result.find("{"), result.rfind("}")
    if start < 0:
        raise RuntimeError(f"Claude returned no JSON: {result[:200]}")
    return json.loads(result[start:end + 1])


def to_post_fields(d: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": str(d.get("title", ""))[:100],
        "caption": str(d.get("caption", "")),
        "overrides": {p: {"caption": str(d[p])} for p in config.SOCIAL_CAPTIONS
                      if d.get(p)},
    }
