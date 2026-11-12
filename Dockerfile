# RatioGuard 运行镜像：L1 常驻采集服务与只读大屏共用同一份镜像，仅 command 不同。
# 依赖极简：业务只用 PyYAML 读 config.yaml；行情采集走 urllib 标准库，不装 akshare/pandas。
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# tzdata 是 slim 镜像里 zoneinfo("Asia/Shanghai") 的兜底数据源；
# 同时在 compose 中挂载宿主机 /etc/localtime，二者任一可用即可。
RUN pip install --no-cache-dir "PyYAML>=6.0" "tzdata>=2024.1"

COPY . /app
RUN mkdir -p /app/data /app/reports /app/logs

EXPOSE 8787
STOPSIGNAL SIGTERM

# 默认跑 L1 常驻服务；大屏服务在 docker-compose.yml 里覆盖 command。
CMD ["python", "qctl.py", "serve", "--check-market"]
