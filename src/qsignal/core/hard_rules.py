"""内置风控硬规则 —— 代码化常量 + 断言 + 单元测试。

这些约束不依赖 SKILL.md 文本。Skill 只做 SOP 说明，不得作为运行约束。
任何修改本文件常量的行为都必须同步修改 tests/test_m0_core.py 并重新通过验收。
"""
from __future__ import annotations

from typing import Any

# 硬下限：配置值不得低于此，防止通过 config.yaml 下调门槛
MIN_QUALIFIED_SAMPLES_FLOOR = 60
MIN_QUALIFIED_SESSIONS_FLOOR = 10
MIN_WINDOW_SAMPLES_FLOOR = 30
MIN_EFFECT_SIZE_FLOOR = 0.0

# 每日唯一快照冻结时刻（盘中不刷新快照放行交易信号）
EOD_FREEZE_HOUR = 15
EOD_FREEZE_MINUTE = 10

# 行为约束（常量 + 断言 + 静态扫描共同保证）
NO_LOOKAHEAD = True                    # 信号生成禁止引用未来行情
HOLD_EXCLUDED_FROM_WINRATE = True     # hold 不计入胜率/置信区间
AMBIGUOUS_PATH_CONSERVATIVE = True    # 同区间双触保守判 loss
NO_ORDER_EXECUTION = True             # 无下单 / 键鼠 / OCR
DEFAULT_ALLOW_CROSS_VERSION_MERGE = False
DISCLAIMER_MANDATORY = True           # 每条推送与报表强制带完整免责

HARD_RULES: tuple[tuple[str, Any], ...] = (
    ("min_qualified_samples_floor", MIN_QUALIFIED_SAMPLES_FLOOR),
    ("min_qualified_sessions_floor", MIN_QUALIFIED_SESSIONS_FLOOR),
    ("eod_freeze", f"{EOD_FREEZE_HOUR:02d}:{EOD_FREEZE_MINUTE:02d}"),
    ("no_lookahead", NO_LOOKAHEAD),
    ("hold_excluded_from_winrate", HOLD_EXCLUDED_FROM_WINRATE),
    ("ambiguous_path_conservative", AMBIGUOUS_PATH_CONSERVATIVE),
    ("no_order_execution", NO_ORDER_EXECUTION),
    ("default_allow_cross_version_merge", DEFAULT_ALLOW_CROSS_VERSION_MERGE),
    ("disclaimer_mandatory", DISCLAIMER_MANDATORY),
)


class HardRuleViolation(RuntimeError):
    """配置项违反硬规则。"""


def assert_hard_rules(cfg: Any) -> list[str]:
    """校验配置不突破硬规则，返回警告列表（不阻断的显式例外）。

    违反下限直接抛 HardRuleViolation；显式开启跨版本合并只记录警告，
    因为该开关必须在报表显著标注，属于人工明确决策。
    """
    warnings: list[str] = []
    checks = (
        ("min_qualified_samples", MIN_QUALIFIED_SAMPLES_FLOOR, int),
        ("min_qualified_sessions", MIN_QUALIFIED_SESSIONS_FLOOR, int),
        ("min_window_samples", MIN_WINDOW_SAMPLES_FLOOR, int),
    )
    for key, floor, caster in checks:
        try:
            value = caster(cfg.get(key, floor))
        except (TypeError, ValueError) as exc:
            raise HardRuleViolation(f"{key} 非法: {exc}") from exc
        if value < floor:
            raise HardRuleViolation(
                f"{key}={value} 低于硬下限 {floor}，不得通过配置下调"
            )
    if float(cfg.get("min_effect_size", 0)) < MIN_EFFECT_SIZE_FLOOR:
        raise HardRuleViolation("min_effect_size 不得为负")
    if cfg.get("allow_cross_version_merge", False) is True:
        warnings.append(
            "allow_cross_version_merge=True：跨 config_hash 样本已合并，报表必须显著标注"
        )
    if str(cfg.get("eod_freeze", "")) != f"{EOD_FREEZE_HOUR:02d}:{EOD_FREEZE_MINUTE:02d}":
        raise HardRuleViolation("eod_freeze 固定为 15:10，不得修改")
    if int(cfg.get("drift_comparisons", 1)) < 1:
        raise HardRuleViolation("drift_comparisons 必须 >= 1")
    return warnings
