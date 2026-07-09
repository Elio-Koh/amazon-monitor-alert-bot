#!/usr/bin/env python3
"""Daily ASIN monitor for Feishu alerts."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from cryptography.fernet import Fernet


PANGOLIN_BASE_URL = "https://scrapeapi.pangolinfo.com"
PARENT_FIELDS = ("major_rank", "minor_rank", "stars", "rating_count")
CHILD_FIELDS = (
    "price",
    "coupon",
    "promotion",
    "frequently_returned",
    "inventory",
    "fulfillment_method",
    "delivery_promise",
)
SITE_BY_MARKETPLACE = {"US": "amz_us", "CA": "amz_ca", "UK": "amz_uk", "DE": "amz_de", "AU": "amz_au", "MX": "amz_mx"}
ASIN_PATTERN = re.compile(r"^[A-Z0-9]{10}$")
BEIJING_TZ = timezone(timedelta(hours=8))
DELIVERY_KEYS = {
    "delivery",
    "deliverydate",
    "delivery_date",
    "deliveryinfo",
    "delivery_info",
    "deliverypromise",
    "delivery_promise",
    "deliverytime",
    "delivery_time",
    "estimateddelivery",
    "estimated_delivery",
    "fastestdelivery",
    "fastest_delivery",
    "arrival",
    "arrivaldate",
    "arrival_date",
    "availability",
}
RETURN_BADGE_KEYS = {
    "frequentlyreturned",
    "frequently_returned",
    "frequently_return",
    "frequentlyreturnedbadge",
    "highreturnrate",
    "high_return_rate",
    "returnratewarning",
    "return_rate_warning",
    "returnwarning",
    "return_warning",
    "productbadges",
    "badges",
    "badge",
}
RETURN_BADGE_TEXT_PATTERNS = (
    "frequently returned item",
    "frequently returned",
    "high return",
    "highly returned",
    "return warning",
)


class MonitorError(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def format_report_time(value: Any) -> str:
    text = first_text(value)
    if not text:
        dt = datetime.now(timezone.utc)
    else:
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return text
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(BEIJING_TZ).strftime("北京时间 %Y-%m-%d %H:%M:%S")


def first_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, Mapping):
        for key in ("text", "value", "name", "title", "label", "deliveryTime", "deliveryDate", "fastestDelivery", "estimatedDelivery", "arrivalDate"):
            text = first_text(value.get(key))
            if text:
                return text
        return None
    if isinstance(value, list):
        for item in value:
            text = first_text(item)
            if text:
                return text
        return None
    return str(value).strip() or None


def first_present(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def find_nested_value(value: Any, keys: set[str]) -> Any:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).replace("-", "_").lower() in keys:
                return child
        for child in value.values():
            found = find_nested_value(child, keys)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_nested_value(child, keys)
            if found is not None:
                return found
    return None


def contains_text(value: Any, patterns: Sequence[str]) -> bool:
    if isinstance(value, str):
        lower = value.lower()
        return any(pattern in lower for pattern in patterns)
    if isinstance(value, Mapping):
        return any(contains_text(child, patterns) for child in value.values())
    if isinstance(value, list):
        return any(contains_text(child, patterns) for child in value)
    return False


def listify(value: Any) -> List[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def parse_float(value: Any) -> Optional[float]:
    text = first_text(value)
    if not text:
        return None
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)", text.replace(",", ""))
    return float(match.group(1)) if match else None


def parse_int(value: Any) -> Optional[int]:
    text = first_text(value)
    if not text:
        return None
    match = re.search(r"([0-9][0-9,]*)", text)
    return int(match.group(1).replace(",", "")) if match else None


def parse_inventory(value: Any) -> Optional[int]:
    text = first_text(value)
    if not text:
        return None
    lower = text.lower()
    if "out of stock" in lower or "currently unavailable" in lower or "unavailable" in lower:
        return 0
    return parse_int(text)


def config_int(config: Mapping[str, str], key: str, default: int) -> int:
    value = parse_int(config.get(key))
    return value if value is not None and value > 0 else default


def config_bool(config: Mapping[str, str], key: str, default: bool = False) -> bool:
    value = parse_bool(config.get(key))
    return default if value is None else value


def is_asin(value: Any) -> bool:
    text = first_text(value)
    return bool(text and ASIN_PATTERN.fullmatch(text.upper()))


def parse_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    text = first_text(value)
    if not text:
        return None
    lower = text.lower()
    if lower in {"true", "yes", "y", "1", "on", "deal", "lightning_deal"}:
        return True
    if lower in {"false", "no", "n", "0", "off"}:
        return False
    if any(phrase in lower for phrase in ("frequently returned", "high return", "highly returned")):
        return True
    return None


def parse_rank_items(value: Any) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []

    def add(rank: Optional[int], category: Optional[str]) -> None:
        if rank is None or not category:
            return
        item = {"rank": rank, "category": category.strip(" .:-")}
        if item["category"] and item not in items:
            items.append(item)

    def parse_value(raw: Any) -> None:
        if raw is None:
            return
        if isinstance(raw, list):
            for child in raw:
                parse_value(child)
            return
        if isinstance(raw, Mapping):
            add(
                parse_int(raw.get("rank") or raw.get("bsr_rank") or raw.get("position") or raw.get("value")),
                first_text(raw.get("category") or raw.get("categoryName") or raw.get("label") or raw.get("name")),
            )
            parse_value(raw.get("text") or raw.get("display") or raw.get("bestSellersRank"))
            return
        text = first_text(raw)
        if not text:
            return
        for match in re.finditer(r"#?\s*([0-9][0-9,]*)\s+in\s+([^#;\n|]+)", text, flags=re.I):
            add(int(match.group(1).replace(",", "")), match.group(2))

    parse_value(value)
    return items


def extract_child_asins(detail: Mapping[str, Any]) -> List[str]:
    found: List[str] = []
    candidates = [
        detail.get("variationList"),
        detail.get("variationAsins"),
        detail.get("childAsins"),
        detail.get("children"),
        detail.get("variantDetails"),
    ]
    if isinstance(detail.get("variations"), list):
        candidates.append(detail.get("variations"))
    for value in candidates:
        for item in listify(value):
            asin = first_text(item.get("asin") if isinstance(item, Mapping) else item)
            if asin and is_asin(asin):
                found.append(asin.upper())
    return sorted(set(found))


def normalize_delivery(value: Any) -> Optional[str]:
    if isinstance(value, Mapping):
        delivery = first_text(value.get("deliveryTime") or value.get("deliveryDate") or value.get("delivery") or value.get("estimatedDelivery") or value.get("arrivalDate"))
        fastest = first_text(value.get("fastestDelivery"))
        if delivery and fastest:
            return f"{delivery}; fastest {fastest}"
        return delivery or fastest
    return first_text(value)


def front_detail_is_valid(detail: Mapping[str, Any], requested_asin: Optional[str] = None) -> bool:
    if not detail:
        return False
    asin = first_text(detail.get("asin"))
    if asin is not None:
        if not is_asin(asin):
            return False
        if requested_asin and asin.upper() != requested_asin.upper():
            return False
    return bool(
        parse_float(detail.get("price") or detail.get("finalPrice") or detail.get("price_display")) is not None
        or first_text(detail.get("title"))
        or normalize_delivery(
            detail.get("delivery")
            or detail.get("deliveryTime")
            or detail.get("deliveryPromise")
            or find_nested_value(detail, DELIVERY_KEYS)
        )
        or first_text(detail.get("seller"))
    )


def normalize_promotion(detail: Mapping[str, Any]) -> Optional[str]:
    parts: List[str] = []

    def add(value: Any) -> None:
        text = first_text(value)
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
    savings = first_text(detail.get("savingsPercentage"))
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


def unwrap_detail_payload(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, Mapping):
        return {}
    data = payload.get("data", payload)
    if not isinstance(data, Mapping):
        return {}
    detail = data.get("asin", data)
    if not isinstance(detail, Mapping):
        return {}
    out = dict(detail)
    if "couponTrends" in data:
        out["couponTrends"] = data["couponTrends"]
    return out


def normalize_parent(parent_asin: str, detail: Mapping[str, Any], source: str) -> Dict[str, Any]:
    rank_items = parse_rank_items(
        detail.get("bestSellersRankItems")
        or detail.get("subcategories")
        or detail.get("bestSellersRank")
        or detail.get("bsrRank")
    )
    if detail.get("bsrRank") is not None:
        bsr_item = {"rank": parse_int(detail.get("bsrRank")), "category": first_text(detail.get("bsrLabel") or detail.get("categoryName") or "BSR")}
        if bsr_item["rank"] is not None and bsr_item not in rank_items:
            rank_items.insert(0, bsr_item)
    rating_count_source = detail.get("ratings") or detail.get("rating_count") or detail.get("customerReviews") or detail.get("reviewCount")
    rating_text = first_text(detail.get("rating"))
    if rating_count_source is None and rating_text and "rating" in rating_text.lower():
        rating_count_source = rating_text
    if rating_count_source is None and detail.get("star") is not None:
        rating_count_source = rating_text
    return {
        "asin": parent_asin.upper(),
        "major_rank": rank_items[0]["rank"] if rank_items else parse_int(detail.get("bsrRank")),
        "major_category": rank_items[0]["category"] if rank_items else first_text(detail.get("bsrLabel")),
        "minor_rank": rank_items[-1]["rank"] if rank_items else None,
        "minor_category": rank_items[-1]["category"] if rank_items else None,
        "stars": parse_float(detail.get("star") or detail.get("rating") or detail.get("ratingValue")),
        "rating_count": parse_int(rating_count_source),
        "child_asins": extract_child_asins(detail),
        "source": source,
    }


def normalize_child(child_asin: str, detail: Mapping[str, Any], inventory: Optional[int], source: str) -> Dict[str, Any]:
    badge = detail.get("badge") if isinstance(detail.get("badge"), Mapping) else {}
    has_detail = any(key != "asin" for key in detail)
    coupon = first_text(detail.get("coupon") or detail.get("couponInfo") or detail.get("couponText"))
    promotion = normalize_promotion(detail)
    return_badge = first_present(
        detail.get("frequentlyReturned"),
        detail.get("frequently_returned"),
        detail.get("frequently_return"),
        badge.get("frequentlyReturned"),
        find_nested_value(detail, RETURN_BADGE_KEYS),
    )
    delivery = first_present(
        detail.get("deliveryTime"),
        detail.get("delivery"),
        detail.get("deliveryPromise"),
        detail.get("delivery_promise"),
        detail.get("availability"),
        find_nested_value(detail, DELIVERY_KEYS),
    )
    front_inventory = parse_inventory(detail.get("inStock") or detail.get("stock") or detail.get("stockStatus") or detail.get("availability"))
    frequently_returned = parse_bool(return_badge)
    if frequently_returned is None and contains_text(detail, RETURN_BADGE_TEXT_PATTERNS):
        frequently_returned = True
    if frequently_returned is None and str(source).startswith("pangolin") and front_detail_is_valid(detail, child_asin):
        frequently_returned = False
    return {
        "asin": child_asin.upper(),
        "price": parse_float(detail.get("price") or detail.get("finalPrice") or detail.get("price_display")),
        "coupon": coupon if coupon is not None else ("" if has_detail else None),
        "promotion": promotion if promotion is not None else ("" if has_detail else None),
        "frequently_returned": frequently_returned,
        "inventory": inventory if inventory is not None else front_inventory,
        "inventory_source": "xingshang" if inventory is not None else ("front_detail" if front_inventory is not None else None),
        "fulfillment_method": first_text(
            detail.get("fulfillment")
            or detail.get("fulfillmentMethod")
            or detail.get("fulfillment_method")
            or detail.get("seller")
        ),
        "delivery_promise": normalize_delivery(delivery),
        "source": source,
    }


def merge_missing_detail(primary: Mapping[str, Any], fallback: Mapping[str, Any]) -> Dict[str, Any]:
    merged = dict(primary)
    for key, value in fallback.items():
        current = merged.get(key)
        if current in (None, "", [], {}):
            merged[key] = value
    return merged


def parent_needs_supplement(parent_asin: str, detail: Mapping[str, Any]) -> bool:
    parent = normalize_parent(parent_asin, {**detail, "asin": parent_asin}, "pangolin")
    return parent.get("rating_count") is None or not parent.get("child_asins")


def child_needs_supplement(child_asin: str, detail: Mapping[str, Any]) -> bool:
    child = normalize_child(child_asin, {**detail, "asin": child_asin}, None, "pangolin")
    return child.get("price") is None or child.get("fulfillment_method") is None


def inventory_by_asin(payload: Mapping[str, Any]) -> Dict[str, Optional[int]]:
    out: Dict[str, Optional[int]] = {}
    for item in listify(payload.get("items") if isinstance(payload, Mapping) else []):
        if not isinstance(item, Mapping):
            continue
        asin = first_text(item.get("asin"))
        if asin:
            out[asin.upper()] = parse_int(item.get("inventory"))
    return out


def merge_child_asins(parent: Mapping[str, Any], children: Sequence[Mapping[str, Any]], inventory_payload: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    inventories = inventory_by_asin(inventory_payload)
    child_asins = set(str(asin).upper() for asin in parent.get("child_asins") or [])
    child_asins.update(str(row.get("asin", "")).upper() for row in children if row.get("asin"))
    merged = {str(row.get("asin")).upper(): dict(row) for row in children if row.get("asin")}
    for asin in sorted(child_asins):
        row = merged.setdefault(asin, {"asin": asin})
        if asin in inventories:
            row["inventory"] = inventories[asin]
    return dict(sorted(merged.items()))


def previous_inventory_payload(previous: Optional[Mapping[str, Any]], parent_asin: str) -> Dict[str, Any]:
    if not isinstance(previous, Mapping):
        return {}
    parents = previous.get("parents", {}) if isinstance(previous.get("parents"), Mapping) else {}
    children = previous.get("children", {}) if isinstance(previous.get("children"), Mapping) else {}
    parent = parents.get(parent_asin.upper(), {}) if isinstance(parents.get(parent_asin.upper(), {}), Mapping) else {}
    child_asins = set(str(asin).upper() for asin in parent.get("child_asins") or [])
    child_asins.update(str(asin).upper() for asin in children)
    items = []
    for asin in sorted(child_asins):
        if not is_asin(asin):
            continue
        row = children.get(asin, {})
        if not isinstance(row, Mapping) or row.get("inventory") is None:
            continue
        items.append({"asin": asin, "inventory": row.get("inventory")})
    return {"items": items} if items else {}


def _json_body(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def http_json(method: str, url: str, payload: Optional[Mapping[str, Any]] = None, headers: Optional[Mapping[str, str]] = None, timeout: int = 30) -> Any:
    req = urllib.request.Request(url, data=_json_body(payload) if payload is not None else None, headers=dict(headers or {}), method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise MonitorError(f"HTTP {exc.code} from {url}: {body[:300]}") from exc
    except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
        raise MonitorError(f"Network error from {url}: {exc}") from exc
    return json.loads(body) if body else {}


def pangolin_scrape(api_token: str, parser_name: str, content: str, *, site: str, zipcode: str, timeout: int = 45) -> Dict[str, Any]:
    return http_json(
        "POST",
        f"{PANGOLIN_BASE_URL}/api/v1/scrape",
        {
            "url": "",
            "parserName": parser_name,
            "site": site,
            "content": content,
            "format": "json",
            "bizContext": {"zipcode": zipcode},
        },
        {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
            "User-Agent": "amazon-monitor-alert-bot/1.0",
        },
        timeout=timeout,
    )


def extract_results(response: Mapping[str, Any]) -> List[Dict[str, Any]]:
    code = response.get("code")
    if code not in {None, 0, "0", 200, "200", "OK", "ok"} and response.get("data") is None:
        raise MonitorError(f"pangolin response {code}: {first_text(response.get('message')) or 'unknown error'}")
    data = response.get("data")
    if not isinstance(data, Mapping):
        return []
    raw = data.get("json", data)
    entries = raw if isinstance(raw, list) else [raw]
    rows: List[Dict[str, Any]] = []
    for entry in entries:
        if isinstance(entry, str):
            try:
                entry = json.loads(entry)
            except json.JSONDecodeError:
                continue
        if not isinstance(entry, Mapping):
            continue
        payload = entry.get("data", entry)
        if not isinstance(payload, Mapping):
            continue
        for key in ("results", "items"):
            value = payload.get(key)
            if isinstance(value, Mapping):
                value = value.get("data")
            if isinstance(value, list):
                rows.extend(row for row in value if isinstance(row, Mapping))
    return rows


async def call_mcp_tool(server_url: str, name_fragments: Iterable[str], args: Mapping[str, Any], headers: Optional[Mapping[str, str]] = None) -> Any:
    import httpx
    from mcp import ClientSession
    from mcp.client.sse import sse_client
    from mcp.client.streamable_http import streamable_http_client

    fragments = [fragment.lower() for fragment in name_fragments]

    async def call_with_streamable() -> Any:
        async with httpx.AsyncClient(headers=dict(headers or {}), follow_redirects=True) as client:
            async with streamable_http_client(server_url, http_client=client) as streams:
                read_stream, write_stream = streams[0], streams[1]
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    return await _call_matching_tool(session, [tool.name for tool in tools.tools], fragments, args)

    async def call_with_sse() -> Any:
        async with sse_client(server_url, headers=dict(headers or {})) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                tools = await session.list_tools()
                return await _call_matching_tool(session, [tool.name for tool in tools.tools], fragments, args)

    try:
        return await call_with_streamable()
    except Exception:
        return await call_with_sse()


async def _call_matching_tool(session: Any, names: Sequence[str], fragments: Sequence[str], args: Mapping[str, Any]) -> Any:
    for name in names:
        compact = name.lower()
        if all(fragment in compact for fragment in fragments):
            return mcp_result_to_json(await session.call_tool(name, dict(args)))
    raise MonitorError(f"MCP tool not found for fragments: {', '.join(fragments)}")


def mcp_result_to_json(result: Any) -> Any:
    content = getattr(result, "content", result)
    if isinstance(content, list) and content:
        text = getattr(content[0], "text", None)
        if text is not None:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"raw_text": text}
    return result


def run_mcp(coro: Any, timeout: int) -> Any:
    return asyncio.run(asyncio.wait_for(coro, timeout=timeout))


def fetch_inventory(parent_asin: str, url_template: str, timeout: int = 30, force_refresh: bool = False) -> Dict[str, Any]:
    if not url_template:
        return {}
    server_url = url_template.format(parent_asin=parent_asin.upper(), PARENT_ASIN=parent_asin.upper())
    if not server_url.endswith("/"):
        server_url += "/"
    return run_mcp(call_mcp_tool(server_url, ("get_store_asin_info",), {"spu_item_id_list": [parent_asin.upper()], "force_refresh": force_refresh}), timeout)


def fetch_optional_detail_from_mcp(
    server_url: str,
    asin: str,
    marketplace: str,
    *,
    headers: Optional[Mapping[str, str]] = None,
    fragments: Iterable[str] = ("asin_detail",),
    timeout: int = 30,
) -> Dict[str, Any]:
    if not server_url:
        return {}
    payload = run_mcp(call_mcp_tool(server_url, fragments, {"asin": asin.upper(), "marketplace": marketplace}, headers=headers), timeout)
    return unwrap_detail_payload(payload)


def mcp_headers(config: Mapping[str, str], key: str) -> Dict[str, str]:
    if key == "SELLERSPRITE_MCP_URL" and config.get("SELLERSPRITE_MCP_SECRET_KEY"):
        return {"secret-key": config["SELLERSPRITE_MCP_SECRET_KEY"]}
    return {}


def fetch_fallback_detail(config: Mapping[str, str], asin: str, marketplace: str, errors: List[str], label: str) -> tuple[Dict[str, Any], str]:
    timeout = config_int(config, "MCP_TIMEOUT_SECONDS", 20)
    attempts = (
        ("SELLERSPRITE_MCP_URL", ("asin_detail_with_coupon_trend",)),
        ("SELLERSPRITE_MCP_URL", ("asin_detail",)),
        ("SORFTIME_MCP_URL", ("asin_detail",)),
        ("SIF_MCP_URL", ("asin_detail",)),
    )
    for key, fragments in attempts:
        if not config.get(key):
            continue
        try:
            detail = fetch_optional_detail_from_mcp(config[key], asin, marketplace, headers=mcp_headers(config, key), fragments=fragments, timeout=timeout)
        except Exception as exc:
            errors.append(f"{asin}: {key} {label} failed: {exc}")
            continue
        if detail:
            return detail, key
    return {}, ""


def describe_exception(exc: BaseException, *, timeout: Optional[int] = None) -> str:
    message = str(exc).strip()
    suffix = f", timeout {timeout}s" if timeout else ""
    return f"{type(exc).__name__}{suffix}: {message}" if message else f"{type(exc).__name__}{suffix}"


def collect_snapshot(config: Mapping[str, str], previous: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    site = SITE_BY_MARKETPLACE.get(config.get("MARKETPLACE", "US").upper(), "amz_us")
    marketplace = config.get("MARKETPLACE", "US").upper()
    pangolin_timeout = config_int(config, "PANGOLIN_TIMEOUT_SECONDS", 45)
    mcp_timeout = config_int(config, "MCP_TIMEOUT_SECONDS", 20)
    xingshang_timeout = config_int(config, "XINGSHANG_TIMEOUT_SECONDS", mcp_timeout)
    xingshang_force_refresh = config_bool(config, "XINGSHANG_FORCE_REFRESH", False)
    parents = [asin.strip().upper() for asin in config["MONITOR_PARENT_ASINS"].split(",") if asin.strip()]
    snapshot = {"schema_version": "1.0", "captured_at": now_iso(), "parents": {}, "children": {}, "errors": [], "warnings": []}
    for parent_asin in parents:
        parent_source = "pangolin"
        parent_pangolin_empty = False
        parent_variation_asins: List[str] = []
        parent_variation_source = ""
        try:
            parent_rows = extract_results(
                pangolin_scrape(
                    config["PANGOLINFO_API_TOKEN"],
                    "amzProductDetail",
                    parent_asin,
                    site=site,
                    zipcode=config.get("PANGOLIN_ZIPCODE", "10041"),
                    timeout=pangolin_timeout,
                )
            )
            parent_detail = parent_rows[0] if parent_rows else {}
            parent_pangolin_empty = not bool(parent_rows)
        except Exception as exc:
            parent_detail = {}
            snapshot["errors"].append(f"{parent_asin}: pangolin parent failed: {exc}")
        if not parent_detail:
            parent_detail, parent_source = fetch_fallback_detail(config, parent_asin, marketplace, snapshot["errors"], "parent")
            if parent_detail and parent_pangolin_empty:
                snapshot["warnings"].append(f"{parent_asin}: pangolin parent empty; using {parent_source}")
        elif parent_source == "pangolin":
            needs_supplement = parent_needs_supplement(parent_asin, parent_detail)
            has_front_fallback = any(config.get(key) for key in ("SELLERSPRITE_MCP_URL", "SORFTIME_MCP_URL", "SIF_MCP_URL"))
            if needs_supplement or has_front_fallback:
                fallback_errors = snapshot["errors"] if needs_supplement else []
                fallback_detail, fallback_source = fetch_fallback_detail(config, parent_asin, marketplace, fallback_errors, "parent")
                if fallback_detail:
                    fallback_child_asins = extract_child_asins(fallback_detail)
                    pangolin_child_asins = extract_child_asins(parent_detail)
                    if fallback_child_asins and set(fallback_child_asins) != set(pangolin_child_asins):
                        parent_variation_asins = fallback_child_asins
                        parent_variation_source = fallback_source
                    if needs_supplement:
                        parent_detail = merge_missing_detail(parent_detail, fallback_detail)
                        snapshot["warnings"].append(f"{parent_asin}: pangolin parent partial; supplemented by {fallback_source}")
                    elif parent_variation_asins:
                        snapshot["warnings"].append(f"{parent_asin}: pangolin parent variations supplemented by {fallback_source}")
        if not parent_detail:
            parent_source = "xingshang_inventory_only"
            snapshot["errors"].append(f"{parent_asin}: 前台数据缺失")
        parent = normalize_parent(parent_asin, {**parent_detail, "asin": parent_asin}, parent_source)
        if parent_variation_asins:
            parent["child_asins"] = parent_variation_asins
            parent["variation_source"] = parent_variation_source
        try:
            inventory_payload = fetch_inventory(
                parent_asin,
                config.get("XINGSHANG_MCP_URL_TEMPLATE", ""),
                timeout=xingshang_timeout,
                force_refresh=xingshang_force_refresh,
            )
            if inventory_payload:
                parent["inventory_source"] = "xingshang"
        except Exception as exc:
            error = f"{parent_asin}: xingshang failed: {describe_exception(exc, timeout=xingshang_timeout)}"
            inventory_payload = previous_inventory_payload(previous, parent_asin)
            if inventory_payload:
                parent["inventory_source"] = "previous_snapshot"
                snapshot["warnings"].append(f"{parent_asin}: xingshang failed; using previous inventory snapshot")
            snapshot["errors"].append(error)
        inventories = inventory_by_asin(inventory_payload)
        source_child_asins = set(parent["child_asins"])
        candidate_asins = sorted(source_child_asins | set(inventories))
        child_asins: List[str] = []
        inventory_only_asins: List[str] = []
        front_unavailable_asins: List[str] = []
        child_rows = []
        for child_asin in candidate_asins:
            child_source = "pangolin"
            detail = {}
            pangolin_failed = False
            try:
                rows = extract_results(
                    pangolin_scrape(
                        config["PANGOLINFO_API_TOKEN"],
                        "amzProductDetail",
                        child_asin,
                        site=site,
                        zipcode=config.get("PANGOLIN_ZIPCODE", "10041"),
                        timeout=pangolin_timeout,
                    )
                )
                detail = rows[0] if rows else {}
            except Exception as exc:
                detail = {}
                pangolin_failed = True
                snapshot["errors"].append(f"{child_asin}: pangolin child failed: {describe_exception(exc, timeout=pangolin_timeout)}")
            detail_asin = first_text(detail.get("asin")) if detail else None
            detail_matches_child = not detail_asin or (is_asin(detail_asin) and detail_asin.upper() == child_asin)
            detail_valid = front_detail_is_valid(detail, child_asin) if detail else False
            if detail and child_source == "pangolin" and detail_matches_child and (child_asin in source_child_asins or detail_valid) and child_needs_supplement(child_asin, detail):
                fallback_detail, fallback_source = fetch_fallback_detail(config, child_asin, marketplace, snapshot["errors"], "child")
                if fallback_detail:
                    detail = merge_missing_detail(detail, fallback_detail)
                    child_source = f"pangolin+{fallback_source}"
            if detail and front_detail_is_valid(detail, child_asin):
                child_asins.append(child_asin)
                child_rows.append(normalize_child(child_asin, detail, inventories.get(child_asin), child_source))
            else:
                inventory_only_asins.append(child_asin)
                front_unavailable_asins.append(child_asin)
                child_rows.append(
                    {
                        "asin": child_asin,
                        "inventory": inventories.get(child_asin),
                        "front_status": "不可售/404",
                        "source": "xingshang_inventory_only",
                    }
                )
        children = merge_child_asins(parent, child_rows, inventory_payload)
        parent["child_asins"] = sorted(set(child_asins))
        parent["inventory_only_asins"] = sorted(set(inventory_only_asins))
        parent["front_unavailable_asins"] = sorted(set(front_unavailable_asins))
        snapshot["parents"][parent_asin] = parent
        snapshot["children"].update(children)
    return snapshot


def diff_snapshots(previous: Optional[Mapping[str, Any]], current: Mapping[str, Any]) -> List[str]:
    if not previous:
        return []
    changes: List[str] = []
    prev_parents = previous.get("parents", {}) if isinstance(previous.get("parents"), Mapping) else {}
    cur_parents = current.get("parents", {}) if isinstance(current.get("parents"), Mapping) else {}
    prev_children = previous.get("children", {}) if isinstance(previous.get("children"), Mapping) else {}
    cur_children = current.get("children", {}) if isinstance(current.get("children"), Mapping) else {}
    for asin in sorted(set(prev_parents) | set(cur_parents)):
        prev = prev_parents.get(asin, {})
        cur = cur_parents.get(asin, {})
        for field in PARENT_FIELDS:
            if prev.get(field) != cur.get(field):
                changes.append(f"{asin} parent {field}: {prev.get(field)} -> {cur.get(field)}")
        prev_live_children = set(prev.get("child_asins") or [])
        cur_live_children = set(cur.get("child_asins") or [])
        for child_asin in sorted(cur_live_children - prev_live_children):
            changes.append(f"{asin} child added: {child_asin}")
        for child_asin in sorted(prev_live_children - cur_live_children):
            changes.append(f"{asin} child removed: {child_asin}")
        prev_inventory_only = set(prev.get("inventory_only_asins") or [])
        cur_inventory_only = set(cur.get("inventory_only_asins") or [])
        for child_asin in sorted(cur_inventory_only - prev_inventory_only):
            if parse_int((cur_children.get(child_asin) or {}).get("inventory")):
                changes.append(f"{asin} inventory-only child added: {child_asin}")
        for child_asin in sorted(prev_inventory_only - cur_inventory_only):
            if parse_int((prev_children.get(child_asin) or {}).get("inventory")):
                changes.append(f"{asin} inventory-only child removed: {child_asin}")
    for asin in sorted(set(prev_children) & set(cur_children)):
        prev = prev_children.get(asin, {})
        cur = cur_children.get(asin, {})
        for field in CHILD_FIELDS:
            if prev.get(field) != cur.get(field):
                changes.append(f"{asin} child {field}: {prev.get(field)} -> {cur.get(field)}")
    for error in current.get("errors") or []:
        changes.append(str(error))
    return changes


def normalize_fernet_key(key: str) -> bytes:
    key = key.strip()
    try:
        raw = base64.urlsafe_b64decode(key)
        if len(raw) == 32:
            return base64.urlsafe_b64encode(raw)
    except Exception:
        pass
    return base64.urlsafe_b64encode(hashlib.sha256(key.encode("utf-8")).digest())


def encrypt_snapshot(snapshot: Mapping[str, Any], key: str) -> str:
    token = Fernet(normalize_fernet_key(key)).encrypt(_json_body(snapshot)).decode("ascii")
    return json.dumps({"schema_version": "1.0", "created_at": now_iso(), "data": token}, ensure_ascii=False, indent=2)


def decrypt_snapshot(text: str, key: str) -> Dict[str, Any]:
    envelope = json.loads(text)
    data = envelope.get("data")
    if not isinstance(data, str):
        raise MonitorError("encrypted snapshot missing data")
    return json.loads(Fernet(normalize_fernet_key(key)).decrypt(data.encode("ascii")).decode("utf-8"))


def load_previous(path: str, key: str) -> Optional[Dict[str, Any]]:
    if not path or not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as handle:
        return decrypt_snapshot(handle.read(), key)


def save_current(path: str, snapshot: Mapping[str, Any], key: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(encrypt_snapshot(snapshot, key))


def feishu_payload(text: str, *, timestamp: Optional[int] = None, secret: str = "") -> Dict[str, Any]:
    payload = {"msg_type": "text", "content": {"text": text}}
    if secret:
        ts = str(timestamp or int(time.time()))
        sign = hmac.new(f"{ts}\n{secret}".encode("utf-8"), b"", hashlib.sha256).digest()
        payload.update({"timestamp": ts, "sign": base64.b64encode(sign).decode("ascii")})
    return payload


def send_feishu(text: str, webhook_url: str, secret: str = "") -> None:
    if not webhook_url:
        return
    http_json(
        "POST",
        webhook_url,
        feishu_payload(text, secret=secret),
        {"Content-Type": "application/json"},
        timeout=20,
    )


FIELD_LABELS = {
    "major_rank": "大类排名",
    "minor_rank": "小类排名",
    "stars": "评分",
    "rating_count": "评论数",
    "price": "价格",
    "coupon": "Coupon",
    "promotion": "促销/Deal",
    "frequently_returned": "高退货率标签",
    "inventory": "库存",
    "fulfillment_method": "配送方式",
    "delivery_promise": "配送时效",
}


def format_change_value(value: Any) -> str:
    if value in {None, "None", ""}:
        return "未知"
    if value == "False":
        return "否"
    if value == "True":
        return "是"
    return str(value)


def format_message(changes: Sequence[str], *, baseline: bool = False, captured_at: Optional[str] = None) -> str:
    if baseline:
        return "ASIN monitor baseline established. 后续每日 09:00 有变化时提醒。"
    parents: Dict[str, List[str]] = {}
    children: Dict[str, List[str]] = {}
    membership: List[str] = []
    inventory_only: List[str] = []
    errors: List[str] = []
    for raw in changes:
        line = str(raw)
        match = re.fullmatch(r"([A-Z0-9]{10}) parent ([a-z_]+): (.*) -> (.*)", line)
        if match:
            asin, field, before, after = match.groups()
            parents.setdefault(asin, []).append(f"- {FIELD_LABELS.get(field, field)}：{format_change_value(before)} → {format_change_value(after)}")
            continue
        match = re.fullmatch(r"([A-Z0-9]{10}) child ([a-z_]+): (.*) -> (.*)", line)
        if match:
            asin, field, before, after = match.groups()
            children.setdefault(asin, []).append(f"{FIELD_LABELS.get(field, field)}：{format_change_value(before)} → {format_change_value(after)}")
            continue
        match = re.fullmatch(r"([A-Z0-9]{10}) child (added|removed): ([A-Z0-9]{10})", line)
        if match:
            parent_asin, action, child_asin = match.groups()
            label = "新增" if action == "added" else "解绑"
            membership.append(f"- {label}：{parent_asin} / {child_asin}")
            continue
        match = re.fullmatch(r"([A-Z0-9]{10}) inventory-only child (added|removed): ([A-Z0-9]{10})", line)
        if match:
            parent_asin, action, child_asin = match.groups()
            label = "新增库存侧异常" if action == "added" else "移除库存侧异常"
            inventory_only.append(f"- {label}：{parent_asin} / {child_asin}")
            continue
        errors.append(line)
    lines = [f"ASIN 变化提醒｜{format_report_time(captured_at or now_iso())}", f"状态：发现 {len(changes)} 项变化"]
    if parents:
        for asin in sorted(parents):
            lines.extend(["", f"父 ASIN {asin}", "", "父体变化："])
            lines.extend(parents[asin])
    if children:
        lines.extend(["", "子体变化："])
        for index, asin in enumerate(sorted(children), 1):
            lines.append(f"{index}. {asin}")
            lines.extend(f"   {item}" for item in children[asin])
    if membership:
        lines.extend(["", "子体关系："])
        lines.extend(membership)
    if inventory_only:
        lines.extend(["", "库存侧异常："])
        lines.extend(inventory_only)
    if errors:
        lines.extend(["", "数据源异常："])
        lines.extend(f"- {line}" for line in errors[:20])
    return "\n".join(lines)


def format_rank(rank: Any, category: Any) -> str:
    if rank is None:
        return "未知"
    label = first_text(category)
    return f"{rank} ({label})" if label else str(rank)


def format_value(value: Any, *, empty: str = "无", unknown: str = "未知") -> str:
    if value is None:
        return unknown
    if value == "":
        return empty
    if isinstance(value, bool):
        return "是" if value else "否"
    return str(value)


def has_front_detail(row: Mapping[str, Any]) -> bool:
    return row.get("source") not in {"", None, "xingshang_inventory_only"}


def format_source(value: Any) -> str:
    text = first_text(value)
    if text == "SELLERSPRITE_MCP_URL":
        return "SellerSprite"
    if text == "pangolin":
        return "Pangolinfo"
    if text == "xingshang_inventory_only":
        return "xingshang 库存"
    if text == "previous_snapshot":
        return "上次快照"
    return text or "未知"


def report_status(snapshot: Mapping[str, Any]) -> str:
    parents = snapshot.get("parents", {}) if isinstance(snapshot.get("parents"), Mapping) else {}
    children = snapshot.get("children", {}) if isinstance(snapshot.get("children"), Mapping) else {}
    errors = [str(error) for error in snapshot.get("errors") or []]
    if errors:
        if all("xingshang failed" in error for error in errors) and any(isinstance(parent, Mapping) and parent.get("inventory_source") == "previous_snapshot" for parent in parents.values()):
            return "部分数据：库存沿用上次快照"
        return "部分数据：数据源异常"
    live_children = []
    for parent in parents.values():
        if isinstance(parent, Mapping):
            live_children.extend(children.get(str(asin), {}) for asin in parent.get("child_asins") or [])
    if any(not has_front_detail(row) for row in list(parents.values()) + live_children):
        return "部分数据：前台数据缺失"
    if snapshot.get("warnings"):
        return "完整数据（SellerSprite 补源）"
    return "完整数据"


def format_optional_text(value: Any, row: Mapping[str, Any]) -> str:
    if value is None:
        return "未知"
    if value == "" and has_front_detail(row):
        return "无"
    return str(value)


def format_coverage(value: Any, row: Optional[Mapping[str, Any]] = None) -> str:
    if value is None:
        return "未覆盖"
    if value == "":
        return "无" if row is None or has_front_detail(row) else "未覆盖"
    if isinstance(value, bool):
        return "是" if value else "否"
    return str(value)


def format_source_summary(snapshot: Mapping[str, Any], parent: Mapping[str, Any]) -> str:
    front = format_source(parent.get("source"))
    inventory = format_source(parent.get("inventory_source")) if parent.get("inventory_source") else "未覆盖"
    parts = [f"前台 {front}", f"库存 {inventory}"]
    if snapshot.get("warnings"):
        pangolin_warnings = [str(warning).lower() for warning in snapshot.get("warnings") or [] if "pangolin" in str(warning).lower()]
        if any("empty" in warning for warning in pangolin_warnings):
            parts.append("Pangolinfo 空结果已补源")
        elif pangolin_warnings:
            parts.append("Pangolinfo 部分字段已补源")
        if parent.get("inventory_source") == "previous_snapshot":
            parts.append("xingshang 异常，库存沿用上次快照")
    return "；".join(parts)


def format_parent_snapshot_report(snapshot: Mapping[str, Any], parent_asin: str, parent: Mapping[str, Any], children: Mapping[str, Any]) -> str:
    child_asins = [str(asin) for asin in parent.get("child_asins") or []]
    inventory_only_asins = [str(asin) for asin in parent.get("inventory_only_asins") or []]
    lines = [f"ASIN 今日数据｜{format_report_time(snapshot.get('captured_at') or now_iso())}", f"父 ASIN {parent_asin}"]
    if not has_front_detail(parent):
        lines.append("- 前台数据缺失")
    lines.append(f"- 大类排名: {format_rank(parent.get('major_rank'), parent.get('major_category'))}")
    lines.append(f"- 小类排名: {format_rank(parent.get('minor_rank'), parent.get('minor_category'))}")
    lines.append(f"- 评分：{format_value(parent.get('stars'))}｜评论：{format_value(parent.get('rating_count'))}｜子体：{len(child_asins)}｜异常：{len(inventory_only_asins)}")
    lines.append("")
    lines.append("子体明细：")
    for index, child_asin in enumerate(sorted(child_asins), 1):
        child = children.get(child_asin, {})
        lines.append(
            f"{index}. {child_asin}｜"
            f"价 {format_value(child.get('price'))}｜"
            f"库存 {format_value(child.get('inventory'))}｜"
            f"Coupon {format_optional_text(child.get('coupon'), child)}｜"
            f"促销 {format_optional_text(child.get('promotion'), child)}｜"
            f"配送 {format_coverage(child.get('fulfillment_method'), child)}｜"
            f"退货 {format_coverage(child.get('frequently_returned'), child)}｜"
            f"时效 {format_coverage(child.get('delivery_promise'), child)}"
        )
    if inventory_only_asins:
        lines.append("")
        lines.append("库存侧异常：")
        for child_asin in sorted(inventory_only_asins):
            child = children.get(child_asin, {})
            lines.append(
                f"- {child_asin}｜"
                f"库存 {format_value(child.get('inventory'))}｜"
                f"前台状态 {format_value(child.get('front_status'))}｜"
                f"来源 xingshang"
            )
    lines.append("")
    lines.append(f"数据源：{format_source_summary(snapshot, parent)}")
    return "\n".join(lines)


def format_snapshot_report_messages(snapshot: Mapping[str, Any]) -> List[str]:
    captured_at = snapshot.get("captured_at") or now_iso()
    parents = snapshot.get("parents", {}) if isinstance(snapshot.get("parents"), Mapping) else {}
    children = snapshot.get("children", {}) if isinstance(snapshot.get("children"), Mapping) else {}
    errors = [str(error) for error in snapshot.get("errors") or []]
    total_children = sum(len(parent.get("child_asins") or []) for parent in parents.values() if isinstance(parent, Mapping))
    total_inventory_only = sum(len(parent.get("inventory_only_asins") or []) for parent in parents.values() if isinstance(parent, Mapping))
    overview = [
        f"ASIN 今日数据总览｜{format_report_time(captured_at)}",
        f"状态：{report_status(snapshot)}",
        f"父 ASIN：{len(parents)}｜正常子体：{total_children}｜库存侧异常：{total_inventory_only}｜数据源异常：{len(errors)}",
        "",
        "父体摘要：",
    ]
    for index, parent_asin in enumerate(sorted(parents), 1):
        parent = parents[parent_asin]
        if not isinstance(parent, Mapping):
            continue
        child_count = len(parent.get("child_asins") or [])
        inventory_only_count = len(parent.get("inventory_only_asins") or [])
        overview.append(
            f"{index}. {parent_asin}｜"
            f"排名 {format_value(parent.get('major_rank'))} / {format_value(parent.get('minor_rank'))}｜"
            f"评分 {format_value(parent.get('stars'))}｜"
            f"评论 {format_value(parent.get('rating_count'))}｜"
            f"子体 {child_count}｜异常 {inventory_only_count}"
        )
    messages = ["\n".join(overview)]
    for parent_asin in sorted(parents):
        parent = parents[parent_asin]
        if isinstance(parent, Mapping):
            messages.append(format_parent_snapshot_report(snapshot, parent_asin, parent, children))
    if errors:
        messages.append("数据源异常汇总:\n" + "\n".join(f"- {error}" for error in errors[:50]))
    return messages


def format_snapshot_report(snapshot: Mapping[str, Any]) -> str:
    return "\n\n---\n\n".join(format_snapshot_report_messages(snapshot))


def env_config() -> Dict[str, str]:
    required = ["PANGOLINFO_API_TOKEN", "FEISHU_WEBHOOK_URL", "MONITOR_PARENT_ASINS", "STATE_ENCRYPTION_KEY", "XINGSHANG_MCP_URL_TEMPLATE"]
    config = {key: os.environ.get(key, "") for key in required}
    missing = [key for key, value in config.items() if not value]
    if missing:
        raise MonitorError("missing required env vars: " + ", ".join(missing))
    for optional in (
        "FEISHU_WEBHOOK_SECRET",
        "SELLERSPRITE_MCP_URL",
        "SELLERSPRITE_MCP_SECRET_KEY",
        "SORFTIME_MCP_URL",
        "SIF_MCP_URL",
        "MARKETPLACE",
        "PANGOLIN_ZIPCODE",
        "PANGOLIN_TIMEOUT_SECONDS",
        "MCP_TIMEOUT_SECONDS",
        "XINGSHANG_TIMEOUT_SECONDS",
        "XINGSHANG_FORCE_REFRESH",
        "FORCE_CURRENT_REPORT",
    ):
        config[optional] = os.environ.get(optional, "")
    config["MARKETPLACE"] = config.get("MARKETPLACE") or "US"
    config["PANGOLIN_ZIPCODE"] = config.get("PANGOLIN_ZIPCODE") or "10041"
    return config


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", default="state/latest.enc.json")
    parser.add_argument("--output", default="state/latest.enc.json")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report-current", action="store_true")
    args = parser.parse_args(argv)

    config = env_config()
    previous = load_previous(args.state, config["STATE_ENCRYPTION_KEY"])
    current = collect_snapshot(config, previous=previous)
    changes = diff_snapshots(previous, current)
    messages: List[str]
    if args.report_current or config.get("FORCE_CURRENT_REPORT", "").lower() == "true":
        messages = format_snapshot_report_messages(current)
    elif previous is None:
        messages = [format_message(changes, baseline=True)]
    elif changes:
        messages = [format_message(changes)]
    else:
        messages = []
    if messages:
        if args.dry_run:
            print("\n\n---\n\n".join(messages))
        else:
            for message in messages:
                send_feishu(message, config["FEISHU_WEBHOOK_URL"], config.get("FEISHU_WEBHOOK_SECRET", ""))
                time.sleep(0.5)
    if not current.get("errors"):
        save_current(args.output, current, config["STATE_ENCRYPTION_KEY"])
    elif previous is None:
        save_current(args.output, current, config["STATE_ENCRYPTION_KEY"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
