#!/usr/bin/env bash
# 本机一键建立到服务器大屏的 SSH 隧道（可选）。
# 默认部署的 web 监听 0.0.0.0:8788，可信局域网可直接 http://<服务器IP>:8788 访问。
# 若把 compose 端口改回 "127.0.0.1:8788:8787" 做严格隔离，则用本脚本访问。
# 电脑休眠后隧道会断，重新执行即可；采集在服务器侧不受影响。
set -euo pipefail

# 服务器地址不写进仓库，部署时显式传入：
#   RG_SSH_HOST=user@<服务器IP> bash scripts/tunnel.sh
HOST="${RG_SSH_HOST:-}"
if [ -z "$HOST" ]; then
  echo "请先设置 RG_SSH_HOST（例：RG_SSH_HOST=user@192.168.x.x bash scripts/tunnel.sh）" >&2
  exit 1
fi
# 本机 8788 可能仍被早期的本地演示实例占用，故隧道默认用 8890
LOCAL_PORT="${RG_LOCAL_PORT:-8890}"
REMOTE_PORT="${RG_REMOTE_PORT:-8788}"

if lsof -nP -iTCP:"$LOCAL_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "本机 $LOCAL_PORT 已在监听（隧道可能已建立或端口被占用）"
  echo "打开 http://127.0.0.1:${LOCAL_PORT}/"
  exit 0
fi

ssh -fN \
  -L "${LOCAL_PORT}:127.0.0.1:${REMOTE_PORT}" \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -o ExitOnForwardFailure=yes \
  -o ConnectTimeout=8 \
  "$HOST"

echo "隧道已建立：http://127.0.0.1:${LOCAL_PORT}/"
echo "  ?source=live  真实运行库（交易时段逐日积累）"
echo "  ?source=demo  离线演示库（人工仿真，顶部有 DEMO 标识）"
