"""M5 验收：编排接入与端到端全链路（离线，不依赖 akshare / 网络）。

覆盖：启动自检 -> 配置版本守卫 -> 采集 -> 信号(quote_id 绑定 / 无前视)
-> 结算(不刷新快照) -> 15:10 冻结 -> 漂移复核 -> 推送放行 -> 报表 / 大屏
-> 静态扫描 -> CLI 冒烟。
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from base import TempDBMixin, insert_quote, insert_signal, make_cfg  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qsignal.core.audit import audit_no_lookahead  # noqa: E402
from qsignal.core.db import get_state  # noqa: E402
from qsignal.core.disclaimer import DISCLAIMER, compliance_errors  # noqa: E402
from qsignal.core.static_scan import scan_tree  # noqa: E402
from qsignal.market.collector import FakeProvider  # noqa: E402
from qsignal.notify.notifier import (render_signal_message,  # noqa: E402
                                     resume_trade_alert)
from qsignal.report.renderer import render_markdown_report  # noqa: E402
from qsignal.service.runner import EOD_STATE_KEY, Runner  # noqa: E402
from qsignal.stats.drift import run_drift_monitor  # noqa: E402
from qsignal.stats.snapshot import get_latest_frozen  # noqa: E402

DAY1 = "2026-11-10"
DAY2 = "2026-11-11"


def moment(day: str, hhmm: str) -> datetime:
    return datetime.fromisoformat(f"{day}T{hhmm}:00+08:00")


def iso(day: str, hhmm: str, seconds: int = 0) -> str:
    return (moment(day, hhmm) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def fake_quote(ts: str, price_a: float = 4.04, price_b: float = 4.0) -> dict:
    return {"quote_ts": ts, "code_a": "510300", "price_a": price_a,
            "code_b": "159919", "price_b": price_b, "source": "fake"}


class OpenCalendar:
    """离线验收用交易日历：不触网、不受真实星期影响。"""

    def __init__(self, allow: bool = True, reason: str = "ok"):
        self.allow, self.reason = allow, reason

    def should_run(self, _dt) -> tuple[bool, str]:
        return (True, "ok") if self.allow else (False, self.reason)


class M5E2ECase(TempDBMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.out_dir = Path(self._tmp.name) / "reports"

    def cfg(self, **overrides):
        defaults = {
            "base_ratio_mode": "fixed",
            "notify_channel": "none",
            "report_dir": str(self.out_dir),
        }
        defaults.update(overrides)
        return make_cfg(**defaults)

    def runner(self, cfg, provider=None, calendar=None, clock=None):
        return Runner(self.conn, cfg, provider=provider,
                      calendar=calendar or OpenCalendar(),
                      clock=clock or (lambda: moment(DAY1, "10:00")),
                      report_dir=self.out_dir)

    def seed_auditable(self, cfg, wins: int, losses: int, holds: int = 0,
                       spread_days: int = 20, day: str = DAY1,
                       ambiguous: int = 0) -> None:
        """造已闭环样本，且每条都绑定有效原始行情（供无前视 SQL 审计）。"""
        start = moment(day, "09:30")
        total = wins + losses + holds
        outcomes = ["win"] * wins + ["loss"] * losses + ["hold"] * holds
        span = max(spread_days, 1)
        amb = ambiguous
        for idx, outcome in enumerate(outcomes):
            offset = min(span - 1, (idx * span) // total) if total else 0
            ts = (start + timedelta(days=offset, seconds=idx)).isoformat(timespec="seconds")
            qid = insert_quote(self.conn, ts, source="seed")
            flag = 0
            if outcome == "loss" and amb > 0:
                flag, amb = 1, amb - 1
            insert_signal(self.conn, ts, result=outcome,
                          config_hash=cfg.config_hash, quote_id=qid,
                          settle_ts=ts, ambiguity=flag)

    def count(self, table: str) -> int:
        return self.conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]

    def logs(self, action: str) -> list:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM run_log WHERE action=? ORDER BY id", (action,)
        ).fetchall()]

    # ------------------------------------------------------------ 启动与配置

    def test_bootstrap_registers_config_hash(self):
        cfg = self.cfg()
        boot = Runner.bootstrap(self.conn, cfg)
        self.assertTrue(boot["ok"], msg=str(boot["selftest"]["checks"]))
        self.assertTrue(boot["config_guard"]["initial"])
        self.assertEqual(get_state(self.conn, "active_config_hash"), cfg.config_hash)
        again = Runner.bootstrap(self.conn, cfg)
        self.assertFalse(again["config_guard"]["changed"])
        self.assertEqual(self.logs("bootstrap")[0]["level"], "INFO")

    def test_config_change_is_logged_not_treated_as_drift(self):
        old = self.cfg()
        Runner.bootstrap(self.conn, old)
        new = self.cfg(stop_loss_offset=0.005)
        result = Runner.bootstrap(self.conn, new)
        self.assertTrue(result["config_guard"]["changed"])
        self.assertNotEqual(old.config_hash, new.config_hash)
        rows = self.conn.execute(
            "SELECT * FROM drift_log WHERE status='config_change'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["drift_alert"], 0)
        self.assertIn("非分布漂移", rows[0]["note"])
        self.assertTrue(self.logs("config_change"))

    # ------------------------------------------------------------ 会话门禁

    def test_tick_outside_session_makes_no_request(self):
        cfg = self.cfg()
        provider = FakeProvider([fake_quote(iso(DAY1, "10:00"))])
        runner = self.runner(cfg, provider=provider,
                             calendar=OpenCalendar(False, "non_trading_day"))
        out = runner.tick(now=moment(DAY1, "10:00"))
        self.assertEqual(out["gate"], "non_trading_day")
        self.assertEqual(provider.calls, 0)
        self.assertEqual(self.count("quote_table"), 0)
        self.assertEqual(self.count("signal_table"), 0)

    # ------------------------------------------------------------ 采集与信号

    def test_tick_collect_generate_and_no_lookahead(self):
        cfg = self.cfg()
        ts = iso(DAY1, "10:00")
        out = self.runner(cfg, provider=FakeProvider([fake_quote(ts)])).tick(
            now=moment(DAY1, "10:00"))
        self.assertTrue(out["collect"]["ok"], msg=str(out["collect"]))
        self.assertTrue(out["signal"]["ok"], msg=str(out["signal"]))
        row = self.conn.execute("SELECT * FROM signal_table").fetchone()
        self.assertEqual(row["signal_type"], "spread_long")
        self.assertEqual(row["status"], "open")
        self.assertIsNotNone(row["quote_id"])
        self.assertEqual(row["config_hash"], cfg.config_hash)
        self.assertEqual(audit_no_lookahead(self.conn), [])
        # 无冻结快照 -> 仅观测；盘中绝不刷新快照
        self.assertEqual(out["notify"]["kind"], "signal_insufficient")
        self.assertEqual(out["notify"]["reason"], "no_frozen_snapshot")
        self.assertEqual(self.count("snapshot_table"), 0)
        self.assertIn(DISCLAIMER, out["notify"]["dispatch"]["text"])
        gate = self.logs("signal_gate")
        self.assertEqual(len(gate), 1)
        self.assertIn("不刷新快照", gate[0]["message"])

    # ------------------------------------------------------------ 结算与快照

    def test_intraday_settle_never_refreshes_snapshot(self):
        cfg = self.cfg(enable_collect=False)
        sig_ts = iso(DAY1, "10:00")
        qid = insert_quote(self.conn, sig_ts, source="entry")
        insert_signal(self.conn, sig_ts, status="open", result="win",
                      config_hash=cfg.config_hash, quote_id=qid)
        # 后续分钟行情：比值 1.006 <= tp(1.008) -> win
        for sec in (60, 120, 180):
            insert_quote(self.conn, iso(DAY1, "10:00", sec),
                         price_a=4.024, price_b=4.0, source="after")
        out = self.runner(cfg).tick(now=moment(DAY1, "10:05"))
        self.assertEqual(out["settle"]["closed"], 1)
        self.assertEqual(out["settle"]["details"][0]["result"], "win")
        self.assertEqual(self.count("snapshot_table"), 0,
                         "盘中结算事件不得写入 snapshot_table")
        self.assertEqual(self.conn.execute(
            "SELECT result FROM signal_table").fetchone()["result"], "win")

    def test_eod_freeze_once_idempotent_and_reports(self):
        cfg = self.cfg(enable_collect=False)
        self.seed_auditable(cfg, 140, 10, spread_days=20, ambiguous=3)
        runner = self.runner(cfg)
        m = moment(DAY1, "15:10")
        out = runner.tick(now=m)
        eod = out["eod"]
        self.assertFalse(eod["skipped"])
        self.assertEqual(eod["freeze_date"], DAY1)
        self.assertEqual(self.count("snapshot_table"), 3)
        self.assertEqual(get_state(self.conn, EOD_STATE_KEY), DAY1)
        frozen = get_latest_frozen(self.conn)
        self.assertEqual(frozen["qualified"], 1)
        self.assertEqual(frozen["n"], 150)
        self.assertGreater(frozen["ci_low"], frozen["break_even"])

        # 同日重放：日终例程幂等，快照不重复写入
        again = runner.tick(now=m)
        self.assertTrue(again["eod"]["skipped"])
        self.assertEqual(self.count("snapshot_table"), 3)

        # 报表 / CSV / 大屏
        files = eod["reports"]
        for key in ("markdown", "csv", "dashboard"):
            self.assertTrue(Path(files[key]).exists(), key)
        self.assertGreater(files["csv_rows"], 0)
        payload = json.loads(Path(files["dashboard"]).read_text(encoding="utf-8"))
        self.assertEqual(payload["config_version_tag"],
                         f"{cfg.strategy_version}@{cfg.config_hash}")
        self.assertIn("ambiguity_rate", payload["quality"])
        self.assertIn("hold_rate", payload["quality"])
        self.assertIn("z_stat", payload["drift"])
        self.assertEqual(payload["disclaimer"], DISCLAIMER)
        self.assertTrue(self.logs("freeze"))
        self.assertTrue(self.logs("eod_routine"))

    # ------------------------------------------------------------ 冻结快照放行

    def test_signal_gate_uses_only_frozen_snapshot(self):
        frozen_cfg = self.cfg(enable_collect=False)
        self.seed_auditable(frozen_cfg, 140, 10, spread_days=20)
        self.runner(frozen_cfg).tick(now=moment(DAY1, "15:10"))
        self.assertEqual(self.count("snapshot_table"), 3)

        # 盘中配置开启采集；config_hash 与冻结时一致（模块开关不参与 hash）
        cfg = self.cfg()
        self.assertEqual(cfg.config_hash, frozen_cfg.config_hash)
        ts = iso(DAY2, "10:00")
        runner = self.runner(cfg, provider=FakeProvider([fake_quote(ts)]))
        out = runner.tick(now=moment(DAY2, "10:00"))
        self.assertTrue(out["signal"]["ok"], msg=str(out["signal"]))
        self.assertEqual(out["notify"]["kind"], "signal_qualified")
        self.assertTrue(out["notify"]["frozen_qualified"])
        self.assertEqual(self.count("snapshot_table"), 3,
                         "盘中不得刷新放行依据")
        gate = json.loads(self.logs("signal_gate")[-1]["input_json"])
        self.assertTrue(gate["frozen_qualified"])
        text = out["notify"]["dispatch"]["text"]
        self.assertIn(DISCLAIMER, text)
        self.assertEqual(compliance_errors(text), [])

    def test_insufficient_frozen_snapshot_gate(self):
        frozen_cfg = self.cfg(enable_collect=False)
        self.seed_auditable(frozen_cfg, 3, 2, spread_days=1)
        self.runner(frozen_cfg).tick(now=moment(DAY1, "15:10"))
        frozen = get_latest_frozen(self.conn)
        self.assertEqual(frozen["qualified"], 0)

        cfg = self.cfg()
        runner = self.runner(cfg, provider=FakeProvider([fake_quote(iso(DAY2, "10:00"))]))
        out = runner.tick(now=moment(DAY2, "10:00"))
        self.assertEqual(out["notify"]["kind"], "signal_insufficient")
        self.assertEqual(out["notify"]["reason"], frozen["qualified_reason"])
        self.assertEqual(self.count("snapshot_table"), 3)

    # ------------------------------------------------------------ 漂移与暂停

    def test_drift_alert_pauses_then_resume(self):
        cfg = self.cfg()
        samples = ([{"result": "win"}] * 95 + [{"result": "loss"}] * 5
                   + [{"result": "win"}] * 20 + [{"result": "loss"}] * 80)
        result = run_drift_monitor(self.conn, cfg,
                                   as_of=iso(DAY1, "15:10"), samples=samples)
        self.assertTrue(result["drift_alert"], msg=str(result))
        self.assertEqual(get_state(self.conn, "pause_trade_alert"), "true")

        runner = self.runner(cfg, provider=FakeProvider([fake_quote(iso(DAY1, "10:00"))]))
        out = runner.tick(now=moment(DAY1, "10:00"))
        self.assertEqual(out["notify"]["dispatch"]["reason"], "paused")
        self.assertFalse(out["notify"]["dispatch"]["sent"])

        resume_trade_alert(self.conn, reason="人工复核后恢复")
        self.assertEqual(get_state(self.conn, "pause_trade_alert"), "false")
        self.assertTrue(self.logs("resume"))

    # ------------------------------------------------------------ 主循环 / 自检

    def test_run_loop_respects_max_ticks(self):
        cfg = self.cfg(enable_collect=False)
        runner = self.runner(cfg, calendar=OpenCalendar(False, "non_trading_day"))
        slept = []
        out = runner.run(max_ticks=3, sleep_fn=lambda s: slept.append(s),
                         install_signals=False)
        self.assertEqual(out["ticks"], 3)
        self.assertEqual(len(slept), 2)
        self.assertTrue(self.logs("run_exit"))

    def test_static_scan_clean_including_m5_code(self):
        scan = scan_tree()
        self.assertTrue(scan["ok"], msg=str(scan["violations"][:5]))
        self.assertGreaterEqual(scan["scanned"], 20)
        joined = " ".join(scan["files"])
        self.assertIn("qctl.py", joined)
        self.assertIn("runner.py", joined)

    def test_eod_audit_flags_lookahead_violation(self):
        cfg = self.cfg(enable_collect=False)
        future_qid = insert_quote(self.conn, iso(DAY2, "10:00"), source="future")
        insert_signal(self.conn, iso(DAY1, "10:00"), result="win",
                      config_hash=cfg.config_hash, quote_id=future_qid,
                      settle_ts=iso(DAY1, "10:00"))
        out = self.runner(cfg).eod_routine(now=moment(DAY1, "15:10"))
        self.assertEqual(out["lookahead_violations"], 1)
        warn = [r for r in self.logs("lookahead_audit") if r["level"] == "WARN"]
        self.assertEqual(len(warn), 1)

    # ------------------------------------------------------------ 合规与 CLI

    def test_all_outputs_carry_disclaimer(self):
        cfg = self.cfg()
        self.seed_auditable(cfg, 140, 10, spread_days=20)
        report = render_markdown_report(self.conn, cfg, as_of=iso(DAY1, "15:10"))
        self.assertIn(DISCLAIMER, report)
        self.assertEqual(compliance_errors(report), [])
        payload = self.runner(cfg).write_reports(iso(DAY1, "15:10"))
        self.assertEqual(
            json.loads(Path(payload["dashboard"]).read_text(encoding="utf-8"))["disclaimer"],
            DISCLAIMER)
        for kind in ("signal_qualified", "signal_insufficient", "drift_alert"):
            text = render_signal_message(kind, {}, cfg)
            self.assertIn(DISCLAIMER, text)
            self.assertEqual(compliance_errors(text), [])
        template = (ROOT / "templates" / "dingtalk.md").read_text(encoding="utf-8")
        self.assertIn(DISCLAIMER, template)

    def test_cli_commands_smoke(self):
        spec = importlib.util.spec_from_file_location("qctl_cli", ROOT / "qctl.py")
        cli = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(cli)
        common = ["--config", str(ROOT / "config.yaml"), "--db", str(self.db_path)]
        commands = [
            ["selftest"], ["bootstrap"], ["status"], ["audit"],
            ["pause", "--reason", "M5 验收"], ["resume"],
            ["settle"], ["freeze"], ["drift"],
            ["report", "--out-dir", str(self.out_dir)],
        ]
        for cmd in commands:
            with self.subTest(cmd=cmd[0]):
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = cli.main(common + cmd)
                self.assertEqual(rc, 0, msg=f"{cmd} 返回 {rc}")
        self.assertTrue((self.out_dir / "dashboard.json").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
