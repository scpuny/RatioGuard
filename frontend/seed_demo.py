#!/usr/bin/env python3
"""生成独立的演示数据库（不改写真实 data/qsignal.db）。

用项目已验证的模块写入一批仿真信号，并触发 15:10 冻结 + 漂移复核，
使大屏能展示「统计达标 / 三档快照 / 漂移正常 / 信号清单」等填充态。
仅用于界面演示，与真实交易数据无关。

运行：python frontend/seed_demo.py
"""
from __future__ import annotations

import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))

from qsignal.core.config import load_config      # noqa: E402
from qsignal.core.db import init_db, set_state   # noqa: E402
from qsignal.core.audit import log_run           # noqa: E402
from qsignal.core.wilson import z_critical       # noqa: E402
from qsignal.stats.snapshot import freeze_daily_snapshot  # noqa: E402
from qsignal.stats.drift import run_drift_monitor        # noqa: E402

TZ = timezone(timedelta(hours=8))
random.seed(20261111)
cfg = load_config()

demo = HERE / "demo_qsignal.db"
if demo.exists():
    demo.unlink()
conn = init_db(str(demo))

# 取最近 30 个交易日（从 2026-10-13 往前/往后铺满）
days = []
d = datetime(2026, 10, 13, tzinfo=TZ)
while len(days) < 30:
    if d.weekday() < 5:
        days.append(d)
    d += timedelta(days=1)

quote_id = 0
sid = 0
used_quote_ts = set()
for idx, day in enumerate(days):
    # 近期窗口胜率明显高于前期，使漂移 z 非零但仍处「正常」区间，便于直观展示仪表
    win_p = 0.80 + 0.004 * idx
    for _ in range(random.randint(7, 11)):
        # 用递增序号消除时间戳碰撞（真实行情天然唯一），且保持 quote_ts <= signal_ts
        st = day.replace(hour=random.choice([10, 11, 14, 15]),
                         minute=random.randint(0, 59), second=random.randint(0, 59)) \
            + timedelta(seconds=quote_id % 540)
        qt = st - timedelta(seconds=1 + (quote_id % 50))
        while qt.isoformat(timespec="seconds") in used_quote_ts:
            qt -= timedelta(seconds=1)
        used_quote_ts.add(qt.isoformat(timespec="seconds"))
        sig_iso = st.isoformat(timespec="seconds")
        quote_iso = qt.isoformat(timespec="seconds")
        quote_id += 1
        sid += 1
        typ = random.choice(["spread_long", "spread_short"])
        ratio = 1.005 + random.uniform(-0.012, 0.012)
        r = random.random()
        if r < 0.05:
            result, amb = "hold", 0
        elif r < 0.05 + win_p:
            result, amb = "win", (1 if random.random() < 0.06 else 0)
        else:
            result, amb = "loss", 0
        # 行情（满足无前视审计：quote_ts <= signal_ts 且有效）
        conn.execute(
            "INSERT INTO quote_table(quote_id,quote_ts,ingest_ts,source,code_a,price_a,"
            "code_b,price_b,ratio,is_valid) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (quote_id, quote_iso, quote_iso, "primary", "510300",
             round(random.uniform(3.9, 4.1), 3), "159919",
             round(random.uniform(1.9, 2.1), 3), round(ratio, 6), 1))
        tp = ratio - 0.002 if typ == "spread_long" else ratio + 0.002
        sl = ratio + 0.004 if typ == "spread_long" else ratio - 0.004
        conn.execute(
            "INSERT INTO signal_table(signal_ts,signal_type,price_a,price_b,ratio,base_ratio,"
            "tp,sl,status,settle_ts,result,result_ratio,quote_id,config_hash,"
            "strategy_version,ambiguity,note) VALUES(?,?,?,?,?,?,?,?,'closed',?,?,?,?,?,?,?,'demo')",
            (sig_iso, typ, 4.0, 2.0, round(ratio, 6), 1.005, round(tp, 6), round(sl, 6),
             sig_iso, result, round(ratio, 6), quote_id, cfg.config_hash,
             cfg.strategy_version, amb))
conn.commit()

last_day = days[-1]
freeze_daily_snapshot(conn, cfg, now=last_day.replace(hour=15, minute=11), created_by="eod")
run_drift_monitor(conn, cfg, as_of=last_day.replace(hour=15, minute=12))
set_state(conn, "eod_done_date", last_day.date().isoformat(), operator="system", reason="demo")

# 历史漂移检验记录（演示用，非实时逐笔计算）：最近若干交易日各一条，
# z 在 [-1.8, 1.8] 波动、偶发越界告警，使「漂移检验记录」面板有完整历史。
random.seed(20261111)
zc = z_critical(1.0 - float(cfg.get("drift_alpha", 0.05)) / int(cfg.get("drift_comparisons", 2)))
hist_days = days[-26:-1]  # 排除已写入真实结果的 last_day
random.seed(424242)
for d in hist_days:
    z = round(random.uniform(-2.8, 2.8), 3)
    effect = round(abs(z) * 0.08, 4)
    alert = (abs(z) > zc) and (effect >= 0.10)
    status = "alert" if alert else "ok"
    p1 = round(random.uniform(0.80, 0.93), 4)
    p2 = round(min(0.985, max(0.72, p1 + z * 0.02)), 4)
    n1, n2 = random.randint(82, 100), random.randint(82, 100)
    k1, k2 = int(p1 * n1), int(p2 * n2)
    conn.execute(
        "INSERT INTO drift_log(ts,config_hash,scope,status,window_size,n1,k1,p1,n2,k2,p2,"
        "z_stat,z_critical,effect_size,min_effect_size,drift_alert,action,note) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (d.replace(hour=15, minute=12).isoformat(timespec="seconds"), cfg.config_hash,
         "mutual_window", status, int(cfg.get("roll_window_1", 100)),
         n1, k1, p1, n2, k2, p2, z, round(zc, 4), effect, 0.10,
         int(alert), "pause_trade_alert" if alert else "none",
         "z 显著且效应量达门槛" if alert else ("z 显著但效应量低于门槛" if abs(z) > zc else "z 未超校正临界值")))

# 跨日运行日志（演示）：每个交易日若干条，使「运行 / 审计日志」面板有跨日历史
for d in days:
    for _ in range(random.randint(3, 6)):
        hh = random.choice([9, 10, 11, 13, 14])
        mm = random.randint(0, 59)
        log_run(conn, "collector", "tick", input_obj={}, output_obj={},
                message=f"行情采集成功 510300/159919 ratio={round(1.0+random.uniform(-0.02,0.02),4)}",
                ts=d.replace(hour=hh, minute=mm, second=random.randint(0, 59)).isoformat(timespec="seconds"))

for i in range(18):
    log_run(conn, "service", "tick", input_obj={"demo": i}, output_obj={},
            message=f"demo run log entry #{i}",
            ts=(last_day.replace(hour=14, minute=0) + timedelta(minutes=i)).isoformat(timespec="seconds"))
conn.commit()
conn.close()
print(f"演示库已生成: {demo}  | 信号 {sid} 条 / 行情 {quote_id} 条")
