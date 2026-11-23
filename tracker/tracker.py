#!/usr/bin/env python3
"""
Signal to Noise desktop tracker.

Reads what you were doing from ActivityWatch (which records the app and window
title in front of you, and when you're away from the keyboard), decides for each
5-minute slot whether it was signal or noise, and fills those slots in on the
site. Only slots you haven't scored yourself are filled; your own scoring
always wins.

What leaves your computer:
  - To the site: only the date, the 5-minute slot, signal/noise and a short
    label (e.g. "Coding"). Never window titles or app names.
  - To the AI (Anthropic's API): app names and window titles that none of your
    rules matched, so it can classify them. Set "ai": false in config.json to
    use rules only.

Usage:
  python3 tracker.py              fill in everything since the last run
  python3 tracker.py --dry-run    show what it would fill in, send nothing
  python3 tracker.py --since 6h   look back 6 hours (ignores the last run)
  python3 tracker.py --loop       keep running, syncing every 15 minutes

Needs Python 3.9+ and nothing else installed. See README.md.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(HERE, ".state.json")
CACHE_PATH = os.path.join(HERE, ".cache.json")

SLOT = 5 * 60  # seconds
BROWSERS = ("chrome", "firefox", "safari", "edge", "brave", "arc", "opera", "vivaldi", "chromium", "zen")

DEFAULTS = {
    "site": "https://nliptas.pages.dev",
    "access_key": "",
    "activitywatch": "http://localhost:5600",
    "ai": True,
    "anthropic_api_key": "",
    "anthropic_base_url": "https://api.anthropic.com",
    "model": "claude-haiku-5-5",
    "about_me": "",
    "rules": [],
    "ignore": ["loginwindow", "lockscreen", "screensaver"],
    "min_active_minutes": 2.5,
    "first_run_hours": 3,
    "max_back_hours": 48,
    "interval_minutes": 15,
}


class TrackerError(Exception):
    pass


# ---------------------------------------------------------------- utilities

def log(*a):
    print(datetime.now().strftime("%H:%M:%S"), *a, flush=True)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as e:
        raise TrackerError(f"{os.path.basename(path)} isn't valid JSON: {e}")


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def http(method, url, body=None, headers=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    h = {"accept": "application/json", "user-agent": "signal-to-noise-tracker/1"}
    if body is not None:
        h["content-type"] = "application/json"
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"error": raw.decode(errors="replace")[:300]}


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def floor_slot(dt):
    """Start of the 5-minute slot dt falls in (dt timezone-aware)."""
    ts = dt.timestamp()
    return datetime.fromtimestamp(ts - ts % SLOT, tz=timezone.utc)


def intersect(a, b):
    """Overlap of two sorted lists of (start, end) intervals."""
    out, i, j = [], 0, 0
    while i < len(a) and j < len(b):
        s, e = max(a[i][0], b[j][0]), min(a[i][1], b[j][1])
        if s < e:
            out.append((s, e))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


def merge(intervals):
    out = []
    for s, e in sorted(intervals):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def parse_since(text):
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([mhd])", text.strip().lower())
    if not m:
        raise TrackerError("--since takes something like 90m, 6h or 2d")
    n, unit = float(m.group(1)), m.group(2)
    return timedelta(minutes=n) if unit == "m" else timedelta(hours=n) if unit == "h" else timedelta(days=n)


def norm_kind(k):
    k = str(k or "").strip().lower()
    if k in ("s", "signal", "productive"):
        return "s"
    if k in ("n", "noise", "unproductive"):
        return "n"
    return None


# ---------------------------------------------------------------- ActivityWatch

class ActivityWatch:
    def __init__(self, base):
        self.base = base.rstrip("/")

    def get(self, path, **params):
        url = f"{self.base}/api/0/{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        try:
            status, data = http("GET", url, timeout=30)
        except urllib.error.URLError:
            raise TrackerError(f"Can't reach ActivityWatch at {self.base}. Is it running?")
        if status != 200:
            raise TrackerError(f"ActivityWatch answered {status} for {path}")
        return data

    def buckets(self):
        found = {"window": [], "afk": [], "web": []}
        for bid, b in (self.get("buckets/") or {}).items():
            t = b.get("type", "")
            if t == "currentwindow":
                found["window"].append(bid)
            elif t == "afkstatus":
                found["afk"].append(bid)
            elif t.startswith("web.tab"):
                found["web"].append(bid)
        if not found["window"]:
            raise TrackerError("ActivityWatch has no window watcher data yet. Leave it running for a few minutes and try again.")
        return found

    def events(self, bid, start, end):
        rows = self.get(f"buckets/{urllib.parse.quote(bid, safe='')}/events",
                        start=start.isoformat(), end=end.isoformat(), limit=-1)
        out = []
        for ev in rows or []:
            s = parse_ts(ev["timestamp"])
            e = s + timedelta(seconds=float(ev.get("duration") or 0))
            s, e = max(s, start), min(e, end)
            if s < e:
                out.append((s, e, ev.get("data") or {}))
        out.sort(key=lambda x: x[0])
        return out


def activity_by_slot(aw, start, end, ignore):
    """{slot_start_utc: {activity: active_seconds}} for every slot in [start, end)."""
    b = aw.buckets()
    windows = [x for bid in b["window"] for x in aw.events(bid, start, end)]

    if b["afk"]:
        active = merge([(s, e) for bid in b["afk"] for s, e, d in aw.events(bid, start, end)
                        if d.get("status") == "not-afk"])
    else:
        active = [(start, end)]

    web = sorted((s, e, d) for bid in b["web"] for s, e, d in aw.events(bid, start, end))

    pieces = []  # (start, end, activity)
    for s, e, d in windows:
        app = str(d.get("app") or "").strip()
        title = str(d.get("title") or "").strip()
        name = f"{app} — {title}" if title else app
        if not name or any(w.lower() in name.lower() for w in ignore):
            continue
        for a0, a1 in intersect([(s, e)], active):
            if web and any(x in app.lower() for x in BROWSERS):
                covered = []
                for w0, w1, wd in web:
                    for p0, p1 in intersect([(a0, a1)], [(w0, w1)]):
                        host = urllib.parse.urlparse(str(wd.get("url") or "")).hostname or ""
                        wt = str(wd.get("title") or title)
                        pieces.append((p0, p1, f"{app} — {host} — {wt}" if host else name))
                        covered.append((p0, p1))
                # Whatever the web watcher didn't cover keeps the window title.
                gaps, cur = [], a0
                for c0, c1 in merge(covered):
                    if c0 > cur:
                        gaps.append((cur, c0))
                    cur = max(cur, c1)
                if cur < a1:
                    gaps.append((cur, a1))
                pieces.extend((g0, g1, name) for g0, g1 in gaps)
            else:
                pieces.append((a0, a1, name))

    slots = defaultdict(lambda: defaultdict(float))
    for s, e, name in pieces:
        t = s
        while t < e:
            slot = floor_slot(t)
            nxt = min(e, slot + timedelta(seconds=SLOT))
            slots[slot][name[:200]] += (nxt - t).total_seconds()
            t = nxt
    return slots


# ---------------------------------------------------------------- classification

def match_rules(name, rules):
    low = name.lower()
    for r in rules:
        m = str(r.get("match") or "").lower()
        k = norm_kind(r.get("kind"))
        if m and k and m in low:
            return k, str(r.get("label") or "").strip()[:60]
    return None


AI_SYSTEM = """You sort a person's computer activity into signal (time well spent, by their own standards) and noise (time wasted).

You get numbered activities, each "App — window title" (sometimes "Browser — website — page title"). For each, decide:
- kind: "s" for signal or "n" for noise. Judge by what the person says matters to them. When it's genuinely unclear (a blank browser tab, a file manager), lean on the app and title together and pick the more likely one.
- label: what the time went to, 1 to 3 words. If one of the person's existing labels fits, use it exactly as written. Only invent a new label when none fits, in the same style as theirs.

Answer with only a JSON array, one object per activity, like:
[{"i": 1, "kind": "s", "label": "Coding"}, {"i": 2, "kind": "n", "label": "YouTube"}]"""


def classify_ai(cfg, names, labels):
    key = cfg["anthropic_api_key"] or os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise TrackerError('No Anthropic API key. Put it in config.json as "anthropic_api_key", or set "ai": false to use rules only.')
    about = cfg["about_me"].strip() or "No description given: use common sense about productive vs. wasted time."
    user = (
        f"About me: {about}\n\n"
        f"My signal labels: {', '.join(labels.get('signal') or []) or '(none yet)'}\n"
        f"My noise labels: {', '.join(labels.get('noise') or []) or '(none yet)'}\n\n"
        "Activities:\n" + "\n".join(f"{i + 1}. {n[:160]}" for i, n in enumerate(names))
    )
    status, data = http(
        "POST", cfg["anthropic_base_url"].rstrip("/") + "/v1/messages",
        {"model": cfg["model"], "max_tokens": 4000, "system": AI_SYSTEM,
         "messages": [{"role": "user", "content": user}]},
        {"x-api-key": key, "anthropic-version": "2023-06-01"}, timeout=120,
    )
    if status != 200:
        err = (data or {}).get("error")
        raise TrackerError(f"The AI request failed ({status}): {err.get('message') if isinstance(err, dict) else err}")
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        raise TrackerError("The AI's answer had no JSON in it.")
    out = {}
    for row in json.loads(m.group(0)):
        try:
            i = int(row["i"]) - 1
        except (KeyError, TypeError, ValueError):
            continue
        k = norm_kind(row.get("kind"))
        if 0 <= i < len(names) and k:
            out[names[i]] = (k, str(row.get("label") or "").strip()[:60])
    return out


def classify(cfg, names, labels, cache):
    """{activity: (kind, label)} for every activity we can place."""
    result, todo = {}, []
    for n in names:
        hit = match_rules(n, cfg["rules"])
        if hit:
            result[n] = hit
        elif n in cache:
            result[n] = tuple(cache[n])
        else:
            todo.append(n)
    if todo and cfg["ai"]:
        for i in range(0, len(todo), 60):
            got = classify_ai(cfg, todo[i:i + 60], labels)
            for n, v in got.items():
                cache[n] = list(v)
            result.update(got)
    return result


def decide(slot_acts, verdicts, min_active):
    """(kind, label, top_activity) for one slot, or None if it was mostly idle."""
    by_kind = defaultdict(float)
    by_label = defaultdict(float)
    for name, secs in slot_acts.items():
        v = verdicts.get(name)
        if v:
            by_kind[v[0]] += secs
            by_label[v] += secs
    if sum(by_kind.values()) < min_active:
        return None
    kind = max(by_kind, key=by_kind.get)
    label = max((lv for lv in by_label if lv[0] == kind), key=by_label.get)[1]
    top = max(slot_acts, key=slot_acts.get)
    return kind, label, top


# ---------------------------------------------------------------- the site

def site_call(cfg, method, body=None):
    if not cfg["access_key"].startswith("slk_"):
        raise TrackerError("No access key. Make one on the site under Settings → Desktop tracker and put it in config.json.")
    try:
        status, data = http(method, cfg["site"].rstrip("/") + "/api/autolog", body,
                            {"authorization": "Bearer " + cfg["access_key"]})
    except urllib.error.URLError as e:
        raise TrackerError(f"Can't reach {cfg['site']}: {e.reason}")
    if status == 401:
        raise TrackerError("The site didn't accept the access key. It may have been revoked: make a new one in Settings.")
    if status != 200:
        raise TrackerError(f"The site answered {status}: {(data or {}).get('error')}")
    return data


# ---------------------------------------------------------------- one run

def run_once(cfg, since=None, dry_run=False):
    state = load_json(STATE_PATH, {})
    cache = load_json(CACHE_PATH, {})
    now = datetime.now(timezone.utc)
    # Stop a slot short of now: the slot in progress isn't finished, and
    # ActivityWatch can still be extending its last event.
    end = floor_slot(now) - timedelta(seconds=SLOT)
    oldest = end - timedelta(hours=cfg["max_back_hours"])
    if since is not None:
        start = floor_slot(now - since)
    elif state.get("last_end"):
        start = max(parse_ts(state["last_end"]), oldest)
    else:
        start = floor_slot(now - timedelta(hours=cfg["first_run_hours"]))
    start = max(start, oldest)
    if end <= start:
        log("Nothing new to sync yet.")
        return

    aw = ActivityWatch(cfg["activitywatch"])
    slots = activity_by_slot(aw, start, end, cfg["ignore"])
    # A dry run works without an access key; it just won't know your labels.
    info = site_call(cfg, "GET") if not dry_run or cfg["access_key"].startswith("slk_") else {}
    labels = info.get("labels") or {}

    names = sorted({n for acts in slots.values() for n in acts})
    verdicts = classify(cfg, names, labels, cache)
    save_json(CACHE_PATH, cache)

    min_active = float(cfg["min_active_minutes"]) * 60
    out, rows = [], []
    for slot in sorted(slots):
        d = decide(slots[slot], verdicts, min_active)
        if not d:
            continue
        local = slot.astimezone()
        kind, label, top = d
        out.append({"date": local.strftime("%Y-%m-%d"), "slot": f"{local.hour}:{local.minute:02d}",
                    "kind": kind, "label": label})
        rows.append((local, kind, label, top))

    span = f"{start.astimezone():%a %H:%M} → {end.astimezone():%H:%M}"
    if dry_run:
        print(f"\nWould fill in {len(out)} slots ({span}):\n")
        for local, kind, label, top in rows:
            print(f"  {local:%a %H:%M}  {'SIGNAL' if kind == 's' else 'noise '}  {label or '-':<18}  {top[:70]}")
        unplaced = [n for n in names if n not in verdicts]
        if unplaced:
            print(f"\n{len(unplaced)} activities couldn't be classified (no rule matched and AI is off):")
            for n in unplaced[:15]:
                print("  ", n[:90])
        print("\nDry run: nothing was sent.")
        return

    filled = skipped = 0
    for i in range(0, len(out), 1000):
        res = site_call(cfg, "POST", {"slots": out[i:i + 1000]})
        filled += res.get("filled", 0)
        skipped += res.get("skipped", 0)
    state["last_end"] = end.isoformat()
    save_json(STATE_PATH, state)
    signal = sum(1 for r in rows if r[1] == "s")
    log(f"{span}: {len(out)} active slots ({signal * 5} min signal, {(len(out) - signal) * 5} min noise). "
        f"Filled {filled}, left {skipped} you'd already scored.")


def load_config():
    if not os.path.exists(CONFIG_PATH):
        raise TrackerError(f"No config.json yet. Copy config.example.json to config.json in {HERE} and fill it in.")
    cfg = dict(DEFAULTS)
    cfg.update(load_json(CONFIG_PATH, {}))
    if not isinstance(cfg["rules"], list):
        raise TrackerError('"rules" in config.json should be a list.')
    return cfg


def main():
    p = argparse.ArgumentParser(description="Fill in Signal to Noise from ActivityWatch.")
    p.add_argument("--dry-run", action="store_true", help="show what would be filled in, send nothing")
    p.add_argument("--since", help="look back this far instead of since the last run, e.g. 90m, 6h, 2d")
    p.add_argument("--loop", action="store_true", help="keep running and sync every interval_minutes")
    args = p.parse_args()
    try:
        cfg = load_config()
        since = parse_since(args.since) if args.since else None
        if not args.loop:
            run_once(cfg, since, args.dry_run)
            return
        log(f"Syncing every {cfg['interval_minutes']} minutes. Ctrl+C to stop.")
        while True:
            try:
                run_once(cfg, since, args.dry_run)
            except TrackerError as e:
                log("Skipped this round:", e)
            except Exception as e:  # keep the loop alive through network blips
                log("Skipped this round (unexpected):", repr(e))
            since = None
            time.sleep(max(1, float(cfg["interval_minutes"])) * 60)
    except TrackerError as e:
        print("tracker:", e, file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
