"""JSON backup of the database (read-only on the server).

Only fixed SHOW / SELECT statements are run, inside one read-only transaction, so the real
database is never modified. The result is written to  backups/<database-name>.json
(the database name comes from .env), e.g. backups/flintel.json.
"""
import gzip
import json
import re
import threading
import time as _time
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy.exc import SQLAlchemyError

from . import config, db

NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+\.json(\.gz)?$")
FORMAT = "flintel-json-backup-v1"
CHUNK = 500  # rows fetched from the server at a time (keeps memory low)
_lock = threading.Lock()
# Shared progress, read by /backup/status while the backup thread runs.
STATE = {"running": False, "table": None, "rows": 0, "tables_done": 0, "tables_total": 0,
         "started": None, "finished": None, "ok": None, "msg": None, "file": None}


class BackupError(Exception):
    pass


def _bt(name: str) -> str:
    """Quote an identifier with backticks."""
    return "`" + name.replace("`", "``") + "`"


def _json_default(v):
    """Convert MySQL values that JSON cannot hold natively."""
    if isinstance(v, (bytes, bytearray, memoryview)):
        return {"__hex__": bytes(v).hex()}          # BLOB / embeddings: byte-exact
    if isinstance(v, Decimal):
        return str(v)                               # keeps full precision
    if isinstance(v, (datetime, date, time)):
        return v.isoformat()
    if isinstance(v, timedelta):
        return str(v)
    if isinstance(v, (set, frozenset)):
        return sorted(v)
    return str(v)


def _dumps(obj) -> str:
    return json.dumps(obj, default=_json_default, ensure_ascii=False)


def create_backup() -> str:
    """Blocking backup (used by the background thread). Writes backups/<db>.json.gz."""
    try:
        s = config.get_db_settings()
    except config.ConfigError as e:
        raise BackupError(str(e))

    safe_db = re.sub(r"[^A-Za-z0-9_-]", "", s["database"]) or "database"
    final = config.BACKUP_DIR / ("%s.json.gz" % safe_db)
    tmp = final.with_suffix(".gz.tmp")

    if not _lock.acquire(blocking=False):
        raise BackupError("A backup is already running. Please wait for it to finish.")
    try:
        try:
            _write_backup(tmp, s["database"])
        except (SQLAlchemyError, config.ConfigError) as e:
            tmp.unlink(missing_ok=True)
            print("Backup DB error:", type(e).__name__, getattr(e, "orig", e))
            raise BackupError("Backup failed: " + db.friendly_error(e))
        except BackupError:
            tmp.unlink(missing_ok=True)
            raise
        except Exception as e:  # disk full, etc.
            tmp.unlink(missing_ok=True)
            print("Backup error:", type(e).__name__, e)
            raise BackupError("Backup failed (%s). Check the server log." % type(e).__name__)
        tmp.replace(final)   # only a fully written file ever becomes the real backup
        return final.name
    finally:
        _lock.release()


def _run_in_thread():
    try:
        name = create_backup()
        STATE.update(ok=True, file=name, msg="Backup created: " + name)
    except BackupError as e:
        STATE.update(ok=False, msg=str(e))
    except Exception as e:
        print("Backup thread error:", type(e).__name__, e)
        STATE.update(ok=False, msg="Backup failed (%s). Check the server log." % type(e).__name__)
    finally:
        STATE.update(running=False, finished=_time.time())


def start_backup() -> bool:
    """Start the backup in a background thread so the web request returns at once
    (a single long HTTP request gets cut off by Render's proxy). False if already running."""
    if STATE["running"]:
        return False
    STATE.update(running=True, table=None, rows=0, tables_done=0, tables_total=0,
                 started=_time.time(), finished=None, ok=None, msg=None, file=None)
    threading.Thread(target=_run_in_thread, daemon=True, name="backup").start()
    return True


def _write_backup(tmp_path, database: str) -> None:
    engine = db.get_backup_engine()
    with engine.connect() as conn:   # one read-only transaction = consistent snapshot
        version = db.run_ro(conn, "SELECT VERSION()").scalar()
        listing = db.run_ro(conn, "SHOW FULL TABLES").all()
        objects = [(r[0], "VIEW" if str(r[1]).upper().endswith("VIEW") else "TABLE") for r in listing]
        objects.sort(key=lambda x: x[0])
        if not objects:
            raise BackupError("Backup failed: the database has no tables.")
        STATE["tables_total"] = len(objects)

        with gzip.open(tmp_path, "wt", encoding="utf-8", newline="\n", compresslevel=5) as f:
            f.write("{\n")
            f.write('"format": %s,\n' % _dumps(FORMAT))
            f.write('"database": %s,\n' % _dumps(database))
            f.write('"created_at": %s,\n' % _dumps(datetime.now().isoformat(timespec="seconds")))
            f.write('"server_version": %s,\n' % _dumps(version))
            f.write('"note": %s,\n' % _dumps(
                'Values like {"__hex__": "ab12"} are BLOB bytes in hex. Decimals/dates are strings.'))
            f.write('"tables": {\n')

            for i, (name, kind) in enumerate(objects):
                if i:
                    f.write(",\n")
                STATE["table"] = name
                _write_object(f, conn, name, kind)
                STATE["tables_done"] = i + 1

            f.write("\n},\n")
            f.write('"completed_at": %s\n}\n' % _dumps(datetime.now().isoformat(timespec="seconds")))


def _write_object(f, conn, name: str, kind: str) -> None:
    q = _bt(name)
    try:
        create_row = db.run_ro(conn, "SHOW CREATE %s %s" % (kind, q)).first()
        create_sql = create_row[1] if create_row else None
    except SQLAlchemyError as e:
        if kind == "TABLE":
            raise
        create_sql = None   # views need SHOW VIEW privilege; skip the definition, keep going
        print("Could not read view definition for %s: %s" % (name, getattr(e, "orig", e)))

    if kind == "VIEW":
        f.write("%s: %s" % (_dumps(name), _dumps({"type": "view", "create_sql": create_sql})))
        return

    cols = db.run_ro(conn, "SHOW COLUMNS FROM %s" % q).all()
    columns = [{"name": c[0], "type": c[1]} for c in cols]

    f.write('%s: {\n"type": "table",\n"create_sql": %s,\n"columns": %s,\n"rows": [' % (
        _dumps(name), _dumps(create_sql), _dumps(columns)))

    count = 0
    result = conn.execution_options(stream_results=True).exec_driver_sql("SELECT * FROM %s" % q)
    try:
        for chunk in result.partitions(CHUNK):
            for row in chunk:
                f.write(("\n" if count == 0 else ",\n") + _dumps(list(row)))
                count += 1
            STATE["rows"] += len(chunk)
    finally:
        result.close()

    f.write("\n],\n\"row_count\": %d\n}" % count)


def list_backups():
    out = []
    for p in sorted(list(config.BACKUP_DIR.glob("*.json")) + list(config.BACKUP_DIR.glob("*.json.gz"))):
        st = p.stat()
        out.append({"name": p.name, "size": st.st_size,
                    "when": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")})
    return sorted(out, key=lambda x: x["name"], reverse=True)


def backup_path(name: str):
    if not NAME_RE.match(name):
        return None
    p = (config.BACKUP_DIR / name).resolve()
    return p if p.parent == config.BACKUP_DIR.resolve() and p.is_file() else None
