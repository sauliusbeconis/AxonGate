"""AxonGate buyer example.

This script demonstrates the client side of AxonGate's paid Clean Context
Broker flow. It does not create or sign an x402 payment proof by itself; pass a
payment proof produced by your wallet/facilitator in AXONGATE_PAYMENT_SIGNATURE
or with --payment-signature.

Examples:
    python examples/python_client.py --probe-only
    python examples/python_client.py --target-url https://example.com --payment-signature "$PAYMENT_SIGNATURE"
    python examples/python_client.py --target-url https://example.com --retry-credit "$RETRY_CREDIT"
    python examples/python_client.py --target-url https://example.com --legacy-tx-hash "$BASE_TX_HASH"
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
from typing import Any

import httpx


DEFAULT_BASE_URL = "https://api.axongate.one"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Call AxonGate's x402 Clean Context Broker.")
    parser.add_argument("--base-url", default=os.getenv("AXONGATE_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--target-url", default=os.getenv("TARGET_URL"))
    parser.add_argument(
        "--tier",
        default=os.getenv("AXONGATE_TIER", "fresh"),
        choices=["cached", "basic", "fresh", "deep"],
    )
    parser.add_argument("--force-refresh", action="store_true")
    parser.add_argument("--probe-only", action="store_true")
    parser.add_argument(
        "--payment-signature",
        default=os.getenv("AXONGATE_PAYMENT_SIGNATURE"),
        help="x402 proof for PAYMENT-SIGNATURE. Keep this out of source control.",
    )
    parser.add_argument(
        "--payment-header",
        default=os.getenv("AXONGATE_PAYMENT_HEADER", "PAYMENT-SIGNATURE"),
        help="Header name expected by your x402 facilitator or client library.",
    )
    parser.add_argument(
        "--retry-credit",
        default=os.getenv("AXONGATE_RETRY_CREDIT"),
        help="Short-lived retry credit returned by AxonGate after a retryable post-payment outage.",
    )
    parser.add_argument(
        "--legacy-tx-hash",
        default=os.getenv("AXONGATE_PAYMENT_HASH"),
        help="Base USDC tx hash for the legacy /v1/access compatibility route.",
    )
    parser.add_argument("--timeout", type=float, default=float(os.getenv("AXONGATE_CLIENT_TIMEOUT", "30")))
    return parser.parse_args()


def decode_payment_required(headers: httpx.Headers) -> dict[str, Any] | None:
    encoded = headers.get("PAYMENT-REQUIRED") or headers.get("X-Payment-Required")
    if not encoded:
        return None

    try:
        return json.loads(base64.b64decode(encoded).decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        return None


def request_body(args: argparse.Namespace) -> dict[str, Any]:
    if not args.target_url:
        raise SystemExit("Provide --target-url or TARGET_URL unless using --probe-only.")

    return {
        "target_url": args.target_url,
        "tier": args.tier,
        "force_refresh": bool(args.force_refresh),
    }


async def probe_requirements(client: httpx.AsyncClient) -> dict[str, Any] | None:
    response = await client.get("/v1/x402/access")
    challenge = decode_payment_required(response.headers)
    print(f"Probe status: {response.status_code}")
    if challenge:
        print(json.dumps(challenge, indent=2))
    else:
        print(response.text[:1000])
    return challenge


def print_response(response: httpx.Response) -> None:
    print(f"Response status: {response.status_code}")
    retry_credit = response.headers.get("X-AxonGate-Retry-Credit")
    if retry_credit:
        print("Retry credit received. Store it briefly and retry /v1/x402/retry.")

    content_type = response.headers.get("content-type", "")
    if "application/json" in content_type:
        print(json.dumps(response.json(), indent=2))
    else:
        print(response.text[:4000])


async def call_standard_x402(client: httpx.AsyncClient, args: argparse.Namespace) -> httpx.Response:
    if not args.payment_signature:
        await probe_requirements(client)
        raise SystemExit(
            "No payment proof supplied. Create an x402 proof, then pass --payment-signature "
            "or set AXONGATE_PAYMENT_SIGNATURE."
        )

    headers = {
        "Content-Type": "application/json",
        args.payment_header: args.payment_signature,
        "X-AxonGate-Tier": args.tier,
    }
    return await client.post("/v1/x402/access", json=request_body(args), headers=headers)


async def call_retry_credit(client: httpx.AsyncClient, args: argparse.Namespace) -> httpx.Response:
    headers = {
        "Content-Type": "application/json",
        "X-AxonGate-Retry-Credit": args.retry_credit,
    }
    return await client.post("/v1/x402/retry", json=request_body(args), headers=headers)


async def call_legacy_tx_hash(client: httpx.AsyncClient, args: argparse.Namespace) -> httpx.Response:
    headers = {
        "Content-Type": "application/json",
        "X-AxonGate-Payment-Hash": args.legacy_tx_hash,
    }
    return await client.post("/v1/access", json=request_body(args), headers=headers)


async def main() -> int:
    args = parse_args()
    base_url = args.base_url.rstrip("/")

    async with httpx.AsyncClient(base_url=base_url, timeout=args.timeout) as client:
        if args.probe_only:
            await probe_requirements(client)
            return 0

        if args.retry_credit:
            response = await call_retry_credit(client, args)
        elif args.legacy_tx_hash:
            response = await call_legacy_tx_hash(client, args)
        else:
            response = await call_standard_x402(client, args)

        print_response(response)
        return 0 if response.status_code < 400 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
