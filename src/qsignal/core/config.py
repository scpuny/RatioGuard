"""配置加载与 config_hash。

config_hash 只覆盖「影响统计口径」的键：修改通知地址、数据库路径、轮询间隔等运行态
参数不会切分统计样本；修改阈值 / 止盈止损 / 成本 / 标的 / 统计口径则会切分。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG_PATH = ROOT / "config.yaml"

# 参与 config_hash 的键（顺序无关，序列化时 sort_keys=True）
STAT_KEYS = (
    "strategy_version",
    "symbol_a", "symbol_b",
    "base_ratio_mode", "base_ratio", "base_ratio_lookback_days",
    "spread_threshold", "tp_sl_basis",
    "take_profit_offset", "stop_loss_offset", "cost_per_unit",
    "confidence_level", "min_qualified_samples", "min_qualified_sessions",
    "roll_window_1", "roll_window_2", "min_window_samples",
    "min_effect_size", "drift_alpha", "drift_comparisons",
    "ambiguity_warn_ratio", "hold_rate_warn", "allow_cross_version_merge",
    "max_jump_pct", "max_pair_skew_sec", "max_quote_age_sec",
)

REQUIRED_KEYS = (
    "symbol_a", "symbol_b", "spread_threshold",
    "take_profit_offset", "stop_loss_offset",
    "confidence_level", "min_qualified_samples",
    "roll_window_1", "roll_window_2",
    "trade_start", "trade_end", "trade_start2", "trade_end2",
    "eod_freeze", "db_path",
)


class ConfigError(ValueError):
    """配置非法。"""


class Config:
    """只读配置包装。所有模块统一通过 cfg.get() / cfg[key] 取值。"""

    def __init__(self, raw: dict[str, Any], path: str | Path | None = None):
        if not isinstance(raw, dict):
            raise ConfigError("config.yaml 顶层必须是映射")
        self.raw = raw
        self.path = Path(path) if path else None
        self.root = ROOT
        self._validate()

    def _validate(self) -> None:
        missing = [k for k in REQUIRED_KEYS if k not in self.raw]
        if missing:
            raise ConfigError(f"缺少必需配置项: {missing}")
        if self.raw.get("tp_sl_basis", "entry_ratio") != "entry_ratio":
            raise ConfigError(
                "当前版本仅支持 tp_sl_basis=entry_ratio；base_ratio 口径下盈亏距离不再等于"
                "配置偏移，break_even 需重推（见方案 3.8），暂不启用"
            )
        if self.raw.get("base_ratio_mode", "rolling_median") not in ("fixed", "rolling_median"):
            raise ConfigError("base_ratio_mode 必须是 fixed 或 rolling_median")
        for key in ("take_profit_offset", "stop_loss_offset", "spread_threshold", "confidence_level"):
            if float(self.raw[key]) <= 0:
                raise ConfigError(f"{key} 必须为正数")
        if not 0 < float(self.raw["confidence_level"]) < 1:
            raise ConfigError("confidence_level 必须在 (0, 1) 内")
        if float(self.raw.get("cost_per_unit", 0)) < 0:
            raise ConfigError("cost_per_unit 不得为负")

    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.raw[key]

    @property
    def config_hash(self) -> str:
        return compute_config_hash(self.raw)

    @property
    def strategy_version(self) -> str:
        return str(self.raw.get("strategy_version", "unknown"))

    def stat_subset(self) -> dict[str, Any]:
        return {k: self.raw.get(k) for k in STAT_KEYS}

    def resolve(self, value: str | Path) -> Path:
        """把配置里的相对路径解析到项目根目录。"""
        p = Path(value)
        return p if p.is_absolute() else (self.root / p).resolve()


def compute_config_hash(raw: dict[str, Any]) -> str:
    subset = {k: raw.get(k) for k in STAT_KEYS}
    payload = json.dumps(subset, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def load_config(path: str | Path | None = None) -> Config:
    """加载配置。相对路径优先按当前工作目录解析，回退到项目根目录。"""
    if path is None:
        p = DEFAULT_CONFIG_PATH
    else:
        p = Path(path)
        if not p.is_absolute():
            candidate = (Path.cwd() / p).resolve()
            p = candidate if candidate.exists() else (ROOT / p).resolve()
    if not p.exists():
        raise ConfigError(f"配置文件不存在: {p}")
    with p.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return Config(raw, p)
