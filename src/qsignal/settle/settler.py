"""自动结算与信号闭环。

判定依据优先使用分钟 K 线高低（由 quote_table 的 5s 采样按分钟聚合），
而非单点快照，以修复离散采样下止盈止损先后不可观测的问题。

硬规则（代码化）：
  - 同一分钟区间内 tp 与 sl 同时被触及且无法定序 -> 保守判 loss，ambiguity=1
  - 当日 15:00 收盘仍未触达 -> hold（日内不隔夜），hold 不计入胜率统计
  - 结算写入 WHERE status='open'，重复运行幂等
  - 结算完成不刷新统计快照（快照每日 15:10 唯一冻结一次）
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from ..core.audit import log_run, now_iso, parse_iso
from ..core.calendar import parse_hhmm, to_local


@dataclass
class Bar:
    """一分钟区间的比值 K 线。"""
    bar_ts: str
    ratio_high: float
    ratio_low: float
    ratio_close: float


@dataclass
class Settlement:
    action: str                 # close | keep_open
    result: str | None          # win | loss | hold
    ambiguity: int
    result_ratio: float | None
    reason: str
    bar_ts: str | None


def build_bars(rows) -> list[Bar]:
    """把 5s 原始行情按分钟聚合为 high/low/close 序列（时间升序）。"""
    if not rows:
        return []
    first_ts = rows[0]["quote_ts"]
    offset = first_ts[-6:] if len(first_ts) > 10 and first_ts[-6] in "+-" else "+08:00"
    groups: dict[str, list[float]] = {}
    for row in rows:
        key = row["quote_ts"][:16]
        if row["ratio"] is not None:
            groups.setdefault(key, []).append(float(row["ratio"]))
    bars = []
    for key in sorted(groups):
        values = groups[key]
        bars.append(Bar(
            bar_ts=key + ":00" + offset,
            ratio_high=max(values), ratio_low=min(values),
            ratio_close=values[-1],
        ))
    return bars


def evaluate(signal, bar: Bar, cfg=None) -> Settlement:
    """对单根分钟 K 线判定止盈止损触及情况。"""
    signal_type = signal["signal_type"]
    tp, sl = float(signal["tp"]), float(signal["sl"])
    high, low = bar.ratio_high, bar.ratio_low
    if signal_type == "spread_long":
        hit_tp = low <= tp
        hit_sl = high >= sl
    elif signal_type == "spread_short":
        hit_tp = high >= tp
        hit_sl = low <= sl
    else:
        raise ValueError(f"未知信号类型: {signal_type}")

    # 保守分支：同一区间双触，路径不可观测，判 loss 并标记歧义
    if hit_tp and hit_sl:
        return Settlement("close", "loss", 1, bar.ratio_close,
                          "ambiguous_path_both_hit", bar.bar_ts)
    if hit_tp:
        return Settlement("close", "win", 0, tp, "take_profit", bar.bar_ts)
    if hit_sl:
        return Settlement("close", "loss", 0, sl, "stop_loss", bar.bar_ts)
    return Settlement("keep_open", None, 0, None, "no_touch", bar.bar_ts)


def _apply_settlement(conn, signal, outcome: Settlement, stamp: str) -> bool:
    cur = conn.execute(
        "UPDATE signal_table SET status='closed',settle_ts=?,result=?,result_ratio=?,"
        "ambiguity=?,note=? WHERE id=? AND status='open'",
        (stamp, outcome.result, outcome.result_ratio, outcome.ambiguity,
         outcome.reason, signal["id"]),
    )
    if cur.rowcount == 0:
        return False
    conn.commit()
    log_run(conn, "settler", "signal_closed",
            input_obj={"signal_id": signal["id"],
                       "signal_type": signal["signal_type"],
                       "tp": signal["tp"], "sl": signal["sl"]},
            output_obj=asdict(outcome),
            message="结算完成；本事件不刷新统计快照",
            ts=stamp)
    return True


def settle_open_signals(conn, cfg, now: datetime | None = None) -> dict[str, Any]:
    """扫描全部 open 信号并闭环。全天可运行，交易时段外仅做收盘判定。"""
    tz = cfg.get("timezone", "Asia/Shanghai")
    moment = now or datetime.now(ZoneInfo(tz))
    stamp = now_iso(tz=tz, dt=moment)
    local = to_local(moment, tz)
    close_time = parse_hhmm(cfg["trade_end2"])
    eod_reached = local.time() >= close_time

    open_rows = conn.execute(
        "SELECT * FROM signal_table WHERE status='open' ORDER BY signal_ts, id"
    ).fetchall()
    closed: list[dict[str, Any]] = []
    for signal in open_rows:
        signal_day = parse_iso(signal["signal_ts"], tz).date()
        forced_eod = eod_reached or signal_day < local.date()
        rows = conn.execute(
            "SELECT quote_ts,ratio FROM quote_table "
            "WHERE is_valid=1 AND ratio IS NOT NULL AND quote_ts>? AND quote_ts<=? "
            "ORDER BY quote_ts, quote_id",
            (signal["signal_ts"], stamp),
        ).fetchall()
        bars = build_bars(rows)
        outcome = Settlement("keep_open", None, 0, None, "no_bars", None)
        for bar in bars:
            trial = evaluate(signal, bar, cfg)
            if trial.action == "close":
                outcome = trial
                break
        if outcome.action == "keep_open" and forced_eod:
            close_ratio = bars[-1].ratio_close if bars else float(signal["ratio"])
            outcome = Settlement("close", "hold", 0, close_ratio,
                                 "eod_close", stamp)
        if outcome.action == "close":
            if _apply_settlement(conn, signal, outcome, stamp):
                closed.append({"signal_id": signal["id"], "result": outcome.result,
                               "ambiguity": outcome.ambiguity,
                               "reason": outcome.reason})
    return {"scanned": len(open_rows), "closed": len(closed),
            "details": closed, "eod": eod_reached, "ts": stamp}
