#!/usr/bin/env python3
"""SQLite 在线备份（WAL 安全），由宿主机 cron 通过 docker exec 调用。

用 sqlite3.Connection.backup() 而不是 cp，避免 WAL 未 checkpoint 时拿到不一致副本。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

DATA = Path("/app/data")
SRC = DATA / "qsignal.db"
OUT = DATA / "backups"
KEEP = 14


def main() -> int:
    if not SRC.exists():
        print(f"skip: {SRC} 不存在（服务尚未启动）")
        return 0
    OUT.mkdir(parents=True, exist_ok=True)
    day = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
    dst = OUT / f"qsignal-{day}.db"

    src = sqlite3.connect(f"file:{SRC}?mode=ro", uri=True)
    target = sqlite3.connect(dst)
    try:
        with target:
            src.backup(target)
    finally:
        src.close()
        target.close()

    for old in sorted(OUT.glob("qsignal-*.db"))[:-KEEP]:
        old.unlink()
    print(f"backup ok: {dst} ({dst.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
