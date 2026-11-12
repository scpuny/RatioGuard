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
| 可选依赖 | `akshare`：仅「东财 ETF 全表」源需要；腾讯 / 新浪源为纯标准库实现，**不装也能采集真实行情** |
| 数据库 | SQLite（标准库内置，无需安装服务） |
| 网络 | 采集阶段 + 大屏 `/api/market` 只读行情探针访问免费行情源；钉钉推送可选 |

下文统一用 `$PYTHON` 指代已装好 `PyYAML` 的解释器。普通 venv 即可：

```bash
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -r requirements.txt
PYTHON=python
```

本机开发验证用的是 WorkBuddy 托管 venv（默认 `python3` 缺 PyYAML，可直接指定绝对路径）：

```bash
PYTHON=/Users/yangbo/.workbuddy/binaries/python/envs/default/bin/python
```

> `akshare` 只在想启用「东财 ETF 全表」源时才需要：`python -m pip install akshare`。
> 不装时会依次尝试东财单只 → 腾讯 → 新浪 → 同花顺，前三级均为纯标准库、免费无 key。
> 离线自检与全部 100 个单测不访问网络，也不需要 akshare。

---

## 1. 安装

```bash
git clone https://github.com/scpuny/RatioGuard.git
cd RatioGuard
python -m pip install -r requirements.txt
```

目录无需额外初始化，首次运行 `qctl.py` 会自动创建 `data/qsignal.db` 与 6 张表。

> `frontend/demo_qsignal.db` 是演示库二进制，**不随仓库分发**（`.gitignore` 排除 `*.db`）。
> 想看大屏演示数据需本地生成，见 §6.4。

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
$PYTHON qctl.py selftest --check-market  # 追加第 7 项：真实行情链路，打印 source= 与 ratio=
$PYTHON qctl.py bootstrap           # 自检 + 登记 config_hash
$PYTHON run_acceptance.py ALL       # M0→M5 全量验收，应输出 PASS 100/100
$PYTHON tools/static_scan.py        # 生产代码禁用标识符扫描，应输出 ok=true
```

`--check-market` 会真实访问免费行情源并按五级降级链依次尝试，离线环境可跳过。
它只探测连通性，不写库、不生成信号。正常输出形如
`source=tencent, ratio=0.9577659819986153`。

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

> 上表全部由 L1 常驻服务完成，**不依赖 WorkBuddy**。`deploy/workbuddy-automation.md`
> 描述的 L2 低频编排是设计备选，**从未创建、当前不推荐启用**（唯一增量是用 LLM
> 写摘要，有持续 token 成本）；直接看 `qctl.py status` / `audit`、
> `reports/` 与 `frontend/` 大屏即可，数据同源且零成本。

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
| 首次生成前端演示库 | `$PYTHON frontend/seed_demo.py` |
| 启动前端大屏（只读 HTTP） | `$PYTHON frontend/serve.py [--port 8787] [--demo]` |

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

### 6.4 前端大屏（科技蓝，可选）

`frontend/` 是一个**只读**本地大屏：1920×1080 设计稿等比缩放，展示核心指标、放行判定链、
三档快照对比、漂移 z 值趋势、质量指标、最近信号 / 漂移记录 / 运行日志。
统计口径全部复用 `build_dashboard_payload()`，前端不重新实现任何判定逻辑。

#### 启动

```bash
$PYTHON frontend/serve.py                          # 默认 127.0.0.1:8787，读 config.yaml 的真实库
$PYTHON frontend/serve.py --demo                   # 默认切到离线演示库
$PYTHON frontend/serve.py --port 8788 --db data/qsignal.db
# 浏览器打开 http://127.0.0.1:8787/
```

参数：`--host`（默认 127.0.0.1，仅本机监听）、`--port`（默认 8787）、`--db PATH`、
`--demo`；环境变量 `QSIGNAL_DB` 指定默认真实库、`QSIGNAL_DEMO_DB` 指定演示库
（Docker 部署时必须指向**可写**的持久化卷，因为 `init_db` 会执行
`PRAGMA journal_mode=WAL`）。页面每 5s 自动拉数据。
**改 html / css / js 刷新浏览器即可；改 `serve.py` 或采集链必须重启 Python 进程。**

#### 演示库

`frontend/demo_qsignal.db` 是二进制演示数据，**不随仓库分发**。首次使用需本地生成：

```bash
$PYTHON frontend/seed_demo.py
```

生成的是人工仿真数据（`note='demo'`，覆盖 30 个交易日的信号与行情、历史漂移检验记录、
跨日运行日志），仅用于界面演示，不代表真实策略表现；
大屏顶部常驻琥珀色 `DEMO 演示数据` 标识。

#### 双数据源切换（无需重启）

| 优先级 | 方式 | 说明 |
| --- | --- | --- |
| 1 | URL `?source=live` / `?source=demo` | 前端透传给 `/api/state`，即时生效 |
| 2 | 启动参数 `--db PATH` / `--demo` | 决定不带参数时的默认库 |
| 3 | 环境变量 `QSIGNAL_DB` | 部署时覆盖默认库 |
| 4 | `config.yaml` 的 `db_path` | 兜底，指向 `data/qsignal.db` |

顶部绿色 `LIVE 真实库` / 琥珀 `DEMO 演示数据` chip 标明当前数据源，悬浮可见库路径、
各表行数与最后行情时间。

#### 接口（白名单路由，只认以下路径）

| 路径 | 内容 |
| --- | --- |
| `/`、`/styles.css`、`/app.js` | 静态页面 |
| `/api/state?source=...` | `dashboard` + `signals`(40) + `drift`(25) + `runlog`(40) + `meta`（数据源、库路径、各表行数、库修改时间） |
| `/api/market` | 只读真实行情探针，20s TTL |

`/api/market` 复用 L1 采集的 `AkShareProvider` 五级降级链，返回两只 ETF 最新价、
各自报价时间戳、`fresh`（是否落在 `max_quote_age_sec` 内）、实时比价与 `base_ratio`。
**它不写 `quote_table`、不生成信号、不进入任何统计 / 放行口径**；盘后返回的是最近一次
收盘快照，`fresh=false` 时行情 chip 置灰，属预期行为。

#### 真实数据从哪来

大屏不是行情源，也不自己产生统计。`data/qsignal.db` 的数据由 L1 常驻服务在
**交易时段（09:30–11:30 / 13:00–15:00）**采集、结算、每日 15:10 冻结产生：

```bash
$PYTHON qctl.py selftest --check-market   # 确认行情源可达
$PYTHON qctl.py serve                     # 常驻：5s 采集 / 结算 / 15:10 冻结+漂移+报表
$PYTHON frontend/serve.py                 # 另开终端看大屏（?source=live）
```

新库初始为空，大屏指标显示 `—`（不是 `0.00%`），各表格与漂移趋势区给出空态说明。
放行要求 **N ≥ 100 且覆盖 ≥ 20 个交易日**，只能逐日积累；系统**不做历史行情回灌**——
把回测数据伪装成实盘样本快照会破坏无前视与冻结口径。若用 `qctl.py freeze` 手动补冻，
快照会标记 `created_by=manual`。

> **关于 `base_ratio` 初值**：`config.yaml` 的 `1.005` 仅在 `quote_table` 为空时作为
> `rolling_median` 的初值；`estimate_base_ratio()` 实际从已落库行情取 60 天中位数，
> 而 `generate_signal()` 必然在 `collect_once()` 插入首条行情之后调用，因此首条行情
> 即自校准到真实比价（约 `0.9578`），**不会误触发 `spread_short`**。该键属于
> `STAT_KEYS`，改动会切分 `config_hash`、分裂样本分组，通常无需修改；
> 确要改必须在积累样本之前改。

#### 合规提示与兼容

- 顶部 `放行依据：15:10冻结快照` 强调盘中不实时重算；核心指标面板常驻
  `⚠ 历史快照` 警示条（高胜率≠未来、结构切换失效、依赖融券、本金门槛高）；
  `盈亏平衡胜率` 标注已计入双边成本 `c=0.0005`；底部保留后端合规免责原文。
- 空样本显示 `—`，`z` 值缺失不显示为 `0.000`（避免把「未检验」误读为「无漂移」）。
- 后端为旧版（`/api/state` 无 `meta`、`/api/market` 返回 404）时，新增的数据源 / 行情
  chip 自动隐藏，页面保持旧版外观，不会报错。
- 大屏只读 SQLite，不触发任何交易动作，也**不修改**统计、放行、快照口径。

### 6.5 Docker 常驻部署（推荐）

L1 是 5s 轮询的常驻进程，只在交易时段采集、每天 15:10 冻结一次。开发机白天休眠
会让整条数据链路断供，所以真实运行应放到 7×24 常开的机器上：

```bash
# 本机同步源码（*.db / data / reports / logs 不进镜像，演示库单独传）
rsync -az --exclude='.git/' --exclude='.workbuddy/' --exclude='*.pyc' \
  --exclude='*.db' --exclude='data/' --exclude='reports/' --exclude='logs/' \
  ./ <user>@<host>:/path/to/ratioguard/
rsync -az frontend/demo_qsignal.db <user>@<host>:/path/to/ratioguard/data/

# 服务器
ssh <user>@<host>
cd /path/to/ratioguard
printf 'UID=%s\nGID=%s\n' "$(id -u)" "$(id -g)" > .env
docker-compose build collector && docker-compose up -d
```

`collector`（唯一数据生产者）与 `web`（只读大屏）共用同一镜像，SQLite 落在宿主机
卷 `./data`。默认 `web.ports: "8788:8787"`，可信局域网直接打开
`http://<服务器IP>:8788/`；服务无鉴权，**勿做路由器端口转发或公网反代**。
改任何 `.py` 都要重建镜像并重启容器，改 html/css/js 刷新浏览器即可。

首次启动若已过 15:10 会补跑一次日终例程、冻结一份空快照，属幂等补执行。
自动恢复（watchdog）、每日在线备份、升级流程、镜像加速与完整前置条件见
**`deploy/DOCKER.md`**。

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

## 8. 设计备选：WorkBuddy 低频编排（未启用，不推荐）

> **现状**：`deploy/workbuddy-automation.md` 里的 3 条 automation **从未创建**；
> `skills/S1..S6/SKILL.md` 是普通目录下的人类可读 SOP，**不在 WorkBuddy 的
> Skill 加载路径**、从未被加载，生产代码零引用。系统拔掉这一层照常运行。

该文档列出 3 条可选配置值（日终复核 15:15、周报周一 09:00、月报每月 1 日 09:30）
的 RRULE 与 prompt，仅供将来参考。保留它的意义是记录架构决策依据，不代表
这些能力已存在。若确实要启用，还需额外创建 automation、迁移 SKILL.md 到
`.workbuddy/skills/`、按目标机器修正 `cwds` 并注册 slash command。

**为什么当前不推荐启用**：L1 的 15:10 日终例程已产出全部原始报表与结构化数据，
L2 剩下的唯一增量只是「用 LLM 把结构化结果汇总成人话摘要」，却要为此承担
每次触发一次全新 Agent 会话的 token 开销和免费档限流 / 429 风险。
零成本替代：`qctl.py status` / `audit`、`reports/`、以及 `frontend/` 大屏的
`/api/state`。

其余能力边界（若将来启用仍需遵守）：

- automation 只支持分钟级，**不能**承担 5s 采集；
- 每次触发是全新会话，状态全部依赖 SQLite；
- RRULE 不识别 A 股节假日，会话首步必须调用 `session_gate()` 判断交易日；
- 文档早期列出的 `/report` `/status` 等斜杠指令**从未注册、并不存在**，
  人工复核请直接执行 `qctl.py` 对应子命令（见 §5）。

---

## 9. 故障排查

| 现象 | 原因 / 处理 |
| --- | --- |
| `ModuleNotFoundError: No module named 'yaml'` | 用错解释器。先 `pip install -r requirements.txt`，或改用已装 PyYAML 的托管 venv（见 §0） |
| `serve` 启动即退出，退出码 1 | 自检未通过。先跑 `qctl.py selftest` 看哪项失败（常见：硬下限被下调） |
| 钉钉没收到消息 | `status` 看 `pause_trade_alert`；未配 webhook 时 `reason=dry_run` 属正常，文案仍生成并校验 |
| `qualified` 长期为 False | 样本不足或漂移暂停，属硬规则；不要为了放行而下调门槛 |
| 推送停止 | 漂移告警触发了暂停，人工复核后 `qctl.py resume` |
| `audit` 返回 1 | 存在 `quote_id` 缺失或 `quote_ts > signal_ts` 的信号，需人工核查数据来源 |
| `database is locked` | 已启用 WAL + `busy_timeout=30000`；仍出现时检查是否有多个 `serve` 进程同时运行 |
| 行情全部跳过 | 非交易时段不发请求（正常）；交易时段检查 `run_log` 中 `fetch_failed` 与网络 |
| 东财行情连不上 | 已内置腾讯 / 新浪纯标准库兜底；`selftest --check-market` 看实际命中的 `source`，`run_log` 看每轮采集来源 |
| 大屏指标全是 `—`、表格提示空态 | 真实库尚无样本，属正常；见 §6.4「真实数据从哪来」，交易时段跑 `qctl.py serve` 逐日积累 |
| 大屏 `?source=demo` 无数据 | 演示库未生成（不随仓库分发），先跑 `$PYTHON frontend/seed_demo.py` |
| 大屏行情 chip 无价格 / 显示灰色 | 免费行情源全部不可达，或盘后快照 `fresh=false`；用 `selftest --check-market` 确认链路 |
| 改了前端没生效 | html / css / js 刷新浏览器；改 `serve.py` / `collector.py` 必须重启 Python 进程 |
| 找不到 `/report` `/status` 等斜杠指令，或 automation / Skill 没生效 | 这些指令从未注册、3 条 automation 从未创建、`skills/` 不在 Skill 加载路径，属**设计上未启用**；直接用 §5 的 `qctl.py` 子命令与 `frontend/` 大屏即可（见 §8） |
| 大屏两个新 chip 不显示 | 后端是旧版进程（无 `meta` / `/api/market` 404），前端自动隐藏属预期；重启 `frontend/serve.py` 即可 |
| Docker 下 `?source=demo` 报 `attempt to write a readonly database` | 演示库被只读挂载；用 `QSIGNAL_DEMO_DB` 指向可写的 `data/demo_qsignal.db`（见 §6.5 / `deploy/DOCKER.md`） |
| 容器停了没自动起来 | 显式 `docker kill` 会让 daemon 挂起重启策略；等 5 分钟 watchdog，或手动 `bash scripts/watchdog.sh` / `docker-compose up -d` |
| 局域网打不开服务器大屏 | 确认 `web.ports` 为 `"8788:8787"`、`ss -ltnp` 监听 `0.0.0.0:8788`，并检查服务器防火墙 |

结构化日志检索示例：

```sql
SELECT ts, module, action, level, message FROM run_log
WHERE action IN ('signal_gate','eod_routine','drift_pause','lookahead_audit')
ORDER BY id DESC LIMIT 20;
```

---

## 10. 边界重申

本系统只做观测、记录、统计、告警，**没有任何自动下单、键鼠自动化、OCR 逻辑**；
前端大屏与 `/api/market` 只读，不写业务表、不生成信号、不参与放行判定。
统计只代表历史样本快照，不预测收益。所有输出不构成投资建议。

更多设计口径见 `README.md`，验收与规则逐条对照见 `ACCEPTANCE.md`，
人工 SOP 见 `skills/S1..S6/SKILL.md`，前端大屏完整说明见 §6.4，
Docker 常驻部署与运维见 §6.5 和 `deploy/DOCKER.md`。
