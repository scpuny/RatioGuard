"""配置版本守卫：检测 config_hash 变更并留痕，不误判为分布漂移。

参数变更会产生新的 config_hash；样本自动按新 hash 分组（默认不合并），
变更事件写入 run_log 与 drift_log(status=config_change, drift_alert=0)，
漂移监控查询时排除该状态，因此不会把参数切换当作行情分布漂移。
"""
from __future__ import annotations

from typing import Any

from .audit import log_config_change, now_iso
from .db import get_state, set_state

STATE_KEY = "active_config_hash"


def check_config_change(conn, cfg, ts: str | None = None) -> dict[str, Any]:
    """服务启动 / 日终复核时调用。首次调用仅登记，不算变更。"""
    stamp = ts or now_iso()
    old = get_state(conn, STATE_KEY, "") or ""
    new = cfg.config_hash
    if not old:
        # 首次登记，不算参数变更
        set_state(conn, STATE_KEY, new, operator="system",
                  reason="initial_registration", ts=stamp)
        return {"changed": False, "old": "", "new": new, "initial": True}
    if old == new:
        return {"changed": False, "old": old, "new": new, "initial": False}
    log_config_change(conn, old or "(initial)", new,
                      {"old_config_hash": old, "new_config_hash": new}, ts=stamp)
    set_state(conn, STATE_KEY, new, operator="system",
              reason="config_hash_changed", ts=stamp)
    return {"changed": True, "old": old, "new": new, "initial": False}
