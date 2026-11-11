"""测试公共基座：临时 SQLite、配置构造、造数助手。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qsignal.core.audit import now_iso  # noqa: E402
from qsignal.core.config import Config, load_config  # noqa: E402
from qsignal.core.db import connect, init_db  # noqa: E402


def make_cfg(**overrides) -> Config:
    """以真实 config.yaml 为底，覆盖少量字段。"""
    base = load_config(ROOT / "config.yaml")
    raw = dict(base.raw)
    raw.update(overrides)
    return Config(raw, base.path)


def local_ts(day: str, hhmm: str) -> str:
    """构造带 +08:00 偏移的 ISO8601 时间戳。"""
    return f"{day}T{hhmm}:00+08:00"


class TempDBMixin:
    """每个用例一个独立临时库。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "acceptance.db"
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()


def insert_quote(conn, quote_ts: str, price_a: float = 4.0, price_b: float = 4.0,
                 source: str = "fake", is_valid: int = 1,
                 drop_reason: str | None = None,
                 iopv_a: float | None = None, iopv_b: float | None = None,
                 code_a: str = "510300", code_b: str = "159919") -> int:
    ratio = (price_a / price_b) if price_b else None
    cur = conn.execute(
        "INSERT OR IGNORE INTO quote_table"
        "(quote_ts,ingest_ts,source,code_a,price_a,code_b,price_b,ratio,iopv_a,iopv_b,is_valid,drop_reason) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (quote_ts, now_iso(), source, code_a, price_a, code_b, price_b,
         ratio, iopv_a, iopv_b, is_valid, drop_reason),
    )
    conn.commit()
    row = conn.execute("SELECT quote_id FROM quote_table WHERE quote_ts=? AND source=?",
                       (quote_ts, source)).fetchone()
    return int(row["quote_id"]) if row else int(cur.lastrowid)


def insert_signal(conn, signal_ts: str, *, signal_type: str = "spread_long",
                  result: str = "win", status: str = "closed",
                  config_hash: str = "cfgtest", ambiguity: int = 0,
                  quote_id: int | None = None, settle_ts: str | None = None,
                  ratio: float = 1.010, tp: float | None = None,
                  sl: float | None = None, note: str = "") -> int:
    if tp is None:
        tp = ratio - 0.002 if signal_type == "spread_long" else ratio + 0.002
    if sl is None:
        sl = ratio + 0.004 if signal_type == "spread_long" else ratio - 0.004
    conn.execute(
        "INSERT OR IGNORE INTO signal_table"
        "(signal_ts,signal_type,price_a,price_b,ratio,base_ratio,tp,sl,status,settle_ts,"
        " result,result_ratio,quote_id,config_hash,strategy_version,ambiguity,note) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (signal_ts, signal_type, 4.04, 4.0, ratio, 1.005, tp, sl, status,
         settle_ts or signal_ts, result, ratio, quote_id, config_hash,
         "pair_ratio_v1", ambiguity, note),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM signal_table WHERE signal_ts=? AND signal_type=? AND config_hash=?",
        (signal_ts, signal_type, config_hash),
    ).fetchone()
    return int(row["id"])


def seed_samples(conn, wins: int, losses: int, holds: int = 0,
                 config_hash: str = "cfgtest", day: str = "2026-11-10",
                 ambiguous: int = 0, spread_days: int = 1,
                 shuffle: bool = False) -> None:
    """批量造已闭环样本，时间戳严格递增且唯一。

    shuffle=False 保持 win -> loss -> hold 顺序（便于窗口构造）；
    shuffle=True 用固定种子打散（便于验证 hold 与有效样本交错）。
    """
    from datetime import datetime, timedelta
    import random

    start = datetime.fromisoformat(f"{day}T09:30:00+08:00")
    span = max(spread_days, 1)
    total = wins + losses + holds
    outcomes = ["win"] * wins + ["loss"] * losses + ["hold"] * holds
    if shuffle:
        random.Random(7).shuffle(outcomes)
    ambiguous_left = ambiguous
    for idx, outcome in enumerate(outcomes):
        # 按索引均匀映射到 span 个交易日；seconds=idx 保证时间戳全局严格递增
        day_offset = min(span - 1, (idx * span) // total) if total else 0
        ts = (start + timedelta(days=day_offset,
                                seconds=idx)).isoformat(timespec="seconds")
        flag = 0
        if outcome == "loss" and ambiguous_left > 0:
            flag, ambiguous_left = 1, ambiguous_left - 1
        insert_signal(
            conn, ts, result=outcome, config_hash=config_hash,
            ambiguity=flag, settle_ts=ts,
        )
