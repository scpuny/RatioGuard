# WorkBuddy 半自动量化信号系统

A 股 ETF 配对统计套利信号系统（510300 / 159919）。**仅观测 / 记录 / 统计 / 告警**，
无自动下单、无券商客户端调用、无键鼠自动化、无 OCR。人工最终审核信号。

> 统计结果仅对已收集样本负责，是历史样本快照，不预测未来、不保证收益。

## 1. 架构：模式 A（Python 常驻服务 + WorkBuddy 低频编排）

| 层 | 职责 | 位置 |
| --- | --- | --- |
| L1 执行层 | 5s 采集、信号生成、1min 结算、15:10 冻结、漂移复核、报表、推送 | `src/qsignal/`、`qctl.py` |
| L2 编排层 | 日终复核、周报、月报；低频、人工交互 | `deploy/workbuddy-automation.md` |
| L3 口径层 | 6 个 Skill 的 SOP 说明（**非运行约束**） | `skills/S1..S6/SKILL.md` |
| L4 人工层 | 复核信号、暂停 / 恢复推送、修改配置 | `qctl.py` |
| 持久化 | SQLite WAL，跨会话状态全部落盘 | `data/qsignal.db` |

选择模式 A 的原因：WorkBuddy automation 只支持分钟级触发，且每次触发是全新
会话，无法承担秒级轮询与事件级结算；高频链路必须由本地常驻进程承担。

## 2. 目录结构

```
config.yaml                 唯一运行配置
sql/schema.sql              6 张表建表语句
qctl.py                     统一 CLI（serve / tick / freeze / report / status ...）
run_acceptance.py           M0→M5 分阶段验收入口
requirements.txt            最小依赖（PyYAML；akshare 可选）
src/qsignal/
  core/       配置 / DB / Wilson / 公式 / 硬规则 / 免责 / 审计 / 日历 / 自检 / 静态扫描
  market/     行情采集与脏数据过滤（三级降级链）
  strategy/   信号生成（quote_id 绑定、SQL 无前视）
  settle/     分钟 K 结算状态机
  stats/      统计快照与互斥窗口漂移检验
  notify/     客观推送、钉钉加签、去重、暂停恢复
  report/     Markdown 报表 / CSV / 动态大屏 JSON
  service/    常驻服务编排（Runner）
tests/         M0–M5 单元测试与端到端验收
skills/        6 个 SKILL.md（SOP，非运行约束）
templates/     钉钉模板、免责副本、大屏 schema 与示例
deploy/        systemd unit、环境变量样例、automation 建议、部署说明
tools/         静态自检扫描器与禁用模式配置
ACCEPTANCE.md  验收清单与规则对照
```

## 3. 快速开始

```bash
cd /home/yangbo/WorkBuddy/charge
PYTHON=/home/yangbo/.workbuddy/binaries/python/envs/default/bin/python

$PYTHON qctl.py selftest            # 启动自检
$PYTHON run_acceptance.py ALL       # M0→M5 全量验收
$PYTHON qctl.py serve --once        # 单轮编排（离线可跑）
$PYTHON qctl.py status              # 运行态与最近冻结快照
```

> `python3` 默认可能指向未装 PyYAML 的解释器；请使用上面的托管 venv，
> 或先 `pip install -r requirements.txt`。akshare 只在需要真实行情时才需要。

## 4. CLI 速查

| 命令 | 说明 |
| --- | --- |
| `selftest [--check-market]` | 静态扫描 / 硬规则 / Wilson 对拍 / 公式 / schema / 免责 |
| `bootstrap` | 自检 + 配置版本登记 |
| `serve [--once] [--max-ticks N]` | 常驻主循环 |
| `tick` | 单轮编排 |
| `settle` | 立即结算 open 信号 |
| `freeze` | 手动冻结快照（`created_by=manual`） |
| `drift` | 互斥窗口漂移复核 |
| `report [--out-dir DIR]` | Markdown / CSV / 大屏 JSON |
| `status` | 运行态、冻结快照、各表计数、审计违规数 |
| `audit` | 无前视 SQL 审计（违规退出码 1） |
| `pause` / `resume` | 暂停 / 恢复交易类消息推送 |

## 5. 关键口径（硬规则已代码化）

1. 硬规则、Wilson 区间、盈亏平衡公式、结算逻辑全在 Python 代码 + 单元测试；
   `SKILL.md` 只做 SOP，不作为运行约束。
2. 统计快照每日 **15:10 唯一冻结一次**；盘中只引用最近冻结快照放行信号，
   绝不刷新快照，杜绝序贯窥探偏差。
3. 漂移监控用**互斥窗口两比例检验**（W1/W2 不重叠）+ Bonferroni 校正 +
   最小效应门槛；样本不足返回 `insufficient` 而非「无漂移」。
4. 结算优先用分钟 K 线 high/low；同区间双触无法定序时保守判 `loss`
   并置 `ambiguity=1`，报表与大屏单独展示歧义率。
5. 原始行情入 `quote_table`，每条信号绑定 `quote_id`，
   `audit_no_lookahead()` 可 SQL 审计，杜绝前视偏差。
6. `config_hash` 覆盖统计口径参数；样本按版本分组，默认不跨版本合并；
   参数变更写日志，不误判为漂移。
7. 推送只用客观描述，禁止「建议交易」类话术；每条推送与报表强制带完整免责声明。
8. `tools/static_scan.py` 扫描生产代码，禁止键鼠控制 / OCR / 券商下单相关标识符。
9. 交付物：config.yaml、SQL、全部 Python 模块、单元测试、6 个 SKILL.md、
   钉钉模板、动态大屏 JSON 结构。
10. 大屏新增：歧义样本占比、hold 率、互斥窗口 z 值、配置版本标记。

`hold` 不计入胜率 / Wilson 区间 / 盈亏平衡判定，但进入 hold 率与质量指标；
含成本 `break_even = (sl + cost) / (tp + sl)`；日内不隔夜，15:00 强制 hold。

## 6. 交付与验收

- 完整验收清单与规则逐条对照见 `ACCEPTANCE.md`；
- 部署见 `deploy/README.md`；低频编排见 `deploy/workbuddy-automation.md`；
- 动态大屏字段说明见 `templates/dashboard_schema.json`。

## 7. 边界

本系统只做观测、记录、统计、告警。**没有任何自动下单、键鼠自动化、OCR 逻辑**；
统计只代表历史样本快照，不预测收益。所有信号均需人工复核行情、流动性、
ETF 申赎状态、券源与交易成本后再决定是否操作，不构成投资建议。
