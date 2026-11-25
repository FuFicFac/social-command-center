"""Private YouTube Studio staging through the Mac's signed-in Chrome window."""

import json
import re
import subprocess as sp
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

STATE = Path.home() / "Library/Application Support/Fini Vids"
HELPER = STATE / "bin/studio-ax"
CHANNELS = {"wam": "UC_FIRST_CHANNEL_ID_HERE_", "alist": "UC_SECOND_CHANNEL_ID_HERE"}


def command(*args, **kwargs):
    return sp.check_output([str(a) for a in args], text=True, timeout=25, **kwargs).strip()


def apple(script):
    return command("osascript", "-e", script)


def lines(text):
    return [line.strip() for line in text.splitlines() if line.strip()]


def controls():
    data = json.loads(command(HELPER))
    if data.get("state") != "ready":
        raise RuntimeError(f"Chrome controls unavailable: {data.get('state')}")
    return data["rows"]


def name(row):
    return row["title"] or row["desc"] or row["text"]


def find(rows, role, pattern):
    return next(
        (r for r in rows if r["role"] == role and re.search(pattern, name(r), re.IGNORECASE)), None
    )


def wait(check, seconds=18):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        result = check()
        if result:
            return result
        time.sleep(0.35)
    raise RuntimeError(f"Studio timed out at {getattr(check, '__name__', 'control')}")


class Studio:
    def __init__(self, channel):
        self.channel = CHANNELS[channel]
        self.home = f"https://studio.youtube.com/channel/{self.channel}/videos"
        self.window = None
        self.video_id = None

    def select_window(self):
        # Only take focus on an existing, signed-in window for the intended channel.
        script = f'''tell application "Google Chrome"
            repeat with w in windows
                if URL of active tab of w starts with "{self.home}" then
                    set wid to id of w
                    set index of w to 1
                    activate
                    return wid
                end if
            end repeat
        end tell'''
        result = apple(script)
        if not result.isdigit():
            # Reuse an existing Studio tab before opening a dedicated channel window.
            old = apple(f'''tell application "Google Chrome"
                repeat with w in windows
                    repeat with n from 1 to count tabs of w
                        if URL of tab n of w starts with "{self.home}" then
                            set active tab index of w to n
                            set index of w to 1
                            activate
                            return id of w
                        end if
                    end repeat
                end repeat
            end tell''')
            if old.isdigit():
                result = old
            else:
                result = apple(f'''tell application "Google Chrome"
                    set w to make new window
                    set URL of active tab of w to "{self.home}/upload"
                    activate
                    return id of w
                end tell''')
                time.sleep(2)
        if not result.isdigit():
            raise RuntimeError("Open the intended channel in Studio")
        self.window = int(result)
        self.guard()
        identity = {
            "UC_FIRST_CHANNEL_ID_HERE_": "Why AI Matters",
            "UC_SECOND_CHANNEL_ID_HERE": "on the A List",
        }[self.channel]
        if not any(
            identity.lower() in name(r).lower() for r in controls() if r["role"] == "AXStaticText"
        ):
            raise RuntimeError("Studio channel name has not been verified")

    def guard(self):
        state = apple(
            'tell application "Google Chrome" to get {id,URL of active tab} of front window'
        )
        wid, _, url = state.partition(", ")
        details = (
            self.video_id
            and url.split("?")[0] == f"https://studio.youtube.com/video/{self.video_id}/edit"
        )
        if wid != str(self.window) or not (url.startswith(self.home) or details):
            raise RuntimeError("Chrome switched away from the verified Studio window")
        return url

    def read(self):
        self.guard()
        return controls()

    def click(self, row, field=False, sheet=False):
        if not sheet:
            self.guard()
        if not row or not row["enabled"] or row["w"] <= 0 or row["h"] <= 0:
            raise RuntimeError("Studio control unavailable")
        x = row["x"] + min(20, row["w"] / 2) if field else row["x"] + row["w"] / 2
        y = row["y"] + min(9, row["h"] / 2) if field else row["y"] + row["h"] / 2
        command(HELPER, "click", x, y)
        time.sleep(0.22)

    def open_picker(self):
        rows = self.read()
        select = find(rows, "AXButton", "^Select files$")
        if not select:
            if find(rows, "AXTextArea", "^Add a title"):
                raise RuntimeError("Another upload is open")
            if not find(rows, "AXMenuItem", "^Upload videos$"):
                self.click(find(rows, "AXButton", "^Create$"))

            def menu():
                rs = self.read()
                return (
                    find(rs, "AXButton", "^Select files$")
                    or find(rs, "AXMenuItem", "^Upload videos$")
                    or find(rs, "AXStaticText", "^Upload videos$")
                )

            item = wait(menu)
            if name(item).lower() != "select files":
                self.click(item)
            select = wait(lambda: find(self.read(), "AXButton", "^Select files$"))
        self.click(select)
        wait(lambda: find(controls(), "AXSheet", "open"))

    def choose(self, source, checkpoint):
        if not source.is_absolute() or not source.is_file():
            raise ValueError("Missing rendered file")
        # Chrome's Apple event bridge can block while the real Open sheet is visible.
        apple('tell application "System Events" to keystroke "g" using {command down, shift down}')
        time.sleep(0.35)
        sp.run(["pbcopy"], input=str(source), text=True, check=True)
        apple('tell application "System Events" to key code 9 using command down')
        time.sleep(0.35)
        apple('tell application "System Events" to key code 36')

        def selection():
            rows = controls()
            if not find(rows, "AXSheet", "open"):
                return None
            exact = any(
                r["url"].startswith("file:") and Path(unquote(urlparse(r["url"]).path)) == source
                for r in rows
            )
            return find(rows, "AXButton", "^Open$") if exact else None

        button = wait(selection)
        checkpoint("submitting", {})
        self.click(button, sheet=True)

    def draft(self, source):
        def opened():
            rows = self.read()
            for r in rows:
                if re.search(
                    r"(daily upload limit|upload limit reached|reached your daily)",
                    name(r),
                    re.IGNORECASE,
                ):
                    raise RuntimeError(name(r))
            if not find(rows, "AXStaticText", "^" + re.escape(source.name) + "$"):
                return None
            marker = find(rows, "AXStaticText", "^Video link$")
            if not marker:
                return None
            for r in rows[rows.index(marker) + 1 :]:
                if r["role"] == "AXLink":
                    m = re.fullmatch(
                        r"https://(?:www\.)?(?:youtube\.com/(?:shorts/|watch\?v=)|youtu\.be/)([\w-]{11})",
                        r["url"],
                    )
                    if m:
                        return m[1]
            return None

        self.video_id = wait(opened, 90)
        return self.video_id

    def field(self, pattern, value):
        row = find(self.read(), "AXTextArea", pattern)
        if lines(row["text"]) == lines(value):
            return
        self.click(row, field=True)
        current = find(self.read(), "AXTextArea", pattern)
        if not current or not current["focused"]:
            raise RuntimeError("Studio field did not receive focus")
        self.guard()
        sp.run(["pbcopy"], input=value, text=True, check=True)
        apple(
            'tell application "System Events"\nkey code 0 using command down\ndelay 0.12\nkey code 9 using command down\nend tell'
        )
        wait(lambda: lines(find(self.read(), "AXTextArea", pattern)["text"]) == lines(value), 6)

    def metadata(self, title, caption):
        self.field("^Add a title", title)
        original = find(self.read(), "AXTextArea", "^Tell viewers")["text"].strip()
        if "[DESC PLACEHOLDER]" in original:
            description = original.replace("[DESC PLACEHOLDER]", caption, 1)
        elif original.startswith(caption):
            description = original
        else:
            description = (caption + "\n\n" + original).strip()
        self.field("^Tell viewers", description)
        return description

    def save_private(self):
        rs = self.read()
        if not any(
            "this video is set to not made for kids" in name(r).lower()
            for r in rs
            if r["role"] == "AXStaticText"
        ):
            raise RuntimeError("Audience setting needs review")
        self.click(find(rs, "AXRadioButton", "^Visibility$"))
        radio = wait(lambda: find(self.read(), "AXRadioButton", r"^Private\b"))
        self.click(radio)

        def private_selected():
            current = self.read()
            private = find(current, "AXRadioButton", r"^Private\b")
            return current if private and (private["checked"] == 1 or private["selected"]) else None

        rs = wait(private_selected)
        save = find(rs, "AXButton", "^Save$")
        if not save or not save["enabled"]:
            raise RuntimeError("Private Save is unavailable")
        self.click(save)

        def completed():
            rows = self.read()
            if any("your private video is still processing" in name(r).lower() for r in rows):
                close = next(
                    (
                        r
                        for r in rows
                        if r["role"] == "AXButton"
                        and name(r) == "Close"
                        and r["y"] > 0
                        and r["enabled"]
                    ),
                    None,
                )
                if close:
                    self.click(close)
                    return False
            return not find(rows, "AXRadioButton", "^Visibility$")

        wait(completed, 90)

    def verify(self, source, title, description):
        url = f"https://studio.youtube.com/video/{self.video_id}/edit"
        self.guard()
        apple(
            f'tell application "Google Chrome" to set URL of active tab of front window to "{url}"'
        )

        def saved():
            rs = self.read()
            a = find(rs, "AXTextArea", "^Add a title")
            b = find(rs, "AXTextArea", "^Tell viewers")
            return (
                a
                and b
                and a["text"].strip() == title.strip()
                and lines(b["text"]) == lines(description)
                and find(rs, "AXStaticText", "^Private$")
                and find(rs, "AXStaticText", "^" + re.escape(source.name) + "$")
            )

        wait(saved, 25)
        self.guard()
        apple(
            f'tell application "Google Chrome" to set URL of active tab of front window to "{self.home}/upload"'
        )
        wait(lambda: find(self.read(), "AXHeading", "^Channel content$"))
        return {
            "video_id": self.video_id,
            "studio_url": url,
            "privacy": "private",
            "verified": "reopened Studio Details",
        }


def stage(source, title, caption, channel, checkpoint):
    from .media import probe

    info = probe(source)
    if info.vertical and info.duration <= 180:
        raise ValueError("Send this Short through Social Command Center")
    studio = Studio(channel)
    studio.select_window()
    studio.open_picker()
    studio.choose(source, checkpoint)
    vid = studio.draft(source)
    checkpoint(
        "draft", {"video_id": vid, "studio_url": f"https://studio.youtube.com/video/{vid}/edit"}
    )
    description = studio.metadata(title, caption)
    checkpoint("metadata", {"description": description})
    studio.save_private()
    result = studio.verify(source, title, description)
    checkpoint("private", result)
    return result
