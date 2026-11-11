---
name: S2-signal-generate
description: 信号生成 SOP。说明配对比值偏离阈值如何生成信号、绑定 quote_id、防止重复与杜绝前视偏差。仅作人工 SOP，不参与运行约束。
---

# S2 · 信号生成（SOP）

> **约束声明**：本文件是**人工 SOP 说明**，不是运行约束。阈值方向、
> `quote_id` 绑定、无前视 SQL、`config_hash` 分组均以
> `src/qsignal/strategy/generator.py` 及 `tests/test_m1_collect.py` 为准。

## 1. 对应实现

| 项目 | 位置 |
| --- | --- |
| 信号生成 | `src/qsignal/strategy/generator.py::generate_signal` |
| 止盈止损价位 | `generator.signal_levels` |
| 基准比值 | `generator.estimate_base_ratio` |
| 信号表 | `sql/schema.sql::signal_table` |
| 运行入口 | `qctl.py tick` / `qctl.py serve` |
| 开关 | `config.yaml::enable_signal` |
| 验收 | `python run_acceptance.py M1` |

## 2. 生成逻辑

```
ratio = price_a / price_b
base  = fixed(config.base_ratio) | rolling_median(近 lookback 日有效行情中位数)

ratio > base + spread_threshold -> spread_long （预期比值回落）
ratio < base - spread_threshold -> spread_short（预期比值回升）
否则                              -> 不生成信号
```

止盈止损按 `tp_sl_basis = entry_ratio`（当前唯一支持的口径）：

| 类型 | 止盈 tp | 止损 sl |
| --- | --- | --- |
| `spread_long` | `ratio - take_profit_offset` | `ratio + stop_loss_offset` |
| `spread_short` | `ratio + take_profit_offset` | `ratio - stop_loss_offset` |

> `tp_sl_basis = base_ratio` 与当前 `break_even` 公式不兼容（盈亏距离不再等于
> 配置偏移），`core/config.py` 会直接抛 `ConfigError` 拒绝加载，需重推公式后启用。

## 3. 硬规则（代码化）

- **无前视**：SQL 层强制 `quote_id` 指向 `is_valid=1 AND quote_ts <= signal_ts` 的行情行，
  查不到即抛 `LookaheadViolation`；未来行情在 SQL 中根本不可见；
- **绑定行情**：每条信号写入 `quote_id`，供 `audit_no_lookahead()` 审计；
- **防重复**：同 `signal_type` + 同 `config_hash` 存在 `status='open'` 时不重复生成；
- **配置版本**：写入 `config_hash` 与 `strategy_version`，样本默认不跨版本合并；
- **不立即推送**：新信号状态为 `open`、`note=pending_settlement`，等待 S3 结算闭环。

## 4. 运行态说明（重要）

信号入库后由 `service/runner.py::_on_new_signal` 判定是否推送，**唯一依据是
最近一次 15:10 冻结快照**；盘中不计算、不写入任何新快照。目的：避免逐笔刷新
Wilson 置信区间带来的序贯窥探偏差。详见 S4 / S5。

## 5. 人工操作

- 查看未结算信号：`python qctl.py settle`；
- 审计无前视：`python qctl.py audit`（返回码 1 表示存在违规，需人工核查）；
- 新增策略时**只替换信号生成条件**，共用信号库、结算、统计与推送框架，
  统计逻辑固定不变。

## 6. 边界

本模块只生成观测记录，不含委托、成交、键鼠自动化或 OCR 逻辑。

---

⚠️ 统计结果仅基于当前已收集样本，是历史样本快照，不代表未来行情概率分布，市场结构变化会直接失效；半自动模式，请人工复核行情、流动性、ETF 申赎状态、券源与交易成本后再决定是否操作，不构成投资建议。本信号未纳入统计样本，历史统计不构成对其结果的推断。
