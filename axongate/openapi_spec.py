"""OpenAPI detail for the paid routes.

The handlers read raw requests on purpose (GET query, POST JSON, legacy aliases), so FastAPI
cannot infer schemas from them. This module builds the request and response documentation
from the same INPUTS and OUTPUT_EXAMPLES tables that feed /docs and the x402 discovery metadata,
so the three stay in sync.
"""

from __future__ import annotations

from typing import Any

from . import config, payments
from .config import Product

ERROR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["error", "message"],
    "properties": {
        "error": {"type": "string", "description": "Machine-readable error code."},
        "message": {"type": "string", "description": "Human-readable explanation."},
    },
}

# Status codes each product can return besides 200 and 402. Failed calls are never settled.
ERRORS: dict[str, dict[int, str]] = {
    "read": {400: "Invalid URL, or a private, loopback or metadata address (refused).",
             413: "Page is larger than the 5 MB limit.", 415: "Unsupported content type.",
             422: "Host did not resolve, or the page had no readable content.",
             424: "The target site refused, failed or redirected too often.", 504: "The page took too long to load."},
    "search": {400: "q is required.", 503: "Search supplier unavailable.", 504: "Timed out."},
    "answer": {400: "question is required, or a supplied URL is invalid.", 422: "A supplied URL did not resolve.",
               424: "No readable sources were found.", 503: "Search or reasoning model unavailable.", 504: "Timed out."},
    "verify": {400: "claim is required, or a supplied URL is invalid.", 422: "A supplied URL did not resolve.",
               424: "No readable sources were found.", 503: "Search or reasoning model unavailable.", 504: "Timed out."},
    "scan": {400: "text is required.", 413: "text is limited to 200000 characters."},
    "token": {400: "Unsupported chain or malformed address.", 404: "No token data for this address on this chain.",
              503: "Token security supplier unavailable.", 504: "Timed out."},
    "batch": {400: "urls must be a list of 1-20 URLs.", 502: "None of the URLs could be read.", 504: "Timed out."},
}


def schema_from_example(value: Any) -> dict[str, Any]:
    """Infer a JSON Schema from an example value (objects, arrays and scalars)."""
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "number"}
    if isinstance(value, str):
        return {"type": "string"}
    if isinstance(value, list):
        return {"type": "array", "items": schema_from_example(value[0]) if value else {}}
    if isinstance(value, dict):
        return {"type": "object", "properties": {k: schema_from_example(v) for k, v in value.items()}}
    return {}


def payment_required_response(product: Product) -> dict[str, Any]:
    networks = ", ".join(r.network for r in payments.rails())
    return {
        "description": (
            f"Payment required: ${product.price} USDC per successful call. The PAYMENT-REQUIRED header carries "
            f"base64 JSON x402 v2 requirements (scheme exact; networks {networks}). Sign an EIP-3009 USDC "
            "authorization with any x402 client and retry with a PAYMENT-SIGNATURE header."
        ),
        "headers": {
            "PAYMENT-REQUIRED": {"description": "Base64-encoded JSON x402 payment requirements.", "schema": {"type": "string"}},
        },
    }


def operation_extra(product: Product, method: str) -> dict[str, Any]:
    """openapi_extra for one paid route: parameters or body, typed 200, 402 and error responses."""
    properties, required, example = payments.INPUTS[product.key]
    extra: dict[str, Any] = {}
    if method == "GET":
        extra["parameters"] = [
            {"name": name, "in": "query", "required": name in required, "description": spec.get("description", ""),
             "schema": {k: v for k, v in spec.items() if k != "description"},
             **({"example": example[name]} if name in example and not isinstance(example[name], (list, dict)) else {})}
            for name, spec in properties.items()
            if spec.get("type") != "array"
        ]
    else:
        extra["requestBody"] = {
            "required": True,
            "content": {"application/json": {
                "schema": {"type": "object", "properties": properties, "required": required},
                "example": example,
            }},
        }

    output = payments.OUTPUT_EXAMPLES[product.key]
    responses: dict[str, Any] = {
        "200": {
            "description": "Success. The call is settled only after this response.",
            "headers": {"PAYMENT-RESPONSE": {"description": "Base64 JSON settlement receipt.", "schema": {"type": "string"}}},
            "content": {"application/json": {"schema": schema_from_example(output), "example": output}},
        },
        "402": payment_required_response(product),
    }
    for status, description in ERRORS.get(product.key, {}).items():
        responses[str(status)] = {
            "description": f"{description} Not charged.",
            "content": {"application/json": {"schema": ERROR_SCHEMA}},
        }
    extra["responses"] = responses
    extra["x-price-usd"] = str(product.price)
    return extra


def info_extra() -> dict[str, Any]:
    return {
        "contact": {"name": f"AxonGate ({config.CONTACT_EMAIL})", "url": config.PUBLIC_BASE_URL},
        "x-source": config.GITHUB_REPO_URL,
    }
