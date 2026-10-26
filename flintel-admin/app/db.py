"""Read-only database access. Only fixed SELECT/SHOW queries are ever run."""
import re
import time
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import SQLAlchemyError
from . import config

TABLE = "flintel_signals"
BLOB_TYPES = {"tinyblob", "blob", "mediumblob", "longblob"}
TEXT_TYPES = {"char", "varchar", "tinytext", "text", "mediumtext", "longtext", "json"}
SAFE_NAME = re.compile(r"[A-Za-z0-9_$]+")
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


def _columns(c):
    rows = _q(c, "SELECT column_name AS name, column_type AS type, is_nullable AS nullable, "
                 "column_key AS ckey, extra AS extra FROM information_schema.columns "
                 "WHERE table_schema = DATABASE() AND table_name = :t "
                 "ORDER BY ordinal_position", {"t": TABLE}).all()
    return [dict(r._mapping) for r in rows]


def _is_blob(col) -> bool:
    return str(col["type"]).split("(")[0].lower() in BLOB_TYPES


def _embedding_col(cols):
    blobs = [x["name"] for x in cols if _is_blob(x)]
    col = "embedding" if "embedding" in blobs else (blobs[0] if blobs else None)
    return col if col and SAFE_NAME.fullmatch(col) else None


_counts_cache = {"t": 0.0, "data": None}


def exact_counts(force: bool = False) -> dict:
    """Exact COUNT(*) numbers. Slow on big tables, so: only on demand, long timeout, cached 10 min."""
    if not force and _counts_cache["data"] and time.time() - _counts_cache["t"] < 600:
        return dict(_counts_cache["data"], cached=True)
    with get_backup_engine().connect() as c:           # 900 s read timeout
        cols = _columns(c)
        emb = _embedding_col(cols)
        data = {"rows": run_ro(c, "SELECT COUNT(*) FROM `%s`" % TABLE).scalar(),
                "embedding_col": emb, "embeddings": None}
        if emb:
            data["embeddings"] = run_ro(c, "SELECT COUNT(`%s`) FROM `%s`" % (emb, TABLE)).scalar()
    _counts_cache.update(t=time.time(), data=data)
    return dict(data, cached=False)


TEXT_PREVIEW = 300


def _plain(v):
    if isinstance(v, (bytes, bytearray, memoryview)):
        return "(binary, %d bytes)" % len(v)
    if v is None or isinstance(v, (int, float, str)):
        return v
    return str(v)


def browse(before=None, limit: int = 50) -> dict:
    """One page of the table, newest first, keyset pagination on the primary key.
    Never uses OFFSET and never reads BLOB bytes; long text is cut to a preview."""
    limit = max(1, min(int(limit), 200))
    with get_engine().connect() as c:
        cols = [x for x in _columns(c) if SAFE_NAME.fullmatch(x["name"])]
        pks = [x["name"] for x in cols if x["ckey"] == "PRI"]
        if len(pks) != 1:
            raise RuntimeError("Browsing needs a table with a single-column primary key.")
        pk = pks[0]
        parts = []
        for x in cols:
            n, t = x["name"], str(x["type"]).split("(")[0].lower()
            if _is_blob(x):
                parts.append("LENGTH(`%s`) AS `%s`" % (n, n))          # size only, not the bytes
            elif t in TEXT_TYPES:
                parts.append("LEFT(`%s`, %d) AS `%s`" % (n, TEXT_PREVIEW, n))
            else:
                parts.append("`%s`" % n)
        sql = "SELECT %s FROM `%s`" % (", ".join(parts), TABLE)
        params = {"lim": limit}
        if before not in (None, ""):
            sql += " WHERE `%s` < :b" % pk
            params["b"] = before
        sql += " ORDER BY `%s` DESC LIMIT :lim" % pk
        res = _q(c, sql, params)
        keys = list(res.keys())
        rows = [[_plain(v) for v in r] for r in res.all()]
    nxt = rows[-1][keys.index(pk)] if len(rows) == limit else None
    blobs = [x["name"] for x in cols if _is_blob(x)]
    return {"columns": keys, "rows": rows, "next": nxt, "pk": pk, "blob_cols": blobs, "limit": limit}


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

            me = next(t for t in out["tables"] if t["name"] == TABLE)
            # NO COUNT(*) here: on a big table it scans every row (and every BLOB) on each page load.
            # The approximate row count comes free from information_schema; exact counts are on demand.
            sig = {"rows": me["approx_rows"], "rows_exact": False,
                   "size": (me["data_len"] or 0) + (me["idx_len"] or 0)}

            cols = _columns(c)
            out["columns"] = cols
            out["indexes"] = [dict(r._mapping) for r in _q(
                c, "SELECT index_name AS name, non_unique AS non_unique, "
                   "GROUP_CONCAT(column_name ORDER BY seq_in_index) AS cols "
                   "FROM information_schema.statistics WHERE table_schema = DATABASE() "
                   "AND table_name = :t GROUP BY index_name, non_unique", {"t": TABLE}).all()]

            sig["embedding_col"] = _embedding_col(cols)
            sig["embeddings"] = None
            if not sig["embedding_col"]:
                out["warnings"].append("No BLOB embedding column found in '%s'." % TABLE)
            out["signals"] = sig
    except (SQLAlchemyError, config.ConfigError) as e:
        out["ok"] = out["ok"] and False
        out["error"] = friendly_error(e)
        print("DB error:", type(e).__name__, getattr(e, "orig", e))
    return out

