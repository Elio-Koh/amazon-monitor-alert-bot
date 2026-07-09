import base64
import json
import os
import unittest

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


if __name__ == "__main__":
    unittest.main()
