# IDR Image Search — Internal Tool

Perceptual-hash based image duplicate finder for the IDR editorial team.
Fully self-hosted, no external APIs, open source only.

---

## What is (and is not) in this repo

Only code is in git. These live on the server only (see `.gitignore`):

| Folder | What | How to get it back |
|---|---|---|
| `venv/` | Python environment | `python3 -m venv venv && venv/bin/pip install -r requirements-lock.txt --extra-index-url https://download.pytorch.org/whl/cpu` |
| `models/` | SSCD model weights (~95 MB) | Downloaded automatically on first start (`sscd_model.py`) |
| `db/` | `images.db` (hashes, embeddings, articles) + FAISS index | Rebuilt by running `indexer.py` over the CSVs (takes hours) |
| `data/` | CSV exports from WordPress | Export again from the site |
| `logs/` | Indexer logs | Created automatically |

`requirements-lock.txt` is the exact package list from the server (`pip freeze`);
`requirements.txt` is the original short list.

## Shared IDR tools look

`templates/_nav.html`, `static/idr-tools.css` and `static/idr-tools.js` are the same files
as in the Translate, Audio and Email Collector repos. Change them in all four.
Set `APP_USERNAME` and `APP_PASSWORD` in the environment to require the same browser login
as the other tools (off when unset).

---

## Requirements

- Python 3.10+
- pip
- Linux server (Ubuntu/Debian recommended)

---

## Setup

```bash
# 1. Clone / copy the project folder to your server
cd /opt/idr-image-search   # or wherever you want to install

# 2. Create a virtual environment (recommended)
python3 -m venv venv
source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Initialise the database
python db_setup.py
```

---

## CSV files

Drop your four CSV exports into the `data/` folder:

```
data/
  English-Articles.csv
  English-Features.csv
  English-GU.csv
  English-Posts.csv
```

Required CSV columns: `ID`, `Title`, `Contributor`, `Permalink`, `Image Path`
The `Image Path` column should be pipe-separated (`|`) server paths.

---

## Running the indexer (first time)

The first run will download ~3,000+ images slowly to avoid Cloudflare
rate limiting. Expect 12–15 hours. Run it in a screen/tmux session:

```bash
source venv/bin/activate
screen -S indexer
python indexer.py
# Ctrl+A then D to detach — it runs in background
```

Monitor progress:
```bash
tail -f logs/indexer.log
```

---

## Running the web app

```bash
source venv/bin/activate
python app.py
# Accessible at http://your-server:5000
```

For production, use gunicorn behind nginx:

```bash
pip install gunicorn
gunicorn -w 2 -b 127.0.0.1:5000 app:app
```

Nginx config snippet:
```nginx
location /image-search/ {
    proxy_pass http://127.0.0.1:5000/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    client_max_body_size 20M;
}
```

Keep the app running with systemd or supervisor (see below).

---

## Cron — automatic nightly re-indexing

After the first full index, new CSVs will be dropped into `data/` and
the cron will pick up only new images (incremental).

Edit crontab:
```bash
crontab -e
```

Add this line (runs at 2 AM daily):
```
0 2 * * * /opt/idr-image-search/venv/bin/python /opt/idr-image-search/indexer.py >> /opt/idr-image-search/logs/cron.log 2>&1
```

---

## Systemd service for the web app

Create `/etc/systemd/system/idr-image-search.service`:

```ini
[Unit]
Description=IDR Image Search
After=network.target

[Service]
User=www-data
WorkingDirectory=/opt/idr-image-search
ExecStart=/opt/idr-image-search/venv/bin/gunicorn -w 2 -b 127.0.0.1:5000 app:app
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable idr-image-search
systemctl start idr-image-search
```

---

## How similarity works

The tool uses **perceptual hashing (pHash)**. Every image gets a 64-bit
fingerprint. Two images are compared by their **Hamming distance** —
the number of bits that differ.

| Distance | Meaning |
|---|---|
| 0 | Exact duplicate (identical pixels) |
| 1–5 | Near-exact (same image, different compression/format) |
| 6–10 | Very similar (slight crop, brightness change) |
| 11–20 | Loosely similar |

The **similarity strictness** slider in the UI maps to these distances.
Default is ≤ 10 (catches same image in different formats/sizes).

---

## File structure

```
idr-image-search/
├── data/                  ← drop CSVs here
├── db/
│   └── images.db          ← SQLite (auto-created)
├── logs/
│   └── indexer.log
├── templates/
│   └── index.html
├── app.py                 ← Flask web app
├── db_setup.py            ← DB schema
├── indexer.py             ← CSV → download → pHash → SQLite
└── requirements.txt
```

---

## Troubleshooting

**429 errors during indexing** — Cloudflare is rate limiting. The indexer
will automatically wait 60s and retry. If it keeps failing, increase
`RETRY_WAIT` in `indexer.py`.

**Image not found in search** — The image may not be indexed yet (check
the progress bar), or it may have been filtered as a resized variant.
Check `logs/indexer.log` for skipped URLs.

**App not starting** — Make sure port 5000 is free: `lsof -i :5000`
