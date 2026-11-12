# 部署说明 · 模式 A（L1 常驻服务，L2 为未启用的设计备选）

## 1. 架构定位

```
L1  Python 常驻服务（本目录 qsignal.service）
    └─ 5s 采集 / 信号生成 / 1min 结算 / 15:10 冻结 / 漂移 / 报表 / 推送
L2  WorkBuddy automation（见 workbuddy-automation.md）⚪ 未启用、不推荐
    └─ 设计备选：日终复核 / 周报 / 月报摘要；3 条 automation 从未创建
       L1 已产出全部原始报表，L2 仅多一层 LLM 摘要且有持续 token 成本
只读展示  frontend/serve.py（可选）
    └─ 科技蓝大屏 + /api/state、/api/market；读同一 SQLite，不参与任何判定
共享持久化：data/qsignal.db（SQLite WAL）
```

**为什么必须有 L1**：WorkBuddy automation 只支持分钟级触发，每次触发是全新
Agent 会话，无法承担 5s 轮询与事件级结算。高频链路必须放在本地常驻进程。
**部署时只需启动 L1 与可选的 `frontend/` 大屏，无需配置任何 WorkBuddy 能力**；
`skills/` 是普通 SOP 文档目录，不参与部署、不需要注册。

**两种等价落地方式**：① systemd 直跑（本文件 §3）；② Docker Compose 双容器
（`deploy/DOCKER.md`，推荐用于 7×24 常开服务器）。两者只是承载同一个 L1 进程，
统计口径完全一致。开发机白天休眠会让 5s 采集链路断供，要积累真实样本必须用
常开机器，即方式 ②。

## 2. 环境准备

```bash
cd RatioGuard                       # 进入仓库根目录

# 使用已装 PyYAML 的解释器；若新建 venv：
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -r requirements.txt
PYTHON=python

# 本机开发验证可用托管 venv（macOS 路径，按目标环境调整）：
# PYTHON=/Users/yangbo/.workbuddy/binaries/python/envs/default/bin/python

# akshare 仅「东财 ETF 全表」源需要；腾讯 / 新浪源为纯标准库，不装也能采真实行情
# $PYTHON -m pip install akshare

$PYTHON qctl.py selftest --check-market   # 含真实行情链路连通性探测
$PYTHON qctl.py bootstrap                 # 自检 + 配置版本登记
$PYTHON run_acceptance.py ALL             # M0→M5 全量验收
```

> 自检不通过时 `qctl.py serve` 会拒绝启动（退出码 1）。
> `--check-market` 真实访问免费行情源，离线环境可去掉该参数。

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

### 3.1 Docker Compose（推荐，常开服务器）

```bash
printf 'UID=%s\nGID=%s\n' "$(id -u)" "$(id -g)" > .env   # 容器以宿主机 uid 运行
docker-compose config --quiet
docker-compose build collector
docker-compose up -d
docker-compose ps
```

`collector` 与 `web` 共用镜像，数据在宿主机卷 `./data`；默认大屏
`http://<服务器IP>:8788/`。代码改动需 `build collector && up -d`。
完整前置条件、镜像加速、备份/watchdog、排障见 **`deploy/DOCKER.md`**。

## 4. 配置与密钥

- 唯一运行配置：`config.yaml`（所有参数集中于此）；
- 钉钉密钥**不要**写入 `config.yaml`，用环境变量或 `deploy/qsignal.env`；
- 修改属于 `STAT_KEYS` 的参数会生成新 `config_hash`，样本自动分段，
  变更写入 `run_log` 与 `drift_log(status=config_change)`，不误判为漂移；
- `eod_freeze` 固定 `15:10`、`min_qualified_samples >= 60`、
  `min_qualified_sessions >= 10`、`min_window_samples >= 30` 为硬下限，
  配置下调会被 `core/hard_rules.py` 拒绝；
- `base_ratio=1.005` 只是 `quote_table` 为空时 `rolling_median` 的初值，首条行情
  入库后 `estimate_base_ratio()` 即自校准到真实比价，通常无需修改；该键属于
  `STAT_KEYS`，确要改必须在积累样本之前（详见 `USAGE.md` §6.4）。

## 5. 日常运维

| 目的 | 命令 |
| --- | --- |
| 运行态概览 | `qctl.py status` |
| 无前视审计 | `qctl.py audit`（违规返回码 1） |
| 手动补冻快照 | `qctl.py freeze`（`created_by=manual`） |
| 立即结算 | `qctl.py settle` |
| 立即漂移复核 | `qctl.py drift` |
| 生成报表 / 大屏 JSON | `qctl.py report` |
| 启动前端大屏（只读） | `frontend/serve.py [--port 8787] [--demo]` |
| 暂停推送 | `qctl.py pause --reason "<说明>"` |
| 恢复推送 | `qctl.py resume --reason "人工复核后恢复"` |

### 5.1 前端大屏（可选）

```bash
$PYTHON frontend/seed_demo.py      # 首次生成离线演示库（*.db 不随仓库分发）
$PYTHON frontend/serve.py          # http://127.0.0.1:8787/
```

- 本地直跑默认只监听 `127.0.0.1`；Docker 部署按 `docker-compose.yml` 监听
  `0.0.0.0:8788`，可信局域网可直接访问 `http://<服务器IP>:8788/`。
  服务**无鉴权**，`/api/state` 的 `meta` 会暴露数据库绝对路径、库大小与各表行数，
  因此仅限内网使用，**禁止路由器端口转发 / 公网反向代理**。
  更严格隔离可在 `web.ports` 用三段式 `"127.0.0.1:8788:8787"`（首段是宿主机绑定
  IP；默认部署保持两段式 `"8788:8787"`），只走 SSH 隧道（`scripts/tunnel.sh`，
  需显式设置 `RG_SSH_HOST`）。详见 `deploy/DOCKER.md`。
- 大屏只读同一个 SQLite，不修改统计 / 放行 / 快照口径；`/api/market` 复用 L1 的
  五级行情降级链，20s 缓存，不写库、不生成信号、不进统计。
- 页面加 `?source=live` / `?source=demo` 可即时切库，无需重启；生产环境建议
  固定 `--db data/qsignal.db`，并在界面确认顶部显示 `LIVE 真实库`。
- 改 html / css / js 刷新浏览器即可；改 `serve.py` 或采集链需重启 Python 进程。
- 完整说明（接口、双数据源、空库语义、真实数据积累）见 `USAGE.md` §6.4。

## 6. 数据备份与升级

- 库文件：`data/qsignal.db`（WAL 模式，备份时连 `-wal` / `-shm` 一起处理，
  或使用 `sqlite3 .backup`）；
- Docker 部署已内置 `scripts/backup_db.py`（`sqlite3.Connection.backup()`，WAL 安全，
  每工作日 16:30、保留 14 份）与 `scripts/watchdog.sh`（每 5 分钟兜底拉起容器），
  cron 配置见 `deploy/DOCKER.md`；
- 报表与 CSV：`reports/`；
- 演示库 `frontend/demo_qsignal.db` 可随时用 `seed_demo.py` 重建，无需备份；
- 升级前先跑 `run_acceptance.py ALL`，升级后跑 `qctl.py status` 与
  `qctl.py audit` 核对 `config_hash` 变化。

## 7. 边界

系统只做观测、记录、统计、告警。不含自动下单、券商客户端调用、
键鼠自动化、OCR 逻辑；前端大屏与行情探针只读，不写业务表、不生成信号、
不参与放行判定。`tools/static_scan.py` 会扫描生产代码，
命中禁用标识符即返回退出码 1。
