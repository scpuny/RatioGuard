"""L1 常驻服务层（模式 A 的执行主体）。

WorkBuddy automation 只支持分钟级低频触发，无法承担 5s 行情轮询；
因此采集 / 信号 / 结算 / 日终冻结全部由本层常驻进程完成，状态落 SQLite。
L2 低频编排是设计备选，3 条 automation 从未创建、运行时不需要。

硬规则以 core/hard_rules.py 与各计算模块的代码 + 单元测试为准，
skills/ 下的 SKILL.md 只是人工 SOP 说明，不在 WorkBuddy 的 Skill 加载路径，
不参与运行约束。
"""
