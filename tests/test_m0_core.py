"""M0 验收：核心骨架与自检。

验收标准（方案 M0）：
  - Wilson 区间与统计参考值对拍通过，边界与非法输入处理正确
  - 盈亏平衡公式含成本、两比例 z 检验、质量指标正确
  - 硬规则不可通过 config.yaml 下调（N 下限、15:10 冻结时刻）
  - config_hash 仅随统计口径变化，运行态参数不切分样本
  - 静态扫描能检出键鼠/OCR/下单代码，且生产代码零违规
  - 免责文案完整且话术合规
  - 数据库 6 张表齐全，启动自检全绿
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from base import ROOT, TempDBMixin, make_cfg

from qsignal.core import hard_rules
from qsignal.core.config import compute_config_hash
from qsignal.core.db import EXPECTED_TABLES, table_names
from qsignal.core.disclaimer import (
    DISCLAIMER, ComplianceError, assert_compliant, compliance_errors,
)
from qsignal.core.formulas import (
    ambiguity_rate, break_even, hold_rate, two_proportion_z,
)
from qsignal.core.selftest import run_selftest
from qsignal.core.static_scan import load_patterns, scan_file, scan_tree
from qsignal.core.wilson import wilson_interval, z_critical


class TestWilson(unittest.TestCase):
    def test_known_values(self):
        self.assertAlmostEqual(z_critical(0.95), 1.959964, places=5)
        lo, hi = wilson_interval(5, 10, 0.95)
        self.assertAlmostEqual(lo, 0.236593, places=5)
        self.assertAlmostEqual(hi, 0.763407, places=5)
        self.assertAlmostEqual(wilson_interval(0, 10, 0.95)[1], 0.27754, places=4)

    def test_bounds_and_monotonic(self):
        self.assertEqual(wilson_interval(0, 0), (0.0, 0.0))
        lo0, _ = wilson_interval(0, 50)
        lo1, _ = wilson_interval(25, 50)
        lo2, _ = wilson_interval(50, 50)
        self.assertEqual(lo0, 0.0)
        self.assertLess(lo0, lo1)
        self.assertLess(lo1, lo2)
        for k in range(0, 21):
            lo, hi = wilson_interval(k, 20)
            self.assertGreaterEqual(lo, 0.0)
            self.assertLessEqual(hi, 1.0)
            self.assertLessEqual(lo, hi)

    def test_invalid_input(self):
        with self.assertRaises(ValueError):
            wilson_interval(-1, 10)
        with self.assertRaises(ValueError):
            wilson_interval(11, 10)
        with self.assertRaises(ValueError):
            z_critical(1.5)


class TestFormulas(unittest.TestCase):
    def test_break_even_without_cost(self):
        self.assertAlmostEqual(break_even(0.002, 0.004, 0.0), 2 / 3, places=9)

    def test_break_even_with_cost(self):
        # 成本只在分子出现：(0.004 + 0.0005) / 0.006 = 0.75
        self.assertAlmostEqual(break_even(0.002, 0.004, 0.0005), 0.75, places=9)

    def test_break_even_invalid(self):
        for args in ((0, 1), (1, 0), (1, 1, -0.1)):
            with self.assertRaises(ValueError):
                break_even(*args)

    def test_two_proportion_z(self):
        result = two_proportion_z(80, 100, 50, 100)
        self.assertAlmostEqual(result["p1"], 0.8)
        self.assertAlmostEqual(result["p2"], 0.5)
        self.assertAlmostEqual(result["effect_size"], 0.3)
        self.assertGreater(result["z_stat"], 4.0)
        self.assertEqual(two_proportion_z(0, 10, 0, 10)["z_stat"], 0.0)

    def test_quality_ratios(self):
        self.assertAlmostEqual(ambiguity_rate(3, 30), 0.1)
        self.assertEqual(ambiguity_rate(3, 0), 0.0)
        self.assertAlmostEqual(hold_rate(5, 50), 0.1)
        self.assertEqual(hold_rate(5, 0), 0.0)


class TestHardRules(unittest.TestCase):
    def test_default_passes(self):
        self.assertEqual(hard_rules.assert_hard_rules(make_cfg()), [])

    def test_cannot_downgrade_sample_floor(self):
        with self.assertRaises(hard_rules.HardRuleViolation):
            hard_rules.assert_hard_rules(make_cfg(min_qualified_samples=30))

    def test_cannot_change_eod_freeze(self):
        with self.assertRaises(hard_rules.HardRuleViolation):
            hard_rules.assert_hard_rules(make_cfg(eod_freeze="14:00"))

    def test_cross_merge_is_warned_not_blocked(self):
        warnings = hard_rules.assert_hard_rules(make_cfg(allow_cross_version_merge=True))
        self.assertTrue(any("cross_version" in w for w in warnings))

    def test_constants_are_code_not_skill_text(self):
        self.assertEqual(hard_rules.EOD_FREEZE_HOUR, 15)
        self.assertEqual(hard_rules.EOD_FREEZE_MINUTE, 10)
        self.assertTrue(hard_rules.NO_LOOKAHEAD)
        self.assertTrue(hard_rules.AMBIGUOUS_PATH_CONSERVATIVE)
        self.assertFalse(hard_rules.DEFAULT_ALLOW_CROSS_VERSION_MERGE)


class TestConfigHash(unittest.TestCase):
    def test_runtime_keys_do_not_change_hash(self):
        base = make_cfg()
        for key in ("dingtalk_webhook", "db_path", "poll_interval", "notify_dedup_sec"):
            self.assertEqual(base.config_hash, make_cfg(**{key: "changed"}).config_hash,
                             f"{key} 不应切分统计样本")

    def test_stat_keys_change_hash(self):
        base = make_cfg()
        for key, value in (("spread_threshold", 0.005),
                           ("take_profit_offset", 0.003),
                           ("cost_per_unit", 0.001),
                           ("stop_loss_offset", 0.005)):
            self.assertNotEqual(base.config_hash, make_cfg(**{key: value}).config_hash,
                                f"{key} 必须切分样本")

    def test_unsupported_basis_is_rejected(self):
        from qsignal.core.config import ConfigError
        with self.assertRaises(ConfigError):
            make_cfg(tp_sl_basis="base_ratio")

    def test_hash_is_deterministic(self):
        raw = make_cfg().raw
        self.assertEqual(compute_config_hash(raw), compute_config_hash(dict(raw)))


class TestStaticScan(unittest.TestCase):
    def test_detects_forbidden_identifiers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.py"
            path.write_text(
                "import pyautogui\n"
                "from easyocr import Reader\n"
                "def submit_order():\n"
                "    pyautogui.click()\n",
                encoding="utf-8",
            )
            hits = scan_file(path, load_patterns())
            tokens = {h["token"] for h in hits}
            self.assertIn("pyautogui", tokens)
            self.assertIn("easyocr", tokens)
            self.assertIn("submit_order", tokens)

    def test_comments_are_not_false_positive(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "comment.py"
            path.write_text("# 本系统不含自动下单逻辑，不包含 OCR 模块\nvalue = 1\n",
                            encoding="utf-8")
            self.assertEqual(scan_file(path, load_patterns()), [])

    def test_production_code_is_clean(self):
        result = scan_tree(ROOT)
        self.assertTrue(result["ok"], f"生产代码存在违规: {result['violations']}")
        self.assertGreater(result["scanned"], 5)


class TestDisclaimer(unittest.TestCase):
    def test_full_disclaimer_passes(self):
        assert_compliant(DISCLAIMER)
        self.assertEqual(compliance_errors(DISCLAIMER), [])

    def test_missing_disclaimer(self):
        errors = compliance_errors("信号已记录")
        self.assertIn("missing_full_disclaimer", errors)

    def test_persuasive_phrase_rejected(self):
        with self.assertRaises(ComplianceError):
            assert_compliant("建议交易，胜率良好。" + DISCLAIMER)


class TestSchemaAndSelftest(TempDBMixin, unittest.TestCase):
    def test_all_tables_present(self):
        self.assertEqual(table_names(self.conn), set(EXPECTED_TABLES))

    def test_selftest_all_green(self):
        result = run_selftest(make_cfg(), check_market=False)
        self.assertTrue(result["ok"], f"自检未通过: {result['checks']}")
        self.assertEqual(len(result["checks"]), 6)


if __name__ == "__main__":
    unittest.main()
