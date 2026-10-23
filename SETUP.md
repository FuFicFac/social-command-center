# Connecting the platforms

Do these yourself: they're your accounts, and the secrets go straight from your keyboard into the Keychain (`scc secret set …` hides input). Each section ends with the command that flips that platform from `dryrun` to live. Run `uv run scc status` any time to see what's still missing.

Rules checked 2026-10-23:

| Platform | Free direct API? | The catch |
|---|---|---|
| Instagram Reels | ✅ | Needs an IG **professional** account linked to a Facebook Page. A Meta app in Development mode works for your own accounts with no review. Limit: 100 API posts per 24 hours. |
| Facebook Reels | ✅ | Same Meta app and same sign-in as Instagram. Reels must be 3–90 seconds. |
| YouTube Shorts | ✅ | Until your Google Cloud project passes YouTube's free API compliance audit, uploads are **force-locked to Private**. The tool marks them `private`. Flip them in Studio, or file the audit once. |
| TikTok | ⚠️ | Unaudited apps can only post privately (`SELF_ONLY`). TikTok explicitly lists "a utility tool to help upload contents to the account(s) you or your team manages" as **not** acceptable for audit. A personal app won't get public auto-posting. |

**So, for TikTok:**
- **Option A:** route it through Upload-Post. Fully automatic and public. Paid (Basic is about $16–24/month; the free tier excludes TikTok). Check the live pricing page.
- **Option B:** use the direct `inbox` mode, which is free. The video lands in your TikTok app's inbox and you tap Post. You get one tap per short, and you can add a trending sound while you're there.

---

## 1. Instagram + Facebook (Meta), about 20 minutes

1. Make sure Instagram is a **Professional** account (Creator or Business) and is linked to your Facebook Page: Instagram → Settings → Account type, and Page settings → Linked accounts.
2. Go to developers.facebook.com → My Apps → **Create App** → type **Business**.
3. Add products **Facebook Login for Business** and **Instagram** (choose "API setup with Facebook login").
4. App settings → Basic: copy the **App ID** and **App Secret**.
5. Store them:
   ```bash
   uv run scc config meta.app_id YOUR_APP_ID
   uv run scc secret set meta.app_secret
   ```
6. Connect:
   ```bash
   uv run scc connect meta
   ```
   It walks you through Graph API Explorer: pick your app, tick the listed permissions, generate a token and paste it (hidden). It swaps that for a **non-expiring Page token** and finds your Page and Instagram account.
7. Go live:
   ```bash
   uv run scc route direct instagram,facebook
   ```

## 2. YouTube, about 15 minutes

1. Go to console.cloud.google.com → New project (for example "Social Command Center").
2. APIs & Services → Library → enable **YouTube Data API v3**.
3. OAuth consent screen → External → add yourself as a **Test user**.
4. Credentials → Create → **OAuth client ID** → type **Desktop app**. Copy the Client ID and Secret.
5. Store them and sign in:
   ```bash
   uv run scc config youtube.client_id YOUR_CLIENT_ID
   uv run scc secret set youtube.client_secret
   uv run scc connect youtube
   ```
   Pick the Why AI Matters channel when Google asks.
6. Go live:
   ```bash
   uv run scc route direct youtube
   ```
7. *(Once, later)* To get public uploads straight from the API, file the **YouTube API Services audit** form for this project. It's free and checks the project against the API terms of service.

> Leaving the consent screen in "Testing" expires refresh tokens after 7 days. Set it to **In production**; personal use under 100 users needs no verification for these scopes. If `scc status` starts failing, run `scc connect youtube` again.

## 3a. TikTok via Upload-Post (recommended), about 5 minutes

1. Sign up at upload-post.com. Create a **profile** (for example `whyaimatters`) and connect TikTok to it. Optionally connect the others too.
2. Dashboard → API Keys → create a key.
3. Connect:
   ```bash
   uv run scc connect upload-post
   ```
   It asks for the key (hidden) and the profile name.
4. Go live:
   ```bash
   uv run scc route upload_post tiktok
   ```

You could route *everything* through Upload-Post and skip sections 1 and 2. That works, but you'd pay for the YouTube and Meta posting that the free direct lanes already handle.

## 3b. TikTok direct, inbox mode (free), about 20 minutes plus TikTok's scope approval

1. Go to developers.tiktok.com → Manage apps → Create. Add **Login Kit** and **Content Posting API**, and request the `video.upload` scope.
2. Add the redirect URI `http://localhost:8790/callback/` (desktop platform).
3. Store the credentials and connect:
   ```bash
   uv run scc config tiktok.client_key YOUR_CLIENT_KEY
   uv run scc secret set tiktok.client_secret
   uv run scc connect tiktok
   uv run scc route direct tiktok
   ```
   The video shows up in your TikTok inbox notifications. Tap it and post. The dashboard shows `inbox` until you do, then `published`.

---

## 4. Turn on hands-off scheduling

```bash
scripts/install-scheduler.sh     # every minute: post approved + due shorts, check statuses
scripts/uninstall-scheduler.sh   # turn it off
```

Without it, nothing posts until you click **Run scheduler** or **Post now**, or run `scc run`.
