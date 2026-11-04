"""JSON backup of the database (read-only on the server).

Only fixed SHOW / SELECT statements are run, inside one read-only transaction, so the real
database is never modified. The result is written to  backups/<database-name>.json
(the database name comes from .env), e.g. backups/flintel.json.
"""
import json
import os
import re
import threading
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from dotenv import dotenv_values
from sqlalchemy.exc import SQLAlchemyError

from . import config, db

NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+\.json$")
FORMAT = "flintel-json-backup-v1"
CHUNK = 500  # rows fetched from the server at a time (keeps memory low)
_lock = threading.Lock()


class BackupError(Exception):
    pass


def _env(name: str):
    """Read a setting from .env (re-read on every backup, so no restart is needed locally),
    falling back to the real environment (Render's Environment tab)."""
    vals = dotenv_values(config.BASE_DIR / ".env")
    raw = vals.get(name)
    if raw is None:
        raw = os.getenv(name)
    return (raw or "").strip()


def _range_settings():
    """Optional partial backup of the messages table (db.TABLE), by row id, set in .env:
         BACKUP_FROM = first id (inclusive)      BACKUP_TO = last id (inclusive)
    Both empty -> None -> a normal full backup, exactly as before."""
    def num(name):
        raw = _env(name).replace(",", "").replace("_", "")
        if not raw:
            return None
        if not raw.isdigit():
            raise BackupError("%s must be a whole number (got '%s')." % (name, raw))
        return int(raw)

    lo, hi = num("BACKUP_FROM"), num("BACKUP_TO")
    if lo is None and hi is None:
        return None
    lo = 1 if lo is None else lo
    if hi is not None and lo > hi:
        raise BackupError("BACKUP_FROM (%d) is bigger than BACKUP_TO (%d)." % (lo, hi))
    return lo, hi


def _bt(name: str) -> str:
    """Quote an identifier with backticks. 'db.table' -> `db`.`table`."""
    return ".".join("`" + part.replace("`", "``") + "`" for part in name.split("."))


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
    """Backup = SIRF wohi table jo .env mein SIGNALS_MYSQL_DATABASE + SIGNALS_MYSQL_TABLE se set hai
    (database khali ho to connected MYSQL_DATABASE). BACKUP_FROM / BACKUP_TO ho to us table ki id range."""
    try:
        s = config.get_db_settings()
    except config.ConfigError as e:
        raise BackupError(str(e))

    schema = db.SIGNALS_DB or s["database"]
    table = db.TABLE
    for part in (schema, table):
        if not re.fullmatch(r"[A-Za-z0-9_$]+", part or ""):
            raise BackupError("SIGNALS_MYSQL_DATABASE / SIGNALS_MYSQL_TABLE has invalid characters.")
    rng = _range_settings()   # None = poori table

    base = re.sub(r"[^A-Za-z0-9_-]", "", "%s_%s" % (schema, table)) or "backup"
    if rng:   # one file per range, so a new range never overwrites the previous one
        final = config.BACKUP_DIR / ("%s_%d-%s.json" % (base, rng[0], "end" if rng[1] is None else rng[1]))
    else:
        final = config.BACKUP_DIR / ("%s.json" % base)
    tmp = final.with_suffix(".json.tmp")

    if not _lock.acquire(blocking=False):
        raise BackupError("A backup is already running. Please wait for it to finish.")
    try:
        try:
            counts = _write_backup(tmp, s["database"], schema, table, rng)
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
        try:   # chhota sidecar: table kitni rows ke saath file mein hai (dashboard dikhata hai)
            _manifest_path(final.name).write_text(
                json.dumps({"tables": counts, "created_at": datetime.now().isoformat(timespec="seconds")}),
                encoding="utf-8")
        except Exception as e:
            print("Could not write backup manifest:", e)
        return final.name
    finally:
        _lock.release()


def _write_backup(tmp_path, database: str, schema: str, table: str, rng=None):
    full = "%s.%s" % (schema, table)
    engine = db.get_backup_engine()
    with engine.connect() as conn:   # one read-only transaction = consistent snapshot
        version = db.run_ro(conn, "SELECT VERSION()").scalar()
        # names upar regex se validate ho chuke hain
        row = db.run_ro(conn, "SELECT table_type FROM information_schema.tables "
                              "WHERE table_schema = '%s' AND table_name = '%s'" % (schema, table)).first()
        if row is None:
            raise BackupError("Backup failed: table '%s' was not found (or this MySQL user has no SELECT "
                              "permission on it). Check SIGNALS_MYSQL_DATABASE / SIGNALS_MYSQL_TABLE in .env." % full)
        objects = [(full, "VIEW" if "VIEW" in str(row[0]).upper() else "TABLE")]

        with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
            f.write("{\n")
            f.write('"format": %s,\n' % _dumps(FORMAT))
            f.write('"database": %s,\n' % _dumps(schema))
            f.write('"table": %s,\n' % _dumps(table))
            f.write('"created_at": %s,\n' % _dumps(datetime.now().isoformat(timespec="seconds")))
            f.write('"server_version": %s,\n' % _dumps(version))
            f.write('"note": %s,\n' % _dumps(
                'Values like {"__hex__": "ab12"} are BLOB bytes in hex. Decimals/dates are strings.'))
            f.write('"tables": {\n')

            counts = {}
            for i, (name, kind) in enumerate(objects):
                if i:
                    f.write(",\n")
                counts[name] = _write_object(f, conn, name, kind, rng)

            f.write("\n},\n")
            f.write('"completed_at": %s\n}\n' % _dumps(datetime.now().isoformat(timespec="seconds")))
    return counts


def _write_object(f, conn, name: str, kind: str, rng=None) -> None:
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
        return None

    cols = db.run_ro(conn, "SHOW COLUMNS FROM %s" % q).all()
    columns = [{"name": c[0], "type": c[1]} for c in cols]

    where, range_note = "", ""
    if rng:   # backup sirf ek table ka hai: range usi par lagti hai
        pks = [c[0] for c in cols if str(c[3]).upper() == "PRI"]
        if len(pks) != 1:
            raise BackupError("Backup failed: a range backup needs a single-column primary key on %s." % name)
        lo, hi = rng
        where = " WHERE %s >= %d" % (_bt(pks[0]), lo)
        if hi is not None:
            where += " AND %s <= %d" % (_bt(pks[0]), hi)
        where += " ORDER BY %s ASC" % _bt(pks[0])       # messages in order: lowest id first
        range_note = '"range": %s,\n' % _dumps({"column": pks[0], "from": lo, "to": hi})

    f.write('%s: {\n"type": "table",\n"create_sql": %s,\n"columns": %s,\n%s"rows": [' % (
        _dumps(name), _dumps(create_sql), _dumps(columns), range_note))

    count = 0
    result = conn.execution_options(stream_results=True).exec_driver_sql("SELECT * FROM %s%s" % (q, where))
    try:
        for chunk in result.partitions(CHUNK):
            for row in chunk:
                f.write(("\n" if count == 0 else ",\n") + _dumps(list(row)))
                count += 1
    finally:
        result.close()

    f.write("\n],\n\"row_count\": %d\n}" % count)
    return count


def _manifest_path(name: str):
    return config.BACKUP_DIR / (name + ".info")


def manifest_text(name: str) -> str:
    """'flintel_signals=6999, hacker_news=2039' (ya '' agar manifest nahi: purana backup)."""
    try:
        data = json.loads(_manifest_path(name).read_text(encoding="utf-8"))
        return ", ".join("%s=%s" % (t, "view" if n is None else n) for t, n in data.get("tables", {}).items())
    except Exception:
        return ""


def list_backups():
    out = []
    for p in config.BACKUP_DIR.glob("*.json"):
        st = p.stat()
        out.append({"name": p.name, "size": st.st_size, "tables": manifest_text(p.name),
                    "when": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")})
    return sorted(out, key=lambda x: x["name"], reverse=True)


def backup_path(name: str):
    if not NAME_RE.match(name):
        return None
    p = (config.BACKUP_DIR / name).resolve()
    return p if p.parent == config.BACKUP_DIR.resolve() and p.is_file() else None
