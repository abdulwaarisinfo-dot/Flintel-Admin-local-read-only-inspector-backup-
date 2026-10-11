Connects to your **existing** Render MySQL database. It only runs fixed SELECT/SHOW queries
(sessions are also set to READ ONLY) and `mysqldump`. No schema changes, no restore, no arbitrary SQL.

## Structure
```
flintel-admin/
  app/ main.py  config.py  db.py  backup.py  templates/{base,index,login}.html
  backups/            <- .sql files land here
  .env.example  requirements.txt  run.bat  README.md  .gitignore
```

## Where to put the Render credentials
1. Copy `.env.example` to `.env` (same folder as this README).
2. Fill in either `DATABASE_URL`, or `MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_USER`, `MYSQL_PASSWORD`.
   Use the **external** host/port of your Render MySQL service, not an internal/private hostname
   (private hostnames only resolve inside Render; Render MySQL is usually a private service,
   so it must be exposed over TCP for your PC to reach it).
3. Set `ADMIN_PASSWORD` (login for this tool). `.env` is git-ignored. `MYSQL_ROOT_PASSWORD` is not needed.

## Run on Windows
1. Install Python 3.10+ (tick "Add to PATH").
2. Install MySQL client tools so `mysqldump.exe` exists (MySQL Installer -> "MySQL Server" or
   "Client only"). If it is not on PATH, set `MYSQLDUMP_PATH` in `.env`.
3. In PowerShell inside the project folder:
```
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
```
   (or just double-click `run.bat`)
4. Open http://127.0.0.1:8000 and log in. Health check: http://127.0.0.1:8000/health

## Backups
"Create Backup" runs mysqldump with `--single-transaction --hex-blob --triggers`, writing
`backups/flintel_YYYY-MM-DD_HHMMSS.sql` (structure, indexes, keys, AUTO_INCREMENT, rows, JSON, BLOBs as hex so
embeddings are byte-exact). The file is written to a temp name and only kept if mysqldump finished cleanly.
Use "Download" in the page to save it.

## Testing a backup safely (never against production)
```
mysql -u root -p -e "CREATE DATABASE flintel_restore_test CHARACTER SET utf8mb4"
mysql -u root -p flintel_restore_test < backups\flintel_2026-10-08_013000.sql
```
Note: the dump contains DROP TABLE IF EXISTS statements, so only ever import it into a throwaway database.

## Notes
- The tool binds to 127.0.0.1 only. Don't expose it publicly.
- MySQL user needs SELECT on `flintel` (and `SHOW VIEW`/`TRIGGER` if you use views/triggers).
