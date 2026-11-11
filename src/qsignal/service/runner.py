"""L1 常驻服务编排：模式 A（Python 常驻服务；L2 低频编排为设计备选、未启用）。

运行时不依赖 WorkBuddy：本进程是唯一数据生产者，无 automation / Skill 参与。

职责边界
--------
- 本进程只做 观测 / 记录 / 统计 / 告警；不做委托、成交、键鼠或 OCR 动作。
- 所有硬规则、Wilson 区间、盈亏平衡公式、结算逻辑都在 core/ 与 settle/ 的代码里，
  本文件只负责调度与串联，不复制、不重新定义任何判定规则。
- 统计快照每日 15:10 唯一冻结一次。盘中信号放行**只引用**最近一次冻结快照
  （get_latest_frozen），盘中绝不调用 compute_snapshot / freeze_daily_snapshot
  去刷新放行依据，从而杜绝逐笔刷新 CI 带来的序贯窥探偏差。

循环节奏
--------
- poll_interval   (默认 5s)  -> 采集 + 信号生成
- settle_interval (默认 60s) -> 结算扫描（幂等；结算事件不刷新快照）
- 每日 15:10 之后首个 tick  -> 日终例程：冻结 + 漂移复核 + 报表 + 大屏 + CSV + 审计

跨会话状态全部落在 SQLite 的 system_state，进程重启不丢失、不重复执行。
"""
from __future__ import annotations

import signal
import time as _time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from ..core.audit import audit_no_lookahead, log_run, now_iso
from ..core.calendar import TradeCalendar, in_trade_window, is_eod, to_local
from ..core.config_guard import check_config_change
from ..core.db import get_state, set_state
from ..market.collector import collect_once
from ..notify.notifier import dispatch
from ..report.renderer import export_csv, render_markdown_report, write_dashboard
from ..settle.settler import settle_open_signals
from ..stats.drift import run_drift_monitor
from ..stats.snapshot import freeze_daily_snapshot, get_latest_frozen
from ..strategy.generator import generate_signal

EOD_STATE_KEY = "eod_done_date"


class Runner:
    """可注入时钟 / 行情源 / 交易日历的常驻服务，便于端到端离线验收。"""

    def __init__(self, conn, cfg, provider=None, clock: Callable[[], datetime] | None = None,
                 calendar=None, report_dir: str | Path | None = None):
        self.conn = conn
        self.cfg = cfg
        self.tz = cfg.get("timezone", "Asia/Shanghai")
        self._provider = provider
        self.clock = clock or (lambda: datetime.now(ZoneInfo(self.tz)))
        self.calendar = calendar or TradeCalendar(cfg)
        self.report_dir = (Path(report_dir) if report_dir
                           else cfg.resolve(cfg.get("report_dir", "reports")))
        self._last_settle: datetime | None = None
        self._stop = False

    # ------------------------------------------------------------------ 基础设施

    def _provider_or_default(self):
        """延迟构造真实行情源：离线自检 / 单测不应因 akshare 未安装而失败。"""
        if self._provider is None:
            from ..market.collector import AkShareProvider
            self._provider = AkShareProvider()
        return self._provider

    def stop(self) -> None:
        """由 SIGINT / SIGTERM 触发，优雅退出主循环。"""
        self._stop = True

    def install_signal_handlers(self) -> None:
        def _handler(signum, _frame):
            log_run(self.conn, "service", "shutdown_signal",
                    input_obj={"signal": int(signum)},
                    message="收到退出信号，完成当前 tick 后退出",
                    ts=now_iso(tz=self.tz))
            self.stop()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):
                # 非主线程（例如单测）无法注册，忽略即可
                pass

    @staticmethod
    def bootstrap(conn, cfg, check_market: bool = False) -> dict[str, Any]:
        """启动自检 + 配置版本守卫。自检不通过时调用方必须拒绝启动。"""
        from ..core.selftest import run_selftest

        selftest = run_selftest(cfg, check_market=check_market)
        guard = check_config_change(conn, cfg)
        log_run(conn, "service", "bootstrap",
                input_obj={"check_market": check_market,
                           "config_hash": cfg.config_hash,
                           "strategy_version": cfg.strategy_version},
                output_obj={"selftest_ok": selftest["ok"],
                            "warnings": selftest["warnings"],
                            "config_changed": guard["changed"]},
                level="INFO" if selftest["ok"] else "ERROR",
                message="启动自检；静态扫描 / 硬规则 / 公式 / schema / 免责声明")
        return {"ok": bool(selftest["ok"]), "selftest": selftest,
                "config_guard": guard}

    # ------------------------------------------------------------------ 单轮编排

    def _should_settle(self, moment: datetime) -> bool:
        interval = max(1, int(self.cfg.get("settle_interval", 60)))
        if self._last_settle is None:
            return True
        return (moment - self._last_settle).total_seconds() >= interval

    def _on_new_signal(self, sig: dict[str, Any], moment: datetime) -> dict[str, Any]:
        """信号入库后的放行判定。

        唯一依据 = 最近一次 15:10 冻结快照；盘中不计算、不写入任何新快照。
        """
        frozen = get_latest_frozen(self.conn)
        if frozen is None:
            kind, reason, snap = "signal_insufficient", "no_frozen_snapshot", {}
        elif int(frozen["qualified"]) == 1:
            kind, reason, snap = "signal_qualified", "frozen_snapshot_qualified", dict(frozen)
        else:
            kind, reason, snap = ("signal_insufficient",
                                 str(frozen["qualified_reason"]), dict(frozen))
        ctx = {
            "signal_ts": sig["signal_ts"],
            "signal_type": sig["signal_type"],
            "ratio": sig["ratio"],
            "qualified_reason": reason,
            "snapshot": snap,
        }
        out = dispatch(self.conn, self.cfg, kind, ctx, now=moment)
        log_run(self.conn, "service", "signal_gate",
                input_obj={"signal_id": sig["signal_id"],
                           "frozen_snap_ts": frozen["snap_ts"] if frozen else None,
                           "frozen_qualified": bool(frozen and int(frozen["qualified"]) == 1),
                           "kind": kind, "reason": reason},
                output_obj={"sent": out.get("sent"), "result": out.get("reason")},
                message="盘中放行引用冻结快照；本轮不刷新快照",
                ts=now_iso(tz=self.tz, dt=moment))
        return {"kind": kind, "reason": reason, "dispatch": out,
                "frozen_qualified": bool(frozen and int(frozen["qualified"]) == 1)}

    def tick(self, now: datetime | None = None) -> dict[str, Any]:
        """执行一轮编排。全部动作幂等，可安全重放。"""
        moment = now or self.clock()
        stamp = now_iso(tz=self.tz, dt=moment)
        out: dict[str, Any] = {
            "ts": stamp, "gate": None, "collect": None, "signal": None,
            "settle": None, "eod": None, "notify": None,
        }

        allowed, gate_reason = self.calendar.should_run(moment)
        out["gate"] = gate_reason
        if not allowed:
            return out

        # 1) 采集 + 信号生成：仅交易时段内，且受模块开关控制
        if in_trade_window(moment, self.cfg):
            if self.cfg.get("enable_collect", True):
                collected = collect_once(self.conn, self.cfg,
                                         self._provider_or_default(), now=moment)
                out["collect"] = collected
                if (collected.get("ok") and collected.get("quote_id")
                        and self.cfg.get("enable_signal", True)):
                    sig = generate_signal(self.conn, self.cfg,
                                          int(collected["quote_id"]),
                                          signal_ts=stamp, now=moment)
                    out["signal"] = sig
                    if sig.get("ok"):
                        out["notify"] = self._on_new_signal(sig, moment)
            else:
                out["collect"] = {"skipped": True, "reason": "collect_disabled"}
        else:
            out["collect"] = {"skipped": True, "reason": "outside_trade_window"}

        # 2) 结算：全天可跑，按 settle_interval 节流；结算不刷新快照
        if self.cfg.get("enable_settle", True) and self._should_settle(moment):
            out["settle"] = settle_open_signals(self.conn, self.cfg, now=moment)
            self._last_settle = moment

        # 3) 日终例程：15:10 之后首个 tick，按 freeze_date 落盘幂等
        if is_eod(moment, self.cfg):
            out["eod"] = self.eod_routine(moment)
        return out

    # ------------------------------------------------------------------ 日终例程

    def write_reports(self, as_of: str | None = None) -> dict[str, Any]:
        """生成 Markdown 报表 + CSV 备份 + 动态大屏 JSON。

        报表里的 live 快照仅用于展示；放行依据始终且仅为冻结快照。
        """
        stamp = as_of or now_iso(tz=self.tz)
        day = stamp[:10]
        self.report_dir.mkdir(parents=True, exist_ok=True)
        md_path = self.report_dir / f"report_{day}.md"
        csv_path = self.report_dir / f"signals_{day}.csv"
        dashboard_path = self.report_dir / "dashboard.json"

        text = render_markdown_report(self.conn, self.cfg, as_of=stamp)
        md_path.write_text(text, encoding="utf-8")
        rows = export_csv(self.conn, csv_path)
        payload = write_dashboard(self.conn, self.cfg, dashboard_path, as_of=stamp)
        return {
            "markdown": str(md_path), "markdown_bytes": md_path.stat().st_size,
            "csv": str(csv_path), "csv_rows": rows,
            "dashboard": str(dashboard_path),
            "config_version_tag": payload["config_version_tag"],
            "quality": payload["quality"], "drift": payload["drift"],
        }

    def eod_routine(self, now: datetime | None = None) -> dict[str, Any]:
        """每日 15:10 唯一执行一次：冻结快照 + 漂移复核 + 报表 + 大屏 + 审计。"""
        moment = now or self.clock()
        stamp = now_iso(tz=self.tz, dt=moment)
        day = to_local(moment, self.tz).date().isoformat()
        if get_state(self.conn, EOD_STATE_KEY) == day:
            return {"skipped": True, "reason": "already_done", "freeze_date": day}

        settle = (settle_open_signals(self.conn, self.cfg, now=moment)
                  if self.cfg.get("enable_settle", True)
                  else {"skipped": True, "reason": "settle_disabled"})
        freeze = (freeze_daily_snapshot(self.conn, self.cfg, now=moment,
                                        created_by="eod")
                  if self.cfg.get("enable_snapshot", True)
                  else {"skipped": True, "reason": "snapshot_disabled"})
        drift = run_drift_monitor(self.conn, self.cfg, as_of=stamp)
        reports = self.write_reports(stamp)
        violations = audit_no_lookahead(self.conn)
        if violations:
            log_run(self.conn, "service", "lookahead_audit",
                    input_obj={"count": len(violations)},
                    output_obj={"sample": violations[:5]}, level="WARN",
                    message="发现违反无前视约束的信号，需人工核查", ts=stamp)

        set_state(self.conn, EOD_STATE_KEY, day, operator="system",
                  reason="eod_15:10", ts=stamp)
        log_run(self.conn, "service", "eod_routine",
                input_obj={"freeze_date": day, "config_hash": self.cfg.config_hash},
                output_obj={"freeze_skipped": freeze.get("skipped"),
                            "drift_status": drift.get("status"),
                            "drift_alert": drift.get("drift_alert"),
                            "lookahead_violations": len(violations)},
                message="每日 15:10 冻结一次；盘中未刷新快照", ts=stamp)
        return {
            "skipped": False, "freeze_date": day, "settle": settle,
            "freeze": freeze, "drift": drift, "reports": reports,
            "lookahead_violations": len(violations),
        }

    # ------------------------------------------------------------------ 主循环

    def run(self, max_ticks: int | None = None,
            sleep_fn: Callable[[float], None] | None = None,
            install_signals: bool = True) -> dict[str, Any]:
        """常驻主循环。max_ticks / sleep_fn 可注入，用于离线验收与 --once。"""
        if install_signals:
            self.install_signal_handlers()
        sleep = sleep_fn or _time.sleep
        interval = max(1, int(self.cfg.get("poll_interval", 5)))
        ticks = 0
        while not self._stop and (max_ticks is None or ticks < max_ticks):
            self.tick()
            ticks += 1
            if not self._stop and (max_ticks is None or ticks < max_ticks):
                sleep(interval)
        log_run(self.conn, "service", "run_exit",
                input_obj={"ticks": ticks, "stopped": self._stop},
                message="常驻服务退出", ts=now_iso(tz=self.tz))
        return {"ticks": ticks, "stopped": self._stop,
                "ts": now_iso(tz=self.tz)}
