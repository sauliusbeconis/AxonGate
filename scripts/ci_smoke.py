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
            "/quote",
            "/proof-pack",
            "/proof-pack/sample",
            "/proof-pack/quote",
            "/v1/proof-pack/sample",
            "/demo",
            "/robots.txt",
            "/sitemap.xml",
            "/openapi.json",
            "/metrics",
        ]
        for path in expected_ok:
            response = await client.get(path)
            assert response.status_code == 200, f"{path} returned {response.status_code}"

        secure_sample = await client.get(
            "/proof-pack/sample",
            headers={"host": "api.axongate.one", "x-forwarded-proto": "https"},
        )
        assert secure_sample.headers.get("strict-transport-security"), "HTTPS sample missing HSTS header"
        assert secure_sample.headers.get("x-content-type-options") == "nosniff", "security headers missing"

        public_http_sample = await client.get(
            "/proof-pack/sample",
            headers={"host": "api.axongate.one", "x-forwarded-proto": "http"},
            follow_redirects=False,
        )
        assert public_http_sample.status_code == 308, "public HTTP sample should redirect to HTTPS"
        assert public_http_sample.headers.get("location") == (
            "https://api.axongate.one/proof-pack/sample"
        ), "public HTTP sample redirect location mismatch"

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

        source_starter_probe = await client.get("/from/x402-list/v1/x402/starter")
        assert source_starter_probe.status_code == 402, f"source starter probe returned {source_starter_probe.status_code}"
        source_starter_payload = json.loads(
            base64.b64decode(source_starter_probe.headers["PAYMENT-REQUIRED"]).decode("utf-8")
        )
        assert source_starter_payload["accepts"][0]["amount"] == "12000", "source starter path should cost 0.012 USDC"

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

        quote = (await client.get("/v1/x402/quote?target_url=https://www.iana.org/domains/reserved&source=ci")).json()
        assert quote["status"] == "quote", "quote endpoint returned wrong status"
        assert quote["supplier_spend"] is False, "quote endpoint should not trigger supplier spend"
        assert quote["recommended_tier"] == "starter", "sample target should recommend starter"
        assert quote["tiers"]["starter"]["amount_units"] == "12000", "quote endpoint should expose starter amount"
        assert "buyer_command" in quote["next_steps"], "quote endpoint missing buyer command"

        proof_quote = (
            await client.get(
                "/v1/proof-pack/quote?target_url=https://www.iana.org/domains/reserved&pack=quick&source=ci"
            )
        ).json()
        assert proof_quote["status"] == "proof_pack_quote", "Proof Pack quote endpoint returned wrong status"
        assert proof_quote["supplier_spend"] is False, "Proof Pack quote should not trigger supplier spend"
        assert proof_quote["amount_units"] == "100000", "quick Proof Pack should cost 0.10 USDC"
        assert proof_quote["packs"]["standard"]["amount_units"] == "250000", "standard Proof Pack amount mismatch"
        assert "buyer_command" in proof_quote["next_steps"], "Proof Pack quote missing buyer command"
        assert proof_quote["next_steps"]["proof_pack_sample_api"].endswith(
            "/v1/proof-pack/sample"
        ), "Proof Pack quote should point to sample JSON"
        assert proof_quote["next_steps"]["proof_pack_quote_page"].endswith(
            "/proof-pack/quote"
        ), "Proof Pack quote should point to the human quote page"

        proof_quote_page = await client.get(
            "/proof-pack/quote?target_url=https://www.iana.org/domains/reserved&pack=quick&source=ci"
        )
        assert proof_quote_page.status_code == 200, "Proof Pack quote page should render"
        assert "Proof Pack Quote" in proof_quote_page.text, "Proof Pack quote page missing heading"
        assert "100000" in proof_quote_page.text, "Proof Pack quote page missing quick amount"
        assert "Probe Payment Terms" in proof_quote_page.text, "Proof Pack quote page missing paid next step"

        proof_sample = (await client.get("/v1/proof-pack/sample?source=ci")).json()
        assert proof_sample["status"] == "sample", "Proof Pack sample returned wrong status"
        assert proof_sample["supplier_spend"] is False, "Proof Pack sample should not spend supplier budget"
        assert proof_sample["payment"]["mode"] == "sample-no-payment", "Proof Pack sample should not require payment"
        assert proof_sample["payment"]["live_pack_amount_units"] == "100000", "Proof Pack sample live quick amount mismatch"
        assert proof_sample["cache"]["sample"] is True, "Proof Pack sample should identify embedded cache material"
        assert proof_sample["llm_used"] is False, "Proof Pack sample must not call the LLM"
        assert proof_sample["citations"], "Proof Pack sample missing citations"

        proof_probe = await client.get("/v1/x402/proof-pack?pack=standard")
        assert proof_probe.status_code == 402, f"Proof Pack probe returned {proof_probe.status_code}"
        proof_payload = json.loads(base64.b64decode(proof_probe.headers["PAYMENT-REQUIRED"]).decode("utf-8"))
        assert proof_payload["accepts"][0]["amount"] == "250000", "standard Proof Pack should cost 0.25 USDC"
        assert proof_payload["accepts"][0]["extra"]["pack"] == "standard", "standard Proof Pack challenge mismatch"
        assert "extensions" in proof_payload, "Proof Pack challenge missing official extensions"

        quick_proof_probe = await client.get("/v1/x402/proof-pack?pack=quick")
        quick_proof_payload = json.loads(
            base64.b64decode(quick_proof_probe.headers["PAYMENT-REQUIRED"]).decode("utf-8")
        )
        assert quick_proof_payload["accepts"][0]["amount"] == "100000", "quick Proof Pack should cost 0.10 USDC"

        deep_proof_probe = await client.get("/v1/x402/proof-pack?pack=deep")
        deep_proof_payload = json.loads(
            base64.b64decode(deep_proof_probe.headers["PAYMENT-REQUIRED"]).decode("utf-8")
        )
        assert deep_proof_payload["accepts"][0]["amount"] == "1000000", "deep Proof Pack should cost 1.00 USDC"

        proof_source_alias = await client.get("/from/x402-list/v1/x402/proof-pack?pack=standard")
        assert proof_source_alias.status_code == 402, "Proof Pack source alias should return a challenge"

        unpaid_proof_post = await client.post(
            "/v1/x402/proof-pack?pack=standard",
            json={
                "target_url": "https://example.com",
                "question": "What does this source establish?",
                "pack": "standard",
                "force_refresh": False,
            },
        )
        assert unpaid_proof_post.status_code == 402, f"unpaid Proof Pack POST returned {unpaid_proof_post.status_code}"
        unpaid_proof_challenge = unpaid_proof_post.headers.get("PAYMENT-REQUIRED")
        assert unpaid_proof_challenge, "unpaid Proof Pack POST missing payment challenge"
        unpaid_proof_payload = json.loads(base64.b64decode(unpaid_proof_challenge).decode("utf-8"))
        assert unpaid_proof_payload["accepts"][0]["amount"] == "250000", "unpaid Proof Pack POST amount mismatch"

        citations = gateway.extract_proof_pack_evidence(
            "# Source\n\nAxonGate Proof Packs return cited claims for agent builders.",
            "https://example.com/source",
            "standard",
        )
        assert citations[0]["id"] == "c1", "Proof Pack evidence IDs should be stable"
        proof_fallback = await gateway.generate_proof_pack_content(
            target_url="https://example.com/source",
            question="What does this source establish?",
            pack="standard",
            markdown="# Source\n\nAxonGate Proof Packs return cited claims for agent builders.",
            cache_hit=False,
        )
        assert proof_fallback["llm_used"] is False, "LLM-disabled Proof Pack should use deterministic fallback"
        assert proof_fallback["source_profile"]["content_sha256"], "Proof Pack fallback missing source hash"
        try:
            gateway.validate_json_schema(instance={"answer": "missing fields"}, schema=gateway.PROOF_PACK_LLM_SCHEMA)
            raise AssertionError("malformed Proof Pack LLM output should fail schema validation")
        except gateway.JsonSchemaValidationError:
            pass
        sanitized = gateway.sanitize_llm_proof_pack(
            {
                "answer": "Unsupported claim",
                "executive_summary": "Unsupported claim",
                "confidence_score": 0.9,
                "key_claims": [{"claim": "Not in evidence", "citation_ids": ["missing"], "confidence": 0.9}],
                "risks": [],
            },
            citations,
            proof_fallback,
        )
        assert sanitized["llm_used"] is False, "unsupported LLM claims should fall back or be dropped"

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
        assert "proofPacks" in x402_discovery["metadata"], "Proof Pack pricing missing from public discovery"
        assert "proofPackSampleApi" in x402_discovery["metadata"], "Proof Pack sample missing from public discovery"
        assert "proofPackQuote" in x402_discovery["metadata"], "Proof Pack quote page missing from public discovery"

        openapi = (await client.get("/openapi.json")).json()
        schemas = openapi.get("components", {}).get("schemas", {})
        assert schemas.get("AccessRequest", {}).get("examples"), "AccessRequest examples missing"
        assert schemas.get("ProofPackRequest", {}).get("examples"), "ProofPackRequest examples missing"
        post_operation = openapi.get("paths", {}).get("/v1/x402/access", {}).get("post", {})
        assert post_operation.get("x-payment-info"), "OpenAPI payment extension missing from paid endpoint"
        proof_operation = openapi.get("paths", {}).get("/v1/x402/proof-pack", {}).get("post", {})
        assert proof_operation.get("x-payment-info"), "OpenAPI payment extension missing from Proof Pack endpoint"
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
        assert "proof_pack_pricing" in metrics, "Proof Pack pricing missing from metrics"


if __name__ == "__main__":
    asyncio.run(main())
