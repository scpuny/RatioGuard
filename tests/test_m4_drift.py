"""M4 验收：互斥窗口漂移检验、最小效应门槛、配置版本隔离、全库静态扫描。

验收标准（方案 M4）：
  - 样本不足时返回 insufficient（而非「无漂移」），修复子集包含导致的监控失效
  - W1 / W2 完全互斥且等长，z 值来自两比例检验，alpha 经 Bonferroni 校正
  - z 显著但效应量低于 min_effect_size 时不告警
  - 告警触发 pause_trade_alert 并写 drift_pause 日志
  - config_hash 变更写 run_log / drift_log(status=config_change, drift_alert=0)，
    样本按新 hash 分组，且不误判为漂移
  - 全库静态扫描零违规
"""
from __future__ import annotations

import unittest

from base import ROOT, TempDBMixin, make_cfg, seed_samples

from qsignal.core.config_guard import check_config_change
from qsignal.core.db import get_state
from qsignal.core.static_scan import scan_tree
from qsignal.core.wilson import z_critical
from qsignal.stats.drift import compute_drift, run_drift_monitor
from qsignal.stats.snapshot import compute_snapshot


def block(wins: int, losses: int) -> list[dict]:
    return [{"result": "win"}] * wins + [{"result": "loss"}] * losses


class TestMutualWindow(TempDBMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.cfg = make_cfg()

    def test_insufficient_when_windows_too_small(self):
        # 原方案在 N=80 时「滚动窗口 == 全量」，差值恒为 0，漂移静默失效
        result = compute_drift(self.conn, self.cfg, samples=block(50, 30))
        self.assertEqual(result["status"], "insufficient")
        self.assertFalse(result["drift_alert"])
        self.assertEqual(result["n2"], 0)
        self.assertIn("样本不足", result["note"])

    def test_windows_are_disjoint_and_alert_fires(self):
        w2 = block(60, 40)     # p2 = 0.60
        w1 = block(90, 10)     # p1 = 0.90
        result = compute_drift(self.conn, self.cfg, samples=w2 + w1)
        self.assertEqual((result["n1"], result["n2"]), (100, 100))
        self.assertAlmostEqual(result["p1"], 0.90)
        self.assertAlmostEqual(result["p2"], 0.60)
        self.assertEqual((result["k1"], result["k2"]), (90, 60))
        self.assertGreater(abs(result["z_stat"]), result["z_critical"])
        self.assertAlmostEqual(result["effect_size"], 0.30)
        self.assertTrue(result["drift_alert"])
        self.assertEqual(result["status"], "alert")

    def test_bonferroni_corrected_critical_value(self):
        expected = z_critical(1.0 - 0.05 / 2)
        self.assertAlmostEqual(expected, 2.2414, places=3)
        result = compute_drift(self.conn, self.cfg,
                               samples=block(60, 40) + block(90, 10))
        self.assertAlmostEqual(result["z_critical"], expected, places=9)

    def test_min_effect_size_blocks_alert_even_if_z_significant(self):
        # 大样本下 p1=0.75 / p2=0.72 -> z≈3.4 显著，但效应量 0.03 < 0.10
        w2 = block(3600, 1400)
        w1 = block(3750, 1250)
        result = compute_drift(self.conn, self.cfg, window=5000,
                               samples=w2 + w1)
        self.assertGreater(abs(result["z_stat"]), result["z_critical"])
        self.assertLess(result["effect_size"], result["min_effect_size"])
        self.assertFalse(result["drift_alert"])
        self.assertEqual(result["status"], "ok")
        self.assertIn("效应量低于", result["note"])

    def test_alert_pauses_and_logs(self):
        result = run_drift_monitor(self.conn, self.cfg,
                                   samples=block(60, 40) + block(90, 10))
        self.assertTrue(result["drift_alert"])
        self.assertEqual(get_state(self.conn, "pause_trade_alert"), "true")
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) c FROM run_log WHERE action='drift_pause'"
        ).fetchone()["c"], 1)

    def test_drift_log_recorded_for_dashboard(self):
        compute_drift(self.conn, self.cfg,
                      samples=block(60, 40) + block(90, 10))
        row = self.conn.execute(
            "SELECT status,z_stat,n1,n2,drift_alert FROM drift_log "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(row["status"], "alert")
        self.assertEqual((row["n1"], row["n2"]), (100, 100))
        self.assertIsNotNone(row["z_stat"])


class TestConfigVersionIsolation(TempDBMixin, unittest.TestCase):
    def test_config_change_is_logged_and_not_misread_as_drift(self):
        cfg1 = make_cfg()
        seed_samples(self.conn, 30, 10, config_hash=cfg1.config_hash,
                     spread_days=1)
        first = check_config_change(self.conn, cfg1)
        self.assertFalse(first["changed"])
        self.assertTrue(first["initial"])

        cfg2 = make_cfg(spread_threshold=0.005)
        self.assertNotEqual(cfg1.config_hash, cfg2.config_hash)
        changed = check_config_change(self.conn, cfg2)
        self.assertTrue(changed["changed"])
        self.assertEqual(get_state(self.conn, "active_config_hash"),
                         cfg2.config_hash)

        # 变更事件：日志留痕，且明确不是漂移
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) c FROM run_log WHERE action='config_change'"
        ).fetchone()["c"], 1)
        rows = self.conn.execute(
            "SELECT status,drift_alert FROM drift_log"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "config_change")
        self.assertEqual(rows[0]["drift_alert"], 0)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) c FROM drift_log WHERE status='config_change' "
            "AND drift_alert=1"
        ).fetchone()["c"], 0)

        # 新配置版本下样本分组为空，不会把旧样本算进来
        snap = compute_snapshot(self.conn, cfg2, "full")
        self.assertEqual(snap.n, 0)
        # 新组样本不足时漂移判定为 insufficient，不误告警
        drift = compute_drift(self.conn, cfg2, samples=block(30, 10))
        self.assertEqual(drift["status"], "insufficient")
        self.assertFalse(drift["drift_alert"])

    def test_runtime_only_change_does_not_split_samples(self):
        cfg1 = make_cfg()
        check_config_change(self.conn, cfg1)
        cfg2 = make_cfg(notify_dedup_sec=999)
        self.assertEqual(cfg2.config_hash, cfg1.config_hash)
        self.assertFalse(check_config_change(self.conn, cfg2)["changed"])


class TestStaticScanWholeRepo(unittest.TestCase):
    def test_production_code_has_no_forbidden_identifiers(self):
        result = scan_tree(ROOT)
        self.assertTrue(result["ok"], f"违规: {result['violations']}")
        self.assertGreaterEqual(result["scanned"], 15)


if __name__ == "__main__":
    unittest.main()
