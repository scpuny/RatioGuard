"""A 股交易时段与交易日历。

自动化 rrule 只能按星期表达，无法识别节假日；因此任何会话首步必须调用本模块判断交易日。
"""
from __future__ import annotations

from datetime import date, datetime, time
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

from .audit import now_iso


def to_local(dt: datetime, tz: str = "Asia/Shanghai") -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=ZoneInfo(tz))
    return dt.astimezone(ZoneInfo(tz))


def parse_hhmm(value: str) -> time:
    parts = str(value).split(":")
    return time(int(parts[0]), int(parts[1]))


def in_trade_window(dt: datetime, cfg) -> bool:
    local = to_local(dt, cfg.get("timezone", "Asia/Shanghai"))
    moment = local.time()
    windows = (
        (parse_hhmm(cfg["trade_start"]), parse_hhmm(cfg["trade_end"])),
        (parse_hhmm(cfg["trade_start2"]), parse_hhmm(cfg["trade_end2"])),
    )
    return any(start <= moment <= end for start, end in windows)


def is_eod(now: datetime, cfg) -> bool:
    """是否到达每日 15:10 冻结时刻（当天首次触发由调用方幂等控制）。"""
    local = to_local(now, cfg.get("timezone", "Asia/Shanghai"))
    freeze = parse_hhmm(cfg["eod_freeze"])
    return local.time() >= freeze


def is_trading_day(day: date, trading_days: Iterable[str] | None = None) -> bool:
    """trading_days 为 ISO 日期字符串集合；为空时降级为工作日判断。"""
    if trading_days:
        return day.isoformat() in set(trading_days)
    return day.weekday() < 5


class TradeCalendar:
    """akshare 交易日历 + 离线降级。可注入 loader 便于单测。"""

    def __init__(self, cfg, loader: Callable[[], Iterable[str]] | None = None):
        self.cfg = cfg
        self._loader = loader or self._akshare_loader
        self._days: set[str] | None = None
        self.approximate = False

    @staticmethod
    def _akshare_loader() -> Iterable[str]:
        import akshare as ak  # 延迟导入：离线自检与单测不需要
        frame = ak.tool_trade_date_hist_sina()
        return {str(x) for x in frame["trade_date"].astype(str)}

    def trading_days(self) -> set[str]:
        if self._days is None:
            try:
                self._days = set(self._loader())
            except Exception:
                self._days = set()
                self.approximate = True
        return self._days

    def is_trading_day(self, dt: datetime) -> bool:
        local = to_local(dt, self.cfg.get("timezone", "Asia/Shanghai"))
        if self.approximate or not self.trading_days():
            return local.date().weekday() < 5
        return local.date().isoformat() in self.trading_days()

    def should_run(self, dt: datetime) -> tuple[bool, str]:
        """会话首步统一入口。返回 (是否继续, 原因)。"""
        if not self.is_trading_day(dt):
            return False, "non_trading_day"
        if not in_trade_window(dt, self.cfg) and not is_eod(dt, self.cfg):
            return False, "outside_trade_window"
        return True, "ok"


def session_gate(cfg, dt: datetime | None = None,
                 trading_days: Iterable[str] | None = None) -> tuple[bool, str]:
    """轻量交易日门禁（供 qctl.py status 复用；可选 L2 会话亦可调用）。

    与 WorkBuddy 无运行时依赖，不依赖 akshare 也能工作。
    """
    moment = dt or datetime.now(ZoneInfo(cfg.get("timezone", "Asia/Shanghai")))
    local = to_local(moment, cfg.get("timezone", "Asia/Shanghai"))
    if not is_trading_day(local.date(), trading_days):
        return False, "non_trading_day"
    return True, "ok"
