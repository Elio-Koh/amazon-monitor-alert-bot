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
