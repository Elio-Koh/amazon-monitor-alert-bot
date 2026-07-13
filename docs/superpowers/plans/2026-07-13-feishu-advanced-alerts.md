# Feishu Advanced Alerts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace noisy daily Feishu dumps with one action-focused Feishu alert that expands only P0/P1 events, deduplicates repeated alerts, and keeps the complete report outside the group chat.

**Architecture:** Keep snapshot collection and raw diffing in `monitor.py`, but add a focused `alerting.py` module that converts raw diff strings into typed `ChangeEvent` records, applies severity rules, thresholds, and dedupe. Daily scheduled runs will build the complete report for storage/artifacts, then send only one summary payload to Feishu by default.

**Tech Stack:** Python 3.11, stdlib `dataclasses`, existing encrypted JSON state, existing Feishu webhook sender, GitHub Actions artifact upload.

---

## Scope Decisions

- Implement the advanced notification path for `--daily-report`.
- Keep `--report-current` and `--render-state-only` capable of producing the full report.
- Do not include "监控范围和数据质量" in the Feishu group summary.
- Do not include counts for low-priority/P2 changes in the Feishu group summary.
- Do not send the complete report to Feishu.
- Preserve backward-compatible text sending for baseline and non-daily change alerts until those flows are intentionally migrated.
- Use one Feishu message per daily run. If the final card would exceed Feishu limits or the card send fails with a business error, send one plain-text fallback summary instead of sending multiple detail messages.

## File Structure

- Create `alerting.py`
  - Owns alert config parsing, `ChangeEvent`, raw diff parsing, severity classification, threshold logic, dedupe history, and summary rendering primitives.
- Modify `monitor.py`
  - Imports `alerting.py`.
  - Adds Feishu card payload support while keeping existing text payload support.
  - Uses `alerting.py` for daily Feishu output.
  - Writes a full report file when requested.
  - Persists alert dedupe history in encrypted snapshot state.
- Modify `tests/test_monitor.py`
  - Update daily report expectations.
  - Add Feishu card/signing tests around the existing sender behavior.
  - Add full-report-output and state persistence tests.
- Create `tests/test_alerting.py`
  - Unit tests for event parsing, severity, thresholds, dedupe, and summary rendering.
- Modify `.github/workflows/daily-monitor.yml`
  - Add alert env vars.
  - Pass a full report output path to `monitor.py`.
  - Upload the full daily report as a GitHub Actions artifact.

## Alert Rules

Default severity rules:

- `P0`
  - Child inventory changes to `0`.
  - Child moves into `inventory_only_asins` with positive inventory, meaning inventory exists but front detail is unavailable.
  - Child is removed from live child list.
  - Coupon or promotion changes from present to absent, or absent to present.
  - Price changes by at least `ALERT_CRITICAL_PRICE_PCT_THRESHOLD`, default `10`.
  - Parent/front data is missing or unusable.
- `P1`
  - Price changes by both `ALERT_PRICE_PCT_THRESHOLD` percent and `ALERT_PRICE_ABS_THRESHOLD` absolute amount.
  - Inventory changes to a positive value at or below `ALERT_LOW_INVENTORY_THRESHOLD`.
  - Rank changes by at least `ALERT_RANK_PCT_THRESHOLD`.
  - Coupon or promotion changes between two non-empty values.
  - Delivery promise gets slower by at least `ALERT_DELIVERY_DAYS_THRESHOLD`, default `2`.
  - Data source error affects freshness but previous snapshot is used.
- `P2`
  - Normal inventory fluctuations above the low-inventory threshold.
  - Small rank changes.
  - Rating count changes.
  - Newly improved field coverage that was already ignored by `field_changed`.
  - Data source warnings that do not affect alertable fields.

Default group send behavior:

- Send no Feishu message when there are no P0/P1 events and `ALERT_SEND_NO_CHANGE=false`.
- Still mark the daily run as completed when there are no P0/P1 events, so the backup cron does not recollect the same day.
- Send exactly one Feishu message when there is at least one non-deduped P0/P1 event.
- Include only P0/P1 sections and a full-report link if available.
- Do not mention P2 counts.

Default config:

```python
AlertConfig(
    price_pct_threshold=5.0,
    critical_price_pct_threshold=10.0,
    price_abs_threshold=1.0,
    rank_pct_threshold=20.0,
    low_inventory_threshold=5,
    delivery_days_threshold=2,
    max_summary_items=10,
    min_severity="P1",
    dedupe_window_days=1,
    send_no_change=False,
    feishu_message_mode="card",
    full_report_output="",
    full_report_url="",
)
```

---

### Task 1: Add the `ChangeEvent` Model and Alert Config

**Files:**
- Create: `alerting.py`
- Create: `tests/test_alerting.py`

- [ ] **Step 1: Write failing tests for config parsing and event fields**

Add this to `tests/test_alerting.py`:

```python
import unittest

import alerting


class AlertingTest(unittest.TestCase):
    def test_alert_config_reads_thresholds_from_env_mapping(self):
        config = alerting.AlertConfig.from_mapping(
            {
                "ALERT_PRICE_PCT_THRESHOLD": "7.5",
                "ALERT_CRITICAL_PRICE_PCT_THRESHOLD": "12",
                "ALERT_PRICE_ABS_THRESHOLD": "2.5",
                "ALERT_RANK_PCT_THRESHOLD": "30",
                "ALERT_LOW_INVENTORY_THRESHOLD": "4",
                "ALERT_DELIVERY_DAYS_THRESHOLD": "3",
                "ALERT_MAX_SUMMARY_ITEMS": "6",
                "ALERT_MIN_SEVERITY": "P0",
                "ALERT_DEDUPE_WINDOW_DAYS": "2",
                "ALERT_SEND_NO_CHANGE": "true",
                "FEISHU_MESSAGE_MODE": "text",
                "FULL_REPORT_OUTPUT": "state-report.txt",
                "FULL_REPORT_URL": "https://github.example/actions/runs/1",
            }
        )

        self.assertEqual(config.price_pct_threshold, 7.5)
        self.assertEqual(config.critical_price_pct_threshold, 12.0)
        self.assertEqual(config.price_abs_threshold, 2.5)
        self.assertEqual(config.rank_pct_threshold, 30.0)
        self.assertEqual(config.low_inventory_threshold, 4)
        self.assertEqual(config.delivery_days_threshold, 3)
        self.assertEqual(config.max_summary_items, 6)
        self.assertEqual(config.min_severity, "P0")
        self.assertEqual(config.dedupe_window_days, 2)
        self.assertTrue(config.send_no_change)
        self.assertEqual(config.feishu_message_mode, "text")
        self.assertEqual(config.full_report_output, "state-report.txt")
        self.assertEqual(config.full_report_url, "https://github.example/actions/runs/1")

    def test_change_event_exposes_stable_dedupe_key(self):
        event = alerting.ChangeEvent(
            severity="P0",
            category="promotion",
            parent_asin="PARENT1234",
            child_asin="CHILD00001",
            field="promotion",
            before="Limited time deal",
            after="",
            title="CHILD00001 促销结束",
            detail="促销/Deal：Limited time deal -> 无",
            action="检查广告预算和价格竞争力",
            raw="CHILD00001 child promotion: Limited time deal -> ",
        )

        self.assertEqual(event.dedupe_key(), "P0|promotion|PARENT1234|CHILD00001|promotion|Limited time deal|")
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
python3.11 -m unittest discover -s tests -v
```

Expected result:

```text
ModuleNotFoundError: No module named 'alerting'
```

If `python3.11` is not installed locally, run the same command in the GitHub Actions Python 3.11 environment or install a local Python 3.11 runtime before continuing. The default macOS `python3` in this workspace is 3.8.9 and cannot import `zoneinfo`.

- [ ] **Step 3: Implement `AlertConfig` and `ChangeEvent`**

Create `alerting.py` with:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional


SEVERITY_ORDER = {"P0": 0, "P1": 1, "P2": 2}


def _float_config(config: Mapping[str, str], key: str, default: float) -> float:
    try:
        return float(str(config.get(key, "")).strip())
    except ValueError:
        return default


def _int_config(config: Mapping[str, str], key: str, default: int) -> int:
    try:
        value = int(float(str(config.get(key, "")).strip()))
    except ValueError:
        return default
    return value if value >= 0 else default


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
```

- [ ] **Step 4: Run tests**

Run:

```bash
python3.11 -m unittest tests.test_alerting.AlertingTest -v
```

Expected result:

```text
test_alert_config_reads_thresholds_from_env_mapping ... ok
test_change_event_exposes_stable_dedupe_key ... ok
```

- [ ] **Step 5: Commit**

```bash
git add alerting.py tests/test_alerting.py
git commit -m "feat: add alert event model"
```

---

### Task 2: Convert Raw Snapshot Diffs Into Classified Events

**Files:**
- Modify: `alerting.py`
- Modify: `tests/test_alerting.py`

- [ ] **Step 1: Write failing tests for severity classification**

Append to `AlertingTest` in `tests/test_alerting.py`:

```python
    def test_build_change_events_classifies_promotion_loss_as_p0(self):
        previous = {
            "parents": {"PARENT1234": {"child_asins": ["CHILD00001"]}},
            "children": {"CHILD00001": {"promotion": "Limited time deal"}},
        }
        current = {
            "captured_at": "2026-07-13T01:15:00Z",
            "parents": {"PARENT1234": {"child_asins": ["CHILD00001"]}},
            "children": {"CHILD00001": {"promotion": ""}},
        }
        changes = ["CHILD00001 child promotion: Limited time deal -> "]

        events = alerting.build_change_events(previous, current, changes, alerting.AlertConfig())

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].severity, "P0")
        self.assertEqual(events[0].parent_asin, "PARENT1234")
        self.assertEqual(events[0].child_asin, "CHILD00001")
        self.assertEqual(events[0].category, "promotion")
        self.assertEqual(events[0].title, "CHILD00001 促销/Deal结束")
        self.assertEqual(events[0].action, "检查广告预算、价格竞争力和促销排期")

    def test_build_change_events_filters_small_price_change_to_p2(self):
        previous = {
            "parents": {"PARENT1234": {"child_asins": ["CHILD00001"]}},
            "children": {"CHILD00001": {"price": 20.0}},
        }
        current = {
            "captured_at": "2026-07-13T01:15:00Z",
            "parents": {"PARENT1234": {"child_asins": ["CHILD00001"]}},
            "children": {"CHILD00001": {"price": 20.4}},
        }
        changes = ["CHILD00001 child price: 20.0 -> 20.4"]

        events = alerting.build_change_events(previous, current, changes, alerting.AlertConfig())

        self.assertEqual(events[0].severity, "P2")
        self.assertEqual(events[0].category, "price")

    def test_build_change_events_classifies_large_price_change_as_p1_and_critical_as_p0(self):
        previous = {
            "parents": {"PARENT1234": {"child_asins": ["CHILD00001", "CHILD00002"]}},
            "children": {"CHILD00001": {"price": 20.0}, "CHILD00002": {"price": 20.0}},
        }
        current = {
            "captured_at": "2026-07-13T01:15:00Z",
            "parents": {"PARENT1234": {"child_asins": ["CHILD00001", "CHILD00002"]}},
            "children": {"CHILD00001": {"price": 21.2}, "CHILD00002": {"price": 23.0}},
        }
        changes = [
            "CHILD00001 child price: 20.0 -> 21.2",
            "CHILD00002 child price: 20.0 -> 23.0",
        ]

        events = alerting.build_change_events(previous, current, changes, alerting.AlertConfig())

        self.assertEqual([event.severity for event in events], ["P0", "P1"])
        self.assertEqual(events[0].child_asin, "CHILD00002")
        self.assertEqual(events[1].child_asin, "CHILD00001")

    def test_build_change_events_classifies_inventory_zero_as_p0_and_low_inventory_as_p1(self):
        previous = {
            "parents": {"PARENT1234": {"child_asins": ["CHILD00001", "CHILD00002"]}},
            "children": {"CHILD00001": {"inventory": 3}, "CHILD00002": {"inventory": 8}},
        }
        current = {
            "captured_at": "2026-07-13T01:15:00Z",
            "parents": {"PARENT1234": {"child_asins": ["CHILD00001", "CHILD00002"]}},
            "children": {"CHILD00001": {"inventory": 0}, "CHILD00002": {"inventory": 4}},
        }
        changes = [
            "CHILD00001 child inventory: 3 -> 0",
            "CHILD00002 child inventory: 8 -> 4",
        ]

        events = alerting.build_change_events(previous, current, changes, alerting.AlertConfig(low_inventory_threshold=5))

        self.assertEqual([event.severity for event in events], ["P0", "P1"])
        self.assertEqual(events[0].title, "CHILD00001 库存归零")
        self.assertEqual(events[1].title, "CHILD00002 低库存")

    def test_build_change_events_classifies_child_removed_and_inventory_only_as_p0(self):
        previous = {
            "parents": {"PARENT1234": {"child_asins": ["CHILD00001"], "inventory_only_asins": []}},
            "children": {"CHILD00001": {"inventory": 8}, "CHILD00002": {"inventory": 2}},
        }
        current = {
            "captured_at": "2026-07-13T01:15:00Z",
            "parents": {"PARENT1234": {"child_asins": [], "inventory_only_asins": ["CHILD00002"]}},
            "children": {"CHILD00001": {"inventory": 8}, "CHILD00002": {"inventory": 2}},
        }
        changes = [
            "PARENT1234 child removed: CHILD00001",
            "PARENT1234 inventory-only child added: CHILD00002",
        ]

        events = alerting.build_change_events(previous, current, changes, alerting.AlertConfig())

        self.assertEqual([event.severity for event in events], ["P0", "P0"])
        self.assertEqual(events[0].category, "availability")
        self.assertEqual(events[1].category, "availability")
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
python3.11 -m unittest tests.test_alerting.AlertingTest -v
```

Expected result:

```text
AttributeError: module 'alerting' has no attribute 'build_change_events'
```

- [ ] **Step 3: Implement raw diff parsing, parent lookup, and classification**

Add these functions to `alerting.py`:

```python
import re
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Iterable, List, Tuple


ASIN_RE = r"[A-Z0-9]{10}"


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _as_float(value: Any) -> Optional[float]:
    try:
        if value in {None, "", "None", "未知"}:
            return None
        return float(str(value).replace(",", "").strip())
    except ValueError:
        return None


def _as_int(value: Any) -> Optional[int]:
    number = _as_float(value)
    return None if number is None else int(number)


def _is_empty(value: Any) -> bool:
    return value in {None, "", "None", "未知", "无"}


def _pct_change(before: Any, after: Any) -> Optional[float]:
    old = _as_float(before)
    new = _as_float(after)
    if old is None or new is None or old == 0:
        return None
    return abs(new - old) / abs(old) * 100


def _parent_memberships(previous: Mapping[str, Any], current: Mapping[str, Any]) -> Dict[str, str]:
    memberships: Dict[str, str] = {}
    for snapshot in (previous, current):
        parents = snapshot.get("parents", {}) if isinstance(snapshot.get("parents"), Mapping) else {}
        for parent_asin, parent in parents.items():
            if not isinstance(parent, Mapping):
                continue
            for field in ("child_asins", "inventory_only_asins"):
                for child_asin in parent.get(field) or []:
                    memberships[str(child_asin)] = str(parent_asin)
    return memberships


def _event(
    *,
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
    before: str,
    after: str,
    raw: str,
    config: AlertConfig,
) -> ChangeEvent:
    if field == "inventory":
        current = _as_int(after)
        if current == 0:
            return _event(
                severity="P0",
                category="inventory",
                parent_asin=parent_asin,
                child_asin=child_asin,
                field=field,
                before=before,
                after=after,
                title=f"{child_asin} 库存归零",
                detail=f"库存：{before} -> {after}",
                action="检查补货、广告预算和前台可售状态",
                raw=raw,
            )
        if current is not None and 0 < current <= config.low_inventory_threshold:
            return _event(
                severity="P1",
                category="inventory",
                parent_asin=parent_asin,
                child_asin=child_asin,
                field=field,
                before=before,
                after=after,
                title=f"{child_asin} 低库存",
                detail=f"库存：{before} -> {after}",
                action="确认补货节奏和广告消耗",
                raw=raw,
            )
        return _event(
            severity="P2",
            category="inventory",
            parent_asin=parent_asin,
            child_asin=child_asin,
            field=field,
            before=before,
            after=after,
            title=f"{child_asin} 库存变化",
            detail=f"库存：{before} -> {after}",
            action="归档",
            raw=raw,
        )
    if field in {"coupon", "promotion"}:
        label = "Coupon" if field == "coupon" else "促销/Deal"
        changed_presence = _is_empty(before) != _is_empty(after)
        return _event(
            severity="P0" if changed_presence else "P1",
            category="promotion",
            parent_asin=parent_asin,
            child_asin=child_asin,
            field=field,
            before=before,
            after=after,
            title=f"{child_asin} {label}{'开始' if _is_empty(before) and not _is_empty(after) else '结束' if changed_presence else '变化'}",
            detail=f"{label}：{before or '无'} -> {after or '无'}",
            action="检查广告预算、价格竞争力和促销排期",
            raw=raw,
        )
    if field == "price":
        old = _as_float(before)
        new = _as_float(after)
        abs_delta = None if old is None or new is None else abs(new - old)
        pct_delta = _pct_change(before, after)
        severity = "P2"
        if pct_delta is not None and pct_delta >= config.critical_price_pct_threshold:
            severity = "P0"
        elif abs_delta is not None and pct_delta is not None and abs_delta >= config.price_abs_threshold and pct_delta >= config.price_pct_threshold:
            severity = "P1"
        return _event(
            severity=severity,
            category="price",
            parent_asin=parent_asin,
            child_asin=child_asin,
            field=field,
            before=before,
            after=after,
            title=f"{child_asin} 价格变化",
            detail=f"价格：{before} -> {after}",
            action="检查竞品价格、广告 ACOS 和预算",
            raw=raw,
        )
    if field == "delivery_promise":
        return _event(
            severity="P1",
            category="delivery",
            parent_asin=parent_asin,
            child_asin=child_asin,
            field=field,
            before=before,
            after=after,
            title=f"{child_asin} 配送时效变化",
            detail=f"配送时效：{before} -> {after}",
            action="检查库存、配送方式和转化率影响",
            raw=raw,
        )
    return _event(
        severity="P2",
        category=field,
        parent_asin=parent_asin,
        child_asin=child_asin,
        field=field,
        before=before,
        after=after,
        title=f"{child_asin} {field}变化",
        detail=f"{field}：{before} -> {after}",
        action="归档",
        raw=raw,
    )


def _classify_parent_field(parent_asin: str, field: str, before: str, after: str, raw: str, config: AlertConfig) -> ChangeEvent:
    severity = "P2"
    if field in {"major_rank", "minor_rank"}:
        pct_delta = _pct_change(before, after)
        if pct_delta is not None and pct_delta >= config.rank_pct_threshold:
            severity = "P1"
    title_by_field = {
        "major_rank": "大类排名变化",
        "minor_rank": "小类排名变化",
        "stars": "评分变化",
        "rating_count": "评论数变化",
    }
    return _event(
        severity=severity,
        category="rank" if field in {"major_rank", "minor_rank"} else "parent_metric",
        parent_asin=parent_asin,
        child_asin=None,
        field=field,
        before=before,
        after=after,
        title=f"{parent_asin} {title_by_field.get(field, field)}",
        detail=f"{title_by_field.get(field, field)}：{before} -> {after}",
        action="复核自然排名和广告表现" if severity == "P1" else "归档",
        raw=raw,
    )


def _classify_error(raw: str) -> ChangeEvent:
    lower = raw.lower()
    severity = "P0" if "前台数据缺失" in raw or "parent failed" in lower else "P1"
    asins = re.findall(ASIN_RE, raw)
    parent_asin = asins[0] if asins else None
    return _event(
        severity=severity,
        category="data_source",
        parent_asin=parent_asin,
        child_asin=None,
        field="error",
        before="",
        after=raw,
        title=f"{parent_asin or '数据源'} 异常",
        detail=raw,
        action="确认采集源是否影响今日判断",
        raw=raw,
    )


def build_change_events(
    previous: Optional[Mapping[str, Any]],
    current: Mapping[str, Any],
    changes: Iterable[str],
    config: AlertConfig,
) -> List[ChangeEvent]:
    previous_map = previous or {}
    memberships = _parent_memberships(previous_map, current)
    events: List[ChangeEvent] = []
    for raw_value in changes:
        raw = str(raw_value)
        parent_match = re.fullmatch(rf"({ASIN_RE}) parent ([a-z_]+): (.*) -> (.*)", raw)
        if parent_match:
            parent_asin, field, before, after = parent_match.groups()
            events.append(_classify_parent_field(parent_asin, field, before, after, raw, config))
            continue
        child_match = re.fullmatch(rf"({ASIN_RE}) child ([a-z_]+): (.*) -> (.*)", raw)
        if child_match:
            child_asin, field, before, after = child_match.groups()
            events.append(_classify_child_field(child_asin, memberships.get(child_asin), field, before, after, raw, config))
            continue
        relation_match = re.fullmatch(rf"({ASIN_RE}) child (added|removed): ({ASIN_RE})", raw)
        if relation_match:
            parent_asin, action, child_asin = relation_match.groups()
            removed = action == "removed"
            events.append(
                _event(
                    severity="P0" if removed else "P1",
                    category="availability",
                    parent_asin=parent_asin,
                    child_asin=child_asin,
                    field=f"child_{action}",
                    before="live" if removed else "",
                    after="" if removed else "live",
                    title=f"{child_asin} {'子体解绑' if removed else '新增子体'}",
                    detail=f"父体 {parent_asin} {'解绑' if removed else '新增'}子体 {child_asin}",
                    action="检查前台可售状态、变体关系和广告投放范围",
                    raw=raw,
                )
            )
            continue
        inventory_only_match = re.fullmatch(rf"({ASIN_RE}) inventory-only child (added|removed): ({ASIN_RE})", raw)
        if inventory_only_match:
            parent_asin, action, child_asin = inventory_only_match.groups()
            added = action == "added"
            events.append(
                _event(
                    severity="P0" if added else "P1",
                    category="availability",
                    parent_asin=parent_asin,
                    child_asin=child_asin,
                    field=f"inventory_only_{action}",
                    before="" if added else "inventory_only",
                    after="inventory_only" if added else "",
                    title=f"{child_asin} {'库存侧有货但前台不可售' if added else '库存侧异常解除'}",
                    detail=f"父体 {parent_asin} 库存侧异常子体 {child_asin} {'新增' if added else '移除'}",
                    action="检查前台链接、变体上架状态和库存同步",
                    raw=raw,
                )
            )
            continue
        events.append(_classify_error(raw))
    return sorted(events, key=lambda event: (SEVERITY_ORDER.get(event.severity, 9), event.parent_asin or "", event.child_asin or "", event.category, event.field))
```

- [ ] **Step 4: Run tests**

Run:

```bash
python3.11 -m unittest tests.test_alerting.AlertingTest -v
```

Expected result: all `AlertingTest` tests pass.

- [ ] **Step 5: Commit**

```bash
git add alerting.py tests/test_alerting.py
git commit -m "feat: classify monitor changes"
```

---

### Task 3: Add Dedupe History and Severity Filtering

**Files:**
- Modify: `alerting.py`
- Modify: `tests/test_alerting.py`

- [ ] **Step 1: Write failing tests for filtering and dedupe**

Append to `AlertingTest`:

```python
    def test_filter_events_keeps_only_min_severity_and_hides_p2(self):
        events = [
            alerting.ChangeEvent("P0", "inventory", "PARENT1234", "CHILD00001", "inventory", 1, 0, "p0", "detail", "act", "raw1"),
            alerting.ChangeEvent("P1", "price", "PARENT1234", "CHILD00002", "price", 10, 11, "p1", "detail", "act", "raw2"),
            alerting.ChangeEvent("P2", "rating_count", "PARENT1234", None, "rating_count", 1, 2, "p2", "detail", "act", "raw3"),
        ]

        filtered = alerting.filter_events(events, alerting.AlertConfig(min_severity="P1"))

        self.assertEqual([event.title for event in filtered], ["p0", "p1"])

    def test_apply_dedupe_suppresses_same_event_within_window(self):
        event = alerting.ChangeEvent(
            "P1",
            "price",
            "PARENT1234",
            "CHILD00001",
            "price",
            "20.0",
            "21.5",
            "CHILD00001 价格变化",
            "价格：20.0 -> 21.5",
            "检查竞品价格、广告 ACOS 和预算",
            "CHILD00001 child price: 20.0 -> 21.5",
        )
        history = {event.dedupe_key(): {"last_sent_on": "2026-07-13", "severity": "P1"}}

        fresh, updated = alerting.apply_dedupe([event], history, "2026-07-13T01:15:00Z", alerting.AlertConfig(dedupe_window_days=1))

        self.assertEqual(fresh, [])
        self.assertEqual(updated[event.dedupe_key()]["last_seen_on"], "2026-07-13")

    def test_apply_dedupe_allows_event_after_window(self):
        event = alerting.ChangeEvent(
            "P1",
            "price",
            "PARENT1234",
            "CHILD00001",
            "price",
            "20.0",
            "21.5",
            "CHILD00001 价格变化",
            "价格：20.0 -> 21.5",
            "检查竞品价格、广告 ACOS 和预算",
            "CHILD00001 child price: 20.0 -> 21.5",
        )
        history = {event.dedupe_key(): {"last_sent_on": "2026-07-11", "severity": "P1"}}

        fresh, updated = alerting.apply_dedupe([event], history, "2026-07-13T01:15:00Z", alerting.AlertConfig(dedupe_window_days=1))

        self.assertEqual(fresh, [event])
        self.assertEqual(updated[event.dedupe_key()]["last_sent_on"], "2026-07-13")
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
python3.11 -m unittest tests.test_alerting.AlertingTest -v
```

Expected result:

```text
AttributeError: module 'alerting' has no attribute 'filter_events'
```

- [ ] **Step 3: Implement severity filtering and dedupe**

Add to `alerting.py`:

```python
def filter_events(events: Iterable[ChangeEvent], config: AlertConfig) -> List[ChangeEvent]:
    max_order = SEVERITY_ORDER.get(config.min_severity, SEVERITY_ORDER["P1"])
    return [event for event in events if SEVERITY_ORDER.get(event.severity, 9) <= max_order]


def _event_date(captured_at: str) -> date:
    try:
        dt = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
    except ValueError:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone(timedelta(hours=8))).date()


def _parse_date(value: Any) -> Optional[date]:
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def apply_dedupe(
    events: Iterable[ChangeEvent],
    history: Mapping[str, Any],
    captured_at: str,
    config: AlertConfig,
) -> Tuple[List[ChangeEvent], Dict[str, Dict[str, str]]]:
    current_day = _event_date(captured_at)
    output: List[ChangeEvent] = []
    updated: Dict[str, Dict[str, str]] = {}
    for key, value in history.items():
        if not isinstance(value, Mapping):
            continue
        last_seen = _parse_date(value.get("last_seen_on"))
        last_sent = _parse_date(value.get("last_sent_on"))
        keep_date = last_seen or last_sent
        if keep_date is not None and (current_day - keep_date).days <= max(config.dedupe_window_days, 1):
            updated[str(key)] = {
                "last_seen_on": str(last_seen or keep_date),
                "last_sent_on": str(last_sent or keep_date),
                "severity": str(value.get("severity", "")),
            }
    for event in events:
        key = event.dedupe_key()
        prior = updated.get(key) or (history.get(key) if isinstance(history.get(key), Mapping) else None)
        last_sent = _parse_date(prior.get("last_sent_on")) if prior else None
        suppress = last_sent is not None and (current_day - last_sent).days <= config.dedupe_window_days
        if not suppress:
            output.append(event)
        updated[key] = {
            "last_seen_on": str(current_day),
            "last_sent_on": str(last_sent if suppress and last_sent else current_day),
            "severity": event.severity,
        }
    return output, updated
```

- [ ] **Step 4: Run tests**

Run:

```bash
python3.11 -m unittest tests.test_alerting.AlertingTest -v
```

Expected result: all `AlertingTest` tests pass.

- [ ] **Step 5: Commit**

```bash
git add alerting.py tests/test_alerting.py
git commit -m "feat: dedupe alert events"
```

---

### Task 4: Render One Action-Focused Feishu Summary

**Files:**
- Modify: `alerting.py`
- Modify: `tests/test_alerting.py`

- [ ] **Step 1: Write failing summary rendering tests**

Append to `AlertingTest`:

```python
    def test_render_text_summary_includes_only_p0_p1_and_full_report_link(self):
        events = [
            alerting.ChangeEvent("P0", "promotion", "PARENT1234", "CHILD00001", "promotion", "Deal", "", "CHILD00001 促销/Deal结束", "促销/Deal：Deal -> 无", "检查广告预算、价格竞争力和促销排期", "raw1"),
            alerting.ChangeEvent("P1", "price", "PARENT1234", "CHILD00002", "price", "20.0", "21.2", "CHILD00002 价格变化", "价格：20.0 -> 21.2", "检查竞品价格、广告 ACOS 和预算", "raw2"),
            alerting.ChangeEvent("P2", "rating_count", "PARENT1234", None, "rating_count", "10", "11", "P2 hidden", "评论数：10 -> 11", "归档", "raw3"),
        ]
        config = alerting.AlertConfig(full_report_url="https://github.example/actions/runs/1")

        summary = alerting.render_text_summary(events, "2026-07-13T01:15:00Z", config)

        self.assertIn("ASIN 每日重点提醒｜北京时间 2026-07-13 09:15:00", summary)
        self.assertIn("今日结论：P0 1 项｜P1 1 项", summary)
        self.assertIn("P0 必看", summary)
        self.assertIn("CHILD00001 促销/Deal结束", summary)
        self.assertIn("P1 复核", summary)
        self.assertIn("CHILD00002 价格变化", summary)
        self.assertIn("完整报告：https://github.example/actions/runs/1", summary)
        self.assertNotIn("P2 hidden", summary)
        self.assertNotIn("监控范围", summary)
        self.assertNotIn("低优先级", summary)

    def test_render_text_summary_returns_empty_when_no_alertable_events(self):
        events = [
            alerting.ChangeEvent("P2", "rating_count", "PARENT1234", None, "rating_count", "10", "11", "P2 hidden", "评论数：10 -> 11", "归档", "raw3"),
        ]

        summary = alerting.render_text_summary(events, "2026-07-13T01:15:00Z", alerting.AlertConfig(send_no_change=False))

        self.assertEqual(summary, "")
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
python3.11 -m unittest tests.test_alerting.AlertingTest -v
```

Expected result:

```text
AttributeError: module 'alerting' has no attribute 'render_text_summary'
```

- [ ] **Step 3: Implement text summary rendering**

Add to `alerting.py`:

```python
def format_report_time(value: Any) -> str:
    text = str(value or "").strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone(timedelta(hours=8))).strftime("北京时间 %Y-%m-%d %H:%M:%S")


def _section_lines(title: str, events: List[ChangeEvent], limit: int) -> List[str]:
    if not events:
        return []
    lines = ["", title]
    for index, event in enumerate(events[:limit], 1):
        parent = f"｜父体 {event.parent_asin}" if event.parent_asin else ""
        lines.append(f"{index}. {event.title}{parent}")
        lines.append(f"   {event.detail}")
        lines.append(f"   建议：{event.action}")
    hidden = len(events) - limit
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
    p1_limit = remaining if remaining > 0 else config.max_summary_items
    lines.extend(_section_lines("P1 复核：", p1, p1_limit))
    if config.full_report_url:
        lines.extend(["", f"完整报告：{config.full_report_url}"])
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests**

Run:

```bash
python3.11 -m unittest tests.test_alerting.AlertingTest -v
```

Expected result: all `AlertingTest` tests pass.

- [ ] **Step 5: Commit**

```bash
git add alerting.py tests/test_alerting.py
git commit -m "feat: render focused alert summary"
```

---

### Task 5: Add Feishu Card Payload Support With Text Fallback

**Files:**
- Modify: `monitor.py`
- Modify: `tests/test_monitor.py`

- [ ] **Step 1: Write failing Feishu card payload tests**

Add these tests near the existing Feishu tests in `tests/test_monitor.py`:

```python
    def test_feishu_card_payload_uses_optional_signature(self):
        card = {
            "config": {"wide_screen_mode": True},
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": "hello"}}],
            "header": {"title": {"tag": "plain_text", "content": "title"}, "template": "red"},
        }

        unsigned = monitor.feishu_card_payload(card, timestamp=123, secret="")
        signed = monitor.feishu_card_payload(card, timestamp=123, secret="secret")

        self.assertEqual(unsigned["msg_type"], "interactive")
        self.assertEqual(unsigned["card"], card)
        self.assertNotIn("sign", unsigned)
        self.assertEqual(signed["timestamp"], "123")
        self.assertIn("sign", signed)

    def test_build_feishu_alert_card_contains_summary_markdown(self):
        card = monitor.build_feishu_alert_card("ASIN 每日重点提醒\n\nP0 必看：\n1. CHILD00001 库存归零")

        self.assertEqual(card["config"]["wide_screen_mode"], True)
        self.assertEqual(card["header"]["template"], "red")
        self.assertEqual(card["header"]["title"]["content"], "ASIN 每日重点提醒")
        self.assertEqual(card["elements"][0]["tag"], "div")
        self.assertIn("P0 必看", card["elements"][0]["text"]["content"])

    def test_send_daily_alert_payload_falls_back_to_text_when_card_fails(self):
        card_payload = {"msg_type": "interactive", "card": {"elements": []}}
        text_payload = {"msg_type": "text", "content": {"text": "fallback"}}

        with patch("monitor.send_feishu_payload", side_effect=[monitor.MonitorError("card rejected"), None]) as send:
            monitor.send_daily_alert_payload(card_payload, text_payload, "https://example.com")

        self.assertEqual(send.call_count, 2)
        self.assertEqual(send.call_args_list[0].args[0], card_payload)
        self.assertEqual(send.call_args_list[1].args[0], text_payload)
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
python3.11 -m unittest tests.test_monitor.MonitorTest -v
```

Expected result:

```text
AttributeError: module 'monitor' has no attribute 'feishu_card_payload'
```

- [ ] **Step 3: Implement card payload helpers while preserving existing text helper**

Add these helpers below `feishu_payload` in `monitor.py`:

```python
def sign_feishu_payload(payload: Dict[str, Any], *, timestamp: Optional[int] = None, secret: str = "") -> Dict[str, Any]:
    out = dict(payload)
    if secret:
        ts = str(timestamp or int(time.time()))
        sign = hmac.new(f"{ts}\n{secret}".encode("utf-8"), b"", hashlib.sha256).digest()
        out.update({"timestamp": ts, "sign": base64.b64encode(sign).decode("ascii")})
    return out


def feishu_card_payload(card: Mapping[str, Any], *, timestamp: Optional[int] = None, secret: str = "") -> Dict[str, Any]:
    return sign_feishu_payload({"msg_type": "interactive", "card": dict(card)}, timestamp=timestamp, secret=secret)


def build_feishu_alert_card(summary_text: str) -> Dict[str, Any]:
    first_line = summary_text.splitlines()[0] if summary_text.splitlines() else "ASIN 每日重点提醒"
    template = "red" if "P0 " in summary_text else "orange"
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": first_line.split("｜", 1)[0]},
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": summary_text}},
        ],
    }
```

Then simplify the existing `feishu_payload` to use the shared signer:

```python
def feishu_payload(text: str, *, timestamp: Optional[int] = None, secret: str = "") -> Dict[str, Any]:
    return sign_feishu_payload({"msg_type": "text", "content": {"text": text}}, timestamp=timestamp, secret=secret)
```

Also add a generic payload sender below `send_feishu()`:

```python
def send_feishu_payload(payload: Mapping[str, Any], webhook_url: str) -> None:
    if not webhook_url:
        return
    response = http_json(
        "POST",
        webhook_url,
        payload,
        {"Content-Type": "application/json"},
        timeout=20,
    )
    if isinstance(response, Mapping):
        status = response.get("StatusCode", response.get("status_code"))
        if status not in {None, 0, "0"}:
            message = first_text(response.get("StatusMessage")) or first_text(response.get("status_message")) or "unknown error"
            raise MonitorError(f"Feishu response {status}: {message}")
```

- [ ] **Step 4: Add daily payload send fallback**

Add below `send_feishu_payload()`:

```python
def send_daily_alert_payload(primary_payload: Mapping[str, Any], fallback_payload: Optional[Mapping[str, Any]], webhook_url: str) -> None:
    try:
        send_feishu_payload(primary_payload, webhook_url)
    except MonitorError:
        if fallback_payload and primary_payload.get("msg_type") == "interactive":
            send_feishu_payload(fallback_payload, webhook_url)
            return
        raise
```

This function intentionally falls back only from card to text. A text-send failure should still fail the run, so `delivery.enc.json` is not updated after a real notification failure.

- [ ] **Step 5: Run Feishu tests**

Run:

```bash
python3.11 -m unittest tests.test_monitor.MonitorTest.test_feishu_payload_uses_optional_signature tests.test_monitor.MonitorTest.test_feishu_card_payload_uses_optional_signature tests.test_monitor.MonitorTest.test_build_feishu_alert_card_contains_summary_markdown tests.test_monitor.MonitorTest.test_send_daily_alert_payload_falls_back_to_text_when_card_fails -v
```

Expected result: all four tests pass.

- [ ] **Step 6: Commit**

```bash
git add monitor.py tests/test_monitor.py
git commit -m "feat: add Feishu card payloads"
```

---

### Task 6: Route Daily Runs Through Alert Events and Persist Dedupe

**Files:**
- Modify: `monitor.py`
- Modify: `tests/test_monitor.py`

- [ ] **Step 1: Write failing daily-routing tests**

Add these tests near the daily report tests in `tests/test_monitor.py`:

```python
    def test_daily_report_sends_one_focused_summary_and_persists_alert_history(self):
        key = base64.urlsafe_b64encode(os.urandom(32)).decode()
        previous = {
            "captured_at": "2026-07-12T01:15:00Z",
            "parents": {"PARENT1234": {"child_asins": ["CHILD00001"]}},
            "children": {"CHILD00001": {"promotion": "Limited time deal"}},
            "errors": [],
        }
        current = {
            "captured_at": "2026-07-13T01:15:00Z",
            "parents": {"PARENT1234": {"child_asins": ["CHILD00001"]}},
            "children": {"CHILD00001": {"promotion": ""}},
            "errors": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            state_path = os.path.join(directory, "latest.enc.json")
            delivery_path = os.path.join(directory, "delivery.enc.json")
            monitor.save_current(state_path, previous, key)
            with patch(
                "monitor.env_config",
                return_value={
                    "STATE_ENCRYPTION_KEY": key,
                    "FEISHU_WEBHOOK_URL": "https://example.com",
                    "FEISHU_WEBHOOK_SECRET": "",
                    "FEISHU_MESSAGE_MODE": "text",
                    "FULL_REPORT_URL": "https://github.example/actions/runs/1",
                },
            ), patch("monitor.now_iso", return_value="2026-07-13T01:15:00Z"), patch("monitor.collect_snapshot", return_value=current), patch("monitor.http_json", return_value={"StatusCode": 0}) as http:
                result = monitor.main(["--daily-report", "--state", state_path, "--output", state_path, "--delivery-state", delivery_path])

            self.assertEqual(result, 0)
            self.assertEqual(http.call_count, 1)
            payload = http.call_args.args[2]
            text = payload["content"]["text"]
            self.assertIn("ASIN 每日重点提醒", text)
            self.assertIn("P0 必看", text)
            self.assertIn("CHILD00001 促销/Deal结束", text)
            self.assertIn("完整报告：https://github.example/actions/runs/1", text)
            self.assertNotIn("ASIN 变化明细", text)
            saved = monitor.load_previous(state_path, key)
            self.assertIn("alert_history", saved)
            self.assertTrue(saved["alert_history"])

    def test_daily_report_does_not_send_when_only_p2_events_exist(self):
        key = base64.urlsafe_b64encode(os.urandom(32)).decode()
        previous = {
            "captured_at": "2026-07-12T01:15:00Z",
            "parents": {"PARENT1234": {"rating_count": 10, "child_asins": ["CHILD00001"]}},
            "children": {"CHILD00001": {}},
            "errors": [],
        }
        current = {
            "captured_at": "2026-07-13T01:15:00Z",
            "parents": {"PARENT1234": {"rating_count": 11, "child_asins": ["CHILD00001"]}},
            "children": {"CHILD00001": {}},
            "errors": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            state_path = os.path.join(directory, "latest.enc.json")
            delivery_path = os.path.join(directory, "delivery.enc.json")
            monitor.save_current(state_path, previous, key)
            with patch(
                "monitor.env_config",
                return_value={"STATE_ENCRYPTION_KEY": key, "FEISHU_WEBHOOK_URL": "https://example.com", "FEISHU_MESSAGE_MODE": "text"},
            ), patch("monitor.now_iso", return_value="2026-07-13T01:15:00Z"), patch("monitor.collect_snapshot", return_value=current), patch("monitor.http_json") as http:
                result = monitor.main(["--daily-report", "--state", state_path, "--output", state_path, "--delivery-state", delivery_path])

            self.assertEqual(result, 0)
            http.assert_not_called()
            self.assertEqual(monitor.load_delivery_date(delivery_path, key), "2026-07-13")
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
python3.11 -m unittest tests.test_monitor.MonitorTest.test_daily_report_sends_one_focused_summary_and_persists_alert_history tests.test_monitor.MonitorTest.test_daily_report_does_not_send_when_only_p2_events_exist -v
```

Expected result: failures because `main()` still sends legacy daily messages.

- [ ] **Step 3: Import `alerting` and include optional env config**

At the top of `monitor.py`, add:

```python
import alerting
```

In `env_config()`, add optional keys:

```python
        "ALERT_PRICE_PCT_THRESHOLD",
        "ALERT_CRITICAL_PRICE_PCT_THRESHOLD",
        "ALERT_PRICE_ABS_THRESHOLD",
        "ALERT_RANK_PCT_THRESHOLD",
        "ALERT_LOW_INVENTORY_THRESHOLD",
        "ALERT_DELIVERY_DAYS_THRESHOLD",
        "ALERT_MAX_SUMMARY_ITEMS",
        "ALERT_MIN_SEVERITY",
        "ALERT_DEDUPE_WINDOW_DAYS",
        "ALERT_SEND_NO_CHANGE",
        "FEISHU_MESSAGE_MODE",
        "FULL_REPORT_OUTPUT",
        "FULL_REPORT_URL",
```

- [ ] **Step 4: Add a daily alert payload builder**

Add below `format_daily_report_messages()` in `monitor.py`:

```python
def build_daily_alert_payload(
    previous: Optional[Mapping[str, Any]],
    current: Mapping[str, Any],
    changes: Sequence[str],
    config: Mapping[str, str],
) -> tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], Dict[str, Any]]:
    alert_config = alerting.AlertConfig.from_mapping(config)
    events = alerting.build_change_events(previous, current, changes, alert_config)
    visible = alerting.filter_events(events, alert_config)
    history = previous.get("alert_history", {}) if isinstance(previous, Mapping) and isinstance(previous.get("alert_history"), Mapping) else {}
    fresh, updated_history = alerting.apply_dedupe(visible, history, str(current.get("captured_at") or now_iso()), alert_config)
    summary = alerting.render_text_summary(fresh, str(current.get("captured_at") or now_iso()), alert_config)
    current_with_history = dict(current)
    current_with_history["alert_history"] = updated_history
    if not summary:
        return None, None, current_with_history
    fallback_payload = feishu_payload(summary, secret=config.get("FEISHU_WEBHOOK_SECRET", ""))
    if alert_config.feishu_message_mode == "card":
        return feishu_card_payload(build_feishu_alert_card(summary), secret=config.get("FEISHU_WEBHOOK_SECRET", "")), fallback_payload, current_with_history
    return fallback_payload, None, current_with_history
```

- [ ] **Step 5: Use the generic payload sender for daily alerts**

`send_feishu_payload()` and `send_daily_alert_payload()` were introduced in Task 5. If this task is executed independently, add them here before changing `main()`:

```python
def send_feishu_payload(payload: Mapping[str, Any], webhook_url: str) -> None:
    if not webhook_url:
        return
    response = http_json(
        "POST",
        webhook_url,
        payload,
        {"Content-Type": "application/json"},
        timeout=20,
    )
    if isinstance(response, Mapping):
        status = response.get("StatusCode", response.get("status_code"))
        if status not in {None, 0, "0"}:
            message = first_text(response.get("StatusMessage")) or first_text(response.get("status_message")) or "unknown error"
            raise MonitorError(f"Feishu response {status}: {message}")


def send_daily_alert_payload(primary_payload: Mapping[str, Any], fallback_payload: Optional[Mapping[str, Any]], webhook_url: str) -> None:
    try:
        send_feishu_payload(primary_payload, webhook_url)
    except MonitorError:
        if fallback_payload and primary_payload.get("msg_type") == "interactive":
            send_feishu_payload(fallback_payload, webhook_url)
            return
        raise
```

In `main()`, replace the daily branch:

```python
    if args.daily_report:
        messages = format_daily_report_messages(previous, snapshot_to_persist, changes)
```

with:

```python
    alert_payload: Optional[Dict[str, Any]] = None
    fallback_alert_payload: Optional[Dict[str, Any]] = None
    daily_evaluated = False
    if args.daily_report:
        alert_payload, fallback_alert_payload, snapshot_to_persist = build_daily_alert_payload(previous, snapshot_to_persist, changes, config)
        messages = []
        daily_evaluated = True
```

Then update the send block from:

```python
    if messages:
        if args.dry_run:
            print("\n\n---\n\n".join(messages))
        else:
            for message in messages:
                send_feishu(message, config["FEISHU_WEBHOOK_URL"], config.get("FEISHU_WEBHOOK_SECRET", ""))
                time.sleep(0.5)
            if args.daily_report:
                save_delivery_date(args.delivery_state, config["STATE_ENCRYPTION_KEY"], today)
```

to:

```python
    delivered = False
    if alert_payload is not None:
        if args.dry_run:
            print(json.dumps(alert_payload, ensure_ascii=False, indent=2))
            delivered = True
        else:
            send_daily_alert_payload(alert_payload, fallback_alert_payload, config["FEISHU_WEBHOOK_URL"])
            delivered = True
    elif messages:
        if args.dry_run:
            print("\n\n---\n\n".join(messages))
            delivered = True
        else:
            for message in messages:
                send_feishu(message, config["FEISHU_WEBHOOK_URL"], config.get("FEISHU_WEBHOOK_SECRET", ""))
                time.sleep(0.5)
            delivered = True
    if args.daily_report and not args.dry_run and daily_evaluated and (alert_payload is None or delivered):
        save_delivery_date(args.delivery_state, config["STATE_ENCRYPTION_KEY"], today)
```

- [ ] **Step 6: Run daily-routing tests**

Run:

```bash
python3.11 -m unittest tests.test_monitor.MonitorTest.test_daily_report_sends_one_focused_summary_and_persists_alert_history tests.test_monitor.MonitorTest.test_daily_report_does_not_send_when_only_p2_events_exist -v
```

Expected result: both tests pass.

- [ ] **Step 7: Commit**

```bash
git add monitor.py tests/test_monitor.py
git commit -m "feat: send focused daily alerts"
```

---

### Task 7: Keep Full Reports as Files and Artifacts, Not Feishu Messages

**Files:**
- Modify: `monitor.py`
- Modify: `tests/test_monitor.py`
- Modify: `.github/workflows/daily-monitor.yml`

- [ ] **Step 1: Write failing full-report-output test**

Add to `tests/test_monitor.py`:

```python
    def test_daily_report_writes_full_report_without_sending_it_to_feishu(self):
        key = base64.urlsafe_b64encode(os.urandom(32)).decode()
        previous = {
            "captured_at": "2026-07-12T01:15:00Z",
            "parents": {"PARENT1234": {"child_asins": ["CHILD00001"]}},
            "children": {"CHILD00001": {"promotion": "Limited time deal"}},
            "errors": [],
        }
        current = {
            "captured_at": "2026-07-13T01:15:00Z",
            "parents": {"PARENT1234": {"child_asins": ["CHILD00001"], "major_rank": 10}},
            "children": {"CHILD00001": {"promotion": "", "price": 20.0}},
            "errors": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            state_path = os.path.join(directory, "latest.enc.json")
            delivery_path = os.path.join(directory, "delivery.enc.json")
            full_report_path = os.path.join(directory, "full-report.txt")
            monitor.save_current(state_path, previous, key)
            with patch(
                "monitor.env_config",
                return_value={
                    "STATE_ENCRYPTION_KEY": key,
                    "FEISHU_WEBHOOK_URL": "https://example.com",
                    "FEISHU_MESSAGE_MODE": "text",
                    "FULL_REPORT_OUTPUT": full_report_path,
                },
            ), patch("monitor.now_iso", return_value="2026-07-13T01:15:00Z"), patch("monitor.collect_snapshot", return_value=current), patch("monitor.http_json", return_value={"StatusCode": 0}) as http:
                result = monitor.main(["--daily-report", "--state", state_path, "--output", state_path, "--delivery-state", delivery_path])

            self.assertEqual(result, 0)
            self.assertTrue(os.path.exists(full_report_path))
            full_report = open(full_report_path, encoding="utf-8").read()
            self.assertIn("ASIN 每日监控", full_report)
            self.assertIn("ASIN 变化明细", full_report)
            sent_text = http.call_args.args[2]["content"]["text"]
            self.assertNotIn("ASIN 变化明细", sent_text)
```

- [ ] **Step 2: Run test to verify failure**

Run:

```bash
python3.11 -m unittest tests.test_monitor.MonitorTest.test_daily_report_writes_full_report_without_sending_it_to_feishu -v
```

Expected result: failure because `FULL_REPORT_OUTPUT` is not written.

- [ ] **Step 3: Implement full report writing**

Add helper in `monitor.py`:

```python
def write_text_file(path: str, text: str) -> None:
    if not path:
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
```

In `main()`, immediately after computing `changes`, add:

```python
    if args.daily_report and config.get("FULL_REPORT_OUTPUT"):
        write_text_file(config["FULL_REPORT_OUTPUT"], "\n\n---\n\n".join(format_daily_report_messages(previous, snapshot_to_persist, changes)))
```

This call must run before `build_daily_alert_payload()` mutates `snapshot_to_persist` with `alert_history`, so the full report reflects business snapshot changes and not alert bookkeeping.

- [ ] **Step 4: Run full-report-output test**

Run:

```bash
python3.11 -m unittest tests.test_monitor.MonitorTest.test_daily_report_writes_full_report_without_sending_it_to_feishu -v
```

Expected result: pass.

- [ ] **Step 5: Update GitHub Actions workflow**

In `.github/workflows/daily-monitor.yml`, add env vars:

```yaml
      ALERT_PRICE_PCT_THRESHOLD: ${{ secrets.ALERT_PRICE_PCT_THRESHOLD || '5' }}
      ALERT_CRITICAL_PRICE_PCT_THRESHOLD: ${{ secrets.ALERT_CRITICAL_PRICE_PCT_THRESHOLD || '10' }}
      ALERT_PRICE_ABS_THRESHOLD: ${{ secrets.ALERT_PRICE_ABS_THRESHOLD || '1' }}
      ALERT_RANK_PCT_THRESHOLD: ${{ secrets.ALERT_RANK_PCT_THRESHOLD || '20' }}
      ALERT_LOW_INVENTORY_THRESHOLD: ${{ secrets.ALERT_LOW_INVENTORY_THRESHOLD || '5' }}
      ALERT_DELIVERY_DAYS_THRESHOLD: ${{ secrets.ALERT_DELIVERY_DAYS_THRESHOLD || '2' }}
      ALERT_MAX_SUMMARY_ITEMS: ${{ secrets.ALERT_MAX_SUMMARY_ITEMS || '10' }}
      ALERT_MIN_SEVERITY: ${{ secrets.ALERT_MIN_SEVERITY || 'P1' }}
      ALERT_DEDUPE_WINDOW_DAYS: ${{ secrets.ALERT_DEDUPE_WINDOW_DAYS || '1' }}
      ALERT_SEND_NO_CHANGE: ${{ secrets.ALERT_SEND_NO_CHANGE || 'false' }}
      FEISHU_MESSAGE_MODE: ${{ secrets.FEISHU_MESSAGE_MODE || 'card' }}
      FULL_REPORT_OUTPUT: state-report.txt
      FULL_REPORT_URL: https://github.com/${{ github.repository }}/actions/runs/${{ github.run_id }}
```

Add an upload step after `Run monitor`:

```yaml
      - name: Upload full daily report
        if: env.DAILY_REPORT == 'true' && env.RENDER_STATE_ONLY != 'true' && hashFiles('state-report.txt') != ''
        uses: actions/upload-artifact@v4
        with:
          name: asin-full-daily-report
          path: state-report.txt
```

- [ ] **Step 6: Commit**

```bash
git add monitor.py tests/test_monitor.py .github/workflows/daily-monitor.yml
git commit -m "feat: archive full daily reports"
```

---

### Task 8: Update Existing Daily Report Tests for the New Default Contract

**Files:**
- Modify: `tests/test_monitor.py`

- [ ] **Step 1: Keep full report function tests intact**

Do not rewrite `test_daily_report_without_changes_is_a_single_summary`, `test_daily_report_includes_only_changed_parent_details`, or `test_daily_parent_detail_embeds_changes_except_inventory_inline_marks` to assert Feishu behavior. These tests cover `format_daily_report_messages()` as the complete report renderer, and that renderer remains useful for artifacts and `--render-state-only`.

- [ ] **Step 2: Update main-path tests that assume daily sends complete report messages**

For `test_partial_daily_report_merges_and_persists_snapshot`, keep the merge assertions but add `FEISHU_MESSAGE_MODE: "text"` to the patched config and assert one focused alert payload or no payload depending on its fixture severity.

Use this config shape:

```python
{
    "STATE_ENCRYPTION_KEY": key,
    "FEISHU_WEBHOOK_URL": "https://example.com",
    "FEISHU_MESSAGE_MODE": "text",
    "ALERT_MIN_SEVERITY": "P1",
}
```

If the existing fixture only produces inventory changes above the low-inventory threshold, expect `send_feishu` or `http_json` not to be called. If it produces a P1/P0 event, expect exactly one call.

- [ ] **Step 3: Run the full test suite**

Run:

```bash
python3.11 -m unittest discover -s tests -v
```

Expected result:

```text
OK
```

- [ ] **Step 4: Commit**

```bash
git add tests/test_monitor.py
git commit -m "test: align daily alert expectations"
```

---

### Task 9: End-to-End Dry Run and Workflow Validation

**Files:**
- Modify only if validation exposes a concrete issue: `monitor.py`, `alerting.py`, `.github/workflows/daily-monitor.yml`, tests.

- [ ] **Step 1: Run unit tests**

Run:

```bash
python3.11 -m unittest discover -s tests -v
```

Expected result:

```text
OK
```

- [ ] **Step 2: Run a dry daily report with current encrypted state**

Run with real required env vars loaded:

```bash
FEISHU_MESSAGE_MODE=text FULL_REPORT_OUTPUT=state-report.txt FULL_REPORT_URL=https://example.invalid/actions/runs/local python3.11 monitor.py --daily-report --dry-run --state state/latest.enc.json --output state/latest.enc.json --delivery-state state/delivery.enc.json
```

Expected result:

- stdout is either one JSON text payload or empty if only P2/no events exist.
- `state-report.txt` exists.
- `state-report.txt` contains the complete daily report, including full detail sections.
- stdout does not contain `ASIN 变化明细`.

- [ ] **Step 3: Validate card payload in dry run**

Run:

```bash
FEISHU_MESSAGE_MODE=card FULL_REPORT_OUTPUT=state-report.txt FULL_REPORT_URL=https://example.invalid/actions/runs/local python3.11 monitor.py --daily-report --dry-run --force-daily-report --state state/latest.enc.json --output state/latest.enc.json --delivery-state state/delivery.enc.json
```

Expected result:

- stdout is JSON with `"msg_type": "interactive"` when there are non-deduped P0/P1 events or no output if there are no alertable events.
- If stdout contains a payload, it has a `card.header.title.content` value of `ASIN 每日重点提醒`.

- [ ] **Step 4: Validate GitHub Actions YAML syntax locally if available**

Run:

```bash
python3.11 - <<'PY'
import pathlib
text = pathlib.Path(".github/workflows/daily-monitor.yml").read_text()
for required in ["FULL_REPORT_OUTPUT", "FULL_REPORT_URL", "Upload full daily report", "ALERT_MIN_SEVERITY"]:
    assert required in text, required
print("workflow checks ok")
PY
```

Expected result:

```text
workflow checks ok
```

- [ ] **Step 5: Commit validation fixes if any**

If validation required code changes, commit them:

```bash
git add monitor.py alerting.py tests/test_monitor.py tests/test_alerting.py .github/workflows/daily-monitor.yml
git commit -m "fix: validate advanced alert flow"
```

If validation required no code changes, do not create an empty commit.

---

## Final Verification Checklist

- [ ] `python3.11 -m unittest discover -s tests -v` passes.
- [ ] Daily run sends at most one Feishu payload.
- [ ] Daily Feishu payload contains only P0/P1 events.
- [ ] Daily Feishu payload does not include "监控范围", "数据质量", "低优先级", or P2 counts.
- [ ] Complete report is written to `FULL_REPORT_OUTPUT`.
- [ ] Complete report is uploaded as the `asin-full-daily-report` artifact in GitHub Actions.
- [ ] `alert_history` is persisted inside encrypted state.
- [ ] Repeated same-day events are suppressed according to `ALERT_DEDUPE_WINDOW_DAYS`.
- [ ] `--render-state-only` still prints the complete report.
- [ ] Existing Feishu text webhook behavior remains compatible.

## Rollback Plan

If Feishu card mode fails in production, set this secret:

```text
FEISHU_MESSAGE_MODE=text
```

If alert filtering hides too much, set:

```text
ALERT_MIN_SEVERITY=P2
```

If dedupe suppresses too aggressively, set:

```text
ALERT_DEDUPE_WINDOW_DAYS=0
```

If the advanced alert path must be disabled quickly, temporarily remove `--daily-report` from the scheduled workflow or set `ALERT_SEND_NO_CHANGE=false` and `ALERT_MIN_SEVERITY=P0` while keeping state collection intact.

## Self-Review

- Spec coverage: ChangeEvent layer is covered in Tasks 1-2; one-message Feishu summary is covered in Tasks 4 and 6; P0/P1-only expansion is covered in Tasks 2, 3, 4, and 6; thresholds and dedupe are covered in Tasks 1-3; full report outside Feishu is covered in Task 7; GitHub artifact/card advanced path is covered in Tasks 5 and 7.
- Placeholder scan: no task uses unresolved placeholders; each code-changing task names exact files, tests, commands, and expected outcomes.
- Type consistency: `AlertConfig`, `ChangeEvent`, `build_change_events`, `filter_events`, `apply_dedupe`, and `render_text_summary` are introduced before `monitor.py` imports and uses them.
