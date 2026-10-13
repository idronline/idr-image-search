#!/usr/bin/env python3
"""
IDR Image Search — Flask Web App (SSCD edition)

Two-stage search:
  Stage 1 — pHash  (<5ms)   exact/near-exact matches
  Stage 2 — SSCD   (~400ms) crops, flips, edits, colour changes

Both stages run on every search. Results are merged and deduplicated.
pHash results show higher confidence; SSCD results show similarity score.
"""

import io
import os

import imagehash
import requests
from flask import Flask, jsonify, render_template, request
from PIL import Image, UnidentifiedImageError

from db_setup import get_conn, init_db
from faiss_index import load_index, search_index, COSINE_THRESHOLD

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20 MB

ALLOWED_EXTENSIONS  = {"jpg", "jpeg", "png", "webp", "gif"}
PHASH_THRESHOLD     = 10   # Hamming distance — exact/near-exact
SSCD_THRESHOLD      = 0.85 # Cosine similarity — vetted to give ~0 FP

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# ── Module-level singletons loaded once at startup ────────────────────────────

print("[app] Loading SSCD model...")
try:
    from sscd_model import get_model as _get_sscd
    _sscd_model = _get_sscd()
    SSCD_AVAILABLE = True
    print("[app] SSCD model ready.")
except Exception as e:
    print(f"[app] WARNING: SSCD model not available ({e}). Falling back to pHash only.")
    SSCD_AVAILABLE = False
    _sscd_model = None

print("[app] Loading FAISS index...")
_faiss_index, _faiss_urls = load_index()
print(f"[app] FAISS index: {_faiss_index.ntotal} images.")

# ── Helpers ───────────────────────────────────────────────────────────────────

def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[-1].lower() in ALLOWED_EXTENSIONS


def open_image(img_bytes: bytes) -> Image.Image | None:
    try:
        return Image.open(io.BytesIO(img_bytes)).convert("RGB")
    except (UnidentifiedImageError, Exception):
        return None


def phash_search(img: Image.Image, threshold: int) -> list[dict]:
    """
    Stage 1: scan all pHashes in DB and return matches within Hamming threshold.
    Fast — integer comparison only, no model inference.
    """
    try:
        q_ph = imagehash.phash(img)
        q_dh = imagehash.dhash(img)
    except Exception:
        return []

    conn = get_conn()
    rows = conn.execute("SELECT image_url, phash, dhash FROM image_hashes").fetchall()
    conn.close()

    matches = []
    for row in rows:
        try:
            ph_dist = int(imagehash.hex_to_hash(row["phash"]) - q_ph)
            dh_dist = int(imagehash.hex_to_hash(row["dhash"]) - q_dh) if row["dhash"] else 64
            # AND logic: both hashes must agree
            if ph_dist <= threshold and dh_dist <= threshold:
                sim = max(0, round((1 - ph_dist / 64) * 100))
                matches.append({
                    "image_url":  row["image_url"],
                    "distance":   ph_dist,
                    "similarity": sim,
                    "match_type": "Exact match" if ph_dist == 0 else "Near match",
                    "stage":      "phash",
                })
        except Exception:
            continue

    matches.sort(key=lambda x: x["distance"])
    return matches


def sscd_search(img: Image.Image, threshold: float) -> list[dict]:
    """
    Stage 2: compute SSCD embedding and search FAISS index.
    Handles crops, flips, edits. ~400ms on CPU.
    """
    if not SSCD_AVAILABLE or _faiss_index.ntotal == 0:
        return []

    embedding = _sscd_model.embed(img)
    if embedding is None:
        return []

    raw = search_index(_faiss_index, _faiss_urls, embedding,
                       top_k=20, threshold=threshold)

    results = []
    for r in raw:
        results.append({
            "image_url":  r["image_url"],
            "similarity": round(r["similarity"] * 100),  # 0.85 → 85%
            "distance":   None,
            "match_type": "Similar" if r["similarity"] < 0.92 else "Near match",
            "stage":      "sscd",
        })
    return results


def enrich_with_articles(matches: list[dict]) -> list[dict]:
    """Add article metadata to each match."""
    if not matches:
        return []

    conn = get_conn()
    enriched = []
    for m in matches:
        arts = conn.execute("""
            SELECT article_id, article_title, permalink, contributor, csv_source
            FROM article_images WHERE image_url = ?
        """, (m["image_url"],)).fetchall()
        enriched.append({**m, "articles": [dict(a) for a in arts]})
    conn.close()
    return enriched


def merge_results(phash_results: list[dict], sscd_results: list[dict]) -> list[dict]:
    """
    Merge Stage 1 and Stage 2 results.
    - pHash results take priority (exact/near-exact)
    - SSCD results fill in anything pHash missed
    - Deduplicate by image_url, keeping the higher-confidence result
    """
    seen   = {}
    merged = []

    # pHash first (higher confidence)
    for r in phash_results:
        seen[r["image_url"]] = r
        merged.append(r)

    # SSCD second — only add if not already found by pHash
    for r in sscd_results:
        if r["image_url"] not in seen:
            seen[r["image_url"]] = r
            merged.append(r)

    return merged

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    conn = get_conn()
    row = conn.execute("SELECT * FROM indexer_status WHERE id = 1").fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "no status"}), 500

    data  = dict(row)
    total = data.get("total_discovered") or 0
    hashed   = data.get("total_hashed") or 0
    embedded = data.get("total_embedded") or 0

    data["percent_hashed"]   = round(hashed   / total * 100) if total > 0 else 0
    data["percent_embedded"] = round(embedded / total * 100) if total > 0 else 0
    data["faiss_total"]      = _faiss_index.ntotal
    data["sscd_available"]   = SSCD_AVAILABLE
    return jsonify(data)


@app.route("/api/search", methods=["POST"])
def api_search():
    img_bytes = None
    source    = None

    # Source 1: file upload
    if "file" in request.files:
        f = request.files["file"]
        if f and f.filename and allowed_file(f.filename):
            img_bytes = f.read()
            source    = f"Uploaded: {f.filename}"

    # Source 2: URL
    if img_bytes is None:
        url = request.form.get("url", "").strip()
        if url:
            try:
                resp = requests.get(url, timeout=15,
                                    headers={"User-Agent": USER_AGENT})
                if resp.status_code == 200:
                    img_bytes = resp.content
                    source    = f"URL: {url}"
                else:
                    return jsonify({"error": f"Could not fetch URL (HTTP {resp.status_code})"}), 400
            except requests.RequestException as e:
                return jsonify({"error": f"Could not fetch URL: {str(e)}"}), 400

    if img_bytes is None:
        return jsonify({"error": "No image provided."}), 400

    img = open_image(img_bytes)
    if img is None:
        return jsonify({"error": "Could not read image. Is it a valid JPG/PNG/WebP?"}), 400

    # Run both stages
    phash_results = phash_search(img, PHASH_THRESHOLD)
    sscd_results  = sscd_search(img, SSCD_THRESHOLD)

    # Merge, deduplicate, enrich with article metadata
    merged  = merge_results(phash_results, sscd_results)
    results = enrich_with_articles(merged)

    return jsonify({
        "source":          source,
        "count":           len(results),
        "phash_matches":   len(phash_results),
        "sscd_matches":    len(sscd_results),
        "sscd_available":  SSCD_AVAILABLE,
        "faiss_indexed":   _faiss_index.ntotal,
        "results":         results,
    })


@app.route("/api/reload-index", methods=["POST"])
def api_reload_index():
    """
    Reload the FAISS index from disk after a new embedding pass.
    Call this after running python indexer.py --embed on weekends.
    """
    global _faiss_index, _faiss_urls
    _faiss_index, _faiss_urls = load_index()
    return jsonify({
        "ok":    True,
        "total": _faiss_index.ntotal,
    })


# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=False)
