"""SQLite access layer: schema, connections, safe read-only SQL execution."""
from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pandas as pd

from .config import get_db_path

SCHEMA = """
CREATE TABLE IF NOT EXISTS merchants (
    merchant_id   TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    tier          TEXT NOT NULL CHECK (tier IN ('Enterprise','Growth','Standard')),
    region        TEXT NOT NULL,
    vertical      TEXT NOT NULL,
    onboarded_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    product_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    merchant_id  TEXT NOT NULL REFERENCES merchants(merchant_id),
    sku          TEXT NOT NULL,
    title        TEXT NOT NULL,
    brand        TEXT,
    category     TEXT,
    price        REAL,
    currency     TEXT NOT NULL DEFAULT 'USD',
    image_url    TEXT,
    description  TEXT,
    status       TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','draft','archived')),
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    UNIQUE (merchant_id, sku)
);
CREATE INDEX IF NOT EXISTS idx_products_merchant ON products(merchant_id);
CREATE INDEX IF NOT EXISTS idx_products_category ON products(category);

CREATE TABLE IF NOT EXISTS merchant_requests (
    request_id                TEXT PRIMARY KEY,
    merchant_id               TEXT NOT NULL REFERENCES merchants(merchant_id),
    subject                   TEXT NOT NULL,
    description               TEXT NOT NULL,
    request_type              TEXT NOT NULL,
    priority                  TEXT NOT NULL CHECK (priority IN ('P1','P2','P3','P4')),
    status                    TEXT NOT NULL,
    channel                   TEXT NOT NULL,
    assignee                  TEXT,
    affected_items            INTEGER NOT NULL DEFAULT 0,
    created_at                TEXT NOT NULL,
    due_at                    TEXT NOT NULL,
    first_response_at         TEXT,
    resolved_at               TEXT,
    rework_count              INTEGER NOT NULL DEFAULT 0,
    classification_source     TEXT NOT NULL DEFAULT 'rules',
    classification_confidence REAL
);
CREATE INDEX IF NOT EXISTS idx_req_status ON merchant_requests(status);
CREATE INDEX IF NOT EXISTS idx_req_merchant ON merchant_requests(merchant_id);
CREATE INDEX IF NOT EXISTS idx_req_created ON merchant_requests(created_at);

CREATE TABLE IF NOT EXISTS request_events (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id  TEXT NOT NULL REFERENCES merchant_requests(request_id),
    event_type  TEXT NOT NULL,
    from_status TEXT,
    to_status   TEXT,
    actor       TEXT NOT NULL,
    note        TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_request ON request_events(request_id);

CREATE TABLE IF NOT EXISTS app_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS upload_log (
    upload_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    merchant_id   TEXT NOT NULL,
    filename      TEXT NOT NULL,
    rows_total    INTEGER NOT NULL,
    rows_valid    INTEGER NOT NULL,
    rows_rejected INTEGER NOT NULL,
    rows_inserted INTEGER NOT NULL,
    rows_updated  INTEGER NOT NULL,
    created_at    TEXT NOT NULL
);
"""

TABLES = ["merchants", "products", "merchant_requests", "request_events", "upload_log", "app_meta"]


@contextmanager
def connect(path: str | Path | None = None, readonly: bool = False) -> Iterator[sqlite3.Connection]:
    """Open a connection; commits on success, rolls back on error, always closes."""
    db_path = Path(path) if path else get_db_path()
    if readonly:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        conn.execute("PRAGMA query_only = ON")
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        if not readonly:
            conn.commit()
    except Exception:
        if not readonly:
            conn.rollback()
        raise
    finally:
        conn.close()


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def drop_all(conn: sqlite3.Connection) -> None:
    for table in reversed(TABLES):
        conn.execute(f"DROP TABLE IF EXISTS {table}")


def is_seeded(conn: sqlite3.Connection) -> bool:
    try:
        return conn.execute("SELECT COUNT(*) FROM merchants").fetchone()[0] > 0
    except sqlite3.Error:
        return False


def query_df(conn: sqlite3.Connection, sql: str, params: dict | tuple = ()) -> pd.DataFrame:
    cur = conn.execute(sql, params)
    cols = [c[0] for c in cur.description or []]
    return pd.DataFrame([tuple(r) for r in cur.fetchall()], columns=cols)


# --- Read-only SQL console safety ------------------------------------------
class UnsafeSQLError(ValueError):
    """Raised when a user-supplied query is not a single read-only statement."""


_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|pragma|vacuum|reindex|truncate)\b",
    re.IGNORECASE,
)


def validate_readonly_sql(sql: str) -> str:
    """Return a cleaned single SELECT/WITH statement or raise UnsafeSQLError."""
    cleaned = re.sub(r"--.*?$|/\*.*?\*/", " ", sql or "", flags=re.MULTILINE | re.DOTALL).strip()
    cleaned = cleaned.rstrip(";").strip()
    if not cleaned:
        raise UnsafeSQLError("Query is empty.")
    if ";" in cleaned:
        raise UnsafeSQLError("Only a single statement is allowed.")
    if not re.match(r"^(select|with)\b", cleaned, re.IGNORECASE):
        raise UnsafeSQLError("Only SELECT queries are allowed.")
    if _FORBIDDEN.search(cleaned):
        raise UnsafeSQLError("Query contains a forbidden keyword; the console is read-only.")
    return cleaned


def run_readonly_query(
    sql: str, params: dict | None = None, max_rows: int = 1000, path: str | Path | None = None
) -> pd.DataFrame:
    """Execute a validated read-only query on a read-only connection."""
    cleaned = validate_readonly_sql(sql)
    with connect(path, readonly=True) as conn:
        try:
            df = query_df(conn, f"SELECT * FROM ({cleaned}) LIMIT {int(max_rows)}", params or {})
        except sqlite3.Error as exc:
            raise UnsafeSQLError(f"SQL error: {exc}") from exc
    return df
