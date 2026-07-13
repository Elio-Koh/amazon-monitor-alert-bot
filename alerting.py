from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping, Optional


SEVERITY_ORDER = {"P0": 0, "P1": 1, "P2": 2}


def _float_config(config: Mapping[str, str], key: str, default: float) -> float:
    try:
        value = float(str(config.get(key, "")).strip())
    except ValueError:
        return default
    return value if math.isfinite(value) else default


def _int_config(config: Mapping[str, str], key: str, default: int) -> int:
    try:
        value = float(str(config.get(key, "")).strip())
    except ValueError:
        return default
    if not math.isfinite(value):
        return default
    return int(value)


def _bool_config(config: Mapping[str, str], key: str, default: bool = False) -> bool:
    value = str(config.get(key, "")).strip().lower()
    if value in {"true", "yes", "y", "1", "on"}:
        return True
    if value in {"false", "no", "n", "0", "off"}:
        return False
    return default


@dataclass(frozen=True)
class AlertConfig:
    price_pct_threshold: float = 5.0
    critical_price_pct_threshold: float = 10.0
    price_abs_threshold: float = 1.0
    rank_pct_threshold: float = 20.0
    low_inventory_threshold: int = 5
    delivery_days_threshold: int = 2
    max_summary_items: int = 10
    min_severity: str = "P1"
    dedupe_window_days: int = 1
    send_no_change: bool = False
    feishu_message_mode: str = "card"
    full_report_output: str = ""
    full_report_url: str = ""

    @classmethod
    def from_mapping(cls, config: Mapping[str, str]) -> "AlertConfig":
        min_severity = str(config.get("ALERT_MIN_SEVERITY", "P1")).strip().upper() or "P1"
        if min_severity not in SEVERITY_ORDER:
            min_severity = "P1"
        mode = str(config.get("FEISHU_MESSAGE_MODE", "card")).strip().lower() or "card"
        if mode not in {"card", "text"}:
            mode = "card"
        return cls(
            price_pct_threshold=_float_config(config, "ALERT_PRICE_PCT_THRESHOLD", 5.0),
            critical_price_pct_threshold=_float_config(config, "ALERT_CRITICAL_PRICE_PCT_THRESHOLD", 10.0),
            price_abs_threshold=_float_config(config, "ALERT_PRICE_ABS_THRESHOLD", 1.0),
            rank_pct_threshold=_float_config(config, "ALERT_RANK_PCT_THRESHOLD", 20.0),
            low_inventory_threshold=_int_config(config, "ALERT_LOW_INVENTORY_THRESHOLD", 5),
            delivery_days_threshold=_int_config(config, "ALERT_DELIVERY_DAYS_THRESHOLD", 2),
            max_summary_items=max(1, _int_config(config, "ALERT_MAX_SUMMARY_ITEMS", 10)),
            min_severity=min_severity,
            dedupe_window_days=max(0, _int_config(config, "ALERT_DEDUPE_WINDOW_DAYS", 1)),
            send_no_change=_bool_config(config, "ALERT_SEND_NO_CHANGE", False),
            feishu_message_mode=mode,
            full_report_output=str(config.get("FULL_REPORT_OUTPUT", "")).strip(),
            full_report_url=str(config.get("FULL_REPORT_URL", "")).strip(),
        )


@dataclass(frozen=True)
class ChangeEvent:
    severity: str
    category: str
    parent_asin: Optional[str]
    child_asin: Optional[str]
    field: str
    before: Any
    after: Any
    title: str
    detail: str
    action: str
    raw: str

    def dedupe_key(self) -> str:
        parts = [
            self.severity,
            self.category,
            self.parent_asin or "",
            self.child_asin or "",
            self.field,
            "" if self.before is None else str(self.before),
            "" if self.after is None else str(self.after),
        ]
        return "|".join(parts)
