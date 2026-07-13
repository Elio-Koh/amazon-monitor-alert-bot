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

    def test_alert_config_clamps_negative_minimum_values(self):
        config = alerting.AlertConfig.from_mapping(
            {
                "ALERT_MAX_SUMMARY_ITEMS": "-5",
                "ALERT_DEDUPE_WINDOW_DAYS": "-5",
            }
        )

        self.assertEqual(config.max_summary_items, 1)
        self.assertEqual(config.dedupe_window_days, 0)

    def test_alert_config_falls_back_for_invalid_sanitized_values(self):
        config = alerting.AlertConfig.from_mapping(
            {
                "ALERT_MAX_SUMMARY_ITEMS": "not-a-number",
                "ALERT_DEDUPE_WINDOW_DAYS": "not-a-number",
                "ALERT_MIN_SEVERITY": "P9",
                "FEISHU_MESSAGE_MODE": "markdown",
            }
        )

        self.assertEqual(config.max_summary_items, 10)
        self.assertEqual(config.dedupe_window_days, 1)
        self.assertEqual(config.min_severity, "P1")
        self.assertEqual(config.feishu_message_mode, "card")

    def test_alert_config_falls_back_for_non_finite_numeric_values(self):
        config = alerting.AlertConfig.from_mapping(
            {
                "ALERT_PRICE_PCT_THRESHOLD": "nan",
                "ALERT_CRITICAL_PRICE_PCT_THRESHOLD": "inf",
                "ALERT_MAX_SUMMARY_ITEMS": "inf",
                "ALERT_DEDUPE_WINDOW_DAYS": "inf",
            }
        )

        self.assertEqual(config.price_pct_threshold, 5.0)
        self.assertEqual(config.critical_price_pct_threshold, 10.0)
        self.assertEqual(config.max_summary_items, 10)
        self.assertEqual(config.dedupe_window_days, 1)

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

    def test_change_event_dedupe_key_uses_empty_slots_for_none_values(self):
        event = alerting.ChangeEvent(
            severity="P1",
            category="price",
            parent_asin=None,
            child_asin=None,
            field="price",
            before=None,
            after=None,
            title="price changed",
            detail="price changed",
            action="review price",
            raw="price changed",
        )

        self.assertEqual(event.dedupe_key(), "P1|price|||price||")

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

    def test_build_change_events_classifies_small_price_change_as_p2(self):
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

    def test_build_change_events_sorts_large_price_changes_by_severity(self):
        previous = {
            "parents": {"PARENT1234": {"child_asins": ["CHILD00001", "CHILD00002"]}},
            "children": {"CHILD00001": {"price": 20.0}, "CHILD00002": {"price": 20.0}},
        }
        current = {
            "captured_at": "2026-07-13T01:15:00Z",
            "parents": {"PARENT1234": {"child_asins": ["CHILD00001", "CHILD00002"]}},
            "children": {"CHILD00001": {"price": 21.2}, "CHILD00002": {"price": 23.0}},
        }
        changes = ["CHILD00001 child price: 20.0 -> 21.2", "CHILD00002 child price: 20.0 -> 23.0"]

        events = alerting.build_change_events(previous, current, changes, alerting.AlertConfig())

        self.assertEqual([event.severity for event in events], ["P0", "P1"])
        self.assertEqual(events[0].child_asin, "CHILD00002")
        self.assertEqual(events[1].child_asin, "CHILD00001")

    def test_build_change_events_classifies_inventory_zero_and_low_inventory(self):
        previous = {
            "parents": {"PARENT1234": {"child_asins": ["CHILD00001", "CHILD00002"]}},
            "children": {"CHILD00001": {"inventory": 3}, "CHILD00002": {"inventory": 8}},
        }
        current = {
            "captured_at": "2026-07-13T01:15:00Z",
            "parents": {"PARENT1234": {"child_asins": ["CHILD00001", "CHILD00002"]}},
            "children": {"CHILD00001": {"inventory": 0}, "CHILD00002": {"inventory": 4}},
        }
        changes = ["CHILD00001 child inventory: 3 -> 0", "CHILD00002 child inventory: 8 -> 4"]

        events = alerting.build_change_events(previous, current, changes, alerting.AlertConfig())

        self.assertEqual([event.severity for event in events], ["P0", "P1"])
        self.assertEqual([event.title for event in events], ["CHILD00001 库存归零", "CHILD00002 低库存"])

    def test_build_change_events_classifies_removed_and_inventory_only_added_as_p0_availability(self):
        previous = {
            "parents": {"PARENT1234": {"child_asins": ["CHILD00001"], "inventory_only_asins": []}},
            "children": {"CHILD00001": {"inventory": 8}, "CHILD00002": {"inventory": 2}},
        }
        current = {
            "captured_at": "2026-07-13T01:15:00Z",
            "parents": {"PARENT1234": {"child_asins": [], "inventory_only_asins": ["CHILD00002"]}},
            "children": {"CHILD00001": {"inventory": 8}, "CHILD00002": {"inventory": 2}},
        }
        changes = ["PARENT1234 child removed: CHILD00001", "PARENT1234 inventory-only child added: CHILD00002"]

        events = alerting.build_change_events(previous, current, changes, alerting.AlertConfig())

        self.assertEqual([event.severity for event in events], ["P0", "P0"])
        self.assertEqual([event.category for event in events], ["availability", "availability"])

    def test_build_change_events_labels_promotion_start_and_change(self):
        previous = {
            "parents": {"PARENT1234": {"child_asins": ["CHILD00001", "CHILD00002"]}},
            "children": {"CHILD00001": {"promotion": ""}, "CHILD00002": {"promotion": "7-Day Deal"}},
        }
        current = {
            "parents": {"PARENT1234": {"child_asins": ["CHILD00001", "CHILD00002"]}},
            "children": {"CHILD00001": {"promotion": "7-Day Deal"}, "CHILD00002": {"promotion": "Limited time deal"}},
        }
        changes = [
            "CHILD00001 child promotion:  -> 7-Day Deal",
            "CHILD00002 child promotion: 7-Day Deal -> Limited time deal",
        ]

        events = alerting.build_change_events(previous, current, changes, alerting.AlertConfig())

        self.assertEqual([event.severity for event in events], ["P0", "P1"])
        self.assertEqual(events[0].title, "CHILD00001 促销/Deal开始")
        self.assertEqual(events[0].detail, "促销/Deal：无 -> 7-Day Deal")
        self.assertEqual(events[1].title, "CHILD00002 促销/Deal变化")

    def test_build_change_events_classifies_parent_failed_case_insensitively(self):
        events = alerting.build_change_events({}, {}, ["PARENT1234 PARENT FAILED: timeout"], alerting.AlertConfig())

        self.assertEqual(events[0].severity, "P0")
        self.assertEqual(events[0].category, "data_source")

    def test_build_change_events_classifies_child_added_lower_than_child_removed(self):
        changes = ["PARENT1234 child added: CHILD00001", "PARENT1234 child removed: CHILD00002"]

        events = alerting.build_change_events({}, {}, changes, alerting.AlertConfig())

        self.assertEqual([event.severity for event in events], ["P0", "P1"])
        self.assertEqual(events[0].detail, "父体 PARENT1234 移除子体 CHILD00002")
        self.assertEqual(events[1].detail, "父体 PARENT1234 新增子体 CHILD00001")

    def test_build_change_events_classifies_inventory_only_removed_lower_than_added(self):
        changes = [
            "PARENT1234 inventory-only child added: CHILD00001",
            "PARENT1234 inventory-only child removed: CHILD00002",
        ]

        events = alerting.build_change_events({}, {}, changes, alerting.AlertConfig())

        self.assertEqual([event.severity for event in events], ["P0", "P1"])
        self.assertEqual(events[0].detail, "父体 PARENT1234 库存侧异常子体 CHILD00001 新增")
        self.assertEqual(events[1].detail, "父体 PARENT1234 库存侧异常子体 CHILD00002 移除")

    def test_build_change_events_uses_human_readable_detail_for_price_and_inventory(self):
        changes = ["CHILD00001 child price: 20.0 -> 21.2", "CHILD00002 child inventory: 3 -> 0"]

        events = alerting.build_change_events({}, {}, changes, alerting.AlertConfig())

        self.assertEqual(events[0].detail, "库存：3 -> 0")
        self.assertEqual(events[1].detail, "价格：20.0 -> 21.2")

    def test_build_change_events_uses_delivery_time_title(self):
        events = alerting.build_change_events(
            {},
            {},
            ["CHILD00001 child delivery_promise: Tue -> Thu"],
            alerting.AlertConfig(),
        )

        self.assertEqual(events[0].title, "CHILD00001 配送时效变化")
