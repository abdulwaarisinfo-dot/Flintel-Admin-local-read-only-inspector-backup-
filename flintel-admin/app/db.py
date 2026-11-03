"""Read-only database access. Only fixed SELECT/SHOW queries are ever run."""
import re
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import SQLAlchemyError
from . import config

TABLE = "flintel_signals"
# Hacker News table (service ke HN_MYSQL_TABLE se match; env se badal sakte hain).
HN_TABLE = (__import__("os").getenv("HN_MYSQL_TABLE") or "hacker_news").strip() or "hacker_news"
# HN ka DATABASE naam (optional). Khali = wahi database jis se connect hain (MYSQL_DATABASE).
HN_DB = (__import__("os").getenv("HN_MYSQL_DATABASE") or "").strip()
BLOB_TYPES = {"tinyblob", "blob", "mediumblob", "longblob"}
_engine = None
_backup_engine = None


def _detail(e: Exception) -> str:
    """Asli MySQL error (code + message) — isme user/host/table ka naam hota hai, password nahi."""
    orig = getattr(e, "orig", None)
    if orig is not None and getattr(orig, "args", None):
        a = orig.args
        if len(a) >= 2 and isinstance(a[0], int):
            return " [MySQL %s: %s]" % (a[0], a[1])
        return " [%s]" % (a,)
    return ""


def friendly_error(e: Exception) -> str:
    if isinstance(e, config.ConfigError):
        return str(e)
    orig = getattr(e, "orig", None)
    code = orig.args[0] if orig is not None and orig.args and isinstance(orig.args[0], int) else None
    d = _detail(e)
    if code == 1045:
        return "Authentication failed: MySQL rejected the username or password. Check the credentials in .env." + d
    if code == 1044:
        m = re.search(r"to database '([^']+)'", str(orig.args[1]) if len(orig.args) > 1 else "")
        bad = m.group(1) if m else ""
        if bad and bad in (HN_TABLE, TABLE):
            return ("'%s' is a TABLE, not a database. Set MYSQL_DATABASE=flintel (the database that CONTAINS the "
                    "'%s' table) in .env, or the database name at the end of DATABASE_URL (.../flintel), then restart. "
                    "The Hacker News data is shown automatically from the '%s' table." % (bad, bad, bad)) + d
    if code in (1044, 1142, 1143):
        return ("Access denied: this MySQL user lacks permission for the database/table. "
                "Fix: use the same MYSQL_USER/MYSQL_PASSWORD/MYSQL_DATABASE as the Flintel service, or run "
                "GRANT SELECT, SHOW VIEW ON <database>.* TO '<user>'@'%'; FLUSH PRIVILEGES;" + d)
    if code == 1049:
        return "Database not found on the server. Check MYSQL_DATABASE (should be 'flintel')." + d
    if code in (2002, 2003, 2005, 2006, 2013):
        return ("Cannot reach the MySQL server (unreachable or timed out). Check host/port, "
                "that the Render database is running and reachable from this PC, and MYSQL_SSL." + d)
    return "Database error%s. Check the console log for details." % (" (code %s)" % code if code else "") + d


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


def _count_scope(columns, warnings):
    """What the dashboard may count. Returns (where_sql, range_info, state).
    state "full"  : no range in .env -> count the whole table, exactly as before.
    state "range" : BACKUP_FROM / BACKUP_TO set -> count ONLY those ids (a primary-key range, fast);
                    the rest of the table is never read.
    state "error" : bad value / no single primary key -> counts are skipped, nothing is scanned."""
    from . import backup   # imported here because backup.py imports db at the top
    try:
        rng = backup._range_settings()
    except backup.BackupError as e:
        warnings.append("Counts skipped: " + str(e))
        return None, None, "error"
    if not rng:
        return "", None, "full"
    pks = [x["name"] for x in columns if x["ckey"] == "PRI"]
    if len(pks) != 1 or not re.fullmatch(r"[A-Za-z0-9_$]+", pks[0]):
        warnings.append("A backup range is set but '%s' has no single-column primary key, so counts are skipped." % TABLE)
        return None, None, "error"
    lo, hi = rng
    where = " WHERE `%s` >= %d" % (pks[0], lo)
    if hi is not None:
        where += " AND `%s` <= %d" % (pks[0], hi)
    return where, {"column": pks[0], "from": lo, "to": hi}, "range"


def _hn_status(c, tables) -> dict:
    """Hacker News table ka read-only status. Database = HN_MYSQL_DATABASE (khali ho to connected DB),
    table = HN_MYSQL_TABLE. Exact rows, stories/comments, embeddings, latest/earliest fetched_at.
    Table na ho ya koi query fail ho to dashboard/baqi status kabhi nahi tootta."""
    schema = HN_DB or (_q(c, "SELECT DATABASE()").scalar() or "")
    info = {"table": HN_TABLE, "schema": schema, "full": "%s.%s" % (schema, HN_TABLE), "exists": False,
            "rows": None, "embeddings": None, "size": None, "latest": None, "earliest": None,
            "by_platform": [], "columns": [], "indexes": [], "error": None}
    ident = r"[A-Za-z0-9_$]+"
    if not (re.fullmatch(ident, HN_TABLE) and re.fullmatch(ident, schema or "x")):
        info["error"] = "HN_MYSQL_DATABASE / HN_MYSQL_TABLE has invalid characters."
        return info
    full = "`%s`.`%s`" % (schema, HN_TABLE)
    try:
        me = _q(c, "SELECT data_length AS data_len, index_length AS idx_len FROM information_schema.tables "
                   "WHERE table_schema = :s AND table_name = :t", {"s": schema, "t": HN_TABLE}).first()
        if me is None:
            return info
        info["exists"] = True
        info["size"] = (me.data_len or 0) + (me.idx_len or 0)
        info["rows"] = _q(c, "SELECT COUNT(*) FROM %s" % full).scalar()
        cols = _q(c, "SELECT column_name AS name, column_type AS type, is_nullable AS nullable, "
                     "column_key AS ckey, extra AS extra FROM information_schema.columns "
                     "WHERE table_schema = :s AND table_name = :t ORDER BY ordinal_position",
                  {"s": schema, "t": HN_TABLE}).all()
        info["columns"] = [dict(r._mapping) for r in cols]
        names = {x["name"] for x in info["columns"]}
        info["indexes"] = [dict(r._mapping) for r in _q(
            c, "SELECT index_name AS name, non_unique AS non_unique, "
               "GROUP_CONCAT(column_name ORDER BY seq_in_index) AS cols "
               "FROM information_schema.statistics WHERE table_schema = :s "
               "AND table_name = :t GROUP BY index_name, non_unique", {"s": schema, "t": HN_TABLE}).all()]
        if "embedding" in names:
            info["embeddings"] = _q(c, "SELECT COUNT(*) FROM %s WHERE `embedding` IS NOT NULL" % full).scalar()
        if "fetched_at" in names:
            row = _q(c, "SELECT MIN(`fetched_at`), MAX(`fetched_at`) FROM %s" % full).first()
            if row:
                info["earliest"], info["latest"] = row[0], row[1]
        if "platform" in names:
            info["by_platform"] = [dict(r._mapping) for r in _q(
                c, "SELECT `platform` AS platform, COUNT(*) AS n FROM %s GROUP BY `platform` "
                   "ORDER BY n DESC" % full).all()]
            bp = {x["platform"]: x["n"] for x in info["by_platform"]}
            info["stories"] = bp.get("hackernews", 0)
            info["comment_docs"] = bp.get("hackernews_comment", 0)
            info["nested_comments"] = None
            if "hn_comments" in names:
                try:
                    info["nested_comments"] = int(_q(
                        c, "SELECT COALESCE(SUM(JSON_LENGTH(`hn_comments`)), 0) FROM %s "
                           "WHERE `platform` = 'hackernews'" % full).scalar() or 0)
                except SQLAlchemyError as e:
                    print("HN nested comments count failed:", getattr(e, "orig", e))
            info["total_comments"] = info["comment_docs"] + (info["nested_comments"] or 0)
    except SQLAlchemyError as e:
        info["error"] = friendly_error(e)
        print("HN status error:", type(e).__name__, getattr(e, "orig", e))
    return info


def collect_status() -> dict:
    out = {"ok": False, "error": None, "database": None, "version": None, "tables": [],
           "db_size": None, "signals": None, "hn": None, "backup_tables": None, "columns": [], "indexes": [], "warnings": []}
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

            try:
                from . import backup as _b
                out["backup_tables"] = _b._table_filter()
            except Exception:
                out["backup_tables"] = None
            out["hn"] = _hn_status(c, out["tables"])   # hacker_news card (flintel_signals se alag, independent)

            names = [t["name"] for t in out["tables"]]
            if TABLE not in names:
                out["warnings"].append("Table '%s' was not found (or this MySQL user has no SELECT permission on it)." % TABLE)
                return out

            try:
                sig = {}
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

                where, sig["range"], sig["range_state"] = _count_scope(out["columns"], out["warnings"])
                sig["rows"] = None if where is None else _q(c, "SELECT COUNT(*) FROM `%s`%s" % (TABLE, where)).scalar()

                blobs = [x["name"] for x in out["columns"]
                         if str(x["type"]).split("(")[0].lower() in BLOB_TYPES]
                col = "embedding" if "embedding" in blobs else (blobs[0] if blobs else None)
                sig["embedding_col"] = col
                sig["embeddings"] = None
                if col and re.fullmatch(r"[A-Za-z0-9_$]+", col):
                    if where is not None:
                        cond = (where + " AND" if where else " WHERE") + " `%s` IS NOT NULL" % col
                        sig["embeddings"] = _q(c, "SELECT COUNT(*) FROM `%s`%s" % (TABLE, cond)).scalar()
                else:
                    out["warnings"].append("No BLOB embedding column found in '%s'." % TABLE)
                out["signals"] = sig
            except SQLAlchemyError as e:   # sirf is table par permission/error: baqi dashboard chalta rahe
                out["signals"] = None
                out["warnings"].append("Could not read '%s': %s" % (TABLE, friendly_error(e)))
                print("signals status error:", type(e).__name__, getattr(e, "orig", e))
    except (SQLAlchemyError, config.ConfigError) as e:
        out["ok"] = out["ok"] and False
        out["error"] = friendly_error(e)
        print("DB error:", type(e).__name__, getattr(e, "orig", e))
    return out
