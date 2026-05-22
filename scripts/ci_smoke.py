"""CI smoke checks for AxonGate.

The checks avoid paid supplier calls and only exercise discovery, OpenAPI,
manifest, and x402 challenge behavior.
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import axongate_gateway as gateway


async def main() -> None:
    manifest_path = Path(__file__).resolve().parents[1] / "manifest.json"
    json.loads(manifest_path.read_text(encoding="utf-8"))

    transport = httpx.ASGITransport(app=gateway.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        expected_ok = [
            "/",
            "/health",
            "/manifest.json",
            "/.well-known/agent.json",
            "/.well-known/agent-card.json",
            "/.well-known/x402",
            "/.well-known/x402.json",
            "/discovery/resources",
            "/llms.txt",
            "/docs",
            "/operator",
            "/quickstart",
            "/paid-test",
            "/demo",
            "/robots.txt",
            "/sitemap.xml",
            "/openapi.json",
            "/metrics",
        ]
        for path in expected_ok:
            response = await client.get(path)
            assert response.status_code == 200, f"{path} returned {response.status_code}"

        probe = await client.get("/v1/x402/access")
        assert probe.status_code == 402, f"x402 probe returned {probe.status_code}"
        assert probe.headers.get("PAYMENT-REQUIRED"), "x402 probe missing PAYMENT-REQUIRED"
        assert probe.headers.get("X-AxonGate-Quickstart"), "x402 probe missing quickstart path"
        assert probe.headers.get("X-AxonGate-Paid-Test"), "x402 probe missing paid-test buyer path"
        probe_payload = json.loads(base64.b64decode(probe.headers["PAYMENT-REQUIRED"]).decode("utf-8"))
        assert "extensions" in probe_payload, "GET x402 challenge missing official extensions"
        assert "bazaar" in probe_payload["extensions"], "GET x402 challenge missing Bazaar discovery"
        assert "payment-identifier" in probe_payload["extensions"], "GET x402 challenge missing payment identifier"
        probe_body = probe.json()
        assert "next_steps" in probe_body.get("detail", {}), "x402 probe body missing buyer next steps"

        source_alias_probe = await client.get("/from/x402-list/v1/x402/access")
        assert source_alias_probe.status_code == 402, f"source alias probe returned {source_alias_probe.status_code}"
        assert source_alias_probe.headers.get("PAYMENT-REQUIRED"), "source alias probe missing payment terms"

        starter_probe = await client.get("/v1/x402/access?tier=starter")
        assert starter_probe.status_code == 402, f"starter probe returned {starter_probe.status_code}"
        starter_payload = json.loads(base64.b64decode(starter_probe.headers["PAYMENT-REQUIRED"]).decode("utf-8"))
        assert starter_payload["accepts"][0]["amount"] == "12000", "starter tier should cost 0.012 USDC"
        assert starter_payload["accepts"][0]["extra"]["tier"] == "starter", "starter challenge tier mismatch"
        starter_markdown = await gateway.get_cache_candidate_for_tier(
            "https://www.iana.org/domains/reserved",
            "starter",
            False,
        )
        assert starter_markdown and "Reserved Domains" in starter_markdown, "starter sample markdown missing"

        unpaid_post = await client.post(
            "/v1/x402/access?tier=fresh",
            json={"target_url": "https://example.com", "tier": "fresh", "force_refresh": True},
        )
        assert unpaid_post.status_code == 402, f"unpaid POST returned {unpaid_post.status_code}"
        post_challenge = unpaid_post.headers.get("PAYMENT-REQUIRED") or unpaid_post.headers.get("X-Payment-Required")
        assert post_challenge, "unpaid POST missing payment challenge"
        post_payload = json.loads(base64.b64decode(post_challenge).decode("utf-8"))
        assert "extensions" in post_payload, "POST x402 challenge missing official extensions"
        assert "bazaar" in post_payload["extensions"], "POST x402 challenge missing Bazaar discovery"
        assert "payment-identifier" in post_payload["extensions"], "POST x402 challenge missing payment identifier"
        assert unpaid_post.headers.get("X-AxonGate-Buyer-Example"), "unpaid POST missing buyer example header"

        x402_discovery = (await client.get("/.well-known/x402")).json()
        assert "extensions" in x402_discovery, "public x402 discovery missing protocol extensions"
        assert "metadata" in x402_discovery, "public x402 discovery should keep non-protocol metadata"
        assert "cached" in x402_discovery["metadata"]["tiers"], "cached tier missing from public discovery"

        openapi = (await client.get("/openapi.json")).json()
        schemas = openapi.get("components", {}).get("schemas", {})
        assert schemas.get("AccessRequest", {}).get("examples"), "AccessRequest examples missing"
        post_operation = openapi.get("paths", {}).get("/v1/x402/access", {}).get("post", {})
        assert post_operation.get("x-payment-info"), "OpenAPI payment extension missing from paid endpoint"
        assert openapi.get("x-payment-info"), "OpenAPI root payment extension missing"

        metrics = (await client.get("/metrics")).json()
        assert "conversion_funnel" in metrics, "conversion_funnel missing from metrics"
        assert "payment_replay_rejections" in metrics["conversion_funnel"], "replay rejection funnel metric missing"
        assert "attribution" in metrics, "attribution missing from metrics"
        assert "rolling_attribution" in metrics, "rolling_attribution missing from metrics"
        rolling_windows = metrics["rolling_attribution"].get("windows", {})
        for label in ("1h", "24h", "7d"):
            assert label in rolling_windows, f"{label} rolling attribution window missing"
        assert rolling_windows["24h"]["stages"]["discovery_hits"] > 0, "rolling discovery hits should be tracked"
        assert "direct" in rolling_windows["24h"]["sources"], "rolling source attribution should include direct hits"
        assert "metrics_backend" in metrics, "metrics_backend missing from metrics"
        assert "attribution_redis_key" in metrics["metrics_backend"], "attribution Redis key missing from metrics"
        assert "attribution_events_redis_key" in metrics["metrics_backend"], "attribution event Redis key missing from metrics"
        assert "alerts" in metrics, "alerts block missing from metrics"


if __name__ == "__main__":
    asyncio.run(main())
