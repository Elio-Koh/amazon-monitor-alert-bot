import base64
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import monitor


class MonitorTest(unittest.TestCase):
    def _daily_config(self, key, **overrides):
        config = {
            "PANGOLINFO_API_TOKEN": "token",
            "FEISHU_WEBHOOK_URL": "https://example.com",
            "MONITOR_PARENT_ASINS": "PARENT1234",
            "STATE_ENCRYPTION_KEY": key,
            "XINGSHANG_MCP_URL_TEMPLATE": "https://example.com/{parent_asin}",
            "FEISHU_WEBHOOK_SECRET": "secret",
            "MARKETPLACE": "US",
            "PANGOLIN_ZIPCODE": "10041",
        }
        config.update(overrides)
        return config

    def _daily_previous_snapshot(self, **overrides):
        snapshot = {
            "captured_at": "2026-07-09T01:15:00Z",
            "parents": {
                "PARENT1234": {
                    "major_rank": 100,
                    "minor_rank": 20,
                    "stars": 4.5,
                    "rating_count": 100,
                    "child_asins": ["CHILD00001"],
                    "source": "pangolin",
                }
            },
            "children": {
                "CHILD00001": {
                    "price": 20.0,
                    "coupon": "",
                    "promotion": "",
                    "inventory": 10,
                    "delivery_promise": "Wednesday, July 15",
                    "source": "pangolin",
                }
            },
            "errors": [],
        }
        snapshot.update(overrides)
        return snapshot

    def _daily_current_snapshot(self, **overrides):
        snapshot = {
            "captured_at": "2026-07-10T01:15:00Z",
            "parents": {
                "PARENT1234": {
                    "major_rank": 130,
                    "minor_rank": 20,
                    "stars": 4.5,
                    "rating_count": 100,
                    "child_asins": ["CHILD00001"],
                    "source": "pangolin",
                }
            },
            "children": {
                "CHILD00001": {
                    "price": 23.0,
                    "coupon": "",
                    "promotion": "",
                    "inventory": 0,
                    "delivery_promise": "Wednesday, July 15",
                    "source": "pangolin",
                }
            },
            "errors": [],
        }
        snapshot.update(overrides)
        return snapshot

    def test_normalizes_sources_into_parent_and_children_snapshot(self):
        parent_detail = {
            "asin": "PARENT1234",
            "star": "4.7",
            "rating": "1,234 ratings",
            "bestSellersRankItems": [
                {"rank": "4,335", "category": "Home & Kitchen"},
                {"rank": 16, "category": "Milk Frothers"},
            ],
            "variationList": [{"asin": "CHILD00001"}, {"asin": "CHILD00002"}],
        }
        child_detail = {
            "asin": "CHILD00001",
            "price": "$23.99",
            "coupon": "10% coupon",
            "badge": {"frequentlyReturned": "Y"},
            "fulfillment": "FBA",
            "deliveryTime": {"deliveryTime": "Tomorrow", "fastestDelivery": "Today"},
        }
        inventory = {"items": [{"asin": "CHILD00002", "inventory": 7}]}

        parent = monitor.normalize_parent("PARENT1234", parent_detail, "pangolin")
        child = monitor.normalize_child("CHILD00001", child_detail, 3, "pangolin")
        children = monitor.merge_child_asins(parent, [child], inventory)

        self.assertEqual(parent["major_rank"], 4335)
        self.assertEqual(parent["minor_rank"], 16)
        self.assertEqual(parent["rating_count"], 1234)
        self.assertEqual(parent["child_asins"], ["CHILD00001", "CHILD00002"])
        self.assertEqual(children["CHILD00001"]["price"], 23.99)
        self.assertEqual(children["CHILD00001"]["coupon"], "10% coupon")
        self.assertTrue(children["CHILD00001"]["frequently_returned"])
        self.assertEqual(children["CHILD00002"]["inventory"], 7)

    def test_diff_reports_parent_child_and_membership_changes(self):
        previous = {
            "parents": {
                "PARENT1234": {
                    "major_rank": 5,
                    "minor_rank": 2,
                    "stars": 4.6,
                    "rating_count": 100,
                    "child_asins": ["CHILD00001", "OLDCHILD01"],
                }
            },
            "children": {
                "CHILD00001": {"price": 10.0, "inventory": 3, "coupon": ""},
                "OLDCHILD01": {"price": 9.0},
            },
            "errors": [],
        }
        current = {
            "parents": {
                "PARENT1234": {
                    "major_rank": 7,
                    "minor_rank": 2,
                    "stars": 4.6,
                    "rating_count": 101,
                    "child_asins": ["CHILD00001", "NEWCHILD01"],
                }
            },
            "children": {
                "CHILD00001": {"price": 11.0, "inventory": 0, "coupon": "5% coupon", "promotion": "7-Day Deal"},
                "NEWCHILD01": {"price": 12.0},
            },
            "errors": ["PARENT1234: xingshang failed"],
        }

        changes = monitor.diff_snapshots(previous, current)
        lines = "\n".join(changes)

        self.assertIn("PARENT1234 parent major_rank: 5 -> 7", lines)
        self.assertIn("PARENT1234 parent rating_count: 100 -> 101", lines)
        self.assertIn("PARENT1234 child added: NEWCHILD01", lines)
        self.assertIn("PARENT1234 child removed: OLDCHILD01", lines)
        self.assertIn("CHILD00001 child price: 10.0 -> 11.0", lines)
        self.assertIn("CHILD00001 child inventory: 3 -> 0", lines)
        self.assertNotIn("CHILD00001 child promotion: None -> 7-Day Deal", lines)
        self.assertIn("PARENT1234: xingshang failed", lines)

        message = monitor.format_message(changes, captured_at="2026-07-10T01:00:12Z")

        self.assertIn("ASIN 变化提醒｜北京时间 2026-07-10 09:00:12", message)
        self.assertIn("状态：发现 8 项变化", message)
        self.assertIn("父 ASIN PARENT1234", message)
        self.assertIn("大类排名：5 → 7", message)
        self.assertIn("评论数：100 → 101", message)
        self.assertIn("1. CHILD00001", message)
        self.assertIn("价格：10.0 → 11.0", message)
        self.assertNotIn("促销/Deal：未知 → 7-Day Deal", message)
        self.assertIn("数据源异常：", message)

    def test_diff_ignores_unknown_to_known_field_values(self):
        previous = {
            "parents": {
                "PARENT1234": {
                    "major_rank": None,
                    "minor_rank": None,
                    "stars": None,
                    "rating_count": None,
                    "child_asins": ["CHILD00001"],
                }
            },
            "children": {"CHILD00001": {"price": None, "inventory": None, "coupon": None, "promotion": None}},
            "errors": [],
        }
        current = {
            "parents": {
                "PARENT1234": {
                    "major_rank": 100,
                    "minor_rank": 5,
                    "stars": 4.5,
                    "rating_count": 61,
                    "child_asins": ["CHILD00001"],
                }
            },
            "children": {"CHILD00001": {"price": 23.99, "inventory": 7, "coupon": "10% coupon", "promotion": "Deal"}},
            "errors": [],
        }

        self.assertEqual(monitor.diff_snapshots(previous, current), [])

    def test_diff_ignores_inventory_only_recovery_to_live_child(self):
        previous = {
            "parents": {"PARENT1234": {"child_asins": [], "inventory_only_asins": ["CHILD00001"]}},
            "children": {"CHILD00001": {"inventory": 8, "front_status": "不可售/404"}},
            "errors": [],
        }
        current = {
            "parents": {"PARENT1234": {"child_asins": ["CHILD00001"], "inventory_only_asins": []}},
            "children": {"CHILD00001": {"inventory": 8}},
            "errors": [],
        }

        changes = monitor.diff_snapshots(previous, current)

        self.assertEqual(changes, [])

    def test_diff_reports_inventory_only_anomaly_without_child_removal_when_live_child_becomes_inventory_only(self):
        previous = {
            "parents": {"PARENT1234": {"child_asins": ["CHILD00001"], "inventory_only_asins": []}},
            "children": {"CHILD00001": {"inventory": 8}},
            "errors": [],
        }
        current = {
            "parents": {"PARENT1234": {"child_asins": [], "inventory_only_asins": ["CHILD00001"]}},
            "children": {"CHILD00001": {"inventory": 8, "front_status": "不可售/404"}},
            "errors": [],
        }

        changes = monitor.diff_snapshots(previous, current)

        self.assertNotIn("PARENT1234 child removed: CHILD00001", changes)
        self.assertIn("PARENT1234 inventory-only child added: CHILD00001", changes)

    def test_merge_snapshot_preserves_unknown_current_fields_and_keeps_real_empty_values(self):
        previous = {
            "captured_at": "2026-07-09T01:15:00Z",
            "parents": {
                "PARENT1234": {
                    "major_rank": 10,
                    "major_category": "Home",
                    "minor_rank": 2,
                    "stars": 4.4,
                    "rating_count": 20,
                    "child_asins": ["CHILD00001"],
                }
            },
            "children": {
                "CHILD00001": {"price": 20.0, "coupon": "10% coupon", "promotion": "Deal", "inventory": 7, "delivery_promise": "2 days"}
            },
            "errors": [],
        }
        current = {
            "captured_at": "2026-07-10T01:15:00Z",
            "parents": {
                "PARENT1234": {
                    "major_rank": None,
                    "major_category": None,
                    "minor_rank": 3,
                    "stars": 4.5,
                    "rating_count": 21,
                    "child_asins": ["CHILD00001"],
                }
            },
            "children": {
                "CHILD00001": {"price": None, "coupon": "", "promotion": None, "inventory": 9, "delivery_promise": "3 days"}
            },
            "errors": ["PARENT1234: xingshang failed"],
        }

        merged = monitor.merge_snapshot(previous, current)

        self.assertEqual(merged["captured_at"], "2026-07-10T01:15:00Z")
        self.assertEqual(merged["parents"]["PARENT1234"]["major_rank"], 10)
        self.assertEqual(merged["parents"]["PARENT1234"]["major_category"], "Home")
        self.assertEqual(merged["parents"]["PARENT1234"]["minor_rank"], 3)
        self.assertEqual(merged["parents"]["PARENT1234"]["stars"], 4.5)
        self.assertEqual(merged["children"]["CHILD00001"]["price"], 20.0)
        self.assertEqual(merged["children"]["CHILD00001"]["coupon"], "")
        self.assertEqual(merged["children"]["CHILD00001"]["promotion"], "Deal")
        self.assertEqual(merged["children"]["CHILD00001"]["inventory"], 9)

    def test_feishu_payload_uses_optional_signature(self):
        unsigned = monitor.feishu_payload("hello", timestamp=123, secret="")
        signed = monitor.feishu_payload("hello", timestamp=123, secret="secret")

        self.assertNotIn("sign", unsigned)
        self.assertEqual(unsigned["msg_type"], "text")
        self.assertEqual(signed["timestamp"], "123")
        self.assertEqual(signed["sign"], "/1VVdZH3KitTHu9FiYl+TZ0EGq/rppGGi7XFsB5aJSA=")

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
        self.assertEqual(signed["sign"], "/1VVdZH3KitTHu9FiYl+TZ0EGq/rppGGi7XFsB5aJSA=")

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

    def test_send_daily_alert_payload_does_not_fallback_to_interactive_payload(self):
        primary_payload = {"msg_type": "interactive", "card": {}}
        fallback_payload = {"msg_type": "interactive", "card": {}}

        with patch("monitor.send_feishu_payload", side_effect=monitor.MonitorError("card rejected")) as send:
            with self.assertRaisesRegex(monitor.MonitorError, "card rejected"):
                monitor.send_daily_alert_payload(primary_payload, fallback_payload, "https://example.com")

        self.assertEqual(send.call_count, 1)

    def test_send_daily_alert_payload_does_not_fallback_from_text_primary(self):
        primary_payload = {"msg_type": "text", "content": {"text": "primary"}}
        fallback_payload = {"msg_type": "text", "content": {"text": "fallback"}}

        with patch("monitor.send_feishu_payload", side_effect=monitor.MonitorError("text rejected")) as send:
            with self.assertRaisesRegex(monitor.MonitorError, "text rejected"):
                monitor.send_daily_alert_payload(primary_payload, fallback_payload, "https://example.com")

        self.assertEqual(send.call_count, 1)

    def test_feishu_business_error_is_not_treated_as_delivery(self):
        with patch("monitor.http_json", return_value={"StatusCode": 19022, "StatusMessage": "webhook expired"}):
            with self.assertRaisesRegex(monitor.MonitorError, "Feishu response 19022: webhook expired"):
                monitor.send_feishu("hello", "https://example.com")

    def test_send_feishu_payload_raises_on_business_error(self):
        payload = {"msg_type": "interactive", "card": {"elements": []}}

        with patch("monitor.http_json", return_value={"StatusCode": 19022, "StatusMessage": "webhook expired"}):
            with self.assertRaisesRegex(monitor.MonitorError, "Feishu response 19022: webhook expired"):
                monitor.send_feishu_payload(payload, "https://example.com")

    def test_encrypted_state_round_trips(self):
        key = base64.urlsafe_b64encode(os.urandom(32)).decode()
        snapshot = {"parents": {"PARENT1234": {"major_rank": 1}}, "children": {}, "errors": []}

        encrypted = monitor.encrypt_snapshot(snapshot, key)
        decoded = json.loads(encrypted)

        self.assertIn("data", decoded)
        self.assertEqual(monitor.decrypt_snapshot(encrypted, key), snapshot)

    def test_daily_report_without_changes_is_a_single_summary(self):
        previous = {
            "captured_at": "2026-07-09T01:15:00Z",
            "parents": {"PARENT1234": {"child_asins": ["CHILD00001"]}},
            "children": {"CHILD00001": {}},
            "errors": [],
        }
        current = {
            "captured_at": "2026-07-10T01:15:00Z",
            "parents": {"PARENT1234": {"child_asins": ["CHILD00001"]}},
            "children": {"CHILD00001": {}},
            "errors": [],
        }

        messages = monitor.format_daily_report_messages(previous, current, [])

        self.assertEqual(len(messages), 1)
        self.assertIn("ASIN 每日监控｜北京时间 2026-07-10 09:15:00", messages[0])
        self.assertIn("比较基线：2026-07-09（昨日）", messages[0])
        self.assertIn("状态：较昨日无变化", messages[0])
        self.assertIn("父 ASIN：1｜正常子体：1", messages[0])

    def test_daily_report_includes_only_changed_parent_details(self):
        previous = {
            "captured_at": "2026-07-09T01:15:00Z",
            "parents": {
                "PARENT1234": {"stars": 4.5, "child_asins": ["CHILD00001"], "source": "pangolin"},
                "PARENT5678": {"stars": 4.1, "child_asins": ["CHILD00002"], "source": "pangolin"},
            },
            "children": {"CHILD00001": {"price": 10.0}, "CHILD00002": {"price": 20.0}},
            "errors": [],
        }
        current = {
            "captured_at": "2026-07-10T01:15:00Z",
            "parents": {
                "PARENT1234": {"stars": 4.6, "child_asins": ["CHILD00001"], "source": "pangolin"},
                "PARENT5678": {"stars": 4.1, "child_asins": ["CHILD00002"], "source": "pangolin"},
            },
            "children": {"CHILD00001": {"price": 10.0}, "CHILD00002": {"price": 20.0}},
            "errors": [],
        }
        changes = monitor.diff_snapshots(previous, current)

        messages = monitor.format_daily_report_messages(previous, current, changes)

        self.assertIn("状态：发现 1 项变化", messages[0])
        self.assertIn("受影响父体：PARENT1234", messages[0])
        self.assertTrue(any("父 ASIN PARENT1234" in message for message in messages))
        self.assertFalse(any("父 ASIN PARENT5678" in message for message in messages))

    def test_daily_parent_detail_embeds_changes_except_inventory_inline_marks(self):
        previous = {
            "captured_at": "2026-07-09T01:15:00Z",
            "parents": {
                "PARENT1234": {"major_rank": 100, "stars": 4.5, "child_asins": ["CHILD00001"], "source": "pangolin"}
            },
            "children": {
                "CHILD00001": {"price": 10.0, "inventory": 5, "promotion": "", "delivery_promise": "Wednesday, July 15"}
            },
            "errors": [],
        }
        current = {
            "captured_at": "2026-07-10T01:15:00Z",
            "parents": {
                "PARENT1234": {"major_rank": 90, "stars": 4.6, "child_asins": ["CHILD00001"], "source": "pangolin"}
            },
            "children": {
                "CHILD00001": {
                    "price": 11.0,
                    "inventory": 7,
                    "promotion": "Limited time deal; 10% off",
                    "delivery_promise": "Thursday, July 16",
                }
            },
            "errors": [],
        }
        changes = monitor.diff_snapshots(previous, current)

        messages = monitor.format_daily_report_messages(previous, current, changes)
        detail = next(message for message in messages if "父 ASIN PARENT1234" in message and "子体明细：" in message)

        self.assertIn("ASIN 变化明细｜北京时间 2026-07-10 09:15:00", detail)
        self.assertIn("变化摘要：", detail)
        self.assertIn("- 大类排名：100 → 90", detail)
        self.assertIn("- CHILD00001 价格：10.0 → 11.0", detail)
        self.assertIn("- 库存变化：1 个子体", detail)
        self.assertIn("价 11.0（10.0→11.0）", detail)
        self.assertIn("促销 Limited time deal; 10% off（无→Limited time deal; 10% off）", detail)
        self.assertIn("时效 7天（7/16）（6天（7/15）→7天（7/16））", detail)
        self.assertIn("库存 7｜", detail)
        self.assertNotIn("库存 7（5→7）", detail)

    def test_delivery_date_round_trips_in_encrypted_state(self):
        key = base64.urlsafe_b64encode(os.urandom(32)).decode()
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "delivery.enc.json")

            self.assertIsNone(monitor.load_delivery_date(path, key))
            monitor.save_delivery_date(path, key, "2026-07-10")

            self.assertEqual(monitor.load_delivery_date(path, key), "2026-07-10")

    def test_daily_report_skips_collection_when_today_was_delivered(self):
        key = base64.urlsafe_b64encode(os.urandom(32)).decode()
        with tempfile.TemporaryDirectory() as directory:
            state_path = os.path.join(directory, "latest.enc.json")
            delivery_path = os.path.join(directory, "delivery.enc.json")
            monitor.save_current(state_path, {"captured_at": "2026-07-09T01:15:00Z", "parents": {}, "children": {}, "errors": []}, key)
            monitor.save_current(delivery_path, {"delivered_on": "2026-07-10"}, key)
            with patch("monitor.env_config", return_value={"STATE_ENCRYPTION_KEY": key, "FEISHU_WEBHOOK_URL": "https://example.com"}), patch(
                "monitor.now_iso", return_value="2026-07-10T01:15:00Z"
            ), patch("monitor.collect_snapshot") as collect:
                result = monitor.main(["--daily-report", "--state", state_path, "--delivery-state", delivery_path])

        self.assertEqual(result, 0)
        collect.assert_not_called()

    def test_render_state_only_prints_existing_snapshot_without_required_sources(self):
        key = base64.urlsafe_b64encode(os.urandom(32)).decode()
        snapshot = {
            "captured_at": "2026-07-12T04:52:16Z",
            "parents": {"PARENT1234": {"major_rank": 1, "child_asins": ["CHILD00001"], "source": "pangolin"}},
            "children": {"CHILD00001": {"price": 10.0}},
            "errors": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            state_path = os.path.join(directory, "latest.enc.json")
            monitor.save_current(state_path, snapshot, key)
            stdout = io.StringIO()

            with patch.dict(os.environ, {"STATE_ENCRYPTION_KEY": key}, clear=True), patch("monitor.collect_snapshot") as collect, patch("sys.stdout", stdout):
                self.assertEqual(monitor.main(["--state", state_path, "--render-state-only"]), 0)

            collect.assert_not_called()
            self.assertIn("ASIN 今日数据总览｜北京时间 2026-07-12 12:52:16", stdout.getvalue())
            self.assertIn("父 ASIN PARENT1234", stdout.getvalue())

    def test_render_state_only_with_previous_state_prints_daily_change_report(self):
        key = base64.urlsafe_b64encode(os.urandom(32)).decode()
        previous = {
            "captured_at": "2026-07-11T01:15:00Z",
            "parents": {"PARENT1234": {"major_rank": 100, "child_asins": ["CHILD00001"], "source": "pangolin"}},
            "children": {"CHILD00001": {"price": 10.0, "inventory": 5}},
            "errors": [],
        }
        current = {
            "captured_at": "2026-07-12T01:15:00Z",
            "parents": {"PARENT1234": {"major_rank": 90, "child_asins": ["CHILD00001"], "source": "pangolin"}},
            "children": {"CHILD00001": {"price": 11.0, "inventory": 7}},
            "errors": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            state_path = os.path.join(directory, "latest.enc.json")
            previous_path = os.path.join(directory, "previous.enc.json")
            monitor.save_current(state_path, current, key)
            monitor.save_current(previous_path, previous, key)
            stdout = io.StringIO()

            with patch.dict(os.environ, {"STATE_ENCRYPTION_KEY": key}, clear=True), patch("monitor.collect_snapshot") as collect, patch("sys.stdout", stdout):
                self.assertEqual(monitor.main(["--state", state_path, "--previous-state", previous_path, "--render-state-only"]), 0)

            output = stdout.getvalue()
            collect.assert_not_called()
            self.assertIn("ASIN 每日监控｜北京时间 2026-07-12 09:15:00", output)
            self.assertIn("比较基线：2026-07-11（昨日）", output)
            self.assertIn("状态：发现 3 项变化", output)
            self.assertIn("ASIN 变化明细｜北京时间 2026-07-12 09:15:00", output)
            self.assertIn("- 大类排名：100 → 90", output)
            self.assertIn("价 11.0（10.0→11.0）", output)

    def test_partial_daily_report_merges_and_persists_snapshot(self):
        key = base64.urlsafe_b64encode(os.urandom(32)).decode()
        previous = {
            "captured_at": "2026-07-09T01:15:00Z",
            "parents": {"PARENT1234": {"major_rank": 10, "stars": 4.4, "child_asins": ["CHILD00001"]}},
            "children": {"CHILD00001": {"price": 20.0, "inventory": 7}},
            "errors": [],
        }
        current = {
            "captured_at": "2026-07-10T01:15:00Z",
            "parents": {"PARENT1234": {"major_rank": None, "stars": 4.5, "child_asins": ["CHILD00001"]}},
            "children": {"CHILD00001": {"price": None, "inventory": 9}},
            "errors": ["PARENT1234: xingshang failed"],
        }
        with tempfile.TemporaryDirectory() as directory:
            state_path = os.path.join(directory, "latest.enc.json")
            delivery_path = os.path.join(directory, "delivery.enc.json")
            monitor.save_current(state_path, previous, key)
            with patch("monitor.env_config", return_value={"STATE_ENCRYPTION_KEY": key, "FEISHU_WEBHOOK_URL": "https://example.com"}), patch(
                "monitor.now_iso", return_value="2026-07-10T01:15:00Z"
            ), patch("monitor.collect_snapshot", return_value=current), patch("monitor.send_daily_alert_payload") as send:
                result = monitor.main(
                    ["--daily-report", "--state", state_path, "--output", state_path, "--delivery-state", delivery_path]
                )

            self.assertEqual(result, 0)
            self.assertTrue(send.called)
            self.assertEqual(monitor.load_delivery_date(delivery_path, key), "2026-07-10")
            saved = monitor.load_previous(state_path, key)
            self.assertEqual(saved["captured_at"], "2026-07-10T01:15:00Z")
            self.assertEqual(saved["parents"]["PARENT1234"]["major_rank"], 10)
            self.assertEqual(saved["parents"]["PARENT1234"]["stars"], 4.5)
            self.assertEqual(saved["children"]["CHILD00001"]["price"], 20.0)
            self.assertEqual(saved["children"]["CHILD00001"]["inventory"], 9)

    def test_daily_report_does_not_record_delivery_when_feishu_fails(self):
        key = base64.urlsafe_b64encode(os.urandom(32)).decode()
        previous = self._daily_previous_snapshot()
        current = self._daily_current_snapshot()
        with tempfile.TemporaryDirectory() as directory:
            state_path = os.path.join(directory, "latest.enc.json")
            delivery_path = os.path.join(directory, "delivery.enc.json")
            monitor.save_current(state_path, previous, key)
            with patch("monitor.env_config", return_value={"STATE_ENCRYPTION_KEY": key, "FEISHU_WEBHOOK_URL": "https://example.com"}), patch(
                "monitor.now_iso", return_value="2026-07-10T01:15:00Z"
            ), patch("monitor.collect_snapshot", return_value=current), patch("monitor.send_daily_alert_payload", side_effect=monitor.MonitorError("Feishu failed")):
                with self.assertRaises(monitor.MonitorError):
                    monitor.main(
                        ["--daily-report", "--state", state_path, "--output", state_path, "--delivery-state", delivery_path]
                    )

            self.assertFalse(os.path.exists(delivery_path))

    def test_daily_report_sends_one_interactive_alert_card_with_text_fallback(self):
        key = base64.urlsafe_b64encode(os.urandom(32)).decode()
        previous = self._daily_previous_snapshot()
        current = self._daily_current_snapshot()
        with tempfile.TemporaryDirectory() as directory:
            state_path = os.path.join(directory, "latest.enc.json")
            delivery_path = os.path.join(directory, "delivery.enc.json")
            monitor.save_current(state_path, previous, key)

            with (
                patch("monitor.env_config", return_value=self._daily_config(key)),
                patch("monitor.now_iso", return_value="2026-07-10T01:15:00Z"),
                patch("monitor.collect_snapshot", return_value=current),
                patch("monitor.send_daily_alert_payload") as send_daily,
                patch("monitor.send_feishu") as send_old,
            ):
                result = monitor.main(["--daily-report", "--state", state_path, "--output", state_path, "--delivery-state", delivery_path])

            self.assertEqual(result, 0)
            send_old.assert_not_called()
            send_daily.assert_called_once()
            primary, fallback, webhook = send_daily.call_args.args
            self.assertEqual(webhook, "https://example.com")
            self.assertEqual(primary["msg_type"], "interactive")
            self.assertEqual(primary["card"]["header"]["title"]["content"], "ASIN 每日重点提醒")
            self.assertEqual(fallback["msg_type"], "text")
            self.assertIn("库存归零", fallback["content"]["text"])
            self.assertEqual(monitor.load_delivery_date(delivery_path, key), "2026-07-10")

    def test_full_report_output_writes_original_daily_report_without_sending_it(self):
        key = base64.urlsafe_b64encode(os.urandom(32)).decode()
        previous = self._daily_previous_snapshot()
        current = self._daily_current_snapshot()
        with tempfile.TemporaryDirectory() as directory:
            state_path = os.path.join(directory, "latest.enc.json")
            delivery_path = os.path.join(directory, "delivery.enc.json")
            full_report_path = os.path.join(directory, "reports", "daily.txt")
            monitor.save_current(state_path, previous, key)

            with (
                patch("monitor.env_config", return_value=self._daily_config(key, FULL_REPORT_OUTPUT=full_report_path)),
                patch("monitor.now_iso", return_value="2026-07-10T01:15:00Z"),
                patch("monitor.collect_snapshot", return_value=current),
                patch("monitor.send_daily_alert_payload") as send_daily,
            ):
                result = monitor.main(["--daily-report", "--state", state_path, "--output", state_path, "--delivery-state", delivery_path])

            self.assertEqual(result, 0)
            with open(full_report_path, "r", encoding="utf-8") as handle:
                full_report = handle.read()
            self.assertIn("监控范围：父 ASIN：1｜正常子体：1", full_report)
            self.assertIn("父 ASIN PARENT1234", full_report)
            summary = send_daily.call_args.args[1]["content"]["text"]
            self.assertNotIn("监控范围", summary)

    def test_daily_report_with_only_p2_events_saves_state_and_delivery_without_feishu(self):
        key = base64.urlsafe_b64encode(os.urandom(32)).decode()
        previous = self._daily_previous_snapshot()
        current = self._daily_current_snapshot(
            parents={
                "PARENT1234": {
                    "major_rank": 110,
                    "minor_rank": 20,
                    "stars": 4.6,
                    "rating_count": 101,
                    "child_asins": ["CHILD00001"],
                    "source": "pangolin",
                }
            },
            children=previous["children"],
        )
        with tempfile.TemporaryDirectory() as directory:
            state_path = os.path.join(directory, "latest.enc.json")
            delivery_path = os.path.join(directory, "delivery.enc.json")
            monitor.save_current(state_path, previous, key)

            with (
                patch("monitor.env_config", return_value=self._daily_config(key, ALERT_SEND_NO_CHANGE="false")),
                patch("monitor.now_iso", return_value="2026-07-10T01:15:00Z"),
                patch("monitor.collect_snapshot", return_value=current),
                patch("monitor.send_daily_alert_payload") as send_daily,
                patch("monitor.send_feishu") as send_old,
            ):
                result = monitor.main(["--daily-report", "--state", state_path, "--output", state_path, "--delivery-state", delivery_path])

            self.assertEqual(result, 0)
            send_daily.assert_not_called()
            send_old.assert_not_called()
            self.assertEqual(monitor.load_delivery_date(delivery_path, key), "2026-07-10")
            saved = monitor.load_previous(state_path, key)
            self.assertEqual(saved["parents"]["PARENT1234"]["stars"], 4.6)
            self.assertEqual(saved["alert_dedupe"], {})

    def test_daily_report_dedupe_suppresses_same_event_inside_window_and_persists_history(self):
        key = base64.urlsafe_b64encode(os.urandom(32)).decode()
        previous = self._daily_previous_snapshot(
            captured_at="2026-07-10T01:15:00Z",
            alert_dedupe={
                "P0|inventory|PARENT1234|CHILD00001|inventory|10|0": {
                    "last_seen_on": "2026-07-10",
                    "last_sent_on": "2026-07-10",
                    "severity": "P0",
                }
            },
        )
        current = self._daily_current_snapshot(
            captured_at="2026-07-11T01:15:00Z",
            parents={
                "PARENT1234": {
                    "major_rank": 100,
                    "minor_rank": 20,
                    "stars": 4.5,
                    "rating_count": 100,
                    "child_asins": ["CHILD00001"],
                    "source": "pangolin",
                }
            },
            children={
                "CHILD00001": {
                    "price": 20.0,
                    "coupon": "",
                    "promotion": "",
                    "inventory": 0,
                    "delivery_promise": "Wednesday, July 15",
                    "source": "pangolin",
                }
            },
        )
        with tempfile.TemporaryDirectory() as directory:
            state_path = os.path.join(directory, "latest.enc.json")
            delivery_path = os.path.join(directory, "delivery.enc.json")
            monitor.save_current(state_path, previous, key)

            with (
                patch("monitor.env_config", return_value=self._daily_config(key, ALERT_DEDUPE_WINDOW_DAYS="1")),
                patch("monitor.now_iso", return_value="2026-07-11T01:15:00Z"),
                patch("monitor.collect_snapshot", return_value=current),
                patch("monitor.send_daily_alert_payload") as send_daily,
            ):
                result = monitor.main(["--daily-report", "--state", state_path, "--output", state_path, "--delivery-state", delivery_path])

            self.assertEqual(result, 0)
            send_daily.assert_not_called()
            saved = monitor.load_previous(state_path, key)
            self.assertIn("alert_dedupe", saved)
            self.assertEqual(saved["alert_dedupe"]["P0|inventory|PARENT1234|CHILD00001|inventory|10|0"]["last_sent_on"], "2026-07-10")
            self.assertEqual(saved["alert_dedupe"]["P0|inventory|PARENT1234|CHILD00001|inventory|10|0"]["last_seen_on"], "2026-07-11")

    def test_daily_report_text_mode_sends_text_payload_without_card_fallback(self):
        key = base64.urlsafe_b64encode(os.urandom(32)).decode()
        previous = self._daily_previous_snapshot()
        current = self._daily_current_snapshot()
        with tempfile.TemporaryDirectory() as directory:
            state_path = os.path.join(directory, "latest.enc.json")
            delivery_path = os.path.join(directory, "delivery.enc.json")
            monitor.save_current(state_path, previous, key)

            with (
                patch("monitor.env_config", return_value=self._daily_config(key, FEISHU_MESSAGE_MODE="text")),
                patch("monitor.now_iso", return_value="2026-07-10T01:15:00Z"),
                patch("monitor.collect_snapshot", return_value=current),
                patch("monitor.send_daily_alert_payload") as send_daily,
            ):
                result = monitor.main(["--daily-report", "--state", state_path, "--output", state_path, "--delivery-state", delivery_path])

            self.assertEqual(result, 0)
            send_daily.assert_called_once()
            primary, fallback, _ = send_daily.call_args.args
            self.assertEqual(primary["msg_type"], "text")
            self.assertIsNone(fallback)
            self.assertIn("库存归零", primary["content"]["text"])

    def test_daily_report_dry_run_prints_single_feishu_payload_and_does_not_save_state_or_delivery(self):
        key = base64.urlsafe_b64encode(os.urandom(32)).decode()
        previous = self._daily_previous_snapshot()
        current = self._daily_current_snapshot()
        with tempfile.TemporaryDirectory() as directory:
            state_path = os.path.join(directory, "latest.enc.json")
            delivery_path = os.path.join(directory, "delivery.enc.json")
            full_report_path = os.path.join(directory, "reports", "daily.txt")
            monitor.save_current(state_path, previous, key)
            stdout = io.StringIO()

            with (
                patch("monitor.env_config", return_value=self._daily_config(key, FULL_REPORT_OUTPUT=full_report_path)),
                patch("monitor.now_iso", return_value="2026-07-10T01:15:00Z"),
                patch("monitor.collect_snapshot", return_value=current),
                patch("monitor.send_daily_alert_payload") as send_daily,
                patch("sys.stdout", stdout),
            ):
                result = monitor.main(["--daily-report", "--dry-run", "--state", state_path, "--output", state_path, "--delivery-state", delivery_path])

            self.assertEqual(result, 0)
            send_daily.assert_not_called()
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["msg_type"], "interactive")
            self.assertIn("库存归零", payload["card"]["elements"][0]["text"]["content"])
            self.assertNotIn("监控范围", stdout.getvalue())
            with open(full_report_path, "r", encoding="utf-8") as handle:
                self.assertIn("监控范围：父 ASIN：1｜正常子体：1", handle.read())
            self.assertFalse(os.path.exists(delivery_path))
            saved = monitor.load_previous(state_path, key)
            self.assertEqual(saved["captured_at"], "2026-07-09T01:15:00Z")

    def test_daily_report_does_not_record_delivery_when_state_save_fails_after_send(self):
        key = base64.urlsafe_b64encode(os.urandom(32)).decode()
        previous = self._daily_previous_snapshot()
        current = self._daily_current_snapshot()
        with tempfile.TemporaryDirectory() as directory:
            state_path = os.path.join(directory, "latest.enc.json")
            delivery_path = os.path.join(directory, "delivery.enc.json")
            monitor.save_current(state_path, previous, key)
            original_save_current = monitor.save_current

            def save_current_or_fail(path, snapshot, encryption_key):
                if path == state_path:
                    raise OSError("state write failed")
                original_save_current(path, snapshot, encryption_key)

            with (
                patch("monitor.env_config", return_value=self._daily_config(key)),
                patch("monitor.now_iso", return_value="2026-07-10T01:15:00Z"),
                patch("monitor.collect_snapshot", return_value=current),
                patch("monitor.send_daily_alert_payload") as send_daily,
                patch("monitor.save_current", side_effect=save_current_or_fail),
            ):
                with self.assertRaisesRegex(OSError, "state write failed"):
                    monitor.main(["--daily-report", "--state", state_path, "--output", state_path, "--delivery-state", delivery_path])

            send_daily.assert_called_once()
            self.assertFalse(os.path.exists(delivery_path))

    def test_daily_report_and_current_report_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit):
            monitor.main(["--daily-report", "--report-current"])

    def test_formats_current_snapshot_report(self):
        snapshot = {
            "captured_at": "2026-07-09T03:30:00Z",
            "parents": {
                "PARENT1234": {
                    "major_rank": 4335,
                    "major_category": "Home & Kitchen",
                    "minor_rank": 16,
                    "minor_category": "Milk Frothers",
                    "stars": 4.7,
                    "rating_count": 54,
                    "child_asins": ["CHILD00001"],
                }
            },
            "children": {
                "CHILD00001": {
                    "price": 23.99,
                    "coupon": "10% coupon",
                    "promotion": "7-Day Deal",
                    "frequently_returned": False,
                    "inventory": 7,
                    "fulfillment_method": "FBA",
                    "delivery_promise": "Tomorrow",
                }
            },
            "errors": [],
        }

        message = monitor.format_snapshot_report(snapshot)

        self.assertIn("ASIN 今日数据｜北京时间 2026-07-09 11:30:00", message)
        self.assertIn("PARENT1234", message)
        self.assertIn("大类排名: 4335 (Home & Kitchen)", message)
        self.assertIn("小类排名: 16 (Milk Frothers)", message)
        self.assertIn("CHILD00001", message)
        self.assertIn("价 23.99", message)
        self.assertIn("库存 7", message)
        self.assertIn("促销 7-Day Deal", message)

    def test_formats_current_snapshot_report_as_grouped_messages(self):
        snapshot = {
            "captured_at": "2026-07-09T03:30:00Z",
            "parents": {
                "PARENT1234": {
                    "major_rank": 4335,
                    "major_category": "Home & Kitchen",
                    "minor_rank": 16,
                    "minor_category": "Milk Frothers",
                    "stars": 4.7,
                    "rating_count": 54,
                    "child_asins": ["CHILD00001"],
                    "inventory_only_asins": ["CHILD00002"],
                    "inventory_source": "xingshang",
                    "source": "pangolin",
                },
                "PARENT5678": {
                    "major_rank": 99,
                    "major_category": "Kitchen",
                    "minor_rank": 3,
                    "minor_category": "Tables",
                    "stars": 4.1,
                    "rating_count": 12,
                    "child_asins": ["CHILD00003"],
                    "inventory_source": "xingshang",
                    "source": "pangolin",
                },
            },
            "children": {
                "CHILD00001": {"price": 23.99, "coupon": "", "promotion": "", "inventory": 7, "fulfillment_method": "FBA", "source": "pangolin"},
                "CHILD00002": {"inventory": 2, "front_status": "不可售/404", "source": "xingshang_inventory_only"},
                "CHILD00003": {"price": 33.99, "coupon": "5% coupon", "promotion": "Deal", "inventory": 8, "fulfillment_method": "AMZ", "source": "pangolin"},
            },
            "errors": ["CHILD00004: pangolin child failed"],
        }

        messages = monitor.format_snapshot_report_messages(snapshot)

        self.assertEqual(len(messages), 4)
        self.assertIn("ASIN 今日数据总览｜北京时间 2026-07-09 11:30:00", messages[0])
        self.assertIn("父 ASIN：2｜正常子体：2｜库存侧异常：1｜数据源异常：1", messages[0])
        self.assertIn("PARENT1234｜排名 4335 / 16｜评分 4.7｜评论 54｜子体 1｜异常 1", messages[0])
        self.assertIn("父 ASIN PARENT1234", messages[1])
        self.assertIn("CHILD00001｜价 23.99｜库存 7", messages[1])
        self.assertIn("库存侧异常：", messages[1])
        self.assertIn("父 ASIN PARENT5678", messages[2])
        self.assertIn("数据源异常汇总:", messages[3])

    def test_current_report_omits_fulfillment_and_return_badge(self):
        snapshot = {
            "captured_at": "2026-07-09T21:30:00Z",
            "parents": {
                "B0H1Q77TDL": {
                    "major_rank": 10106,
                    "minor_rank": 36,
                    "stars": 4.5,
                    "rating_count": 2,
                    "child_asins": ["B0GJZYZHJJ"],
                    "inventory_source": "xingshang_empty",
                    "source": "pangolin",
                }
            },
            "children": {
                "B0GJZYZHJJ": {
                    "price": 42.29,
                    "coupon": "",
                    "promotion": "10% off",
                    "inventory": 16,
                    "fulfillment_method": "Amazon.com",
                    "frequently_returned": False,
                    "delivery_promise": "Wednesday, July 15",
                    "source": "pangolin",
                }
            },
            "errors": [],
        }

        message = monitor.format_snapshot_report(snapshot)

        self.assertIn("B0GJZYZHJJ｜价 42.29｜库存 16｜Coupon 无｜促销 10% off｜时效 6天（7/15）", message)
        self.assertNotIn("配送 Amazon.com", message)
        self.assertNotIn("退货", message)

    def test_unwraps_sellersprite_detail_payload(self):
        payload = {
            "code": "OK",
            "data": {
                "asin": {
                    "asin": "B0FFT1JQ9T",
                    "bsrLabel": "Home & Kitchen",
                    "bsrRank": 274790,
                    "subcategories": [{"rank": 120, "label": "Kids' Table & Chair Sets"}],
                    "rating": 4.5,
                    "ratings": 59,
                    "price": 86.57,
                    "coupon": "",
                    "fulfillment": "AMZ",
                    "variations": 13,
                    "variationList": [{"asin": "B0FFT34472"}],
                },
                "couponTrends": [{"asinPrice": 70.99, "couponPrice": 5.68, "finalPrice": 65.31}],
            },
        }

        detail = monitor.unwrap_detail_payload(payload)
        parent = monitor.normalize_parent("B0FFT1JQ9T", detail, "SELLERSPRITE_MCP_URL")
        child = monitor.normalize_child("B0FFT1JQ9T", detail, 3, "SELLERSPRITE_MCP_URL")

        self.assertEqual(parent["major_rank"], 274790)
        self.assertEqual(parent["minor_rank"], 120)
        self.assertEqual(parent["stars"], 4.5)
        self.assertEqual(parent["rating_count"], 59)
        self.assertEqual(parent["child_asins"], ["B0FFT34472"])
        self.assertEqual(child["price"], 86.57)
        self.assertEqual(child["coupon"], "")
        self.assertEqual(child["fulfillment_method"], "AMZ")

    def test_normalizes_pangolin_rating_count_parenthesized_rating(self):
        parent = monitor.normalize_parent(
            "B0FFT1JQ9T",
            {"asin": "B0FFT1JQ9T", "star": "4.4", "rating": "(61)"},
            "pangolin",
        )

        self.assertEqual(parent["stars"], 4.4)
        self.assertEqual(parent["rating_count"], 61)

    def test_extract_child_asins_reads_pangolin_variant_details(self):
        detail = {"variantDetails": [{"asin": "B0FFT34472"}, {"asin": "B0FFT2BF9L"}]}

        self.assertEqual(monitor.extract_child_asins(detail), ["B0FFT2BF9L", "B0FFT34472"])

    def test_normalizes_pangolin_delivery_and_return_badge_aliases(self):
        child = monitor.normalize_child(
            "B0FFT34472",
            {
                "asin": "B0FFT34472",
                "price": "$85.53",
                "deliveryInfo": {"deliveryDate": "Tomorrow, Jul 10"},
                "productBadges": [{"label": "Frequently returned item"}],
            },
            295,
            "pangolin",
        )

        self.assertEqual(child["delivery_promise"], "Tomorrow, Jul 10")
        self.assertTrue(child["frequently_returned"])

    def test_normalizes_absent_return_badge_on_valid_pangolin_page_as_false(self):
        child = monitor.normalize_child(
            "B0FFT34472",
            {"asin": "B0FFT34472", "title": "Kids table", "price": "$85.53", "delivery": {"deliveryTime": "Tomorrow"}},
            None,
            "pangolin",
        )

        self.assertFalse(child["frequently_returned"])

    def test_normalizes_deal_without_treating_amazon_choice_as_promotion(self):
        deal = monitor.normalize_child("B0FFT34472", {"promotions": ["Limited time deal"]}, None, "pangolin")
        choice = monitor.normalize_child(
            "B0FFT34472",
            {"badge": "Amazon's Choice highlights highly rated, well-priced products available to ship immediately."},
            None,
            "pangolin",
        )

        self.assertEqual(deal["promotion"], "Limited time deal")
        self.assertEqual(choice["promotion"], "")

    def test_normalizes_ltd_badge_when_discount_types_are_missing(self):
        child = monitor.normalize_child(
            "B0G5P8HWZ8",
            {"asin": "B0G5P8HWZ8", "badge": "LTD", "savingsPercentage": "23%"},
            None,
            "pangolin",
        )

        self.assertEqual(child["promotion"], "Limited time deal; 23% off")

    def test_normalizes_pangolin_discount_types_savings_and_promotions(self):
        child = monitor.normalize_child(
            "B0GJZYZHJJ",
            {
                "asin": "B0GJZYZHJJ",
                "price": "$20.24",
                "discountTypes": ["Limited time deal"],
                "savingsPercentage": "23%",
                "promotions": [{"quantity": 2, "discount": "10%"}],
            },
            None,
            "pangolin",
        )

        self.assertEqual(child["promotion"], "Limited time deal; 23% off; Buy 2 save 10%")

    def test_normalizes_pangolin_in_stock_as_inventory_when_xingshang_missing(self):
        child = monitor.normalize_child(
            "B0GJZYZHJJ",
            {"asin": "B0GJZYZHJJ", "price": "$42.29", "inStock": "Only 16 left in stock - order soon."},
            None,
            "pangolin",
        )
        overridden = monitor.normalize_child(
            "B0GJZYZHJJ",
            {"asin": "B0GJZYZHJJ", "price": "$42.29", "inStock": "Only 16 left in stock - order soon."},
            9,
            "pangolin",
        )

        self.assertEqual(child["inventory"], 16)
        self.assertEqual(child["inventory_source"], "front_detail")
        self.assertEqual(overridden["inventory"], 9)
        self.assertEqual(overridden["inventory_source"], "xingshang")

    def test_formats_delivery_promise_as_days_in_new_york_time(self):
        self.assertEqual(
            monitor.format_delivery_days("Wednesday, July 15; fastest Tuesday, July 14", "2026-07-09T21:30:00Z"),
            "6天（7/15；最快5天 7/14）",
        )
        self.assertEqual(monitor.format_delivery_days("Tomorrow, Jul 10", "2026-07-09T21:30:00Z"), "1天（7/10）")

    def test_pangolin_error_response_is_not_treated_as_empty(self):
        with self.assertRaisesRegex(monitor.MonitorError, "账户已过期"):
            monitor.extract_results({"code": 2007, "message": "账户已过期", "data": None})

    def test_extract_child_asins_ignores_numeric_variations_and_invalid_asins(self):
        detail = {
            "variationList": [{"asin": "B0FFT34472"}],
            "variations": 13,
            "variationAsins": ["not-an-asin", "B0FFT2BF9L"],
        }

        self.assertEqual(monitor.extract_child_asins(detail), ["B0FFT2BF9L", "B0FFT34472"])

    def test_fetch_inventory_uses_configured_scope_without_overriding_child_asins(self):
        captured = {}

        async def fake_call(server_url, fragments, args, headers=None):
            captured["server_url"] = server_url
            captured["fragments"] = tuple(fragments)
            captured["args"] = dict(args)
            return {"items": []}

        with patch("monitor.call_mcp_tool", side_effect=fake_call):
            monitor.fetch_inventory("B0FFT1JQ9T", "https://example.com/xingshang_config_{parent_asin}", timeout=1)

        self.assertEqual(captured["server_url"], "https://example.com/xingshang_config_B0FFT1JQ9T/")
        self.assertEqual(captured["fragments"], ("get_store_asin_info",))
        self.assertEqual(captured["args"]["force_refresh"], False)
        self.assertNotIn("spu_item_id_list", captured["args"])

    def test_collect_snapshot_separates_inventory_only_asins_from_live_children(self):
        seller_children = [
            "B0FFT34472",
            "B0FFT2BF9L",
            "B0FFSZ7J6L",
            "B0FFT2PHP9",
            "B0FFT28G1M",
            "B0FFT37H43",
            "B0FFT2KZYP",
            "B0FFT1F3PD",
            "B0FFT149KP",
            "B0FFT45VX5",
            "B0FFSZZ3BK",
            "B0FFT149JM",
            "B0FFT38NB4",
        ]
        inventory_children = seller_children + ["B0FVX93K44", "B0FVX6PTYC"]

        def fake_fallback(config, asin, marketplace, errors, label):
            if label == "parent":
                return {
                    "asin": asin,
                    "bsrRank": 274790,
                    "bsrLabel": "Home & Kitchen",
                    "subcategories": [{"rank": 120, "label": "Kids' Table & Chair Sets"}],
                    "rating": 4.5,
                    "ratings": 59,
                    "variations": 13,
                    "variationList": [{"asin": child} for child in seller_children],
                }, "SELLERSPRITE_MCP_URL"
            return {"asin": asin, "price": 1.0, "coupon": "", "fulfillment": "AMZ"}, "SELLERSPRITE_MCP_URL"

        with (
            patch(
                "monitor.pangolin_scrape",
                side_effect=lambda token, parser_name, content, *, site, zipcode, timeout: {"data": {"json": {"data": {"results": [] if content in {"B0FFT1JQ9T", "B0FVX93K44", "B0FVX6PTYC"} else [{"asin": content, "price": "$1.00", "fulfillment": "AMZ"}]}}}},
            ),
            patch("monitor.fetch_fallback_detail", side_effect=fake_fallback),
            patch("monitor.fetch_inventory", return_value={"items": [{"asin": asin, "inventory": index} for index, asin in enumerate(inventory_children, 1)]}),
        ):
            snapshot = monitor.collect_snapshot(
                {
                    "PANGOLINFO_API_TOKEN": "token",
                    "MONITOR_PARENT_ASINS": "B0FFT1JQ9T",
                    "XINGSHANG_MCP_URL_TEMPLATE": "https://example.com/{parent_asin}",
                    "MARKETPLACE": "US",
                    "PANGOLIN_ZIPCODE": "10041",
                    "PANGOLIN_TIMEOUT_SECONDS": "1",
                    "MCP_TIMEOUT_SECONDS": "1",
                }
            )

        parent = snapshot["parents"]["B0FFT1JQ9T"]
        parent_children = parent["child_asins"]
        self.assertEqual(len(parent_children), 13)
        self.assertNotIn("13", parent_children)
        self.assertEqual(parent["inventory_only_asins"], ["B0FVX6PTYC", "B0FVX93K44"])
        self.assertIn("B0FVX93K44", parent["front_unavailable_asins"])
        self.assertEqual(snapshot["children"]["B0FVX93K44"]["inventory"], 14)
        self.assertEqual(snapshot["children"]["B0FVX93K44"]["front_status"], "不可售/404")

    def test_collect_snapshot_uses_sellersprite_parent_variations_when_pangolin_is_partial(self):
        seller_children = [
            "B0FFT34472",
            "B0FFT2BF9L",
            "B0FFSZ7J6L",
            "B0FFT2PHP9",
            "B0FFT28G1M",
            "B0FFT37H43",
            "B0FFT2KZYP",
            "B0FFT1F3PD",
            "B0FFT149KP",
            "B0FFT45VX5",
            "B0FFSZZ3BK",
            "B0FFT149JM",
            "B0FFT38NB4",
        ]
        pangolin_children = seller_children[:8]
        inventory_children = seller_children + ["B0FVX93K44", "B0FVX6PTYC"]

        def fake_pangolin(token, parser_name, content, *, site, zipcode, timeout):
            if content == "B0FFT1JQ9T":
                return {
                    "data": {
                        "json": {
                            "data": {
                                "results": [
                                    {
                                        "asin": content,
                                        "star": "4.5",
                                        "rating": "(61)",
                                        "variantDetails": [{"asin": child} for child in pangolin_children],
                                    }
                                ]
                            }
                        }
                    }
                }
            if content == "B0FVX93K44":
                return {"data": {"json": {"data": {"results": []}}}}
            if content == "B0FFT2PHP9":
                return {"data": {"json": {"data": {"results": [{"asin": "B0FFT149JM", "price": "$78.21", "fulfillment": "AMZ"}]}}}}
            return {"data": {"json": {"data": {"results": [{"asin": content, "price": "$1.00", "fulfillment": "AMZ"}]}}}}

        def fake_fallback(config, asin, marketplace, errors, label):
            if label == "parent":
                return {
                    "asin": asin,
                    "bsrRank": 274790,
                    "bsrLabel": "Home & Kitchen",
                    "subcategories": [{"rank": 120, "label": "Kids' Table & Chair Sets"}],
                    "rating": 4.5,
                    "ratings": 59,
                    "variations": 13,
                    "variationList": [{"asin": child} for child in seller_children],
                }, "SELLERSPRITE_MCP_URL"
            return {"asin": asin, "price": 1.0, "coupon": "", "fulfillment": "AMZ"}, "SELLERSPRITE_MCP_URL"

        with (
            patch("monitor.pangolin_scrape", side_effect=fake_pangolin),
            patch("monitor.fetch_fallback_detail", side_effect=fake_fallback),
            patch("monitor.fetch_inventory", return_value={"items": [{"asin": asin, "inventory": index} for index, asin in enumerate(inventory_children, 1)]}),
        ):
            snapshot = monitor.collect_snapshot(
                {
                    "PANGOLINFO_API_TOKEN": "token",
                    "MONITOR_PARENT_ASINS": "B0FFT1JQ9T",
                    "XINGSHANG_MCP_URL_TEMPLATE": "https://example.com/{parent_asin}",
                    "MARKETPLACE": "US",
                    "PANGOLIN_ZIPCODE": "10041",
                    "PANGOLIN_TIMEOUT_SECONDS": "1",
                    "MCP_TIMEOUT_SECONDS": "1",
                    "SELLERSPRITE_MCP_URL": "https://mcp.sellersprite.com/mcp",
                }
            )

        parent = snapshot["parents"]["B0FFT1JQ9T"]
        self.assertEqual(parent["child_asins"], sorted(set(seller_children) - {"B0FFT2PHP9"} | {"B0FVX6PTYC"}))
        self.assertEqual(parent["inventory_only_asins"], ["B0FFT2PHP9", "B0FVX93K44"])
        self.assertEqual(snapshot["children"]["B0FVX93K44"]["front_status"], "不可售/404")

    def test_collect_snapshot_uses_front_detail_validity_for_live_children(self):
        def fake_pangolin(token, parser_name, content, *, site, zipcode, timeout):
            if content == "B0FFT1JQ9T":
                return {"data": {"json": {"data": {"results": [{"asin": content, "variationList": [{"asin": "B0FFT2PHP9"}]}]}}}}
            if content == "B0FFT2PHP9":
                return {"data": {"json": {"data": {"results": [{"asin": "B0FFT149JM", "price": "$78.21", "fulfillment": "Amazon.com"}]}}}}
            if content == "B0FVX6PTYC":
                return {"data": {"json": {"data": {"results": [{"asin": content, "price": "$59.99", "fulfillment": "Amazon.com"}]}}}}
            if content == "B0FVX93K44":
                return {"data": {"json": {"data": {"results": [{"asin": content}]}}}}
            return {"data": {"json": {"data": {"results": []}}}}

        def fake_fallback(config, asin, marketplace, errors, label):
            if label == "parent":
                return {"asin": asin, "variationList": [{"asin": "B0FFT2PHP9"}]}, "SELLERSPRITE_MCP_URL"
            return {"asin": asin, "price": 1.0, "fulfillment": "AMZ"}, "SELLERSPRITE_MCP_URL"

        with (
            patch("monitor.pangolin_scrape", side_effect=fake_pangolin),
            patch("monitor.fetch_fallback_detail", side_effect=fake_fallback),
            patch("monitor.fetch_inventory", return_value={"items": [{"asin": "B0FFT2PHP9", "inventory": 0}, {"asin": "B0FVX6PTYC", "inventory": 22}, {"asin": "B0FVX93K44", "inventory": 70}]}),
        ):
            snapshot = monitor.collect_snapshot(
                {
                    "PANGOLINFO_API_TOKEN": "token",
                    "MONITOR_PARENT_ASINS": "B0FFT1JQ9T",
                    "XINGSHANG_MCP_URL_TEMPLATE": "https://example.com/{parent_asin}",
                    "MARKETPLACE": "US",
                    "PANGOLIN_ZIPCODE": "10041",
                    "PANGOLIN_TIMEOUT_SECONDS": "1",
                    "MCP_TIMEOUT_SECONDS": "1",
                    "SELLERSPRITE_MCP_URL": "https://mcp.sellersprite.com/mcp",
                }
            )

        parent = snapshot["parents"]["B0FFT1JQ9T"]
        self.assertEqual(parent["child_asins"], ["B0FVX6PTYC"])
        self.assertEqual(parent["inventory_only_asins"], ["B0FFT2PHP9", "B0FVX93K44"])
        self.assertIn("B0FFT2PHP9", parent["front_unavailable_asins"])
        self.assertIn("B0FVX93K44", parent["front_unavailable_asins"])
        self.assertEqual(snapshot["children"]["B0FVX6PTYC"]["price"], 59.99)
        self.assertEqual(snapshot["children"]["B0FVX6PTYC"]["inventory"], 22)
        self.assertEqual(snapshot["children"]["B0FFT2PHP9"]["front_status"], "不可售/404")
        self.assertEqual(snapshot["children"]["B0FVX93K44"]["front_status"], "不可售/404")

    def test_collect_snapshot_does_not_use_fallback_to_prove_front_validity_after_pangolin_timeout(self):
        def fake_pangolin(token, parser_name, content, *, site, zipcode, timeout):
            if content == "B0FFT1JQ9T":
                return {"data": {"json": {"data": {"results": [{"asin": content, "variationList": [{"asin": "B0FFT2PHP9"}]}]}}}}
            if content == "B0FFT2PHP9":
                raise monitor.MonitorError("timeout")
            return {"data": {"json": {"data": {"results": []}}}}

        def fake_fallback(config, asin, marketplace, errors, label):
            if label == "parent":
                return {"asin": asin, "variationList": [{"asin": "B0FFT2PHP9"}]}, "SELLERSPRITE_MCP_URL"
            return {"asin": asin, "price": 76.49, "fulfillment": "AMZ"}, "SELLERSPRITE_MCP_URL"

        with (
            patch("monitor.pangolin_scrape", side_effect=fake_pangolin),
            patch("monitor.fetch_fallback_detail", side_effect=fake_fallback),
            patch("monitor.fetch_inventory", return_value={"items": [{"asin": "B0FFT2PHP9", "inventory": 0}]}),
        ):
            snapshot = monitor.collect_snapshot(
                {
                    "PANGOLINFO_API_TOKEN": "token",
                    "MONITOR_PARENT_ASINS": "B0FFT1JQ9T",
                    "XINGSHANG_MCP_URL_TEMPLATE": "https://example.com/{parent_asin}",
                    "MARKETPLACE": "US",
                    "PANGOLIN_ZIPCODE": "10041",
                    "PANGOLIN_TIMEOUT_SECONDS": "1",
                    "MCP_TIMEOUT_SECONDS": "1",
                    "SELLERSPRITE_MCP_URL": "https://mcp.sellersprite.com/mcp",
                }
            )

        parent = snapshot["parents"]["B0FFT1JQ9T"]
        self.assertEqual(parent["child_asins"], [])
        self.assertEqual(parent["inventory_only_asins"], ["B0FFT2PHP9"])
        self.assertEqual(snapshot["children"]["B0FFT2PHP9"]["front_status"], "不可售/404")

    def test_collect_snapshot_defaults_pangolin_timeout_to_45(self):
        captured = []

        def fake_pangolin(token, parser_name, content, *, site, zipcode, timeout):
            captured.append(timeout)
            if content == "B0FFT1JQ9T":
                return {
                    "data": {
                        "json": {
                            "data": {
                                "results": [
                                    {
                                        "asin": content,
                                        "star": "4.5",
                                        "rating": "(61)",
                                        "variationList": [{"asin": "B0FFT34472"}],
                                    }
                                ]
                            }
                        }
                    }
                }
            return {"data": {"json": {"data": {"results": [{"asin": content, "price": "$1.00", "fulfillment": "AMZ"}]}}}}

        with (
            patch("monitor.pangolin_scrape", side_effect=fake_pangolin),
            patch("monitor.fetch_fallback_detail", return_value=({}, "")),
            patch("monitor.fetch_inventory", return_value={"items": []}),
        ):
            monitor.collect_snapshot(
                {
                    "PANGOLINFO_API_TOKEN": "token",
                    "MONITOR_PARENT_ASINS": "B0FFT1JQ9T",
                    "XINGSHANG_MCP_URL_TEMPLATE": "https://example.com/{parent_asin}",
                    "MARKETPLACE": "US",
                    "PANGOLIN_ZIPCODE": "10041",
                    "MCP_TIMEOUT_SECONDS": "1",
                }
            )

        self.assertTrue(captured)
        self.assertTrue(all(timeout == 45 for timeout in captured))

    def test_collect_snapshot_marks_empty_xingshang_inventory_source(self):
        def fake_pangolin(token, parser_name, content, *, site, zipcode, timeout):
            if content == "B0H1Q77TDL":
                return {"data": {"json": {"data": {"results": [{"asin": content, "variationList": [{"asin": "B0GJZYZHJJ"}]}]}}}}
            return {"data": {"json": {"data": {"results": [{"asin": content, "price": "$42.29", "inStock": "Only 16 left in stock - order soon."}]}}}}

        with (
            patch("monitor.pangolin_scrape", side_effect=fake_pangolin),
            patch("monitor.fetch_fallback_detail", return_value=({}, "")),
            patch("monitor.fetch_inventory", return_value={"success": True, "items": []}),
        ):
            snapshot = monitor.collect_snapshot(
                {
                    "PANGOLINFO_API_TOKEN": "token",
                    "MONITOR_PARENT_ASINS": "B0H1Q77TDL",
                    "XINGSHANG_MCP_URL_TEMPLATE": "https://example.com/{parent_asin}",
                    "MARKETPLACE": "US",
                    "PANGOLIN_ZIPCODE": "10041",
                    "MCP_TIMEOUT_SECONDS": "1",
                }
            )

        self.assertEqual(snapshot["parents"]["B0H1Q77TDL"]["inventory_source"], "xingshang_empty")
        self.assertEqual(snapshot["children"]["B0GJZYZHJJ"]["inventory"], 16)
        self.assertIn("库存 xingshang 未返回库存明细", monitor.format_snapshot_report(snapshot))

    def test_collect_snapshot_retries_empty_xingshang_with_front_child_asins(self):
        inventory_calls = []

        def fake_pangolin(token, parser_name, content, *, site, zipcode, timeout):
            if content == "B0H1Q77TDL":
                return {
                    "data": {
                        "json": {
                            "data": {
                                "results": [
                                    {
                                        "asin": content,
                                        "variationList": [{"asin": "B0G5P8HWZ8"}, {"asin": "B0GJZYZHJJ"}],
                                    }
                                ]
                            }
                        }
                    }
                }
            return {"data": {"json": {"data": {"results": [{"asin": content, "price": "$42.29"}]}}}}

        def fake_inventory(parent_asin, url_template, timeout=30, force_refresh=False, spu_item_id_list=None):
            inventory_calls.append(spu_item_id_list)
            if spu_item_id_list:
                return {
                    "items": [
                        {"asin": "B0G5P8HWZ8", "inventory": 21},
                        {"asin": "B0GJZYZHJJ", "inventory": 13},
                    ]
                }
            return {"success": True, "items": []}

        with (
            patch("monitor.pangolin_scrape", side_effect=fake_pangolin),
            patch("monitor.fetch_fallback_detail", return_value=({}, "")),
            patch("monitor.fetch_inventory", side_effect=fake_inventory),
        ):
            snapshot = monitor.collect_snapshot(
                {
                    "PANGOLINFO_API_TOKEN": "token",
                    "MONITOR_PARENT_ASINS": "B0H1Q77TDL",
                    "XINGSHANG_MCP_URL_TEMPLATE": "https://example.com/{parent_asin}",
                    "MARKETPLACE": "US",
                    "PANGOLIN_ZIPCODE": "10041",
                    "MCP_TIMEOUT_SECONDS": "1",
                }
            )

        self.assertEqual(inventory_calls, [None, ["B0G5P8HWZ8", "B0GJZYZHJJ"]])
        self.assertEqual(snapshot["parents"]["B0H1Q77TDL"]["inventory_source"], "xingshang")
        self.assertEqual(snapshot["children"]["B0G5P8HWZ8"]["inventory"], 21)
        self.assertEqual(snapshot["children"]["B0GJZYZHJJ"]["inventory"], 13)

    def test_collect_snapshot_uses_previous_inventory_when_xingshang_times_out(self):
        previous = {
            "parents": {"B0FFT1JQ9T": {"child_asins": ["B0FFT34472", "B0FVX93K44"]}},
            "children": {
                "B0FFT34472": {"inventory": 295},
                "B0FVX93K44": {"inventory": 70},
            },
        }

        def fake_fallback(config, asin, marketplace, errors, label):
            if label == "parent":
                return {
                    "asin": asin,
                    "bsrRank": 274790,
                    "bsrLabel": "Home & Kitchen",
                    "rating": 4.5,
                    "ratings": 59,
                    "variationList": [{"asin": "B0FFT34472"}],
                }, "SELLERSPRITE_MCP_URL"
            return {"asin": asin, "price": 1.0, "coupon": "", "fulfillment": "AMZ"}, "SELLERSPRITE_MCP_URL"

        with (
            patch(
                "monitor.pangolin_scrape",
                side_effect=lambda token, parser_name, content, *, site, zipcode, timeout: {"data": {"json": {"data": {"results": [{"asin": content, "price": "$1.00", "fulfillment": "AMZ"}] if content == "B0FFT34472" else []}}}},
            ),
            patch("monitor.fetch_fallback_detail", side_effect=fake_fallback),
            patch("monitor.fetch_inventory", side_effect=TimeoutError()),
        ):
            snapshot = monitor.collect_snapshot(
                {
                    "PANGOLINFO_API_TOKEN": "token",
                    "MONITOR_PARENT_ASINS": "B0FFT1JQ9T",
                    "XINGSHANG_MCP_URL_TEMPLATE": "https://example.com/{parent_asin}",
                    "MARKETPLACE": "US",
                    "PANGOLIN_ZIPCODE": "10041",
                    "PANGOLIN_TIMEOUT_SECONDS": "1",
                    "MCP_TIMEOUT_SECONDS": "1",
                    "XINGSHANG_TIMEOUT_SECONDS": "30",
                },
                previous=previous,
            )

        self.assertEqual(snapshot["parents"]["B0FFT1JQ9T"]["inventory_source"], "previous_snapshot")
        self.assertEqual(snapshot["children"]["B0FVX93K44"]["inventory"], 70)
        self.assertNotIn("B0FVX93K44", snapshot["parents"]["B0FFT1JQ9T"]["child_asins"])
        self.assertIn("B0FVX93K44", snapshot["parents"]["B0FFT1JQ9T"]["inventory_only_asins"])
        self.assertTrue(any("xingshang failed" in error for error in snapshot["errors"]))

    def test_inventory_only_positive_stock_triggers_change_alert(self):
        previous = {
            "parents": {"B0FFT1JQ9T": {"child_asins": ["B0FFT34472"], "inventory_only_asins": []}},
            "children": {"B0FFT34472": {"inventory": 295}},
            "errors": [],
        }
        current = {
            "parents": {"B0FFT1JQ9T": {"child_asins": ["B0FFT34472"], "inventory_only_asins": ["B0FVX93K44"]}},
            "children": {"B0FFT34472": {"inventory": 295}, "B0FVX93K44": {"inventory": 70, "front_status": "不可售/404"}},
            "errors": [],
        }

        changes = monitor.diff_snapshots(previous, current)
        message = monitor.format_message(changes, captured_at="2026-07-10T01:00:12Z")

        self.assertIn("B0FFT1JQ9T inventory-only child added: B0FVX93K44", changes)
        self.assertIn("库存侧异常：", message)
        self.assertIn("新增库存侧异常：B0FFT1JQ9T / B0FVX93K44", message)

    def test_collect_snapshot_supplements_partial_pangolin_child_from_fallback(self):
        def fake_pangolin(token, parser_name, content, *, site, zipcode, timeout):
            if content == "B0FFT1JQ9T":
                return {"data": {"json": {"data": {"results": [{"asin": content, "variationList": [{"asin": "B0FVX93K44"}]}]}}}}
            return {"data": {"json": {"data": {"results": [{"asin": content, "coupon": ""}]}}}}

        def fake_fallback(config, asin, marketplace, errors, label):
            if label == "parent":
                return {"asin": asin, "ratings": 59}, "SELLERSPRITE_MCP_URL"
            return {"asin": asin, "price": 89.99, "coupon": "", "fulfillment": "AMZ"}, "SELLERSPRITE_MCP_URL"

        with (
            patch("monitor.pangolin_scrape", side_effect=fake_pangolin),
            patch("monitor.fetch_fallback_detail", side_effect=fake_fallback),
            patch("monitor.fetch_inventory", return_value={"items": [{"asin": "B0FVX93K44", "inventory": 70}]}),
        ):
            snapshot = monitor.collect_snapshot(
                {
                    "PANGOLINFO_API_TOKEN": "token",
                    "MONITOR_PARENT_ASINS": "B0FFT1JQ9T",
                    "XINGSHANG_MCP_URL_TEMPLATE": "https://example.com/{parent_asin}",
                    "MARKETPLACE": "US",
                    "PANGOLIN_ZIPCODE": "10041",
                    "PANGOLIN_TIMEOUT_SECONDS": "1",
                    "MCP_TIMEOUT_SECONDS": "1",
                }
            )

        child = snapshot["children"]["B0FVX93K44"]
        self.assertEqual(child["price"], 89.99)
        self.assertEqual(child["fulfillment_method"], "AMZ")
        self.assertEqual(child["inventory"], 70)

    def test_sellersprite_mcp_uses_secret_key_header(self):
        captured = {}

        async def fake_call(server_url, fragments, args, headers=None):
            captured["server_url"] = server_url
            captured["fragments"] = tuple(fragments)
            captured["args"] = dict(args)
            captured["headers"] = dict(headers or {})
            return {"data": {"asin": {"asin": "B0FFT34472", "price": 85.53}}}

        with patch("monitor.call_mcp_tool", side_effect=fake_call):
            detail = monitor.fetch_optional_detail_from_mcp(
                "https://mcp.sellersprite.com/mcp",
                "B0FFT34472",
                "US",
                headers={"secret-key": "secret"},
                fragments=("asin_detail_with_coupon_trend",),
                timeout=1,
            )

        self.assertEqual(detail["price"], 85.53)
        self.assertEqual(captured["headers"], {"secret-key": "secret"})
        self.assertEqual(captured["fragments"], ("asin_detail_with_coupon_trend",))

    def test_current_report_marks_unknown_values_as_unknown(self):
        message = monitor.format_snapshot_report(
            {
                "captured_at": "2026-07-09T03:30:00Z",
                "parents": {"PARENT1234": {"child_asins": ["CHILD00001"], "source": "xingshang_inventory_only"}},
                "children": {"CHILD00001": {"asin": "CHILD00001", "inventory": 7, "source": "xingshang_inventory_only"}},
                "errors": ["PARENT1234: pangolin parent empty"],
            }
        )

        self.assertIn("前台数据缺失", message)
        self.assertIn("时效 未覆盖", message)
        self.assertNotIn("退货", message)
        self.assertNotIn("frequently return: False", message)

    def test_formats_compact_report_without_internal_source_noise(self):
        child_asins = ["B0FFT34472"]
        snapshot = {
            "captured_at": "2026-07-09T03:52:28Z",
            "parents": {
                "B0FFT1JQ9T": {
                    "major_rank": 274790,
                    "major_category": "Home & Kitchen",
                    "minor_rank": 120,
                    "minor_category": "Kids' Table & Chair Sets",
                    "stars": 4.5,
                    "rating_count": 59,
                    "child_asins": child_asins,
                    "inventory_only_asins": ["B0FVX93K44"],
                    "front_unavailable_asins": ["B0FVX93K44"],
                    "source": "SELLERSPRITE_MCP_URL",
                    "inventory_source": "xingshang",
                }
            },
            "children": {
                "B0FFT34472": {"price": 85.53, "coupon": "", "promotion": "7-Day Deal", "inventory": 295, "fulfillment_method": "AMZ", "source": "SELLERSPRITE_MCP_URL"},
                "B0FVX93K44": {"inventory": 70, "front_status": "不可售/404", "source": "xingshang_inventory_only"},
            },
            "warnings": [
                "B0FFT1JQ9T: pangolin parent empty; using SELLERSPRITE_MCP_URL",
                "B0FFT34472: pangolin child empty; using SELLERSPRITE_MCP_URL",
            ],
            "errors": [],
        }

        message = monitor.format_snapshot_report(snapshot)

        self.assertIn("状态：完整数据（SellerSprite 补源）", message)
        self.assertIn("子体：1", message)
        self.assertIn("B0FFT34472｜价 85.53｜库存 295｜Coupon 无｜促销 7-Day Deal｜时效 未覆盖", message)
        self.assertNotIn("配送 AMZ", message)
        self.assertNotIn("退货", message)
        self.assertIn("库存侧异常：", message)
        self.assertIn("B0FVX93K44｜库存 70｜前台状态 不可售/404｜来源 xingshang", message)
        self.assertIn("数据源：前台 SellerSprite；库存 xingshang；Pangolinfo 空结果已补源", message)
        self.assertNotIn("SELLERSPRITE_MCP_URL", message)
        self.assertNotIn("pangolin child empty", message)
        self.assertNotIn("frequently return", message)


if __name__ == "__main__":
    unittest.main()
