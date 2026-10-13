#!/usr/bin/env python3
"""
backup_db.py — snapshot db/ (SQLite database + FAISS index) and keep it safe.

Rebuilding db/ from scratch takes 12+ hours of downloading, so weekly_update.py
calls this before the indexer touches anything. It can also be run by hand.

  1. Copies db/images.db with SQLite's backup API (a consistent copy, even while
     the web app is reading it) plus db/faiss.index and db/faiss_urls.json into
     backups/images-db-YYYY-MM-DD.tar.gz. Keeps the newest KEEP_LOCAL files.
  2. If BACKUP_S3_BUCKET is set (in ~/idr-tools.env), uploads the file to
     s3://<bucket>/idr-image-search/. Credentials come from the server's IAM role.

Restore (with the app stopped):
  tar -xzf backups/images-db-YYYY-MM-DD.tar.gz -C db/
  # or first: aws s3 cp s3://<bucket>/idr-image-search/images-db-YYYY-MM-DD.tar.gz backups/
"""

import datetime
import os
import sqlite3
import sys
import tarfile
import tempfile

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_DIR = os.path.join(BASE_DIR, "db")
BACKUP_DIR = os.path.join(BASE_DIR, "backups")
FILES = ["images.db", "faiss.index", "faiss_urls.json"]
KEEP_LOCAL = 4
S3_PREFIX = "idr-image-search/"


def log(msg):
    print(f"[backup] {msg}", flush=True)


def make_backup():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    name = f"images-db-{datetime.date.today().isoformat()}.tar.gz"
    out = os.path.join(BACKUP_DIR, name)
    with tempfile.TemporaryDirectory(prefix="dbbackup_") as tmp:
        snap = os.path.join(tmp, "images.db")
        src = sqlite3.connect(os.path.join(DB_DIR, "images.db"))
        dst = sqlite3.connect(snap)
        with dst:
            src.backup(dst)
        src.close()
        rows = dst.execute("SELECT COUNT(*) FROM image_hashes").fetchone()[0]
        ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
        dst.close()
        if ok != "ok":
            raise RuntimeError(f"database copy failed its integrity check: {ok}")

        partial = out + ".tmp"
        with tarfile.open(partial, "w:gz") as tar:
            tar.add(snap, arcname="images.db")
            for f in FILES[1:]:
                path = os.path.join(DB_DIR, f)
                if os.path.exists(path):
                    tar.add(path, arcname=f)
        os.replace(partial, out)
    log(f"saved {name} ({os.path.getsize(out) / 1e6:.1f} MB, {rows} images)")
    return out


def prune_local():
    files = sorted(f for f in os.listdir(BACKUP_DIR) if f.startswith("images-db-") and f.endswith(".tar.gz"))
    for old in files[:-KEEP_LOCAL]:
        os.remove(os.path.join(BACKUP_DIR, old))
        log(f"removed old local backup {old}")


def upload(path, bucket):
    import boto3  # only needed when an S3 bucket is configured

    key = S3_PREFIX + os.path.basename(path)
    boto3.client("s3").upload_file(path, bucket, key)
    log(f"uploaded to s3://{bucket}/{key}")


def main():
    path = make_backup()
    prune_local()
    bucket = os.environ.get("BACKUP_S3_BUCKET")
    if bucket:
        upload(path, bucket)
    else:
        log("BACKUP_S3_BUCKET not set - kept on this server only")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FAILED: {type(e).__name__}: {e}")
        sys.exit(1)
