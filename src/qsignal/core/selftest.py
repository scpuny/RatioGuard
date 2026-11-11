"""启动自检：静态扫描、硬规则、Wilson 对拍、公式、数据库、免责文案、行情连通性。

行情连通性为 warn-only（离线环境不应导致自检失败），可用 --check-market 显式开启。
"""
from __future__ import annotations

import sqlite3
from typing import Any

from . import hard_rules
from .audit import now_iso
from .db import EXPECTED_TABLES, init_db, table_names
from .disclaimer import DISCLAIMER, compliance_errors
from .formulas import break_even
from .static_scan import scan_tree
from .wilson import wilson_interval, z_critical


def _check(name: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": detail}


def run_selftest(cfg, check_market: bool = False) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    warnings: list[str] = []

    # 1. 静态扫描：禁止键鼠 / OCR / 下单代码
    scan = scan_tree()
    checks.append(_check(
        "static_scan", scan["ok"],
        f"scanned={scan['scanned']} files, violations={len(scan['violations'])}",
    ))
    if scan["violations"]:
        warnings.append(f"静态扫描命中: {scan['violations'][:3]}")

    # 2. 硬规则常量与配置下限
    try:
        warns = hard_rules.assert_hard_rules(cfg)
        checks.append(_check("hard_rules", True, f"warnings={warns}"))
        warnings.extend(warns)
    except hard_rules.HardRuleViolation as exc:
        checks.append(_check("hard_rules", False, str(exc)))

    # 3. Wilson 区间已知值对拍（标准统计参考值）
    hi = wilson_interval(0, 10, 0.95)[1]
    lo5, hi5 = wilson_interval(5, 10, 0.95)
    z = z_critical(0.95)
    checks.append(_check(
        "wilson_known_values",
        abs(hi - 0.277533) < 0.001 and abs(lo5 - 0.236593) < 0.001
        and abs(hi5 - 0.763407) < 0.001 and abs(z - 1.959964) < 1e-5,
        f"W(0,10).hi={hi:.6f}, W(5,10)=({lo5:.6f},{hi5:.6f}), z={z:.6f}",
    ))

    # 4. 盈亏平衡公式（含成本）
    be0 = break_even(0.002, 0.004, 0.0)
    be1 = break_even(0.002, 0.004, 0.0005)
    checks.append(_check(
        "break_even",
        abs(be0 - 0.6666667) < 1e-6 and abs(be1 - 0.75) < 1e-9,
        f"no_cost={be0:.6f}, with_cost={be1:.6f}",
    ))

    # 5. 数据库初始化与表结构
    try:
        conn = init_db(":memory:")
        missing = set(EXPECTED_TABLES) - table_names(conn)
        conn.close()
        checks.append(_check("db_schema", not missing, f"missing={sorted(missing)}"))
    except Exception as exc:  # pragma: no cover
        checks.append(_check("db_schema", False, str(exc)))

    # 6. 免责文案完整性（自身不应触发禁用话术）
    checks.append(_check(
        "disclaimer", bool(DISCLAIMER) and not compliance_errors(DISCLAIMER),
        f"length={len(DISCLAIMER)}, errors={compliance_errors(DISCLAIMER)}",
    ))

    # 7. 行情连通性（可选，失败仅告警）
    if check_market:
        try:
            from ..market.collector import AkShareProvider
            quote = AkShareProvider().fetch(cfg)
            checks.append(_check("market_connectivity", True,
                                 f"source={quote.source}, ratio={quote.ratio}"))
        except Exception as exc:
            checks.append(_check("market_connectivity", True,
                                 f"warn-only: {type(exc).__name__}: {exc}"))
            warnings.append(f"行情连通性不可用: {exc}")
    else:
        warnings.append("未执行行情连通性检查（--check-market 开启）")

    return {
        "ok": all(c["ok"] for c in checks),
        "ts": now_iso(),
        "checks": checks,
        "warnings": warnings,
    }
