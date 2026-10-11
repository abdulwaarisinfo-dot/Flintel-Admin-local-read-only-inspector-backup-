"""Logical backup via mysqldump (read-only on the server)."""
import glob, os, re, shutil, subprocess
from datetime import datetime
from . import config

NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+\.sql$")


class BackupError(Exception):
    pass


def find_mysqldump():
    custom = os.getenv("MYSQLDUMP_PATH", "").strip().strip('"')
    if custom:
        return custom if os.path.isfile(custom) else None
    found = shutil.which("mysqldump")
    if found:
        return found
    for pat in (r"C:\Program Files\MySQL\*\bin\mysqldump.exe",
                r"C:\Program Files\MariaDB*\bin\mysqldump.exe",
                r"C:\xampp\mysql\bin\mysqldump.exe"):
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[-1]
    return None


def _help(exe):
    try:
        r = subprocess.run([exe, "--help"], capture_output=True, text=True, timeout=20)
        return r.stdout + r.stderr
    except Exception:
        return ""


def create_backup() -> str:
    exe = find_mysqldump()
    if not exe:
        raise BackupError("mysqldump was not found. Install the MySQL client tools, or set "
                          "MYSQLDUMP_PATH in .env to the full path of mysqldump.exe.")
    try:
        s = config.get_db_settings()
    except config.ConfigError as e:
        raise BackupError(str(e))

    helptxt = _help(exe)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    final = config.BACKUP_DIR / ("%s_%s.sql" % (re.sub(r"[^A-Za-z0-9_-]", "", s["database"]), stamp))
    tmp = final.with_suffix(".sql.tmp")

    cmd = [exe, "--host=%s" % s["host"], "--port=%s" % s["port"], "--user=%s" % s["user"],
           "--protocol=TCP",
           "--single-transaction",   # consistent snapshot, no table locks
           "--quick", "--hex-blob",  # BLOB/embedding bytes stored as hex: exact round-trip
           "--default-character-set=utf8mb4",
           "--triggers", "--result-file=%s" % tmp]
    # structure (incl. indexes, PK, unique keys, AUTO_INCREMENT) + data are included by default
    for flag in ("--no-tablespaces", "--set-gtid-purged=OFF"):
        if flag.split("=")[0] in helptxt:
            cmd.append(flag)
    if "column-statistics" in helptxt:
        cmd.append("--column-statistics=0")
    if config.use_ssl():
        cmd.append("--ssl-mode=REQUIRED" if "ssl-mode" in helptxt else "--ssl")
    cmd.append(s["database"])

    env = dict(os.environ, MYSQL_PWD=s["password"])  # keeps password off the command line
    try:
        r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        tmp.unlink(missing_ok=True)
        raise BackupError("Backup timed out after 15 minutes.")
    except OSError:
        raise BackupError("mysqldump could not be started.")

    if r.returncode != 0:
        tmp.unlink(missing_ok=True)
        err = (r.stderr or "").replace(s["password"], "***")[-400:]
        print("mysqldump failed:", err)
        low = err.lower()
        if "access denied" in low:
            raise BackupError("Backup failed: MySQL rejected the credentials or lacks privileges (SELECT, LOCK/PROCESS not required).")
        if "can't connect" in low or "unknown mysql server host" in low or "lost connection" in low:
            raise BackupError("Backup failed: cannot reach the MySQL server.")
        raise BackupError("Backup failed. mysqldump said: " + (err.strip().splitlines() or ["unknown error"])[-1])

    ok = tmp.exists() and tmp.stat().st_size > 0
    if ok:
        with open(tmp, "rb") as f:
            f.seek(max(0, tmp.stat().st_size - 300))
            ok = b"Dump completed" in f.read()
    if not ok:
        tmp.unlink(missing_ok=True)
        raise BackupError("Backup file looks incomplete and was discarded.")
    tmp.replace(final)
    return final.name


def list_backups():
    out = []
    for p in config.BACKUP_DIR.glob("*.sql"):
        st = p.stat()
        out.append({"name": p.name, "size": st.st_size,
                    "when": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")})
    return sorted(out, key=lambda x: x["name"], reverse=True)


def backup_path(name: str):
    if not NAME_RE.match(name):
        return None
    p = (config.BACKUP_DIR / name).resolve()
    return p if p.parent == config.BACKUP_DIR.resolve() and p.is_file() else None
