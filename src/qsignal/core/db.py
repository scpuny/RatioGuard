"""SQLite 连接与初始化（WAL + busy_timeout，支持并发读写）。"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCHEMA_PATH = ROOT / "sql" / "schema.sql"

EXPECTED_TABLES = (
    "quote_table", "signal_table", "snapshot_table",
    "drift_log", "run_log", "system_state",
)


def connect(db_path: str | Path, timeout: float = 30.0) -> sqlite3.Connection:
    """建立连接。':memory:' 用于自检与单测。"""
    p = Path(db_path)
    if str(p) != ":memory:":
        p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=timeout, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(db_path: str | Path, schema_path: str | Path | None = None) -> sqlite3.Connection:
    """执行建表脚本并返回已提交的连接（调用方负责 close）。"""
    conn = connect(db_path)
    sql = Path(schema_path or SCHEMA_PATH).read_text(encoding="utf-8")
    conn.executescript(sql)
    conn.commit()
    return conn


def table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {r["name"] for r in rows}


def assert_schema(conn: sqlite3.Connection) -> None:
    missing = set(EXPECTED_TABLES) - table_names(conn)
    if missing:
        raise RuntimeError(f"数据库缺少表: {sorted(missing)}")


@contextmanager
def transaction(conn: sqlite3.Connection):
    """显式事务。异常回滚，正常提交。"""
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def get_state(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM system_state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(conn: sqlite3.Connection, key: str, value: str,
              operator: str = "system", reason: str = "", ts: str | None = None,
              commit: bool = True) -> None:
    from .audit import now_iso
    conn.execute(
        "INSERT INTO system_state(key,value,updated_ts,operator,reason) VALUES(?,?,?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_ts=excluded.updated_ts, "
        "operator=excluded.operator, reason=excluded.reason",
        (key, str(value), ts or now_iso(), operator, reason),
    )
    if commit:
        conn.commit()
