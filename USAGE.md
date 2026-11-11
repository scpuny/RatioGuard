# 使用手册 · RatioGuard 半自动量化信号系统

> **定位**：仅观测 / 记录 / 统计 / 告警，**不自动下单**。所有信号必须人工复核后才可考虑操作。
> 统计只是已收集样本的历史快照，不预测未来、不保证收益。

本文按「安装 → 配置 → 自检 → 启动 → 日常操作 → 读懂输出 → 人工复核 → 排障」顺序编排，可逐步照做。

---

## 0. 环境要求

| 项目 | 要求 |
| --- | --- |
| Python | 3.10+（开发验证环境 3.13.12） |
| 必需依赖 | `PyYAML>=6.0`（`requirements.txt`） |
| 可选依赖 | `akshare`：仅采集真实行情时需要；离线自检与全部 100 个单测不需要 |
| 数据库 | SQLite（标准库内置，无需安装服务） |
| 网络 | 仅采集阶段访问行情源；钉钉推送可选 |

本机已验证的解释器（**推荐直接用它**，默认 `python3` 缺 PyYAML）：

```bash
PYTHON=/home/yangbo/.workbuddy/binaries/python/envs/default/bin/python
```

其他机器用普通 venv 即可：

```bash
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -r requirements.txt
# 需要真实行情时再装：python -m pip install akshare
```

---

## 1. 安装

```bash
git clone https://github.com/scpuny/RatioGuard.git
cd RatioGuard
python -m pip install -r requirements.txt
```

目录无需额外初始化，首次运行 `qctl.py` 会自动创建 `data/qsignal.db` 与 6 张表。

---

## 2. 配置 `config.yaml`

唯一运行配置。常用项：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `symbol_a` / `symbol_b` | `510300` / `159919` | 配对标的 |
| `base_ratio_mode` | `rolling_median` | `fixed` 用 `base_ratio`；`rolling_median` 取回看窗口中位数 |
| `spread_threshold` | `0.003` | 偏离阈值，超过才生成信号 |
| `take_profit_offset` / `stop_loss_offset` | `0.002` / `0.004` | 相对入场比值的止盈 / 止损偏移 |
| `cost_per_unit` | `0.0005` | 双边成本折算，进入 `break_even` 分子 |
| `min_qualified_samples` / `min_qualified_sessions` | `100` / `20` | 放行所需样本数与覆盖交易日数 |
| `roll_window_1` / `roll_window_2` | `100` / `200` | 滚动快照与互斥窗口长度 |
| `min_effect_size` | `0.10` | 漂移最小效应门槛 |
| `eod_freeze` | `15:10` | 每日唯一快照冻结时刻 |
| `poll_interval` / `settle_interval` | `5` / `60` | 采集 / 结算节奏（秒），仅常驻服务生效 |
| `enable_collect/signal/settle/snapshot` | `true` | 模块开关，可单独关闭 |
| `enable_notify` / `notify_channel` | `true` / `dingtalk` | 推送开关与渠道（`dingtalk` / `desktop` / `none`） |

**三条重要规则：**

1. **硬下限不可通过配置下调**：`eod_freeze` 固定 15:10、`min_qualified_samples >= 60`、
   `min_qualified_sessions >= 10`、`min_window_samples >= 30`。违反会在自检时抛
   `HardRuleViolation`，`serve` 拒绝启动。
2. **改统计参数会切分样本**：`STAT_KEYS` 中的键（阈值、止盈止损、成本、统计门槛、
   窗口等）参与 `config_hash`。改动后新样本按新版本分组，默认不跨版本合并，
   变更写入 `run_log` 与 `drift_log(status=config_change)`，**不会被误判为漂移**。
3. **密钥不要写进 config.yaml**：用环境变量或 `deploy/qsignal.env`：

```bash
export QSIGNAL_DINGTALK_WEBHOOK="https://oapi.dingtalk.com/robot/send?access_token=..."
export QSIGNAL_DINGTALK_SECRET="SEC..."
```

`config.yaml` 中 `dingtalk_webhook` / `dingtalk_secret` 留空即可。

---

## 3. 首次自检与验收（务必先做）

```bash
$PYTHON qctl.py selftest            # 6 项：静态扫描 / 硬规则 / Wilson / 公式 / schema / 免责
$PYTHON qctl.py bootstrap           # 自检 + 登记 config_hash
$PYTHON run_acceptance.py ALL       # M0→M5 全量验收，应输出 PASS 100/100
$PYTHON tools/static_scan.py        # 生产代码禁用标识符扫描，应输出 ok=true
```

自检不通过时 `qctl.py serve` 会直接退出（返回码 1），不会带病运行。

---

## 4. 启动

### 4.1 前台调试

```bash
$PYTHON qctl.py serve --once          # 只跑一轮（离线可跑，用于验证链路）
$PYTHON qctl.py serve --max-ticks 10  # 跑 10 轮后退出
$PYTHON qctl.py serve                 # 常驻，Ctrl-C 优雅退出
```

`--once` 在 15:10 之后执行会顺带跑一次日终例程，生成 `reports/` 下三类文件。

### 4.2 systemd 常驻（生产）

```bash
cp deploy/qsignal.env.example deploy/qsignal.env
chmod 600 deploy/qsignal.env          # 填入真实 webhook / secret
# 编辑 deploy/qsignal.service 中的 User / WorkingDirectory / ExecStart 绝对路径
sudo cp deploy/qsignal.service /etc/systemd/system/qsignal.service
sudo systemctl daemon-reload
sudo systemctl enable --now qsignal
sudo systemctl status qsignal
journalctl -u qsignal -f
```

业务日志写 `data/service.log`，全链路结构化日志在 SQLite `run_log` 表。

### 4.3 运行时序（服务自动完成，无需人工触发）

| 时刻 | 动作 |
| --- | --- |
| 09:30–11:30 / 13:00–15:00 | 每 5s 采集行情并落 `quote_table`；触发阈值则生成信号（绑定 `quote_id`） |
| 全天每 60s | 扫描 open 信号，用分钟 K 线高低判止盈止损；结算**不刷新快照** |
| 15:00 后 | 未触达信号强制 `hold`，日内不隔夜 |
| 15:10 | 冻结 `full` / `roll100` / `roll200` 三张快照（当日唯一一次，幂等） |
| 15:10 例程内 | 互斥窗口漂移复核、生成 Markdown / CSV / 大屏 JSON、无前视审计 |
| 信号入库时 | 只引用最近一次冻结快照决定是否推送；盘中绝不刷新放行依据 |

> WorkBuddy automation 只承担日终复核 / 周报 / 月报（见
> `deploy/workbuddy-automation.md`），**不参与秒级采集**。

---

## 5. 日常操作命令

| 想做什么 | 命令 |
| --- | --- |
| 看运行态、最近冻结快照、各表计数 | `qctl.py status` |
| 检查有无前视偏差 | `qctl.py audit`（违规返回码 1） |
| 立即结算未闭合信号 | `qctl.py settle` |
| 手动补冻当日快照 | `qctl.py freeze`（`created_by=manual`） |
| 立即复核漂移 | `qctl.py drift` |
| 生成报表 / CSV / 大屏 JSON | `qctl.py report [--out-dir DIR]` |
| 单轮编排（调试） | `qctl.py tick` |
| 暂停所有交易类推送 | `qctl.py pause --reason "<说明>"` |
| 人工复核后恢复推送 | `qctl.py resume --reason "人工复核后恢复"` |
| 换库 / 换配置 | `qctl.py --db /path/db --config /path/config.yaml <命令>` |

**典型一天：**

```bash
# 开盘前
$PYTHON qctl.py status && $PYTHON qctl.py selftest

# 盘中（如需人工查看，服务本身自动运行）
$PYTHON qctl.py status

# 收盘后
$PYTHON qctl.py status        # 看 eod_done_date 是否为今天
$PYTHON qctl.py audit         # 应为 ok=true, count=0
$PYTHON qctl.py report        # 生成当日复盘
```

---

## 6. 读懂输出

### 6.1 三类推送（均为客观描述，不带「建议交易」类话术）

| 标题 | 含义 | 系统动作 |
| --- | --- | --- |
| 信号已记录 · 历史统计达标 | 最近冻结快照 `qualified=True` | 已入库，等待结算闭环 |
| 信号已记录 · 置信不足，仅观测 | 无冻结快照或快照未达标 | 仅入库记录，不推送交易类消息 |
| 策略分布漂移 · 暂停交易推送 | 互斥窗口检验告警 | `pause_trade_alert=true`，之后信号只记录 |

每条推送与每份报表末尾强制附带完整免责声明，由 `assert_compliant()` 校验。

### 6.2 `qualified_reason` 判定链

`below_hard_floor`（N<60）→ `insufficient_sample`（N<100）→ `insufficient_sessions`（<20 日）
→ `drift_paused`（漂移暂停中）→ `ci_low_below_break_even` → `qualified`。

**新系统长时间显示「置信不足」是正常表现**：需要 ≥100 笔有效样本且覆盖 ≥20 个交易日，
Wilson 95% 下限才可能超过含成本盈亏平衡胜率（当前配置 0.75）。这是硬规则，不是故障。

### 6.3 报表与大屏

| 文件 | 内容 |
| --- | --- |
| `reports/report_YYYY-MM-DD.md` | 信号清单、三张快照对比表、漂移记录、免责声明 |
| `reports/signals_YYYY-MM-DD.csv` | `signal_table` 全量离线备份 |
| `reports/dashboard.json` | 动态大屏指标 |

大屏四项新口径：

```json
{
  "config_version_tag": "pair_ratio_v1@<config_hash>",
  "quality": { "ambiguity_rate": 0.0, "hold_rate": 0.0 },
  "drift":   { "z_stat": null, "z_critical": 2.2414, "status": "insufficient" },
  "disclaimer": "<完整免责声明>"
}
```

- `quality.ambiguity_rate`：同分钟双触、保守判 loss 的样本占比（路径不可定序）；
- `quality.hold_rate`：收盘 hold 占全部闭环样本比例；
- `drift.z_stat`：互斥窗口两比例检验 z 值（Bonferroni 校正临界值见 `z_critical`）；
- `config_version_tag`：策略版本 + `config_hash`，标识统计口径。

字段结构详见 `templates/dashboard_schema.json`。

---

## 7. 人工复核（半自动模式的核心）

系统只给历史统计与告警，**最终决策在人**。对每条信号至少复核：

1. 当前实时行情与流动性，是否与信号时刻明显背离；
2. 两只在手 ETF 的申赎状态、折溢价与券源；
3. 实际交易成本是否仍接近 `cost_per_unit`（成本直接进入盈亏平衡胜率）；
4. `quality.ambiguity_rate` / `hold_rate` 是否超过告警线；
5. 是否处于漂移暂停状态（`qctl.py status` 的 `pause_trade_alert`）。

漂移告警后**不要自动恢复**，必须人工复核再执行：

```bash
$PYTHON qctl.py resume --reason "人工复核后恢复"
```

恢复操作会写入 `run_log` 留痕。

---

## 8. 可选：WorkBuddy 低频编排

`deploy/workbuddy-automation.md` 给出 3 条可直接创建的 automation（日终复核 15:15、
周报周一 09:00、月报每月 1 日 09:30）及其 RRULE 与 prompt。注意：

- automation 只支持分钟级，**不能**承担 5s 采集；
- 每次触发是全新会话，状态全部依赖 SQLite；
- RRULE 不识别 A 股节假日，会话首步必须调用 `session_gate()` 判断交易日。

---

## 9. 故障排查

| 现象 | 原因 / 处理 |
| --- | --- |
| `ModuleNotFoundError: No module named 'yaml'` | 用错解释器。改用托管 venv：`/home/yangbo/.workbuddy/binaries/python/envs/default/bin/python`，或 `pip install -r requirements.txt` |
| `serve` 启动即退出，退出码 1 | 自检未通过。先跑 `qctl.py selftest` 看哪项失败（常见：硬下限被下调） |
| 钉钉没收到消息 | `status` 看 `pause_trade_alert`；未配 webhook 时 `reason=dry_run` 属正常，文案仍生成并校验 |
| `qualified` 长期为 False | 样本不足或漂移暂停，属硬规则；不要为了放行而下调门槛 |
| 推送停止 | 漂移告警触发了暂停，人工复核后 `qctl.py resume` |
| `audit` 返回 1 | 存在 `quote_id` 缺失或 `quote_ts > signal_ts` 的信号，需人工核查数据来源 |
| `database is locked` | 已启用 WAL + `busy_timeout=30000`；仍出现时检查是否有多个 `serve` 进程同时运行 |
| 行情全部跳过 | 非交易时段不发请求（正常）；交易时段检查 `run_log` 中 `fetch_failed` 与网络 |

结构化日志检索示例：

```sql
SELECT ts, module, action, level, message FROM run_log
WHERE action IN ('signal_gate','eod_routine','drift_pause','lookahead_audit')
ORDER BY id DESC LIMIT 20;
```

---

## 10. 边界重申

本系统只做观测、记录、统计、告警，**没有任何自动下单、键鼠自动化、OCR 逻辑**；
统计只代表历史样本快照，不预测收益。所有输出不构成投资建议。

更多设计口径见 `README.md`，验收与规则逐条对照见 `ACCEPTANCE.md`，
人工 SOP 见 `skills/S1..S6/SKILL.md`。
