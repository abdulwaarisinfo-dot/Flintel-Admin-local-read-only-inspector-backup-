import os
from pathlib import Path
from urllib.parse import urlparse, unquote
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")
BACKUP_DIR = BASE_DIR / "backups"
BACKUP_DIR.mkdir(exist_ok=True)


class ConfigError(Exception):
    pass


def get_db_settings() -> dict:
    """Read DB settings from DATABASE_URL, else MYSQL_* variables."""
    url = os.getenv("DATABASE_URL", "").strip()
    if url:
        p = urlparse(url)
        s = dict(
            host=p.hostname or "",
            port=p.port or 3306,
            database=(p.path or "").lstrip("/") or os.getenv("MYSQL_DATABASE", "flintel"),
            user=unquote(p.username or ""),
            password=unquote(p.password or ""),
        )
    else:
        s = dict(
            host=os.getenv("MYSQL_HOST", "").strip(),
            port=int(os.getenv("MYSQL_PORT") or 3306),
            database=os.getenv("MYSQL_DATABASE", "flintel").strip(),
            user=os.getenv("MYSQL_USER", "").strip(),
            password=os.getenv("MYSQL_PASSWORD", ""),
        )
    missing = [k for k in ("host", "user", "password", "database") if not s[k]]
    if missing:
        raise ConfigError(
            "Database settings are incomplete (missing: %s). Fill in .env "
            "(MYSQL_HOST / MYSQL_USER / MYSQL_PASSWORD / MYSQL_DATABASE, or DATABASE_URL)."
            % ", ".join(missing)
        )
    return s


def use_ssl() -> bool:
    return os.getenv("MYSQL_SSL", "").strip().lower() in ("1", "true", "yes", "on")
 
