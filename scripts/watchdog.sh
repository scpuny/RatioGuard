#!/usr/bin/env bash
# 无人值守兜底：定时确认两个容器都在运行，异常时主动 docker-compose up -d。
# restart: always 覆盖进程意外退出；本脚本额外覆盖「手动/外部终止后
# daemon 挂起重启策略」这类边界，以及 compose 未及拉起的场景。
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs

need=0
for c in ratioguard-collector ratioguard-web; do
  status=$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null || echo missing)
  if [ "$status" != "running" ]; then
    echo "$(date '+%F %T') WARN $c status=$status" >> logs/watchdog.log
    need=1
  fi
done

if [ "$need" -eq 1 ]; then
  echo "$(date '+%F %T') RECOVER docker-compose up -d" >> logs/watchdog.log
  docker-compose up -d >> logs/watchdog.log 2>&1
  echo "$(date '+%F %T') RECOVER done" >> logs/watchdog.log
fi
