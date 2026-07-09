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
        self.assertIn("CHILD00001 child promotion: None -> 7-Day Deal", lines)
        self.assertIn("PARENT1234: xingshang failed", lines)

        message = monitor.format_message(changes, captured_at="2026-07-10T01:00:12Z")

        self.assertIn("ASIN 变化提醒｜北京时间 2026-07-10 09:00:12", message)
        self.assertIn("状态：发现 9 项变化", message)
        self.assertIn("父 ASIN PARENT1234", message)
        self.assertIn("大类排名：5 → 7", message)
        self.assertIn("评论数：100 → 101", message)
        self.assertIn("1. CHILD00001", message)
        self.assertIn("价格：10.0 → 11.0", message)
        self.assertIn("促销/Deal：未知 → 7-Day Deal", message)
        self.assertIn("数据源异常：", message)

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

    def test_fetch_inventory_defaults_to_non_force_refresh(self):
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
        self.assertIn("退货 未覆盖", message)
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
        self.assertIn("B0FFT34472｜价 85.53｜库存 295｜Coupon 无｜促销 7-Day Deal｜配送 AMZ｜退货 未覆盖｜时效 未覆盖", message)
        self.assertIn("库存侧异常：", message)
        self.assertIn("B0FVX93K44｜库存 70｜前台状态 不可售/404｜来源 xingshang", message)
        self.assertIn("数据源：前台 SellerSprite；库存 xingshang；Pangolinfo 空结果已补源", message)
        self.assertNotIn("SELLERSPRITE_MCP_URL", message)
        self.assertNotIn("pangolin child empty", message)
        self.assertNotIn("frequently return", message)


if __name__ == "__main__":
    unittest.main()
