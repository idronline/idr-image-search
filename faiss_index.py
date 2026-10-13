"""
faiss_index.py — Build, save, load, and search the SSCD FAISS index.

The index maps a position (integer) to an image_url.
We keep a parallel list `url_list` in the same order as FAISS vectors
so we can go from FAISS result index → image_url.

The index file and url list are stored alongside the DB:
  db/faiss.index   — the FAISS flat cosine index
  db/faiss_urls.json — ordered list of image URLs matching index positions
"""

import json
import os
import struct

import faiss
import numpy as np

from db_setup import DB_PATH, get_conn

INDEX_PATH    = os.path.join(os.path.dirname(DB_PATH), "faiss.index")
URL_LIST_PATH = os.path.join(os.path.dirname(DB_PATH), "faiss_urls.json")

EMBEDDING_DIM = 512   # SSCD produces 512-dim L2-normalised vectors
COSINE_THRESHOLD = 0.85  # Minimum cosine similarity to count as a match


# ── Helpers ───────────────────────────────────────────────────────────────────

def embedding_to_bytes(vec: np.ndarray) -> bytes:
    """Serialise float32 numpy array to bytes for SQLite BLOB storage."""
    return struct.pack(f"{len(vec)}f", *vec.tolist())


def bytes_to_embedding(blob: bytes) -> np.ndarray:
    """Deserialise bytes from SQLite BLOB to float32 numpy array."""
    n = len(blob) // 4
    return np.array(struct.unpack(f"{n}f", blob), dtype=np.float32)


# ── Build index from DB ───────────────────────────────────────────────────────

def build_index() -> tuple[faiss.IndexFlatIP, list[str]]:
    """
    Read all embeddings from image_embeddings table and build a FAISS
    IndexFlatIP (inner product = cosine similarity on L2-normalised vectors).
    Returns (index, url_list).
    """
    conn = get_conn()
    rows = conn.execute(
        "SELECT image_url, embedding FROM image_embeddings ORDER BY rowid"
    ).fetchall()
    conn.close()

    if not rows:
        index = faiss.IndexFlatIP(EMBEDDING_DIM)
        return index, []

    url_list   = [r["image_url"] for r in rows]
    embeddings = np.stack([bytes_to_embedding(r["embedding"]) for r in rows])

    # Normalise to unit vectors (cosine similarity = inner product after this)
    faiss.normalize_L2(embeddings)

    index = faiss.IndexFlatIP(EMBEDDING_DIM)
    index.add(embeddings)

    print(f"[faiss] Built index: {index.ntotal} vectors")
    return index, url_list


def save_index(index: faiss.IndexFlatIP, url_list: list[str]):
    """Save FAISS index and URL list to disk."""
    os.makedirs(os.path.dirname(INDEX_PATH), exist_ok=True)
    faiss.write_index(index, INDEX_PATH)
    with open(URL_LIST_PATH, "w") as f:
        json.dump(url_list, f)
    print(f"[faiss] Saved index ({index.ntotal} vectors) to {INDEX_PATH}")


def load_index() -> tuple[faiss.IndexFlatIP, list[str]]:
    """Load FAISS index and URL list from disk. Returns empty index if not found."""
    if not os.path.exists(INDEX_PATH) or not os.path.exists(URL_LIST_PATH):
        print("[faiss] No index on disk — returning empty index")
        return faiss.IndexFlatIP(EMBEDDING_DIM), []

    index = faiss.read_index(INDEX_PATH)
    with open(URL_LIST_PATH) as f:
        url_list = json.load(f)

    print(f"[faiss] Loaded index: {index.ntotal} vectors")
    return index, url_list


def add_to_index(
    index: faiss.IndexFlatIP,
    url_list: list[str],
    image_url: str,
    embedding: np.ndarray,
):
    """
    Add a single new embedding to an existing in-memory index.
    Call save_index() periodically to persist.
    """
    vec = embedding.copy().astype(np.float32).reshape(1, -1)
    faiss.normalize_L2(vec)
    index.add(vec)
    url_list.append(image_url)


# ── Search ────────────────────────────────────────────────────────────────────

def search_index(
    index: faiss.IndexFlatIP,
    url_list: list[str],
    query_embedding: np.ndarray,
    top_k: int = 10,
    threshold: float = COSINE_THRESHOLD,
) -> list[dict]:
    """
    Search the FAISS index for images similar to query_embedding.
    Returns list of dicts: {image_url, similarity} sorted by similarity desc.
    Only returns results with cosine similarity >= threshold.
    """
    if index.ntotal == 0:
        return []

    vec = query_embedding.copy().astype(np.float32).reshape(1, -1)
    faiss.normalize_L2(vec)

    k = min(top_k, index.ntotal)
    similarities, indices = index.search(vec, k)

    results = []
    for sim, idx in zip(similarities[0], indices[0]):
        if idx < 0:          # FAISS returns -1 for empty slots
            continue
        sim = float(sim)
        if sim < threshold:
            break            # Results are sorted descending, so we can stop
        results.append({
            "image_url":  url_list[idx],
            "similarity": round(sim, 4),
        })

    return results


if __name__ == "__main__":
    # Utility: rebuild index from DB and save
    print("Rebuilding FAISS index from database...")
    idx, urls = build_index()
    save_index(idx, urls)
    print(f"Done. {idx.ntotal} images indexed.")
