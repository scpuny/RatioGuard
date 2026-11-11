"""统计快照计算与每日 15:10 冻结。

硬规则（代码化）：
  - hold 不参与胜率、Wilson 区间、盈亏平衡判定，单独统计 hold 率
  - 样本默认按 config_hash 分组，不跨配置版本合并
  - qualified 判定顺序固定：硬下限 -> 样本量 -> 交易日跨度 -> 漂移暂停 -> ci_low > break_even
  - 快照每日 15:10 唯一冻结一次；结算事件不写 snapshot_table，盘中不刷新放行依据
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from ..core.audit import log_run, now_iso
from ..core.calendar import to_local
from ..core.db import get_state
from ..core.formulas import ambiguity_rate, break_even, hold_rate
from ..core.hard_rules import MIN_QUALIFIED_SAMPLES_FLOOR
from ..core.wilson import wilson_interval

SCOPES = ("full", "roll100", "roll200")


@dataclass
class Snapshot:
    scope: str
    config_hash: str
    strategy_version: str
    n: int
    win_cnt: int
    loss_cnt: int
    hold_cnt: int
    ambiguous_cnt: int
    effective_sessions: int
    win_rate: float
    break_even: float
    ci_low: float
    ci_high: float
    qualified: bool
    qualified_reason: str
    ambiguity_rate: float
    hold_rate: float
    cost_per_unit: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_samples(conn, cfg, allow_merge: bool | None = None) -> list[dict[str, Any]]:
    """载入已闭环样本。默认按当前 config_hash 分组。"""
    merge = cfg.get("allow_cross_version_merge", False) if allow_merge is None else allow_merge
    base = ("SELECT * FROM signal_table "
            "WHERE status='closed' AND result IN ('win','loss','hold')")
    if merge:
        rows = conn.execute(base + " ORDER BY settle_ts, id").fetchall()
    else:
        rows = conn.execute(base + " AND config_hash=? ORDER BY settle_ts, id",
                            (cfg.config_hash,)).fetchall()
    return [dict(r) for r in rows]


def _qualify(n: int, sessions: int, ci_low: float, break_even_value: float,
             cfg, paused: bool) -> tuple[bool, str]:
    if n < MIN_QUALIFIED_SAMPLES_FLOOR:
        return False, "below_hard_floor"
    if n < int(cfg["min_qualified_samples"]):
        return False, "insufficient_sample"
    if sessions < int(cfg["min_qualified_sessions"]):
        return False, "insufficient_sessions"
    if paused:
        return False, "drift_paused"
    if ci_low > break_even_value:
        return True, "qualified"
    return False, "ci_low_below_break_even"


def compute_snapshot(conn, cfg, scope: str = "full",
                     as_of: str | None = None,
                     allow_merge: bool | None = None) -> Snapshot:
    """计算单个 scope 的快照。可被 /report 调用，但不会写入冻结表。"""
    stamp = as_of or now_iso(tz=cfg.get("timezone", "Asia/Shanghai"))
    rows = load_samples(conn, cfg, allow_merge)
    valid = [r for r in rows if r["result"] in ("win", "loss")]
    if scope == "roll100":
        valid = valid[-int(cfg["roll_window_1"]):]
    elif scope == "roll200":
        valid = valid[-int(cfg["roll_window_2"]):]
    elif scope != "full":
        raise ValueError(f"未知 scope: {scope}")

    n = len(valid)
    win_cnt = sum(1 for r in valid if r["result"] == "win")
    loss_cnt = n - win_cnt
    ambiguous_cnt = sum(int(r["ambiguity"] or 0) for r in valid)
    sessions = len({r["signal_ts"][:10] for r in valid})

    if scope == "full":
        # 全量快照的 hold 覆盖全部已闭环样本
        hold_cnt = sum(1 for r in rows if r["result"] == "hold")
    elif valid:
        lo_ts, hi_ts = valid[0]["settle_ts"], valid[-1]["settle_ts"]
        hold_cnt = sum(1 for r in rows if r["result"] == "hold"
                       and lo_ts <= r["settle_ts"] <= hi_ts)
    else:
        hold_cnt = 0
    total_closed = n + hold_cnt

    tp = float(cfg["take_profit_offset"])
    sl = float(cfg["stop_loss_offset"])
    cost = float(cfg.get("cost_per_unit", 0.0))
    be = break_even(tp, sl, cost)
    ci_low, ci_high = wilson_interval(win_cnt, n, float(cfg["confidence_level"]))
    paused = get_state(conn, "pause_trade_alert", "false") == "true"
    qualified, reason = _qualify(n, sessions, ci_low, be, cfg, paused)

    snapshot = Snapshot(
        scope=scope, config_hash=cfg.config_hash,
        strategy_version=cfg.strategy_version,
        n=n, win_cnt=win_cnt, loss_cnt=loss_cnt, hold_cnt=hold_cnt,
        ambiguous_cnt=ambiguous_cnt, effective_sessions=sessions,
        win_rate=(win_cnt / n) if n else 0.0,
        break_even=be, ci_low=ci_low, ci_high=ci_high,
        qualified=qualified, qualified_reason=reason,
        ambiguity_rate=ambiguity_rate(ambiguous_cnt, n),
        hold_rate=hold_rate(hold_cnt, total_closed),
        cost_per_unit=cost,
    )
    log_run(conn, "snapshot", "compute",
            input_obj={"scope": scope, "config_hash": cfg.config_hash,
                       "tp": tp, "sl": sl, "cost": cost,
                       "min_qualified_samples": cfg["min_qualified_samples"],
                       "min_qualified_sessions": cfg["min_qualified_sessions"],
                       "confidence_level": cfg["confidence_level"],
                       "paused": paused},
            output_obj=snapshot.to_dict(),
            message="指标计算留痕（含全部输入参数）", ts=stamp)
    return snapshot


def freeze_daily_snapshot(conn, cfg, now: datetime | None = None,
                          created_by: str = "eod") -> dict[str, Any]:
    """每日 15:10 唯一冻结一次，写入 full / roll100 / roll200 三张快照。同日幂等。"""
    tz = cfg.get("timezone", "Asia/Shanghai")
    moment = now or datetime.now(ZoneInfo(tz))
    stamp = now_iso(tz=tz, dt=moment)
    day = to_local(moment, tz).date().isoformat()
    exists = conn.execute(
        "SELECT COUNT(*) c FROM snapshot_table WHERE freeze_date=? AND created_by=?",
        (day, created_by),
    ).fetchone()["c"]
    if exists:
        return {"skipped": True, "freeze_date": day, "reason": "already_frozen"}

    snapshots = [compute_snapshot(conn, cfg, scope, as_of=stamp) for scope in SCOPES]
    for snap in snapshots:
        d = snap.to_dict()
        conn.execute(
            "INSERT OR REPLACE INTO snapshot_table"
            "(snap_ts,freeze_date,scope,config_hash,strategy_version,n,win_cnt,loss_cnt,"
            " hold_cnt,ambiguous_cnt,effective_sessions,win_rate,break_even,ci_low,ci_high,"
            " qualified,qualified_reason,cost_per_unit,created_by) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (stamp, day, d["scope"], d["config_hash"], d["strategy_version"],
             d["n"], d["win_cnt"], d["loss_cnt"], d["hold_cnt"], d["ambiguous_cnt"],
             d["effective_sessions"], d["win_rate"], d["break_even"], d["ci_low"],
             d["ci_high"], int(d["qualified"]), d["qualified_reason"],
             d["cost_per_unit"], created_by),
        )
    conn.commit()
    log_run(conn, "snapshot", "freeze",
            input_obj={"freeze_date": day, "created_by": created_by,
                       "scopes": list(SCOPES)},
            output_obj={"snapshots": [s.to_dict() for s in snapshots]},
            message="每日 15:10 冻结；盘中不刷新", ts=stamp)
    return {"skipped": False, "freeze_date": day,
            "snapshots": [s.to_dict() for s in snapshots]}


def get_latest_frozen(conn, scope: str = "full") -> dict[str, Any] | None:
    """读取最近一次冻结快照，作为盘中推送的唯一放行依据。"""
    row = conn.execute(
        "SELECT * FROM snapshot_table WHERE scope=? "
        "ORDER BY snap_ts DESC, snap_id DESC LIMIT 1", (scope,),
    ).fetchone()
    return dict(row) if row else None
