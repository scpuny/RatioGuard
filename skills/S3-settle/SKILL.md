---
name: S3-settle
description: 自动结算与信号闭环 SOP。说明分钟 K 线高低判定、双触保守 loss、收盘 hold 与结算不刷新快照。仅作人工 SOP，不参与运行约束。
---

# S3 · 自动结算与信号闭环（SOP）

> **约束声明**：本文件是**人工 SOP 说明**，不是运行约束。所有结算判定、
> 保守分支、`ambiguity` 标记与幂等逻辑均以 `src/qsignal/settle/settler.py`
> 及 `tests/test_m2_settle.py` 为准。

## 1. 对应实现

| 项目 | 位置 |
| --- | --- |
| 结算状态机 | `src/qsignal/settle/settler.py::settle_open_signals` |
| 分钟 K 聚合 | `settler.build_bars` |
| 单根 K 线判定 | `settler.evaluate` |
| 运行入口 | `qctl.py settle`（立即）/ `qctl.py serve`（每 `settle_interval` 秒） |
| 开关 | `config.yaml::enable_settle` |
| 验收 | `python run_acceptance.py M2` |

## 2. 结算优先级（代码化）

把信号之后的 5s 原始行情按分钟聚合为 `ratio_high / ratio_low / ratio_close`，
按时间升序逐根判定：

| 类型 | 止盈触及 | 止损触及 |
| --- | --- | --- |
| `spread_long` | `ratio_low <= tp` | `ratio_high >= sl` |
| `spread_short` | `ratio_high >= tp` | `ratio_low <= sl` |

判定顺序：

1. **同一根分钟 K 线同时触盈触损** → 路径先后不可观测，
   **保守判 `loss`** 并置 `ambiguity = 1`，`note = ambiguous_path_both_hit`；
2. 仅触盈 → `win`；
3. 仅触损 → `loss`；
4. 当日 15:00 收盘仍未触达 → `hold`（中性，**不计入胜率 / Wilson 区间 /
   盈亏平衡判定**，但进入 hold 率与报表质量指标）；
5. 日内不隔夜，历史日期的 open 信号在下一轮结算时强制 `eod_close`。

> 歧义样本占比 `ambiguity_rate` 在 Markdown 报表与动态大屏 `quality` 中单独展示，
> 告警线 `config.yaml::ambiguity_warn_ratio`。

## 3. 硬规则（代码化）

- 结算幂等：`UPDATE ... WHERE status='open'`，重复扫描不会二次闭环；
- **结算完成不刷新统计快照**：`_apply_settlement` 只写日志，
  `snapshot_table` 仅由 S4 在 15:10 冻结时写入；
- 全天可运行，交易时段外仅做收盘判定；
- 每次判定写入 `run_log`，附带 tp/sl/输入参数，可复现校验。

## 4. 人工操作

```bash
python qctl.py settle      # 立即扫描并闭环 open 信号
python qctl.py status      # 查看运行态
```

若发现 `ambiguity_rate` 持续高于告警线，应人工复核分钟行情密度
（5s 采样仍不足以定序），而不是调整结算口径。

## 5. 边界

本模块只做账面结算，不含委托、成交、键鼠自动化或 OCR 逻辑。

---

⚠️ 统计结果仅基于当前已收集样本，是历史样本快照，不代表未来行情概率分布，市场结构变化会直接失效；半自动模式，请人工复核行情、流动性、ETF 申赎状态、券源与交易成本后再决定是否操作，不构成投资建议。本信号未纳入统计样本，历史统计不构成对其结果的推断。
