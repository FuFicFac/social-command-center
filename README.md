# Social Command Center

Prepare and schedule Shorts for YouTube, Instagram, Facebook, TikTok, LinkedIn and X from one dashboard on this Mac.

## Start it

Double-click `OPEN-SOCIAL-COMMAND-CENTER.command`. The dashboard opens at http://127.0.0.1:8787.

## Your first batch

1. **Import new shorts.** They come from your Shorts folder (set it under Connections & Settings; the default is `~/Movies/Shorts Ready`).
2. **Write captions.** Select shorts with the checkboxes (shift-click selects a range), then click ✨ Write captions. Whisper and Claude draft the titles and captions, one at a time.
3. **Edit.** Skim each draft and fix anything in the editor drawer.
4. **Auto-schedule.** Click 📅 Auto-schedule… and look at the preview plan. Leave "Approve them so they post automatically" ticked, then confirm.
5. **Check the Calendar.** Drag posts to rearrange them. Use "+ Add" to fill an open slot. Posts with no time yet wait in the Unscheduled tray.

## Posting rhythm

Settings → Posting schedule sets the times each day and the weekdays. The default is 9:00 AM and 5:00 PM, every day, Pacific time.

## What makes posts actually go out

Each platform starts in **Dry run**. **Upload-Post** supports all six destinations. YouTube, Instagram, Facebook and TikTok also have **Direct** routes. Select LinkedIn and X per Short in the editor. LinkedIn uses the connected personal profile. Review each selected account’s posting history before release.

Each brand has its own accounts. The editor shows the brand, platform and real handle for every destination, and a live post goes only to the accounts you ticked. Set this up under Connections & Settings → Brands & accounts.

Posts only go out while a scheduler runs. You have two options:

- Keep the dashboard open. It runs the scheduler while the page is open.
- Install the background scheduler (Settings → Install background scheduler). Posts then go out even when the dashboard is closed.

Either way, the Mac must be awake.

To connect your accounts and switch a platform off Dry run, see [SETUP.md](SETUP.md).

## Safety

- **Approval gate.** Imports and AI captions are drafts. Nothing posts until you approve it.
- **Never double-posts.** If an upload dies mid-flight, the post is marked *needs review*. It is never retried blindly.
- **Changed-video guard.** If you change a video after approving it, it is blocked and un-approved (SHA-256 check).
- **Keychain secrets.** Passwords and tokens live in the macOS Keychain. They never go into files or logs.
- **Unscheduling is safe.** Unscheduling a post returns it to draft, so it never posts by itself.

## Status meanings

These are the labels you'll see on each post:

| Label | What it means |
|---|---|
| Waiting | Approved or scheduled, not sent yet |
| Uploading… | The file is going up |
| Processing… | The platform is still working on it |
| Live | It's posted |
| Uploaded (private) | It went up, but YouTube locked it to Private |
| In TikTok inbox — tap Post | It's in your TikTok app. Open the app and tap Post |
| Dry run | Nothing was sent. This is only a preview |
| Failed | Nothing went live. It's safe to retry |
| Check manually | It may have posted. Check the platform, then mark it or retry it |

## Platform limits worth knowing

- **Facebook Reels** must be 3–90 seconds long. The API allows about 30 a day.
- **Instagram** allows 100 API posts a day.
- **TikTok** direct inbox mode allows at most 5 pending posts a day.
- **YouTube** uploads may be locked to Private until your Google project passes the API audit. The dashboard reads back the real privacy setting, so you'll see "Uploaded (private)" when that happens.

## CLI (optional)

Run these from Terminal in the project folder, with the prefix `uv run`. For example: `uv run scc status`.

| Command | What it does |
|---|---|
| `scc status` | Show routes, connection readiness and queue counts |
| `scc import` | Queue every new MP4 in Big Short Run Videos as a draft |
| `scc captions [ID]` | Draft captions for one post, or for every uncaptioned draft |
| `scc list` | Show the queue |
| `scc show ID` | Show one post and its event log |
| `scc approve ID [--at TIME]` | Approve a post, optionally with a time (runs checks first) |
| `scc publish ID` | Approve and post now. It asks you to type `post` |
| `scc schedule auto ID... [--approve]` | Fill the next free slots with these posts |
| `scc slots` | Show upcoming slots and which post holds each one |
| `scc scheduler install` / `uninstall` | Turn the background scheduler on or off |
| `scc retry ID [platforms]` | Reset a post's platforms to pending |
| `scc mark ID platform status --url …` | Record a result you checked by hand |
| `scc route upload_post tiktok` | Choose where a platform's posts go |
| `scc serve` | Open the dashboard |

## Dev

```bash
uv run pytest && uv run ruff check .
```
