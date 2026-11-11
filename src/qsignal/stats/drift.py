"""漂移监控：互斥窗口两比例检验 + 最小效应门槛。

修复原方案「滚动窗口 vs 全量胜率」的子集包含缺陷：
  W1 = 最近 W 笔有效样本，W2 = 紧邻其前的 W 笔（与 W1 完全不重叠）。
  两窗口样本都不足 min_window_samples 时明确返回 insufficient，
  不返回「无漂移」，避免早期监控静默失效。

统计口径：合并方差两比例 z 检验，alpha 经 Bonferroni 校正
（alpha' = drift_alpha / drift_comparisons），并叠加效应量门槛，
避免大样本下微小差异触发假警报。
"""
from __future__ import annotations

from typing import Any

from ..core.audit import log_run, now_iso
from ..core.formulas import two_proportion_z
from ..core.wilson import z_critical
from .snapshot import load_samples


def _write(conn, cfg, result: dict[str, Any], stamp: str) -> None:
    conn.execute(
        "INSERT INTO drift_log"
        "(ts,config_hash,scope,status,window_size,n1,k1,p1,n2,k2,p2,"
        " z_stat,z_critical,effect_size,min_effect_size,drift_alert,action,note) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (stamp, cfg.config_hash, "mutual_window", result["status"],
         result["window_size"], result["n1"], result["k1"], result["p1"],
         result["n2"], result["k2"], result["p2"], result["z_stat"],
         result["z_critical"], result["effect_size"], result["min_effect_size"],
         int(result["drift_alert"]),
         "pause_trade_alert" if result["drift_alert"] else "none",
         result["note"]),
    )
    conn.commit()
    log_run(conn, "drift", "drift_check",
            input_obj={"config_hash": cfg.config_hash,
                       "window_size": result["window_size"],
                       "drift_alpha": cfg.get("drift_alpha", 0.05),
                       "drift_comparisons": cfg.get("drift_comparisons", 2),
                       "min_window_samples": cfg.get("min_window_samples", 60),
                       "min_effect_size": result["min_effect_size"]},
            output_obj=result,
            message="互斥窗口两比例检验（含输入参数，可复现）", ts=stamp)


def compute_drift(conn, cfg, window: int | None = None,
                  as_of: str | None = None,
                  samples: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """计算一次漂移检验。samples 可注入以便纯逻辑单测（不落库造数）。"""
    w = int(window or cfg["roll_window_1"])
    stamp = as_of or now_iso(tz=cfg.get("timezone", "Asia/Shanghai"))
    if samples is None:
        valid = [r for r in load_samples(conn, cfg)
                 if r["result"] in ("win", "loss")]
    else:
        valid = list(samples)

    alpha = float(cfg.get("drift_alpha", 0.05))
    comparisons = max(1, int(cfg.get("drift_comparisons", 2)))
    z_crit = z_critical(1.0 - alpha / comparisons)
    min_effect = float(cfg.get("min_effect_size", 0.10))
    min_window = int(cfg.get("min_window_samples", 60))

    w1 = valid[-w:]
    w2 = valid[-2 * w:-w]
    n1, n2 = len(w1), len(w2)
    base = {
        "ts": stamp, "config_hash": cfg.config_hash, "window_size": w,
        "n1": n1, "k1": None, "p1": None,
        "n2": n2, "k2": None, "p2": None,
        "z_stat": None, "z_critical": z_crit,
        "effect_size": None, "min_effect_size": min_effect,
        "drift_alert": False,
    }
    if n1 < min_window or n2 < min_window:
        result = {**base, "status": "insufficient",
                  "note": "互斥双窗口样本不足，跳过判定（不视为无漂移）"}
        _write(conn, cfg, result, stamp)
        return result

    k1 = sum(1 for r in w1 if r["result"] == "win")
    k2 = sum(1 for r in w2 if r["result"] == "win")
    test = two_proportion_z(k1, n1, k2, n2)
    significant = abs(test["z_stat"]) > z_crit
    large_effect = test["effect_size"] >= min_effect
    drift_alert = bool(significant and large_effect)
    note = ("z 显著且效应量达门槛" if drift_alert else
            "z 未超校正临界值" if not significant else
            "z 显著但效应量低于最小效应门槛，不告警")
    result = {
        **base,
        "status": "alert" if drift_alert else "ok",
        "k1": k1, "p1": test["p1"], "k2": k2, "p2": test["p2"],
        "z_stat": test["z_stat"], "effect_size": test["effect_size"],
        "drift_alert": drift_alert, "note": note,
    }
    _write(conn, cfg, result, stamp)
    return result


def run_drift_monitor(conn, cfg, as_of: str | None = None,
                      samples: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """日终复核入口：检验 + 告警 + 暂停开关 + 推送。samples 仅用于单测注入。"""
    from ..notify.notifier import dispatch, pause_trade_alert

    result = compute_drift(conn, cfg, as_of=as_of, samples=samples)
    if result["drift_alert"]:
        pause_trade_alert(conn, reason=f"mutual_window_z={result['z_stat']:.4f}")
        dispatch(conn, cfg, "drift_alert", {
            "window_size": result["window_size"],
            "z_stat": result["z_stat"], "z_critical": result["z_critical"],
            "effect_size": result["effect_size"],
            "min_effect_size": result["min_effect_size"],
            "n1": result["n1"], "k1": result["k1"], "p1": result["p1"],
            "n2": result["n2"], "k2": result["k2"], "p2": result["p2"],
        })
        log_run(conn, "drift", "drift_pause",
                input_obj={"z_stat": result["z_stat"]},
                message="漂移告警激活，交易类消息推送已暂停",
                ts=result["ts"])
    return result
