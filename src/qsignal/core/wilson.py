"""威尔逊二项分布置信区间（无连续性修正）。

标准库 statistics.NormalDist 提供精确的逆正态 CDF，无需 scipy。
口径：仅作描述性统计；样本非 i.i.d. 时区间偏窄，报表须披露此局限。
"""
from __future__ import annotations

from statistics import NormalDist


def z_critical(confidence: float = 0.95) -> float:
    """双侧 (1-alpha) 对应的 z 值，如 0.95 -> 1.959963984540054。"""
    if not 0.0 < confidence < 1.0:
        raise ValueError("confidence 必须在 (0, 1) 内")
    return NormalDist().inv_cdf(1.0 - (1.0 - confidence) / 2.0)


def wilson_interval(k: int, n: int, confidence: float = 0.95) -> tuple[float, float]:
    """返回 (ci_low, ci_high)。n=0 时返回 (0.0, 0.0)。

    参数
    ----
    k : 成功（win）计数
    n : 有效样本数 win + loss（hold 不参与）
    """
    if n < 0 or k < 0 or k > n:
        raise ValueError(f"非法计数 k={k}, n={n}")
    if n == 0:
        return (0.0, 0.0)
    z = z_critical(confidence)
    p = k / n
    z2 = z * z
    den = 1.0 + z2 / n
    center = (p + z2 / (2.0 * n)) / den
    half = (z / den) * ((p * (1.0 - p) / n + z2 / (4.0 * n * n)) ** 0.5)
    return (max(0.0, center - half), min(1.0, center + half))
