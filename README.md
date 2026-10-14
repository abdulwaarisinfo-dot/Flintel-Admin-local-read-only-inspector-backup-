# Flintel-Admin-local-read-only-inspector-backup-

 Connects to your **existing** Render MySQL database. It only runs fixed SELECT/SHOW queries
(sessions are also set to READ ONLY). No schema changes, no restore, no arbitrary SQL.
Backups are plain **JSON files** (no `mysqldump` needed), so it runs on a normal Python host.

## Structure
```
flintel-admin/
  app/ main.py  config.py  db.py  backup.py  templates/{base,index,login}.html
  backups/            <- <database>.json lands here
  .env.example  requirements.txt  run.bat  README.md  .gitignore
```

## Environment variables (.env locally, Environment tab on Render)
- `DATABASE_URL`, **or** `MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_DATABASE`, `MYSQL_USER`, `MYSQL_PASSWORD`
- `ADMIN_PASSWORD` (login for this tool, NOT the MySQL password), `SESSION_SECRET` (long random string)
- `MYSQL_SSL=true` only if your server requires TLS

## Backups (JSON)
"Create Backup" writes `backups/<database>.json`, where `<database>` is the database name from
your env (e.g. `flintel.json`). It contains, for every table: `create_sql`, `columns`, `rows`, `row_count`.
BLOB/embedding bytes are stored as `{"__hex__": "..."}` (byte-exact); Decimals and dates are strings.
The file is written to a temp name and only kept if the whole export succeeded. A new backup replaces
the previous `<database>.json`, so download it first if you want to keep older copies.
The export runs in one read-only transaction (consistent snapshot) and streams rows, so memory stays low.

On Render the disk is temporary unless you add a Persistent Disk (mount path `/app/backups`),
so click **Download** right after creating the backup.

## Run on Render (Python 3, no Docker)
- Root Directory: `flintel-admin`
- Build Command: `pip install -r requirements.txt`
- Start Command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- Health Check Path: `/health`

## Run locally (Windows)
```
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
```
(or double-click `run.bat`). Open http://127.0.0.1:8000.

## Notes
- This page is public on Render. Use a strong `ADMIN_PASSWORD`.
- MySQL user only needs SELECT on the database (plus SHOW VIEW if you have views).

