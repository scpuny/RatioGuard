#!/usr/bin/env python3
"""独立静态自检入口：python tools/static_scan.py [--root PATH]

扫描生产代码，禁止键鼠控制 / OCR / 券商下单相关标识符与交易端点。
退出码 0 = 零违规；1 = 存在违规。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qsignal.core.static_scan import scan_tree  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="静态自检：禁止键鼠/OCR/下单代码")
    parser.add_argument("--root", default=str(ROOT), help="扫描根目录")
    args = parser.parse_args()
    result = scan_tree(args.root)
    print(json.dumps({
        "ok": result["ok"],
        "scanned": result["scanned"],
        "violations": result["violations"],
    }, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
