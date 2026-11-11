"""免责声明与话术合规检查（单一事实来源）。

推送模板与报表必须引用本文件的 DISCLAIMER 全文，并调用 assert_compliant() 校验。
"""
from __future__ import annotations

DISCLAIMER = (
    "⚠️ 统计结果仅基于当前已收集样本，是历史样本快照，不代表未来行情概率分布，"
    "市场结构变化会直接失效；半自动模式，请人工复核行情、流动性、ETF 申赎状态、"
    "券源与交易成本后再决定是否操作，不构成投资建议。本信号未纳入统计样本，"
    "历史统计不构成对其结果的推断。"
)

# 禁止出现的主观/诱导性话术。系统只做观测、记录、统计、告警。
PERSUASIVE_PHRASES = (
    "建议交易", "建议买入", "建议卖出", "建议开仓", "建议加仓",
    "推荐买入", "推荐卖出", "推荐交易", "可入场", "可建仓",
    "稳赚", "必涨", "必赚", "保证收益", "立即下单", "果断买入",
    "交易参考信号", "买入信号", "卖出信号",
)


class ComplianceError(RuntimeError):
    """推送 / 报表文案不合规。"""


def compliance_errors(text: str) -> list[str]:
    """返回不合规原因列表；空列表表示通过。"""
    errors: list[str] = []
    if DISCLAIMER not in text:
        errors.append("missing_full_disclaimer")
    for phrase in PERSUASIVE_PHRASES:
        if phrase in text:
            errors.append(f"persuasive_phrase:{phrase}")
    return errors


def assert_compliant(text: str) -> None:
    """强制校验：完整免责文案 + 无诱导性话术。"""
    errors = compliance_errors(text)
    if errors:
        raise ComplianceError("文案合规校验失败: " + ", ".join(errors))
