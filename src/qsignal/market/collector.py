"""行情采集与脏数据过滤（L1 常驻服务）。

数据源降级链（已核实 ak.fund_etf_spot_em 存在返回条数截断风险）：
  1) ak.fund_etf_spot_em()          东财 ETF 全表快照
  2) push2.eastmoney 单只报价        按 secid 精确查询，不受分页截断影响
  3) ak.fund_etf_spot_ths()         同花顺 ETF 实时行情
全部失败 -> 本轮跳过并写 run_log，不生成信号。

约束：非交易时段不发起请求；每一轮（含被丢弃的）原始行情都落 quote_table。
"""
from __future__ import annotations

import json
import math
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Protocol
from zoneinfo import ZoneInfo

from ..core.audit import log_run, now_iso, parse_iso
from ..core.calendar import in_trade_window


class QuoteUnavailable(RuntimeError):
    """所有行情源均不可用。"""


@dataclass
class Quote:
    """一轮采集的原始快照。ratio 由价格推导，不接受外部传入，避免口径不一致。"""
    quote_ts: str
    code_a: str
    price_a: float
    code_b: str
    price_b: float
    source: str = "primary"
    iopv_a: float | None = None
    iopv_b: float | None = None
    quote_ts_a: str | None = None
    quote_ts_b: str | None = None
    is_valid: bool = True
    drop_reason: str | None = None
    ingest_ts: str | None = None

    @property
    def ratio(self) -> float | None:
        if self.price_a is None or self.price_b in (None, 0):
            return None
        return self.price_a / self.price_b


class QuoteProvider(Protocol):
    def fetch(self, cfg) -> Quote: ...


class FakeProvider:
    """可注入的行情源，用于单测与离线回放（无需 akshare / 网络）。"""

    def __init__(self, quotes: Iterable[Quote | dict[str, Any]] = ()):
        self.quotes = list(quotes)
        self.calls = 0

    def fetch(self, cfg) -> Quote:
        self.calls += 1
        if not self.quotes:
            raise QuoteUnavailable("fake provider exhausted")
        item = self.quotes.pop(0)
        return Quote(**item) if isinstance(item, dict) else item


def normalize_ts(value: Any, tz: str = "Asia/Shanghai") -> str | None:
    """把行情源时间戳归一化为带 +08:00 的 ISO8601（秒精度）。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in ("nan", "none", "null", "-"):
        return None
    try:
        if "T" not in text and " " not in text and text.count(":") == 2:
            today = datetime.now(ZoneInfo(tz)).date().isoformat()
            text = f"{today}T{text}"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo(tz))
        return dt.astimezone(ZoneInfo(tz)).isoformat(timespec="seconds")
    except (ValueError, TypeError):
        return None


class AkShareProvider:
    """生产行情源：主接口 + 两级降级。所有网络访问均有超时。"""

    TIMEOUT = 5

    def fetch(self, cfg) -> Quote:
        errors: list[str] = []
        for name, handler in (
            ("primary", self._primary),
            ("eastmoney_single", self._single),
            ("ths", self._ths),
        ):
            try:
                quote = handler(cfg)
                if quote is not None:
                    return quote
                errors.append(f"{name}:empty")
            except Exception as exc:
                errors.append(f"{name}:{type(exc).__name__}:{exc}")
        raise QuoteUnavailable(" | ".join(errors))

    @staticmethod
    def _column(frame, *candidates: str) -> str | None:
        for name in candidates:
            if name in frame.columns:
                return name
        return None

    def _primary(self, cfg) -> Quote:
        import akshare as ak  # 延迟导入：离线自检与单测不需要

        frame = ak.fund_etf_spot_em()
        if frame is None or frame.empty:
            raise RuntimeError("主接口返回空")
        code_col = self._column(frame, "代码", "code", "symbol")
        price_col = self._column(frame, "最新价", "price", "close")
        ts_col = self._column(frame, "更新时间", "update_time", "time")
        iopv_col = self._column(frame, "IOPV实时估值", "IOPV", "iopv")
        if not code_col or not price_col:
            raise RuntimeError(f"主接口列名未识别: {list(frame.columns)[:8]}")
        frame = frame.copy()
        frame[code_col] = frame[code_col].astype(str).str.zfill(6)
        index = {str(row[code_col]): row for _, row in frame.iterrows()}
        a, b = str(cfg["symbol_a"]), str(cfg["symbol_b"])
        if a not in index or b not in index:
            # 2025 年该接口多次被截断至 100/200 条，此时必须走降级链
            raise RuntimeError(f"主接口未返回目标代码（共 {len(frame)} 行，疑似条数截断）")
        ra, rb = index[a], index[b]
        stamp = (normalize_ts(ra[ts_col]) or now_iso()) if ts_col else now_iso()
        return Quote(
            quote_ts=stamp,
            code_a=a, price_a=float(ra[price_col]),
            code_b=b, price_b=float(rb[price_col]),
            iopv_a=float(ra[iopv_col]) if iopv_col else None,
            iopv_b=float(rb[iopv_col]) if iopv_col else None,
            source="primary",
        )

    def _fetch_push2(self, code: str) -> dict[str, Any]:
        """东财单只报价。f43=最新价(fltt=2 为浮点)，f86=更新 Unix 秒。

        字段换算以实盘自检校准为准；若返回缩放整数（>100）则按 /1000 还原。
        """
        secid = ("1." if code.startswith(("5", "6", "9")) else "0.") + code
        params = urllib.parse.urlencode({
            "secid": secid,
            "fields": "f43,f57,f58,f86",
            "fltt": "2", "invt": "2",
            "ut": "fa5fd1943c7b386f172d6893dbfba10b",
        })
        url = "https://push2.eastmoney.com/api/qt/stock/get?" + params
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=self.TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        data = payload.get("data") or {}
        if "f43" not in data:
            raise RuntimeError("单只报价接口未返回 f43")
        price = float(data["f43"])
        if price > 100:
            price /= 1000.0
        stamp = (datetime.fromtimestamp(int(data["f86"]), ZoneInfo("Asia/Shanghai"))
                 .isoformat(timespec="seconds") if data.get("f86") else now_iso())
        return {"price": price, "ts": stamp}

    def _single(self, cfg) -> Quote:
        a = self._fetch_push2(str(cfg["symbol_a"]))
        b = self._fetch_push2(str(cfg["symbol_b"]))
        return Quote(
            quote_ts=max(a["ts"], b["ts"]),
            code_a=str(cfg["symbol_a"]), price_a=a["price"],
            code_b=str(cfg["symbol_b"]), price_b=b["price"],
            quote_ts_a=a["ts"], quote_ts_b=b["ts"],
            source="eastmoney_single",
        )

    def _ths(self, cfg) -> Quote:
        import akshare as ak

        frame = ak.fund_etf_spot_ths()
        if frame is None or frame.empty:
            raise RuntimeError("同花顺接口返回空")
        code_col = self._column(frame, "代码", "code")
        price_col = self._column(frame, "最新价", "现价", "price")
        if not code_col or not price_col:
            raise RuntimeError("同花顺接口列名未识别")
        frame = frame.copy()
        frame[code_col] = frame[code_col].astype(str).str.zfill(6)
        index = {str(row[code_col]): row for _, row in frame.iterrows()}
        a, b = str(cfg["symbol_a"]), str(cfg["symbol_b"])
        if a not in index or b not in index:
            raise RuntimeError("同花顺接口未返回目标代码")
        return Quote(
            quote_ts=now_iso(),
            code_a=a, price_a=float(index[a][price_col]),
            code_b=b, price_b=float(index[b][price_col]),
            source="ths",
        )


def validate_quote(quote: Quote, prev: Quote | None, cfg,
                   now: datetime | None = None) -> tuple[bool, str | None]:
    """脏数据过滤。返回 (是否通过, 丢弃原因)。

    检查项：价格有效性、比值有效性、行情新鲜度、两标的报价时间对齐、
    相对上一轮价格突变、IOPV 折溢价异常。
    """
    tz = cfg.get("timezone", "Asia/Shanghai")
    for label, value in (("price_a", quote.price_a), ("price_b", quote.price_b)):
        if value is None or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            return False, f"invalid_{label}"
    ratio = quote.ratio
    if ratio is None or not math.isfinite(ratio) or ratio <= 0:
        return False, "invalid_ratio"

    reference = now or datetime.now(ZoneInfo(tz))
    try:
        quote_time = parse_iso(quote.quote_ts, tz)
        age = (reference - quote_time).total_seconds()
    except (ValueError, TypeError):
        return False, "bad_timestamp"
    if age > float(cfg.get("max_quote_age_sec", 60)):
        return False, "stale_quote"
    if age < -float(cfg.get("max_pair_skew_sec", 10)):
        return False, "future_quote"

    if quote.quote_ts_a and quote.quote_ts_b:
        try:
            skew = abs((parse_iso(quote.quote_ts_a, tz)
                        - parse_iso(quote.quote_ts_b, tz)).total_seconds())
            if skew > float(cfg.get("max_pair_skew_sec", 10)):
                return False, "pair_skew"
        except (ValueError, TypeError):
            return False, "pair_skew_unparsable"

    if prev is not None and prev.is_valid:
        for label, (current, old) in (
            ("a", (quote.price_a, prev.price_a)),
            ("b", (quote.price_b, prev.price_b)),
        ):
            if old and abs(current / old - 1.0) > float(cfg.get("max_jump_pct", 0.05)):
                return False, f"price_jump_{label}"

    for label, price, iopv in (("a", quote.price_a, quote.iopv_a),
                               ("b", quote.price_b, quote.iopv_b)):
        if iopv and iopv > 0 and abs(price / iopv - 1.0) > 0.10:
            return False, f"iopv_deviation_{label}"
    return True, None


def _last_valid_quote(conn) -> Quote | None:
    row = conn.execute(
        "SELECT * FROM quote_table WHERE is_valid=1 "
        "ORDER BY quote_ts DESC, quote_id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return Quote(
        quote_ts=row["quote_ts"], code_a=row["code_a"], price_a=row["price_a"],
        code_b=row["code_b"], price_b=row["price_b"], source=row["source"],
        is_valid=True,
    )


def collect_once(conn, cfg, provider: QuoteProvider,
                 now: datetime | None = None) -> dict[str, Any]:
    """单轮采集。非交易时段直接休眠（不发起请求）。"""
    tz = cfg.get("timezone", "Asia/Shanghai")
    moment = now or datetime.now(ZoneInfo(tz))
    if not in_trade_window(moment, cfg):
        return {"ok": False, "skipped": True, "quote_id": None,
                "reason": "outside_trade_window"}
    try:
        quote = provider.fetch(cfg)
    except Exception as exc:
        log_run(conn, "collector", "fetch_failed",
                input_obj={"source": type(provider).__name__},
                output_obj={"error": str(exc)}, level="WARN",
                message="全部行情源不可用，本轮跳过，不生成信号",
                ts=now_iso(tz=tz, dt=moment))
        return {"ok": False, "skipped": False, "quote_id": None,
                "reason": "quote_unavailable", "error": str(exc)}

    ok, reason = validate_quote(quote, _last_valid_quote(conn), cfg, moment)
    quote.is_valid, quote.drop_reason = ok, reason
    quote.ingest_ts = now_iso(tz=tz, dt=moment)
    conn.execute(
        "INSERT OR IGNORE INTO quote_table"
        "(quote_ts,ingest_ts,source,code_a,price_a,code_b,price_b,ratio,iopv_a,iopv_b,is_valid,drop_reason) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (quote.quote_ts, quote.ingest_ts, quote.source, quote.code_a, quote.price_a,
         quote.code_b, quote.price_b, quote.ratio, quote.iopv_a, quote.iopv_b,
         int(ok), reason),
    )
    row = conn.execute(
        "SELECT quote_id FROM quote_table WHERE quote_ts=? AND source=?",
        (quote.quote_ts, quote.source),
    ).fetchone()
    quote_id = int(row["quote_id"]) if row else None
    conn.commit()
    log_run(conn, "collector", "collect",
            input_obj={"source": quote.source, "quote_ts": quote.quote_ts,
                       "price_a": quote.price_a, "price_b": quote.price_b},
            output_obj={"quote_id": quote_id, "is_valid": ok, "drop_reason": reason},
            ts=quote.ingest_ts)
    return {"ok": ok, "skipped": False, "quote_id": quote_id,
            "drop_reason": reason, "source": quote.source}
