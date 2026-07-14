# Parent ASIN Excel Report Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a GitHub Actions Excel artifact where filtering the `父 ASIN` column shows the selected parent row plus all related child ASIN rows, and fill the missing LD/BD Deal discount percentage as structured data.

**Architecture:** Reuse the existing structured snapshot in `monitor.py`; do not parse the text report. Normalize Deal label and Deal discount percentage into child snapshot fields, then add a workbook writer that flattens each parent into one parent row plus one row per normal child and inventory-only child.

**Tech Stack:** Python 3.11, `unittest`, `openpyxl`, GitHub Actions `actions/upload-artifact@v4`.

**Execution status:** Implemented on branch `codex/parent-asin-excel-deal-discount` in these commits:

- `8610b6d` `feat: capture deal discount percentages`
- `bee01a1` `feat: add parent asin workbook writer`
- `cc1bd34` `feat: write excel report artifacts`
- `7614650` `chore: upload excel report artifacts`

---

## Current State

- `monitor.py` already stores structured data as `snapshot["parents"]` and `snapshot["children"]`.
- `normalize_promotion()` recognizes Deal labels and direct `savingsPercentage`, but it stores percentage inside the `promotion` string and does not preserve a separate `promotion_discount_pct` field.
- `format_daily_report_messages()` and `format_snapshot_report()` currently render human-readable txt.
- `.github/workflows/daily-monitor.yml` uploads only `state-report.txt` for daily reports and render-state-only reports.
- `requirements.txt` does not include an Excel writer.

## Target Deal Discount Contract

Add child field `promotion_discount_pct`.

Rules:

- Normalize Deal aliases: `LTD` -> `Limited time deal`, `LD` -> `Lightning Deal`, `BD` -> `Best Deal`, `DOTD` -> `Deal of the Day`.
- Extract the first percentage from direct or nested Deal discount fields such as `savingsPercentage`, `savings_percentage`, `discountPercentage`, `discount_percentage`, `dealDiscountPercentage`, `deal_discount_percentage`, `dealPercentage`, `deal_percentage`, `percent`, `percentage`, `discountPercent`, and `discount_percent`.
- Also detect percentages embedded in Deal-bearing text fields such as `promotion`, `deal`, `dealBadge`, `dealType`, `discountTypes`, `badge`, and `promotions`.
- Do not treat coupon-only text as Deal discount percentage unless a Deal label is also present.
- Preserve the existing `promotion` display string, but expose `promotion_discount_pct` separately for diffing, txt detail, and Excel filtering.

## Target Workbook Contract

Create `state-report.xlsx` with first sheet `父体筛选明细`.

The sheet must have one filterable table. Column A is `父 ASIN`, and every row related to the same parent repeats that same parent ASIN:

1. One `父体` row per parent ASIN.
2. One `正常子体` row for each ASIN in `parent["child_asins"]`.
3. One `库存侧异常子体` row for each ASIN in `parent["inventory_only_asins"]`.

Required columns, in this exact order:

```text
父 ASIN
行类型
子 ASIN
子体状态
采集时间
报告状态
大类排名
大类类目
小类排名
小类类目
评分
评论数
正常子体数
库存侧异常数
价格
库存
Coupon
促销/Deal
Deal 折扣百分比
配送时效
高退货提示
前台状态
前台来源
库存来源
变化摘要
数据源摘要
```

Usability requirements:

- `ws.auto_filter.ref` covers the whole populated range.
- `ws.freeze_panes = "A2"`.
- Header row is bold and filled.
- Column widths are set so ASIN, status, source, and summary fields are readable.
- The artifact keeps the existing txt output; Excel is additive.

## File Structure

- Modify `monitor.py`
  - Add Deal alias and discount percentage extraction helpers.
  - Add `promotion_discount_pct` to normalized child snapshots and `CHILD_FIELDS`.
  - Show `Deal折扣` in per-parent txt detail rows.
  - Add workbook row-building helpers near the existing report-formatting helpers.
  - Add `write_parent_filter_workbook(path, previous, current, changes)`.
  - Read `FULL_REPORT_XLSX_OUTPUT` from env/config.
  - Write the workbook in `--daily-report` and `--render-state-only` paths.
- Modify `requirements.txt`
  - Add `openpyxl>=3.1,<4`.
- Modify `tests/test_monitor.py`
  - Add Deal discount normalization and display tests.
  - Add workbook contract tests.
  - Add integration tests for daily report and render-state-only workbook output.
- Modify `.github/workflows/daily-monitor.yml`
  - Set `FULL_REPORT_XLSX_OUTPUT: state-report.xlsx`.
  - Upload both `state-report.txt` and `state-report.xlsx`.
- Modify `README.md`
  - Document the new Excel artifact and env var.

---

### Task 1: Normalize Deal Discount Percentage

**Files:**
- Modify: `monitor.py`
- Modify: `tests/test_monitor.py`

- [ ] **Step 1: Write failing tests for Deal percentage extraction**

In `tests/test_monitor.py`, add these tests after `test_normalizes_pangolin_discount_types_savings_and_promotions`:

```python
    def test_normalizes_deal_discount_percentage_from_alias_and_nested_fields(self):
        child = monitor.normalize_child(
            "B0GJZYZHJJ",
            {
                "asin": "B0GJZYZHJJ",
                "price": "$20.24",
                "dealType": "BD",
                "deal": {"savingsPercentage": "18%"},
            },
            None,
            "pangolin",
        )

        self.assertEqual(child["promotion"], "Best Deal; 18% off")
        self.assertEqual(child["promotion_discount_pct"], "18%")

    def test_normalizes_lightning_deal_discount_percentage_without_percent_symbol(self):
        child = monitor.normalize_child(
            "B0GJZYZHJJ",
            {
                "asin": "B0GJZYZHJJ",
                "price": "$20.24",
                "discountTypes": ["LD"],
                "discountPercentage": "23",
            },
            None,
            "pangolin",
        )

        self.assertEqual(child["promotion"], "Lightning Deal; 23% off")
        self.assertEqual(child["promotion_discount_pct"], "23%")

    def test_coupon_only_percentage_is_not_treated_as_deal_discount(self):
        child = monitor.normalize_child(
            "B0GJZYZHJJ",
            {"asin": "B0GJZYZHJJ", "price": "$20.24", "coupon": "10% coupon"},
            None,
            "pangolin",
        )

        self.assertEqual(child["promotion"], "")
        self.assertIsNone(child["promotion_discount_pct"])
```

- [ ] **Step 2: Write failing tests for diff and txt display**

In `tests/test_monitor.py`, add this test after `test_daily_report_includes_only_changed_parent_details`:

```python
    def test_daily_report_shows_deal_discount_percentage_changes(self):
        previous = {
            "captured_at": "2026-07-09T01:15:00Z",
            "parents": {
                "PARENT1234": {"major_rank": 100, "stars": 4.5, "child_asins": ["CHILD00001"], "source": "pangolin"}
            },
            "children": {
                "CHILD00001": {
                    "price": 10.0,
                    "inventory": 5,
                    "promotion": "Lightning Deal",
                    "promotion_discount_pct": "10%",
                    "delivery_promise": "Wednesday, July 15",
                }
            },
            "errors": [],
        }
        current = {
            "captured_at": "2026-07-10T01:15:00Z",
            "parents": {
                "PARENT1234": {"major_rank": 100, "stars": 4.5, "child_asins": ["CHILD00001"], "source": "pangolin"}
            },
            "children": {
                "CHILD00001": {
                    "price": 10.0,
                    "inventory": 5,
                    "promotion": "Lightning Deal",
                    "promotion_discount_pct": "23%",
                    "delivery_promise": "Wednesday, July 15",
                }
            },
            "errors": [],
        }

        changes = monitor.diff_snapshots(previous, current)
        detail = monitor.format_parent_snapshot_report(current, "PARENT1234", current["parents"]["PARENT1234"], current["children"], previous, changes)

        self.assertIn("CHILD00001 child promotion_discount_pct: 10% -> 23%", changes)
        self.assertIn("Deal折扣 23%（10%→23%）", detail)
```

- [ ] **Step 3: Run the focused tests to verify they fail**

Run:

```bash
python3.11 -m unittest \
  tests.test_monitor.MonitorTest.test_normalizes_deal_discount_percentage_from_alias_and_nested_fields \
  tests.test_monitor.MonitorTest.test_normalizes_lightning_deal_discount_percentage_without_percent_symbol \
  tests.test_monitor.MonitorTest.test_coupon_only_percentage_is_not_treated_as_deal_discount \
  tests.test_monitor.MonitorTest.test_daily_report_shows_deal_discount_percentage_changes \
  -v
```

Expected: failures because `promotion_discount_pct` and Deal alias normalization do not exist yet.

- [ ] **Step 4: Add Deal aliases, percent extraction, and child field**

In `monitor.py`, change `CHILD_FIELDS` near the top to:

```python
CHILD_FIELDS = (
    "price",
    "coupon",
    "promotion",
    "promotion_discount_pct",
    "inventory",
    "delivery_promise",
)
```

Add these constants after `ASIN_PATTERN`:

```python
DEAL_LABEL_ALIASES = {
    "LTD": "Limited time deal",
    "LD": "Lightning Deal",
    "BD": "Best Deal",
    "DOTD": "Deal of the Day",
}
DEAL_PERCENT_KEYS = {
    "savingspercentage",
    "savings_percentage",
    "discountpercentage",
    "discount_percentage",
    "dealdiscountpercentage",
    "deal_discount_percentage",
    "dealpercentage",
    "deal_percentage",
    "discountpercent",
    "discount_percent",
    "percent",
    "percentage",
}
DEAL_TEXT_KEYS = {"promotion", "deal", "dealbadge", "dealtype", "discounttypes", "badge", "promotions"}
PERCENT_PATTERN = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*%")
```

Add these helpers after `parse_float()`:

```python
def normalize_percent_text(value: Any) -> Optional[str]:
    text = first_text(value)
    if not text:
        return None
    percent_match = PERCENT_PATTERN.search(text)
    if percent_match:
        return f"{percent_match.group(1)}%"
    number = parse_float(text)
    if number is None:
        return None
    if number.is_integer():
        return f"{int(number)}%"
    return f"{number}%"


def normalize_deal_label(value: Any) -> Optional[str]:
    if isinstance(value, Mapping):
        for key in ("type", "dealType", "text", "value", "name", "title", "label"):
            text = normalize_deal_label(value.get(key))
            if text:
                return text
        return None
    text = first_text(value)
    if not text:
        return None
    alias = DEAL_LABEL_ALIASES.get(text.upper())
    return alias or text


def detail_has_deal_label(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(detail_has_deal_label(child) for child in value.values())
    if isinstance(value, list):
        return any(detail_has_deal_label(child) for child in value)
    text = normalize_deal_label(value)
    if not text:
        return False
    lower = text.lower()
    return any(label in lower for label in ("limited time deal", "lightning deal", "best deal", "deal of the day", "prime member price", "prime exclusive"))


def extract_deal_discount_pct(detail: Mapping[str, Any]) -> Optional[str]:
    def scan(value: Any, key_hint: str = "") -> Optional[str]:
        normalized_key = key_hint.replace("-", "_").lower()
        if normalized_key in DEAL_PERCENT_KEYS:
            percent = normalize_percent_text(value)
            if percent:
                return percent
        if isinstance(value, Mapping):
            for key, child in value.items():
                found = scan(child, str(key))
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = scan(child, key_hint)
                if found:
                    return found
        elif key_hint.replace("-", "").lower() in DEAL_TEXT_KEYS:
            return normalize_percent_text(value)
        return None

    if not any(detail_has_deal_label(detail.get(key)) for key in ("promotion", "deal", "dealBadge", "dealType", "discountTypes", "badge", "promotions")):
        return None
    found = scan(detail)
    if found:
        return found
    for key in ("promotion", "deal", "dealBadge", "dealType", "discountTypes", "badge", "promotions"):
        found = scan(detail.get(key), key)
        if found:
            return found
    return None
```

- [ ] **Step 5: Update promotion normalization**

Replace `normalize_promotion()` with:

```python
def normalize_promotion(detail: Mapping[str, Any], discount_pct: Optional[str] = None) -> Optional[str]:
    parts: List[str] = []

    def add(value: Any) -> None:
        text = normalize_deal_label(value)
        if not text:
            return
        lower = text.lower()
        if "amazon's choice" in lower or "amazon choice" in lower or "best seller" in lower:
            return
        if text not in parts:
            parts.append(text)

    for key in ("promotion", "deal", "dealBadge", "dealType", "discountTypes"):
        for item in listify(detail.get(key)):
            add(item)
    badge = first_text(detail.get("badge"))
    if badge and (badge.upper() in DEAL_LABEL_ALIASES or any(label in badge.lower() for label in ("limited time deal", "lightning deal", "best deal", "deal of the day", "prime member price"))):
        add(badge)
    savings = discount_pct or normalize_percent_text(detail.get("savingsPercentage"))
    if savings:
        add(f"{savings} off")
    for item in listify(detail.get("promotions")):
        if isinstance(item, Mapping):
            quantity = first_text(item.get("quantity"))
            discount = first_text(item.get("discount"))
            add(f"Buy {quantity} save {discount}" if quantity and discount else item)
        else:
            add(item)
    return "; ".join(parts) if parts else None
```

In `normalize_child()`, replace:

```python
    promotion = normalize_promotion(detail)
```

with:

```python
    promotion_discount_pct = extract_deal_discount_pct(detail)
    promotion = normalize_promotion(detail, promotion_discount_pct)
```

Add this row in the returned child dict after `promotion`:

```python
        "promotion_discount_pct": promotion_discount_pct,
```

- [ ] **Step 6: Update field labels and txt display**

In `FIELD_LABELS`, add:

```python
    "promotion_discount_pct": "Deal折扣",
```

In `format_parent_snapshot_report()`, replace the child detail line segment:

```python
            f"促销 {format_optional_text(child.get('promotion'), child)}{markers.get('promotion', '')}｜"
            f"时效 {format_delivery_days(child.get('delivery_promise'), captured_at)}{markers.get('delivery_promise', '')}"
```

with:

```python
            f"促销 {format_optional_text(child.get('promotion'), child)}{markers.get('promotion', '')}｜"
            f"Deal折扣 {format_coverage(child.get('promotion_discount_pct'), child)}{markers.get('promotion_discount_pct', '')}｜"
            f"时效 {format_delivery_days(child.get('delivery_promise'), captured_at)}{markers.get('delivery_promise', '')}"
```

In `tests/test_monitor.py`, update `test_formats_compact_report_without_internal_source_noise` by replacing:

```python
        self.assertIn("B0FFT34472｜价 85.53｜库存 295｜Coupon 无｜促销 7-Day Deal｜时效 未覆盖", message)
```

with:

```python
        self.assertIn("B0FFT34472｜价 85.53｜库存 295｜Coupon 无｜促销 7-Day Deal｜Deal折扣 未覆盖｜时效 未覆盖", message)
```

- [ ] **Step 7: Run the focused tests to verify they pass**

Run:

```bash
python3.11 -m unittest \
  tests.test_monitor.MonitorTest.test_normalizes_deal_discount_percentage_from_alias_and_nested_fields \
  tests.test_monitor.MonitorTest.test_normalizes_lightning_deal_discount_percentage_without_percent_symbol \
  tests.test_monitor.MonitorTest.test_coupon_only_percentage_is_not_treated_as_deal_discount \
  tests.test_monitor.MonitorTest.test_daily_report_shows_deal_discount_percentage_changes \
  -v
```

Expected: `OK`.

- [ ] **Step 8: Commit Task 1**

Run:

```bash
git add monitor.py tests/test_monitor.py
git commit -m "feat: capture deal discount percentages"
```

---

### Task 2: Add Workbook Writer Contract

**Files:**
- Modify: `requirements.txt`
- Modify: `monitor.py`
- Modify: `tests/test_monitor.py`

- [ ] **Step 1: Add Excel dependency**

Change `requirements.txt` to:

```text
cryptography>=42.0,<43
mcp>=1.9
openpyxl>=3.1,<4
```

- [ ] **Step 2: Write the failing workbook contract test**

In `tests/test_monitor.py`, add this import near the existing imports:

```python
from openpyxl import load_workbook
```

Add this test method inside `MonitorTest`, after `test_full_report_output_writes_original_daily_report_without_sending_it`:

```python
    def test_write_parent_filter_workbook_includes_parent_child_and_inventory_rows(self):
        previous = self._daily_previous_snapshot()
        current = self._daily_current_snapshot(
            parents={
                "PARENT1234": {
                    "major_rank": 130,
                    "major_category": "Home & Kitchen",
                    "minor_rank": 20,
                    "minor_category": "Milk Frothers",
                    "stars": 4.5,
                    "rating_count": 100,
                    "child_asins": ["CHILD00001"],
                    "inventory_only_asins": ["CHILD00002"],
                    "source": "pangolin",
                    "inventory_source": "xingshang",
                }
            },
            children={
                "CHILD00001": {
                    "price": 23.0,
                    "coupon": "",
                    "promotion": "Limited time deal",
                    "promotion_discount_pct": "23%",
                    "inventory": 0,
                    "delivery_promise": "Wednesday, July 15",
                    "frequently_returned": False,
                    "source": "pangolin",
                    "inventory_source": "xingshang",
                },
                "CHILD00002": {
                    "inventory": 4,
                    "front_status": "不可售/404",
                    "source": "xingshang_inventory_only",
                    "inventory_source": "xingshang",
                },
            },
        )
        changes = monitor.diff_snapshots(previous, current)

        with tempfile.TemporaryDirectory() as directory:
            workbook_path = os.path.join(directory, "state-report.xlsx")

            monitor.write_parent_filter_workbook(workbook_path, previous, current, changes)

            workbook = load_workbook(workbook_path)
            self.assertIn("父体筛选明细", workbook.sheetnames)
            worksheet = workbook["父体筛选明细"]
            headers = [cell.value for cell in worksheet[1]]
            self.assertEqual(
                headers,
                [
                    "父 ASIN",
                    "行类型",
                    "子 ASIN",
                    "子体状态",
                    "采集时间",
                    "报告状态",
                    "大类排名",
                    "大类类目",
                    "小类排名",
                    "小类类目",
                    "评分",
                    "评论数",
                    "正常子体数",
                    "库存侧异常数",
                    "价格",
                    "库存",
                    "Coupon",
                    "促销/Deal",
                    "Deal 折扣百分比",
                    "配送时效",
                    "高退货提示",
                    "前台状态",
                    "前台来源",
                    "库存来源",
                    "变化摘要",
                    "数据源摘要",
                ],
            )
            self.assertEqual(worksheet.freeze_panes, "A2")
            self.assertEqual(worksheet.auto_filter.ref, worksheet.dimensions)

            rows = list(worksheet.iter_rows(min_row=2, values_only=True))
            self.assertEqual([row[0] for row in rows], ["PARENT1234", "PARENT1234", "PARENT1234"])
            self.assertEqual([row[1] for row in rows], ["父体", "正常子体", "库存侧异常子体"])
            self.assertEqual(rows[1][2], "CHILD00001")
            self.assertEqual(rows[1][3], "正常")
            self.assertEqual(rows[1][14], 23.0)
            self.assertEqual(rows[1][15], 0)
            self.assertEqual(rows[1][17], "Limited time deal")
            self.assertEqual(rows[1][18], "23%")
            self.assertIn("价格", rows[1][24])
            self.assertEqual(rows[2][2], "CHILD00002")
            self.assertEqual(rows[2][3], "库存侧异常")
            self.assertEqual(rows[2][21], "不可售/404")
```

- [ ] **Step 3: Run the focused test to verify it fails**

Run:

```bash
python3.11 -m unittest tests.test_monitor.MonitorTest.test_write_parent_filter_workbook_includes_parent_child_and_inventory_rows -v
```

Expected: `ERROR` with `AttributeError: module 'monitor' has no attribute 'write_parent_filter_workbook'`.

- [ ] **Step 4: Add workbook writer implementation**

In `monitor.py`, add this code after `format_daily_report_messages()` and before `env_config()`:

```python
WORKBOOK_HEADERS = [
    "父 ASIN",
    "行类型",
    "子 ASIN",
    "子体状态",
    "采集时间",
    "报告状态",
    "大类排名",
    "大类类目",
    "小类排名",
    "小类类目",
    "评分",
    "评论数",
    "正常子体数",
    "库存侧异常数",
    "价格",
    "库存",
    "Coupon",
    "促销/Deal",
    "Deal 折扣百分比",
    "配送时效",
    "高退货提示",
    "前台状态",
    "前台来源",
    "库存来源",
    "变化摘要",
    "数据源摘要",
]


def workbook_value(value: Any, *, empty: str = "无", unknown: str = "未知") -> Any:
    if value is None:
        return unknown
    if value == "":
        return empty
    if isinstance(value, bool):
        return "是" if value else "否"
    return value


def workbook_change_summary(
    parent_asin: str,
    child_asin: Optional[str],
    previous: Optional[Mapping[str, Any]],
    current: Mapping[str, Any],
    changes: Sequence[str],
) -> str:
    if not previous or not changes:
        return ""
    captured_at = current.get("captured_at") or now_iso()
    parts: List[str] = []
    for raw in changes:
        line = str(raw)
        parent_match = re.fullmatch(r"([A-Z0-9]{10}) parent ([a-z_]+): (.*) -> (.*)", line)
        if parent_match and child_asin is None:
            asin, field, before, after = parent_match.groups()
            if asin == parent_asin:
                label = FIELD_LABELS.get(field, field)
                parts.append(f"{label}：{format_change_field_value(field, before, captured_at)} → {format_change_field_value(field, after, captured_at)}")
            continue

        child_match = re.fullmatch(r"([A-Z0-9]{10}) child ([a-z_]+): (.*) -> (.*)", line)
        if child_match and child_asin is not None:
            asin, field, before, after = child_match.groups()
            if asin == child_asin:
                label = FIELD_LABELS.get(field, field)
                parts.append(f"{label}：{format_change_field_value(field, before, captured_at)} → {format_change_field_value(field, after, captured_at)}")
            continue

        relation_match = re.fullmatch(r"([A-Z0-9]{10}) child (added|removed): ([A-Z0-9]{10})", line)
        if relation_match:
            asin, action, changed_child = relation_match.groups()
            if asin == parent_asin and (child_asin is None or changed_child == child_asin):
                label = "新增子体" if action == "added" else "解绑子体"
                parts.append(f"{label}：{changed_child}")
            continue

        inventory_match = re.fullmatch(r"([A-Z0-9]{10}) inventory-only child (added|removed): ([A-Z0-9]{10})", line)
        if inventory_match:
            asin, action, changed_child = inventory_match.groups()
            if asin == parent_asin and (child_asin is None or changed_child == child_asin):
                label = "新增库存侧异常" if action == "added" else "移除库存侧异常"
                parts.append(f"{label}：{changed_child}")
            continue

        if child_asin is None and parent_asin in line:
            parts.append(f"数据源异常：{line}")
    return "；".join(parts)


def parent_workbook_rows(
    snapshot: Mapping[str, Any],
    previous: Optional[Mapping[str, Any]] = None,
    changes: Sequence[str] = (),
) -> List[List[Any]]:
    captured_at = snapshot.get("captured_at") or now_iso()
    parents = snapshot.get("parents", {}) if isinstance(snapshot.get("parents"), Mapping) else {}
    children = snapshot.get("children", {}) if isinstance(snapshot.get("children"), Mapping) else {}
    rows: List[List[Any]] = []

    for parent_asin in sorted(parents):
        parent = parents[parent_asin]
        if not isinstance(parent, Mapping):
            continue
        child_asins = sorted(str(asin) for asin in parent.get("child_asins") or [])
        inventory_only_asins = sorted(str(asin) for asin in parent.get("inventory_only_asins") or [])
        common = [
            parent_asin,
            captured_at,
            report_status(snapshot),
            workbook_value(parent.get("major_rank")),
            workbook_value(parent.get("major_category")),
            workbook_value(parent.get("minor_rank")),
            workbook_value(parent.get("minor_category")),
            workbook_value(parent.get("stars")),
            workbook_value(parent.get("rating_count")),
            len(child_asins),
            len(inventory_only_asins),
        ]
        source_summary = format_source_summary(snapshot, parent, children)
        rows.append(
            [
                common[0],
                "父体",
                "",
                "",
                common[1],
                common[2],
                common[3],
                common[4],
                common[5],
                common[6],
                common[7],
                common[8],
                common[9],
                common[10],
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "" if has_front_detail(parent) else "前台数据缺失",
                format_source(parent.get("source")),
                format_source(parent.get("inventory_source")) if parent.get("inventory_source") else "未覆盖",
                workbook_change_summary(parent_asin, None, previous, snapshot, changes),
                source_summary,
            ]
        )

        for child_asin in child_asins:
            child = children.get(child_asin, {})
            child_row = child if isinstance(child, Mapping) else {}
            rows.append(
                [
                    common[0],
                    "正常子体",
                    child_asin,
                    "正常",
                    common[1],
                    common[2],
                    common[3],
                    common[4],
                    common[5],
                    common[6],
                    common[7],
                    common[8],
                    common[9],
                    common[10],
                    workbook_value(child_row.get("price")),
                    workbook_value(child_row.get("inventory")),
                    format_optional_text(child_row.get("coupon"), child_row),
                    format_optional_text(child_row.get("promotion"), child_row),
                    workbook_value(child_row.get("promotion_discount_pct"), unknown="未覆盖"),
                    format_delivery_days(child_row.get("delivery_promise"), captured_at),
                    workbook_value(child_row.get("frequently_returned"), unknown="未覆盖"),
                    workbook_value(child_row.get("front_status"), unknown=""),
                    format_source(child_row.get("source")),
                    format_source(child_row.get("inventory_source")) if child_row.get("inventory_source") else "未覆盖",
                    workbook_change_summary(parent_asin, child_asin, previous, snapshot, changes),
                    source_summary,
                ]
            )

        for child_asin in inventory_only_asins:
            child = children.get(child_asin, {})
            child_row = child if isinstance(child, Mapping) else {}
            rows.append(
                [
                    common[0],
                    "库存侧异常子体",
                    child_asin,
                    "库存侧异常",
                    common[1],
                    common[2],
                    common[3],
                    common[4],
                    common[5],
                    common[6],
                    common[7],
                    common[8],
                    common[9],
                    common[10],
                    workbook_value(child_row.get("price")),
                    workbook_value(child_row.get("inventory")),
                    format_optional_text(child_row.get("coupon"), child_row),
                    format_optional_text(child_row.get("promotion"), child_row),
                    workbook_value(child_row.get("promotion_discount_pct"), unknown="未覆盖"),
                    format_delivery_days(child_row.get("delivery_promise"), captured_at),
                    workbook_value(child_row.get("frequently_returned"), unknown="未覆盖"),
                    workbook_value(child_row.get("front_status"), unknown="不可售/404"),
                    format_source(child_row.get("source")),
                    format_source(child_row.get("inventory_source")) if child_row.get("inventory_source") else "未覆盖",
                    workbook_change_summary(parent_asin, child_asin, previous, snapshot, changes),
                    source_summary,
                ]
            )
    return rows


def write_parent_filter_workbook(
    path: str,
    previous: Optional[Mapping[str, Any]],
    current: Mapping[str, Any],
    changes: Sequence[str],
) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "父体筛选明细"
    worksheet.append(WORKBOOK_HEADERS)
    for row in parent_workbook_rows(current, previous, changes):
        worksheet.append(row)

    header_fill = PatternFill("solid", fgColor="D9EAF7")
    for cell in worksheet[1]:
        cell.font = Font(bold=True)
        cell.fill = header_fill
        cell.alignment = Alignment(vertical="center")

    worksheet.freeze_panes = "A2"
    worksheet.auto_filter.ref = worksheet.dimensions
    worksheet.column_dimensions["A"].width = 14
    worksheet.column_dimensions["B"].width = 16
    worksheet.column_dimensions["C"].width = 14
    worksheet.column_dimensions["D"].width = 16
    worksheet.column_dimensions["E"].width = 22
    worksheet.column_dimensions["F"].width = 24
    for index in range(7, 21):
        worksheet.column_dimensions[get_column_letter(index)].width = 14
    worksheet.column_dimensions["U"].width = 14
    worksheet.column_dimensions["V"].width = 18
    worksheet.column_dimensions["W"].width = 18
    worksheet.column_dimensions["X"].width = 18
    worksheet.column_dimensions["Y"].width = 48
    worksheet.column_dimensions["Z"].width = 44
    for row in worksheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)

    workbook.save(path)
```

- [ ] **Step 5: Run the focused test to verify it passes**

Run:

```bash
python3.11 -m unittest tests.test_monitor.MonitorTest.test_write_parent_filter_workbook_includes_parent_child_and_inventory_rows -v
```

Expected: `OK`.

- [ ] **Step 6: Commit Task 2**

Run:

```bash
git add requirements.txt monitor.py tests/test_monitor.py
git commit -m "feat: add parent asin workbook writer"
```

---

### Task 3: Wire Workbook Output Into Report Modes

**Files:**
- Modify: `monitor.py`
- Modify: `tests/test_monitor.py`
- Modify: `alerting.py`
- Modify: `tests/test_alerting.py`

- [ ] **Step 1: Extend config tests**

In `tests/test_alerting.py`, update `test_alert_config_reads_thresholds_from_env_mapping` so the mapping includes:

```python
                "FULL_REPORT_XLSX_OUTPUT": "state-report.xlsx",
```

Add this assertion:

```python
        self.assertEqual(config.full_report_xlsx_output, "state-report.xlsx")
```

- [ ] **Step 2: Extend `AlertConfig`**

In `alerting.py`, add the field after `full_report_output`:

```python
    full_report_xlsx_output: str = ""
```

In `AlertConfig.from_mapping()`, add this argument after `full_report_output`:

```python
            full_report_xlsx_output=str(config.get("FULL_REPORT_XLSX_OUTPUT", "")).strip(),
```

- [ ] **Step 3: Add integration test for daily report workbook output**

In `tests/test_monitor.py`, add this test after `test_full_report_output_writes_original_daily_report_without_sending_it`:

```python
    def test_full_report_xlsx_output_writes_filterable_workbook_without_sending_it(self):
        key = base64.urlsafe_b64encode(os.urandom(32)).decode()
        previous = self._daily_previous_snapshot()
        current = self._daily_current_snapshot()
        with tempfile.TemporaryDirectory() as directory:
            state_path = os.path.join(directory, "latest.enc.json")
            delivery_path = os.path.join(directory, "delivery.enc.json")
            workbook_path = os.path.join(directory, "reports", "daily.xlsx")
            monitor.save_current(state_path, previous, key)

            with (
                patch("monitor.env_config", return_value=self._daily_config(key, FULL_REPORT_XLSX_OUTPUT=workbook_path)),
                patch("monitor.now_iso", return_value="2026-07-10T01:15:00Z"),
                patch("monitor.collect_snapshot", return_value=current),
                patch("monitor.send_daily_alert_payload"),
            ):
                result = monitor.main(["--daily-report", "--state", state_path, "--output", state_path, "--delivery-state", delivery_path])

            self.assertEqual(result, 0)
            workbook = load_workbook(workbook_path)
            worksheet = workbook["父体筛选明细"]
            self.assertEqual(worksheet["A1"].value, "父 ASIN")
            self.assertEqual(worksheet["A2"].value, "PARENT1234")
            self.assertEqual(worksheet["A3"].value, "PARENT1234")
            self.assertEqual(worksheet["B2"].value, "父体")
            self.assertEqual(worksheet["B3"].value, "正常子体")
```

- [ ] **Step 4: Add integration test for render-state-only workbook output**

In `tests/test_monitor.py`, add this test near `test_render_state_only_with_previous_state_prints_daily_change_report`:

```python
    def test_render_state_only_writes_filterable_workbook_when_configured(self):
        key = base64.urlsafe_b64encode(os.urandom(32)).decode()
        previous = self._daily_previous_snapshot()
        current = self._daily_current_snapshot()
        with tempfile.TemporaryDirectory() as directory:
            state_path = os.path.join(directory, "latest.enc.json")
            previous_path = os.path.join(directory, "previous.enc.json")
            workbook_path = os.path.join(directory, "state-report.xlsx")
            monitor.save_current(state_path, current, key)
            monitor.save_current(previous_path, previous, key)
            stdout = io.StringIO()

            with (
                patch.dict(os.environ, {"STATE_ENCRYPTION_KEY": key, "FULL_REPORT_XLSX_OUTPUT": workbook_path}, clear=True),
                patch("monitor.collect_snapshot") as collect,
                patch("sys.stdout", stdout),
            ):
                result = monitor.main(["--state", state_path, "--previous-state", previous_path, "--render-state-only"])

            self.assertEqual(result, 0)
            collect.assert_not_called()
            self.assertIn("ASIN 每日监控", stdout.getvalue())
            worksheet = load_workbook(workbook_path)["父体筛选明细"]
            self.assertEqual(worksheet["A1"].value, "父 ASIN")
            self.assertEqual(worksheet["A2"].value, "PARENT1234")
            self.assertEqual(worksheet["A3"].value, "PARENT1234")
```

- [ ] **Step 5: Run the new integration tests to verify they fail**

Run:

```bash
python3.11 -m unittest \
  tests.test_alerting.AlertingTest.test_alert_config_reads_thresholds_from_env_mapping \
  tests.test_monitor.MonitorTest.test_full_report_xlsx_output_writes_filterable_workbook_without_sending_it \
  tests.test_monitor.MonitorTest.test_render_state_only_writes_filterable_workbook_when_configured \
  -v
```

Expected:

- The alert config test fails until `full_report_xlsx_output` exists.
- The daily report test fails until `FULL_REPORT_XLSX_OUTPUT` is read and written.
- The render-state-only test fails until that path writes the workbook.

- [ ] **Step 6: Read workbook env var in `env_config()`**

In `monitor.py`, add this optional key after `FULL_REPORT_OUTPUT`:

```python
        "FULL_REPORT_XLSX_OUTPUT",
```

- [ ] **Step 7: Write workbook in `--render-state-only`**

Replace the `args.render_state_only` branch in `monitor.py` with this implementation:

```python
    if args.render_state_only:
        key = os.environ.get("STATE_ENCRYPTION_KEY", "")
        if not key:
            raise MonitorError("missing required env vars: STATE_ENCRYPTION_KEY")
        snapshot = load_previous(args.state, key)
        if snapshot is None:
            raise MonitorError(f"state file not found: {args.state}")
        previous = load_previous(args.previous_state, key)
        if previous is not None:
            changes = diff_snapshots(previous, snapshot)
            print("\n\n---\n\n".join(format_daily_report_messages(previous, snapshot, changes)))
        else:
            changes = []
            print(format_snapshot_report(snapshot))
        workbook_output = os.environ.get("FULL_REPORT_XLSX_OUTPUT", "").strip()
        if workbook_output:
            write_parent_filter_workbook(workbook_output, previous, snapshot, changes)
        return 0
```

- [ ] **Step 8: Write workbook in `--daily-report`**

In the `if args.daily_report:` block, immediately after writing `alert_config.full_report_output`, add:

```python
        if alert_config.full_report_xlsx_output:
            write_parent_filter_workbook(alert_config.full_report_xlsx_output, previous, snapshot_to_persist, changes)
```

- [ ] **Step 9: Run the new integration tests to verify they pass**

Run:

```bash
python3.11 -m unittest \
  tests.test_alerting.AlertingTest.test_alert_config_reads_thresholds_from_env_mapping \
  tests.test_monitor.MonitorTest.test_full_report_xlsx_output_writes_filterable_workbook_without_sending_it \
  tests.test_monitor.MonitorTest.test_render_state_only_writes_filterable_workbook_when_configured \
  -v
```

Expected: `OK`.

- [ ] **Step 10: Commit Task 3**

Run:

```bash
git add alerting.py monitor.py tests/test_alerting.py tests/test_monitor.py
git commit -m "feat: write excel report artifacts"
```

---

### Task 4: Upload Excel Artifact And Document It

**Files:**
- Modify: `.github/workflows/daily-monitor.yml`
- Modify: `README.md`

- [x] **Step 1: Configure workbook output in Actions**

In `.github/workflows/daily-monitor.yml`, add this env var after `FULL_REPORT_OUTPUT: state-report.txt`:

```yaml
      FULL_REPORT_XLSX_OUTPUT: state-report.xlsx
```

- [x] **Step 2: Upload txt and xlsx together for daily reports**

Replace the daily upload block:

```yaml
      - name: Upload full daily report
        if: always() && env.DAILY_REPORT == 'true' && env.RENDER_STATE_ONLY != 'true' && hashFiles('state-report.txt') != ''
        uses: actions/upload-artifact@v4
        with:
          name: asin-full-daily-report
          path: state-report.txt
```

with:

```yaml
      - name: Upload full daily report
        if: always() && env.DAILY_REPORT == 'true' && env.RENDER_STATE_ONLY != 'true' && hashFiles('state-report.txt') != ''
        uses: actions/upload-artifact@v4
        with:
          name: asin-full-daily-report
          path: |
            state-report.txt
            state-report.xlsx
```

- [x] **Step 3: Upload txt and xlsx together for render-state-only reports**

Replace the rendered-state upload block:

```yaml
      - name: Upload rendered state report
        if: env.RENDER_STATE_ONLY == 'true'
        uses: actions/upload-artifact@v4
        with:
          name: asin-rendered-state-report
          path: state-report.txt
```

with:

```yaml
      - name: Upload rendered state report
        if: env.RENDER_STATE_ONLY == 'true'
        uses: actions/upload-artifact@v4
        with:
          name: asin-rendered-state-report
          path: |
            state-report.txt
            state-report.xlsx
```

- [x] **Step 4: Document the Excel artifact**

In `README.md`, update the default Feishu strategy section by replacing:

```markdown
- 完整日报写入 `FULL_REPORT_OUTPUT`，在 GitHub Actions 中上传为 `asin-full-daily-report` artifact。
```

with:

```markdown
- 完整日报写入 `FULL_REPORT_OUTPUT`，父 ASIN 可筛选明细写入 `FULL_REPORT_XLSX_OUTPUT`，在 GitHub Actions 中一起上传为 `asin-full-daily-report` artifact。
- Excel 第一张表 `父体筛选明细` 的 `父 ASIN` 列可直接筛选；筛选后会保留该父体行、正常子体行和库存侧异常子体行。
- LD / BD 等 Deal 的具体折扣百分比会写入 `Deal 折扣百分比` 列，并在完整 txt 明细中显示为 `Deal折扣`。
```

Add this env var row after `FULL_REPORT_OUTPUT`:

```markdown
| `FULL_REPORT_XLSX_OUTPUT` | empty | 父 ASIN 可筛选 Excel 输出路径；Actions 默认 `state-report.xlsx`。 |
```

Update the local dry-run example by adding:

```bash
FULL_REPORT_XLSX_OUTPUT=state-report.xlsx \
```

after `FULL_REPORT_OUTPUT=state-report.txt \`.

- [ ] **Step 5: Run the full test suite**

Run:

```bash
python3.11 -m unittest discover -s tests -v
```

Expected: all tests pass.

- [ ] **Step 6: Run a local dry-run artifact smoke test**

Run:

```bash
FEISHU_MESSAGE_MODE=card \
FULL_REPORT_OUTPUT=/tmp/asin-state-report.txt \
FULL_REPORT_XLSX_OUTPUT=/tmp/asin-state-report.xlsx \
FULL_REPORT_URL=https://example.invalid/actions/runs/local \
python3.11 monitor.py --daily-report --dry-run --force-daily-report \
  --state state/latest.enc.json \
  --output /tmp/asin-latest.enc.json \
  --delivery-state /tmp/asin-delivery.enc.json
```

Expected:

- `/tmp/asin-state-report.txt` exists.
- `/tmp/asin-state-report.xlsx` exists.
- Opening `/tmp/asin-state-report.xlsx` shows sheet `父体筛选明细`.
- Filtering column `父 ASIN` to one value keeps that parent's parent row plus child rows.
- Child rows with LD / BD / LTD / DOTD Deal data show `Deal 折扣百分比`.

- [ ] **Step 7: Commit Task 4**

Run:

```bash
git add .github/workflows/daily-monitor.yml README.md
git commit -m "chore: upload excel report artifacts"
```

---

## Final Verification

Run:

```bash
python3.11 -m unittest discover -s tests -v
```

Expected: `OK`.

Run:

```bash
git status --short
```

Expected: clean working tree after the four commits.

## Rollout Notes

- Existing Feishu behavior stays unchanged.
- Existing `state-report.txt` artifact stays unchanged.
- Child snapshots gain `promotion_discount_pct`; this is an additive state field used for diffing and reporting.
- New user flow: open GitHub Actions run artifact `asin-full-daily-report`, download `state-report.xlsx`, open sheet `父体筛选明细`, filter `父 ASIN`.
- No encrypted state envelope change is required because Excel is generated from the already persisted snapshot.

## Self-Review

- Spec coverage: The plan adds a real Excel artifact, makes `父 ASIN` filterable, includes both normal children and inventory-only children, and fills LD/BD Deal discount percentages as a structured field.
- Placeholder scan: The plan contains concrete file paths, test names, commands, expected failures, and implementation code.
- Type consistency: `write_parent_filter_workbook(path, previous, current, changes)` is used consistently by direct tests, daily-report wiring, and render-state-only wiring.
