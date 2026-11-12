# Docker 常驻部署 · RatioGuard

> **为什么必须这么部署**：L1 是 5s 轮询的常驻进程，只在交易时段
> 09:30–11:30 / 13:00–15:00 采集，且快照每天 15:10 仅冻结一次。开发机白天休眠
> 就等于整条数据链路断供，前端再完整也只是演示。把采集端放到 7×24 常开的机器，
> 是这个系统唯一能积累出真实样本的前提。
>
> **没有历史回填**：免费源能拉到日 K / 5 分钟 K，但系统结算依赖 5s 采样聚合成
> 1 分钟 bar，且 `effective_sessions` 只按信号日期去重。把历史 K 线灌进主库会
> 制造「回测样本伪装成实盘快照」，直接破坏无前视口径。因此部署后从零开始积累。

## 1. 架构

```
docker-compose
├─ ratioguard-collector   python qctl.py serve --check-market
│    └─ 唯一数据生产者：采集 / 信号 / 结算 / 15:10 冻结 / 漂移 / 报表
└─ ratioguard-web         python frontend/serve.py --host 0.0.0.0
     └─ 只读大屏：/api/state、/api/market，不写业务表、不生成信号

宿主机卷（./data ./reports ./logs）→ 容器内 /app/{data,reports,logs}
共享 SQLite：data/qsignal.db（WAL），collector 写 / web 读
```

两个容器共用同一镜像 `ratioguard:latest`，只是 `command` 不同。均以
`restart: always` + `stop_grace_period: 30s` 运行，SIGTERM 时 `Runner` 优雅退出。

## 2. 前置条件（已实测环境）

| 项 | 要求 | 实测值 |
| --- | --- | --- |
| 系统 | Linux + Docker + Compose | Ubuntu 18.04 / Docker 20.10 / Compose v5 |
| 时区 | **必须 `Asia/Shanghai`** | `timedatectl` 已为 CST +0800；容器另挂 `/etc/localtime` 并装 `tzdata` |
| 用户 | 能直接跑 docker（免 sudo） | `scpun` 在 docker 组 |
| 网络 | 免费行情源可达 | 腾讯 0.09s / 新浪 0.14s / 东财 push2 亦可用 |
| 端口 | 8788 空闲 | 局域网直连 `http://<服务器IP>:8788` |

Docker Hub 直连慢时，先走可用镜像源再打标准 tag，让 Dockerfile 保持
`FROM python:3.11-slim` 的通用写法：

```bash
docker pull docker.1ms.run/library/python:3.11-slim
docker tag  docker.1ms.run/library/python:3.11-slim python:3.11-slim
```

## 3. 首次部署

```bash
# 1) 服务器建目录
mkdir -p /home/scpun/ratioguard/{data,reports,logs}

# 2) 本机同步源码（*.db / data / reports 不进镜像，单独传演示库）
cd RatioGuard
rsync -az \
  --exclude='.git/' --exclude='.workbuddy/' --exclude='__pycache__/' \
  --exclude='*.pyc' --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm' \
  --exclude='data/' --exclude='reports/' --exclude='logs/' --exclude='*.log' \
  ./ scpun@<服务器IP>:/home/scpun/ratioguard/
rsync -az frontend/demo_qsignal.db \
  scpun@<服务器IP>:/home/scpun/ratioguard/data/demo_qsignal.db

# 3) 服务器上生成 .env（容器以宿主机 uid 运行，避免 root 写卷）
ssh scpun@<服务器IP>
cd /home/scpun/ratioguard
printf 'UID=%s\nGID=%s\n' "$(id -u)" "$(id -g)" > .env
# 可选：钉钉推送密钥写入 .env 的 QSIGNAL_DINGTALK_WEBHOOK / _SECRET，不写进仓库

# 4) 校验、构建、启动
docker-compose config --quiet
docker-compose build collector
docker-compose up -d
docker-compose ps
```

首次启动 `collector` 会跑 `bootstrap`（自检 + `config_hash` 登记）。若启动时刻已过
15:10 且当天是交易日，会**补跑一次日终例程**，冻结一份「今日无采集数据」的空快照，
这是 `eod_done_date` 幂等补执行，正常现象，不影响次日冻结。

## 4. 访问大屏

`docker-compose.yml` 的 `web.ports` 为 `"8788:8787"`（等价 `0.0.0.0:8788`）：

```
http://<服务器IP>:8788/              真实运行库（默认）
http://<服务器IP>:8788/?source=demo  离线演示库（人工仿真，顶部 DEMO 标识）
```

**安全边界**：服务无鉴权，`/api/state` 的 `meta` 会暴露数据库绝对路径、库大小与
各表行数。当前配置仅限**可信内网**，切勿做路由器端口转发、公网反向代理或把端口
映射到公网 IP。若需要更严格隔离，把 `web.ports` 改为三段式
`"127.0.0.1:8788:8787"`（**首段是宿主机绑定 IP**，三段式仅用于只绑定回环地址；
默认部署仍是两段式 `"8788:8787"`），然后只走 SSH 隧道：

```bash
RG_SSH_HOST=<user>@<服务器IP> bash scripts/tunnel.sh   # 本机 http://127.0.0.1:8890/
```

电脑休眠只会断掉「查看」链路，服务器采集不受影响；隧道断了重跑脚本即可。

## 5. 日常运维

```bash
cd /home/scpun/ratioguard
docker-compose ps                          # 状态与健康检查
docker-compose logs --tail=50 collector    # 常驻服务日志（很安静，主要日志在 run_log 表）
docker-compose logs --tail=50 web
docker-compose restart collector           # 优雅重启（SIGTERM → 完成当前 tick 后退出）
docker-compose down && docker-compose up -d

# 业务命令要进容器执行
docker exec ratioguard-collector python qctl.py status
docker exec ratioguard-collector python qctl.py audit
docker exec ratioguard-collector python qctl.py selftest --check-market
```

`collector` 日志为空是**正常**的：`qctl.py serve` 是常驻进程，只在退出时输出 JSON，
全过程留痕写在 SQLite `run_log`。

### 自动恢复与备份（已装 cron）

| 机制 | 作用 |
| --- | --- |
| `restart: always` | 进程意外退出时由 Docker 拉起 |
| `scripts/watchdog.sh`，每 5 分钟 | 兜底：显式 `docker kill` 等会让 daemon 挂起重启策略，watchdog 检测非 running 就 `up -d`，日志 `logs/watchdog.log` |
| `scripts/backup_db.py`，每工作日 16:30 | `sqlite3.Connection.backup()` 在线备份到 `data/backups/`，保留最近 14 份，日志 `logs/backup.log` |
| Docker 服务 `enabled` | 服务器重启后容器随 daemon 自动拉起 |

```cron
30 16 * * 1-5 cd /home/scpun/ratioguard && docker exec ratioguard-collector python /app/scripts/backup_db.py >> /home/scpun/ratioguard/logs/backup.log 2>&1
*/5 * * * * bash /home/scpun/ratioguard/scripts/watchdog.sh
```

恢复备份：停服务 → 把 `data/backups/qsignal-YYYYMMDD.db` 复制成 `data/qsignal.db`
→ 删除残留 `qsignal.db-wal` / `-shm` → `docker-compose up -d`。

### 升级代码

```bash
# 本机：改完先自检
python -m py_compile frontend/serve.py src/qsignal/market/collector.py
node --check frontend/app.js
python run_acceptance.py ALL

# 同步到服务器后重建
cd /home/scpun/ratioguard
docker-compose build collector
docker-compose up -d
docker-compose ps
```

改 `html/css/js` 刷新浏览器即可；改任何 `.py` 都要**重建镜像并重启容器**（代码在镜像层）。
改 `config.yaml` 属于 `STAT_KEYS` 的键会切分 `config_hash`，样本重新分组，务必在
积累样本前决定。

## 6. 数据口径与已知限制

- **达标需要时间**：`N ≥ 100` 且覆盖 `≥ 20 个交易日`，每天 15:10 只冻结一次，
  盘中放行只引用最近一次冻结快照。部署后约需连续一个月交易时段才有机会达标。
- **不做历史回灌**：主库只接受实时采集的样本；历史 K 线可用于独立研究，但不得
  混入 `data/qsignal.db`。
- **交易日历降级**：`TradeCalendar` 默认用 akshare 拉交易日历；镜像不装
  akshare（体积大且需 pandas），因此降级为 `weekday() < 5`，法定节假日会被当成
  交易日。实际影响有限——节假日免费源返回的是上一交易日收盘快照，会被
  `max_quote_age_sec=60` 判 `stale_quote` 丢弃，不会产生假样本。
  若需精确日历，自行在镜像里装 akshare 或给 `TradeCalendar` 注入离线日历 loader。
- **`base_ratio` 无需手改**：配置里的 `1.005` 只是 `quote_table` 为空时的初值；
  `estimate_base_ratio()` 从已落库行情取滚动中位数，首条行情入库即自校准。
- **盘后行情 chip 置灰**：`/api/market` 返回最近收盘快照、`fresh=false`，仅做链路
  验证与展示，不参与任何判定。
- **演示库必须可写**：`init_db()` 会执行 `PRAGMA journal_mode=WAL`，所以演示库放
  在持久化卷 `data/demo_qsignal.db`，并用 `QSIGNAL_DEMO_DB` 指过去；只读单文件
  挂载会报 `attempt to write a readonly database`。

## 7. 故障排查

| 现象 | 处理 |
| --- | --- |
| `?source=demo` 返回空 / HTTP 000 | 演示库只读或 `QSIGNAL_DEMO_DB` 未指向可写卷；看 `docker-compose logs web` 的 traceback |
| 容器反复重启 | `docker-compose logs collector` + `docker exec ... python qctl.py selftest` 定位自检失败项 |
| 采不到行情 | `qctl.py selftest --check-market` 看命中 `source`；`run_log` 查 `fetch_failed` / `stale_quote` |
| 局域网打不开大屏 | `ss -ltnp \| grep 8788` 确认监听 `0.0.0.0`；检查服务器防火墙（ufw / iptables） |
| 大屏指标全 `—` | 真实库尚无样本，属正常空态；交易时段逐日积累 |
| 容器停了没自动起来 | 等 5 分钟 watchdog，或手动 `bash scripts/watchdog.sh` / `docker-compose up -d` |
| 镜像构建拉不到基础镜像 | 见 §2 换镜像源并 `docker tag` 成标准名 |
