# Build your own Social Command Center

Paste the block below into Claude Code, Codex, or another coding agent with access to a new project folder. The first demo uses a sample video and simulated accounts. For your own work, the input is an export from whichever production skill fits the story.

```text
Build me a Social Command Center I can run on my own computer. My video-making
skills produce finished edits. I want to drop a reviewed export into a folder,
preview it, edit a caption for each network, choose accounts, and schedule it
weeks ahead. After release, I want to open each live post from the dashboard.

Use the production method that fits the source. If I have a project wiki, consult
its index and follow the entries to the actual skill files. Show me which skills
you found and let me point to an instructor-supplied copy or use a finished MP4.

- Talking-head episode or screen demo: use YouTube Video Builder
  (youtube-video-builder) when available. Cut the recording on complete thoughts,
  show the demo or illustration as the speaker mentions it, and keep the
  HyperFrames project editable for review. Export the approved 16:9 episode and
  make captioned vertical cuts from the original footage. Its optional Peter Yang
  mode applies fast-cut pacing to the horizontal edit.
- A standalone vertical Short from a recording: use Peter Yang’s video-short.
  Pick a hook with a payoff, check the first and last words against the audio,
  frame the speaker for 9:16, and time captions and cutaways to the speech.
  Review the draft with frame-specific notes before exporting the 1080×1920 Short.
- A narrated cinematic story: use cinematic-film to time camera moves across
  photos, maps, headlines, and labels to the narration. For a longer film,
  use the instructor’s extended edition if supplied. Anchor each section to the
  presenter’s recorded take, check framing at every camera beat, review each
  section, and score it with approved cues. Pull a few connected beats into a
  vertical companion with its own framing and captions.

Each route hands a verified export to the same watched folder. Preserve the
editable source project. Keep captions burned into a Short separate from the
post text I edit for Instagram or LinkedIn. Approval of an edit makes it ready
for the library. I choose destinations and approve publishing separately.

Start in a new project folder. Use Python 3.12+, SQLite, FFmpeg/ffprobe, and a simple
HTML/CSS/JavaScript dashboard on localhost. Detect my operating system. Give me one
launch command and a clickable launcher where supported. Use a demo folder and a
generated sample video so I can try it before connecting an account. Build and run the
app in this session.

1. Make the first demo work. Drop a video into the demo folder and show it in a
searchable library with a playable preview, thumbnail, duration, transcript, and draft
captions. Use local Whisper when available. Let me paste a transcript or write
captions if I have no AI service. Keep the title, default caption, and per-platform
edits separate.

2. Add six destinations: Instagram Reels, TikTok, personal LinkedIn, X, Facebook
Reels, and YouTube Shorts. Show the account or brand, exact copy, and delivery status
for each selection. Draft from the spoken content. For example, use the opening hook
on TikTok and the practical takeaway on LinkedIn. Keep YouTube’s title and description
separate. Check the current platform limits before scheduling.

3. Give me a calendar with time zone, posting days, slots, and a preview before bulk
scheduling. Skip occupied slots. Let me reschedule or return a post to drafts. Run one
background scheduler while the computer is awake, and explain how to start it at
login. Start every route in dry run with automatic approval off. Approval covers one
version of a clip, its captions, and its selected destinations. Editing those clears
approval. Add a YouTube pause that applies to every upload path.

4. Watch a folder I choose and show its path in the dashboard. On first run, inventory
existing files and let me select any I want to import. Wait for new files to finish
copying. Route vertical videos up to and including 180 seconds to Shorts. Prepare
other videos separately with a title, description, and 16:9 thumbnail for private
YouTube review. Keep them local while YouTube is paused.

5. Make the publishing route configurable by destination. For the shared route, use Upload-Post. Read its current video-upload and
status docs. Send the video once for the selected accounts on one profile. Save the
request ID before sending, then track each destination independently in SQLite with
its status, platform post ID, and permalink. Distinguish simulated, processing, inbox,
private, scheduled, failed, and public results. Keep credentials in the operating
system’s credential store and out of logs. Protect dashboard changes with a session
token. Offer direct platform routes only where implemented and verified. Live
publishing requires my explicit approval.

I also want a provider-independent option. Use direct Meta publishing where my
professional Instagram account and linked Facebook Page qualify. Offer TikTok
inbox handoff with the approved upload scope and my final tap, or let me post
manually. For personal LinkedIn and X, check my app access, current permissions
and fees before building each adapter. Preserve per-account receipts on every
route. For manual posts let me paste the verified public link and record its origin.
Make the local demo fully usable with zero credentials or paid subscriptions.

6. Prevent duplicate uploads. Fingerprint the file with SHA-256 and keep receipts by
file, platform, and destination account, even if I remove a library entry. Check
account history for posts made elsewhere. Use transcript or audio evidence for recuts,
and ask me when history is inaccessible. A private upload also counts. If a request
times out, look up the original request and reconcile each destination before
permitting a retry.

Show me the finished demo using a mock publisher. Simulate one Short going to four
networks, with two replies delayed and the first upload response lost. Prove there was
one transfer and four distinct receipts after restart. Rename the file and show that
the same accounts are protected. Finish with launch instructions, connection steps,
and a clear account of what you tested locally and what still needs a real account.
Keep all demo traffic local and all real accounts untouched.
```

Two public starting points are [Peter Yang’s Video Short](https://github.com/petergyang/video-short) and [Moh4696’s Cinematic Skill](https://github.com/Moh4696/cinematic-skill). The YouTube Video Builder and the long-form cinematic additions described in class are creator-maintained workflows. A reviewed video from your own setup also works for the publishing exercise.

For live integration, the provider documents the multipart video request and how to recover its results. [Upload-Post video upload](https://docs.upload-post.com/api/upload-video/) · [Upload status](https://docs.upload-post.com/api/upload-status/)

For the provider-independent build, consult [TikTok’s inbox upload guide](https://developers.tiktok.com/docs/en/content-posting-api-get-started-upload-content), [TikTok’s Direct Post rules](https://developers.tiktok.com/docs/en/content-sharing-guidelines), [LinkedIn’s member sharing guide](https://learn.microsoft.com/en-us/linkedin/consumer/integrations/self-serve/share-on-linkedin), and [X API pricing](https://docs.x.com/x-api/getting-started/pricing). An existing open-source scheduler also has a [Postiz self-hosting guide](https://docs.postiz.com/self-host/installation/docker-compose).
