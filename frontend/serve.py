#!/usr/bin/env python3
"""RatioGuard 前端大屏 · 轻量本地服务（仅标准库 + 项目自身模块）。

数据源
------
- LIVE：config.yaml 的 db_path -> data/qsignal.db（真实运行库，默认）
- DEMO：frontend/demo_qsignal.db（离线演示库，数据为人工生成）

切换方式（三选一，互不干扰）
  1. 页面 URL 加 ?source=live / ?source=demo —— 前端透传给 /api/state，
     **无需重启服务**即可即时切换数据源；
  2. 进程启动参数 --db <path> 或 --demo —— 决定无参数时的默认库；
  3. 环境变量 QSIGNAL_DB —— 部署时覆盖默认库路径。

/api/market 是只读真实行情探测：直接复用 L1 采集的 AkShareProvider 多源降级链
（akshare 东财全表 -> 东财单只 -> 腾讯 -> 新浪 -> 同花顺），纯标准库亦可工作、
零成本；不写 quote_table、不生成信号、不进入任何统计口径。

所有统计口径复用 src/qsignal/report/renderer.build_dashboard_payload，
本文件不重新实现任何判定逻辑；只读 SQLite，不触发任何交易动作。

运行：
    python frontend/serve.py [--host 127.0.0.1] [--port 8787] [--demo | --db PATH]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from qsignal.core.config import load_config          # noqa: E402
from qsignal.core.db import init_db                  # noqa: E402
from qsignal.market.collector import AkShareProvider  # noqa: E402
from qsignal.report.renderer import build_dashboard_payload  # noqa: E402

DEFAULT_PORT = 8787
DEMO_DB = HERE / "demo_qsignal.db"
TZ = "Asia/Shanghai"
MARKET_TTL = 20                 # 行情探测缓存秒数，避免前端 5s 刷新打爆外部接口
OVERRIDE_DB: str | None = None  # 由 --db / --demo / QSIGNAL_DB 指定的进程级默认库
_market_cache: dict = {"at": 0.0, "payload": None}

TABLES = ("quote_table", "signal_table", "snapshot_table",
          "drift_log", "run_log", "system_state")


def _now() -> str:
    return datetime.now(ZoneInfo(TZ)).isoformat(timespec="seconds")


def _resolve_db(source: str | None):
    """返回 (cfg, db_path: Path, source_label)。URL 参数优先于进程默认。"""
    cfg = load_config()
    if source == "demo":
        return cfg, DEMO_DB, "DEMO"
    if source == "live":
        return cfg, cfg.resolve(cfg["db_path"]), "LIVE"
    default = OVERRIDE_DB or os.environ.get("QSIGNAL_DB")
    if default:
        path = Path(default)
        if not path.is_absolute():
            path = (PROJECT_ROOT / path).resolve()
        return cfg, path, "DEMO" if "demo" in path.name.lower() else "LIVE"
    return cfg, cfg.resolve(cfg["db_path"]), "LIVE"


def _table_counts(conn) -> dict:
    out = {}
    for name in TABLES:
        try:
            out[name] = conn.execute(f"SELECT COUNT(*) c FROM {name}").fetchone()["c"]
        except Exception:
            out[name] = None
    return out


def build_state(source: str | None = None) -> dict:
    """聚合大屏所需全部数据：核心 payload + 三张明细表 + 数据源元信息。"""
    cfg, db_path, label = _resolve_db(source)
    conn = init_db(db_path)
    try:
        dashboard = build_dashboard_payload(conn, cfg)
        signals = [
            dict(r) for r in conn.execute(
                "SELECT id,signal_ts,signal_type,status,result,ambiguity,"
                "config_hash,ratio,tp,sl "
                "FROM signal_table ORDER BY id DESC LIMIT 40"
            ).fetchall()
        ]
        drift = [
            dict(r) for r in conn.execute(
                "SELECT ts,status,z_stat,z_critical,effect_size,drift_alert,"
                "p1,p2,n1,n2 "
                "FROM drift_log ORDER BY id DESC LIMIT 25"
            ).fetchall()
        ]
        runlog = [
            dict(r) for r in conn.execute(
                "SELECT ts,module,action,level,message "
                "FROM run_log ORDER BY id DESC LIMIT 40"
            ).fetchall()
        ]
        counts = _table_counts(conn)
        last_quote = conn.execute(
            "SELECT MAX(quote_ts) t FROM quote_table"
        ).fetchone()["t"]
        stat = db_path.stat() if db_path.exists() else None
        meta = {
            "source": label,
            "db_path": str(db_path),
            "db_exists": bool(stat),
            "db_bytes": stat.st_size if stat else 0,
            "db_mtime": (datetime.fromtimestamp(stat.st_mtime, ZoneInfo(TZ))
                         .isoformat(timespec="seconds") if stat else None),
            "counts": counts,
            "last_quote_ts": last_quote,
            "server_time": _now(),
            "note": ("离线演示库：数据为人工生成，仅用于界面演示，不代表真实策略表现。"
                     if label == "DEMO" else
                     "真实运行库：数据由 L1 常驻服务在交易时段实际采集/结算产生；"
                     "样本需逐日积累，统计快照每日 15:10 仅冻结一次。"),
        }
        return {
            "dashboard": dashboard,
            "signals": signals,
            "drift": drift,
            "runlog": runlog,
            "meta": meta,
        }
    except Exception as exc:  # 数据库/口径异常时返回空壳，前端优雅降级
        return {
            "dashboard": None,
            "error": f"{type(exc).__name__}: {exc}",
            "signals": [],
            "drift": [],
            "runlog": [],
            "meta": {"source": label, "db_path": str(db_path), "error": str(exc)},
        }
    finally:
        conn.close()


# ---------- 真实行情只读探测 ----------

def probe_market() -> dict:
    """复用 L1 采集链路的 AkShareProvider 多源降级链，探测真实最新报价。

    与正式采集用同一个 provider，结果即「此刻 L1 能否拿到行情」的真实反映。
    明确边界：不写库、不生成信号、不参与任何统计/放行判定。盘后报价时间戳
    已过期，正式采集会被 validate_quote 以 stale_quote 丢弃，属预期行为。
    """
    now = time.monotonic()
    if _market_cache["payload"] and now - _market_cache["at"] < MARKET_TTL:
        return _market_cache["payload"]
    cfg = load_config()
    out = {
        "ok": False,
        "source": None,
        "fetched_at": _now(),
        "note": "只读行情探测：与 L1 采集同一数据源降级链，但不写 quote_table、"
                "不生成信号、不进入任何统计口径；非交易时段返回最近一次行情快照。",
    }
    try:
        q = AkShareProvider().fetch(cfg)
        age = ((datetime.now(ZoneInfo(TZ)) - datetime.fromisoformat(q.quote_ts))
               .total_seconds() if q.quote_ts else None)
        max_age = float(cfg.get("max_quote_age_sec", 60))
        out.update({
            "ok": True,
            "source": q.source,
            "symbol_a": q.code_a, "price_a": q.price_a,
            "ts_a": q.quote_ts_a or q.quote_ts,
            "symbol_b": q.code_b, "price_b": q.price_b,
            "ts_b": q.quote_ts_b or q.quote_ts,
            "quote_ts": q.quote_ts,
            "age_sec": round(age, 1) if age is not None else None,
            "fresh": bool(age is not None and age <= max_age),
            "max_quote_age_sec": max_age,
            "ratio": q.ratio,
            "base_ratio": float(cfg["base_ratio"]),
        })
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    _market_cache["at"] = now
    _market_cache["payload"] = out
    return out


class Handler(BaseHTTPRequestHandler):
    def _serve_file(self, path: Path, content_type: str) -> None:
        if not path.exists():
            self.send_error(404, "Not Found")
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _serve_json(self, obj: dict) -> None:
        data = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path
        query = parse_qs(parsed.query)
        source = (query.get("source") or [None])[0]
        if route in ("/", "/index.html"):
            self._serve_file(HERE / "index.html", "text/html; charset=utf-8")
        elif route == "/styles.css":
            self._serve_file(HERE / "styles.css", "text/css; charset=utf-8")
        elif route == "/app.js":
            self._serve_file(HERE / "app.js", "application/javascript; charset=utf-8")
        elif route == "/api/state":
            self._serve_json(build_state(source))
        elif route == "/api/market":
            self._serve_json(probe_market())
        else:
            self.send_error(404, "Not Found")

    def log_message(self, *args) -> None:  # 静默访问日志
        return


def main() -> None:
    ap = argparse.ArgumentParser(description="RatioGuard 前端大屏本地服务")
    ap.add_argument("--host", default="127.0.0.1", help="绑定地址（默认 127.0.0.1）")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="端口（默认 8787）")
    ap.add_argument("--db", default=None, help="进程默认数据库路径")
    ap.add_argument("--demo", action="store_true", help="进程默认使用离线演示库")
    args = ap.parse_args()

    global OVERRIDE_DB
    if args.demo:
        OVERRIDE_DB = str(DEMO_DB)
    elif args.db:
        OVERRIDE_DB = args.db

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    default_label = "LIVE 真实库" if (OVERRIDE_DB is None and not os.environ.get("QSIGNAL_DB")) \
        else ("DEMO 演示库" if (OVERRIDE_DB and "demo" in OVERRIDE_DB.lower()) else "指定数据库")
    print(f"RatioGuard 大屏已启动: http://{args.host}:{args.port}  (默认 {default_label})")
    print("页面可加 ?source=live / ?source=demo 即时切换，无需重启。")
    print("按 Ctrl-C 退出。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
