# Desktop tracker

Fills in your Score hours automatically from what's actually on your screen.

[ActivityWatch](https://activitywatch.net) (free, open source) records which
app and window is in front of you and when you step away. Every 15 minutes
`tracker.py` reads that, decides whether each 5-minute slot was signal or
noise, and fills it in on the site.

- It only fills slots you **haven't scored**. Anything you've scored yourself
  is never touched, so you can always correct it by hand.
- Auto-logged blocks are **shaded lighter** with a dashed edge. Switching,
  relabeling or splitting one makes it a normal block.
- Slots where you were away (ActivityWatch's AFK detection) or active for under
  2½ of the 5 minutes are left empty.

## What leaves your computer

- **To the site:** only the date, the slot, signal or noise, and a short label
  like "Coding". Never app names or window titles, so your friend sees
  "Noise · YouTube", not what you watched.
- **To the AI** (Anthropic's API): app names and window titles that none of
  your rules matched, so it can classify them. Answers are cached in
  `.cache.json`, so the same window is only asked about once. Set `"ai": false`
  to use your rules only; anything they don't match is left empty.

## Setup (about 10 minutes)

1. **Install ActivityWatch** from activitywatch.net and start it. On a Mac,
   allow it under System Settings → Privacy & Security → Accessibility so it
   can read window titles. For website-level detail, also add the
   ActivityWatch browser extension (aw-watcher-web).
2. **Make an access key** on the site: Settings → Desktop tracker → New access
   key. Copy it now; it's only shown once. A key can only fill in empty hours.
3. **Get an Anthropic API key** at console.anthropic.com (skip this if you set
   `"ai": false`). The default model is a small, cheap one, and
   with caching it sends very little text; check your usage in the console
   after the first day.
4. **Configure:** copy `config.example.json` to `config.json` and fill in both
   keys. Write `about_me` in your own words: it's what the AI uses to judge
   what counts as signal *for you*. Add `rules` for things you never want the
   AI to guess at. The first rule whose `match` appears in "App — title"
   wins (it's case-insensitive).
5. **Try it:** `python3 tracker.py --dry-run --since 3h` shows what it would
   fill in without sending anything. Tweak `about_me` and `rules` until it
   looks right.
6. **Run it:** `python3 tracker.py --loop` syncs every 15 minutes. The first
   run looks back 3 hours; after that it picks up where it left off (up to 48
   hours back, if your computer was off).

Needs Python 3.9 or newer and nothing else (no `pip install`).

## Run it at login

**Mac:** save this as `~/Library/LaunchAgents/nliptas.tracker.plist`
(fix the path), then run `launchctl load ~/Library/LaunchAgents/nliptas.tracker.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>nliptas.tracker</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/python3</string>
    <string>/Users/YOU/nliptas/tracker/tracker.py</string>
    <string>--loop</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/nliptas-tracker.log</string>
  <key>StandardErrorPath</key><string>/tmp/nliptas-tracker.log</string>
</dict></plist>
```

**Windows:** Task Scheduler → Create Basic Task → "When I log on" → Start a
program: `pythonw.exe`, arguments `C:\path\to\tracker\tracker.py --loop`.

**Linux:** add `@reboot /usr/bin/python3 /path/to/tracker/tracker.py --loop`
to `crontab -e`, or make a systemd user service.

## Good to know

- Phones aren't tracked. Score those hours by hand as before.
- If the page is open with unsaved edits to the same day when the tracker
  syncs, the page's save can overwrite the auto blocks for that day. They'll
  just be missing; nothing you scored is lost.
- Revoke a key any time in Settings; the tracker stops at its next sync.
