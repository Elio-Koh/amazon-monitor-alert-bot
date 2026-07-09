import base64
import json
import os
import unittest
from unittest.mock import patch

import monitor


class MonitorTest(unittest.TestCase):
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
                "CHILD00001": {"price": 11.0, "inventory": 0, "coupon": "5% coupon"},
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
        self.assertIn("PARENT1234: xingshang failed", lines)

    def test_feishu_payload_uses_optional_signature(self):
        unsigned = monitor.feishu_payload("hello", timestamp=123, secret="")
        signed = monitor.feishu_payload("hello", timestamp=123, secret="secret")

        self.assertNotIn("sign", unsigned)
        self.assertEqual(unsigned["msg_type"], "text")
        self.assertIn("sign", signed)
        self.assertEqual(signed["timestamp"], "123")

    def test_encrypted_state_round_trips(self):
        key = base64.urlsafe_b64encode(os.urandom(32)).decode()
        snapshot = {"parents": {"PARENT1234": {"major_rank": 1}}, "children": {}, "errors": []}

        encrypted = monitor.encrypt_snapshot(snapshot, key)
        decoded = json.loads(encrypted)

        self.assertIn("data", decoded)
        self.assertEqual(monitor.decrypt_snapshot(encrypted, key), snapshot)

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
                    "promotion": "",
                    "frequently_returned": False,
                    "inventory": 7,
                    "fulfillment_method": "FBA",
                    "delivery_promise": "Tomorrow",
                }
            },
            "errors": [],
        }

        message = monitor.format_snapshot_report(snapshot)

        self.assertIn("ASIN 今日数据", message)
        self.assertIn("PARENT1234", message)
        self.assertIn("大类排名: 4335 (Home & Kitchen)", message)
        self.assertIn("小类排名: 16 (Milk Frothers)", message)
        self.assertIn("CHILD00001", message)
        self.assertIn("价格: 23.99", message)
        self.assertIn("库存: 7", message)

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
                    "fulfillment": "AMZ",
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
        self.assertEqual(child["coupon"], "coupon 5.68; final 65.31")
        self.assertEqual(child["fulfillment_method"], "AMZ")

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
        self.assertIn("frequently return: 未知", message)
        self.assertNotIn("frequently return: False", message)


if __name__ == "__main__":
    unittest.main()
