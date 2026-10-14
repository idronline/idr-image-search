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
Set `APP_USERNAME`, `APP_PASSWORD` and `SECRET_KEY` in the environment to require the shared
IDR tools login page (`idr_login.py`, off when unset). In production the app listens on
127.0.0.1 (`BIND_HOST`) behind nginx at `https://api.idronline.org/image-search/`; people use it through
`https://tools.idronline.org/image-search/` (the tools server only passes requests on), so one sign-in covers all tools
(`deploy/nginx-image-search.conf`).

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

## Running on the server

The web app runs as the systemd service `deploy/idr-image-search.service` (port 5000,
restarts on failure and after reboot). The weekly refresh runs from the ubuntu user's
crontab every **Sunday 22:00 India time** (server clock is UTC):

```
# Image search weekly refresh: Sunday 22:00 IST = Sunday 16:30 UTC (server clock is UTC)
30 16 * * 0 cd /home/ubuntu/idr-image-search && { [ -f /home/ubuntu/idr-tools.env ] && set -a && . /home/ubuntu/idr-tools.env && set +a; venv/bin/python weekly_update.py; } >> logs/weekly_cron.log 2>&1
```

It sits next to the hindisearch refresh (Sunday 00:00 IST) in the same crontab, so the two
never overlap. `idr-tools.env` only matters when the shared login is on (the reload call needs it).

The weekly run (`weekly_update.py`) replaces the old manual export + scp:

1. For each export in `exports.json` (WP All Export cron URLs, **secret - not in git**):
   trigger it, call its processing URL until it reports complete, download the CSV into `data/`.
   A download only replaces the old CSV if it has the `ID` and `Image Path` columns and at
   least 80% of the previous row count; the previous file is kept as `data/<name>.prev`.
2. `indexer.py --embed` - downloads only new images, hashes and embeds them.
   It also refreshes article titles, links and contributors from the new CSVs.
3. Tells the running app to reload the FAISS index (no restart needed).

Logs: `logs/weekly_update.log` (and cron output in `logs/weekly_cron.log`).

```bash
sudo cp deploy/idr-image-search.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now idr-image-search

venv/bin/python weekly_update.py                  # run the weekly refresh now, by hand
venv/bin/python weekly_update.py --no-export      # index the current CSVs only
venv/bin/python weekly_update.py --only Hindi-GU.csv
```

---

## Backups of db/

Off by default. With `BACKUP_ENABLED=1` in `~/idr-tools.env`, `backup_db.py` runs at the
start of every weekly refresh (before the indexer changes anything). It can also be run by hand. It writes `backups/images-db-YYYY-MM-DD.tar.gz`
(SQLite copy made with SQLite's backup API + FAISS index), keeps the newest 4, and - when
`BACKUP_S3_BUCKET` is set in `~/idr-tools.env` - uploads it to
`s3://<bucket>/idr-image-search/` using the server's IAM role (needs `pip install boto3`).

Restore:
```bash
sudo systemctl stop idr-image-search
aws s3 cp s3://<bucket>/idr-image-search/images-db-YYYY-MM-DD.tar.gz backups/   # if not on the server
tar -xzf backups/images-db-YYYY-MM-DD.tar.gz -C db/
sudo systemctl start idr-image-search
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
