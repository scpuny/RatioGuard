# WorkBuddy 低频编排（L2）· automation 配置建议

## 1. 能力边界（已核实，勿越界）

- WorkBuddy automation 的 RRULE 只支持 `HOURLY / DAILY / WEEKLY / MONTHLY / YEARLY`；
  `DAILY` 需 `BYHOUR` + `BYMINUTE`，`WEEKLY` 需 `BYDAY` + `BYHOUR` + `BYMINUTE`。
- **无秒级 / 亚分钟级调度**。5s 行情轮询、1min 结算扫描、15:10 冻结
  全部由 L1 常驻服务（`deploy/qsignal.service`）承担，automation 不参与。
- 每次 automation 触发 = 一次全新 Agent 会话（冷启动、有 token 开销），
  会话间无内存状态；状态全部落在 SQLite。
- `rrule` **不能识别 A 股节假日**。任何会话首步必须调用
  `qsignal.core.calendar.session_gate(cfg)`（或 `TradeCalendar.should_run`）
  判断交易日，非交易日直接结束会话。

## 2. 推荐的 3 条低频编排

> 以下为 `automation_update` 工具可直接使用的字段值；`prompt` 只描述任务本身，
> 时间与工作目录由工具的 `rrule` / `cwds` 字段提供，不写在 prompt 里。

### 2.1 日终复核（每个工作日 15:15）

| 字段 | 值 |
| --- | --- |
| `scheduleType` | `recurring` |
| `rrule` | `FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=15;BYMINUTE=15` |
| `cwds` | `/home/yangbo/WorkBuddy/charge` |
| `status` | `ACTIVE` |

`prompt`：

```text
先调用 qsignal.core.calendar.session_gate() 判断今天是否为 A 股交易日，非交易日直接结束。
确认 qsignal 常驻服务当日 15:10 日终例程已完成（读 system_state.eod_done_date），
然后执行 python qctl.py status 与 python qctl.py audit，汇总当日冻结快照的
qualified / qualified_reason、quality.ambiguity_rate、quality.hold_rate、
drift.status 与 z_stat，以及无前视审计结果。如发现 drift_alert 或审计违规，
在汇总中明确标注并提示人工复核，不要自动恢复推送。所有结论只描述历史样本统计。
```

### 2.2 每周复盘（周一 09:00）

| 字段 | 值 |
| --- | --- |
| `scheduleType` | `recurring` |
| `rrule` | `FREQ=WEEKLY;BYDAY=MO;BYHOUR=9;BYMINUTE=0` |
| `cwds` | `/home/yangbo/WorkBuddy/charge` |

`prompt`：

```text
先判断交易日。生成上一周复盘：执行 python qctl.py report，读取
reports/report_*.md 与 reports/dashboard.json，按周汇总样本量、胜率、
Wilson 95% 区间下限、含成本盈亏平衡胜率、歧义样本占比、hold 率、
互斥窗口检验 z 值与 config_version_tag。若本周发生过 config_hash 变更，
按版本分段展示，不合并统计。报表须保留完整免责声明，只做客观描述。
```

### 2.3 月度归档（每月 1 日 09:30）

| 字段 | 值 |
| --- | --- |
| `scheduleType` | `recurring` |
| `rrule` | `FREQ=MONTHLY;BYMONTHDAY=1;BYHOUR=9;BYMINUTE=30` |
| `cwds` | `/home/yangbo/WorkBuddy/charge` |

`prompt`：

```text
生成上月 Markdown 报表与 CSV 备份清单，核对 reports/ 目录下文件完整性，
输出只读归档摘要（文件路径、样本条数、覆盖交易日数）。不要删除任何历史文件。
```

## 3. 明确不做的事

- 不用 automation 做 5s 采集 / 分钟结算 / 事件级触发；
- 不在 automation 会话内刷新统计快照来放行盘中信号；
- 不自动执行 `qctl.py resume`（恢复推送必须人工复核后操作）；
- 不自动修改 `config.yaml` 的统计口径参数。

## 4. 人工指令

| 指令 | 对应 CLI |
| --- | --- |
| `/report` | `python qctl.py report` |
| `/status` | `python qctl.py status` |
| `/audit` | `python qctl.py audit` |
| `/resume` | `python qctl.py resume --reason "人工复核后恢复"` |
| `/pause` | `python qctl.py pause --reason "<说明>"` |
