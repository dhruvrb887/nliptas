#!/usr/bin/env python3
"""
Signal to Noise recorder for macOS: our own stand-in for ActivityWatch.

Every few seconds it notes which app is in front, its window title, the
website if it's a browser, and whether you've touched the keyboard or mouse
lately. It keeps that in a small log on your Mac, which tracker.py reads.

Nothing here goes anywhere on its own. The log stays in
~/Library/Application Support/SignalToNoise/watcher/ and is pruned after
30 days. Private/incognito browser windows are recorded only as
"Private browsing", with no title or address.

Usage:
  python3 watcher.py --check       take one reading and check permissions
  python3 watcher.py               record in this window (Ctrl+C to stop)
  python3 watcher.py --sync        record, and run tracker.py every 15 minutes
  python3 watcher.py --install     do --sync in the background, from every login
  python3 watcher.py --uninstall   stop that
  python3 watcher.py --recent      show the last things it recorded

Needs macOS and Python 3.9+; nothing to pip install.
"""

import argparse
import json
import os
import plistlib
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("SIGNAL_WATCHER_DIR") or os.path.expanduser(
    "~/Library/Application Support/SignalToNoise/watcher")
LOG_PATH = os.path.expanduser("~/Library/Logs/SignalToNoise/watcher.log")
AGENT_LABEL = "com.signaltonoise.watcher"
AGENT_PATH = os.path.expanduser(f"~/Library/LaunchAgents/{AGENT_LABEL}.plist")
ACCESSIBILITY_PANE = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"

INTERVAL = 5          # seconds between readings
AFK_AFTER = 180       # no keyboard/mouse for this long = away
SPLIT_AFTER = 60      # write long stretches out at least this often
KEEP_DAYS = 30
SEP = "\x1f"          # field separator in AppleScript answers

# Browsers whose address bar AppleScript can read. Firefox can't be asked,
# so for it only the window title is recorded.
CHROMIUM = {
    "com.google.Chrome", "com.google.Chrome.beta", "com.google.Chrome.canary",
    "com.brave.Browser", "com.microsoft.edgemac", "company.thebrowser.Browser",
    "com.vivaldi.Vivaldi", "com.operasoftware.Opera", "org.chromium.Chromium",
}
SAFARI = {"com.apple.Safari", "com.apple.SafariTechnologyPreview"}


# ---------------------------------------------------------------- reading the screen

FRONT_APP = [
    'set sep to ASCII character 31',
    'set appName to ""',
    'set bid to ""',
    'set winTitle to ""',
    'set problem to ""',
    'tell application "System Events"',
    '  set p to first application process whose frontmost is true',
    '  set appName to name of p',
    '  try',
    '    set bid to bundle identifier of p',
    '  end try',
    '  try',
    '    set winTitle to name of window 1 of p',
    '  on error errMsg',
    '    set problem to errMsg',
    '  end try',
    'end tell',
    'return appName & sep & bid & sep & winTitle & sep & problem',
]


def osascript(lines, timeout=4):
    args = ["osascript"]
    for line in lines:
        args += ["-e", line]
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or f"osascript exited {r.returncode}")
    return r.stdout.rstrip("\n")


def browser_lines(bid):
    """AppleScript asking a running browser for its current address."""
    if bid in CHROMIUM:
        return [
            'set sep to ASCII character 31',
            'set m to ""',
            f'tell application id "{bid}"',
            '  try',
            '    set m to mode of front window',
            '  end try',
            '  set u to URL of active tab of front window',
            'end tell',
            'return m & sep & u',
        ]
    if bid in SAFARI:
        return [
            'set sep to ASCII character 31',
            f'tell application id "{bid}" to set u to URL of front document',
            'return "" & sep & u',
        ]
    return None


def idle_seconds():
    """Seconds since the last keyboard or mouse input."""
    out = subprocess.run(["ioreg", "-c", "IOHIDSystem", "-d", "4"],
                         capture_output=True, text=True, timeout=4).stdout
    m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', out)
    return int(m.group(1)) / 1e9 if m else 0.0


def read_screen():
    """One reading: {app, bundle, title, url, idle, problems}."""
    problems = []
    app = bid = title = url = ""
    try:
        app, bid, title, problem = (osascript(FRONT_APP).split(SEP) + ["", "", "", ""])[:4]
        if problem:
            problems.append(("title", problem))
    except Exception as e:
        problems.append(("app", str(e)))
    lines = browser_lines(bid)
    if lines:
        try:
            mode, url = (osascript(lines).split(SEP) + ["", ""])[:2]
            if mode.strip().lower() == "incognito":
                title, url = "Private browsing", ""
        except Exception as e:
            problems.append(("browser", str(e)))
    try:
        idle = idle_seconds()
    except Exception as e:
        idle = 0.0
        problems.append(("idle", str(e)))
    return {"app": app.strip(), "bundle": bid.strip(), "title": title.strip()[:300],
            "url": url.strip()[:500], "idle": idle, "problems": problems}


# ---------------------------------------------------------------- the log

def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def log_path(day):
    return os.path.join(DATA_DIR, f"events-{day}.jsonl")


class Recorder:
    """Turns readings into stretches ("events") and appends them to the log.

    An event is one unbroken stretch of the same app, title and address, or of
    being away. Each line in the log is one event:
      {"start": "...", "end": "...", "afk": false, "app": "...", "title": "...", "url": "..."}
    Long stretches are written out every minute, so a crash or shutdown loses
    at most a minute.
    """

    def __init__(self, write=None, interval=INTERVAL, afk_after=AFK_AFTER):
        self.cur = None
        self.write = write or append_event
        self.interval = interval
        self.afk_after = afk_after

    def _close(self, end):
        if self.cur and end > self.cur["start"]:
            ev = dict(self.cur)
            ev["end"] = end
            self.write(ev)
        self.cur = None

    def _open(self, start, key):
        afk, app, title, url = key
        self.cur = {"start": start, "last": start, "afk": afk, "app": app, "title": title, "url": url}

    def sample(self, now, reading):
        afk = reading["idle"] >= self.afk_after
        key = (True, "", "", "") if afk else (False, reading["app"], reading["title"], reading["url"])
        cur = self.cur

        # A gap much longer than the reading interval means the Mac slept or
        # we were paused: end the last stretch where we last saw it.
        if cur and (now - cur["last"]).total_seconds() > self.interval * 3:
            self._close(cur["last"] + timedelta(seconds=self.interval))
            cur = None

        if cur is None:
            self._open(now - timedelta(seconds=reading["idle"]) if afk else now, key)
            return

        same = (cur["afk"], cur["app"], cur["title"], cur["url"]) == key
        if same:
            cur["last"] = now
            if (now - cur["start"]).total_seconds() >= SPLIT_AFTER:
                self._close(now)
                self._open(now, key)
            return

        if afk and not cur["afk"]:
            # Just noticed we're away: that began `idle` seconds ago, possibly
            # before stretches already written out. The away event starts
            # back then anyway; tracker.py subtracts away time from active time.
            began = now - timedelta(seconds=reading["idle"])
            self._close(max(cur["start"], began))
            self._open(began, key)
            self.cur["last"] = now
        else:
            self._close(now)
            self._open(now, key)

    def stop(self, now=None):
        if self.cur:
            self._close(min(now or self.cur["last"], self.cur["last"] + timedelta(seconds=self.interval)))


def append_event(ev):
    os.makedirs(DATA_DIR, exist_ok=True)
    row = {"start": iso(ev["start"]), "end": iso(ev["end"]), "afk": ev["afk"],
           "app": ev["app"], "title": ev["title"], "url": ev["url"]}
    with open(log_path(ev["start"].astimezone(timezone.utc).date().isoformat()), "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_events(start, end):
    """Every logged event overlapping [start, end), clipped to it, oldest first."""
    out = []
    day = (start - timedelta(days=1)).astimezone(timezone.utc).date()
    last = end.astimezone(timezone.utc).date()
    while day <= last:
        try:
            with open(log_path(day.isoformat()), encoding="utf-8") as f:
                for line in f:
                    try:
                        ev = json.loads(line)
                        s, e = max(parse_ts(ev["start"]), start), min(parse_ts(ev["end"]), end)
                    except (ValueError, KeyError):
                        continue  # a half-written line from a crash
                    if s < e:
                        ev["start"], ev["end"] = s, e
                        out.append(ev)
        except FileNotFoundError:
            pass
        day += timedelta(days=1)
    out.sort(key=lambda ev: ev["start"])
    return out


def prune():
    cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).date().isoformat()
    try:
        for name in os.listdir(DATA_DIR):
            m = re.fullmatch(r"events-(\d{4}-\d{2}-\d{2})\.jsonl", name)
            if m and m.group(1) < cutoff:
                os.remove(os.path.join(DATA_DIR, name))
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------- running

def say(*a):
    print(datetime.now().strftime("%H:%M:%S"), *a, flush=True)


def permission_hint(problems):
    text = " ".join(p for _, p in problems).lower()
    if "assistive" in text or "-25211" in text:
        return ("Window titles are blocked. Turn on Accessibility for "
                + ("Terminal" if os.environ.get("TERM_PROGRAM") else os.path.realpath(sys.executable))
                + " in System Settings → Privacy & Security → Accessibility.")
    if "-1743" in text or "not authorized" in text or "not allowed to send" in text:
        return ("macOS blocked asking an app (System Events or your browser). Allow it in "
                "System Settings → Privacy & Security → Automation.")
    return None


def record(sync=False):
    if sys.platform != "darwin":
        sys.exit("watcher.py only works on macOS. On Windows or Linux, use ActivityWatch.")
    rec = Recorder()
    stopping = threading.Event()

    def stop(*_):
        stopping.set()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    if sync:
        threading.Thread(target=sync_loop, args=(stopping,), daemon=True).start()

    say(f"Recording every {INTERVAL}s to {DATA_DIR}" + (" and syncing." if sync else ". Ctrl+C to stop."))
    prune()
    warned = None
    last_prune = time.time()
    while not stopping.is_set():
        reading = read_screen()
        hint = permission_hint(reading["problems"])
        if hint and hint != warned:
            say(hint)
            warned = hint
        if reading["app"]:
            rec.sample(datetime.now(timezone.utc), reading)
        if time.time() - last_prune > 86400:
            prune()
            last_prune = time.time()
        stopping.wait(INTERVAL)
    rec.stop(datetime.now(timezone.utc))
    say("Stopped.")


def sync_loop(stopping):
    """Run tracker.py's sync every interval_minutes, alongside recording."""
    sys.path.insert(0, HERE)
    import tracker
    stopping.wait(90)  # let a little get recorded first
    while not stopping.is_set():
        minutes = 15
        try:
            cfg = tracker.load_config()
            minutes = max(1, float(cfg["interval_minutes"]))
            tracker.run_once(cfg)
        except tracker.TrackerError as e:
            say("Sync skipped:", e)
        except Exception as e:
            say("Sync skipped (unexpected):", repr(e))
        stopping.wait(minutes * 60)


def check():
    if sys.platform != "darwin":
        sys.exit("watcher.py only works on macOS. On Windows or Linux, use ActivityWatch.")
    print("Reading your screen once...\n")
    r = read_screen()
    print(f"  App:      {r['app'] or '(unknown)'}")
    print(f"  Window:   {r['title'] or '(none)'}")
    if r["url"] or browser_lines(r["bundle"]):
        print(f"  Address:  {r['url'] or '(none)'}")
    print(f"  Idle for: {r['idle']:.0f} seconds\n")
    hint = permission_hint(r["problems"])
    if hint:
        print(hint)
        if "Accessibility" in hint:
            subprocess.run(["open", ACCESSIBILITY_PANE])
            print("(Opened that page for you. Switch the toggle on, then run --check again.)")
    elif r["problems"]:
        for what, p in r["problems"]:
            print(f"Couldn't read the {what}: {p}")
    else:
        print("All good. Tip: switch to your browser within 5 seconds of running this "
              "to check it can read website addresses too:\n  sleep 5; python3 watcher.py --check")


def recent(n=20):
    now = datetime.now(timezone.utc)
    evs = read_events(now - timedelta(hours=6), now)[-n:]
    if not evs:
        print("Nothing recorded in the last 6 hours.")
        return
    for ev in evs:
        mins = (ev["end"] - ev["start"]).total_seconds() / 60
        what = "(away)" if ev["afk"] else f"{ev['app']} — {ev['title']}"
        if ev.get("url"):
            what += f"  [{ev['url'][:50]}]"
        print(f"{ev['start'].astimezone():%H:%M:%S}  {mins:4.1f}m  {what[:110]}")


def install():
    if sys.platform != "darwin":
        sys.exit("watcher.py only works on macOS.")
    if not os.path.exists(os.path.join(HERE, "config.json")):
        sys.exit("Make config.json first (see README.md), then run --install again.")
    python = os.path.realpath(sys.executable)
    os.makedirs(os.path.dirname(AGENT_PATH), exist_ok=True)
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    plist = {
        "Label": AGENT_LABEL,
        "ProgramArguments": [python, os.path.join(HERE, "watcher.py"), "--sync"],
        "WorkingDirectory": HERE,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "StandardOutPath": LOG_PATH,
        "StandardErrorPath": LOG_PATH,
    }
    subprocess.run(["launchctl", "unload", AGENT_PATH], capture_output=True)
    with open(AGENT_PATH, "wb") as f:
        plistlib.dump(plist, f)
    r = subprocess.run(["launchctl", "load", "-w", AGENT_PATH], capture_output=True, text=True)
    if r.returncode != 0:  # newer macOS prefers bootstrap
        r = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", AGENT_PATH], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"launchctl couldn't start it: {r.stderr.strip()}")
    print("Installed. It's running now and will start by itself every time you log in.\n")
    print("One more permission, because it now runs without Terminal:")
    print("  System Settings → Privacy & Security → Accessibility: turn on the entry for")
    print(f"  {python}")
    print("  If it isn't listed, click +, press Cmd+Shift+G, paste that path and press Enter.")
    print("  If macOS asks whether Python may control System Events or your browser, click Allow.\n")
    print(f"See what it's doing:  tail -f '{LOG_PATH}'")
    print("Stop it for good:     python3 watcher.py --uninstall")
    subprocess.run(["open", ACCESSIBILITY_PANE])


def uninstall():
    subprocess.run(["launchctl", "unload", "-w", AGENT_PATH], capture_output=True)
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{AGENT_LABEL}"], capture_output=True)
    try:
        os.remove(AGENT_PATH)
        print("Uninstalled: it's stopped and won't start at login. Your log is still in", DATA_DIR)
    except FileNotFoundError:
        print("It wasn't installed.")


def main():
    p = argparse.ArgumentParser(description="Record what's on screen for Signal to Noise (macOS).")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true", help="take one reading and check permissions")
    g.add_argument("--sync", action="store_true", help="record, and run tracker.py every interval_minutes")
    g.add_argument("--install", action="store_true", help="run --sync in the background from every login")
    g.add_argument("--uninstall", action="store_true", help="stop the background version")
    g.add_argument("--recent", action="store_true", help="show the last things recorded")
    a = p.parse_args()
    if a.check:
        check()
    elif a.install:
        install()
    elif a.uninstall:
        uninstall()
    elif a.recent:
        recent()
    else:
        record(sync=a.sync)


if __name__ == "__main__":
    main()
