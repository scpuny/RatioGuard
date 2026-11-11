"""统计公式：盈亏平衡胜率、两比例 z 检验、质量指标。

所有公式集中在此，配套 tests/test_m0_core.py 与 tests/test_m4_drift.py 单元测试。
"""
from __future__ import annotations

from typing import Any


def break_even(tp_offset: float, sl_offset: float, cost: float = 0.0) -> float:
    """含成本的盈亏平衡胜率。

    推导：E = p*(tp - c) - (1-p)*(sl + c) = 0
          -> p = (sl + c) / (tp + sl)
    成本只在分子出现（tp - c + sl + c = tp + sl，分母不变）。
    """
    if tp_offset <= 0 or sl_offset <= 0:
        raise ValueError("tp_offset / sl_offset 必须为正")
    if cost < 0:
        raise ValueError("cost 不得为负")
    return (sl_offset + cost) / (tp_offset + sl_offset)


def two_proportion_z(k1: int, n1: int, k2: int, n2: int) -> dict[str, Any]:
    """互斥窗口两比例检验（合并方差）。

    W1 = 最近窗口，W2 = 紧邻其前的等长窗口，二者不重叠，
    修复「滚动窗口 vs 全量」子集包含导致差值恒为 0 的缺陷。
    """
    if n1 <= 0 or n2 <= 0:
        raise ValueError("两个窗口样本数必须为正")
    if not (0 <= k1 <= n1 and 0 <= k2 <= n2):
        raise ValueError("成功计数越界")
    p1, p2 = k1 / n1, k2 / n2
    pooled = (k1 + k2) / (n1 + n2)
    var = pooled * (1.0 - pooled) * (1.0 / n1 + 1.0 / n2)
    z = (p1 - p2) / (var ** 0.5) if var > 0 else 0.0
    return {
        "n1": n1, "k1": k1, "p1": p1,
        "n2": n2, "k2": k2, "p2": p2,
        "pooled": pooled,
        "z_stat": z,
        "effect_size": abs(p1 - p2),
    }


def ambiguity_rate(ambiguous_cnt: int, n: int) -> float:
    """路径歧义样本占比，作为胜率高估的上界提示。"""
    return 0.0 if n <= 0 else ambiguous_cnt / n


def hold_rate(hold_cnt: int, total_closed: int) -> float:
    """hold 占全部已闭环信号的比例（hold 不计入胜率，但单独监控）。"""
    return 0.0 if total_closed <= 0 else hold_cnt / total_closed
