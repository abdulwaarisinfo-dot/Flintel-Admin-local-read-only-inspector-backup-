"""Read-only database access. Only fixed SELECT/SHOW queries are ever run."""
import re
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import URL 
from sqlalchemy.exc import SQLAlchemyError
from . import config

TABLE = "flintel_signals"
BLOB_TYPES = {"tinyblob", "blob", "mediumblob", "longblob"}
_engine = None
_backup_engine = None


def friendly_error(e: Exception) -> str:
    if isinstance(e, config.ConfigError):
        return str(e)
    orig = getattr(e, "orig", None)
    code = orig.args[0] if orig is not None and orig.args and isinstance(orig.args[0], int) else None
    if code == 1045:
        return "Authentication failed: MySQL rejected the username or password. Check the credentials in .env."
    if code in (1044, 1142):
        return "Access denied: this MySQL user lacks permission for the database/table."
    if code == 1049:
        return "Database not found on the server. Check MYSQL_DATABASE (should be 'flintel')."
    if code in (2002, 2003, 2005, 2006, 2013):
        return ("Cannot reach the MySQL server (unreachable or timed out). Check host/port, "
                "that the Render database is running and reachable from this PC, and MYSQL_SSL.")
    return "Database error%s. Check the console log for details." % (" (code %s)" % code if code else "")


def _build_engine(read_timeout: int):
    s = config.get_db_settings()
    url = URL.create("mysql+pymysql", username=s["user"], password=s["password"],
                     host=s["host"], port=s["port"], database=s["database"],
                     query={"charset": "utf8mb4"})
    args = {"connect_timeout": 10, "read_timeout": read_timeout}
    if config.use_ssl():
        args["ssl"] = {"check_hostname": False}
    engine = create_engine(url, connect_args=args, pool_pre_ping=True, pool_recycle=300)

    @event.listens_for(engine, "connect")
    def _readonly(dbapi_conn, _rec):  # belt and braces: server rejects writes
        cur = dbapi_conn.cursor()
        cur.execute("SET SESSION TRANSACTION READ ONLY")
        cur.close()
    return engine


def get_engine():
    global _engine
    if _engine is None:
        _engine = _build_engine(read_timeout=30)
    return _engine


def get_backup_engine():
    """Separate read-only engine with a long read timeout, used only by the JSON backup."""
    global _backup_engine
    if _backup_engine is None:
        _backup_engine = _build_engine(read_timeout=900)
    return _backup_engine


def run_ro(conn, sql: str):
    """Run a fixed SELECT/SHOW statement as-is (no bind-parameter parsing). Used by backup."""
    if sql.lstrip().split(None, 1)[0].upper() not in ("SELECT", "SHOW"):
        raise RuntimeError("Only SELECT/SHOW statements are allowed")
    return conn.exec_driver_sql(sql)


def _q(conn, sql, params=None):
    if sql.lstrip().split(None, 1)[0].upper() not in ("SELECT", "SHOW"):
        raise RuntimeError("Only SELECT/SHOW statements are allowed")
    return conn.execute(text(sql), params or {})


def ping() -> bool:
    try:
        with get_engine().connect() as c:
            _q(c, "SELECT 1")
        return True
    except Exception:
        return False


def collect_status() -> dict:
    out = {"ok": False, "error": None, "database": None, "version": None, "tables": [],
           "db_size": None, "signals": None, "columns": [], "indexes": [], "warnings": []}
    try:
        engine = get_engine()
        with engine.connect() as c:
            out["ok"] = True
            out["version"] = _q(c, "SELECT VERSION()").scalar()
            out["database"] = _q(c, "SELECT DATABASE()").scalar()
            rows = _q(c, "SELECT table_name AS name, table_rows AS approx_rows, "
                         "data_length AS data_len, index_length AS idx_len "
                         "FROM information_schema.tables WHERE table_schema = DATABASE() "
                         "ORDER BY table_name").all()
            out["tables"] = [dict(r._mapping) for r in rows]
            out["db_size"] = sum((t["data_len"] or 0) + (t["idx_len"] or 0) for t in out["tables"])

            names = [t["name"] for t in out["tables"]]
            if TABLE not in names:
                out["warnings"].append("Table '%s' was not found." % TABLE)
                return out

            sig = {"rows": _q(c, "SELECT COUNT(*) FROM `%s`" % TABLE).scalar()}
            me = next(t for t in out["tables"] if t["name"] == TABLE)
            sig["size"] = (me["data_len"] or 0) + (me["idx_len"] or 0)

            cols = _q(c, "SELECT column_name AS name, column_type AS type, is_nullable AS nullable, "
                         "column_key AS ckey, extra AS extra FROM information_schema.columns "
                         "WHERE table_schema = DATABASE() AND table_name = :t "
                         "ORDER BY ordinal_position", {"t": TABLE}).all()
            out["columns"] = [dict(r._mapping) for r in cols]
            out["indexes"] = [dict(r._mapping) for r in _q(
                c, "SELECT index_name AS name, non_unique AS non_unique, "
                   "GROUP_CONCAT(column_name ORDER BY seq_in_index) AS cols "
                   "FROM information_schema.statistics WHERE table_schema = DATABASE() "
                   "AND table_name = :t GROUP BY index_name, non_unique", {"t": TABLE}).all()]

            blobs = [x["name"] for x in out["columns"]
                     if str(x["type"]).split("(")[0].lower() in BLOB_TYPES]
            col = "embedding" if "embedding" in blobs else (blobs[0] if blobs else None)
            sig["embedding_col"] = col
            sig["embeddings"] = None
            if col and re.fullmatch(r"[A-Za-z0-9_$]+", col):
                sig["embeddings"] = _q(c, "SELECT COUNT(*) FROM `%s` WHERE `%s` IS NOT NULL" % (TABLE, col)).scalar()
            else:
                out["warnings"].append("No BLOB embedding column found in '%s'." % TABLE)
            out["signals"] = sig
    except (SQLAlchemyError, config.ConfigError) as e:
        out["ok"] = out["ok"] and False
        out["error"] = friendly_error(e)
        print("DB error:", type(e).__name__, getattr(e, "orig", e))
    return out

