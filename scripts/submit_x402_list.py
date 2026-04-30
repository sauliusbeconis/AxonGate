"""Submit AxonGate to x402-list.com for manual review.

This script intentionally requires both --email and --submit. Run without
--submit to print the payload only.
"""

from __future__ import annotations

import argparse
import json
import sys

import httpx


SUBMIT_URL = "https://x402-list.com/api/v1/submit"
AXONGATE_BASE_URL = "https://web-production-8136ee.up.railway.app"


def build_payload(email: str) -> dict:
    return {
        "service_name": "AxonGate",
        "service_url": AXONGATE_BASE_URL,
        "website_url": f"{AXONGATE_BASE_URL}/manifest.json?source=x402-list",
        "email": email,
        "category": "Data",
        "description": (
            "AxonGate is an x402-paid Clean Context Broker on Base. "
            "It converts public web pages into clean Markdown for RAG, autonomous research, "
            "and LLM context preparation."
        ),
        "endpoints": "/v1/x402/access?tier=fresh&source=x402-list",
        "notes": (
            "Basename axongate.base.eth resolves to the AxonGate vault and advertises "
            "the manifest URL in its text records. The standard x402 endpoint supports "
            "tiered pricing via ?tier= or X-AxonGate-Tier, official Bazaar discovery metadata, "
            "optional payment-identifier, source attribution, and cache-only pricing. "
            "Discovery metadata is available at /.well-known/x402, /.well-known/agent.json, "
            "and /discovery/resources."
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

    print(json.dumps(payload, indent=2))

    if not args.submit:
        print("\nDry run only. Add --submit to send this to x402-list.com.")
        return 0

    response = httpx.post(SUBMIT_URL, data=payload, timeout=30, follow_redirects=True)
    print(f"\nHTTP {response.status_code}")
    print(response.text)
    return 0 if response.status_code in {200, 201, 202} else 1


if __name__ == "__main__":
    sys.exit(main())
