"""静态自检扫描器：禁止键鼠控制 / OCR / 券商下单相关代码。

只检查 tokenize 产出的 NAME 与 STRING 词法单元，跳过注释和文档字符串以外的说明性文字，
避免把「本系统不含下单逻辑」这类 SOP 表述误判为违规。
独立可执行入口：tools/static_scan.py
"""
from __future__ import annotations

import io
import json
import re
import tokenize
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
PATTERNS_PATH = ROOT / "tools" / "forbidden_patterns.json"


def load_patterns(path: str | Path | None = None) -> dict[str, Any]:
    return json.loads(Path(path or PATTERNS_PATH).read_text(encoding="utf-8"))


def scan_file(path: str | Path, patterns: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    cfg = patterns or load_patterns()
    source = Path(path).read_text(encoding="utf-8")
    hits: list[dict[str, Any]] = []
    name_pats = [(re.compile(p, re.I), p) for p in cfg["name_patterns"]]
    string_pats = [(re.compile(p, re.I), p) for p in cfg["string_patterns"]]
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        for tok in tokens:
            if tok.type == tokenize.NAME:
                for regex, raw in name_pats:
                    if regex.search(tok.string):
                        hits.append({"file": str(path), "line": tok.start[0],
                                     "kind": "NAME", "token": tok.string, "pattern": raw})
            elif tok.type == tokenize.STRING:
                for regex, raw in string_pats:
                    if regex.search(tok.string):
                        hits.append({"file": str(path), "line": tok.start[0],
                                     "kind": "STRING", "token": tok.string[:60], "pattern": raw})
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # 语法错误由编译检查负责，这里不阻断扫描
        pass
    return hits


def scan_tree(root: str | Path | None = None,
              patterns: dict[str, Any] | None = None) -> dict[str, Any]:
    base = Path(root or ROOT)
    cfg = patterns or load_patterns()
    excluded_dirs = set(cfg["exclude_dirs"]) | {"__pycache__", ".git"}
    excluded_files = set(cfg["exclude_files"])
    files: list[Path] = []
    for path in sorted(base.rglob("*.py")):
        rel = path.relative_to(base)
        if any(part in excluded_dirs for part in rel.parts):
            continue
        if path.name in excluded_files:
            continue
        files.append(path)
    violations: list[dict[str, Any]] = []
    for path in files:
        violations.extend(scan_file(path, cfg))
    return {"scanned": len(files), "files": [str(p) for p in files],
            "violations": violations, "ok": not violations}
