# 部署说明 · 模式 A（L1 常驻服务 + L2 低频编排）

## 1. 架构定位

```
L1  Python 常驻服务（本目录 qsignal.service）
    └─ 5s 采集 / 信号生成 / 1min 结算 / 15:10 冻结 / 漂移 / 报表 / 推送
L2  WorkBuddy automation（见 workbuddy-automation.md）
    └─ 日终复核、周报、月报；低频、人工交互、不触碰高频链路
共享持久化：data/qsignal.db（SQLite WAL）
```

**为什么必须有 L1**：WorkBuddy automation 只支持分钟级触发，每次触发是全新
Agent 会话，无法承担 5s 轮询与事件级结算。高频链路必须放在本地常驻进程。

## 2. 环境准备

```bash
cd /home/yangbo/WorkBuddy/charge

# 使用托管 venv（已装 PyYAML）；若新建 venv：
# /home/yangbo/.workbuddy/binaries/python/versions/3.13.12/bin/python3 -m venv \
#     /home/yangbo/.workbuddy/binaries/python/envs/default
# /home/yangbo/.workbuddy/binaries/python/envs/default/bin/pip install -r requirements.txt

PYTHON=/home/yangbo/.workbuddy/binaries/python/envs/default/bin/python

# akshare 为可选实时行情依赖（离线自检与全部单测不需要）
$PYTHON -m pip install akshare        # 仅当需要真实行情时

$PYTHON qctl.py selftest              # 静态扫描 / 硬规则 / 公式 / schema / 免责
$PYTHON qctl.py bootstrap             # 自检 + 配置版本登记
$PYTHON run_acceptance.py ALL         # M0→M5 全量验收
```

> 自检不通过时 `qctl.py serve` 会拒绝启动（退出码 1）。

## 3. 启动常驻服务

前台调试：

```bash
$PYTHON qctl.py serve --once           # 只跑一轮
$PYTHON qctl.py serve --max-ticks 10   # 跑 10 轮后退出
$PYTHON qctl.py serve                  # 常驻，Ctrl-C 优雅退出
```

systemd：

```bash
cp deploy/qsignal.env.example deploy/qsignal.env
chmod 600 deploy/qsignal.env
# 填入 QSIGNAL_DINGTALK_WEBHOOK / QSIGNAL_DINGTALK_SECRET

sudo cp deploy/qsignal.service /etc/systemd/system/qsignal.service
# 按需修改 User / WorkingDirectory / ExecStart 中的绝对路径
sudo systemctl daemon-reload
sudo systemctl enable --now qsignal
sudo systemctl status qsignal
journalctl -u qsignal -f
```

日志另写 `data/service.log`；业务全链路日志在 SQLite `run_log` 表。

## 4. 配置与密钥

- 唯一运行配置：`config.yaml`（所有参数集中于此）；
- 钉钉密钥**不要**写入 `config.yaml`，用环境变量或 `deploy/qsignal.env`；
- 修改属于 `STAT_KEYS` 的参数会生成新 `config_hash`，样本自动分段，
  变更写入 `run_log` 与 `drift_log(status=config_change)`，不误判为漂移；
- `eod_freeze` 固定 `15:10`、`min_qualified_samples >= 60`、
  `min_qualified_sessions >= 10`、`min_window_samples >= 30` 为硬下限，
  配置下调会被 `core/hard_rules.py` 拒绝。

## 5. 日常运维

| 目的 | 命令 |
| --- | --- |
| 运行态概览 | `qctl.py status` |
| 无前视审计 | `qctl.py audit`（违规返回码 1） |
| 手动补冻快照 | `qctl.py freeze`（`created_by=manual`） |
| 立即结算 | `qctl.py settle` |
| 立即漂移复核 | `qctl.py drift` |
| 生成报表 / 大屏 | `qctl.py report` |
| 暂停推送 | `qctl.py pause --reason "<说明>"` |
| 恢复推送 | `qctl.py resume --reason "人工复核后恢复"` |

## 6. 数据备份与升级

- 库文件：`data/qsignal.db`（WAL 模式，备份时连 `-wal` / `-shm` 一起处理，
  或使用 `sqlite3 .backup`）；
- 报表与 CSV：`reports/`；
- 升级前先跑 `run_acceptance.py ALL`，升级后跑 `qctl.py status` 与
  `qctl.py audit` 核对 `config_hash` 变化。

## 7. 边界

系统只做观测、记录、统计、告警。不含自动下单、券商客户端调用、
键鼠自动化、OCR 逻辑。`tools/static_scan.py` 会扫描生产代码，
命中禁用标识符即返回退出码 1。
