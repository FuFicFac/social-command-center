# Social Command Center

Post one short to **YouTube Shorts, Instagram Reels, Facebook Reels and TikTok** at once — or on a schedule — from a local queue on this Mac.

Built 2026-10-23 from the July 22 spec (`D Second Brain/Builder/Social Command Center Spec.md`). It's the publishing half the spec called for. Just the Good Parts' "Social Command Center" screen stops at a manual handoff; this tool actually posts.

## The flow

```
Shorts Ready/*.mp4 ──import──▶ draft ──AI captions (Whisper + Claude)──▶ you edit
      ──approve (+ optional time)──▶ scheduler (every minute) ──▶ each platform's adapter
      ──▶ live links + status back in the dashboard
```

- **Nothing posts without your approval.** Imports and AI captions are drafts.
- **Never double-posts.** If an upload dies mid-flight it's flagged *needs review*, never blindly retried.
- **Changed the video after approving?** It's blocked and un-approved (SHA-256 check).
- **Secrets live in the macOS Keychain**, never in files or logs.
- **Every platform starts in `dryrun`**, which shows what *would* post and touches nothing.

## Use it

```bash
./OPEN-SOCIAL-COMMAND-CENTER.command   # dashboard at http://127.0.0.1:8787
```

or from the CLI (`uv run scc …`):

| Command | What it does |
|---|---|
| `scc status` | routes, connection readiness, queue counts |
| `scc import` | queue every new MP4 in Shorts Ready as a draft |
| `scc captions [ID]` | AI-draft title + per-platform captions (one, or all uncaptioned drafts) |
| `scc edit ID --title … --caption … --set tiktok.caption=… --to youtube,tiktok --at "2026-10-24 09:00"` | change copy / targets / time |
| `scc approve ID [--at TIME]` | approve (runs preflight checks first) |
| `scc publish ID` | approve + post now (asks you to type `post`) |
| `scc list` / `scc show ID` | queue / one post with its event log |
| `scc run` | one scheduler pass (what launchd runs every minute) |
| `scc retry ID [platforms]` / `scc mark ID platform published --url …` | recover |
| `scc route upload_post tiktok` | choose where a platform's posts go |

Times are Pacific (`general.timezone`).

## Routes

Each platform is routed independently in `config.toml`:

| Route | Meaning |
|---|---|
| `dryrun` | default; logs what would post |
| `direct` | the platform's own API, free, needs your developer app |
| `upload_post` | [Upload-Post](https://upload-post.com): one API key, they hold the audited apps; one upload fans out to every platform routed there |

**Recommended:** `direct` for YouTube, Instagram and Facebook (free), and `upload_post` for TikTok. See SETUP.md for why.

## Delivery states

`pending` → `uploading` → `processing` → **`published`**

Other outcomes:
- `private`: uploaded, but YouTube locked it Private. That's an unaudited API project; flip it in Studio.
- `inbox`: waiting in your TikTok app. Tap Post.
- `simulated`: a dry run.
- `failed`: nothing went live, so it's safe to retry.
- `needs_review`: it may have posted. Check, then `mark` or `retry`.

## Dev

```bash
uv run pytest && uv run ruff check .
```
