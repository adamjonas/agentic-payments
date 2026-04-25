"""Agent tool definitions — each tool maps to a Claude tool_use schema and an executor."""

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from backend.budget import get_remaining_budget
from backend.db import get_transactions
from backend.l402 import (
    DuplicatePayment,
    L402Error,
    UnsafeURL,
    VendorBlocked,
    fetch_with_l402,
    get_wallet_balance,
)

# ---------------------------------------------------------------------------
# Tool schemas (sent to Claude as tools)
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "web_search",
        "description": (
            "Search the web for information. Returns a list of search result "
            "snippets with titles and URLs. Use this to research topics, find "
            "L402-enabled APIs, or gather information for the user."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "fetch_url",
        "description": (
            "Fetch the contents of a URL. If the server responds with HTTP 402 "
            "(Payment Required) and an L402 challenge, the system will "
            "automatically pay the Lightning invoice and retry with the "
            "authorization token — so this tool transparently handles paid "
            "resources. Returns the response body."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to fetch.",
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "check_balance",
        "description": (
            "Check the agent's remaining daily budget in satoshis and recent "
            "transaction history."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "shop_search",
        "description": (
            "Search for products on Amazon or Walmart via the unhuman.shopping "
            "API. Returns product listings with prices, images, and availability. "
            "This is a free call — no payment required."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query or product ID (e.g. 'wireless earbuds', 'B0CX23V2ZK').",
                },
                "retailer": {
                    "type": "string",
                    "enum": ["amazon", "walmart"],
                    "description": "Which retailer to search. Defaults to amazon.",
                },
                "zip": {
                    "type": "string",
                    "description": "US ZIP code for shipping estimates (e.g. '90210').",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "shop_quote",
        "description": (
            "Get an exact price quote for a specific product, including shipping "
            "and service fees. Returns a quote_token valid for 30 minutes that is "
            "required to place an order. This is a free call — no payment required."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "product_id": {
                    "type": "string",
                    "description": "The product ASIN (Amazon) or product ID (Walmart) from search results.",
                },
                "retailer": {
                    "type": "string",
                    "enum": ["amazon", "walmart"],
                    "description": "Which retailer. Defaults to amazon.",
                },
                "zip": {
                    "type": "string",
                    "description": "US ZIP code for exact shipping calculation.",
                },
            },
            "required": ["product_id"],
        },
    },
    {
        "name": "shop_order",
        "description": (
            "Place an order for a product. Requires a quote_token from shop_quote "
            "(valid 30 minutes) and a complete US shipping address. This will pay "
            "via Lightning (L402) — the cost is the quoted total + 1%% service fee. "
            "IMPORTANT: Always confirm the price and shipping address with the user "
            "before calling this tool."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "retailer": {
                    "type": "string",
                    "enum": ["amazon", "walmart"],
                    "description": "Which retailer.",
                },
                "product_id": {
                    "type": "string",
                    "description": "The product ID from the quote.",
                },
                "quote_token": {
                    "type": "string",
                    "description": "The quote_token from shop_quote (valid 30 min).",
                },
                "first_name": {"type": "string"},
                "last_name": {"type": "string"},
                "address_line1": {"type": "string"},
                "address_line2": {"type": "string", "description": "Optional."},
                "city": {"type": "string"},
                "state": {"type": "string", "description": "Two-letter state code."},
                "postal_code": {"type": "string"},
                "phone_number": {
                    "type": "string",
                    "description": "Phone in E.164 format (e.g. +12125551234).",
                },
            },
            "required": [
                "retailer", "product_id", "quote_token",
                "first_name", "last_name", "address_line1",
                "city", "state", "postal_code", "phone_number",
            ],
        },
    },
]


# ---------------------------------------------------------------------------
# Step narration — plain-English descriptions for SSE events (TODOS.md)
# ---------------------------------------------------------------------------


def narrate_tool_call(name: str, args: dict[str, Any]) -> str:
    """Return a plain-English description of a tool call for the SSE stream."""
    match name:
        case "web_search":
            return f"Searching the web for \"{args.get('query', '')}\""
        case "fetch_url":
            url = args.get("url", "")
            domain = urlparse(url).netloc or url
            return f"Fetching resource from {domain}"
        case "check_balance":
            return "Checking wallet balance"
        case "shop_search":
            q = args.get("query", "")
            r = args.get("retailer", "amazon")
            return f"Searching {r} for \"{q}\""
        case "shop_quote":
            pid = args.get("product_id", "")
            return f"Getting price quote for {pid}"
        case "shop_order":
            return "Placing order via Lightning payment"
        case _:
            return f"Running {name}"


def narrate_tool_result(name: str, result: dict[str, Any]) -> str | None:
    """Return an optional follow-up narration after a tool completes."""
    if name == "fetch_url":
        if result.get("paid"):
            amt = result.get("amount_sats", 0)
            vendor = result.get("vendor", "unknown")
            return f"Paid {amt} sats to {vendor} via Lightning"
        if result.get("blocked"):
            vendor = result.get("vendor", "unknown")
            return f"Vendor {vendor} is on the blocklist — skipping"
    if name == "shop_search":
        count = len(result.get("products", []))
        return f"Found {count} product{'s' if count != 1 else ''}"
    if name == "shop_quote":
        total = result.get("checkout_total_cents")
        if total:
            return f"Quoted total: ${total / 100:.2f}"
    if name == "shop_order":
        if result.get("order_id"):
            return f"Order placed! ID: {result['order_id']}"
    return None


# ---------------------------------------------------------------------------
# Tool executors
# ---------------------------------------------------------------------------


@dataclass
class ToolResult:
    output: dict[str, Any]
    narration: str | None = None  # extra narration after execution


async def execute_web_search(query: str) -> ToolResult:
    """Execute a web search using a simple search API.

    This is a placeholder — swap in your preferred search provider
    (Brave, Serper, Tavily, etc.).
    """
    # Placeholder: use httpx to hit a search API
    # For now, return a stub that can be replaced with a real provider
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            # Using DuckDuckGo instant answer API as a zero-config fallback
            resp = await client.get(
                "https://api.duckduckgo.com/",
                params={"q": query, "format": "json", "no_html": "1"},
            )
            data = resp.json()

            results = []
            if data.get("AbstractText"):
                results.append(
                    {
                        "title": data.get("Heading", ""),
                        "snippet": data["AbstractText"],
                        "url": data.get("AbstractURL", ""),
                    }
                )
            for topic in data.get("RelatedTopics", [])[:5]:
                if "Text" in topic:
                    results.append(
                        {
                            "title": topic.get("FirstURL", "").split("/")[-1],
                            "snippet": topic["Text"],
                            "url": topic.get("FirstURL", ""),
                        }
                    )

            if not results:
                return ToolResult(
                    output={
                        "results": [],
                        "note": "No instant results — try a more specific query",
                    }
                )
            return ToolResult(output={"results": results})
    except Exception as e:
        return ToolResult(output={"error": str(e)})


async def execute_fetch_url(url: str) -> ToolResult:
    """Fetch a URL with automatic L402 payment handling."""
    try:
        result = await fetch_with_l402(url)
        output: dict[str, Any] = {
            "status_code": result.status_code,
            "body": result.body[:10_000],  # truncate large responses
            "vendor": result.vendor,
            "paid": result.amount_sats > 0,
            "amount_sats": result.amount_sats,
        }
        narration = None
        if result.amount_sats > 0:
            narration = f"Paid {result.amount_sats} sats to {result.vendor} via Lightning"
        return ToolResult(output=output, narration=narration)
    except UnsafeURL as e:
        return ToolResult(
            output={"error": str(e), "blocked": True},
            narration=str(e),
        )
    except VendorBlocked as e:
        return ToolResult(
            output={"error": str(e), "blocked": True, "vendor": str(e).split()[1]},
            narration=str(e),
        )
    except DuplicatePayment as e:
        return ToolResult(output={"error": str(e), "duplicate": True})
    except L402Error as e:
        return ToolResult(output={"error": str(e)})
    except httpx.HTTPError as e:
        return ToolResult(output={"error": f"HTTP error: {e}"})


async def execute_check_balance() -> ToolResult:
    """Check remaining budget, wallet balance, and recent transactions."""
    remaining_budget = await get_remaining_budget()
    txns = await get_transactions(limit=10)

    # Also fetch the actual wallet balance from MDK
    try:
        wallet_balance = await get_wallet_balance()
    except L402Error:
        wallet_balance = None

    output: dict[str, Any] = {
        "remaining_daily_budget_sats": remaining_budget,
        "recent_transactions": txns,
    }
    if wallet_balance is not None:
        output["wallet_balance_sats"] = wallet_balance

    return ToolResult(output=output)


SHOP_BASE = "https://unhuman.shopping"


async def execute_shop_search(
    query: str, retailer: str = "amazon", zip_code: str | None = None
) -> ToolResult:
    """Search products on unhuman.shopping (free, no L402)."""
    try:
        params: dict[str, str] = {"q": query, "retailer": retailer}
        if zip_code:
            params["zip"] = zip_code

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{SHOP_BASE}/api/catalog", params=params)
            data = resp.json()

        products = data.get("products", [])
        # Trim to essential fields for the agent context
        trimmed = []
        for p in products[:10]:
            trimmed.append({
                "product_id": p.get("product_id") or p.get("asin"),
                "title": p.get("title"),
                "price_cents": p.get("retailer_price_cents"),
                "shipping_cents": p.get("shipping_estimate_cents"),
                "total_cents": p.get("estimated_checkout_total_cents"),
                "is_prime": p.get("is_prime"),
                "rating": p.get("rating"),
                "thumbnail": p.get("thumbnail"),
                "url": p.get("product_url") or p.get("url"),
            })
        return ToolResult(output={"products": trimmed, "count": len(products)})
    except Exception as e:
        return ToolResult(output={"error": str(e)})


async def execute_shop_quote(
    product_id: str, retailer: str = "amazon", zip_code: str | None = None
) -> ToolResult:
    """Get exact pricing for a product (free, no L402)."""
    try:
        params: dict[str, str] = {}
        if retailer != "amazon":
            params["retailer"] = retailer
        if zip_code:
            params["zip"] = zip_code

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{SHOP_BASE}/api/catalog/{product_id}", params=params
            )
            data = resp.json()

        product = data.get("product", data)
        return ToolResult(output={
            "product_id": product.get("product_id") or product.get("asin"),
            "title": product.get("title"),
            "price_cents": product.get("retailer_price_cents"),
            "shipping_cents": product.get("shipping_estimate_cents"),
            "service_fee_cents": product.get("service_fee_cents"),
            "checkout_total_cents": product.get("checkout_total_cents"),
            "quote_token": product.get("quote_token"),
            "quote_expires_at": product.get("quote_expires_at"),
            "thumbnail": product.get("thumbnail"),
            "url": product.get("product_url") or product.get("url"),
        })
    except Exception as e:
        return ToolResult(output={"error": str(e)})


async def execute_shop_order(args: dict[str, Any]) -> ToolResult:
    """Place an order via L402 payment on unhuman.shopping."""
    order_body = {
        "retailer": args["retailer"],
        "products": [{"product_id": args["product_id"], "quantity": 1}],
        "shipping_address": {
            "first_name": args["first_name"],
            "last_name": args["last_name"],
            "address_line1": args["address_line1"],
            "city": args["city"],
            "state": args["state"],
            "postal_code": args["postal_code"],
            "country": "US",
            "phone_number": args["phone_number"],
        },
        "quote_token": args["quote_token"],
    }
    if args.get("address_line2"):
        order_body["shipping_address"]["address_line2"] = args["address_line2"]

    try:
        result = await fetch_with_l402(
            f"{SHOP_BASE}/api/order", method="POST", json_body=order_body
        )
        import json
        try:
            body = json.loads(result.body)
        except Exception:
            body = {"raw": result.body[:2000]}

        output = {
            "order_id": body.get("order_id") or body.get("request_id"),
            "status": body.get("status"),
            "amount_sats": result.amount_sats,
            **{k: v for k, v in body.items() if k not in ("order_id", "request_id", "status")},
        }
        narration = None
        if output.get("order_id"):
            narration = f"Order placed! ID: {output['order_id']}"
        return ToolResult(output=output, narration=narration)
    except L402Error as e:
        return ToolResult(output={"error": str(e)})
    except httpx.HTTPError as e:
        return ToolResult(output={"error": f"HTTP error: {e}"})


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

EXECUTORS = {
    "web_search": lambda args: execute_web_search(args["query"]),
    "fetch_url": lambda args: execute_fetch_url(args["url"]),
    "check_balance": lambda args: execute_check_balance(),
    "shop_search": lambda args: execute_shop_search(
        args["query"], args.get("retailer", "amazon"), args.get("zip")
    ),
    "shop_quote": lambda args: execute_shop_quote(
        args["product_id"], args.get("retailer", "amazon"), args.get("zip")
    ),
    "shop_order": lambda args: execute_shop_order(args),
}


async def execute_tool(name: str, args: dict[str, Any]) -> ToolResult:
    executor = EXECUTORS.get(name)
    if executor is None:
        return ToolResult(output={"error": f"Unknown tool: {name}"})
    return await executor(args)
