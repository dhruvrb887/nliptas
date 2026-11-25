# Desktop tracker

Fills in your Score hours automatically from what's actually on your screen.

Two parts:

- **`watcher.py` records** (Mac). Every 5 seconds it notes which app is in
  front, its window title, the website if it's a browser, and whether you've
  touched the keyboard or mouse in the last 3 minutes. The log stays on your
  Mac and is pruned after 30 days. On Windows or Linux, use
  [ActivityWatch](https://activitywatch.net) for this part instead.
- **`tracker.py` fills in the site.** Every 15 minutes it reads the log, decides
  whether each 5-minute slot was signal or noise, and sends that to the site.

Once installed, both run together in the background from every login. You
don't need to open anything.

How it fills in your hours:

- It only fills slots you **haven't scored**. Anything you scored yourself is
  never touched, so you can always fix things by hand.
- Auto-logged blocks are **shaded lighter** with a dashed edge. Switching,
  relabeling or splitting one makes it a normal block.
- Slots where you were away, or active for under 2½ of the 5 minutes, stay empty.

## What leaves your Mac

- **To the site:** only the date, the slot, signal or noise, and a short label
  like "Coding". Never app names, window titles or websites, so your friend
  sees "Noise · YouTube", not what you watched.
- **To the AI** (Anthropic's API): app names, window titles and website names
  (not full addresses) that none of your rules matched, so it can classify
  them. Each one is only asked about once (answers are cached in `.cache.json`).
  Set `"ai": false` to use your rules only.
- **Private/incognito windows** in Chrome, Brave, Edge, Arc and similar are
  recorded only as "Private browsing", with no title or address. (Safari
  doesn't let apps tell, so Safari private windows are recorded like any other.)

## Setup on a Mac (about 10 minutes)

1. **Make an access key** on the site: Settings → Desktop tracker → New access
   key. Copy it now; it's only shown once.
2. **Get an Anthropic API key** at console.anthropic.com (skip this if you set
   `"ai": false`). The default model is a small, cheap one; check your usage in
   the console after the first day.
3. **Configure:** in this folder, copy `config.example.json` to `config.json`.
   Set `"site"` to your site's address and paste in both keys. Write
   `about_me` in your own words: it's what the AI uses to judge what counts as
   signal *for you*. Add `rules` for things you never want the AI to guess at.
   The first rule whose `match` appears in "App — website — title" wins
   (it's case-insensitive). A rule for "Private browsing" is a good idea.
4. **Check it can see your screen:** open Terminal, type `cd ` (with a space),
   drag this folder into the window, press Enter, then run:

   ```sh
   python3 watcher.py --check
   ```

   It prints the app, window and address it sees. If window titles are blocked,
   it opens System Settings for you: switch **Terminal** on under
   Accessibility, then run it again. When it asks whether Terminal may control
   System Events or your browser, click **Allow**. To check websites, run
   `sleep 5; python3 watcher.py --check` and switch to your browser.
5. **Try it for a bit:** run `python3 watcher.py` and use your computer as
   normal for 20 minutes or so. Then press Ctrl+C and run
   `python3 tracker.py --dry-run --since 30m` to see what it *would* fill in,
   without sending anything. Tweak `about_me` and `rules` until it looks right.
6. **Install it:** `python3 watcher.py --install`. From then on it records and
   syncs in the background, starting whenever you log in. Because it no longer
   runs inside Terminal, macOS asks for permission once more: turn on the
   Python entry it names under Accessibility, and click Allow on any prompts.

Needs Python 3.9 or newer and nothing else (no `pip install`). If `python3`
isn't found, macOS will offer to install the developer tools: say yes.

### Handy commands

| Command | What it does |
| --- | --- |
| `python3 watcher.py --recent` | The last things it recorded. |
| `tail -f ~/Library/Logs/SignalToNoise/watcher.log` | What the background version is doing (Ctrl+C to stop watching). |
| `python3 tracker.py --dry-run --since 3h` | What it would fill in for the last 3 hours. |
| `python3 watcher.py --uninstall` | Stop the background version for good. |

If you move this folder, run `--install` again.

## Windows and Linux

Install [ActivityWatch](https://activitywatch.net) and leave it running (add
its browser extension, aw-watcher-web, for website-level detail). Set
`"source": "activitywatch"` in `config.json`, do steps 1–3 above, try
`python3 tracker.py --dry-run --since 3h`, then keep `python3 tracker.py --loop`
running. To start it at login: on Windows, Task Scheduler → Create Basic Task →
"When I log on" → `pythonw.exe` with `C:\path\to\tracker\tracker.py --loop`; on
Linux, `@reboot /usr/bin/python3 /path/to/tracker/tracker.py --loop` in
`crontab -e`.

## Good to know

- Phones aren't tracked. Score those hours by hand as before.
- If the page is open with unsaved edits to the same day when the tracker
  syncs, the page's save can overwrite the auto blocks for that day. They'll
  just be missing; nothing you scored is lost.
- Revoke a key any time in Settings; the tracker stops at its next sync.
