---
name: S1-market-collect
description: 行情采集与脏数据过滤 SOP。说明 L1 常驻服务如何拉取 ETF 双标的快照、过滤脏数据并落 quote_table。仅作人工 SOP，不参与运行约束。
---

# S1 · 行情采集与脏数据过滤（SOP）

> **约束声明**：本文件是**人工 SOP 说明**，不是运行约束。所有硬规则、脏数据阈值、
> 交易时段判断、降级链均以 `src/qsignal/market/collector.py` 的代码及
> `tests/test_m1_collect.py` 单元测试为准。修改本文件不会改变任何运行行为。

## 1. 对应实现

| 项目 | 位置 |
| --- | --- |
| 采集与过滤 | `src/qsignal/market/collector.py` |
| 交易时段 / 日历 | `src/qsignal/core/calendar.py` |
| 原始行情表 | `sql/schema.sql::quote_table` |
| 运行入口 | `qctl.py serve`（常驻）/ `qctl.py tick`（单轮） |
| 开关 | `config.yaml::enable_collect` |
| 验收 | `python run_acceptance.py M1` |

## 2. 数据源降级链

`ak.fund_etf_spot_em()` 存在返回条数截断风险，故内置三级降级：

1. `primary`：`ak.fund_etf_spot_em()` 东财 ETF 全表快照；
2. `eastmoney_single`：push2 单只报价，按 secid 精确查询，不受分页截断影响；
3. `ths`：`ak.fund_etf_spot_ths()` 同花顺实时行情。

全部失败 → 本轮跳过并写 `run_log(level=WARN, action=fetch_failed)`，
**不生成信号**，绝不中断主循环。

## 3. 硬规则（代码化，见 collector.validate_quote）

- 非交易时段直接返回 `outside_trade_window`，**不发起网络请求**；
- 每一轮采集（含被丢弃的脏数据）都落 `quote_table`，`is_valid` / `drop_reason` 留痕；
- 丢弃原因枚举：`invalid_price_a/b`、`invalid_ratio`、`bad_timestamp`、
  `stale_quote`、`future_quote`、`pair_skew`、`pair_skew_unparsable`、
  `price_jump_a/b`、`iopv_deviation_a/b`；
- 行情新鲜度 `max_quote_age_sec`、两标的报价对齐 `max_pair_skew_sec`、
  相对上一轮价格突变 `max_jump_pct` 均取自 `config.yaml`；
- `ratio = price_a / price_b` 由价格推导，不接受外部传入，避免口径漂移。

## 4. 无前视偏差的起点

`quote_table` 是 SQL 审计的基础：每条信号必须绑定一行
`quote_ts <= signal_ts AND is_valid = 1` 的行情。审计入口：

```bash
python qctl.py audit
```

对应函数 `core.audit.audit_no_lookahead()`，返回空列表表示合规。

## 5. 人工操作

- 检查采集是否健康：`python qctl.py status`（看 `counts.quote_table` 与 `run_log`）；
- 检查行情源连通性：`python qctl.py selftest --check-market`（warn-only，离线不阻断）；
- 修改采集参数后需重跑 M1 验收；参数若属于 `STAT_KEYS`，会生成新的
  `config_hash`，样本自动分段（见 S4）。

## 6. 边界

本模块只做行情观测与记录，不含委托、成交、键鼠自动化或 OCR 逻辑。

---

⚠️ 统计结果仅基于当前已收集样本，是历史样本快照，不代表未来行情概率分布，市场结构变化会直接失效；半自动模式，请人工复核行情、流动性、ETF 申赎状态、券源与交易成本后再决定是否操作，不构成投资建议。本信号未纳入统计样本，历史统计不构成对其结果的推断。
