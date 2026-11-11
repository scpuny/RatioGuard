"""M1 验收：行情采集、脏数据过滤、信号生成、SQL 前视审计。

验收标准（方案 M1）：
  - 各类脏数据被丢弃且原因明确，正常行情通过
  - 非交易时段不发起请求；无效行情同样落 quote_table 供审计
  - 行情源全失败时跳过本轮并写 run_log
  - 信号只能绑定 quote_ts <= signal_ts 的有效行情，引用未来行情直接抛异常
  - audit_no_lookahead() 能检出违规绑定
  - 阈值判定、防重复信号、rolling_median 基准、交易日历门禁正确
"""
from __future__ import annotations

import unittest
from datetime import datetime

from base import (
    TempDBMixin, insert_quote, insert_signal, local_ts, make_cfg,
)

from qsignal.core.audit import audit_no_lookahead
from qsignal.core.calendar import in_trade_window, session_gate
from qsignal.market.collector import (
    FakeProvider, Quote, collect_once, normalize_ts, validate_quote,
)
from qsignal.strategy.generator import (
    LookaheadViolation, estimate_base_ratio, generate_signal, signal_levels,
)

DAY = "2026-11-11"


def moment(hhmm: str) -> datetime:
    return datetime.fromisoformat(f"{DAY}T{hhmm}:00+08:00")


def quote(**kw) -> Quote:
    data = {
        "quote_ts": local_ts(DAY, "10:00"),
        "code_a": "510300", "price_a": 4.0,
        "code_b": "159919", "price_b": 4.0,
    }
    data.update(kw)
    return Quote(**data)


class TestNormalizeAndValidate(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg()
        self.now = moment("10:00:30")

    def test_normalize_ts(self):
        self.assertEqual(normalize_ts("2026-11-11 10:00:00"),
                         "2026-11-11T10:00:00+08:00")
        self.assertIsNone(normalize_ts(None))
        self.assertIsNone(normalize_ts("not-a-date"))
        self.assertTrue(normalize_ts("15:00:00").endswith("T15:00:00+08:00"))

    def test_valid(self):
        ok, reason = validate_quote(quote(), None, self.cfg, self.now)
        self.assertTrue(ok, reason)
        self.assertIsNone(reason)

    def test_zero_price(self):
        ok, reason = validate_quote(quote(price_b=0.0), None, self.cfg, self.now)
        self.assertFalse(ok)
        self.assertEqual(reason, "invalid_price_b")

    def test_stale_and_future(self):
        self.assertEqual(
            validate_quote(quote(quote_ts=local_ts(DAY, "09:55")), None,
                           self.cfg, self.now)[1], "stale_quote")
        self.assertEqual(
            validate_quote(quote(quote_ts=local_ts(DAY, "10:05")), None,
                           self.cfg, self.now)[1], "future_quote")

    def test_bad_timestamp(self):
        self.assertEqual(
            validate_quote(quote(quote_ts="bad"), None, self.cfg, self.now)[1],
            "bad_timestamp")

    def test_pair_skew(self):
        q = quote(quote_ts_a=local_ts(DAY, "10:00"),
                  quote_ts_b=local_ts(DAY, "10:01"))
        self.assertEqual(validate_quote(q, None, self.cfg, self.now)[1], "pair_skew")

    def test_price_jump(self):
        prev = quote(price_a=4.0)
        cur = quote(price_a=4.4)
        self.assertEqual(validate_quote(cur, prev, self.cfg, self.now)[1],
                         "price_jump_a")

    def test_iopv_deviation(self):
        self.assertEqual(
            validate_quote(quote(iopv_a=10.0), None, self.cfg, self.now)[1],
            "iopv_deviation_a")


class TestCollectOnce(TempDBMixin, unittest.TestCase):
    def test_outside_window_does_not_fetch(self):
        provider = FakeProvider([quote()])
        result = collect_once(self.conn, make_cfg(), provider, moment("12:00"))
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "outside_trade_window")
        self.assertEqual(provider.calls, 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) c FROM quote_table")
                         .fetchone()["c"], 0)

    def test_valid_and_invalid_rows_are_both_persisted(self):
        provider = FakeProvider([
            quote(quote_ts=local_ts(DAY, "10:00")),
            quote(quote_ts=local_ts(DAY, "10:01"), price_a=0.0),
        ])
        cfg = make_cfg()
        first = collect_once(self.conn, cfg, provider, moment("10:00:30"))
        second = collect_once(self.conn, cfg, provider, moment("10:01:30"))
        self.assertTrue(first["ok"])
        self.assertFalse(second["ok"])
        self.assertEqual(second["drop_reason"], "invalid_price_a")
        rows = self.conn.execute(
            "SELECT is_valid,drop_reason FROM quote_table ORDER BY quote_id"
        ).fetchall()
        self.assertEqual([r["is_valid"] for r in rows], [1, 0])
        self.assertEqual(rows[1]["drop_reason"], "invalid_price_a")

    def test_all_sources_failed_is_logged(self):
        result = collect_once(self.conn, make_cfg(), FakeProvider(), moment("10:00"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "quote_unavailable")
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) c FROM run_log WHERE action='fetch_failed'"
        ).fetchone()["c"], 1)


class TestGenerator(TempDBMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.cfg = make_cfg(base_ratio_mode="fixed")

    def _quote(self, ratio: float, hhmm: str) -> int:
        return insert_quote(self.conn, local_ts(DAY, hhmm),
                            price_a=ratio * 4.0, price_b=4.0)

    def test_signal_levels(self):
        self.assertEqual(signal_levels(1.010, "spread_long", self.cfg),
                         (1.008, 1.014))
        self.assertEqual(signal_levels(1.000, "spread_short", self.cfg),
                         (1.002, 0.996))

    def test_no_lookahead_rejects_future_quote(self):
        qid = self._quote(1.010, "10:05")
        with self.assertRaises(LookaheadViolation):
            generate_signal(self.conn, self.cfg, qid,
                            signal_ts=local_ts(DAY, "10:00"))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) c FROM signal_table")
                         .fetchone()["c"], 0)

    def test_binds_quote_id_and_passes_sql_audit(self):
        qid = self._quote(1.010, "10:00")
        result = generate_signal(self.conn, self.cfg, qid,
                                 signal_ts=local_ts(DAY, "10:00:05"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["signal_type"], "spread_long")
        self.assertEqual(result["quote_id"], qid)
        row = self.conn.execute("SELECT quote_id,status FROM signal_table WHERE id=?",
                                (result["signal_id"],)).fetchone()
        self.assertEqual(row["quote_id"], qid)
        self.assertEqual(row["status"], "open")
        self.assertEqual(audit_no_lookahead(self.conn), [])

    def test_sql_audit_detects_future_binding(self):
        future_qid = self._quote(1.010, "10:10")
        sid = insert_signal(self.conn, local_ts(DAY, "10:00"),
                            quote_id=future_qid, config_hash="audit")
        violations = audit_no_lookahead(self.conn)
        self.assertTrue(any(v["id"] == sid for v in violations))

    def test_threshold_breach_types(self):
        cases = ((1.009, "spread_long"), (1.001, "spread_short"),
                 (1.005, "no_threshold_breach"))
        for idx, (ratio, expected) in enumerate(cases):
            qid = self._quote(ratio, f"10:{idx:02d}")
            result = generate_signal(self.conn, self.cfg, qid,
                                     signal_ts=local_ts(DAY, f"10:{idx:02d}"))
            if expected == "no_threshold_breach":
                self.assertFalse(result["ok"])
                self.assertEqual(result["reason"], expected)
            else:
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["signal_type"], expected)
                self.conn.execute(
                    "UPDATE signal_table SET status='closed',result='win' WHERE id=?",
                    (result["signal_id"],))
                self.conn.commit()

    def test_duplicate_open_signal(self):
        first = self._quote(1.010, "10:00")
        second = self._quote(1.011, "10:01")
        self.assertTrue(generate_signal(self.conn, self.cfg, first,
                                        signal_ts=local_ts(DAY, "10:00"))["ok"])
        dup = generate_signal(self.conn, self.cfg, second,
                              signal_ts=local_ts(DAY, "10:01"))
        self.assertEqual(dup["reason"], "duplicate_open_signal")

    def test_estimate_base_ratio_rolling_median(self):
        cfg = make_cfg(base_ratio_mode="rolling_median")
        for idx, ratio in enumerate((1.000, 1.010, 1.020)):
            insert_quote(self.conn, local_ts(DAY, f"09:{30 + idx:02d}"),
                         price_a=ratio * 4.0, price_b=4.0)
        value = estimate_base_ratio(cfg=cfg, conn=self.conn,
                                    as_of=local_ts(DAY, "09:35"))
        self.assertAlmostEqual(value, 1.010, places=9)


class TestCalendar(unittest.TestCase):
    def test_trade_windows(self):
        cfg = make_cfg()
        self.assertTrue(in_trade_window(moment("10:00"), cfg))
        self.assertFalse(in_trade_window(moment("12:00"), cfg))
        self.assertTrue(in_trade_window(moment("13:30"), cfg))
        self.assertFalse(in_trade_window(moment("09:00"), cfg))

    def test_session_gate_excludes_non_trading_day(self):
        cfg = make_cfg()
        allowed, reason = session_gate(cfg, moment("10:00"),
                                       trading_days={"2026-11-11"})
        self.assertTrue(allowed)
        allowed, reason = session_gate(cfg, moment("10:00"),
                                       trading_days={"2026-11-10"})
        self.assertFalse(allowed)
        self.assertEqual(reason, "non_trading_day")


if __name__ == "__main__":
    unittest.main()
