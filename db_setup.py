import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "db", "images.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_conn()
    c = conn.cursor()

    c.executescript("""
        -- One row per unique image URL
        CREATE TABLE IF NOT EXISTS images (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            image_url   TEXT    NOT NULL UNIQUE,
            file_format TEXT,
            indexed_at  TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        -- pHash for fast exact/near-exact pre-filter (Stage 1)
        CREATE TABLE IF NOT EXISTS image_hashes (
            image_url   TEXT    NOT NULL PRIMARY KEY,
            phash       TEXT    NOT NULL,
            dhash       TEXT
        );

        -- SSCD embedding stored as BLOB for FAISS rebuild if needed
        -- Primary search goes through the FAISS index file, not this table.
        -- This table is the source of truth for rebuilding the index.
        CREATE TABLE IF NOT EXISTS image_embeddings (
            image_url   TEXT    NOT NULL PRIMARY KEY,
            embedding   BLOB    NOT NULL,   -- 512 float32 values = 2048 bytes
            embedded_at TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        -- Article ↔ image associations
        CREATE TABLE IF NOT EXISTS article_images (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            article_id      TEXT,
            article_title   TEXT,
            permalink       TEXT,
            contributor     TEXT,
            csv_source      TEXT,
            image_url       TEXT NOT NULL,
            UNIQUE(article_id, image_url)
        );

        -- Indexer progress tracking
        CREATE TABLE IF NOT EXISTS indexer_status (
            id                  INTEGER PRIMARY KEY CHECK (id = 1),
            total_discovered    INTEGER DEFAULT 0,
            total_hashed        INTEGER DEFAULT 0,
            total_embedded      INTEGER DEFAULT 0,
            current_csv         TEXT    DEFAULT '',
            current_url         TEXT    DEFAULT '',
            is_running          INTEGER DEFAULT 0,
            started_at          TEXT,
            last_updated_at     TEXT    NOT NULL DEFAULT (datetime('now')),
            last_error          TEXT    DEFAULT ''
        );

        INSERT OR IGNORE INTO indexer_status (id) VALUES (1);
    """)

    conn.commit()

    # Safe migrations for servers upgrading from previous schema
    migrations = [
        "ALTER TABLE indexer_status ADD COLUMN total_hashed INTEGER DEFAULT 0",
        "ALTER TABLE indexer_status ADD COLUMN total_embedded INTEGER DEFAULT 0",
    ]
    for sql in migrations:
        try:
            c.execute(sql)
            conn.commit()
        except sqlite3.OperationalError:
            pass  # Column already exists

    conn.close()
    print(f"[db] Initialised at {DB_PATH}")


if __name__ == "__main__":
    init_db()
