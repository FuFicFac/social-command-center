# Dashboard HTTP API — v2 contract

The backend (`scc/server.py`) and the UI (`scc/static/*`) are built against this file.
If you must deviate, update this file in the same change.

Conventions
- Server binds 127.0.0.1 only. Host header must be `127.0.0.1:<port>` or `localhost:<port>` (else 403).
- Every POST requires header `X-SCC-Token: <token>`; the token is injected into index.html
  by replacing the literal `__SCC_TOKEN__` (the UI reads it from `<meta name="scc-token" content="__SCC_TOKEN__">`).
- JSON in, JSON out. Errors: HTTP 4xx with `{"error": "message", "issues": [..optional..]}`.
- All timestamps on the wire are ISO-8601 **UTC** strings (`2026-10-24T16:00:00+00:00`).
  The UI converts to/from local time using `settings.timezone` (IANA, default America/Los_Angeles).
  Local dates (no time) are `YYYY-MM-DD` strings interpreted in `settings.timezone`.
- Platforms: `youtube`, `instagram`, `facebook`, `tiktok`. Routes: `dryrun`, `direct`, `upload_post`.

## Static
| GET | Returns |
|---|---|
| `/` | `static/index.html` with token injected. CSP: `default-src 'self'; img-src 'self' data:; media-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'` |
| `/static/<file>` | files in `scc/static/` (app.js, styles.css, …). Only plain filenames `[A-Za-z0-9._-]+`; no traversal. Correct content-types for .js/.css/.svg/.png. |
| `/media/<post_id>` | the post's MP4, HTTP Range supported |
| `/thumb/<post_id>.jpg` | poster frame (generated lazily with ffmpeg at ~1.0s, width 360, cached in `data/thumbs/<sha256>.jpg`) |

## GET /api/state
```jsonc
{
  "settings": {
    "timezone": "America/Los_Angeles",
    "shorts_folder": "~/Movies/Shorts Ready",
    "schedule": { "slots": ["09:00", "17:00"], "days": ["mon","tue","wed","thu","fri","sat","sun"] },
    "routing": { "youtube": "dryrun", "instagram": "dryrun", "facebook": "dryrun", "tiktok": "dryrun" },
    "tiktok_mode": "inbox"
  },
  "platforms": {                      // one per platform
    "youtube": { "route": "dryrun", "ready": true, "detail": "dry run — nothing is posted",
                 "configured": { "direct": false, "upload_post": false } }   // has creds for that route?
  },
  "posts": [ Post, ... ],             // newest first
  "jobs": { "<post_id>": "drafting captions" },   // per-post running/queued background work
  "caption_queue": { "running": "<post_id>|null", "queued": ["<post_id>", ...] },
  "connect": { "youtube": "idle|waiting for browser|connected: <name>|error: <msg>", ... },
  "scheduler": { "in_process": true, "launchd_installed": false, "last_run": "<iso>|null", "last_result": "processed 0 due post(s)" },
  "events": [ { "id": 1, "at": "<iso>", "post_id": "..", "platform": "..", "level": "info|warn|error", "message": ".." } ],  // 60 newest
  "counts": { "drafts": 0, "needs_captions": 0, "scheduled": 0, "posted": 0, "attention": 0 },
  "connections": {                    // ADDED (non-secret): what is already saved, so forms can prefill
    "youtube":     { "client_id": "", "has_client_secret": false, "signed_in": false },
    "meta":        { "app_id": "", "has_app_secret": false, "page_id": "", "ig_user_id": "", "signed_in": false },
    "tiktok":      { "client_key": "", "mode": "inbox", "has_client_secret": false, "signed_in": false },
    "upload_post": { "user": "", "has_api_key": false }
  }
}
```
`state.connect` has keys `youtube` and `tiktok`. `state.jobs` labels are `"queued for captions"`,
`"drafting captions"` and `"publishing"`. Secret values are never present anywhere in the state.
`Post`:
```jsonc
{
  "id": "1e019134c7", "video_path": "/abs/file.mp4", "filename": "file.mp4",
  "title": "..", "caption": "..",
  "overrides": { "tiktok": { "caption": ".." } },          // only non-empty overrides
  "scheduled_at": "<iso>|null", "approved_at": "<iso>|null",
  "created_at": "<iso>", "updated_at": "<iso>",
  "duration": 46.9,                                        // seconds, from ffprobe, cached
  "duplicate_of": ["otherId", ...],                        // posts whose normalized title-slug matches
  "bucket": "draft|scheduled|posting|posted|attention",    // see below
  "deliveries": { "youtube": { "status": "pending", "route": "dryrun|null", "url": null, "error": null,
                               "attempts": 0, "meta": { "note": ".." }, "updated_at": "<iso>" }, ... }
                                                           // skipped platforms ARE included with status "skipped"
}
```
`bucket` rules (first match wins, ignoring `skipped` deliveries):
1. any delivery `failed` or `needs_review` → `attention`
2. any delivery `uploading` or `processing` → `posting`
3. all deliveries in `published|private|inbox|simulated` → `posted`
4. `approved_at` set → `scheduled` (includes "approved, post asap")
5. else `draft`
`counts.needs_captions` = drafts whose `caption` is empty.

## Posts
| POST | Body | Effect |
|---|---|---|
| `/api/import` | `{}` | import new MP4s from shorts_folder as drafts → `{ok, imported}` |
| `/api/run` | `{}` | run one scheduler pass in background → `{ok}` |
| `/api/posts/<id>` | any of `{title, caption, overrides, targets: [platforms], scheduled_at: iso\|null}` | update. Changing title/caption/overrides on an approved post clears approval (response `{ok, post, unapproved: true}`) |
| `/api/posts/<id>/approve` | `{}` | preflight then approve; 400 `{error, issues}` if blocked → `{ok, post}` (`unapprove`/`retry` also return `post`) |
| `/api/posts/<id>/unapprove` | `{}` | back to draft (keeps scheduled_at) |
| `/api/posts/<id>/publish` | `{"confirm": "post"}` | approve, clear schedule, post now (background) |
| `/api/posts/<id>/captions` | `{}` | enqueue AI caption draft (409 if already queued/running) |
| `/api/posts/<id>/retry` | `{platforms: [..]}` | reset those deliveries to pending (409 if any is uploading/processing) |
| `/api/posts/<id>/remove` | `{}` | delete from queue (409 if in flight) |

## Bulk
`POST /api/bulk` `{ "action": "captions|approve|unapprove|remove|targets|unschedule", "ids": [..], "targets": [..] }`
→ `{ ok: true, results: { "<id>": "ok" | "<error message>" } }` (per-id; one failure doesn't stop others).
`captions` enqueues each id on the caption queue (sequential worker — never run Whisper/Claude in parallel).

## Scheduling
Slots: every day in `settings.schedule.days`, at each `settings.schedule.slots` local time.
A slot is **taken** if any post has `scheduled_at` equal to that slot instant (to the minute).

| Method | Path | Body / query | Returns |
|---|---|---|---|
| GET | `/api/slots` | `?from=YYYY-MM-DD&days=N` (N ≤ 120, default 28) | `{ slots: [ { at: iso, local: "Fri Oct 24 9:00 AM", post_id: "..|null" } ] }` — future slots only (past ones omitted) |
| POST | `/api/schedule/auto` | `{ ids: [..], start: "YYYY-MM-DD"\|null, approve: bool, dry_run: bool }` | Fills the next free slots (from `start`, default now) in the order of `ids`. Ids that fail preflight while `approve: true` are not scheduled and appear in `errors`. `dry_run: true` returns the plan without saving. → `{ ok, assigned: { id: iso }, errors: { id: "msg" } }` |
| POST | `/api/schedule/clear` | `{ ids: [..] }` | sets scheduled_at null AND clears approval (approved + no time = "post now") → `{ok, results: {id: "ok"\|msg}}` |
| POST | `/api/settings` | any of `{ timezone, shorts_folder, schedule: {slots, days}, routing: {platform: route}, tiktok_mode }` | validate + save config.toml → `{ok, settings}` ; slots must be `HH:MM` 24h, days from mon..sun |

Moving one post on the calendar = `POST /api/posts/<id>` with a new `scheduled_at`.

## Connections (secrets go straight to Keychain; never echoed back, never logged)
| POST | Body | Effect |
|---|---|---|
| `/api/connect/upload_post` | `{ api_key?, user }` | save key (if given) + profile name; then verify → `{ok, detail}` |
| `/api/connect/meta/app` | `{ app_id, app_secret? }` | save |
| `/api/connect/meta/token` | `{ user_token }` | exchange; returns `{ ok, pages: [ {id, name, ig_username|null} ] }`; server keeps the page tokens in memory ≤10 min |
| `/api/connect/meta/page` | `{ page_id }` | pick page → stores page token + ig id → `{ok, detail}` |
| `/api/connect/youtube/client` | `{ client_id, client_secret? }` | save |
| `/api/connect/youtube/start` | `{}` | starts the Google loopback OAuth in a background thread (it opens the system browser); progress in `state.connect.youtube` |
| `/api/connect/tiktok/client` | `{ client_key, client_secret?, mode: "inbox"\|"direct" }` | save |
| `/api/connect/tiktok/start` | `{}` | background loopback OAuth like YouTube |
| `/api/connect/verify` | `{ platform }` | one cheap authenticated API call for the platform's current route (dryrun → `{ok:true, detail:"dry run"}`; not configured → `{ok:false, detail:<what is missing>}` without a network call) → `{ ok, detail }` |
| `/api/disconnect` | `{ platform, route }` | delete that route's stored tokens (not client ids): direct youtube → `youtube.refresh_token`, direct instagram/facebook → `meta.page_token`, direct tiktok → `tiktok.refresh_token`, upload_post → `upload_post.api_key`; `route` must be `direct` or `upload_post` → `{ok, removed: [names]}` |
| `/api/scheduler/install` | `{}` | run `scripts/install-scheduler.sh` → `{ok, output}` |
| `/api/scheduler/uninstall` | `{}` | run `scripts/uninstall-scheduler.sh` |

## Notes added by the backend
- `scheduler.in_process` is true only while `scc serve` is running its 60 s loop (first pass ~3 s after start).
- `/api/connect/upload_post` always returns HTTP 200 once the save succeeded; `ok:false` + `detail` reports a failed verify.
- `/api/connect/meta/token` and `/page` return 400 `{error}` for a rejected token / expired page list (re-paste the token). Error text is scrubbed of the token.
- Slots `HH:MM` accept `9:30` and are normalized to `09:30`; days are returned in Mon..Sun order.
- Settings 400s carry `{error: <first problem>, issues: [all problems]}`; nothing is saved unless everything validates.
- `schedule/auto` also errors per id with `"post is in flight"` and `"no platforms selected"` (approve path) and `"no free slots (check schedule settings)"`.
- CLI parity: `scc slots [--days N]`, `scc schedule auto ID... [--start D] [--approve] [--dry-run]`, `scc scheduler install|uninstall|status`.
