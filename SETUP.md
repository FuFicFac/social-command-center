# Connecting the platforms

Do these yourself: they're your accounts. The secrets go into the Keychain, never into files. Each section starts with the dashboard path. The Terminal commands are an alternative. Each section ends with the step that switches that platform from Dry run to live. The **Connections & Settings** page shows what's still missing for each platform.

Rules checked 2026-10-23:

| Platform | Free direct API? | The catch |
|---|---|---|
| Instagram Reels | ✅ | Needs an IG **professional** account linked to a Facebook Page. A Meta app in Development mode works for your own accounts with no review. Limit: 100 API posts per 24 hours. |
| Facebook Reels | ✅ | Same Meta app and same sign-in as Instagram. Reels must be 3–90 seconds. |
| YouTube Shorts | ✅ | Until your Google Cloud project passes YouTube's free API compliance audit, uploads **may be** locked to Private. The tool reads back the real privacy setting and shows "Uploaded (private)" if it was locked. Flip them in Studio, or file the audit once. |
| TikTok | ⚠️ | Unaudited apps can only post privately (`SELF_ONLY`). TikTok explicitly lists "a utility tool to help upload contents to the account(s) you or your team manages" as **not** acceptable for audit. A personal app won't get public auto-posting. |

**So, for TikTok:**
- **Option A:** route it through Upload-Post. Fully automatic and public. Paid (Basic is about $16–24/month; the free tier excludes TikTok). Check the live pricing page.
- **Option B:** use the direct `inbox` mode, which is free. The video lands in your TikTok app's inbox and you tap Post. You get one tap per short, and you can add a trending sound while you're there.

Open the dashboard, then go to **Connections & Settings**. Each platform has a setup panel under **Set up a connection**. Click a panel to open it. It lists the steps, then the fields to fill in.

---

## 1. Instagram + Facebook (Meta), about 20 minutes

**In the dashboard:** Connections & Settings → **Meta — Instagram + Facebook**.

1. Make sure Instagram is a **Professional** account (Creator or Business) and is linked to your Facebook Page: Instagram → Settings → Account type, and Page settings → Linked accounts.
2. Go to developers.facebook.com → My Apps → **Create App** → type **Business**.
3. Add products **Facebook Login for Business** and **Instagram** (choose "API setup with Facebook login").
4. App settings → Basic: copy the **App ID** and **App Secret**.
5. Paste them into the **App ID** and **App secret** fields and click **Save app**.
6. Open **Graph API Explorer**, pick your app, tick the listed Page and Instagram permissions, and generate a user token. Paste it into **User token from Graph API Explorer** and click **Find my pages**.
7. Pick your Page from the list. The dashboard swaps the token for a **non-expiring Page token** and finds the Instagram account linked to that Page.
8. Set the route to **Direct** for Instagram and Facebook, above in **Platforms**.

The Graph API version defaults to `v25.0`.

**Or from Terminal:**

```bash
uv run scc config meta.app_id YOUR_APP_ID
uv run scc secret set meta.app_secret
uv run scc connect meta
uv run scc route direct instagram,facebook
```

`scc connect meta` walks you through the same Graph API Explorer steps. It asks for the token (hidden).

## 2. YouTube, about 15 minutes

**In the dashboard:** Connections & Settings → **YouTube**.

1. Go to console.cloud.google.com → New project (for example "Social Command Center").
2. APIs & Services → Library → enable **YouTube Data API v3**.
3. OAuth consent screen → External → add yourself as a **Test user**, then set it to **In production** so sign-in doesn't expire after 7 days.
4. Credentials → Create → **OAuth client ID** → type **Desktop app**. Copy the Client ID and secret.
5. Paste them into **Client ID** and **Client secret** and click **Save client**.
6. Click **Sign in with Google** and pick the Why AI Matters channel.
7. Set the route to **Direct** for YouTube, above in **Platforms**.

**Or from Terminal:**

```bash
uv run scc config youtube.client_id YOUR_CLIENT_ID
uv run scc secret set youtube.client_secret
uv run scc connect youtube
uv run scc route direct youtube
```

Pick the Why AI Matters channel when Google asks.

8. *(Once, later)* To get public uploads straight from the API, file the **YouTube API Services audit** form for this project. It's free and checks the project against the API terms of service. Until it passes, uploads may be locked to Private. The dashboard shows "Uploaded (private)" when that happens. Flip them in Studio.

> Leaving the consent screen in "Testing" expires refresh tokens after 7 days. Set it to **In production**; personal use under 100 users needs no verification for these scopes. If `scc status` starts failing, run `scc connect youtube` again (or click **Sign in with Google** again).

## 3a. TikTok via Upload-Post (recommended), about 5 minutes

**In the dashboard:** Connections & Settings → **Upload-Post**.

1. Sign up at upload-post.com. Create a **profile** (for example `whyaimatters`) and connect TikTok to it. Optionally connect the others too.
2. The **profile name** is the profile *username* shown in Upload-Post.
3. In Upload-Post, go to Dashboard → API Keys and create a key.
4. Paste the key into **API key** and the username into **Profile name**. Click **Save & test**.
5. Back in **Platforms** above, set TikTok to the **Upload-Post** route.

**Or from Terminal:**

```bash
uv run scc connect upload-post
uv run scc route upload_post tiktok
```

`scc connect upload-post` asks for the key (hidden) and the profile name.

You could route *everything* through Upload-Post and skip sections 1 and 2. That works, but you'd pay for the YouTube and Meta posting that the free direct lanes already handle.

## 3b. TikTok direct, inbox mode (free), about 20 minutes plus TikTok's scope approval

**In the dashboard:** Connections & Settings → **TikTok direct**.

1. Go to developers.tiktok.com → Manage apps → Create. Add **Login Kit** and **Content Posting API**, and request the `video.upload` scope.
2. Add the redirect URI `http://localhost:8790/callback/` (desktop platform).
3. In the panel, leave **Mode** on **Inbox**.
4. Copy the **Client key** and **Client secret** into their fields and click **Save app**.
5. Click **Sign in with TikTok**. Then set TikTok to the **Direct** route above in **Platforms**.

The video shows up in your TikTok inbox notifications. Tap it and post. The dashboard shows "In TikTok inbox — tap Post" until you do, then "Live".

> TikTok's rule: apps that aren't audited can only post privately, and TikTok doesn't accept personal upload tools for audit. Inbox mode is the free way to get each short onto your TikTok. Upload-Post (section 3a) is the hands-off way to get public posts.

**Or from Terminal:**

```bash
uv run scc config tiktok.client_key YOUR_CLIENT_KEY
uv run scc secret set tiktok.client_secret
uv run scc connect tiktok
uv run scc route direct tiktok
```

---

## 4. Turn on hands-off scheduling

**In the dashboard:** Connections & Settings → **Background scheduler**.

- While the dashboard is open, it runs the scheduler for you. The panel says "Running while this page is open."
- Click **Install background scheduler** to keep posting when the dashboard is closed. The panel then shows the scheduler as installed. Your Mac must be awake.
- Click **Uninstall** to turn the background scheduler off.

**Or from Terminal:**

```bash
scripts/install-scheduler.sh     # every minute: post approved + due shorts, check statuses
scripts/uninstall-scheduler.sh   # turn it off
```

Without the background scheduler, posts go out only while the dashboard is open, or when you click **Run scheduler** or **Post now**, or run `scc run`.

## Fini Vids and the Shorts inventory

Run `.venv/bin/python scripts/install-intake.py` on the Mac to install the dashboard and folder watcher as per-user launch agents. The watcher checks the mounted Fini Vids folder every minute after login. A new render must stay unchanged across checks for at least 90 seconds. Existing files are recorded on first installation. A renamed file or identical copy is matched by file identity or content. Local Whisper, FFmpeg and the signed-in Codex CLI prepare the metadata. The desktop Codex app can be closed.

Open **Shorts inventory** to review existing exports and their YouTube receipts. **Prepare for Instagram, Facebook and TikTok** creates an unapproved local draft from the selected item. Multiple paths for identical media share one entry. Some inventory entries are previews or alternate cuts.

New vertical renders up to three minutes enter the Command Center with copy for YouTube, Instagram, Facebook and TikTok. The default release setting requires approval. The opt-in setting `fini.auto_approve_new_shorts` applies only to future folder drops. A new Short waits for a real route on each platform. The existing library and individually chosen archive files stay under manual control.

The local `youtube.uploads_paused` setting excludes YouTube from new Shorts and holds long-form metadata locally. It also blocks YouTube on every Command Center delivery route. A held long-form package remains available for individual review when uploading resumes.

For a destination with unverified posting history, the editor offers **Already uploaded** and **Checked account · no match**. An existing upload can include its original link. Checking history preserves the post’s normal approval setting. Confirmed uploads are recorded by file hash and platform in `upload_receipts`, independently of the editable queue, so removing a queue entry or renaming the exact file preserves its history. Re-encoded or recut videos may require audio or transcript comparison.

The main Upload-Post profile is the Why AI Matters destination. For a separate A-List profile, use `scc config fini.alist_upload_post_user PROFILE`. A-List deliveries wait until their own destination is configured. Use **Connections & Settings** to save the API key in Keychain, verify the profile, and choose each platform's route. Use the local editor to preview and approve posts.

Long-form renders use private YouTube Studio staging after transcript-based channel selection. Chrome must be signed in with the intended channel available. The Mac must be awake and its graphical session available for Studio interaction. An interrupted transfer is held with the available receipt. The user can add the thumbnail in Studio. The destination never receives an automatic public release from this long-form uploader.

The local intake ledger is the `fini_items` table in `data/scc.sqlite3`. Its `phase`, `message` and `receipt` columns distinguish pending preparation, private Studio staging, review and delivered Shorts drafts. The system log for intake is `~/Library/Logs/com.creator.fini-intake.log`.
