"""M2 验收：结算状态机、保守路径、收盘 hold、统计快照与 15:10 冻结。

验收标准（方案 M2）：
  - 分钟 K 线 high/low 判定止盈止损；同区间双触保守判 loss 且 ambiguity=1
  - 多分钟按时间顺序判定（先触损即 loss）
  - 15:00 收盘未触达判 hold，hold 不计入胜率
  - 结算幂等，且结算事件不写 snapshot_table（盘中不刷新放行依据）
  - 快照三 scope、含成本 break_even、qualified 判定顺序、按 config_hash 分组
  - 每日 15:10 冻结一次且同日幂等
"""
from __future__ import annotations

import unittest
from datetime import datetime

from base import (
    TempDBMixin, insert_quote, insert_signal, local_ts, make_cfg, seed_samples,
)

from qsignal.core.db import set_state
from qsignal.settle.settler import Bar, build_bars, evaluate, settle_open_signals
from qsignal.stats.snapshot import (
    SCOPES, compute_snapshot, freeze_daily_snapshot, get_latest_frozen,
)

DAY = "2026-11-11"


def moment(hhmm: str) -> datetime:
    return datetime.fromisoformat(f"{DAY}T{hhmm}:00+08:00")


def ts(hh: int, mm: int, ss: int = 0) -> str:
    return f"{DAY}T{hh:02d}:{mm:02d}:{ss:02d}+08:00"


LONG = {"signal_type": "spread_long", "tp": 1.008, "sl": 1.014}
SHORT = {"signal_type": "spread_short", "tp": 1.002, "sl": 0.996}


class TestEvaluate(unittest.TestCase):
    def test_take_profit(self):
        r = evaluate(LONG, Bar(ts(10, 1), 1.009, 1.007, 1.0075))
        self.assertEqual((r.action, r.result, r.ambiguity), ("close", "win", 0))

    def test_stop_loss(self):
        r = evaluate(LONG, Bar(ts(10, 1), 1.015, 1.009, 1.014))
        self.assertEqual((r.action, r.result, r.ambiguity), ("close", "loss", 0))

    def test_ambiguous_path_is_conservative_loss(self):
        r = evaluate(LONG, Bar(ts(10, 1), 1.015, 1.007, 1.010))
        self.assertEqual((r.action, r.result, r.ambiguity), ("close", "loss", 1))
        self.assertEqual(r.reason, "ambiguous_path_both_hit")

    def test_no_touch_keeps_open(self):
        r = evaluate(LONG, Bar(ts(10, 1), 1.010, 1.009, 1.0095))
        self.assertEqual(r.action, "keep_open")

    def test_short_direction(self):
        self.assertEqual(evaluate(SHORT, Bar(ts(10, 1), 1.003, 1.001, 1.002)).result,
                         "win")
        self.assertEqual(evaluate(SHORT, Bar(ts(10, 1), 0.997, 0.995, 0.996)).result,
                         "loss")


class TestBuildBars(unittest.TestCase):
    def test_minute_aggregation(self):
        rows = [
            {"quote_ts": ts(10, 1, 5), "ratio": 1.009},
            {"quote_ts": ts(10, 1, 30), "ratio": 1.007},
            {"quote_ts": ts(10, 2, 10), "ratio": 1.012},
        ]
        bars = build_bars(rows)
        self.assertEqual(len(bars), 2)
        self.assertEqual((bars[0].ratio_high, bars[0].ratio_low,
                          bars[0].ratio_close), (1.009, 1.007, 1.007))
        self.assertEqual(bars[1].ratio_high, 1.012)
        self.assertLess(bars[0].bar_ts, bars[1].bar_ts)


class TestSettleOpenSignals(TempDBMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.cfg = make_cfg()

    def _open(self, signal_ts: str, **kw) -> int:
        params = dict(signal_type="spread_long", result=None, status="open",
                      config_hash=self.cfg.config_hash, ratio=1.010,
                      tp=1.008, sl=1.014)
        params.update(kw)
        return insert_signal(self.conn, signal_ts, **params)

    def _quotes(self, *pairs) -> None:
        for hh, mm, ss, ratio in pairs:
            insert_quote(self.conn, ts(hh, mm, ss),
                         price_a=ratio * 4.0, price_b=4.0)

    def test_take_profit_closes_win(self):
        self._open(ts(10, 0))
        self._quotes((10, 1, 5, 1.009), (10, 1, 30, 1.007))
        result = settle_open_signals(self.conn, self.cfg, moment("10:02"))
        self.assertEqual(result["closed"], 1)
        row = self.conn.execute("SELECT status,result,ambiguity FROM signal_table").fetchone()
        self.assertEqual((row["status"], row["result"], row["ambiguity"]),
                         ("closed", "win", 0))

    def test_ambiguous_bar_marks_ambiguity(self):
        self._open(ts(10, 0))
        self._quotes((10, 1, 5, 1.015), (10, 1, 30, 1.007))
        settle_open_signals(self.conn, self.cfg, moment("10:02"))
        row = self.conn.execute("SELECT result,ambiguity,note FROM signal_table").fetchone()
        self.assertEqual((row["result"], row["ambiguity"]), ("loss", 1))
        self.assertEqual(row["note"], "ambiguous_path_both_hit")

    def test_bar_order_wins(self):
        self._open(ts(10, 0))
        self._quotes((10, 1, 5, 1.015), (10, 2, 5, 1.007))
        settle_open_signals(self.conn, self.cfg, moment("10:03"))
        row = self.conn.execute("SELECT result FROM signal_table").fetchone()
        self.assertEqual(row["result"], "loss", "第一分钟已触损，后续触盈不得改写")

    def test_eod_hold_and_hold_excluded_from_stats(self):
        self._open(ts(10, 0))
        self._quotes((10, 1, 5, 1.010))
        result = settle_open_signals(self.conn, self.cfg, moment("15:00"))
        self.assertTrue(result["eod"])
        row = self.conn.execute("SELECT result,ambiguity FROM signal_table").fetchone()
        self.assertEqual((row["result"], row["ambiguity"]), ("hold", 0))
        snap = compute_snapshot(self.conn, self.cfg, "full")
        self.assertEqual(snap.n, 0)
        self.assertEqual(snap.hold_cnt, 1)

    def test_idempotent_and_no_snapshot_refresh(self):
        self._open(ts(10, 0))
        self._quotes((10, 1, 5, 1.009), (10, 1, 30, 1.007))
        settle_open_signals(self.conn, self.cfg, moment("10:02"))
        again = settle_open_signals(self.conn, self.cfg, moment("10:03"))
        self.assertEqual(again["closed"], 0)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) c FROM snapshot_table").fetchone()["c"], 0,
            "结算事件不得刷新统计快照")
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) c FROM run_log WHERE action='signal_closed'"
        ).fetchone()["c"], 1)


class TestSnapshot(TempDBMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.cfg = make_cfg()

    def _seed(self, wins, losses, holds=0, spread_days=20, **kw):
        seed_samples(self.conn, wins, losses, holds,
                     config_hash=self.cfg.config_hash,
                     spread_days=spread_days, **kw)

    def test_qualified_snapshot(self):
        self._seed(135, 15, holds=10, shuffle=True)
        snap = compute_snapshot(self.conn, self.cfg, "full")
        self.assertEqual(snap.n, 150)
        self.assertEqual(snap.win_cnt, 135)
        self.assertEqual(snap.hold_cnt, 10)
        self.assertAlmostEqual(snap.break_even, 0.75, places=9)
        self.assertTrue(snap.qualified, snap.qualified_reason)
        self.assertEqual(snap.qualified_reason, "qualified")
        self.assertGreater(snap.ci_low, snap.break_even)
        self.assertGreater(snap.hold_rate, 0)
        self.assertEqual(snap.effective_sessions, 20)

    def test_hold_excluded_from_win_rate(self):
        self._seed(60, 20, holds=30, shuffle=True)
        snap = compute_snapshot(self.conn, self.cfg, "full")
        self.assertEqual(snap.n, 80)
        self.assertAlmostEqual(snap.win_rate, 0.75)
        self.assertEqual(snap.hold_cnt, 30)

    def test_insufficient_sample(self):
        self._seed(50, 20)
        snap = compute_snapshot(self.conn, self.cfg, "full")
        self.assertFalse(snap.qualified)
        self.assertEqual(snap.qualified_reason, "insufficient_sample")

    def test_insufficient_sessions(self):
        self._seed(135, 15, spread_days=3)
        snap = compute_snapshot(self.conn, self.cfg, "full")
        self.assertEqual(snap.qualified_reason, "insufficient_sessions")

    def test_drift_paused(self):
        self._seed(135, 15)
        set_state(self.conn, "pause_trade_alert", "true", reason="test")
        snap = compute_snapshot(self.conn, self.cfg, "full")
        self.assertEqual(snap.qualified_reason, "drift_paused")

    def test_ci_below_break_even(self):
        self._seed(100, 50)
        snap = compute_snapshot(self.conn, self.cfg, "full")
        self.assertEqual(snap.qualified_reason, "ci_low_below_break_even")

    def test_grouped_by_config_hash(self):
        self._seed(5, 5)
        seed_samples(self.conn, 50, 0, config_hash="other_config",
                     spread_days=1)
        snap = compute_snapshot(self.conn, self.cfg, "full")
        self.assertEqual(snap.n, 10, "默认不跨配置版本合并样本")

    def test_freeze_is_daily_and_idempotent(self):
        self._seed(135, 15)
        self.assertIsNone(get_latest_frozen(self.conn))
        first = freeze_daily_snapshot(self.conn, self.cfg, moment("15:10"))
        self.assertFalse(first["skipped"])
        self.assertEqual(len(first["snapshots"]), 3)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) c FROM snapshot_table").fetchone()["c"], 3)
        self.assertEqual([s["scope"] for s in first["snapshots"]], list(SCOPES))
        frozen = get_latest_frozen(self.conn)
        self.assertIsNotNone(frozen)
        self.assertEqual(frozen["scope"], "full")
        again = freeze_daily_snapshot(self.conn, self.cfg, moment("15:12"))
        self.assertTrue(again["skipped"])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) c FROM snapshot_table").fetchone()["c"], 3)


if __name__ == "__main__":
    unittest.main()
