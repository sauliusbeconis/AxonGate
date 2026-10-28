"""Hosted MCP endpoint (streamable HTTP, stateless JSON) with per-call x402 payment.

Each paid tool follows the x402 MCP transport: an unpaid tools/call returns an error result whose
structuredContent is the PaymentRequired object; the client retries with the signed payment in
params._meta["x402/payment"] and gets the settlement back in result._meta["x402/payment-response"].
Tools run the same handlers as the HTTP routes, so a failed call is never settled.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from . import config, payments
from .config import PRODUCTS
from .store import store

try:
    from x402.extensions.bazaar import DeclareMcpDiscoveryConfig, OutputConfig, declare_mcp_discovery_extension
    from x402.mcp.server_async import PaymentWrapperConfig, create_payment_wrapper
    from x402.mcp.types import PaymentWrapperHooks, ResourceInfo
    from x402.schemas import ResourceConfig
except ImportError:  # pragma: no cover - x402 is a hard requirement in production
    create_payment_wrapper = None

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")

TOOLS: dict[str, tuple[str, str]] = {
    "read": ("axongate_read", "Read a public web page as clean Markdown with a prompt-injection risk scan."),
    "batch": ("axongate_read_batch", "Read up to 20 public web pages in one call, each as clean Markdown with its own prompt-injection scan."),
    "search": ("axongate_search", "Search the live web; optionally read the top results."),
    "answer": ("axongate_answer", "Answer a question from live web sources with verbatim-checked citations."),
    "verify": ("axongate_verify", "Fact-check a claim: verdict, confidence and verbatim-checked evidence."),
    "scan": ("axongate_scan", "Scan untrusted text for prompt injection before passing it to a model."),
    "token": ("axongate_token_check", "Check a crypto token before buying, swapping or accepting it: honeypot, taxes, owner powers, holder concentration and liquidity, as an avoid / caution / ok verdict."),
}
TOOL_PRODUCT = {name: key for key, (name, _) in TOOLS.items()}

INSTRUCTIONS = (
    "AxonGate gives agents safe web access and token checks. Every tool is paid per call in USDC via x402 "
    f"({config.NETWORK_LABELS}); an unpaid call returns the x402 PaymentRequired object. Clients without x402 "
    "support can run the local server instead: npx axongate-mcp (pays from your own wallet, with a spend cap)."
)

_wrappers: dict[str, Any] = {}
_init_lock = asyncio.Lock()


def endpoint() -> str:
    return f"{config.PUBLIC_BASE_URL}/mcp"


def tool_list() -> list[dict[str, Any]]:
    tools = []
    for key, (name, description) in TOOLS.items():
        product = PRODUCTS[key]
        tools.append({
            "name": name,
            "title": f"AxonGate {product.title}",
            "description": f"{description} Costs ${product.price} USDC per call, paid via x402.",
            "inputSchema": payments._input_schema(key),
            "annotations": {"readOnlyHint": True, "openWorldHint": True},
            "_meta": {"x402/price_usdc": str(product.price)},
        })
    return tools


def server_card() -> dict[str, Any]:
    return {
        "serverInfo": {"name": "axongate", "title": "AxonGate", "version": config.VERSION},
        "description": "Safe web read, search, cited answers, fact-checks and token checks for agents, paid per call via x402.",
        "iconUrl": f"{config.PUBLIC_BASE_URL}/icon.png",
        "documentationUrl": f"{config.PUBLIC_BASE_URL}/docs",
        "transport": {"type": "streamable-http", "url": endpoint()},
        "authentication": {"required": False, "schemes": []},
        "payment": {"protocol": "x402", "transport": "mcp", "networks": [r.network for r in payments.rails()]},
        "capabilities": {"tools": {"listChanged": False}},
        "instructions": INSTRUCTIONS,
        "tools": tool_list(),
    }


async def _wrapper(key: str):
    if key in _wrappers:
        return _wrappers[key]
    server = payments.SERVER
    async with _init_lock:
        if not getattr(server, "_initialized", False):
            await asyncio.to_thread(server.initialize)
    product = PRODUCTS[key]
    name, description = TOOLS[key]
    accepts = []
    for rail in payments.rails():
        accepts.extend(server.build_payment_requirements(ResourceConfig(
            scheme="exact", pay_to=rail.pay_to, price=f"${product.price}", network=rail.network, max_timeout_seconds=120,
        )))
    properties, required, example = payments.INPUTS[key]
    extensions = declare_mcp_discovery_extension(DeclareMcpDiscoveryConfig(
        tool_name=name,
        description=description,
        input_schema=payments._input_schema(key),
        transport="streamable-http",
        example=example,
        output=OutputConfig(example=payments.OUTPUT_EXAMPLES[key]),
    ))
    _wrappers[key] = create_payment_wrapper(server, PaymentWrapperConfig(
        accepts=accepts,
        resource=ResourceInfo(
            url=endpoint(), description=product.summary, mime_type="application/json",
            service_name="AxonGate", tags=list(product.tags), icon_url=f"{config.PUBLIC_BASE_URL}/icon.png",
        ),
        hooks=PaymentWrapperHooks(on_after_settlement=_settled(key)),
        extensions=extensions,
    ))
    return _wrappers[key]


def _settled(key: str):
    async def record(ctx) -> None:
        price = PRODUCTS[key].price
        network = str(getattr(ctx.settlement, "network", "") or "unknown")
        await store.incr("settled", key)
        await store.incr("revenue_micro_usdc", key, int(price * 1_000_000))
        await store.incr("settled_networks", network)
        await store.incr("mcp", "settled")
        await store.push_event("paid_events", {
            "ts": int(time.time()), "product": key, "path": "/mcp", "price_usdc": str(price),
            "client": "mcp", "network": network, "tx": str(getattr(ctx.settlement, "transaction", "") or ""),
        })
    return record


async def _invoke(app, handlers, key: str, args: dict[str, Any]) -> tuple[int, Any]:
    """Run the HTTP handler for a product in-process, bypassing the HTTP payment middleware."""
    body = json.dumps(args).encode()
    scope = {
        "type": "http", "method": "POST", "path": PRODUCTS[key].path, "raw_path": PRODUCTS[key].path.encode(),
        "query_string": b"", "headers": [(b"content-type", b"application/json")], "app": app,
    }

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    response = await handlers[key](Request(scope, receive))
    return response.status_code, json.loads(response.body)


def _text(data: Any) -> list[dict[str, Any]]:
    return [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]


async def call_tool(app, handlers, params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    key = TOOL_PRODUCT.get(name)
    if key is None:
        raise _RpcError(-32602, f"Unknown tool: {name}")
    args = params.get("arguments") or {}
    if not isinstance(args, dict):
        raise _RpcError(-32602, "arguments must be an object")
    if payments.SERVER is None or create_payment_wrapper is None:
        return {"content": _text({"error": "payments_unavailable", "message": "Payments are not configured on this server."}), "isError": True}

    async def handler(tool_args, _context):
        status, data = await _invoke(app, handlers, key, tool_args)
        if status >= 400:
            return {"content": _text(data), "isError": True, "structuredContent": data}
        return {"content": _text(data), "structuredContent": data}

    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
    await store.incr("mcp", "paid_call" if "x402/payment" in meta else "unpaid_call")
    result = await (await _wrapper(key))(handler)(args, {"_meta": meta, "toolName": name})
    out: dict[str, Any] = {"content": list(result.content), "isError": bool(result.is_error)}
    if result.structured_content is not None:
        out["structuredContent"] = result.structured_content
    if result.meta:
        out["_meta"] = result.meta
    if result.is_error and "x402/payment" not in meta and isinstance(result.structured_content, dict) and result.structured_content.get("accepts"):
        product = PRODUCTS[key]
        out["content"].append({"type": "text", "text": (
            f"{name} costs ${product.price} USDC per call. This MCP client did not attach an x402 payment. "
            "Use an x402-capable MCP client, run the local server (npx axongate-mcp, pays from your own wallet "
            f"with a spend cap), or call {config.PUBLIC_BASE_URL}{product.path} with any x402 HTTP client."
        )})
    return out


class _RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


async def _dispatch(app, handlers, message: Any) -> dict[str, Any] | None:
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}}
    method, msg_id = message["method"], message.get("id")
    if msg_id is None:  # notification
        return None
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    try:
        if method == "initialize":
            requested = params.get("protocolVersion")
            result: Any = {
                "protocolVersion": requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "axongate", "title": "AxonGate", "version": config.VERSION, "websiteUrl": config.PUBLIC_BASE_URL},
                "instructions": INSTRUCTIONS,
            }
            await store.incr("mcp", "initialize")
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": tool_list()}
        elif method == "tools/call":
            result = await call_tool(app, handlers, params)
        else:
            raise _RpcError(-32601, f"Method not found: {method}")
    except _RpcError as exc:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": exc.code, "message": str(exc)}}
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


async def handle(request: Request, handlers) -> Response:
    if request.method != "POST":
        return Response(status_code=405, headers={"Allow": "POST"})
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}, status_code=400)
    if isinstance(payload, list):
        replies = [r for r in await asyncio.gather(*(_dispatch(request.app, handlers, m) for m in payload)) if r is not None]
        return JSONResponse(replies) if replies else Response(status_code=202)
    reply = await _dispatch(request.app, handlers, payload)
    return JSONResponse(reply) if reply is not None else Response(status_code=202)
