"""Submit AxonGate to x402-list.com for manual review.

This script intentionally requires both --email and --submit. Run without
--submit to print the payload only. It defaults to the custom HTTPS domain,
and AXONGATE_PUBLIC_BASE_URL can override that origin for future moves.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import httpx


SUBMIT_URL = "https://x402-list.com/api/v1/submit"
AXONGATE_BASE_URL = os.getenv("AXONGATE_PUBLIC_BASE_URL", "https://api.axongate.one").rstrip("/")
X402_LIST_SOURCE_PATH = "/from/x402-list/v1/x402/starter"


def build_payload(email: str) -> dict:
    return {
        "url": AXONGATE_BASE_URL,
        "service_name": "AxonGate",
        "website_url": f"{AXONGATE_BASE_URL}/manifest.json?source=x402-list",
        "email": email,
        "category": "Data",
        "description": (
            "AxonGate is an x402-paid Clean Context Broker on Base. "
            "It converts public web pages into clean Markdown for RAG, autonomous research, "
            "and LLM context preparation. The starter tier gives agents a 0.012 USDC first "
            "paid conversion path before live fresh extraction."
        ),
        "endpoint_paths": [X402_LIST_SOURCE_PATH],
        "endpoints": [X402_LIST_SOURCE_PATH],
        "notes": (
            "Basename axongate.base.eth resolves to the AxonGate vault and advertises "
            "the manifest URL in its text records. The standard x402 endpoint supports "
            "tiered pricing via ?tier= or X-AxonGate-Tier. The submitted path is a source "
            "alias for attribution and serves starter x402 payment terms. "
            "The endpoint supports official Bazaar discovery metadata, "
            "optional payment-identifier, source attribution, a supplier-free quote API, "
            "starter sample pricing, and cache-only pricing. "
            "Discovery metadata is available at /.well-known/x402, /.well-known/agent.json, "
            "/v1/x402/quote, and /discovery/resources."
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Submit AxonGate to x402-list.com.")
    parser.add_argument("--email", required=True, help="Operator contact email for reviewer follow-up.")
    parser.add_argument("--submit", action="store_true", help="Actually POST the submission.")
    return parser.parse_args()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    args = parse_args()
    payload = build_payload(args.email)

    printable_payload = {**payload, "email": "<redacted>"}
    print(json.dumps(printable_payload, indent=2))

    if not args.submit:
        print("\nDry run only. Add --submit to send this to x402-list.com.")
        return 0

    response = httpx.post(SUBMIT_URL, json=payload, timeout=30, follow_redirects=True)
    print(f"\nHTTP {response.status_code}")
    print(response.text)
    return 0 if response.status_code in {200, 201, 202} else 1


if __name__ == "__main__":
    sys.exit(main())
