"""Markdown 复盘报表、CSV 备份、动态大屏 JSON 指标输出。

大屏新增口径（规则 10）：歧义样本占比、hold 率、互斥窗口检验 z 值、配置版本标记。
所有报表与 JSON 强制携带完整版免责声明。
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from ..core.audit import log_run, now_iso
from ..core.db import get_state
from ..core.disclaimer import DISCLAIMER, assert_compliant
from ..stats.snapshot import SCOPES, compute_snapshot, get_latest_frozen

CSV_FIELDS = (
    "id", "signal_ts", "signal_type", "price_a", "price_b", "ratio",
    "base_ratio", "tp", "sl", "status", "settle_ts", "result", "result_ratio",
    "quote_id", "config_hash", "strategy_version", "ambiguity", "note",
)


def _f(value: Any, digits: int = 4) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _latest_drift(conn) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM drift_log WHERE status!='config_change' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else {}


def _snapshots(conn, cfg, stamp: str) -> dict[str, dict[str, Any]]:
    return {scope: compute_snapshot(conn, cfg, scope, as_of=stamp).to_dict()
            for scope in SCOPES}


def render_markdown_report(conn, cfg, as_of: str | None = None) -> str:
    """生成完整复盘 Markdown 报表（含三张快照对比、漂移记录、质量指标、信号清单）。"""
    stamp = as_of or now_iso(tz=cfg.get("timezone", "Asia/Shanghai"))
    snaps = _snapshots(conn, cfg, stamp)
    full = snaps["full"]
    drift = _latest_drift(conn)
    frozen = get_latest_frozen(conn)
    amb_warn = full["ambiguity_rate"] > float(cfg.get("ambiguity_warn_ratio", 0.10))
    hold_warn = full["hold_rate"] > float(cfg.get("hold_rate_warn", 0.40))

    lines = [
        "# 半自动量化信号系统 · 复盘报表",
        "",
        f"- 生成时间：{stamp}",
        f"- 配置版本标记：`{cfg.strategy_version}@{cfg.config_hash}`",
        f"- 冻结快照：{frozen['freeze_date'] if frozen else '尚未冻结'} "
        f"（每日 15:10 唯一一次，盘中不刷新）",
        f"- 数据范围：仅已收集样本的历史快照，不预测未来",
        "",
        "## 一、样本概览",
        "",
        f"- 有效样本 N = {full['n']}（win {full['win_cnt']} / loss {full['loss_cnt']}）",
        f"- hold 样本 = {full['hold_cnt']}（不计入胜率与置信区间，单独归档）",
        f"- 有效交易日跨度 = {full['effective_sessions']}",
        f"- 歧义样本占比 = {_f(full['ambiguity_rate'])} "
        f"（{full['ambiguous_cnt']}/{full['n']}，告警线 "
        f"{_f(cfg.get('ambiguity_warn_ratio', 0.10))}，"
        f"{'已超限' if amb_warn else '未超限'}）",
        f"- hold 率 = {_f(full['hold_rate'])} "
        f"（告警线 {_f(cfg.get('hold_rate_warn', 0.40))}，"
        f"{'已超限' if hold_warn else '未超限'}）",
        "",
        "> 歧义样本占比是胜率高估的上界提示；hold 率持续偏高说明信号钝化。",
        "",
        "## 二、三张快照对比",
        "",
        "| scope | N | win | loss | hold | 胜率 | 盈亏平衡 | CI 下限 | CI 上限 | qualified | 判定原因 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | :---: | --- |",
    ]
    for scope in SCOPES:
        s = snaps[scope]
        lines.append(
            f"| {scope} | {s['n']} | {s['win_cnt']} | {s['loss_cnt']} | "
            f"{s['hold_cnt']} | {_f(s['win_rate'])} | {_f(s['break_even'])} | "
            f"{_f(s['ci_low'])} | {_f(s['ci_high'])} | "
            f"{'是' if s['qualified'] else '否'} | {s['qualified_reason']} |"
        )
    lines += [
        "",
        f"> 盈亏平衡胜率含双边成本 cost={_f(cfg.get('cost_per_unit', 0), 6)}；"
        "Wilson 区间为描述性统计，样本非 i.i.d. 时区间偏窄。",
        "",
        "## 三、漂移监控（互斥窗口两比例检验）",
        "",
        "| 状态 | 窗口 | z 值 | 校正临界值 | 效应量 | 最小效应门槛 | W1(n1/k1/p1) | W2(n2/k2/p2) | 告警 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- | --- | :---: |",
        "| {status} | {w} | {z} | {zc} | {eff} | {min_eff} | {w1} | {w2} | {alert} |".format(
            status=drift.get("status", "insufficient"),
            w=_f(drift.get("window_size")),
            z=_f(drift.get("z_stat")), zc=_f(drift.get("z_critical")),
            eff=_f(drift.get("effect_size")),
            min_eff=_f(drift.get("min_effect_size")),
            w1=f"{drift.get('n1', '-')}/{drift.get('k1', '-')}/{_f(drift.get('p1'))}",
            w2=f"{drift.get('n2', '-')}/{drift.get('k2', '-')}/{_f(drift.get('p2'))}",
            alert="是" if drift.get("drift_alert") else "否",
        ),
        "",
        "## 四、最近信号清单（最多 20 条）",
        "",
        "| id | 信号时间 | 类型 | 状态 | 结果 | 歧义 | 配置版本 |",
        "| ---: | --- | --- | --- | --- | :---: | --- |",
    ]
    rows = conn.execute(
        "SELECT id,signal_ts,signal_type,status,result,ambiguity,config_hash "
        "FROM signal_table ORDER BY id DESC LIMIT 20"
    ).fetchall()
    if not rows:
        lines.append("| - | - | - | - | - | - | - |")
    for r in rows:
        lines.append(
            f"| {r['id']} | {r['signal_ts']} | {r['signal_type']} | "
            f"{r['status']} | {r['result'] or '-'} | {r['ambiguity']} | "
            f"{r['config_hash']} |"
        )
    lines += [
        "",
        "## 五、系统状态",
        "",
        f"- pause_trade_alert = "
        f"{'true' if get_state(conn, 'pause_trade_alert', 'false') == 'true' else 'false'}",
        f"- 冻结快照 qualified = "
        f"{'是' if frozen and frozen['qualified'] else '否'}",
        f"- 统计口径：按 config_hash 分组，默认不跨配置版本合并；"
        f"参数变更记录于 run_log / drift_log，不误判为漂移。",
    ]
    text = "\n".join(lines) + "\n\n" + DISCLAIMER
    assert_compliant(text)
    log_run(conn, "report", "markdown_report",
            input_obj={"config_hash": cfg.config_hash, "as_of": stamp},
            output_obj={"length": len(text)}, ts=stamp)
    return text


def build_dashboard_payload(conn, cfg, as_of: str | None = None) -> dict[str, Any]:
    """动态大屏 JSON 指标输出结构。新增四项口径见 quality / drift / config_version_tag。"""
    stamp = as_of or now_iso(tz=cfg.get("timezone", "Asia/Shanghai"))
    snaps = _snapshots(conn, cfg, stamp)
    full = snaps["full"]
    drift = _latest_drift(conn)
    frozen = get_latest_frozen(conn)
    system_snapshot = frozen or full
    return {
        "schema_version": "1.0",
        "generated_at": stamp,
        "as_of_snapshot": frozen["snap_ts"] if frozen else None,
        "config_hash": cfg.config_hash,
        "strategy_version": cfg.strategy_version,
        "config_version_tag": f"{cfg.strategy_version}@{cfg.config_hash}",
        "system": {
            "pause_trade_alert": get_state(conn, "pause_trade_alert", "false") == "true",
            "qualified": bool(system_snapshot["qualified"]),
            "qualified_reason": system_snapshot["qualified_reason"],
            "qualified_source": "frozen" if frozen else "live",
        },
        "snapshots": snaps,
        "quality": {
            "ambiguity_rate": full["ambiguity_rate"],
            "ambiguous_cnt": full["ambiguous_cnt"],
            "hold_rate": full["hold_rate"],
            "hold_cnt": full["hold_cnt"],
            "effective_sessions": full["effective_sessions"],
            "ambiguity_warn": full["ambiguity_rate"] > float(cfg.get("ambiguity_warn_ratio", 0.10)),
            "hold_rate_warn": full["hold_rate"] > float(cfg.get("hold_rate_warn", 0.40)),
        },
        "drift": {
            "status": drift.get("status", "insufficient"),
            "window_size": drift.get("window_size"),
            "n1": drift.get("n1"), "k1": drift.get("k1"), "p1": drift.get("p1"),
            "n2": drift.get("n2"), "k2": drift.get("k2"), "p2": drift.get("p2"),
            "z_stat": drift.get("z_stat"),
            "z_critical": drift.get("z_critical"),
            "effect_size": drift.get("effect_size"),
            "min_effect_size": drift.get("min_effect_size"),
            "drift_alert": bool(drift.get("drift_alert", 0)),
        },
        "disclaimer": DISCLAIMER,
    }


def write_dashboard(conn, cfg, path: str | Path,
                    as_of: str | None = None) -> dict[str, Any]:
    """输出大屏 JSON 文件并返回同一 payload。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = build_dashboard_payload(conn, cfg, as_of)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    log_run(conn, "report", "dashboard_json",
            input_obj={"path": str(target), "config_hash": cfg.config_hash},
            output_obj={"bytes": target.stat().st_size},
            ts=payload["generated_at"])
    return payload


def export_csv(conn, path: str | Path) -> int:
    """持久化 signal_table 全量 CSV 备份，返回导出行数。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    rows = conn.execute("SELECT * FROM signal_table ORDER BY id").fetchall()
    with target.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(CSV_FIELDS))
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row[key] for key in CSV_FIELDS})
    log_run(conn, "report", "csv_backup",
            input_obj={"path": str(target)},
            output_obj={"rows": len(rows)},
            message="CSV 离线备份")
    return len(rows)
