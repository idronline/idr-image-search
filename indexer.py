#!/usr/bin/env python3
"""
IDR Image Search — Indexer (SSCD edition)

Two-pass indexing:
  Pass 1 — pHash (fast, always runs, Cloudflare-safe throttling)
  Pass 2 — SSCD  (run during weekend downtime with other API off)

Pass 1 is the existing behaviour — downloads images, computes pHash/dHash,
stores in image_hashes table. Always runs on cron.

Pass 2 reads images already downloaded (re-fetches if needed), computes
SSCD embeddings, stores in image_embeddings table, rebuilds FAISS index.
Run Pass 2 manually on weekends:
  python indexer.py --embed

Cron (weekly, Monday 2am — Pass 1 only):
  0 2 * * 1 /path/to/venv/bin/python /path/to/indexer.py >> logs/cron.log 2>&1

Weekend run (Pass 2, run manually):
  python indexer.py --embed
"""

import argparse
import csv
import io
import logging
import os
import random
import re
import sqlite3
import sys
import time
from datetime import datetime
from urllib.parse import urlparse

import imagehash
import numpy as np
import requests
from PIL import Image, UnidentifiedImageError

from db_setup import get_conn, init_db
from faiss_index import (
    add_to_index, build_index, bytes_to_embedding,
    embedding_to_bytes, load_index, save_index,
)

# ── Config ────────────────────────────────────────────────────────────────────

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
DATA_DIR  = os.path.join(BASE_DIR, "data")
LOG_DIR   = os.path.join(BASE_DIR, "logs")
LOCK_FILE = os.path.join(BASE_DIR, "indexer.lock")

CSV_FILES = {
    "English-Articles.csv": "Articles",
    "English-Features.csv": "Features",
    "English-GU.csv":       "Ground Up",
    "English-Posts.csv":    "Posts",
    "Hindi-Articles.csv":   "Hindi Articles",
    "Hindi-GU.csv":         "Hindi Ground Up",
    "Hindi-Features.csv":   "Hindi Features",
}

PATH_TO_URL = {
    "/home/idronline.org/public_html":       "https://idronline.org",
    "/home/hindi.idronline.org/public_html": "https://hindi.idronline.org",
}

# Throttling (Pass 1 — download phase)
DELAY_MIN       = 2.0
DELAY_MAX       = 5.0
BATCH_SIZE      = 50
BATCH_PAUSE_MIN = 30.0
BATCH_PAUSE_MAX = 60.0
MAX_RETRIES     = 3
RETRY_WAIT      = 60.0

# Image quality filters
MIN_STD_DEV   = 8.0
MIN_DIMENSION = 100

SKIP_PATTERNS = re.compile(
    r'(_resized[\-_]?\d*\.(jpg|jpeg|png|webp)$)'
    r'|(-\d+x\d+\.(jpg|jpeg|png|webp)$)'
    r'|(-scaled\.(jpg|jpeg|png|webp)$)'
    r'|(-thumbnail\.(jpg|jpeg|png|webp)$)',
    re.IGNORECASE
)

VALID_CONTENT_TYPES = {
    "image/jpeg", "image/jpg", "image/png",
    "image/webp", "image/gif", "image/avif",
}

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# FAISS index save interval during embedding pass
EMBED_SAVE_EVERY = 100   # save index every N images

# ── Logging ───────────────────────────────────────────────────────────────────

os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "indexer.log")),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ── Lockfile ──────────────────────────────────────────────────────────────────

def acquire_lock():
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE) as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)
            log.error(f"Indexer already running (PID {pid}). Exiting.")
            sys.exit(1)
        except PermissionError:
            log.error("Indexer running under a different user. Exiting.")
            sys.exit(1)
        except ProcessLookupError:
            log.warning("Stale lockfile (process gone) — removing.")
            os.remove(LOCK_FILE)
        except ValueError:
            log.warning("Corrupt lockfile — removing.")
            os.remove(LOCK_FILE)
    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))

def release_lock():
    if os.path.exists(LOCK_FILE):
        os.remove(LOCK_FILE)

# ── Image helpers ─────────────────────────────────────────────────────────────

def server_path_to_url(path: str):
    path = path.strip()
    for server_prefix, url_prefix in PATH_TO_URL.items():
        if path.startswith(server_prefix):
            return url_prefix + path[len(server_prefix):]
    if path.startswith("http"):
        return path
    return None


def is_derivative(url: str) -> bool:
    filename = urlparse(url).path.split("/")[-1]
    return bool(SKIP_PATTERNS.search(filename))


def parse_image_paths(raw: str):
    if not raw or not raw.strip():
        return []
    seen, urls = set(), []
    for part in raw.split("|"):
        url = server_path_to_url(part.strip())
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def is_degenerate(img: Image.Image) -> bool:
    arr = np.array(img.convert("L"))
    return float(arr.std()) < MIN_STD_DEV


def is_too_small(img: Image.Image) -> bool:
    w, h = img.size
    return w < MIN_DIMENSION or h < MIN_DIMENSION


def guess_format(url: str) -> str:
    ext = urlparse(url).path.rsplit(".", 1)[-1].lower()
    return ext if ext in ("jpg", "jpeg", "png", "webp", "gif") else "unknown"


def download_image(session: requests.Session, url: str) -> bytes | None:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, timeout=20)
            if resp.status_code == 200:
                ct = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
                if ct not in VALID_CONTENT_TYPES:
                    log.warning(f"Bad content-type '{ct}': {url}")
                    return None
                return resp.content
            elif resp.status_code in (429, 503):
                wait = RETRY_WAIT * attempt
                log.warning(f"Rate limited — waiting {wait:.0f}s: {url}")
                time.sleep(wait)
            elif resp.status_code == 404:
                log.debug(f"404: {url}")
                return None
            else:
                log.warning(f"HTTP {resp.status_code}: {url} (attempt {attempt})")
                time.sleep(5 * attempt)
        except requests.RequestException as e:
            log.warning(f"Request error: {e} (attempt {attempt})")
            time.sleep(5 * attempt)
    log.error(f"Failed after {MAX_RETRIES} attempts: {url}")
    return None


def update_status(conn, **kwargs):
    kwargs["last_updated_at"] = datetime.utcnow().isoformat()
    sets = ", ".join(f"{k} = :{k}" for k in kwargs)
    conn.execute(f"UPDATE indexer_status SET {sets} WHERE id = 1", kwargs)
    conn.commit()

# ── CSV parsing ───────────────────────────────────────────────────────────────

def load_csv(filepath: str, csv_label: str) -> list[dict]:
    articles = []
    for encoding in ["utf-8-sig", "utf-8", "latin-1", "cp1252"]:
        try:
            with open(filepath, newline="", encoding=encoding) as f:
                reader = csv.DictReader(f)
                raw_fields = reader.fieldnames
                if not raw_fields:
                    log.warning(f"Empty/no header: {filepath}")
                    return []
                reader.fieldnames = [h.strip() for h in raw_fields]
                field_map = {h.lower(): h for h in reader.fieldnames}
                img_col = (
                    field_map.get("image path") or field_map.get("image_path")
                    or field_map.get("images") or field_map.get("image")
                )
                if not img_col:
                    log.warning(f"No image column in {filepath}: {reader.fieldnames}")
                    return []
                for row in reader:
                    raw = row.get(img_col, "") or ""
                    all_urls  = parse_image_paths(raw)
                    orig_urls = [u for u in all_urls if not is_derivative(u)]
                    if not orig_urls:
                        continue
                    articles.append({
                        "article_id":  str(row.get("ID", "")).strip(),
                        "title":       row.get("Title", "").strip(),
                        "permalink":   row.get("Permalink", "").strip(),
                        "contributor": row.get("Contributor", "").strip(),
                        "csv_source":  csv_label,
                        "image_urls":  orig_urls,
                    })
            log.info(f"[{csv_label}] {len(articles)} articles (encoding: {encoding})")
            return articles
        except (UnicodeDecodeError, UnicodeError):
            continue
        except FileNotFoundError:
            log.warning(f"CSV not found: {filepath}")
            return []
        except Exception as e:
            log.error(f"Error reading {filepath}: {e}")
            return []
    log.error(f"Could not read {filepath}")
    return []

# ── Pass 1: pHash indexing (weekly cron) ──────────────────────────────────────

def run_hash_pass():
    """
    Download new images and compute pHash/dHash.
    Safe to run weekly on cron — throttled for Cloudflare.
    """
    init_db()
    conn = get_conn()
    log.info("=" * 60)
    log.info("Pass 1 — pHash indexing")
    log.info("=" * 60)

    # Load CSVs
    all_articles = []
    for filename, label in CSV_FILES.items():
        articles = load_csv(os.path.join(DATA_DIR, filename), label)
        log.info(f"  [{label}] {len(articles)} articles")
        all_articles.extend(articles)

    url_to_articles: dict[str, list[dict]] = {}
    for art in all_articles:
        for url in art["image_urls"]:
            url_to_articles.setdefault(url, []).append(art)

    total_unique = len(url_to_articles)
    log.info(f"Unique original image URLs: {total_unique}")

    # Filter to unprocessed
    already = set(
        r[0] for r in conn.execute("SELECT image_url FROM image_hashes").fetchall()
    )
    todo = [u for u in url_to_articles if u not in already]
    log.info(f"Already hashed: {len(already)} — To process: {len(todo)}")

    # Upsert article associations
    for url, arts in url_to_articles.items():
        for art in arts:
            try:
                conn.execute("""
                    INSERT OR IGNORE INTO article_images
                        (article_id, article_title, permalink, contributor, csv_source, image_url)
                    VALUES (:article_id, :title, :permalink, :contributor, :csv_source, :image_url)
                """, {**art, "image_url": url})
            except sqlite3.Error:
                pass
    conn.commit()

    if not todo:
        log.info("Nothing new to hash. Done.")
        update_status(conn, is_running=0, current_csv="", current_url="")
        conn.close()
        return

    update_status(
        conn,
        total_discovered=total_unique,
        total_hashed=len(already),
        is_running=1,
        started_at=datetime.utcnow().isoformat(),
        last_error="",
    )

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    hashed = skipped = errors = 0

    for batch_start in range(0, len(todo), BATCH_SIZE):
        batch = todo[batch_start : batch_start + BATCH_SIZE]
        bn    = batch_start // BATCH_SIZE + 1
        bt    = (len(todo) + BATCH_SIZE - 1) // BATCH_SIZE
        log.info(f"── Batch {bn}/{bt} ({len(batch)} images) ──")

        for url in batch:
            arts = url_to_articles[url]
            update_status(conn,
                current_csv=arts[0]["csv_source"] if arts else "",
                current_url=url,
            )

            img_bytes = download_image(session, url)
            if not img_bytes:
                skipped += 1
                time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
                continue

            try:
                img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
            except UnidentifiedImageError:
                skipped += 1
                time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
                continue

            if is_too_small(img) or is_degenerate(img):
                skipped += 1
                time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
                continue

            try:
                ph = str(imagehash.phash(img))
                dh = str(imagehash.dhash(img))
                conn.execute(
                    "INSERT OR IGNORE INTO images (image_url, file_format) VALUES (?,?)",
                    (url, guess_format(url))
                )
                conn.execute(
                    "INSERT OR IGNORE INTO image_hashes (image_url, phash, dhash) VALUES (?,?,?)",
                    (url, ph, dh)
                )
                conn.commit()
                hashed += 1
                update_status(conn, total_hashed=len(already) + hashed)
                log.debug(f"Hashed [{len(already)+hashed}/{total_unique}]: {url}")
            except Exception as e:
                errors += 1
                log.error(f"Hash error for {url}: {e}")

            time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

        if batch_start + BATCH_SIZE < len(todo):
            pause = random.uniform(BATCH_PAUSE_MIN, BATCH_PAUSE_MAX)
            log.info(f"Batch {bn} done — pausing {pause:.0f}s")
            time.sleep(pause)

    total_hashed_now = len(already) + hashed
    update_status(conn,
        is_running=0,
        total_hashed=total_hashed_now,
        current_csv="", current_url="",
    )
    log.info(f"Pass 1 done — hashed: {hashed}, skipped: {skipped}, errors: {errors}")
    conn.close()

# ── Pass 2: SSCD embedding (weekend, manual) ──────────────────────────────────

def run_embed_pass():
    """
    Compute SSCD embeddings for all images that have been hashed but
    not yet embedded. Rebuilds FAISS index at the end.

    Run this manually on weekends when the other API is down:
      python indexer.py --embed
    """
    # Import here so Pass 1 (cron) never needs torch
    from sscd_model import get_model
    from faiss_index import embedding_to_bytes

    init_db()
    conn = get_conn()
    log.info("=" * 60)
    log.info("Pass 2 — SSCD embedding")
    log.info("=" * 60)

    # Find hashed images not yet embedded
    already_embedded = set(
        r[0] for r in conn.execute(
            "SELECT image_url FROM image_embeddings"
        ).fetchall()
    )
    all_hashed = [
        r[0] for r in conn.execute(
            "SELECT image_url FROM image_hashes"
        ).fetchall()
    ]
    todo = [u for u in all_hashed if u not in already_embedded]

    log.info(f"Already embedded: {len(already_embedded)}")
    log.info(f"To embed:         {len(todo)}")

    if not todo:
        log.info("All images already embedded. Rebuilding FAISS index...")
        idx, urls = build_index()
        save_index(idx, urls)
        update_status(conn, total_embedded=len(already_embedded))
        conn.close()
        return

    # Load SSCD model (downloads if not cached)
    model = get_model()

    # Load existing FAISS index to add to incrementally
    idx, url_list = load_index()

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    embedded = errors = skipped = 0
    total    = len(todo)

    update_status(conn,
        is_running=1,
        total_embedded=len(already_embedded),
        started_at=datetime.utcnow().isoformat(),
    )

    for i, url in enumerate(todo):
        update_status(conn, current_url=url, current_csv="embedding")

        # Download (no throttling needed — no Cloudflare concern on weekend)
        # But we still be polite — short delay
        img_bytes = download_image(session, url)
        if not img_bytes:
            skipped += 1
            time.sleep(1)
            continue

        embedding = model.embed_bytes(img_bytes)
        if embedding is None:
            skipped += 1
            log.warning(f"Could not embed: {url}")
            time.sleep(1)
            continue

        try:
            conn.execute(
                "INSERT OR REPLACE INTO image_embeddings (image_url, embedding) VALUES (?,?)",
                (url, embedding_to_bytes(embedding))
            )
            conn.commit()
            add_to_index(idx, url_list, url, embedding)
            embedded += 1
            update_status(conn, total_embedded=len(already_embedded) + embedded)

            if (embedded % 100) == 0:
                log.info(f"Embedded {embedded}/{total} — saving index checkpoint...")
                save_index(idx, url_list)

            log.debug(f"Embedded [{embedded}/{total}]: {url}")
        except Exception as e:
            errors += 1
            log.error(f"Embed DB error for {url}: {e}")

        # Short delay — polite but not as slow as Pass 1
        time.sleep(random.uniform(0.5, 1.5))

    # Final save
    log.info("Saving final FAISS index...")
    save_index(idx, url_list)

    total_embedded_now = len(already_embedded) + embedded
    update_status(conn,
        is_running=0,
        total_embedded=total_embedded_now,
        current_url="", current_csv="",
    )
    log.info(f"Pass 2 done — embedded: {embedded}, skipped: {skipped}, errors: {errors}")
    log.info(f"FAISS index: {idx.ntotal} vectors total")
    conn.close()

# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="IDR Image Indexer")
    parser.add_argument(
        "--embed",
        action="store_true",
        help="Run SSCD embedding pass (use on weekends with other API off)"
    )
    parser.add_argument(
        "--rebuild-index",
        action="store_true",
        help="Rebuild FAISS index from stored embeddings (no downloads needed)"
    )
    args = parser.parse_args()

    acquire_lock()
    try:
        if args.rebuild_index:
            log.info("Rebuilding FAISS index from stored embeddings...")
            init_db()
            idx, urls = build_index()
            save_index(idx, urls)
            log.info(f"Done. {idx.ntotal} images in index.")
        elif args.embed:
            run_hash_pass()   # catch up on any new images first
            run_embed_pass()
        else:
            run_hash_pass()   # default: cron weekly run
    finally:
        release_lock()


if __name__ == "__main__":
    main()
