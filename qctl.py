#!/usr/bin/env python3
"""WorkBuddy 半自动量化信号系统 · 统一命令行入口。

定位：仅观测 / 记录 / 统计 / 告警，无任何委托成交、键鼠自动化或 OCR 动作。
所有判定规则在 src/qsignal/ 的 Python 代码中；本文件只做命令分发。

常用命令
--------
  python qctl.py selftest --check-market      启动自检（静态扫描 / 硬规则 / 公式 / schema）
  python qctl.py bootstrap                   自检 + 配置版本登记
  python qctl.py serve                        启动常驻服务（Ctrl-C 优雅退出）
  python qctl.py serve --once                 只跑一轮（离线验收 / 调试）
  python qctl.py tick                         单轮编排（采集 / 信号 / 结算 / 日终）
  python qctl.py settle                       立即扫描并闭环 open 信号
  python qctl.py freeze                       手动冻结快照（created_by=manual）
  python qctl.py drift                        立即执行一次互斥窗口漂移复核
  python qctl.py report                       生成 Markdown / CSV / 大屏 JSON
  python qctl.py status                       查看运行态与最近冻结快照
  python qctl.py audit                        无前视 SQL 审计
  python qctl.py pause --reason <说明>         暂停交易类消息推送
  python qctl.py resume                       人工复核后恢复推送
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from qsignal.core.audit import audit_no_lookahead, now_iso  # noqa: E402
from qsignal.core.calendar import session_gate  # noqa: E402
from qsignal.core.config import load_config  # noqa: E402
from qsignal.core.db import get_state, init_db  # noqa: E402
from qsignal.core.selftest import run_selftest  # noqa: E402
from qsignal.notify.notifier import pause_trade_alert, resume_trade_alert  # noqa: E402
from qsignal.service.runner import EOD_STATE_KEY, Runner  # noqa: E402
from qsignal.stats.drift import run_drift_monitor  # noqa: E402
from qsignal.stats.snapshot import freeze_daily_snapshot, get_latest_frozen  # noqa: E402


def _emit(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _open(cfg):
    return init_db(cfg.resolve(cfg["db_path"]))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qctl",
        description="WorkBuddy 半自动量化信号系统（仅观测 / 记录 / 统计 / 告警）",
    )
    parser.add_argument("--config", default=None, help="config.yaml 路径")
    parser.add_argument("--db", default=None, help="覆盖 db_path")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("selftest", help="启动自检")
    p.add_argument("--check-market", action="store_true", help="额外检查行情连通性（失败仅告警）")

    sub.add_parser("bootstrap", help="自检 + 配置版本登记")

    p = sub.add_parser("serve", help="启动常驻服务")
    p.add_argument("--once", action="store_true", help="只执行一轮 tick")
    p.add_argument("--max-ticks", type=int, default=None, help="最多执行轮数")
    p.add_argument("--check-market", action="store_true")

    sub.add_parser("tick", help="单轮编排")
    sub.add_parser("settle", help="结算 open 信号")
    sub.add_parser("freeze", help="手动冻结当日快照（created_by=manual）")
    sub.add_parser("drift", help="互斥窗口漂移复核")

    p = sub.add_parser("report", help="生成报表 / CSV / 大屏 JSON")
    p.add_argument("--out-dir", default=None)

    sub.add_parser("status", help="运行态与最近冻结快照")
    sub.add_parser("audit", help="无前视 SQL 审计")

    p = sub.add_parser("pause", help="暂停交易类消息推送")
    p.add_argument("--reason", default="人工暂停")
    p = sub.add_parser("resume", help="人工恢复推送")
    p.add_argument("--reason", default="人工复核后恢复")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    cfg = load_config(args.config)
    if args.db:
        cfg.raw["db_path"] = args.db
    conn = _open(cfg)
    try:
        return _dispatch(args, conn, cfg)
    finally:
        conn.close()


def _dispatch(args, conn, cfg) -> int:
    cmd = args.command

    if cmd == "selftest":
        result = run_selftest(cfg, check_market=args.check_market)
        _emit(result)
        return 0 if result["ok"] else 1

    if cmd == "bootstrap":
        result = Runner.bootstrap(conn, cfg, check_market=False)
        _emit(result)
        return 0 if result["ok"] else 1

    if cmd == "serve":
        boot = Runner.bootstrap(conn, cfg, check_market=args.check_market)
        if not boot["ok"]:
            _emit({"ok": False, "reason": "selftest_failed", "bootstrap": boot})
            return 1
        runner = Runner(conn, cfg)
        ticks = 1 if args.once else args.max_ticks
        result = runner.run(max_ticks=ticks)
        _emit({"ok": True, "bootstrap": boot, "run": result})
        return 0

    if cmd == "tick":
        result = Runner(conn, cfg).tick()
        _emit(result)
        return 0

    if cmd == "settle":
        from qsignal.settle.settler import settle_open_signals
        _emit(settle_open_signals(conn, cfg))
        return 0

    if cmd == "freeze":
        _emit(freeze_daily_snapshot(conn, cfg, created_by="manual"))
        return 0

    if cmd == "drift":
        _emit(run_drift_monitor(conn, cfg))
        return 0

    if cmd == "report":
        runner = Runner(conn, cfg, report_dir=args.out_dir)
        _emit({"ok": True, "files": runner.write_reports()})
        return 0

    if cmd == "status":
        frozen = get_latest_frozen(conn)
        counts = {
            name: conn.execute(f"SELECT COUNT(*) c FROM {name}").fetchone()["c"]
            for name in ("quote_table", "signal_table", "snapshot_table",
                         "drift_log", "run_log")
        }
        _emit({
            "ts": now_iso(tz=cfg.get("timezone", "Asia/Shanghai")),
            "config_hash": cfg.config_hash,
            "strategy_version": cfg.strategy_version,
            "config_version_tag": f"{cfg.strategy_version}@{cfg.config_hash}",
            "session_gate": session_gate(cfg),
            "pause_trade_alert": get_state(conn, "pause_trade_alert", "false") == "true",
            "eod_done_date": get_state(conn, EOD_STATE_KEY),
            "latest_frozen_snapshot": frozen,
            "counts": counts,
            "lookahead_violations": len(audit_no_lookahead(conn)),
        })
        return 0

    if cmd == "audit":
        violations = audit_no_lookahead(conn)
        _emit({"ok": not violations, "count": len(violations),
               "violations": violations})
        return 0 if not violations else 1

    if cmd == "pause":
        pause_trade_alert(conn, reason=args.reason, operator="human")
        _emit({"ok": True, "pause_trade_alert": True, "reason": args.reason})
        return 0

    if cmd == "resume":
        resume_trade_alert(conn, reason=args.reason, operator="human")
        _emit({"ok": True, "pause_trade_alert": False, "reason": args.reason})
        return 0

    raise ValueError(f"未实现的命令: {cmd}")


if __name__ == "__main__":
    raise SystemExit(main())
