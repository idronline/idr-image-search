#!/usr/bin/env python3
"""
weekly_update.py — refresh the CSV exports and index new images.

Replaces the manual "export, scp, run indexer" routine. Run weekly from
the ubuntu crontab (Sunday 22:00 India time, see README):

  1. For each WP All Export export in exports.json: call its trigger URL,
     call its processing URL until the export is complete, then download
     the file into data/<file>. A download only replaces the old CSV if it
     looks valid (header has ID and Image Path, not much smaller than before).
  2. Back up db/ (backup_db.py) before anything in it changes.
  3. Run `indexer.py --embed` (adds only new images: pHash, then SSCD).
  4. Ask the running web app to reload its FAISS index.

exports.json holds the export URLs. They contain secret keys, so the file is
not in git (see exports.example.json) and URLs are never written to the log.

Usage:
  venv/bin/python weekly_update.py              # full run
  venv/bin/python weekly_update.py --no-export  # reuse current CSVs, just index
  venv/bin/python weekly_update.py --only English-GU.csv
"""

import argparse
import csv
import io
import json
import logging
import os
import subprocess
import sys
import time

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_DIR = os.path.join(BASE_DIR, "logs")
CONFIG = os.path.join(BASE_DIR, "exports.json")

POLL_EVERY = 60            # seconds between processing calls
EXPORT_TIMEOUT = 60 * 60   # give up on one export after an hour
MIN_ROW_RATIO = 0.8        # new CSV must have at least 80% of the old row count
REQUIRED_COLUMNS = {"ID", "Image Path"}
APP_RELOAD_URL = "http://127.0.0.1:5000/api/reload-index"

os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(os.path.join(LOG_DIR, "weekly_update.log")), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("weekly")


def message(resp):
    """WP All Export answers with JSON {"status":..., "message":...}; fall back to the raw text."""
    try:
        return str(resp.json().get("message", "")).strip()
    except ValueError:
        return resp.text.strip()[:200]


def run_export(session, exp):
    """Trigger one export and wait until WP All Export says it is complete."""
    name = exp["file"]
    resp = session.get(exp["trigger"], timeout=60)
    resp.raise_for_status()
    log.info(f"[{name}] trigger: {message(resp)}")

    started, polls = time.time(), 0
    while time.time() - started < EXPORT_TIMEOUT:
        time.sleep(POLL_EVERY if polls else 5)
        resp = session.get(exp["processing"], timeout=300)
        resp.raise_for_status()
        msg, polls = message(resp), polls + 1
        log.info(f"[{name}] processing #{polls}: {msg}")
        low = msg.lower()
        if "complete" in low:
            return
        # After it has run, "not triggered" means there is nothing left to process.
        if "not triggered" in low and polls > 1:
            return
    raise TimeoutError(f"export did not finish within {EXPORT_TIMEOUT // 60} minutes")


def row_count(text):
    return max(0, sum(1 for _ in csv.reader(io.StringIO(text))) - 1)


def download(session, exp):
    """Download the export file and replace data/<file> only if it looks right."""
    name = exp["file"]
    resp = session.get(exp["download"], timeout=300)
    resp.raise_for_status()
    text = resp.content.decode("utf-8-sig")
    header = next(csv.reader(io.StringIO(text)), [])
    missing = REQUIRED_COLUMNS - {h.strip() for h in header}
    if missing:
        raise ValueError(f"download is missing columns {sorted(missing)} (got {header[:6]})")

    rows = row_count(text)
    path = os.path.join(DATA_DIR, name)
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig", errors="replace") as fh:
            old_rows = row_count(fh.read())
        if old_rows and rows < old_rows * MIN_ROW_RATIO:
            raise ValueError(f"only {rows} rows, previous file had {old_rows} - keeping the previous file")
        os.replace(path, path + ".prev")
    else:
        old_rows = 0

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    os.replace(tmp, path)
    log.info(f"[{name}] saved {rows} rows (was {old_rows})")


def reload_app():
    auth = None
    if os.environ.get("APP_USERNAME") and os.environ.get("APP_PASSWORD"):
        auth = (os.environ["APP_USERNAME"], os.environ["APP_PASSWORD"])
    try:
        resp = requests.post(APP_RELOAD_URL, auth=auth, timeout=120)
        resp.raise_for_status()
        log.info(f"Web app reloaded its index: {resp.json().get('total')} images")
        return True
    except requests.RequestException as e:
        log.error(f"Could not reload the web app index ({e}); restart idr-image-search to pick it up")
        return False


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--no-export", action="store_true", help="skip the exports, index the current CSVs")
    ap.add_argument("--only", metavar="FILE", action="append", help="only refresh this CSV (repeatable)")
    args = ap.parse_args()

    log.info("=" * 60)
    log.info("Weekly update started")
    failures = []

    if not args.no_export:
        with open(CONFIG) as fh:
            exports = json.load(fh)
        if args.only:
            exports = [e for e in exports if e["file"] in args.only]
        os.makedirs(DATA_DIR, exist_ok=True)
        session = requests.Session()
        session.headers["User-Agent"] = "IDR image search weekly update"
        for exp in exports:  # one at a time, to keep the load on WordPress low
            try:
                run_export(session, exp)
                download(session, exp)
            except Exception as e:  # keep going; the old CSV stays in place
                failures.append(exp["file"])
                log.error(f"[{exp['file']}] FAILED: {type(e).__name__}: {e}")

    log.info("Backing up db/ ...")
    result = subprocess.run([sys.executable, os.path.join(BASE_DIR, "backup_db.py")], cwd=BASE_DIR,
                            capture_output=True, text=True)
    for line in (result.stdout + result.stderr).strip().splitlines():
        log.info(line)
    if result.returncode != 0:
        failures.append("backup")
        log.error("Backup failed - continuing with the update (the indexer only adds data)")

    log.info("Running indexer (new images only)...")
    result = subprocess.run([sys.executable, os.path.join(BASE_DIR, "indexer.py"), "--embed"], cwd=BASE_DIR)
    if result.returncode != 0:
        failures.append("indexer")
        log.error(f"Indexer exited with code {result.returncode}")

    if not reload_app():
        failures.append("reload")

    if failures:
        log.error(f"Weekly update finished with problems: {', '.join(failures)}")
        sys.exit(1)
    log.info("Weekly update finished OK")


if __name__ == "__main__":
    main()
