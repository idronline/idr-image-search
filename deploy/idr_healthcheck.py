#!/usr/bin/env python3
"""Check that the IDR tools answer and the disk isn't full; tell Slack when that changes.

Same file in email_format_tool/deploy/ and idr-image-search/deploy/. Run from cron every
15 minutes, with ~/idr-tools.env loaded (it holds SLACK_WEBHOOK_URL):

  idr_healthcheck.py Translate=https://tools.idronline.org/translate/login ...
  idr_healthcheck.py --test        # send one test message to Slack

A tool counts as down if it doesn't answer, or answers with a 5xx error, twice in a row
10 seconds apart. Slack gets one message when something goes down and one when it is
back - not one every 15 minutes. Standard library only.
"""

import json
import os
import shutil
import socket
import sys
import time
import urllib.error
import urllib.request

STATE_FILE = os.path.expanduser("~/.idr-health-state.json")
DISK_LIMIT = 90  # % used on / before warning
SERVER = socket.gethostname()


def slack(text):
    url = os.environ.get("SLACK_WEBHOOK_URL")
    if not url:
        print("SLACK_WEBHOOK_URL not set - message not sent:", text)
        return False
    req = urllib.request.Request(url, data=json.dumps({"text": text}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15).read()
        return True
    except Exception as e:
        print(f"Slack message failed: {type(e).__name__}: {e}")
        return False


def problem(url):
    """None if the page answers, else a short reason."""
    try:
        urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "idr-healthcheck"}),
                               timeout=20).read(1024)
        return None
    except urllib.error.HTTPError as e:
        return None if e.code < 500 else f"HTTP {e.code}"
    except Exception as e:
        return f"{type(e).__name__}: {getattr(e, 'reason', e)}"


def checks(args):
    for arg in args:
        name, _, url = arg.partition("=")
        reason = problem(url)
        if reason:
            time.sleep(10)
            reason = problem(url)
        yield name, reason, url
    used = shutil.disk_usage("/")
    pct = round(used.used / used.total * 100)
    yield f"Disk on {SERVER}", (f"{pct}% full" if pct >= DISK_LIMIT else None), ""


def main(args):
    if args == ["--test"]:
        ok = slack(f":white_check_mark: Test message from the IDR tools health check on {SERVER}.")
        print("sent" if ok else "not sent")
        return
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    now = time.time()
    for name, reason, url in checks(args):
        was = state.get(name)
        if reason and not was:
            slack(f":red_circle: *{name}* is down - {reason}\n{url} (checked from {SERVER})")
            state[name] = {"since": now, "reason": reason}
        elif not reason and was:
            mins = round((now - was["since"]) / 60)
            slack(f":large_green_circle: *{name}* is OK again (was down about {mins} min)")
            state.pop(name)
        print(time.strftime("%Y-%m-%d %H:%M"), name, "DOWN " + reason if reason else "ok")
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


if __name__ == "__main__":
    main(sys.argv[1:])
