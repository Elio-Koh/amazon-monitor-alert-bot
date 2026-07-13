from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import math
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple


ASIN_RE = r"[A-Z0-9]{10}"
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


def filter_events(events: Iterable[ChangeEvent], config: AlertConfig) -> List[ChangeEvent]:
    max_order = SEVERITY_ORDER.get(config.min_severity, SEVERITY_ORDER["P1"])
    return [event for event in events if SEVERITY_ORDER.get(event.severity, 99) <= max_order]


def format_report_time(value: Any) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        parsed = datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    beijing = parsed.astimezone(timezone(timedelta(hours=8)))
    return f"北京时间 {beijing:%Y-%m-%d %H:%M:%S}"


def _section_lines(title: str, events: List[ChangeEvent], limit: int) -> List[str]:
    if not events:
        return []

    lines = ["", title]
    visible = events[:max(0, limit)]
    for index, event in enumerate(visible, start=1):
        parent_suffix = f"｜父体 {event.parent_asin}" if event.parent_asin else ""
        lines.extend(
            [
                f"{index}. {event.title}{parent_suffix}",
                f"   {event.detail}",
                f"   建议：{event.action}",
            ]
        )
    hidden = len(events) - len(visible)
    if hidden > 0:
        lines.append(f"... 还有 {hidden} 项同级重点事项，见完整报告")
    return lines


def render_text_summary(events: Iterable[ChangeEvent], captured_at: str, config: AlertConfig) -> str:
    filtered = filter_events(events, config)
    p0 = [event for event in filtered if event.severity == "P0"]
    p1 = [event for event in filtered if event.severity == "P1"]

    if not p0 and not p1 and not config.send_no_change:
        return ""

    lines = [
        f"ASIN 每日重点提醒｜{format_report_time(captured_at)}",
        f"今日结论：P0 {len(p0)} 项｜P1 {len(p1)} 项",
    ]
    lines.extend(_section_lines("P0 必看：", p0, config.max_summary_items))
    remaining = max(0, config.max_summary_items - min(len(p0), config.max_summary_items))
    lines.extend(_section_lines("P1 复核：", p1, remaining))
    if config.full_report_url:
        lines.extend(["", f"完整报告：{config.full_report_url}"])
    return "\n".join(lines)


def _event_date(captured_at: str) -> date:
    try:
        parsed = datetime.fromisoformat(str(captured_at).replace("Z", "+00:00"))
    except ValueError:
        parsed = datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone(timedelta(hours=8))).date()


def _parse_date(value: Any) -> Optional[date]:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def apply_dedupe(
    events: Iterable[ChangeEvent],
    history: Mapping[str, Mapping[str, Any]],
    captured_at: str,
    config: AlertConfig,
) -> Tuple[List[ChangeEvent], Dict[str, Dict[str, str]]]:
    current_day = _event_date(captured_at)
    keep_days = max(config.dedupe_window_days, 1)
    updated: Dict[str, Dict[str, str]] = {}

    for key, value in history.items():
        if not isinstance(value, Mapping):
            continue
        last_seen = _parse_date(value.get("last_seen_on"))
        last_sent = _parse_date(value.get("last_sent_on"))
        last_seen_delta = (current_day - last_seen).days if last_seen is not None else None
        last_sent_delta = (current_day - last_sent).days if last_sent is not None else None
        recent_last_seen = last_seen is not None and last_seen_delta is not None and 0 <= last_seen_delta <= keep_days
        recent_last_sent = last_sent is not None and last_sent_delta is not None and 0 <= last_sent_delta <= keep_days
        if recent_last_seen or recent_last_sent:
            last_sent_value = str(last_sent) if recent_last_sent else str(value.get("last_sent_on", "")) if last_sent is None else ""
            updated[str(key)] = {
                "last_seen_on": str(last_seen) if recent_last_seen else "",
                "last_sent_on": last_sent_value,
                "severity": str(value.get("severity", "")),
            }

    fresh: List[ChangeEvent] = []
    for event in events:
        key = event.dedupe_key()
        prior = updated.get(key)
        last_sent = _parse_date(prior.get("last_sent_on")) if prior else None
        sent_delta = (current_day - last_sent).days if last_sent is not None else None
        suppress = sent_delta is not None and 0 <= sent_delta <= config.dedupe_window_days
        if not suppress:
            fresh.append(event)
        updated[key] = {
            "last_seen_on": str(current_day),
            "last_sent_on": str(last_sent if suppress and last_sent else current_day),
            "severity": event.severity,
        }

    return fresh, updated


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _as_float(value: Any) -> Optional[float]:
    try:
        parsed = float(_text(value))
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None


def _as_int(value: Any) -> Optional[int]:
    parsed = _as_float(value)
    return None if parsed is None else int(parsed)


def _is_empty(value: Any) -> bool:
    return _text(value) in {"", "-", "None", "null", "无"}


def _display(value: Any) -> str:
    return "无" if _is_empty(value) else _text(value)


def _pct_change(before: Any, after: Any) -> float:
    before_value = _as_float(before)
    after_value = _as_float(after)
    if before_value is None or after_value is None or before_value == 0:
        return 0.0
    return abs(after_value - before_value) / abs(before_value) * 100.0


def _delivery_days(value: Any, captured_at: Any) -> Optional[int]:
    text = _text(value)
    if not text:
        return None
    first = re.split(r";\s*fastest\s+", text, maxsplit=1, flags=re.I)[0].strip()
    numeric = re.search(r"(-?\d+)\s*(?:days?|天)\b", first, re.I)
    if numeric:
        return int(numeric.group(1))

    base_date = _event_date(str(captured_at or ""))
    lower = first.lower()
    if lower.startswith("today"):
        return 0
    if lower.startswith("tomorrow"):
        return 1

    for fmt in ("%A, %B %d", "%A, %b %d", "%B %d", "%b %d"):
        try:
            parsed = datetime.strptime(first, fmt).date().replace(year=base_date.year)
        except ValueError:
            continue
        if parsed < base_date:
            parsed = parsed.replace(year=parsed.year + 1)
        return (parsed - base_date).days

    weekday_names = {
        "mon": 0,
        "monday": 0,
        "tue": 1,
        "tuesday": 1,
        "wed": 2,
        "wednesday": 2,
        "thu": 3,
        "thursday": 3,
        "fri": 4,
        "friday": 4,
        "sat": 5,
        "saturday": 5,
        "sun": 6,
        "sunday": 6,
    }
    token = re.sub(r"[^a-z]", "", lower.split(",", 1)[0])
    target = weekday_names.get(token)
    if target is None:
        return None
    return (target - base_date.weekday()) % 7


def _parent_memberships(snapshot: Mapping[str, Any]) -> Dict[str, str]:
    memberships: Dict[str, str] = {}
    for parent_asin, parent in snapshot.get("parents", {}).items():
        if not isinstance(parent, Mapping):
            continue
        for child_asin in parent.get("child_asins", []) or []:
            memberships[str(child_asin)] = str(parent_asin)
        for child_asin in parent.get("inventory_only_asins", []) or []:
            memberships[str(child_asin)] = str(parent_asin)
    return memberships


def _event(
    severity: str,
    category: str,
    parent_asin: Optional[str],
    child_asin: Optional[str],
    field: str,
    before: Any,
    after: Any,
    title: str,
    detail: str,
    action: str,
    raw: str,
) -> ChangeEvent:
    return ChangeEvent(
        severity=severity,
        category=category,
        parent_asin=parent_asin,
        child_asin=child_asin,
        field=field,
        before=before,
        after=after,
        title=title,
        detail=detail,
        action=action,
        raw=raw,
    )


def _classify_child_field(
    child_asin: str,
    parent_asin: Optional[str],
    field: str,
    before: Any,
    after: Any,
    raw: str,
    config: AlertConfig,
    captured_at: Any,
) -> ChangeEvent:
    if field == "inventory":
        inventory = _as_int(after)
        detail = f"库存：{_display(before)} -> {_display(after)}"
        if inventory == 0:
            return _event("P0", "inventory", parent_asin, child_asin, field, before, after, f"{child_asin} 库存归零", detail, "检查补货、广告预算和前台可售状态", raw)
        if inventory is not None and inventory > 0 and inventory <= config.low_inventory_threshold:
            return _event("P1", "inventory", parent_asin, child_asin, field, before, after, f"{child_asin} 低库存", detail, "确认补货节奏和广告消耗", raw)
        return _event("P2", "inventory", parent_asin, child_asin, field, before, after, f"{child_asin} 库存变化", detail, "确认库存变化是否符合预期", raw)

    if field in {"coupon", "promotion"}:
        label = "Coupon" if field == "coupon" else "促销/Deal"
        before_empty = _is_empty(before)
        after_empty = _is_empty(after)
        if before_empty and not after_empty:
            severity = "P0"
            title = f"{child_asin} {label}开始"
        elif not before_empty and after_empty:
            severity = "P0"
            title = f"{child_asin} {label}结束"
        else:
            severity = "P1" if not before_empty and not after_empty else "P0"
            title = f"{child_asin} {label}变化"
        detail = f"{label}：{_display(before)} -> {_display(after)}"
        return _event(severity, "promotion", parent_asin, child_asin, field, before, after, title, detail, "检查广告预算、价格竞争力和促销排期", raw)

    if field == "price":
        before_price = _as_float(before)
        after_price = _as_float(after)
        abs_change = abs((after_price or 0.0) - (before_price or 0.0))
        pct_change = _pct_change(before, after)
        if pct_change >= config.critical_price_pct_threshold:
            severity = "P0"
        elif abs_change >= config.price_abs_threshold and pct_change >= config.price_pct_threshold:
            severity = "P1"
        else:
            severity = "P2"
        detail = f"价格：{_display(before)} -> {_display(after)}"
        return _event(severity, "price", parent_asin, child_asin, field, before, after, f"{child_asin} 价格变化", detail, "检查竞品价格、广告 ACOS 和预算", raw)

    if field == "delivery_promise":
        detail = f"配送时效：{_display(before)} -> {_display(after)}"
        before_days = _delivery_days(before, captured_at)
        after_days = _delivery_days(after, captured_at)
        if before_days is not None and after_days is not None:
            severity = "P1" if after_days - before_days >= config.delivery_days_threshold else "P2"
        else:
            severity = "P2"
        return _event(severity, "delivery", parent_asin, child_asin, field, before, after, f"{child_asin} 配送时效变化", detail, "检查库存、配送方式和转化率影响", raw)

    detail = f"{field}：{_display(before)} -> {_display(after)}"
    return _event("P2", field, parent_asin, child_asin, field, before, after, f"{child_asin} {field}变化", detail, "确认变化是否符合预期", raw)


def _classify_parent_field(parent_asin: str, field: str, before: Any, after: Any, raw: str, config: AlertConfig) -> ChangeEvent:
    detail = f"{field}：{_display(before)} -> {_display(after)}"
    if field in {"major_rank", "minor_rank"}:
        severity = "P1" if _pct_change(before, after) >= config.rank_pct_threshold else "P2"
        return _event(severity, "rank", parent_asin, None, field, before, after, f"{parent_asin} 排名变化", detail, "检查排名变化和流量影响", raw)
    if field in {"stars", "rating_count"}:
        return _event("P2", "parent_metric", parent_asin, None, field, before, after, f"{parent_asin} 评分指标变化", detail, "确认评论指标变化", raw)
    return _event("P2", "parent", parent_asin, None, field, before, after, f"{parent_asin} {field}变化", detail, "确认父体变化是否符合预期", raw)


def _classify_error(raw: str, parent_asin: Optional[str] = None) -> ChangeEvent:
    severity = "P0" if "前台数据缺失" in raw or "parent failed" in raw.lower() else "P1"
    title = f"{parent_asin or '数据源'} 异常"
    return _event(severity, "data_source", parent_asin, None, "error", None, None, title, raw, "确认采集源是否影响今日判断", raw)


def build_change_events(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
    changes: Iterable[str],
    config: AlertConfig,
) -> List[ChangeEvent]:
    previous_memberships = _parent_memberships(previous)
    current_memberships = _parent_memberships(current)
    captured_at = current.get("captured_at") if isinstance(current, Mapping) else None
    events: List[ChangeEvent] = []

    parent_field_re = re.compile(rf"^({ASIN_RE}) parent ([a-z_]+): (.*) -> (.*)$")
    child_field_re = re.compile(rf"^({ASIN_RE}) child ([a-z_]+): (.*) -> (.*)$")
    child_relation_re = re.compile(rf"^({ASIN_RE}) child (added|removed): ({ASIN_RE})$")
    inventory_only_re = re.compile(rf"^({ASIN_RE}) inventory-only child (added|removed): ({ASIN_RE})$")

    for raw in changes:
        raw_text = str(raw)
        match = parent_field_re.match(raw_text)
        if match:
            parent_asin, field, before, after = match.groups()
            events.append(_classify_parent_field(parent_asin, field, before, after, raw_text, config))
            continue

        match = child_field_re.match(raw_text)
        if match:
            child_asin, field, before, after = match.groups()
            parent_asin = current_memberships.get(child_asin) or previous_memberships.get(child_asin)
            events.append(_classify_child_field(child_asin, parent_asin, field, before, after, raw_text, config, captured_at))
            continue

        match = child_relation_re.match(raw_text)
        if match:
            parent_asin, relation, child_asin = match.groups()
            title = f"{child_asin} 子体新增" if relation == "added" else f"{child_asin} 子体移除"
            severity = "P1" if relation == "added" else "P0"
            verb = "新增" if relation == "added" else "移除"
            detail = f"父体 {parent_asin} {verb}子体 {child_asin}"
            events.append(_event(severity, "availability", parent_asin, child_asin, "child", None, relation, title, detail, "检查前台可售状态和变体关系", raw_text))
            continue

        match = inventory_only_re.match(raw_text)
        if match:
            parent_asin, relation, child_asin = match.groups()
            title = f"{child_asin} inventory-only新增" if relation == "added" else f"{child_asin} inventory-only移除"
            severity = "P0" if relation == "added" else "P1"
            verb = "新增" if relation == "added" else "移除"
            detail = f"父体 {parent_asin} 库存侧异常子体 {child_asin} {verb}"
            events.append(_event(severity, "availability", parent_asin, child_asin, "inventory_only", None, relation, title, detail, "检查前台可售状态和变体关系", raw_text))
            continue

        parent_match = re.search(rf"({ASIN_RE})", raw_text)
        events.append(_classify_error(raw_text, parent_match.group(1) if parent_match else None))

    return sorted(
        events,
        key=lambda event: (
            SEVERITY_ORDER.get(event.severity, 99),
            event.parent_asin or "",
            event.child_asin or "",
            event.category,
            event.field,
        ),
    )
