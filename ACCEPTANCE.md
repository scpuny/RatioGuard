# 验收清单 · WorkBuddy 半自动量化信号系统

## 1. 验收总览

| 项目 | 值 |
| --- | --- |
| 验收入口 | `python run_acceptance.py ALL` |
| 最终全量结果 | **PASS（run=100, failures=0, errors=0）** |
| 运行环境 | Python 3.13.12（托管 venv，PyYAML 6.0.3），离线、无 akshare、无网络 |
| 补充验证（本轮） | 五级行情降级链接入后复跑 `run_acceptance.py ALL` → PASS 100/100；`qctl.py selftest --check-market` → `ok=true`，7 项检查通过，`source=tencent, ratio=0.95777` |
| 独立静态自检 | `python tools/static_scan.py` → `ok=true, scanned=29 files, violations=[]` |
| 启动自检 | `python qctl.py selftest` → `ok=true`，6 项检查全通过（加 `--check-market` 为 7 项） |
| 端到端运行证据 | `qctl.py serve --once` 已实际生成 `reports/dashboard.json`、`reports/report_2026-11-11.md`、`reports/signals_2026-11-11.csv` |

阶段用例数：M0=25、M1=20、M2=19、M3=13、M4=9、M5=14，合计 100。

## 2. 分阶段验收

| 阶段 | 用例数 | 结果 | 覆盖要点 |
| --- | --- | --- | --- |
| **M0 核心骨架与自检** | 25 | PASS | Wilson 已知值对拍 / 边界与非法输入；`break_even` 含成本；两比例 z；质量指标；硬规则下限；`config_hash` 与 `tp_sl_basis` 拒绝；静态扫描；免责合规；DB schema；启动自检 |
| **M1 行情采集与信号生成** | 20 | PASS | 行情归一化 / 脏数据过滤；非交易时段不请求；有效与无效行情都落库；行情源失败写日志；降级链结构（现为五级）；`quote_id` 绑定；SQL 无前视审计；未来行情抛 `LookaheadViolation`；阈值方向；重复 open 信号；rolling median；交易日历 |
| **M2 结算与统计快照** | 19 | PASS | 分钟 K 聚合；TP / SL / 双触保守 loss + `ambiguity=1`；短方向；多分钟顺序；EOD hold；hold 不计入统计；结算幂等；**结算不刷新快照**；三 scope；qualified 判定顺序；`config_hash` 分组；15:10 冻结幂等 |
| **M3 报表推送与大屏 JSON** | 13 | PASS | 三类推送合规；禁用话术拦截；冻结快照文案；钉钉 HMAC 加签；dry-run；pause / dedup / resume；Markdown 报表；大屏 JSON 新口径；CSV 导出 |
| **M4 漂移监控与配置版本隔离** | 9 | PASS | 窗口不足返回 `insufficient`；W1/W2 互斥且等长；Bonferroni 校正临界值；最小效应门槛阻止假告警；告警暂停并写日志；`drift_log` 供大屏读取；配置变更不误判漂移；运行态参数变更不切分样本；全库静态扫描零违规 |
| **M5 编排接入与端到端验收** | 14 | PASS | 启动自检 + 配置版本登记；参数变更留痕不误判漂移；会话门禁不发请求；采集→信号→无前视审计；盘中结算不刷新快照；15:10 冻结幂等 + 报表三件套；**冻结快照唯一放行**；置信不足不放行；漂移暂停与人工恢复；主循环 tick 上限；静态扫描覆盖 M5 新代码；日终审计标记违规；全输出携带免责；CLI 10 条命令冒烟 |

## 3. 强制规则逐条对照

| # | 规则 | 实现位置 | 验证用例 |
| --- | --- | --- | --- |
| 1 | 硬规则 / Wilson / 盈亏平衡 / 结算逻辑写在 Python 代码 + 单测；Skill 只做 SOP | `core/hard_rules.py`、`core/wilson.py`、`core/formulas.py`、`settle/settler.py`；`skills/*/SKILL.md` 均声明「非运行约束」 | M0 全部；M2 结算组；M5 `test_all_outputs_carry_disclaimer` |
| 2 | 快照每日 15:10 冻结一次，盘中不刷新来放行信号 | `stats/snapshot.py::freeze_daily_snapshot`（`freeze_date+created_by` 幂等）；`service/runner.py::_on_new_signal` 只读 `get_latest_frozen()` | M2 冻结幂等；M2 结算不刷新；M5 `test_signal_gate_uses_only_frozen_snapshot`、`test_intraday_settle_never_refreshes_snapshot` |
| 3 | 互斥窗口两比例检验 + 最小效应门槛，修复子集包含 | `stats/drift.py::compute_drift`（W1=`valid[-w:]`，W2=`valid[-2w:-w]`，Bonferroni + `min_effect_size`） | M4 全部 9 例 |
| 4 | 分钟 K 高低判触；双触保守 loss + `ambiguity=1`；报表展示歧义率 | `settle/settler.py::build_bars` / `evaluate`；`report/renderer.py::quality.ambiguity_rate` | M2 双触 / 多分钟；M3 大屏口径；M5 报表与冻结 |
| 5 | 原始行情入 `quote_table`，信号绑定 `quote_id`，SQL 审计无前视 | `market/collector.py::collect_once`、`strategy/generator.py` 的 `quote_ts <= signal_ts` 查询、`core/audit.py::audit_no_lookahead` | M1 审计组；M5 `test_tick_collect_generate_and_no_lookahead`、`test_eod_audit_flags_lookahead_violation` |
| 6 | `config_hash` 分组、不跨版本合并、参数变更不误判漂移 | `core/config.py::STAT_KEYS/compute_config_hash`、`core/config_guard.py`、`core/audit.py::log_config_change` | M0 hash 组；M2 分组；M4 配置版本隔离；M5 `test_config_change_is_logged_not_treated_as_drift` |
| 7 | 客观文案、禁「建议交易」、推送与报表强制完整免责 | `core/disclaimer.py`（`DISCLAIMER` / `assert_compliant`）、`notify/notifier.py`、`report/renderer.py`、`templates/dingtalk.md`（已含全文） | M3 合规 / 禁用话术；M5 `test_all_outputs_carry_disclaimer` |
| 8 | 静态自检脚本扫描禁用代码 | `tools/static_scan.py`、`core/static_scan.py`、`tools/forbidden_patterns.json` | M0 / M4 全库扫描；M5 `test_static_scan_clean_including_m5_code`；独立 CLI 运行 |
| 9 | 交付物完整（config / SQL / Python 模块 / 单测 / SKILL.md / 钉钉模板 / 大屏 JSON 结构） | 见第 4 节清单 | M5 CLI 冒烟 + 文件存在性断言 |
| 10 | 大屏新增歧义占比、hold 率、互斥窗口 z 值、配置版本标记 | `report/renderer.py::build_dashboard_payload`；`templates/dashboard_schema.json` / `dashboard_example.json` | M3 大屏用例；M5 `test_eod_freeze_once_idempotent_and_reports`、`test_all_outputs_carry_disclaimer` |

**定位重申**：系统只做观测、记录、统计、告警，无自动下单、键鼠、OCR 逻辑；
统计只代表历史样本快照，不预测收益。`hold` 不计入胜率 / Wilson / 盈亏平衡，
但进入 hold 率；日内不隔夜，15:00 强制 hold。前端大屏只读，不写业务表、
不生成信号、不参与放行判定。

## 4. 交付物清单核对

| 交付物 | 路径 | 状态 |
| --- | --- | --- |
| 运行配置 | `config.yaml` | ✅ |
| SQL 建表语句 | `sql/schema.sql`（6 张表） | ✅ |
| Python 模块 | `src/qsignal/` 下 core / market / strategy / settle / stats / notify / report / service | ✅ |
| 常驻服务编排 | `src/qsignal/service/runner.py`（`Runner.tick` / `eod_routine` / `run`） | ✅ |
| 统一 CLI | `qctl.py`（12 条命令） | ✅ |
| 单元测试 | `tests/test_m0_core.py` … `tests/test_m5_e2e.py` + `tests/base.py` | ✅ 100 例 |
| 分阶段验收入口 | `run_acceptance.py` | ✅ |
| 6 个 SKILL.md | `skills/S1-market-collect` … `skills/S6-report` | ✅ |
| 钉钉推送模板 | `templates/dingtalk.md`（含完整免责全文） | ✅ |
| 免责副本 | `templates/disclaimer.txt` | ✅ |
| 动态大屏结构 | `templates/dashboard_schema.json` + `dashboard_example.json` | ✅ |
| 科技蓝只读大屏 | `frontend/`（`serve.py` / `index.html` / `styles.css` / `app.js` / `seed_demo.py`） | ✅ 本轮新增 |
| 静态自检 | `tools/static_scan.py` + `tools/forbidden_patterns.json` | ✅ |
| systemd unit | `deploy/qsignal.service` + `deploy/qsignal.env.example` | ✅ |
| 低频编排配置 | `deploy/workbuddy-automation.md`（3 条 RRULE 与 prompt） | ✅ |
| 部署说明 | `deploy/README.md` | ✅ |
| 顶层说明 | `README.md` | ✅ |
| 使用手册 | `USAGE.md` | ✅ |
| 验收清单 | `ACCEPTANCE.md`（本文件） | ✅ |
| 运行示例产物 | `reports/dashboard.json`、`reports/report_2026-11-11.md`、`reports/signals_2026-11-11.csv` | ✅ 由 `serve --once` 实际生成 |

## 5. 已知限制与未覆盖项（诚实声明）

1. **真实行情链路已联网验证，但未做盘中回放**：`selftest --check-market` 实测命中
   腾讯源（`source=tencent`，返回 510300 / 159919 收盘快照），证实纯标准库兜底可用；
   五级降级链（akshare 东财全表 → 东财单只 → 腾讯 → 新浪 → 同花顺）的结构与脏数据
   过滤由 `FakeProvider` 单测覆盖，**但尚未在连续交易时段做盘口回放**。
2. **真实钉钉推送未发送**：未配置 Webhook / Secret；签名 URL 构造、
   dry-run、去重、暂停恢复均由单测覆盖，未做真实网络投递。
3. **分钟 K 路径定序依赖采样密度**：5s 采样下同分钟双触仍无法定序，
   已按规则保守判 loss，但真实盘中行情未回放验证歧义率量级。
4. **systemd 未在目标机器启用**：仅交付 unit 文件与部署步骤，
   `ExecStart` / `User` / `WorkingDirectory` 需按目标环境调整。
5. **WorkBuddy automation 未实际创建**：`deploy/workbuddy-automation.md`
   给出可直接使用的 RRULE 与 prompt，是否创建由用户决定；
   且 automation 无法识别 A 股节假日，会话首步必须调用 `session_gate()`。
6. **统计样本需真实积累**：当前 `reports/` 示例基于空库，
   `qualified=False`（`below_hard_floor` / `insufficient_sample`）。
   按配置需 ≥100 笔有效样本且覆盖 ≥20 个交易日，Wilson 下限才可能超过
   含成本盈亏平衡胜率 0.75。这是硬规则，不是缺陷。
7. **`tp_sl_basis` 当前仅支持 `entry_ratio`**：`base_ratio` 口径需重推
   `break_even`，`core/config.py` 会拒绝加载，属于已知的版本边界。
8. **前端大屏未纳入单测**：`frontend/` 为纯展示层，统计口径复用
   `build_dashboard_payload()`，已用无头浏览器按 1920×1080 对 DEMO / LIVE / 空库
   三种状态做视觉与坐标实测；但无自动化测试覆盖，属交付边界。

## 6. 复现命令

```bash
cd RatioGuard                        # 进入仓库根目录
PYTHON=python                        # 已安装 requirements.txt 的解释器

$PYTHON run_acceptance.py ALL        # PASS 100/100
$PYTHON tools/static_scan.py         # ok=true, violations=[]
$PYTHON qctl.py selftest             # ok=true，6 项
$PYTHON qctl.py selftest --check-market  # 追加行情链路探测，7 项
$PYTHON qctl.py status               # 运行态与审计概览
```
