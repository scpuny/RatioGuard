# WorkBuddy 低频编排（L2）· 设计备选（可选 / 未启用 / 不推荐）

> **状态说明 · 务必先读**
>
> - 本文是**方案阶段的设计备选，不是已交付的运行能力**。文中 3 条 automation
>   **从未在 WorkBuddy 中创建**；`skills/S1..S6/SKILL.md` 放在普通 `skills/` 目录，
>   **不在 WorkBuddy 的 Skill 加载路径**（项目级 `.workbuddy/skills/` 或用户级
>   `~/.workbuddy/skills/`），从未被加载过，生产代码零引用。它们是给人类读者
>   看的 SOP 文档。
> - L1 常驻服务（`qctl.py serve` / `runner.py`）已完整产出日终 / 周报 / 月报所需的
>   原始结果：15:10 冻结三档快照、互斥窗口漂移复核、Markdown / CSV / 大屏 JSON、
>   `qctl.py status` / `qctl.py audit`。L2 剩下的唯一增量只是「把结构化结果
>   用 LLM 汇总成人话摘要」。
> - 每次 automation 触发 = 一次全新 Agent 会话（冷启动、token 开销），与本项目的
>   **零持续成本**约束冲突，且免费模型档位存在限流 / 429 风险。
>   **当前推荐不启用 L2**：直接看 `frontend/` 大屏（`/api/state`）、
>   `qctl.py status` / `audit` 与 `reports/`，零成本、数据同源、无模型依赖。
> - 若将来确实要启用，还需补齐：创建 3 条 automation、把 SKILL.md 移入加载路径、
>   按目标机器修正 `cwds`、注册 slash command。本文只保留配置值供参考，
>   **不代表这些能力已经存在**。
>
> **一句话结论：运行时完全不依赖 WorkBuddy，去掉这一层系统照常运行。**
> 这套设计在方案阶段的实际价值，是论证出「高频链路不能交给 automation」，
> 从而定下 Python 常驻服务架构（见 `量化信号系统_完善方案.md` §2）。

## 1. 能力边界（已核实，勿越界）

- WorkBuddy automation 的 RRULE 只支持 `HOURLY / DAILY / WEEKLY / MONTHLY / YEARLY`；
  `DAILY` 需 `BYHOUR` + `BYMINUTE`，`WEEKLY` 需 `BYDAY` + `BYHOUR` + `BYMINUTE`。
- **无秒级 / 亚分钟级调度**。5s 行情轮询、1min 结算扫描、15:10 冻结
  全部由 L1 常驻服务（`deploy/qsignal.service`）承担，automation 不参与。
- 每次 automation 触发 = 一次全新 Agent 会话（冷启动、有 token 开销），
  会话间无内存状态；状态全部落在 SQLite。
- `rrule` **不能识别 A 股节假日**。任何会话首步必须调用
  `qsignal.core.calendar.session_gate(cfg)`（或 `TradeCalendar.should_run`）
  判断交易日，非交易日直接结束会话。该函数目前由 `qctl.py status` 复用，
  并有单测覆盖。

## 2. 可选配置值（3 条均未创建，仅供将来参考）

> 以下为 `automation_update` 工具可使用的字段值；`prompt` 只描述任务本身，
> 时间与工作目录由工具的 `rrule` / `cwds` 字段提供，不写在 prompt 里。
> `cwds` 为本机仓库根目录，迁移到别的机器时按实际路径替换。

### 2.1 日终复核（每个工作日 15:15）

| 字段 | 值 |
| --- | --- |
| `scheduleType` | `recurring` |
| `rrule` | `FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=15;BYMINUTE=15` |
| `cwds` | `/Users/yangbo/WorkBuddy/RatioGuard` |
| `status` | `PAUSED`（若启用再改 `ACTIVE`） |

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
| `cwds` | `/Users/yangbo/WorkBuddy/RatioGuard` |
| `status` | `PAUSED` |

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
| `cwds` | `/Users/yangbo/WorkBuddy/RatioGuard` |
| `status` | `PAUSED` |

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

## 4. 日常复核：直接用 CLI（无需 automation）

文档早期列出的 `/report` `/status` `/audit` `/resume` `/pause` 五个斜杠指令
**从未注册、并不存在**，已移除，避免误以为有快捷指令可直接调用。
需要人工复核时直接执行对应 CLI，或看 `frontend/` 大屏：

| 目的 | 命令 / 入口 |
| --- | --- |
| 运行态与最近冻结快照 | `python qctl.py status` |
| 无前视审计 | `python qctl.py audit` |
| 生成 Markdown / CSV / 大屏 JSON 报表 | `python qctl.py report` |
| 人工复核后恢复推送 | `python qctl.py resume --reason "人工复核后恢复"` |
| 暂停交易类推送 | `python qctl.py pause --reason "<说明>"` |
| 可视化查看（零成本、推荐） | `python frontend/serve.py` → `http://127.0.0.1:8787/` |
