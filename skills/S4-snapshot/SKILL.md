---
name: S4-snapshot
description: 统计快照 SOP。说明每日 15:10 唯一冻结、Wilson 区间、含成本盈亏平衡胜率、hold 剔除与 config_hash 分组。仅作人工 SOP，不参与运行约束。
---

# S4 · 统计快照计算（SOP）

> **约束声明**：本文件是**人工 SOP 说明**，不是运行约束。Wilson 区间、
> `break_even` 公式、样本门槛、15:10 冻结硬时刻均以
> `src/qsignal/core/wilson.py`、`core/formulas.py`、`core/hard_rules.py`、
> `src/qsignal/stats/snapshot.py` 及 `tests/test_m0_core.py` /
> `tests/test_m2_settle.py` 为准。

## 1. 对应实现

| 项目 | 位置 |
| --- | --- |
| Wilson 95% 区间 | `core/wilson.py::wilson_interval` |
| 盈亏平衡公式 | `core/formulas.py::break_even` |
| 硬规则常量 / 下限 | `core/hard_rules.py` |
| 快照计算与冻结 | `stats/snapshot.py` |
| 运行入口 | `qctl.py freeze`（手动）/ 服务 15:10 自动 |
| 开关 | `config.yaml::enable_snapshot` |
| 验收 | M0 + M2 |

## 2. 判定顺序（代码化，`snapshot._qualify`）

```
1. hold 样本剔除，仅 win/loss 进入统计
2. n = win_cnt + loss_cnt
3. n < 硬下限 60                -> qualified=False, below_hard_floor
   n < min_qualified_samples    -> qualified=False, insufficient_sample
   覆盖交易日 < min_qualified_sessions -> qualified=False, insufficient_sessions
4. break_even = (stop_loss_offset + cost_per_unit)
                / (take_profit_offset + stop_loss_offset)
5. ci_low, ci_high = wilson_interval(win_cnt, n, confidence_level)
6. pause_trade_alert=true       -> qualified=False, drift_paused
7. ci_low > break_even          -> qualified=True
   否则                         -> qualified=False, ci_low_below_break_even
```

> 成本 `cost_per_unit` **只出现在分子**：亏损时的损失被成本放大。
> 硬下限（`MIN_QUALIFIED_SAMPLES_FLOOR=60`、`MIN_QUALIFIED_SESSIONS_FLOOR=10`、
> `eod_freeze=15:10`）不允许通过 `config.yaml` 下调。

## 3. 每日 15:10 唯一冻结（关键）

- `freeze_daily_snapshot()` 以 `freeze_date + created_by` 幂等，
  同日重复调用返回 `already_frozen`；
- 一次冻结写入 `full` / `roll100` / `roll200` 三张快照；
- **盘中绝不刷新快照**。结算事件、新信号事件都只写 `run_log`；
- 盘中信号放行只调用 `get_latest_frozen()` 读取最近冻结快照，
  从根上消除逐笔刷新 CI 的序贯窥探偏差；
- `qctl.py freeze` 为人工补冻，`created_by=manual`，与 `eod` 分开计数。

## 4. 样本分组

- 每次计算按 `config_hash` 过滤，默认 `allow_cross_version_merge=false`；
- 参数变更由 `core/config_guard.py` 写 `run_log` 与
  `drift_log(status='config_change', drift_alert=0)`，
  **不误判为分布漂移**；
- 显式开启跨版本合并必须人工决策，且报表显著标注。

## 5. 描述性统计定位

所有输出是**已收集样本的历史快照**，不预测未来、不保证收益；
Wilson 区间描述的是二项比例估计的不确定性，不是未来收益概率。

---

⚠️ 统计结果仅基于当前已收集样本，是历史样本快照，不代表未来行情概率分布，市场结构变化会直接失效；半自动模式，请人工复核行情、流动性、ETF 申赎状态、券源与交易成本后再决定是否操作，不构成投资建议。本信号未纳入统计样本，历史统计不构成对其结果的推断。
