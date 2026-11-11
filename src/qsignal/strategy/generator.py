"""信号生成（策略层）。

硬约束（代码化，非 Skill 文本）：
  - 信号生成只能通过 quote_id 引用 quote_table 中 quote_ts <= signal_ts 的行；
    SQL WHERE 子句即为无前视约束，配合 core.audit.audit_no_lookahead() 可审计。
  - 同方向信号未结算前不重复生成。
  - 新信号入库后仅标记 open，不推送。
"""
from __future__ import annotations

from datetime import timedelta
from statistics import median
from typing import Any

from ..core.audit import log_run, now_iso, parse_iso


class LookaheadViolation(RuntimeError):
    """试图引用未来行情。"""


def signal_levels(ratio: float, signal_type: str, cfg) -> tuple[float, float]:
    """按 tp_sl_basis=entry_ratio 计算止盈/止损比值（相对入场比值）。"""
    tp_off = float(cfg["take_profit_offset"])
    sl_off = float(cfg["stop_loss_offset"])
    if signal_type == "spread_long":
        return (ratio - tp_off, ratio + sl_off)
    if signal_type == "spread_short":
        return (ratio + tp_off, ratio - sl_off)
    raise ValueError(f"未知信号类型: {signal_type}")


def estimate_base_ratio(conn, cfg, as_of: str) -> float:
    """基准比值：fixed 直接取配置；rolling_median 取回看窗口内有效行情的中位数。"""
    if cfg.get("base_ratio_mode", "rolling_median") == "fixed":
        return float(cfg["base_ratio"])
    tz = cfg.get("timezone", "Asia/Shanghai")
    days = int(cfg.get("base_ratio_lookback_days", 60))
    cutoff = (parse_iso(as_of, tz) - timedelta(days=days)).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT ratio FROM quote_table "
        "WHERE is_valid=1 AND ratio IS NOT NULL AND quote_ts>=? AND quote_ts<=? "
        "ORDER BY quote_ts",
        (cutoff, as_of),
    ).fetchall()
    values = [float(r["ratio"]) for r in rows]
    return float(median(values)) if values else float(cfg["base_ratio"])


def generate_signal(conn, cfg, quote_id: int,
                    signal_ts: str | None = None,
                    now=None) -> dict[str, Any]:
    """生成一条信号。返回 dict；未触发阈值或重复时 ok=False。

    参数
    ----
    quote_id : 必须指向已落库且有效的行情行
    signal_ts : 信号时刻（ISO8601）；缺省为当前时刻
    """
    tz = cfg.get("timezone", "Asia/Shanghai")
    stamp = signal_ts or now_iso(tz=tz, dt=now)

    # SQL 层强制无前视：晚于信号时刻的行情根本查不出来
    row = conn.execute(
        "SELECT quote_id,quote_ts,price_a,price_b,ratio FROM quote_table "
        "WHERE quote_id=? AND is_valid=1 AND quote_ts<=?",
        (quote_id, stamp),
    ).fetchone()
    if row is None:
        raise LookaheadViolation(
            f"quote_id={quote_id} 不存在/无效/晚于 {stamp}；禁止引用未来行情"
        )

    ratio = float(row["ratio"])
    base = estimate_base_ratio(conn, cfg, row["quote_ts"])
    threshold = float(cfg["spread_threshold"])
    if ratio > base + threshold:
        signal_type = "spread_long"
    elif ratio < base - threshold:
        signal_type = "spread_short"
    else:
        return {"ok": False, "reason": "no_threshold_breach",
                "ratio": ratio, "base_ratio": base, "threshold": threshold}

    duplicate = conn.execute(
        "SELECT id FROM signal_table "
        "WHERE signal_type=? AND config_hash=? AND status='open' LIMIT 1",
        (signal_type, cfg.config_hash),
    ).fetchone()
    if duplicate:
        return {"ok": False, "reason": "duplicate_open_signal",
                "signal_type": signal_type, "open_signal_id": duplicate["id"]}

    tp, sl = signal_levels(ratio, signal_type, cfg)
    cur = conn.execute(
        "INSERT INTO signal_table"
        "(signal_ts,signal_type,price_a,price_b,ratio,base_ratio,tp,sl,status,"
        " quote_id,config_hash,strategy_version,note) "
        "VALUES(?,?,?,?,?,?,?,?,'open',?,?,?,?)",
        (stamp, signal_type, row["price_a"], row["price_b"], ratio, base,
         tp, sl, quote_id, cfg.config_hash, cfg.strategy_version,
         "pending_settlement"),
    )
    signal_id = int(cur.lastrowid)
    conn.commit()
    log_run(
        conn, "generator", "signal_created",
        input_obj={"quote_id": quote_id, "quote_ts": row["quote_ts"],
                   "ratio": ratio, "base_ratio": base, "threshold": threshold,
                   "config_hash": cfg.config_hash, "strategy_version": cfg.strategy_version},
        output_obj={"signal_id": signal_id, "signal_type": signal_type,
                    "tp": tp, "sl": sl, "status": "open"},
        message="信号入库，待结算，不推送",
        ts=stamp,
    )
    return {
        "ok": True, "signal_id": signal_id, "signal_type": signal_type,
        "ratio": ratio, "base_ratio": base, "tp": tp, "sl": sl,
        "quote_id": quote_id, "signal_ts": stamp,
    }
