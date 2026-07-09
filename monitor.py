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
from datetime import datetime, timezone
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


class MonitorError(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def first_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, Mapping):
        for key in ("text", "value", "name", "title", "label", "deliveryTime", "fastestDelivery"):
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


def config_int(config: Mapping[str, str], key: str, default: int) -> int:
    value = parse_int(config.get(key))
    return value if value is not None and value > 0 else default


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
        detail.get("variations"),
        detail.get("variationAsins"),
        detail.get("childAsins"),
        detail.get("children"),
    ]
    for value in candidates:
        for item in listify(value):
            asin = first_text(item.get("asin") if isinstance(item, Mapping) else item)
            if asin:
                found.append(asin.upper())
    return sorted(set(found))


def normalize_delivery(value: Any) -> Optional[str]:
    if isinstance(value, Mapping):
        delivery = first_text(value.get("deliveryTime") or value.get("delivery"))
        fastest = first_text(value.get("fastestDelivery"))
        if delivery and fastest:
            return f"{delivery}; fastest {fastest}"
        return delivery or fastest
    return first_text(value)


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


def coupon_from_trends(value: Any) -> Optional[str]:
    trends = [item for item in listify(value) if isinstance(item, Mapping)]
    if not trends:
        return None
    latest = trends[-1]
    coupon = parse_float(latest.get("couponPrice"))
    final = parse_float(latest.get("finalPrice"))
    if coupon is None:
        return None
    return f"coupon {coupon:g}; final {final:g}" if final is not None else f"coupon {coupon:g}"


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
    coupon = first_text(detail.get("coupon") or detail.get("couponInfo") or detail.get("couponText")) or coupon_from_trends(detail.get("couponTrends"))
    promotion = first_text(detail.get("promotion") or detail.get("deal") or detail.get("badge") or detail.get("badges"))
    return {
        "asin": child_asin.upper(),
        "price": parse_float(detail.get("price") or detail.get("finalPrice") or detail.get("price_display")),
        "coupon": coupon if coupon is not None else ("" if has_detail else None),
        "promotion": promotion if promotion is not None else ("" if has_detail else None),
        "frequently_returned": parse_bool(
            detail.get("frequentlyReturned")
            or detail.get("frequently_returned")
            or detail.get("frequently_return")
            or badge.get("frequentlyReturned")
        ),
        "inventory": inventory,
        "fulfillment_method": first_text(
            detail.get("fulfillment")
            or detail.get("fulfillmentMethod")
            or detail.get("fulfillment_method")
            or detail.get("seller")
        ),
        "delivery_promise": normalize_delivery(
            detail.get("deliveryTime")
            or detail.get("delivery")
            or detail.get("deliveryPromise")
            or detail.get("delivery_promise")
            or detail.get("availability")
        ),
        "source": source,
    }


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
    child_asins.update(inventories)
    merged = {str(row.get("asin")).upper(): dict(row) for row in children if row.get("asin")}
    for asin in sorted(child_asins):
        row = merged.setdefault(asin, {"asin": asin})
        if asin in inventories:
            row["inventory"] = inventories[asin]
    return dict(sorted(merged.items()))


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
        async with httpx.AsyncClient(headers=dict(headers or {})) as client:
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


def fetch_inventory(parent_asin: str, url_template: str, timeout: int = 30) -> Dict[str, Any]:
    if not url_template:
        return {}
    server_url = url_template.format(parent_asin=parent_asin.upper(), PARENT_ASIN=parent_asin.upper())
    return run_mcp(call_mcp_tool(server_url, ("get_store_asin_info",), {"spu_item_id_list": [parent_asin.upper()], "force_refresh": True}), timeout)


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


def collect_snapshot(config: Mapping[str, str]) -> Dict[str, Any]:
    site = SITE_BY_MARKETPLACE.get(config.get("MARKETPLACE", "US").upper(), "amz_us")
    marketplace = config.get("MARKETPLACE", "US").upper()
    pangolin_timeout = config_int(config, "PANGOLIN_TIMEOUT_SECONDS", 8)
    mcp_timeout = config_int(config, "MCP_TIMEOUT_SECONDS", 20)
    parents = [asin.strip().upper() for asin in config["MONITOR_PARENT_ASINS"].split(",") if asin.strip()]
    snapshot = {"schema_version": "1.0", "captured_at": now_iso(), "parents": {}, "children": {}, "errors": [], "warnings": []}
    for parent_asin in parents:
        parent_source = "pangolin"
        parent_pangolin_empty = False
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
        if not parent_detail:
            parent_source = "xingshang_inventory_only"
            snapshot["errors"].append(f"{parent_asin}: 前台数据缺失")
        parent = normalize_parent(parent_asin, {**parent_detail, "asin": parent_asin}, parent_source)
        try:
            inventory_payload = fetch_inventory(parent_asin, config.get("XINGSHANG_MCP_URL_TEMPLATE", ""), timeout=mcp_timeout)
        except Exception as exc:
            inventory_payload = {}
            snapshot["errors"].append(f"{parent_asin}: xingshang failed: {exc}")
        child_asins = sorted(set(parent["child_asins"]) | set(inventory_by_asin(inventory_payload)))
        child_rows = []
        for child_asin in child_asins:
            child_source = "pangolin"
            child_pangolin_empty = False
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
                child_pangolin_empty = not bool(rows)
            except Exception as exc:
                detail = {}
                snapshot["errors"].append(f"{child_asin}: pangolin child failed: {exc}")
            if not detail:
                detail, child_source = fetch_fallback_detail(config, child_asin, marketplace, snapshot["errors"], "child")
                if detail and child_pangolin_empty:
                    snapshot["warnings"].append(f"{child_asin}: pangolin child empty; using {child_source}")
            if not detail:
                child_source = "xingshang_inventory_only"
                snapshot["errors"].append(f"{child_asin}: 前台数据缺失")
            child_rows.append(normalize_child(child_asin, {**detail, "asin": child_asin}, inventory_by_asin(inventory_payload).get(child_asin), child_source))
        children = merge_child_asins(parent, child_rows, inventory_payload)
        parent["child_asins"] = sorted(children)
        snapshot["parents"][parent_asin] = parent
        snapshot["children"].update(children)
    return snapshot


def diff_snapshots(previous: Optional[Mapping[str, Any]], current: Mapping[str, Any]) -> List[str]:
    if not previous:
        return []
    changes: List[str] = []
    prev_parents = previous.get("parents", {}) if isinstance(previous.get("parents"), Mapping) else {}
    cur_parents = current.get("parents", {}) if isinstance(current.get("parents"), Mapping) else {}
    for asin in sorted(set(prev_parents) | set(cur_parents)):
        prev = prev_parents.get(asin, {})
        cur = cur_parents.get(asin, {})
        for field in PARENT_FIELDS:
            if prev.get(field) != cur.get(field):
                changes.append(f"{asin} parent {field}: {prev.get(field)} -> {cur.get(field)}")
        prev_children = set(prev.get("child_asins") or [])
        cur_children = set(cur.get("child_asins") or [])
        for child_asin in sorted(cur_children - prev_children):
            changes.append(f"{asin} child added: {child_asin}")
        for child_asin in sorted(prev_children - cur_children):
            changes.append(f"{asin} child removed: {child_asin}")
    prev_children = previous.get("children", {}) if isinstance(previous.get("children"), Mapping) else {}
    cur_children = current.get("children", {}) if isinstance(current.get("children"), Mapping) else {}
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


def format_message(changes: Sequence[str], *, baseline: bool = False) -> str:
    if baseline:
        return "ASIN monitor baseline established. 后续每日 09:00 有变化时提醒。"
    head = f"ASIN monitor detected {len(changes)} change(s):"
    return head + "\n" + "\n".join(f"- {line}" for line in changes[:80])


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
    return text or "未知"


def report_status(snapshot: Mapping[str, Any]) -> str:
    parents = snapshot.get("parents", {}) if isinstance(snapshot.get("parents"), Mapping) else {}
    children = snapshot.get("children", {}) if isinstance(snapshot.get("children"), Mapping) else {}
    if any(not has_front_detail(row) for row in list(parents.values()) + list(children.values())):
        return "部分数据：前台数据缺失"
    if snapshot.get("errors"):
        return "部分数据：数据源异常"
    return "完整数据"


def format_optional_text(value: Any, row: Mapping[str, Any]) -> str:
    if value is None:
        return "未知"
    if value == "" and has_front_detail(row):
        return "无"
    return str(value)


def format_snapshot_report(snapshot: Mapping[str, Any]) -> str:
    lines = [f"ASIN 今日数据 ({snapshot.get('captured_at') or now_iso()})", f"状态: {report_status(snapshot)}"]
    parents = snapshot.get("parents", {}) if isinstance(snapshot.get("parents"), Mapping) else {}
    children = snapshot.get("children", {}) if isinstance(snapshot.get("children"), Mapping) else {}
    for parent_asin in sorted(parents):
        parent = parents[parent_asin]
        child_asins = [str(asin) for asin in parent.get("child_asins") or []]
        lines.append("")
        lines.append(f"父 ASIN {parent_asin}")
        lines.append(f"- 前台数据源: {format_source(parent.get('source'))}")
        if not has_front_detail(parent):
            lines.append("- 前台数据缺失")
        lines.append(f"- 大类排名: {format_rank(parent.get('major_rank'), parent.get('major_category'))}")
        lines.append(f"- 小类排名: {format_rank(parent.get('minor_rank'), parent.get('minor_category'))}")
        lines.append(f"- 星级: {format_value(parent.get('stars'))}")
        lines.append(f"- 评论数: {format_value(parent.get('rating_count'))}")
        lines.append(f"- 子 ASIN 数: {len(child_asins)}")
        for child_asin in sorted(child_asins):
            child = children.get(child_asin, {})
            lines.append(
                "- 子 ASIN "
                f"{child_asin}: 价格: {format_value(child.get('price'))}; "
                f"coupon: {format_optional_text(child.get('coupon'), child)}; "
                f"促销: {format_optional_text(child.get('promotion'), child)}; "
                f"frequently return: {format_value(child.get('frequently_returned'))}; "
                f"库存: {format_value(child.get('inventory'))}; "
                f"配送方式: {format_value(child.get('fulfillment_method'))}; "
                f"配送时效: {format_value(child.get('delivery_promise'))}"
            )
    warnings = snapshot.get("warnings") or []
    if warnings:
        lines.append("")
        lines.append("数据源提示:")
        lines.extend(f"- {warning}" for warning in warnings[:20])
    errors = snapshot.get("errors") or []
    if errors:
        lines.append("")
        lines.append("数据源异常:")
        lines.extend(f"- {error}" for error in errors[:20])
    return "\n".join(lines)


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
    current = collect_snapshot(config)
    changes = diff_snapshots(previous, current)
    if args.report_current or config.get("FORCE_CURRENT_REPORT", "").lower() == "true":
        message = format_snapshot_report(current)
    elif previous is None:
        message = format_message(changes, baseline=True)
    elif changes:
        message = format_message(changes)
    else:
        message = ""
    if message:
        if args.dry_run:
            print(message)
        else:
            send_feishu(message, config["FEISHU_WEBHOOK_URL"], config.get("FEISHU_WEBHOOK_SECRET", ""))
    if not current.get("errors"):
        save_current(args.output, current, config["STATE_ENCRYPTION_KEY"])
    elif previous is None:
        save_current(args.output, current, config["STATE_ENCRYPTION_KEY"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
