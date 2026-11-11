"""M3 验收：推送文案合规、去重与暂停、报表、大屏 JSON 新增口径。

验收标准（方案 M3）：
  - 三类推送与报表均携带完整免责声明，且不含任何诱导性话术
  - pause 时信号类消息不发送；/resume 清状态并留痕
  - 钉钉加签可复现，同类消息去重生效，未配置渠道时 dry_run 不阻断
  - Markdown 报表含三张快照、歧义率、hold 率、漂移记录
  - 大屏 JSON 含歧义样本占比 / hold 率 / 互斥窗口 z 值 / 配置版本标记
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from base import TempDBMixin, make_cfg, seed_samples

from qsignal.core.db import get_state
from qsignal.core.disclaimer import (
    DISCLAIMER, PERSUASIVE_PHRASES, assert_compliant,
)
from qsignal.notify.notifier import (
    KINDS, build_signed_url, dispatch, pause_trade_alert,
    render_signal_message, resume_trade_alert,
)
from qsignal.report.renderer import (
    build_dashboard_payload, export_csv, render_markdown_report,
    write_dashboard,
)

CTX = {
    "signal_ts": "2026-11-11T10:00:00+08:00",
    "signal_type": "spread_long",
    "ratio": 1.0100,
    "qualified_reason": "insufficient_sample",
    "window_size": 100, "z_stat": 3.12, "z_critical": 2.2414,
    "effect_size": 0.18, "min_effect_size": 0.10,
    "n1": 100, "k1": 88, "p1": 0.88, "n2": 100, "k2": 70, "p2": 0.70,
    "snapshot": {"freeze_date": "2026-11-10", "n": 150, "win_rate": 0.90,
                 "ci_low": 0.8415, "ci_high": 0.9402, "break_even": 0.75},
}


class TestMessageCompliance(unittest.TestCase):
    def test_all_kinds_are_compliant(self):
        cfg = make_cfg()
        for kind in KINDS:
            text = render_signal_message(kind, CTX, cfg)
            assert_compliant(text)
            self.assertIn(DISCLAIMER, text)
            for phrase in PERSUASIVE_PHRASES:
                self.assertNotIn(phrase, text, f"{kind} 含禁用话术: {phrase}")

    def test_qualified_uses_frozen_snapshot_wording(self):
        text = render_signal_message("signal_qualified", CTX, make_cfg())
        self.assertIn("15:10 冻结快照", text)
        self.assertIn("尚未纳入统计样本", text)

    def test_unknown_kind_rejected(self):
        with self.assertRaises(ValueError):
            render_signal_message("buy_now", CTX, make_cfg())


class TestSignature(unittest.TestCase):
    def test_signature_deterministic(self):
        first = build_signed_url("https://example.com/hook", "sec", timestamp=1000)
        second = build_signed_url("https://example.com/hook", "sec", timestamp=1000)
        self.assertEqual(first, second)
        self.assertIn("timestamp=1000", first)
        self.assertIn("sign=", first)

    def test_no_secret_returns_plain_url(self):
        self.assertEqual(build_signed_url("https://x", "", timestamp=1), "https://x")


class TestDispatch(TempDBMixin, unittest.TestCase):
    def test_dry_run_when_webhook_missing(self):
        result = dispatch(self.conn, make_cfg(), "signal_qualified", CTX)
        self.assertFalse(result["sent"])
        self.assertEqual(result["reason"], "dry_run")
        self.assertIn(DISCLAIMER, result["text"])

    def test_pause_suppresses_signal_messages_but_not_drift(self):
        pause_trade_alert(self.conn, reason="unit-test")
        blocked = dispatch(self.conn, make_cfg(), "signal_qualified", CTX)
        self.assertEqual(blocked["reason"], "paused")
        drift = dispatch(self.conn, make_cfg(), "drift_alert", CTX)
        self.assertNotEqual(drift["reason"], "paused")
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) c FROM run_log WHERE action='notify_suppressed'"
        ).fetchone()["c"], 1)

    def test_sender_called_once_with_dedup(self):
        calls = []

        def fake_sender(text, webhook, secret):
            calls.append(text)
            return True, "ok"

        cfg = make_cfg(dingtalk_webhook="https://example.com/hook",
                       notify_channel="dingtalk", enable_notify=True)
        first = dispatch(self.conn, cfg, "signal_qualified", CTX,
                         sender=fake_sender)
        self.assertTrue(first["sent"])
        self.assertEqual(len(calls), 1)
        second = dispatch(self.conn, cfg, "signal_qualified", CTX,
                          sender=fake_sender)
        self.assertEqual(second["reason"], "dedup")
        self.assertEqual(len(calls), 1)

    def test_resume_clears_state_and_logs(self):
        pause_trade_alert(self.conn, reason="unit-test")
        self.assertEqual(get_state(self.conn, "pause_trade_alert"), "true")
        resume_trade_alert(self.conn, operator="yangbo", reason="人工复核")
        self.assertEqual(get_state(self.conn, "pause_trade_alert"), "false")
        row = self.conn.execute(
            "SELECT input_json FROM run_log WHERE action='resume'"
        ).fetchone()
        self.assertIn("yangbo", row["input_json"])


class TestReport(TempDBMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.cfg = make_cfg()

    def _seed(self):
        seed_samples(self.conn, 135, 15, holds=10,
                     config_hash=self.cfg.config_hash,
                     spread_days=20, shuffle=True, ambiguous=3)

    def test_markdown_report_is_compliant_and_complete(self):
        self._seed()
        text = render_markdown_report(self.conn, self.cfg)
        assert_compliant(text)
        self.assertIn(DISCLAIMER, text)
        self.assertIn("三张快照对比", text)
        self.assertIn("歧义样本占比", text)
        self.assertIn("hold 率", text)
        self.assertIn("互斥窗口两比例检验", text)
        for scope in ("full", "roll100", "roll200"):
            self.assertIn(f"| {scope} |", text)

    def test_dashboard_payload_has_new_metrics(self):
        self._seed()
        payload = build_dashboard_payload(self.conn, self.cfg)
        self.assertEqual(payload["schema_version"], "1.0")
        self.assertEqual(payload["config_version_tag"],
                         f"{self.cfg.strategy_version}@{self.cfg.config_hash}")
        self.assertEqual(set(payload["snapshots"]),
                         {"full", "roll100", "roll200"})
        self.assertIn("ambiguity_rate", payload["quality"])
        self.assertIn("hold_rate", payload["quality"])
        self.assertGreater(payload["quality"]["ambiguity_rate"], 0)
        self.assertGreater(payload["quality"]["hold_rate"], 0)
        self.assertIn("z_stat", payload["drift"])
        self.assertEqual(payload["disclaimer"], DISCLAIMER)

    def test_dashboard_reads_mutual_window_z_stat(self):
        self.conn.execute(
            "INSERT INTO drift_log"
            "(ts,config_hash,scope,status,window_size,n1,k1,p1,n2,k2,p2,"
            " z_stat,z_critical,effect_size,min_effect_size,drift_alert,action,note) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("2026-11-11T15:10:00+08:00", self.cfg.config_hash, "roll100",
             "ok", 100, 100, 88, 0.88, 100, 70, 0.70, 3.12, 2.2414,
             0.18, 0.10, 0, "none", "unit-test"),
        )
        self.conn.commit()
        payload = build_dashboard_payload(self.conn, self.cfg)
        self.assertEqual(payload["drift"]["z_stat"], 3.12)
        self.assertEqual(payload["drift"]["n1"], 100)
        self.assertEqual(payload["drift"]["n2"], 100)

    def test_dashboard_file_and_csv_export(self):
        self._seed()
        with tempfile.TemporaryDirectory() as tmp:
            dash = Path(tmp) / "dashboard.json"
            payload = write_dashboard(self.conn, self.cfg, dash)
            self.assertEqual(json.loads(dash.read_text(encoding="utf-8"))
                             ["config_hash"], payload["config_hash"])
            csv_path = Path(tmp) / "signals.csv"
            count = export_csv(self.conn, csv_path)
            self.assertEqual(count, 160)
            self.assertTrue(csv_path.exists())


if __name__ == "__main__":
    unittest.main()
