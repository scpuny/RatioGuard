"""告警推送（客观描述，不构成投资建议）。

硬规则：
  - 全部文案经 core.disclaimer.assert_compliant() 校验：完整免责 + 无诱导性话术
  - pause_trade_alert=True 时，信号类消息一律不发送，仅入库
  - 同类告警在 notify_dedup_sec 内去重
  - 放行依据只取最近一次 15:10 冻结快照，盘中不重算
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from ..core.audit import log_run, now_iso, parse_iso
from ..core.db import get_state, set_state
from ..core.disclaimer import DISCLAIMER, assert_compliant
from ..stats.snapshot import get_latest_frozen

KINDS = ("signal_qualified", "signal_insufficient", "drift_alert")
SIGNAL_KINDS = ("signal_qualified", "signal_insufficient")
TITLE = "WorkBuddy 半自动量化信号 · 观测记录"


def _f(value: Any, digits: int = 4) -> str:
    if value is None:
        return "-"
    if isinstance(value, str):
        return value
    return f"{float(value):.{digits}f}"


def render_signal_message(kind: str, ctx: dict[str, Any], cfg=None) -> str:
    """渲染单条推送 Markdown。末尾强制附完整版免责声明。"""
    if kind not in KINDS:
        raise ValueError(f"未知推送类型: {kind}")
    snap = ctx.get("snapshot") or {}
    if kind == "signal_qualified":
        lines = [
            "### 信号已记录 · 历史统计达标",
            "",
            f"- 信号时间：{ctx.get('signal_ts', '-')}",
            f"- 信号类型：{ctx.get('signal_type', '-')}",
            f"- 触发比值：{_f(ctx.get('ratio'), 6)}",
            f"- 引用快照：{snap.get('freeze_date', '-')} 15:10 冻结快照",
            f"- 历史样本：N={snap.get('n', '-')}，胜率={_f(snap.get('win_rate'))}，"
            f"95% CI=[{_f(snap.get('ci_low'))}, {_f(snap.get('ci_high'))}]，"
            f"含成本盈亏平衡胜率={_f(snap.get('break_even'))}",
            "- 说明：以上为历史样本的描述性统计，本信号尚未纳入统计样本。",
            "- 系统动作：已入库，等待结算闭环。",
        ]
    elif kind == "signal_insufficient":
        lines = [
            "### 信号已记录 · 置信不足，仅观测",
            "",
            f"- 信号时间：{ctx.get('signal_ts', '-')}",
            f"- 信号类型：{ctx.get('signal_type', '-')}",
            f"- 判定原因：{ctx.get('qualified_reason', 'insufficient_sample')}"
            "（样本量 / 交易日跨度 / 置信区间下限 / 漂移暂停）",
            "- 系统动作：仅入库记录，等待结算闭环，不推送交易类消息。",
        ]
    else:
        lines = [
            "### 策略分布漂移 · 暂停交易推送，继续收集样本",
            "",
            f"- 检验方式：互斥窗口两比例检验（W1 最近 {ctx.get('window_size', '-')} 笔，"
            f"W2 其前 {ctx.get('window_size', '-')} 笔）",
            f"- 检验结果：z={_f(ctx.get('z_stat'))}，临界值={_f(ctx.get('z_critical'))}，"
            f"效应量={_f(ctx.get('effect_size'))}，最小效应门槛={_f(ctx.get('min_effect_size'))}",
            f"- 窗口胜率：p1={_f(ctx.get('p1'))}（n1={ctx.get('n1', '-')}），"
            f"p2={_f(ctx.get('p2'))}（n2={ctx.get('n2', '-')}）",
            "- 系统动作：pause_trade_alert=True；之后所有新信号仅入库记录，不推送交易类消息。",
            "- 恢复方式：人工复核后执行 /resume，操作写入日志。",
        ]
    text = "\n".join(lines) + "\n\n" + DISCLAIMER
    assert_compliant(text)
    return text


def build_signed_url(webhook: str, secret: str,
                     timestamp: int | None = None) -> str:
    """钉钉加签。timestamp 可注入以便单测。"""
    if not secret:
        return webhook
    stamp = str(timestamp if timestamp is not None else round(time.time() * 1000))
    string_to_sign = f"{stamp}\n{secret}"
    code = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"),
                    hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(code))
    sep = "&" if "?" in webhook else "?"
    return f"{webhook}{sep}timestamp={stamp}&sign={sign}"


def _send_dingtalk(text: str, webhook: str, secret: str) -> tuple[bool, str]:
    url = build_signed_url(webhook, secret)
    payload = {"msgtype": "markdown",
               "markdown": {"title": TITLE, "text": text}}
    req = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        body = resp.read().decode("utf-8")
    return True, body[:200]


def _send_desktop(text: str, *_) -> tuple[bool, str]:
    """桌面通知（可选）。无 DISPLAY 时静默降级，不阻断主流程。"""
    import subprocess

    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return True, "no_display_skipped"
    subprocess.run(["notify-send", TITLE, text[:300]], timeout=5, check=False)
    return True, "desktop_notified"


def _recently_sent(conn, kind: str, dedup_sec: int,
                   now: datetime | None = None) -> bool:
    row = conn.execute(
        "SELECT ts FROM run_log WHERE module='notify' AND action='notify_sent' "
        "AND input_json LIKE ? ORDER BY id DESC LIMIT 1",
        (f'%"kind": "{kind}"%',),
    ).fetchone()
    if not row:
        return False
    try:
        last = parse_iso(row["ts"])
        reference = now or datetime.now(ZoneInfo("Asia/Shanghai"))
        return (reference - last).total_seconds() < dedup_sec
    except (ValueError, TypeError):
        return False


def dispatch(conn, cfg, kind: str, ctx: dict[str, Any] | None = None,
             sender: Callable[..., tuple[bool, str]] | None = None,
             now: datetime | None = None) -> dict[str, Any]:
    """按规则分发一条推送。返回执行结果，永不抛异常给主流程。"""
    if kind not in KINDS:
        raise ValueError(f"未知推送类型: {kind}")
    ctx = dict(ctx or {})
    stamp = now_iso(dt=now) if now else now_iso()

    if get_state(conn, "pause_trade_alert", "false") == "true" and kind in SIGNAL_KINDS:
        log_run(conn, "notify", "notify_suppressed",
                input_obj={"kind": kind}, output_obj={"reason": "paused"},
                message="漂移暂停中，信号类消息不发送，仅入库", ts=stamp)
        return {"sent": False, "reason": "paused"}

    if _recently_sent(conn, kind, int(cfg.get("notify_dedup_sec", 1800)), now):
        return {"sent": False, "reason": "dedup"}

    if "snapshot" not in ctx:
        ctx["snapshot"] = get_latest_frozen(conn) or {}
    text = render_signal_message(kind, ctx, cfg)

    channel = cfg.get("notify_channel", "dingtalk")
    webhook = (os.environ.get("QSIGNAL_DINGTALK_WEBHOOK")
               or cfg.get("dingtalk_webhook", ""))
    secret = (os.environ.get("QSIGNAL_DINGTALK_SECRET")
              or cfg.get("dingtalk_secret", ""))
    if not cfg.get("enable_notify", True) or channel == "none" \
            or (channel == "dingtalk" and not webhook):
        log_run(conn, "notify", "notify_dry_run",
                input_obj={"kind": kind, "channel": channel},
                message="未配置渠道，仅生成文案不发送", ts=stamp)
        return {"sent": False, "reason": "dry_run", "text": text}

    handler = sender or (_send_dingtalk if channel == "dingtalk" else _send_desktop)
    try:
        ok, detail = handler(text, webhook, secret)
    except Exception as exc:
        log_run(conn, "notify", "notify_failed",
                input_obj={"kind": kind}, output_obj={"error": str(exc)},
                level="WARN", ts=stamp)
        return {"sent": False, "reason": "send_failed", "error": str(exc),
                "text": text}
    log_run(conn, "notify", "notify_sent",
            input_obj={"kind": kind, "channel": channel},
            output_obj={"ok": ok, "detail": detail}, ts=stamp)
    return {"sent": bool(ok), "detail": detail, "text": text}


def pause_trade_alert(conn, reason: str = "drift_alert",
                      operator: str = "system") -> None:
    set_state(conn, "pause_trade_alert", "true", operator=operator, reason=reason)
    log_run(conn, "notify", "pause",
            input_obj={"reason": reason, "operator": operator},
            message="已暂停交易类消息推送")


def resume_trade_alert(conn, operator: str = "human",
                       reason: str = "人工复核后恢复") -> None:
    set_state(conn, "pause_trade_alert", "false",
              operator=operator, reason=reason)
    log_run(conn, "notify", "resume",
            input_obj={"operator": operator, "reason": reason},
            message="人工恢复交易类消息推送，操作已留痕")
