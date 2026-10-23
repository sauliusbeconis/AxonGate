"""Machine-readable discovery documents for agents, crawlers and x402 directories."""

from __future__ import annotations

import time
from typing import Any

from . import config, payments
from .config import PRODUCTS

TAGLINE = "Safe web access for AI agents"
DESCRIPTION = (
    "AxonGate gives autonomous agents safe, pay-per-call web access: read any public page as clean Markdown, "
    "search the live web, get grounded answers and fact-check claims. Every page is screened for prompt "
    "injection, and every quote in an answer or verdict is checked verbatim against the fetched source. "
    "No account or API key: pay per call in USDC on Base over x402."
)


def _resource(key: str) -> str:
    return f"{config.PUBLIC_BASE_URL}{PRODUCTS[key].path}"


def pricing() -> dict[str, Any]:
    return {
        "currency": "USDC",
        "network": config.NETWORK,
        "asset": config.BASE_USDC_ADDRESS,
        "pay_to": config.PAY_TO,
        "protocol": "x402 v2, exact scheme",
        "facilitator": payments.facilitator_label(),
        "billing": "Charged only when the call succeeds (HTTP 2xx). Errors are never settled.",
        "products": [
            {
                "product": p.key,
                "name": p.title,
                "endpoint": _resource(p.key),
                "methods": list(payments.product_methods(p)),
                "price_usdc": str(p.price),
                "amount_atomic": payments.atomic_amount(p.price),
            }
            for p in PRODUCTS.values()
        ],
    }


def x402_document() -> dict[str, Any]:
    """`/.well-known/x402`: resource URLs (x402scan style) plus full payment requirements."""
    return {
        "version": 1,
        "x402Version": 2,
        "name": "AxonGate",
        "description": DESCRIPTION,
        "resources": [_resource(k) for k in PRODUCTS],
        "items": payments.resource_listing(),
        "docs": f"{config.PUBLIC_BASE_URL}/docs",
        "openapi": f"{config.PUBLIC_BASE_URL}/openapi.json",
        "instructions": (
            "Call any resource without payment to receive a 402 with PAYMENT-REQUIRED requirements, "
            "then retry with a PAYMENT-SIGNATURE header from any x402 client."
        ),
    }


def resources_document() -> dict[str, Any]:
    items = payments.resource_listing()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for item in items:
        item["lastUpdated"] = now
    return {"x402Version": 2, "items": items, "pagination": {"limit": len(items), "offset": 0, "total": len(items)}}


def manifest() -> dict[str, Any]:
    return {
        "$schema": "https://specs.openserv.ai/v1/agent-manifest.schema.json",
        "agent_name": "AxonGate",
        "basename": "axongate.base.eth",
        "payment_protocol": "x402",
        "supported_networks": ["base-mainnet"],
        "identity": {"name": "AxonGate", "basename": "axongate.base.eth", "vault_address": config.PAY_TO, "version": config.VERSION, "status": "active"},
        "service": {
            "name": TAGLINE,
            "category": "Data & Context",
            "description": DESCRIPTION,
            "capabilities": ["web_read", "html_to_markdown", "web_search", "grounded_answers", "fact_checking", "prompt_injection_detection", "verbatim_quote_verification", "ssrf_protection"],
            "input_modes": ["url", "text", "json"],
            "output_modes": ["json", "markdown"],
        },
        "endpoints": [
            {"name": p.title, "url": _resource(p.key), "methods": list(payments.product_methods(p)), "price_usdc": str(p.price), "description": p.summary}
            for p in PRODUCTS.values()
        ],
        "payment": {"protocol": "x402", "version": 2, "scheme": "exact", "network": config.NETWORK, "asset": config.BASE_USDC_ADDRESS, "pay_to": config.PAY_TO},
        "links": {"docs": f"{config.PUBLIC_BASE_URL}/docs", "llms_txt": f"{config.PUBLIC_BASE_URL}/llms.txt", "openapi": f"{config.PUBLIC_BASE_URL}/openapi.json", "source": config.GITHUB_REPO_URL},
    }


def agent_card() -> dict[str, Any]:
    """A2A-style agent card."""
    return {
        "name": "AxonGate",
        "description": DESCRIPTION,
        "url": config.PUBLIC_BASE_URL,
        "version": config.VERSION,
        "provider": {"organization": "AxonGate", "url": config.PUBLIC_BASE_URL},
        "documentationUrl": f"{config.PUBLIC_BASE_URL}/docs",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "defaultInputModes": ["application/json", "text/plain"],
        "defaultOutputModes": ["application/json"],
        "skills": [
            {"id": p.key, "name": p.title, "description": p.summary, "tags": list(p.tags), "examples": [str(payments.INPUTS[p.key][2])]}
            for p in PRODUCTS.values()
        ],
        "payments": {"x402": {"network": config.NETWORK, "asset": config.BASE_USDC_ADDRESS, "payTo": config.PAY_TO, "catalog": f"{config.PUBLIC_BASE_URL}/.well-known/x402"}},
    }


def llms_txt() -> str:
    lines = [
        f"# AxonGate — {TAGLINE}",
        "",
        f"> {DESCRIPTION}",
        "",
        "## Endpoints (pay per call, USDC on Base, x402 v2 exact scheme)",
        "",
    ]
    for p in PRODUCTS.values():
        properties, required, example = payments.INPUTS[p.key]
        methods = "/".join(payments.product_methods(p))
        params = ", ".join(f"{name}{'' if name in required else '?'}" for name in properties)
        lines += [
            f"### {p.title} — ${p.price}",
            f"{methods} {_resource(p.key)}",
            f"Params: {params}",
            f"Example: {example}",
            p.summary,
            "",
        ]
    lines += [
        "## How to pay",
        "1. Call the endpoint without payment. You get HTTP 402 with a PAYMENT-REQUIRED header (base64 JSON requirements).",
        "2. Sign the USDC transfer authorization with any x402 client (e.g. @x402/fetch, x402 Python) and retry with PAYMENT-SIGNATURE.",
        "3. You are charged only if the call succeeds. Errors (4xx/5xx) are never settled.",
        "",
        "## Safety guarantees",
        "- Every page read is scanned for prompt injection; results include injection_risk.",
        "- Quotes in /v1/answer and /v1/verify are checked verbatim against the fetched page; unverifiable quotes are dropped.",
        "- Private, loopback and metadata addresses are refused (SSRF protection).",
        "",
        "## Links",
        f"- Docs: {config.PUBLIC_BASE_URL}/docs",
        f"- Catalog: {config.PUBLIC_BASE_URL}/.well-known/x402",
        f"- OpenAPI: {config.PUBLIC_BASE_URL}/openapi.json",
        f"- Prices: {config.PUBLIC_BASE_URL}/v1/pricing",
        f"- Source: {config.GITHUB_REPO_URL}",
        "",
        "Legacy URLs /v1/x402/access (now Safe Read) and /v1/x402/proof-pack (now Claim Verify) keep working.",
    ]
    return "\n".join(lines) + "\n"
