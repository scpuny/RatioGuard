#!/usr/bin/env python3
"""按阶段运行自测验收：python run_acceptance.py M0 | M1 | ... | M5 | ALL"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

STAGE_MODULES = {
    "M0": ["test_m0_core"],
    "M1": ["test_m1_collect"],
    "M2": ["test_m2_settle"],
    "M3": ["test_m3_notify"],
    "M4": ["test_m4_drift"],
    "M5": ["test_m5_e2e"],
}
ALL_STAGES = list(STAGE_MODULES)


def main() -> int:
    stage = (sys.argv[1] if len(sys.argv) > 1 else "ALL").upper()
    if stage in ("ALL", "-A"):
        stages = ALL_STAGES
    elif stage in STAGE_MODULES:
        stages = [stage]
    else:
        print(f"未知阶段 {stage}，可选: {ALL_STAGES + ['ALL']}")
        return 2

    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for name in stages:
        for module in STAGE_MODULES[name]:
            suite.addTests(loader.loadTestsFromName(module))
    print(f"===== 验收阶段: {', '.join(stages)} | 用例数: {suite.countTestCases()} =====")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print(f"===== 结果: {'PASS' if result.wasSuccessful() else 'FAIL'} "
          f"(run={result.testsRun}, failures={len(result.failures)}, "
          f"errors={len(result.errors)}) =====")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
