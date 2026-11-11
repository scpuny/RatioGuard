"""全链路日志与状态查询：每条指标计算附带输入参数，可复现校验。"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

DEFAULT_TZ = "Asia/Shanghai"


def now_iso(tz: str = DEFAULT_TZ, dt: datetime | None = None) -> str:
    """带 +08:00 偏移的 ISO8601 时间戳（秒精度）。"""
    moment = dt or datetime.now(ZoneInfo(tz))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=ZoneInfo(tz))
    return moment.astimezone(ZoneInfo(tz)).isoformat(timespec="seconds")


def parse_iso(value: str, tz: str = DEFAULT_TZ) -> datetime:
    """解析 ISO8601；无偏移时按配置时区补齐。"""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz))
    return dt.astimezone(ZoneInfo(tz))


def _dump(obj: Any) -> str | None:
    if obj is None:
        return None
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)


def log_run(conn, module: str, action: str,
            input_obj: Any = None, output_obj: Any = None,
            level: str = "INFO", message: str = "",
            ts: str | None = None, commit: bool = True) -> int:
    """写 run_log。事务块内调用请传 commit=False。"""
    cur = conn.execute(
        "INSERT INTO run_log(ts,module,action,input_json,output_json,level,message) "
        "VALUES(?,?,?,?,?,?,?)",
        (ts or now_iso(), module, action, _dump(input_obj), _dump(output_obj), level, message),
    )
    if commit:
        conn.commit()
    return int(cur.lastrowid)


def audit_no_lookahead(conn) -> list[dict[str, Any]]:
    """SQL 审计：每条信号必须绑定一条 quote_ts <= signal_ts 的有效行情。

    返回违规记录列表；空列表表示无前视偏差。
    """
    rows = conn.execute(
        "SELECT s.id, s.signal_ts, s.quote_id, q.quote_ts, q.is_valid "
        "FROM signal_table s LEFT JOIN quote_table q ON s.quote_id = q.quote_id "
        "WHERE s.quote_id IS NULL OR q.quote_id IS NULL "
        "   OR q.quote_ts > s.signal_ts OR q.is_valid <> 1 "
        "ORDER BY s.id"
    ).fetchall()
    return [dict(r) for r in rows]


def log_config_change(conn, old_hash: str, new_hash: str,
                      changed: dict[str, Any] | None = None,
                      ts: str | None = None) -> None:
    """参数变更留痕。刻意不写 drift_alert=1：参数切换不是分布漂移。"""
    stamp = ts or now_iso()
    log_run(
        conn, module="config", action="config_change",
        input_obj={"old_config_hash": old_hash, "new_config_hash": new_hash,
                   "changed_keys": changed or {}},
        output_obj={"note": "样本按新 config_hash 分组；该事件不计入漂移判定"},
        level="WARN", message="配置参数变更，统计样本分段",
        ts=stamp, commit=False,
    )
    conn.execute(
        "INSERT INTO drift_log(ts,config_hash,scope,status,window_size,drift_alert,action,note) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (stamp, new_hash, "config", "config_change", 0, 0, "segment_samples",
         "参数变更导致样本分段，非分布漂移，不触发暂停推送"),
    )
    conn.commit()
