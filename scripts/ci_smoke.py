"""CI smoke checks for AxonGate.

The checks avoid paid supplier calls and only exercise discovery, OpenAPI,
manifest, and x402 challenge behavior.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import sys
import time
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import axongate_gateway as gateway

gateway.OPERATOR_TOKEN = "ci-operator-token"
gateway.STRIPE_WEBHOOK_SECRET = "whsec_ci_smoke"
gateway.EMAIL_DELIVERY_ENABLED = True
gateway.EMAIL_PROVIDER = "resend"
gateway.EMAIL_FROM = "AxonGate <reports@axongate.one>"
gateway.RESEND_API_KEY = "re_ci_smoke"
sent_delivery_emails: list[dict] = []


async def fake_send_resend_email(payload: dict) -> dict:
    sent_delivery_emails.append(payload)
    return {"id": f"email_ci_{len(sent_delivery_emails)}"}


gateway.send_resend_email = fake_send_resend_email


async def fake_fetch_current_base_fee_wei() -> int:
    return 5_000_000


async def fake_fetch_eth_usdc_quote() -> gateway.EthUsdQuote:
    return gateway.EthUsdQuote(
        price=Decimal("3500"),
        source="ci-static",
        fetched_at=int(time.time()),
        floor_applied=False,
    )


gateway.fetch_current_base_fee_wei = fake_fetch_current_base_fee_wei
gateway.fetch_eth_usdc_quote = fake_fetch_eth_usdc_quote


def stripe_signature(payload: bytes, secret: str) -> str:
    timestamp = int(time.time())
    signed_payload = f"{timestamp}.".encode("utf-8") + payload
    digest = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


async def main() -> None:
    manifest_path = Path(__file__).resolve().parents[1] / "manifest.json"
    manifest_json = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest_json["identity"]["version"] == gateway.app.version, "manifest version should match app"
    assert manifest_json["endpoints"]["proof_pack_verify_api_pattern"].endswith("/{report_id}/verify"), (
        "manifest missing Proof Pack verify pattern"
    )
    assert manifest_json["endpoints"]["quote_receipt_api_pattern"].endswith("/v1/quotes/{quote_id}"), (
        "manifest missing quote receipt pattern"
    )
    assert manifest_json["endpoints"]["checkout_resume_pattern"].endswith("/checkout/{quote_id}"), (
        "manifest missing checkout resume pattern"
    )
    assert manifest_json["proof_pack_contract"]["success_response"]["verify_url"] == "string", (
        "manifest Proof Pack contract missing verify_url"
    )
    distribution_path = Path(__file__).resolve().parents[1] / "docs" / "distribution-payloads.json"
    distribution_json = json.loads(distribution_path.read_text(encoding="utf-8"))
    assert distribution_json["proof_pack_verify_api_pattern"].endswith("/{report_id}/verify"), (
        "distribution payload missing Proof Pack verify pattern"
    )
    assert distribution_json["quote_receipt_api_pattern"].endswith("/v1/quotes/{quote_id}"), (
        "distribution payload missing quote receipt pattern"
    )
    resend_request = httpx.Request("POST", "https://api.resend.com/emails")
    resend_response = httpx.Response(
        403,
        json={"message": "Sender reports@axongate.one is not verified."},
        request=resend_request,
    )
    resend_error = httpx.HTTPStatusError("Resend rejected email", request=resend_request, response=resend_response)
    resend_detail, resend_status = gateway.describe_email_delivery_exception(resend_error)
    assert resend_status == 403, "email diagnostic should preserve provider status code"
    assert "HTTP 403" in resend_detail, "email diagnostic should include HTTP status"
    assert "reports@axongate.one" not in resend_detail, "email diagnostic should redact email addresses"

    transport = httpx.ASGITransport(app=gateway.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        expected_ok = [
            "/",
            "/health",
            "/v1/access/health",
            "/v1/agent/diagnose",
            "/v1/agent/trust",
            "/manifest.json",
            "/.well-known/agent.json",
            "/.well-known/agent-card.json",
            "/.well-known/x402",
            "/.well-known/x402.json",
            "/discovery/resources",
            "/x402/discovery/resources",
            "/v2/x402/discovery/resources",
            "/llms.txt",
            "/docs",
            "/about",
            "/faq",
            "/contact",
            "/operator",
            "/quickstart",
            "/paid-test",
            "/checkout/confidence",
            "/v1/checkout/confidence",
            "/quote",
            "/proof-pack",
            "/proof-pack/library",
            "/proof-pack/library/source-trust-for-agent-builders",
            "/proof-pack/share/source-trust-for-agent-builders",
            "/from/x402-list",
            "/proof-pack/sample",
            "/v1/proof-pack/benchmarks",
            "/proof-pack/reports/ppr_sample_source_trust",
            "/v1/proof-pack/reports/ppr_sample_source_trust/verify",
            "/proof-pack/preview",
            "/proof-pack/quote",
            "/proof-pack/request",
            "/proof-pack/bundle",
            "/proof-pack/bundle/quote",
            "/proof-pack/bundle/checkout",
            "/v1/proof-pack/sample",
            "/v1/proof-pack/reports/ppr_sample_source_trust",
            "/v1/proof-pack/preview",
            "/demo",
            "/robots.txt",
            "/sitemap.xml",
            "/openapi.json",
            "/metrics",
        ]
        for path in expected_ok:
            response = await client.get(path)
            assert response.status_code == 200, f"{path} returned {response.status_code}"

        root_page = await client.get("/")
        assert "Can these sources prove your claim?" in root_page.text, "Homepage missing evidence-check headline"
        assert "Start Builder - $7" in root_page.text, "Homepage missing primary Stripe CTA"
        assert "Parser returns text. AxonGate returns a decision." in root_page.text, (
            "Homepage should explain why AxonGate is more than parsing"
        )
        assert "Make the deliverable visible before checkout." in root_page.text, (
            "Homepage should expose buyer-facing report examples"
        )
        legacy_checkout_copy = "Hu" + "man checkout " + "funnel"
        legacy_package_copy = "Choose the " + "h" + "uman package"
        assert legacy_checkout_copy not in root_page.text, "Homepage should not expose internal funnel wording"
        assert legacy_package_copy not in root_page.text, "Homepage should not expose awkward package wording"
        root_discovery = await client.get("/?format=json")
        assert root_discovery.status_code == 200, "Root discovery JSON should still render"
        root_discovery_json = root_discovery.json()
        assert root_discovery_json["status"] == "alive", "Root discovery JSON returned wrong status"
        assert root_discovery_json["checkout_confidence"].endswith("/checkout/confidence"), (
            "Root discovery missing checkout confidence"
        )
        assert root_discovery_json["quote_receipt_api_pattern"].endswith("/v1/quotes/{quote_id}"), (
            "Root discovery missing quote receipt pattern"
        )
        assert root_discovery_json["checkout_resume_pattern"].endswith("/checkout/{quote_id}"), (
            "Root discovery missing checkout resume pattern"
        )
        assert root_discovery_json["proof_pack_library"].endswith("/proof-pack/library"), (
            "Root discovery missing Evidence Library"
        )
        assert root_discovery_json["source_landing_pattern"].endswith("/from/{source}"), (
            "Root discovery missing source landing pattern"
        )
        assert root_discovery_json["agent_diagnostic"].endswith("/v1/agent/diagnose"), (
            "Root discovery missing agent diagnostic"
        )
        assert root_discovery_json["agent_trust"].endswith("/v1/agent/trust"), (
            "Root discovery missing agent trust"
        )
        assert root_discovery_json["proof_pack_benchmarks"].endswith("/v1/proof-pack/benchmarks"), (
            "Root discovery missing Proof Pack benchmarks"
        )
        assert root_discovery_json["proof_pack_verify_api_pattern"].endswith(
            "/v1/proof-pack/reports/{report_id}/verify"
        ), "Root discovery missing Proof Pack verify pattern"
        diagnostic = await client.get("/v1/agent/diagnose?source=ci-diagnostic")
        assert diagnostic.status_code == 200, "Agent diagnostic should render"
        diagnostic_json = diagnostic.json()
        assert diagnostic_json["status"] == "ok", "Agent diagnostic returned wrong status"
        assert diagnostic_json["supplier_spend"] is False, "Agent diagnostic should not spend"
        assert diagnostic_json["payment_header"] == "PAYMENT-SIGNATURE", "Agent diagnostic payment header mismatch"
        assert diagnostic_json["network"] == "eip155:8453", "Agent diagnostic network mismatch"
        assert diagnostic_json["asset"]["address"] == gateway.BASE_USDC_ADDRESS, "Agent diagnostic asset mismatch"
        assert diagnostic_json["paid_endpoints"]["proof_pack"]["amount_units"] == "250000", (
            "Agent diagnostic Proof Pack amount mismatch"
        )
        assert diagnostic_json["proof_pack"]["sample_report_api"].endswith("/ppr_sample_source_trust"), (
            "Agent diagnostic should expose sample retained report"
        )
        assert diagnostic_json["sample_report"]["follow_up_api"].endswith("/follow-up"), (
            "Agent diagnostic should expose sample follow-up"
        )
        assert diagnostic_json["sample_report"]["verify_api"].endswith("/verify"), (
            "Agent diagnostic should expose sample verify receipt"
        )
        assert diagnostic_json["proof_pack"]["verify_api_pattern"].endswith("/{report_id}/verify"), (
            "Agent diagnostic should expose verify receipt pattern"
        )
        assert diagnostic_json["minimal_paid_requests"]["proof_pack"]["headers"]["PAYMENT-SIGNATURE"], (
            "Agent diagnostic missing minimal Proof Pack payment header"
        )
        assert diagnostic_json["common_failure_fixes"], "Agent diagnostic missing common failure fixes"
        assert diagnostic_json["recommended_next_call"]["method"] == "GET", (
            "Agent diagnostic should recommend a no-spend GET next call"
        )
        assert diagnostic_json["trust_url"].endswith("/v1/agent/trust"), "Agent diagnostic missing trust link"
        trust = await client.get("/v1/agent/trust?source=ci-trust")
        assert trust.status_code == 200, "Agent trust endpoint should render"
        trust_json = trust.json()
        assert trust_json["status"] == "ok", "Agent trust returned wrong status"
        assert trust_json["supplier_spend"] is False, "Agent trust should not spend"
        assert trust_json["payment_required"] is False, "Agent trust should not require payment"
        assert trust_json["spend_policy"]["supplier_work_starts_after_payment"] is True, (
            "Agent trust should explain supplier spend boundary"
        )
        assert trust_json["safety_policy"]["rejects_private_networks"] is True, (
            "Agent trust should explain private network rejection"
        )
        assert "source_hash" in trust_json["output_contract"]["stable_fields"], (
            "Agent trust should document source_hash"
        )
        assert "verify_url" in trust_json["output_contract"]["stable_fields"], (
            "Agent trust should document verify_url"
        )
        assert trust_json["verification_policy"]["sample_verify_api"].endswith("/ppr_sample_source_trust/verify"), (
            "Agent trust should expose sample verify receipt"
        )
        assert trust_json["benchmark_library"]["count"] >= 6, "Agent trust should expose benchmark cases"
        assert trust_json["benchmark_library"]["cases"][0]["trust_score_breakdown"], (
            "Benchmark cases should include trust score breakdowns"
        )
        assert trust_json["best_first_paid_call"]["amount_units"] == "250000", (
            "Agent trust should recommend standard Proof Pack amount"
        )
        benchmarks = await client.get("/v1/proof-pack/benchmarks?source=ci-trust")
        assert benchmarks.status_code == 200, "Proof Pack benchmarks should render"
        benchmarks_json = benchmarks.json()
        assert benchmarks_json["supplier_spend"] is False, "Proof Pack benchmarks should not spend"
        assert benchmarks_json["count"] >= 6, "Proof Pack benchmarks should include enough cases"
        actions = {case["agent_action"] for case in benchmarks_json["cases"]}
        assert {"cite", "needs_second_source", "ingest_with_caution", "do_not_cite"} <= actions, (
            "Proof Pack benchmarks should span agent actions"
        )

        proof_pack_page = await client.get("/proof-pack")
        assert "<link rel=\"canonical\"" in proof_pack_page.text, "Proof Pack page missing canonical link"
        assert "Contact</a>" in proof_pack_page.text, "Proof Pack page footer should include Contact"
        assert "A parser returns text" in proof_pack_page.text, "Proof Pack page should keep parser contrast"
        assert "Evidence Library" in proof_pack_page.text, "Proof Pack page should link growth library"
        assert "report_id" in proof_pack_page.text, "Proof Pack page should surface retained report IDs"
        assert "/v1/proof-pack/reports/{report_id}/follow-up" in proof_pack_page.text, (
            "Proof Pack page should surface no-spend follow-up API"
        )
        assert "/v1/proof-pack/reports/{report_id}/verify" in proof_pack_page.text, (
            "Proof Pack page should surface verify receipt API"
        )
        docs_page = await client.get("/docs")
        assert "source_quality_score" in docs_page.text, "Docs page should explain source quality score"
        assert "/v1/agent/diagnose" in docs_page.text, "Docs page should expose agent diagnostic"
        assert "/v1/agent/trust" in docs_page.text, "Docs page should expose agent trust"
        assert "/v1/proof-pack/benchmarks" in docs_page.text, "Docs page should expose Proof Pack benchmarks"
        assert "/v1/proof-pack/reports/{report_id}/refresh" in docs_page.text, (
            "Docs page should explain refresh quote API"
        )
        assert "/v1/proof-pack/reports/{report_id}/verify" in docs_page.text, (
            "Docs page should explain verify receipt API"
        )
        assert "/v1/proof-pack/reports/ppr_sample_source_trust" in docs_page.text, (
            "Docs page should expose concrete sample retained report"
        )
        llms_txt = await client.get("/llms.txt")
        assert "Agent diagnostic API" in llms_txt.text, "llms.txt should expose agent diagnostic"
        assert "Agent trust API" in llms_txt.text, "llms.txt should expose agent trust"
        assert "Proof Pack benchmark API" in llms_txt.text, "llms.txt should expose Proof Pack benchmarks"
        assert "Proof Pack report API pattern" in llms_txt.text, "llms.txt should expose report API pattern"
        assert "Proof Pack sample report API" in llms_txt.text, "llms.txt should expose sample report API"
        assert "Proof Pack verify receipt API pattern" in llms_txt.text, (
            "llms.txt should expose verify receipt pattern"
        )
        assert "Store report_id" in llms_txt.text, "llms.txt should explain report handle storage"
        library_page = await client.get("/proof-pack/library")
        assert "Public source-trust examples" in library_page.text, "Evidence Library missing growth headline"
        assert "Quote Similar Report" in library_page.text, "Evidence Library missing buyer CTA"
        library_entry = await client.get("/proof-pack/library/source-trust-for-agent-builders")
        assert "Source Trust For Agent Builders" in library_entry.text, "Library entry missing title"
        assert "Embed" in library_entry.text, "Library entry missing embed snippet"
        share_page = await client.get("/proof-pack/share/source-trust-for-agent-builders")
        assert "AxonGate evidence check" in share_page.text, "Share page missing evidence-card framing"
        assert "Quote Similar Report" in share_page.text, "Share page missing conversion CTA"
        source_landing = await client.get("/from/x402-list")
        assert "x402 source trust endpoint" in source_landing.text, "Source landing missing tailored headline"
        assert "/from/x402-list/v1/x402/proof-pack" in source_landing.text, "Source landing missing alias endpoint"
        proof_sample_page = await client.get("/proof-pack/sample")
        assert "Sample Evidence Decision" in proof_sample_page.text, "Sample page should lead with evidence decision"
        assert "Why This Is Worth Buying" in proof_sample_page.text, "Sample page should explain buyer value"
        assert "Reusable Sample Report" in proof_sample_page.text, "Sample page should expose sample retained report"
        assert "View full API JSON" in proof_sample_page.text, "Sample page should keep technical JSON available"
        faq_page = await client.get("/faq")
        assert "Is AxonGate just a page parser?" in faq_page.text, "FAQ page missing parser question"
        contact_page = await client.get("/contact")
        assert "Contact AxonGate" in contact_page.text, "Contact page missing heading"
        assert "name=\"email\"" in contact_page.text, "Contact page missing email field"
        sitemap_page = await client.get("/sitemap.xml")
        assert "/proof-pack/library" in sitemap_page.text, "Sitemap missing Evidence Library"
        assert "/proof-pack/share/source-trust-for-agent-builders" in sitemap_page.text, (
            "Sitemap missing share example"
        )
        assert "/from/x402-list" in sitemap_page.text, "Sitemap missing source landing page"
        assert "/v1/agent/trust" in sitemap_page.text, "Sitemap missing agent trust"
        assert "/v1/proof-pack/benchmarks" in sitemap_page.text, "Sitemap missing Proof Pack benchmarks"
        assert "/v1/proof-pack/reports/ppr_sample_source_trust/verify" in sitemap_page.text, (
            "Sitemap missing sample verify receipt"
        )

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
        favicon = await client.get("/favicon.ico")
        assert favicon.status_code == 204, "favicon probes should return a tiny no-content response"

        legacy_health = (await client.get("/v1/access/health")).json()
        assert legacy_health["status"] == "alive", "legacy health compatibility route should be alive"
        assert legacy_health["payment_required"] is True, "legacy health should advertise paid access"
        legacy_probe = await client.get("/v1/access")
        assert legacy_probe.status_code == 402, f"legacy access GET probe returned {legacy_probe.status_code}"
        assert legacy_probe.headers.get("PAYMENT-REQUIRED"), "legacy access GET probe missing payment terms"

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
        assert gateway.validate_target_url("www.example.com") == "https://www.example.com", (
            "bare domain target URLs should be prefixed with https"
        )
        assert gateway.normalize_recovery_url("http://www.example.com/") == gateway.normalize_recovery_url(
            "www.example.com"
        ), "recovery URL matching should be scheme agnostic"

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
        assert proof_quote["next_steps"]["after_payment"]["report_api_pattern"].endswith(
            "/v1/proof-pack/reports/{report_id}"
        ), "Proof Pack quote should explain retained report API"
        assert "follow_up_api_pattern" in proof_quote["next_steps"]["after_payment"], (
            "Proof Pack quote should explain follow-up reuse"
        )
        assert proof_quote["next_steps"]["after_payment"]["verify_api_pattern"].endswith(
            "/v1/proof-pack/reports/{report_id}/verify"
        ), "Proof Pack quote should explain verify receipts"
        assert proof_quote["next_steps"]["after_payment"]["sample_report_id"] == "ppr_sample_source_trust", (
            "Proof Pack quote should expose sample report id"
        )
        assert proof_quote["next_steps"]["after_payment"]["sample_report_api"].endswith(
            "/v1/proof-pack/reports/ppr_sample_source_trust"
        ), "Proof Pack quote should expose sample retained report"
        assert proof_quote["next_steps"]["after_payment"]["sample_verify_api"].endswith(
            "/v1/proof-pack/reports/ppr_sample_source_trust/verify"
        ), "Proof Pack quote should expose sample verify receipt"
        assert proof_quote["next_steps"]["proof_pack_sample_api"].endswith(
            "/v1/proof-pack/sample"
        ), "Proof Pack quote should point to sample JSON"
        assert "proof_pack_preview_page" in proof_quote["next_steps"], "Proof Pack quote should point to mini preview"
        assert proof_quote["next_steps"]["proof_pack_quote_page"].endswith(
            "/proof-pack/quote"
        ), "Proof Pack quote should point to the quote page"
        assert "checkout/confidence" in proof_quote["next_steps"]["checkout_confidence_page"], (
            "Proof Pack quote should point to checkout confidence"
        )
        assert "/v1/checkout/confidence" in proof_quote["next_steps"]["checkout_confidence_api"], (
            "Proof Pack quote should point to checkout confidence API"
        )
        proof_quote_id = proof_quote.get("quote_id", "")
        assert proof_quote_id.startswith("qr_"), "Proof Pack quote should include a stable quote_id"
        assert proof_quote["quote_receipt"]["resume_checkout_url"].endswith(f"/checkout/{proof_quote_id}"), (
            "Proof Pack quote should include a resume URL"
        )
        assert proof_quote["next_steps"]["quote_receipt_api"].endswith(f"/v1/quotes/{proof_quote_id}"), (
            "Proof Pack quote should include quote receipt API"
        )
        proof_quote_receipt = await client.get(f"/v1/quotes/{proof_quote_id}")
        assert proof_quote_receipt.status_code == 200, "Proof Pack quote receipt should be readable"
        proof_quote_receipt_json = proof_quote_receipt.json()
        assert proof_quote_receipt_json["status"] == "quote_receipt", "Proof Pack receipt returned wrong status"
        assert proof_quote_receipt_json["product"] == "proof_pack", "Proof Pack receipt returned wrong product"
        assert proof_quote_receipt_json["quote"]["status"] == "proof_pack_quote", "Receipt should retain the quote"
        proof_quote_resume = await client.get(f"/checkout/{proof_quote_id}")
        assert proof_quote_resume.status_code == 200, "Proof Pack quote resume page should render"
        assert "Quote ID" in proof_quote_resume.text, "Proof Pack quote resume page missing quote ID"
        resumed_proof_quote = (await client.get(f"/v1/proof-pack/quote?quote_id={proof_quote_id}")).json()
        assert resumed_proof_quote["quote_id"] == proof_quote_id, "Proof Pack quote should resume by quote_id"
        proof_confidence_by_quote = (await client.get(f"/v1/checkout/confidence?quote_id={proof_quote_id}")).json()
        assert proof_confidence_by_quote["quote_id"] == proof_quote_id, "Proof Pack confidence should load by quote_id"
        assert proof_confidence_by_quote["selected"]["product"] == "proof_pack", (
            "Proof Pack confidence-by-quote selected wrong product"
        )

        proof_preview = (
            await client.get(
                "/v1/proof-pack/preview?target_url=https://www.iana.org/domains/reserved&pack=quick&source=ci"
            )
        ).json()
        assert proof_preview["status"] == "proof_pack_preview", "Proof Pack preview returned wrong status"
        assert proof_preview["supplier_spend"] is False, "Proof Pack preview should not spend supplier budget"
        assert proof_preview["preview_available"] is True, "sample target should produce a mini preview"
        assert proof_preview["payment"]["full_pack_amount_units"] == "100000", "preview should expose quick amount"
        assert proof_preview["citations"], "cached mini preview should include citations"
        assert "buyer_command" in proof_preview["next_steps"], "Proof Pack preview missing buyer command"
        assert "checkout/confidence" in proof_preview["next_steps"]["checkout_confidence_page"], (
            "Proof Pack preview missing checkout confidence"
        )

        preview_miss = (
            await client.get(
                "/v1/proof-pack/preview?target_url=https://www.rfc-editor.org/rfc/rfc9110.html&pack=quick&source=ci"
            )
        ).json()
        assert preview_miss["status"] == "proof_pack_preview", "Proof Pack preview miss returned wrong status"
        assert preview_miss["supplier_spend"] is False, "Proof Pack preview miss should not spend supplier budget"
        assert preview_miss["preview_available"] is False, "uncached example preview should miss cleanly"
        assert preview_miss["citations"] == [], "uncached preview should not invent citations"

        proof_preview_page = await client.get(
            "/proof-pack/preview?target_url=https://www.iana.org/domains/reserved&pack=quick&source=ci"
        )
        assert proof_preview_page.status_code == 200, "Proof Pack preview page should render"
        assert "Proof Pack Preview" in proof_preview_page.text, "Proof Pack preview page missing heading"
        assert "Mini Answer" in proof_preview_page.text, "Proof Pack preview page missing mini answer"
        assert "Try Mini Preview" in proof_preview_page.text or "Open Preview JSON" in proof_preview_page.text, (
            "Proof Pack preview page missing conversion links"
        )

        proof_quote_page = await client.get(
            "/proof-pack/quote?target_url=https://www.iana.org/domains/reserved&pack=quick&source=ci"
        )
        assert proof_quote_page.status_code == 200, "Proof Pack quote page should render"
        assert "Proof Pack Quote" in proof_quote_page.text, "Proof Pack quote page missing heading"
        assert "100000" in proof_quote_page.text, "Proof Pack quote page missing quick amount"
        assert "Probe Payment Terms" in proof_quote_page.text, "Proof Pack quote page missing paid next step"
        assert "Request This Report" in proof_quote_page.text, "Proof Pack quote page missing request CTA"
        assert "Try Mini Preview" in proof_quote_page.text, "Proof Pack quote page missing preview CTA"
        assert "Payment Confidence" in proof_quote_page.text, "Proof Pack quote page missing confidence CTA"
        assert "Quote ID" in proof_quote_page.text, "Proof Pack quote page missing quote ID"
        assert "POST /v1/x402/proof-pack" in proof_quote_page.text, "Proof Pack quote page missing short endpoint"
        assert "Full Paid Endpoint" in proof_quote_page.text, "Proof Pack quote page should keep full URL in a scroll-safe block"

        pack_confidence = (
            await client.get(
                "/v1/checkout/confidence?product=proof_pack&target_url=https://www.iana.org/domains/reserved&pack=quick&source=ci"
            )
        ).json()
        assert pack_confidence["status"] == "checkout_confidence", "Proof Pack confidence returned wrong status"
        assert pack_confidence["supplier_spend"] is False, "Proof Pack confidence should not spend"
        assert pack_confidence["payment_required"] is False, "Proof Pack confidence should not require payment"
        assert pack_confidence["selected"]["product"] == "proof_pack", "Proof Pack confidence selected wrong product"
        assert pack_confidence["selected"]["amount_units"] == "100000", "Proof Pack confidence quick amount mismatch"
        assert pack_confidence["proof_before_payment"], "Proof Pack confidence missing proof points"
        assert pack_confidence["next_steps"]["sample_verify_api"].endswith(
            "/v1/proof-pack/reports/ppr_sample_source_trust/verify"
        ), "Proof Pack confidence missing sample verify receipt"
        pack_confidence_page = await client.get(
            "/checkout/confidence?product=proof_pack&target_url=https://www.iana.org/domains/reserved&pack=quick&source=ci"
        )
        assert pack_confidence_page.status_code == 200, "Proof Pack confidence page should render"
        assert "Payment Confidence" in pack_confidence_page.text, "Proof Pack confidence page missing heading"
        assert "Proof Before Payment" in pack_confidence_page.text, "Proof Pack confidence page missing proof section"
        assert "Payment Failure Fixes" in pack_confidence_page.text, "Proof Pack confidence page missing payment fixes"

        proof_request_page = await client.get(
            "/proof-pack/request?target_url=https://www.iana.org/domains/reserved&pack=quick&source=ci"
        )
        assert proof_request_page.status_code == 200, "Proof Pack request page should render"
        assert "Proof Pack Request" in proof_request_page.text, "Proof Pack request page missing heading"
        assert "POST /v1/x402/proof-pack" in proof_request_page.text, "Proof Pack request page missing short endpoint"

        lead_payload = {
            "contact": "codex-test@example.invalid",
            "target_url": "https://www.iana.org/domains/reserved",
            "question": "Which claims can my agent cite?",
            "pack": "quick",
            "use_case": "CI smoke test",
            "budget_usdc": "10/month",
            "source": "ci",
            "notes": "No-spend lead capture check.",
        }
        proof_lead = await client.post("/v1/proof-pack/leads", json=lead_payload)
        assert proof_lead.status_code == 200, f"Proof Pack lead API returned {proof_lead.status_code}"
        proof_lead_json = proof_lead.json()
        assert proof_lead_json["status"] == "received", "Proof Pack lead API returned wrong status"
        assert proof_lead_json["amount_units"] == "100000", "Proof Pack lead should preserve quick price"
        assert proof_lead_json["contact_received"] is True, "Proof Pack lead should acknowledge contact privately"
        assert "contact" not in proof_lead_json, "Proof Pack lead response must not echo contact"
        assert "proof-pack/request" in proof_lead_json["next_steps"]["request_page"], "Lead response missing request page"

        form_lead = await client.post(
            "/proof-pack/request",
            content=urlencode(lead_payload),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        assert form_lead.status_code == 200, f"Proof Pack request form returned {form_lead.status_code}"
        assert "Request received" in form_lead.text, "Proof Pack request form should acknowledge submit"

        bundle_query = urlencode(
            {
                "target_urls": "https://www.iana.org/domains/reserved\nhttps://example.com",
                "bundle": "scout",
                "source": "ci",
            }
        )
        proof_bundle_quote = (await client.get(f"/v1/proof-pack/bundle/quote?{bundle_query}")).json()
        assert proof_bundle_quote["status"] == "proof_bundle_quote", "Proof Bundle quote returned wrong status"
        assert proof_bundle_quote["supplier_spend"] is False, "Proof Bundle quote should not spend supplier budget"
        assert proof_bundle_quote["target_count"] == 2, "Proof Bundle quote should preserve target count"
        assert proof_bundle_quote["amount_units"] == "2000000", "scout Proof Bundle should cost 2.00 USDC"
        assert proof_bundle_quote["source_limit"] == 3, "scout Proof Bundle source limit mismatch"
        assert "bundle_page" in proof_bundle_quote["next_steps"], "Proof Bundle quote missing bundle page"
        assert "proof-pack/bundle/pay" in proof_bundle_quote["next_steps"]["checkout_url"], (
            "Proof Bundle quote missing tracked checkout URL"
        )
        assert "proof-pack/bundle/checkout" in proof_bundle_quote["next_steps"]["checkout_review_url"], (
            "Proof Bundle quote missing customer checkout review URL"
        )
        assert "checkout/confidence" in proof_bundle_quote["next_steps"]["checkout_confidence_page"], (
            "Proof Bundle quote missing checkout confidence page"
        )
        assert "/v1/checkout/confidence" in proof_bundle_quote["next_steps"]["checkout_confidence_api"], (
            "Proof Bundle quote missing checkout confidence API"
        )
        assert "checkout_confidence_page" in proof_bundle_quote["bundles"]["scout"], (
            "Proof Bundle variants missing confidence links"
        )
        proof_bundle_quote_id = proof_bundle_quote.get("quote_id", "")
        assert proof_bundle_quote_id.startswith("qr_"), "Proof Bundle quote should include a stable quote_id"
        assert proof_bundle_quote["quote_receipt"]["resume_checkout_url"].endswith(f"/checkout/{proof_bundle_quote_id}"), (
            "Proof Bundle quote should include a resume URL"
        )
        assert proof_bundle_quote["next_steps"]["checkout_url"].endswith(f"/proof-pack/bundle/pay?quote_id={proof_bundle_quote_id}"), (
            "Proof Bundle quote should shorten tracked checkout with quote_id"
        )
        proof_bundle_receipt = await client.get(f"/v1/quotes/{proof_bundle_quote_id}")
        assert proof_bundle_receipt.status_code == 200, "Proof Bundle quote receipt should be readable"
        proof_bundle_receipt_json = proof_bundle_receipt.json()
        assert proof_bundle_receipt_json["product"] == "proof_bundle", "Proof Bundle receipt returned wrong product"
        assert proof_bundle_receipt_json["quote"]["status"] == "proof_bundle_quote", "Bundle receipt should retain quote"
        resumed_bundle_quote = (await client.get(f"/v1/proof-pack/bundle/quote?quote_id={proof_bundle_quote_id}")).json()
        assert resumed_bundle_quote["quote_id"] == proof_bundle_quote_id, "Proof Bundle quote should resume by quote_id"

        bundle_confidence = (await client.get(f"/v1/checkout/confidence?product=proof_bundle&{bundle_query}")).json()
        assert bundle_confidence["status"] == "checkout_confidence", "Proof Bundle confidence returned wrong status"
        assert bundle_confidence["supplier_spend"] is False, "Proof Bundle confidence should not spend"
        assert bundle_confidence["selected"]["product"] == "proof_bundle", "Proof Bundle confidence selected wrong product"
        assert bundle_confidence["selected"]["amount_units"] == "2000000", "Proof Bundle confidence scout amount mismatch"
        assert bundle_confidence["target_urls"], "Proof Bundle confidence should preserve targets"
        assert bundle_confidence["low_friction_options"], "Proof Bundle confidence missing alternatives"
        bundle_confidence_by_quote = (await client.get(f"/v1/checkout/confidence?quote_id={proof_bundle_quote_id}")).json()
        assert bundle_confidence_by_quote["quote_id"] == proof_bundle_quote_id, (
            "Proof Bundle confidence should load by quote_id"
        )
        assert bundle_confidence_by_quote["selected"]["product"] == "proof_bundle", (
            "Proof Bundle confidence-by-quote selected wrong product"
        )

        proof_bundle_page = await client.get(f"/proof-pack/bundle/quote?{bundle_query}")
        assert proof_bundle_page.status_code == 200, "Proof Bundle quote page should render"
        assert "Proof Bundle Quote" in proof_bundle_page.text, "Proof Bundle quote page missing heading"
        assert "Request Bundle" in proof_bundle_page.text, "Proof Bundle quote page missing request CTA"
        assert "Review checkout" in proof_bundle_page.text, "Proof Bundle quote page missing checkout review CTA"
        assert "Payment Confidence" in proof_bundle_page.text, "Proof Bundle quote page missing confidence CTA"
        assert "Quote ID" in proof_bundle_page.text, "Proof Bundle quote page missing quote ID"
        assert "Before you pay" in proof_bundle_page.text, "Proof Bundle quote page should explain value before payment"
        assert "What This Payment Buys" in proof_bundle_page.text, "Proof Bundle quote page missing deliverables"
        assert "After Checkout" in proof_bundle_page.text, "Proof Bundle quote page missing post-payment flow"
        assert "Parser returns text. AxonGate returns a decision." in proof_bundle_page.text, (
            "Proof Bundle quote page should explain the buyer value beyond parsing"
        )
        assert "Delivery promise" in proof_bundle_page.text, "Proof Bundle quote page missing delivery promise"
        assert "Make the deliverable visible before checkout." in proof_bundle_page.text, (
            "Proof Bundle quote page should show report examples before checkout"
        )
        assert "POST https://api.axongate.one/v1/x402/proof-pack?pack=standard" in proof_bundle_page.text, (
            "Proof Bundle quote should keep immediate x402 fallback in a scroll-safe block"
        )
        proof_bundle_checkout_review = await client.get(f"/proof-pack/bundle/checkout?{bundle_query}")
        assert proof_bundle_checkout_review.status_code == 200, "Proof Bundle checkout review should render"
        assert "Review your Evidence Bundle" in proof_bundle_checkout_review.text, "Checkout review missing heading"
        assert "Continue to" in proof_bundle_checkout_review.text, "Checkout review missing final continue CTA"
        assert "Payment Confidence" in proof_bundle_checkout_review.text, "Checkout review missing confidence CTA"
        assert "What This Payment Buys" in proof_bundle_checkout_review.text, "Checkout review missing value section"
        assert "Parser returns text. AxonGate returns a decision." in proof_bundle_checkout_review.text, (
            "Checkout review should keep parser contrast before payment"
        )
        assert "Delivery promise" in proof_bundle_checkout_review.text, "Checkout review missing delivery promise"
        assert "/proof-pack/bundle/pay" in proof_bundle_checkout_review.text, "Checkout review should preserve tracked pay redirect"
        proof_bundle_checkout_by_quote = await client.get(f"/proof-pack/bundle/checkout?quote_id={proof_bundle_quote_id}")
        assert proof_bundle_checkout_by_quote.status_code == 200, "Proof Bundle checkout should resume by quote_id"
        assert "Quote ID" in proof_bundle_checkout_by_quote.text, "Resumed checkout should show quote ID"
        assert f"quote_id={proof_bundle_quote_id}" in proof_bundle_checkout_by_quote.text, (
            "Resumed checkout should preserve quote_id in pay redirect"
        )
        proof_bundle_pay_by_quote = await client.get(
            f"/proof-pack/bundle/pay?quote_id={proof_bundle_quote_id}",
            follow_redirects=False,
        )
        assert proof_bundle_pay_by_quote.status_code == 302, "Proof Bundle pay should accept quote_id"
        proof_bundle_checkout = await client.get(f"/proof-pack/bundle/pay?{bundle_query}", follow_redirects=False)
        assert proof_bundle_checkout.status_code == 302, "Proof Bundle checkout should redirect"
        assert "/proof-pack/bundle" in proof_bundle_checkout.headers["location"], (
            "Unconfigured Proof Bundle checkout should fall back to request capture"
        )

        bundle_lead_payload = {
            "contact": "codex-bundle@example.invalid",
            "target_urls": ["https://www.iana.org/domains/reserved", "https://example.com"],
            "question": "Which claims can our agent cite across these sources?",
            "bundle": "scout",
            "use_case": "CI multi-source smoke test",
            "budget_usdc": "20/month",
            "source": "ci",
            "notes": "No-spend bundle lead capture check.",
        }
        proof_bundle_lead = await client.post("/v1/proof-pack/bundle/leads", json=bundle_lead_payload)
        assert proof_bundle_lead.status_code == 200, f"Proof Bundle lead API returned {proof_bundle_lead.status_code}"
        proof_bundle_lead_json = proof_bundle_lead.json()
        assert proof_bundle_lead_json["status"] == "received", "Proof Bundle lead API returned wrong status"
        assert proof_bundle_lead_json["product"] == "proof_bundle", "Proof Bundle lead response should mark product"
        assert proof_bundle_lead_json["target_count"] == 2, "Proof Bundle lead target count mismatch"
        assert proof_bundle_lead_json["amount_units"] == "2000000", "Proof Bundle lead should preserve scout price"
        assert proof_bundle_lead_json["contact_received"] is True, "Proof Bundle lead should acknowledge contact privately"
        assert "contact" not in proof_bundle_lead_json, "Proof Bundle lead response must not echo contact"

        no_ua_operator_orders = await client.get("/operator/orders", headers={"User-Agent": ""})
        assert no_ua_operator_orders.status_code == 204, "blank-UA operator crawler should be quietly suppressed"
        assert no_ua_operator_orders.headers.get("X-AxonGate-Crawler-Guard") == "no-user-agent", (
            "blank-UA guard should mark suppressed private probes"
        )
        no_ua_paid_requests = await client.get("/operator/paid-requests", headers={"User-Agent": ""})
        assert no_ua_paid_requests.status_code == 204, "blank-UA paid request crawler should be quietly suppressed"
        no_ua_webhook = await client.post("/v1/stripe/webhook", headers={"User-Agent": ""})
        assert no_ua_webhook.status_code == 204, "blank-UA webhook crawler should be quietly suppressed"

        operator_leads_unauthorized = await client.get("/operator/leads", headers={"User-Agent": "ci-browser"})
        assert operator_leads_unauthorized.status_code == 401, "operator leads should require a token"
        operator_orders_unauthorized = await client.get("/operator/orders", headers={"User-Agent": "ci-browser"})
        assert operator_orders_unauthorized.status_code == 401, "operator orders should require a token"
        operator_paid_requests_unauthorized = await client.get(
            "/v1/operator/paid-requests",
            headers={"User-Agent": "ci-browser"},
        )
        assert operator_paid_requests_unauthorized.status_code == 401, "operator paid requests should require a token"
        operator_paid_requests_page_unauthorized = await client.get(
            "/operator/paid-requests",
            headers={"User-Agent": "ci-browser"},
        )
        assert operator_paid_requests_page_unauthorized.status_code == 401, (
            "operator paid requests page should require a token"
        )

        operator_leads_page = await client.get("/operator/leads?operator_token=ci-operator-token&limit=10")
        assert operator_leads_page.status_code == 200, "operator leads page should accept operator token"
        assert "Proof Pack Leads" in operator_leads_page.text, "operator leads page missing heading"
        assert "codex-test@example.invalid" in operator_leads_page.text, "operator leads page should show private contact"
        assert "codex-bundle@example.invalid" in operator_leads_page.text, "operator leads page should show bundle contact"
        assert "Buyer Command" in operator_leads_page.text, "operator leads page missing conversion next step"

        operator_leads_api = await client.get(
            "/v1/operator/leads?limit=10",
            headers={"X-AxonGate-Operator-Token": "ci-operator-token"},
        )
        assert operator_leads_api.status_code == 200, "operator leads API should accept operator token header"
        operator_leads_json = operator_leads_api.json()
        assert operator_leads_json["status"] == "ok", "operator leads API returned wrong status"
        assert operator_leads_json["stats"]["retained"] >= 3, "operator leads API should retain smoke leads"
        assert operator_leads_json["stats"]["by_product"].get("proof_bundle", 0) >= 1, (
            "operator leads API should summarize Proof Bundle leads"
        )
        assert "new" in operator_leads_json["stats"]["by_status"], "operator leads API should include pipeline status"
        assert any(
            lead.get("contact") == "codex-test@example.invalid"
            for lead in operator_leads_json["leads"]
        ), "operator leads API should include private contact"
        assert any(
            lead.get("contact") == "codex-bundle@example.invalid" and lead.get("product") == "proof_bundle"
            for lead in operator_leads_json["leads"]
        ), "operator leads API should include private bundle contact"
        operator_orders_page = await client.get("/operator/orders?operator_token=ci-operator-token&limit=10")
        assert operator_orders_page.status_code == 200, "operator orders page should accept operator token"
        assert "Proof Bundle Orders" in operator_orders_page.text, "operator orders page missing heading"
        assert "codex-bundle@example.invalid" in operator_orders_page.text, "operator orders page should show bundle contact"
        assert "Order Console" in operator_orders_page.text, "operator orders page missing console table"
        operator_orders_api = await client.get(
            "/v1/operator/orders?limit=10",
            headers={"X-AxonGate-Operator-Token": "ci-operator-token"},
        )
        assert operator_orders_api.status_code == 200, "operator orders API should accept operator token header"
        operator_orders_json = operator_orders_api.json()
        assert operator_orders_json["status"] == "ok", "operator orders API returned wrong status"
        assert operator_orders_json["stats"]["orders"] >= 1, "operator orders API should include bundle orders"
        assert any(
            order.get("contact") == "codex-bundle@example.invalid"
            for order in operator_orders_json["orders"]
        ), "operator orders API should expose private bundle buyer details"
        await gateway.store_paid_request_event(
            {
                "product": "proof_pack",
                "status": "delivered",
                "source": "ci-paid",
                "target_url": "https://example.com/source",
                "question": "Which claims can my agent cite?",
                "pack": "standard",
                "amount_units": "250000",
                "cache_hit": False,
                "llm_used": False,
                "fallback_reason": "llm_disabled",
                "citation_count": 1,
                "confidence_score": 0.62,
                "report_id": "ppr_ci_event",
                "report_url": "https://api.axongate.one/v1/proof-pack/reports/ppr_ci_event",
                "result_hash": "ci_result_hash",
            }
        )
        operator_paid_requests_page = await client.get(
            "/operator/paid-requests?operator_token=ci-operator-token&limit=10"
        )
        assert operator_paid_requests_page.status_code == 200, "operator paid requests page should accept token"
        assert "Paid Request Diagnostics" in operator_paid_requests_page.text, (
            "operator paid requests page missing heading"
        )
        assert "Which claims can my agent cite?" in operator_paid_requests_page.text, (
            "operator paid requests page should expose private paid question text"
        )
        assert "ppr_ci_event" in operator_paid_requests_page.text, (
            "operator paid requests page should expose retained report id"
        )
        operator_paid_requests_api = await client.get(
            "/v1/operator/paid-requests?limit=10",
            headers={"X-AxonGate-Operator-Token": "ci-operator-token"},
        )
        assert operator_paid_requests_api.status_code == 200, "operator paid requests API should accept token header"
        operator_paid_requests_json = operator_paid_requests_api.json()
        assert operator_paid_requests_json["status"] == "ok", "operator paid requests API returned wrong status"
        assert operator_paid_requests_json["stats"]["by_product"].get("proof_pack", 0) >= 1, (
            "operator paid requests API should summarize paid Proof Pack records"
        )
        assert any(
            event.get("question") == "Which claims can my agent cite?"
            for event in operator_paid_requests_json["events"]
        ), "operator paid requests API should expose private paid question text"
        operator_status = await client.post(
            f"/v1/operator/leads/{proof_bundle_lead_json['lead_id']}/status",
            headers={"X-AxonGate-Operator-Token": "ci-operator-token"},
            json={
                "status": "paid",
                "note": "CI marked paid.",
                "fulfillment_url": "https://example.com/report",
            },
        )
        assert operator_status.status_code == 200, "operator status API should accept valid status updates"
        operator_status_json = operator_status.json()
        assert operator_status_json["lead"]["status"] == "paid", "operator status API should persist paid status"
        assert operator_status_json["lead"]["fulfillment_url"] == "https://example.com/report", (
            "operator status API should persist fulfillment URL"
        )

        await gateway.set_cached_markdown(
            "https://www.iana.org/domains/reserved",
            "basic",
            gateway.STARTER_SAMPLE_MARKDOWN,
            3600,
        )
        await gateway.set_cached_markdown(
            "https://example.com",
            "basic",
            "# Example Domain\n\nExample Domain is reserved for illustrative examples in documents.",
            3600,
        )
        stripe_event = {
            "id": "evt_ci_axongate_checkout_paid",
            "object": "event",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_ci_axongate_paid",
                    "object": "checkout.session",
                    "payment_status": "paid",
                    "currency": "usd",
                    "amount_total": 200,
                    "payment_link": "plink_ci_scout",
                    "payment_intent": "pi_ci_axongate",
                    "metadata": {"bundle": "scout", "source": "ci-stripe"},
                    "customer_details": {
                        "email": "stripe-buyer@example.invalid",
                        "name": "Stripe Buyer",
                    },
                    "custom_fields": [
                        {
                            "key": "target_urls",
                            "label": {"type": "custom", "custom": "Target URLs"},
                            "type": "text",
                            "text": {
                                "value": "https://www.iana.org/domains/reserved\nhttps://example.com"
                            },
                        },
                        {
                            "key": "question_or_claim_to_verify",
                            "label": {"type": "custom", "custom": "Question or claim to verify"},
                            "type": "text",
                            "text": {"value": "Can these sources support the claim?"},
                        },
                    ],
                }
            },
        }
        stripe_payload = json.dumps(stripe_event, separators=(",", ":"), sort_keys=True).encode("utf-8")
        stripe_webhook = await client.post(
            "/v1/stripe/webhook",
            content=stripe_payload,
            headers={
                "Stripe-Signature": stripe_signature(stripe_payload, gateway.STRIPE_WEBHOOK_SECRET),
                "content-type": "application/json",
            },
        )
        assert stripe_webhook.status_code == 200, f"Stripe webhook returned {stripe_webhook.status_code}"
        stripe_webhook_json = stripe_webhook.json()
        assert stripe_webhook_json["status"] == "fulfilled", "Stripe webhook should fulfill paid checkout"
        assert stripe_webhook_json["bundle"] == "scout", "Stripe webhook should preserve bundle metadata"
        assert stripe_webhook_json["lead_status"] == "paid", "Stripe webhook should mark lead paid"
        assert "/proof-pack/bundle/delivery" in stripe_webhook_json["delivery_url"], (
            "Stripe webhook should return a customer delivery URL"
        )

        stripe_delivery = await client.get(
            "/v1/proof-pack/bundle/delivery?session_id=cs_ci_axongate_paid"
        )
        assert stripe_delivery.status_code == 200, "Stripe delivery JSON should resolve by session ID"
        stripe_delivery_json = stripe_delivery.json()
        assert stripe_delivery_json["status"] == "ready", "Stripe delivery should generate a ready report"
        assert stripe_delivery_json["lead_status"] == "fulfilled", "Stripe delivery should mark lead fulfilled"
        assert stripe_delivery_json["report"]["successful_sources"] >= 1, "Stripe delivery report should include sources"
        assert stripe_delivery_json["delivery"]["version"] == "delivery-v2", "Stripe delivery should expose Delivery v2 model"
        assert stripe_delivery_json["delivery"]["quality_label"], "Delivery v2 should include an evidence quality label"
        assert stripe_delivery_json["delivery"]["evidence_matrix"], "Delivery v2 should include source verdict matrix"
        assert stripe_delivery_json["delivery"]["evidence_matrix"][0]["verdict"], (
            "Evidence matrix should include a source verdict"
        )
        assert stripe_delivery_json["delivery"]["evidence_matrix"][0]["next_action"], (
            "Evidence matrix should include a next action"
        )
        assert "download_pdf" in stripe_delivery_json["delivery"]["actions"], "Delivery v2 should include PDF action"
        assert "download_json" in stripe_delivery_json["delivery"]["actions"], "Delivery v2 should include JSON action"
        assert sent_delivery_emails, "Stripe delivery should send a customer report email when configured"
        assert sent_delivery_emails[-1]["to"] == ["stripe-buyer@example.invalid"], "Delivery email recipient mismatch"
        assert "Open report" in sent_delivery_emails[-1]["html"], "Delivery email should include report link"
        assert "Evidence decision" in sent_delivery_emails[-1]["html"], "Delivery email should include decision signal"
        assert "Evidence Matrix" in sent_delivery_emails[-1]["html"], "Delivery email should include source verdict matrix"
        assert "Evidence quality" in sent_delivery_emails[-1]["html"], "Delivery email should include quality signal"
        assert "Download PDF" in sent_delivery_emails[-1]["html"], "Delivery email should include PDF download"
        assert "](" not in sent_delivery_emails[-1]["html"], "Delivery email should not expose raw markdown links"

        stripe_delivery_page = await client.get(
            "/proof-pack/bundle/delivery?session_id=cs_ci_axongate_paid"
        )
        assert stripe_delivery_page.status_code == 200, "Stripe delivery page should render"
        assert "Proof Bundle Report" in stripe_delivery_page.text, "Stripe delivery page missing report heading"
        assert "Evidence decision" in stripe_delivery_page.text, "Stripe delivery page missing decision section"
        assert "Evidence Matrix" in stripe_delivery_page.text, "Stripe delivery page missing evidence matrix"
        assert "Next action:" in stripe_delivery_page.text, "Stripe delivery page missing source next action"
        assert "What You Paid For" in stripe_delivery_page.text, "Stripe delivery page missing value section"
        assert "What This Establishes" in stripe_delivery_page.text, "Stripe delivery page missing findings section"
        assert "Recommended Next Actions" in stripe_delivery_page.text, "Stripe delivery page missing next actions"
        assert "Source Quality Audit" in stripe_delivery_page.text, "Stripe delivery page missing source audit"
        assert "Download PDF" in stripe_delivery_page.text, "Stripe delivery page missing PDF action"

        stripe_json_download = await client.get(
            "/proof-pack/bundle/delivery.json?session_id=cs_ci_axongate_paid"
        )
        assert stripe_json_download.status_code == 200, "Stripe delivery JSON download should resolve"
        assert stripe_json_download.headers.get("content-disposition", "").endswith(".json\""), (
            "JSON download should include attachment filename"
        )
        assert stripe_json_download.json()["delivery"]["version"] == "delivery-v2", "JSON download should include Delivery v2"
        assert stripe_json_download.json()["delivery"]["evidence_matrix"], "JSON download should include evidence matrix"

        stripe_pdf_download = await client.get(
            "/proof-pack/bundle/delivery.pdf?session_id=cs_ci_axongate_paid"
        )
        assert stripe_pdf_download.status_code == 200, "Stripe delivery PDF download should resolve"
        assert stripe_pdf_download.content.startswith(b"%PDF-1.4"), "PDF download should return PDF bytes"
        assert stripe_pdf_download.headers.get("content-disposition", "").endswith(".pdf\""), (
            "PDF download should include attachment filename"
        )

        stripe_print_page = await client.get(
            "/proof-pack/bundle/delivery/print?session_id=cs_ci_axongate_paid"
        )
        assert stripe_print_page.status_code == 200, "Stripe delivery print page should render"
        assert "Proof Bundle Report" in stripe_print_page.text, "Print page missing report heading"
        assert "Evidence Matrix" in stripe_print_page.text, "Print page missing evidence matrix"

        recovery_form = await client.get("/proof-pack/bundle/recover")
        assert recovery_form.status_code == 200, "Proof Bundle recovery form should render"
        assert "Recover Proof Bundle Delivery" in recovery_form.text, "Proof Bundle recovery form missing heading"
        assert 'type="text" name="target_url"' in recovery_form.text, "Recovery form should accept bare domains"
        assert "www.example.com or https://example.com" in recovery_form.text, "Recovery form should explain URL input"

        recovered_delivery = await client.get(
            "/v1/proof-pack/bundle/recover",
            params={
                "email": "stripe-buyer@example.invalid",
                "target_url": "https://example.com",
            },
        )
        assert recovered_delivery.status_code == 200, "Stripe delivery should recover by email and target URL"
        recovered_delivery_json = recovered_delivery.json()
        assert recovered_delivery_json["status"] == "ready", "Recovered delivery should be ready"
        assert recovered_delivery_json["lead_id"] == stripe_delivery_json["lead_id"], "Recovered delivery should match paid lead"

        missing_recovery = await client.get(
            "/v1/proof-pack/bundle/recover",
            params={
                "email": "stripe-buyer@example.invalid",
                "target_url": "https://missing.example.invalid",
            },
        )
        assert missing_recovery.status_code == 404, "Recovery should reject unmatched target URLs"

        mismatch_target = "https://example.com/mismatched-email-recovery"
        await gateway.set_cached_markdown(
            mismatch_target,
            "basic",
            "# Mismatched Email Recovery\n\nThis source is cached for a paid bundle recovery test.",
            3600,
        )
        mismatched_email_stripe_event = {
            "id": "evt_ci_axongate_checkout_mismatched_email",
            "object": "event",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_ci_axongate_mismatched_email",
                    "object": "checkout.session",
                    "payment_status": "paid",
                    "currency": "usd",
                    "amount_total": 200,
                    "payment_link": "plink_ci_scout",
                    "payment_intent": "pi_ci_axongate_mismatched_email",
                    "metadata": {"bundle": "scout", "source": "ci-stripe"},
                    "customer_details": {
                        "email": "recorded-buyer@example.invalid",
                        "name": "Recorded Buyer",
                    },
                    "custom_fields": [
                        {
                            "key": "target_urls",
                            "label": {"type": "custom", "custom": "Target URLs"},
                            "type": "text",
                            "text": {"value": mismatch_target},
                        },
                    ],
                }
            },
        }
        mismatched_email_payload = json.dumps(
            mismatched_email_stripe_event,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        mismatched_email_webhook = await client.post(
            "/v1/stripe/webhook",
            content=mismatched_email_payload,
            headers={
                "Stripe-Signature": stripe_signature(mismatched_email_payload, gateway.STRIPE_WEBHOOK_SECRET),
                "content-type": "application/json",
            },
        )
        assert mismatched_email_webhook.status_code == 200, "Mismatched-email Stripe webhook should be accepted"
        mismatched_email_recovery = await client.get(
            "/v1/proof-pack/bundle/recover",
            params={
                "email": "wrong-buyer@example.invalid",
                "target_url": mismatch_target,
            },
        )
        assert mismatched_email_recovery.status_code == 200, "Unique paid target should recover despite email mismatch"
        assert mismatched_email_recovery.json()["lead_id"] == mismatched_email_webhook.json()["lead_id"], (
            "Mismatched email recovery should return the unique paid target lead"
        )

        no_email_stripe_event = {
            "id": "evt_ci_axongate_checkout_no_email",
            "object": "event",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_ci_axongate_no_email",
                    "object": "checkout.session",
                    "payment_status": "paid",
                    "currency": "usd",
                    "amount_total": 200,
                    "payment_link": "plink_ci_scout",
                    "payment_intent": "pi_ci_axongate_no_email",
                    "metadata": {"bundle": "scout", "source": "ci-stripe"},
                    "custom_fields": [
                        {
                            "key": "target_urls",
                            "label": {"type": "custom", "custom": "Target URLs"},
                            "type": "text",
                            "text": {"value": "https://example.com"},
                        },
                    ],
                }
            },
        }
        no_email_payload = json.dumps(no_email_stripe_event, separators=(",", ":"), sort_keys=True).encode("utf-8")
        no_email_webhook = await client.post(
            "/v1/stripe/webhook",
            content=no_email_payload,
            headers={
                "Stripe-Signature": stripe_signature(no_email_payload, gateway.STRIPE_WEBHOOK_SECRET),
                "content-type": "application/json",
            },
        )
        assert no_email_webhook.status_code == 200, "No-email Stripe webhook should be accepted"
        no_email_recovery = await client.get(
            "/v1/proof-pack/bundle/recover",
            params={
                "email": "buyer-entered@example.invalid",
                "target_url": "https://example.com",
            },
        )
        assert no_email_recovery.status_code == 200, "No-email Stripe delivery should recover by target URL"
        no_email_recovery_json = no_email_recovery.json()
        assert no_email_recovery_json["status"] == "ready", "No-email recovered delivery should be ready"

        pending_recovery_target = "https://example.com/pending-recovered-target"
        await gateway.set_cached_markdown(
            pending_recovery_target,
            "basic",
            "# Pending Recovery\n\nThis source is cached for a recovered pending Stripe bundle.",
            3600,
        )
        pending_recovery_lead = {
            "id": "stripe_ci_pending_recovery",
            "created_at": int(time.time()),
            "product": "proof_bundle",
            "contact": "stripe-session:cs_ci_pending_recovery",
            "target_url": "not-a-public-url",
            "target_urls": ["not-a-public-url"],
            "target_count": 1,
            "question": "Recover the single pending paid bundle.",
            "bundle": "scout",
            "pack": "scout",
            "source": "stripe",
            "status": "paid",
            "stripe": {
                "session_id": "cs_ci_pending_recovery",
                "payment_status": "paid",
            },
        }
        await gateway.store_proof_pack_lead(pending_recovery_lead)
        pending_recovery = await client.get(
            "/v1/proof-pack/bundle/recover",
            params={
                "email": "buyer-entered@example.invalid",
                "target_url": pending_recovery_target,
            },
        )
        assert pending_recovery.status_code == 200, "Single pending Stripe bundle should recover as a rescue path"
        pending_recovery_json = pending_recovery.json()
        assert pending_recovery_json["lead_id"] == "stripe_ci_pending_recovery", (
            "Pending recovery should return the single unresolved Stripe lead"
        )
        assert pending_recovery_json["status"] == "ready", "Pending recovery should use the submitted target URL"
        assert pending_recovery_json["report"]["source_reports"][0]["target_url"] == pending_recovery_target, (
            "Pending recovery report should use the customer-submitted target URL"
        )

        duplicate_stripe_webhook = await client.post(
            "/v1/stripe/webhook",
            content=stripe_payload,
            headers={
                "Stripe-Signature": stripe_signature(stripe_payload, gateway.STRIPE_WEBHOOK_SECRET),
                "content-type": "application/json",
            },
        )
        assert duplicate_stripe_webhook.status_code == 200, "Duplicate Stripe event should be acknowledged"
        assert duplicate_stripe_webhook.json()["status"] == "duplicate", "Duplicate Stripe event should not refill"

        bad_stripe_webhook = await client.post(
            "/v1/stripe/webhook",
            content=stripe_payload,
            headers={"Stripe-Signature": "t=1,v1=bad", "content-type": "application/json"},
        )
        assert bad_stripe_webhook.status_code == 400, "Invalid Stripe signature should be rejected"

        operator_leads_after_stripe = (
            await client.get(
                "/v1/operator/leads?limit=20",
                headers={"X-AxonGate-Operator-Token": "ci-operator-token"},
            )
        ).json()
        stripe_lead = next(
            (
                lead
                for lead in operator_leads_after_stripe["leads"]
                if lead.get("contact") == "stripe-buyer@example.invalid"
            ),
            None,
        )
        assert stripe_lead, "Stripe webhook should create an operator lead"
        assert stripe_lead["status"] == "fulfilled", "Stripe-created lead should be auto-fulfilled"
        assert stripe_lead["stripe"]["session_id"] == "cs_ci_axongate_paid", "Stripe lead should retain session ID"

        operator_orders_after_stripe_page = await client.get("/operator/orders?operator_token=ci-operator-token&limit=20")
        assert operator_orders_after_stripe_page.status_code == 200, "operator orders page should render after Stripe"
        assert "stripe-buyer@example.invalid" in operator_orders_after_stripe_page.text, (
            "operator orders page should show Stripe buyer email"
        )
        assert "cs_ci_axongate_paid" in operator_orders_after_stripe_page.text, (
            "operator orders page should show Stripe session ID"
        )
        assert "Resend Email" in operator_orders_after_stripe_page.text, (
            "operator orders page should expose email recovery action"
        )
        operator_orders_after_stripe_api = await client.get(
            "/v1/operator/orders?limit=20",
            headers={"X-AxonGate-Operator-Token": "ci-operator-token"},
        )
        assert operator_orders_after_stripe_api.status_code == 200, "operator orders API should render after Stripe"
        operator_orders_after_stripe_json = operator_orders_after_stripe_api.json()
        stripe_order = next(
            (
                order
                for order in operator_orders_after_stripe_json["orders"]
                if order.get("stripe_session_id") == "cs_ci_axongate_paid"
            ),
            None,
        )
        assert stripe_order, "operator orders API should include Stripe order"
        assert stripe_order["status"] == "fulfilled", "Stripe order should be fulfilled in orders API"
        assert stripe_order["delivery_ready"] is True, "Stripe order should expose ready delivery state"
        assert stripe_order["email_status"] == "sent", "Stripe order should expose sent email status"
        sent_delivery_count = len(sent_delivery_emails)
        resend_order_email = await client.post(
            f"/v1/operator/orders/{stripe_lead['id']}/resend-email",
            headers={"X-AxonGate-Operator-Token": "ci-operator-token"},
        )
        assert resend_order_email.status_code == 200, "operator order resend API should succeed"
        resend_order_email_json = resend_order_email.json()
        assert resend_order_email_json["status"] == "ok", "operator order resend API returned wrong status"
        assert resend_order_email_json["order"]["email_status"] == "sent", "resend should restore sent email status"
        assert len(sent_delivery_emails) == sent_delivery_count + 1, "resend should send one extra delivery email"

        proof_sample = (await client.get("/v1/proof-pack/sample?source=ci")).json()
        assert proof_sample["status"] == "sample", "Proof Pack sample returned wrong status"
        assert proof_sample["supplier_spend"] is False, "Proof Pack sample should not spend supplier budget"
        assert proof_sample["report_id"] == "ppr_sample_source_trust", "Proof Pack sample should expose sample report id"
        assert proof_sample["report_url"].endswith("/v1/proof-pack/reports/ppr_sample_source_trust"), (
            "Proof Pack sample should expose sample report API"
        )
        assert proof_sample["verify_url"].endswith("/v1/proof-pack/reports/ppr_sample_source_trust/verify"), (
            "Proof Pack sample should expose sample verify receipt"
        )
        assert proof_sample["follow_up_url"].endswith("/ppr_sample_source_trust/follow-up"), (
            "Proof Pack sample should expose sample follow-up API"
        )
        assert proof_sample["refresh_url"].endswith("/ppr_sample_source_trust/refresh"), (
            "Proof Pack sample should expose sample refresh quote API"
        )
        assert proof_sample["payment"]["mode"] == "sample-no-payment", "Proof Pack sample should not require payment"
        assert proof_sample["payment"]["live_pack_amount_units"] == "100000", "Proof Pack sample live quick amount mismatch"
        assert proof_sample["cache"]["sample"] is True, "Proof Pack sample should identify embedded cache material"
        assert proof_sample["llm_used"] is False, "Proof Pack sample must not call the LLM"
        assert proof_sample["citations"], "Proof Pack sample missing citations"
        assert proof_sample["decision"]["label"], "Proof Pack sample should expose machine-readable decision"
        assert proof_sample["source_quality_score"] >= 0, "Proof Pack sample should expose source quality score"
        assert proof_sample["agent_action"], "Proof Pack sample should expose recommended agent action"
        assert proof_sample["recommended_next_call"]["action"], "Proof Pack sample should expose the next agent call"
        assert proof_sample["supported_findings"], "Proof Pack sample should expose supported findings"
        assert proof_sample["gaps"], "Proof Pack sample should expose evidence gaps"
        assert proof_sample["citation_coverage"]["citation_count"] >= 1, (
            "Proof Pack sample should expose citation coverage"
        )
        assert proof_sample["report_card"]["decision_label"], "Proof Pack sample should expose a buyer decision"
        assert proof_sample["report_card"]["buyer_value"], "Proof Pack sample should expose buyer value"
        assert proof_sample["next_steps"]["sample_report_api"].endswith(
            "/v1/proof-pack/reports/ppr_sample_source_trust"
        ), "Proof Pack sample should expose concrete retained report API"
        assert proof_sample["next_steps"]["sample_verify_api"].endswith(
            "/v1/proof-pack/reports/ppr_sample_source_trust/verify"
        ), "Proof Pack sample should expose concrete verify receipt API"

        sample_report_api = await client.get("/v1/proof-pack/reports/ppr_sample_source_trust")
        assert sample_report_api.status_code == 200, "sample retained report API should return"
        sample_report_json = sample_report_api.json()
        assert sample_report_json["status"] == "sample_report", "sample retained report returned wrong status"
        assert sample_report_json["sample"] is True, "sample retained report should be marked as sample"
        assert sample_report_json["supplier_spend"] is False, "sample retained report should not spend supplier budget"
        assert sample_report_json["report_id"] == "ppr_sample_source_trust", "sample retained report id mismatch"
        assert sample_report_json["result_hash"], "sample retained report should expose result hash"
        assert sample_report_json["source_hash"], "sample retained report should expose source hash"
        assert sample_report_json["verify_url"].endswith("/ppr_sample_source_trust/verify"), (
            "sample retained report should expose verify receipt URL"
        )
        sample_report_page = await client.get("/proof-pack/reports/ppr_sample_source_trust")
        assert sample_report_page.status_code == 200, "sample retained report page should render"
        assert "Proof Pack Report" in sample_report_page.text, "sample retained report page should identify report"
        assert "/v1/proof-pack/reports/ppr_sample_source_trust/verify" in sample_report_page.text, (
            "sample report page should expose verify receipt API"
        )
        sample_verify = await client.get("/v1/proof-pack/reports/ppr_sample_source_trust/verify?source=ci-sample")
        assert sample_verify.status_code == 200, "sample retained report verify receipt should succeed"
        sample_verify_json = sample_verify.json()
        assert sample_verify_json["status"] == "verified", "sample verify receipt returned wrong status"
        assert sample_verify_json["supplier_spend"] is False, "sample verify receipt should not spend"
        assert sample_verify_json["payment_required"] is False, "sample verify receipt should not require payment"
        assert sample_verify_json["result_hash"] == sample_report_json["result_hash"], (
            "sample verify receipt hash mismatch"
        )
        assert sample_verify_json["source_hash"] == sample_report_json["source_hash"], (
            "sample verify receipt source hash mismatch"
        )
        assert sample_verify_json["checks"]["result_hash_matches_canonical_payload"] is True, (
            "sample verify receipt should recompute matching result hash"
        )
        assert sample_verify_json["checks"]["retained"] is True, "sample verify receipt should show retained report"
        assert sample_verify_json["citation_count"] >= 1, "sample verify receipt should expose citation count"
        sample_follow_up = await client.post(
            "/v1/proof-pack/reports/ppr_sample_source_trust/follow-up",
            json={"question": "Can my agent cite reserved domains?"},
        )
        assert sample_follow_up.status_code == 200, "sample retained report follow-up should succeed"
        sample_follow_up_json = sample_follow_up.json()
        assert sample_follow_up_json["status"] == "follow_up", "sample report follow-up returned wrong status"
        assert sample_follow_up_json["supplier_spend"] is False, "sample report follow-up should not spend"
        assert sample_follow_up_json["citations"], "sample report follow-up should reuse sample citations"
        sample_refresh = await client.post("/v1/proof-pack/reports/ppr_sample_source_trust/refresh?source=ci-sample")
        assert sample_refresh.status_code == 200, "sample retained report refresh quote should succeed"
        sample_refresh_json = sample_refresh.json()
        assert sample_refresh_json["status"] == "refresh_quote", "sample report refresh quote returned wrong status"
        assert sample_refresh_json["amount_units"] == "100000", "quick sample report refresh quote amount mismatch"
        assert sample_refresh_json["next_steps"]["compare_result_hash"] == sample_report_json["result_hash"], (
            "sample refresh quote should compare against sample report hash"
        )

        proof_probe = await client.get("/v1/x402/proof-pack?pack=standard")
        assert proof_probe.status_code == 402, f"Proof Pack probe returned {proof_probe.status_code}"
        assert proof_probe.headers.get("X-AxonGate-Agent-Diagnostic", "").endswith("/v1/agent/diagnose"), (
            "Proof Pack probe missing agent diagnostic header"
        )
        assert proof_probe.headers.get("X-AxonGate-Agent-Trust", "").endswith("/v1/agent/trust"), (
            "Proof Pack probe missing agent trust header"
        )
        assert proof_probe.headers.get("X-AxonGate-Checkout-Confidence", "").endswith("/checkout/confidence"), (
            "Proof Pack probe missing checkout confidence header"
        )
        assert proof_probe.headers.get("X-AxonGate-Proof-Pack-Benchmarks", "").endswith("/v1/proof-pack/benchmarks"), (
            "Proof Pack probe missing benchmark header"
        )
        assert proof_probe.headers.get("X-AxonGate-Proof-Pack-Verify", "").endswith(
            "/v1/proof-pack/reports/ppr_sample_source_trust/verify"
        ), "Proof Pack probe missing verify receipt header"
        assert proof_probe.json()["detail"]["links"]["agent_diagnostic"].endswith("/v1/agent/diagnose"), (
            "Proof Pack probe body missing agent diagnostic"
        )
        assert proof_probe.json()["detail"]["links"]["agent_trust"].endswith("/v1/agent/trust"), (
            "Proof Pack probe body missing agent trust"
        )
        assert proof_probe.json()["detail"]["links"]["checkout_confidence"].endswith("product=proof_pack&pack=standard"), (
            "Proof Pack probe body missing checkout confidence"
        )
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
        assert unpaid_proof_post.headers.get("X-AxonGate-Agent-Diagnostic", "").endswith("/v1/agent/diagnose"), (
            "unpaid Proof Pack POST missing agent diagnostic header"
        )
        assert unpaid_proof_post.headers.get("X-AxonGate-Agent-Trust", "").endswith("/v1/agent/trust"), (
            "unpaid Proof Pack POST missing agent trust header"
        )
        assert unpaid_proof_post.headers.get("X-AxonGate-Checkout-Confidence", "").endswith("/checkout/confidence"), (
            "unpaid Proof Pack POST missing checkout confidence header"
        )
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

        report_markdown = "\n".join(
            [
                "# Source trust notes",
                "",
                "- AxonGate Proof Packs return cited claims for agent builders evaluating public sources.",
                "- AxonGate reports include source hashes so returning agents can compare repeat runs.",
                "- AxonGate retained reports expose follow-up URLs for no-spend reuse of stored citations.",
                "- AxonGate refresh quotes give agents a paid rerun path when source recency matters.",
            ]
        )
        proof_report_content = await gateway.generate_proof_pack_content(
            target_url="https://example.com/source",
            question="Which AxonGate report fields can my agent cite?",
            pack="deep",
            markdown=report_markdown,
            cache_hit=False,
        )
        public_report_citations = [
            {key: value for key, value in citation.items() if key != "fingerprint"}
            for citation in proof_report_content["citations"]
        ]
        stored_report = await gateway.store_proof_pack_report(
            {
                "status": "success",
                "target_url": "https://example.com/source",
                "question": "Which AxonGate report fields can my agent cite?",
                "pack": "deep",
                "answer": proof_report_content["answer"],
                "executive_summary": proof_report_content["executive_summary"],
                "decision": proof_report_content["decision"],
                "confidence_score": proof_report_content["confidence_score"],
                "source_quality_score": proof_report_content["source_quality_score"],
                "agent_action": proof_report_content["agent_action"],
                "recommended_next_call": gateway.recommended_next_call(
                    "https://example.com/source",
                    "Which AxonGate report fields can my agent cite?",
                    "deep",
                    "ci",
                    proof_report_content["agent_action"],
                ),
                "key_claims": proof_report_content["key_claims"],
                "supported_findings": proof_report_content["supported_findings"],
                "gaps": proof_report_content["gaps"],
                "citation_coverage": proof_report_content["citation_coverage"],
                "citations": public_report_citations,
                "risks": proof_report_content["risks"],
                "source_profile": proof_report_content["source_profile"],
                "cache": {"hit": False},
                "llm_used": proof_report_content["llm_used"],
                "llm_model": proof_report_content["llm_model"],
                "fallback_reason": proof_report_content["fallback_reason"],
                "payment": {
                    "mode": "ci",
                    "amount_usdc": float(gateway.price_for_proof_pack("deep")),
                    "source": "ci",
                },
                "ueg_receipt": {"status": "ci"},
            },
            report_id="ppr_ci_smoke_report",
        )
        assert stored_report["report_id"] == "ppr_ci_smoke_report", "stored report should keep normalized id"
        assert stored_report["report_url"].endswith("/v1/proof-pack/reports/ppr_ci_smoke_report"), (
            "stored report should expose reusable JSON URL"
        )
        assert stored_report["verify_url"].endswith("/v1/proof-pack/reports/ppr_ci_smoke_report/verify"), (
            "stored report should expose verify receipt URL"
        )
        assert stored_report["recommended_next_call"]["endpoint"].endswith("/ppr_ci_smoke_report"), (
            "stored report next call should use the concrete report endpoint"
        )
        assert stored_report["result_hash"], "stored report should expose a result hash"
        stored_report_api = await client.get(f"/v1/proof-pack/reports/{stored_report['report_id']}")
        assert stored_report_api.status_code == 200, "stored report API should return retained report"
        stored_report_json = stored_report_api.json()
        assert stored_report_json["result_hash"] == stored_report["result_hash"], "stored report API hash mismatch"
        stored_report_page = await client.get(f"/proof-pack/reports/{stored_report['report_id']}")
        assert stored_report_page.status_code == 200, "stored report HTML page should render"
        assert "Proof Pack Report" in stored_report_page.text, "stored report page should identify the report"
        assert f"/v1/proof-pack/reports/{stored_report['report_id']}/verify" in stored_report_page.text, (
            "stored report page should expose verify receipt API"
        )
        stored_verify = await client.get(f"/v1/proof-pack/reports/{stored_report['report_id']}/verify?source=ci-report")
        assert stored_verify.status_code == 200, "stored report verify receipt should succeed"
        stored_verify_json = stored_verify.json()
        assert stored_verify_json["status"] == "verified", "stored report verify receipt returned wrong status"
        assert stored_verify_json["supplier_spend"] is False, "stored report verify receipt should not spend"
        assert stored_verify_json["payment_required"] is False, "stored report verify receipt should not require payment"
        assert stored_verify_json["result_hash"] == stored_report["result_hash"], "stored verify receipt hash mismatch"
        assert stored_verify_json["source_hash"] == stored_report["source_hash"], "stored verify receipt source mismatch"
        assert stored_verify_json["checks"]["result_hash_matches_canonical_payload"] is True, (
            "stored verify receipt should recompute matching result hash"
        )
        assert stored_verify_json["checks"]["retained"] is True, "stored verify receipt should show retained report"
        assert stored_verify_json["citation_count"] >= 1, "stored verify receipt should expose citations"
        report_follow_up = await client.post(
            f"/v1/proof-pack/reports/{stored_report['report_id']}/follow-up",
            json={"question": "Can my agent cite the source hash and refresh URL?"},
        )
        assert report_follow_up.status_code == 200, "stored report follow-up should succeed"
        follow_up_json = report_follow_up.json()
        assert follow_up_json["status"] == "follow_up", "stored report follow-up returned wrong status"
        assert follow_up_json["supplier_spend"] is False, "stored report follow-up should not spend supplier budget"
        assert follow_up_json["citations"], "stored report follow-up should reuse retained citations"
        assert follow_up_json["recommended_next_call"]["endpoint"].endswith("/refresh"), (
            "stored report follow-up should expose refresh path"
        )
        report_refresh = await client.post(
            f"/v1/proof-pack/reports/{stored_report['report_id']}/refresh?source=ci-refresh"
        )
        assert report_refresh.status_code == 200, "stored report refresh quote should succeed without a body"
        refresh_json = report_refresh.json()
        assert refresh_json["status"] == "refresh_quote", "stored report refresh quote returned wrong status"
        assert refresh_json["amount_units"] == "1000000", "deep stored report refresh amount mismatch"
        assert refresh_json["next_steps"]["compare_result_hash"] == stored_report["result_hash"], (
            "refresh quote should tell agents which result hash to compare"
        )

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
        assert unpaid_post.headers.get("X-AxonGate-Agent-Diagnostic", "").endswith("/v1/agent/diagnose"), (
            "unpaid POST missing agent diagnostic header"
        )
        assert unpaid_post.headers.get("X-AxonGate-Agent-Trust", "").endswith("/v1/agent/trust"), (
            "unpaid POST missing agent trust header"
        )
        assert unpaid_post.headers.get("X-AxonGate-Checkout-Confidence", "").endswith("/checkout/confidence"), (
            "unpaid POST missing checkout confidence header"
        )

        x402_discovery = (await client.get("/.well-known/x402")).json()
        assert "extensions" in x402_discovery, "public x402 discovery missing protocol extensions"
        assert "metadata" in x402_discovery, "public x402 discovery should keep non-protocol metadata"
        assert x402_discovery["metadata"].get("agentDiagnostic", "").endswith("/v1/agent/diagnose"), (
            "public x402 discovery missing agent diagnostic"
        )
        assert x402_discovery["metadata"].get("agentTrust", "").endswith("/v1/agent/trust"), (
            "public x402 discovery missing agent trust"
        )
        assert x402_discovery["metadata"].get("checkoutConfidence", "").endswith("/checkout/confidence"), (
            "public x402 discovery missing checkout confidence"
        )
        assert x402_discovery["metadata"].get("checkoutConfidenceApi", "").endswith("/v1/checkout/confidence"), (
            "public x402 discovery missing checkout confidence API"
        )
        assert x402_discovery["metadata"].get("proofPackBenchmarks", "").endswith("/v1/proof-pack/benchmarks"), (
            "public x402 discovery missing Proof Pack benchmarks"
        )
        assert "cached" in x402_discovery["metadata"]["tiers"], "cached tier missing from public discovery"
        assert "proofPacks" in x402_discovery["metadata"], "Proof Pack pricing missing from public discovery"
        assert "proofPackSampleApi" in x402_discovery["metadata"], "Proof Pack sample missing from public discovery"
        assert "proofPackPreview" in x402_discovery["metadata"], "Proof Pack preview page missing from public discovery"
        assert "proofPackPreviewApi" in x402_discovery["metadata"], "Proof Pack preview API missing from public discovery"
        assert "proofPackLibrary" in x402_discovery["metadata"], "Evidence Library missing from public discovery"
        assert "proofPackSharePattern" in x402_discovery["metadata"], "share pattern missing from public discovery"
        assert "sourceLandingPattern" in x402_discovery["metadata"], "source landing pattern missing from public discovery"
        assert "proofPackQuote" in x402_discovery["metadata"], "Proof Pack quote page missing from public discovery"
        assert "proofPackRequest" in x402_discovery["metadata"], "Proof Pack request page missing from public discovery"
        assert "proofPackLeadApi" in x402_discovery["metadata"], "Proof Pack lead API missing from public discovery"
        assert "proofPackReportApiPattern" in x402_discovery["metadata"], "Proof Pack report API missing from public discovery"
        assert "proofPackVerifyApiPattern" in x402_discovery["metadata"], "Proof Pack verify API missing from public discovery"
        assert "proofPackFollowUpApiPattern" in x402_discovery["metadata"], "Proof Pack follow-up API missing from public discovery"
        assert "proofPackRefreshQuoteApiPattern" in x402_discovery["metadata"], "Proof Pack refresh quote API missing from public discovery"
        assert x402_discovery["metadata"]["proofPackSampleReportApi"].endswith(
            "/v1/proof-pack/reports/ppr_sample_source_trust"
        ), "Proof Pack sample report missing from public discovery"
        assert x402_discovery["metadata"]["proofPackSampleVerifyApi"].endswith(
            "/ppr_sample_source_trust/verify"
        ), "Proof Pack sample verify receipt missing from public discovery"
        assert x402_discovery["metadata"]["proofPackSampleFollowUpApi"].endswith(
            "/ppr_sample_source_trust/follow-up"
        ), "Proof Pack sample follow-up missing from public discovery"
        assert "proofBundleQuote" in x402_discovery["metadata"], "Proof Bundle quote missing from public discovery"
        assert "proofBundles" in x402_discovery["metadata"], "Proof Bundle pricing missing from public discovery"
        assert "operatorOrders" not in x402_discovery["metadata"], "Operator orders should stay out of public discovery"
        assert "stripeWebhook" not in x402_discovery["metadata"], "Stripe webhook should stay out of public discovery"

        openapi = (await client.get("/openapi.json")).json()
        schemas = openapi.get("components", {}).get("schemas", {})
        assert schemas.get("AccessRequest", {}).get("examples"), "AccessRequest examples missing"
        assert schemas.get("ProofPackRequest", {}).get("examples"), "ProofPackRequest examples missing"
        assert schemas.get("ProofPackLeadRequest", {}).get("examples"), "ProofPackLeadRequest examples missing"
        assert schemas.get("ProofBundleLeadRequest", {}).get("examples"), "ProofBundleLeadRequest examples missing"
        assert schemas.get("ContactRequest", {}).get("examples"), "ContactRequest examples missing"
        post_operation = openapi.get("paths", {}).get("/v1/x402/access", {}).get("post", {})
        assert post_operation.get("x-payment-info"), "OpenAPI payment extension missing from paid endpoint"
        assert post_operation["x-payment-info"].get("agentDiagnostic", "").endswith("/v1/agent/diagnose"), (
            "OpenAPI paid endpoint missing agent diagnostic"
        )
        assert post_operation["x-payment-info"].get("agentTrust", "").endswith("/v1/agent/trust"), (
            "OpenAPI paid endpoint missing agent trust"
        )
        assert post_operation["x-payment-info"].get("checkoutConfidence", "").endswith("/checkout/confidence"), (
            "OpenAPI paid endpoint missing checkout confidence"
        )
        assert "X-AxonGate-Agent-Diagnostic" in post_operation.get("responses", {}).get("402", {}).get("headers", {}), (
            "OpenAPI paid endpoint missing agent diagnostic header docs"
        )
        assert "X-AxonGate-Agent-Trust" in post_operation.get("responses", {}).get("402", {}).get("headers", {}), (
            "OpenAPI paid endpoint missing agent trust header docs"
        )
        assert "X-AxonGate-Checkout-Confidence" in post_operation.get("responses", {}).get("402", {}).get("headers", {}), (
            "OpenAPI paid endpoint missing checkout confidence header docs"
        )
        assert "X-AxonGate-Proof-Pack-Verify" in post_operation.get("responses", {}).get("402", {}).get("headers", {}), (
            "OpenAPI paid endpoint missing Proof Pack verify header docs"
        )
        proof_operation = openapi.get("paths", {}).get("/v1/x402/proof-pack", {}).get("post", {})
        assert proof_operation.get("x-payment-info"), "OpenAPI payment extension missing from Proof Pack endpoint"
        assert "X-AxonGate-Agent-Diagnostic" in proof_operation.get("responses", {}).get("402", {}).get("headers", {}), (
            "OpenAPI Proof Pack endpoint missing agent diagnostic header docs"
        )
        assert "X-AxonGate-Agent-Trust" in proof_operation.get("responses", {}).get("402", {}).get("headers", {}), (
            "OpenAPI Proof Pack endpoint missing agent trust header docs"
        )
        assert "X-AxonGate-Checkout-Confidence" in proof_operation.get("responses", {}).get("402", {}).get("headers", {}), (
            "OpenAPI Proof Pack endpoint missing checkout confidence header docs"
        )
        assert "X-AxonGate-Proof-Pack-Verify" in proof_operation.get("responses", {}).get("402", {}).get("headers", {}), (
            "OpenAPI Proof Pack endpoint missing verify header docs"
        )
        contact_operation = openapi.get("paths", {}).get("/v1/contact", {}).get("post", {})
        assert contact_operation, "OpenAPI contact endpoint missing"
        openapi_paths = openapi.get("paths", {})
        assert "/v1/operator/leads" not in openapi_paths, "OpenAPI should not advertise private lead API"
        assert "/v1/operator/orders" not in openapi_paths, "OpenAPI should not advertise private order API"
        assert "/operator/paid-requests" not in openapi_paths, (
            "OpenAPI should not advertise private paid request page"
        )
        assert "/v1/operator/paid-requests" not in openapi_paths, (
            "OpenAPI should not advertise private paid request diagnostics"
        )
        assert "/v1/operator/orders/{lead_id}/resend-email" not in openapi_paths, (
            "OpenAPI should not advertise private order email API"
        )
        assert "/v1/stripe/webhook" not in openapi_paths, "OpenAPI should not advertise private Stripe webhook"
        assert openapi.get("x-payment-info"), "OpenAPI root payment extension missing"
        assert openapi["x-payment-info"].get("agentDiagnostic"), "OpenAPI payment info missing agent diagnostic"
        assert openapi["x-payment-info"].get("agentTrust"), "OpenAPI payment info missing agent trust"
        assert openapi["x-payment-info"].get("proofPackBenchmarks"), "OpenAPI payment info missing Proof Pack benchmarks"
        assert openapi["x-payment-info"].get("proofPackLibrary"), "OpenAPI payment info missing Evidence Library"
        assert openapi["x-payment-info"].get("sourceLandingPattern"), "OpenAPI payment info missing source landing pattern"
        assert openapi["x-payment-info"].get("proofPackReportApiPattern"), "OpenAPI payment info missing Proof Pack report pattern"
        assert openapi["x-payment-info"].get("proofPackVerifyApiPattern"), (
            "OpenAPI payment info missing Proof Pack verify pattern"
        )

        contact_payload = {
            "name": "CI Builder",
            "email": "contact-ci@example.invalid",
            "company": "CI Labs",
            "use_case": "SEO and buyer inquiry smoke test",
            "message": "We want to evaluate whether AxonGate can support a launch workflow.",
            "source": "ci-contact",
        }
        contact_submit = await client.post(
            "/contact",
            content=urlencode(contact_payload),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert contact_submit.status_code == 200, f"Contact form returned {contact_submit.status_code}"
        assert "Message received" in contact_submit.text, "Contact form should acknowledge submit"
        contact_api = await client.post("/v1/contact", json={**contact_payload, "email": "contact-api@example.invalid"})
        assert contact_api.status_code == 200, f"Contact API returned {contact_api.status_code}"
        contact_api_json = contact_api.json()
        assert contact_api_json["status"] == "received", "Contact API should acknowledge submit"
        assert "message" not in contact_api_json, "Contact API response must not echo private message"

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
        assert "paid_request_events" in metrics, "Paid request event storage snapshot missing from metrics"
        assert metrics["paid_request_events"]["count"] >= 1, "Paid request event snapshot should count retained records"
        assert metrics["paid_request_events"]["latest"]["target_domain_hash"], (
            "Paid request event public snapshot should expose only target hash"
        )
        assert "proof_pack_reports" in metrics, "Proof Pack report storage snapshot missing from metrics"
        assert metrics["proof_pack_reports"]["count"] >= 1, "Proof Pack report snapshot should count retained reports"
        assert metrics["proof_pack_reports"]["latest"]["target_domain_hash"], (
            "Proof Pack report public snapshot should hash target domains"
        )
        assert metrics["metrics"].get("proof_pack_reports_total", 0) >= 1, "Proof Pack report storage should be counted"
        assert metrics["metrics"].get("proof_pack_report_reads_total", 0) >= 2, "Proof Pack report reads should be counted"
        assert metrics["metrics"].get("proof_pack_report_verifications_total", 0) >= 2, (
            "Proof Pack report verifications should be counted"
        )
        assert metrics["conversion_funnel"].get("proof_pack_report_verifications", 0) >= 2, (
            "Proof Pack report verifications missing from funnel"
        )
        assert metrics["metrics"].get("proof_pack_followups_total", 0) >= 1, "Proof Pack follow-ups should be counted"
        assert metrics["metrics"].get("proof_pack_refresh_quotes_total", 0) >= 1, "Proof Pack refresh quotes should be counted"
        assert metrics["metrics"].get("discovery_operator_paid_requests_hits_total", 0) >= 2, (
            "Operator paid request page/API visits should be counted"
        )
        assert metrics["metrics"].get("proof_pack_previews_total", 0) >= 3, "Proof Pack previews should be counted"
        assert metrics["conversion_funnel"].get("proof_pack_previews", 0) >= 3, "Proof Pack previews missing from funnel"
        assert metrics["metrics"].get("proof_pack_leads_total", 0) >= 2, "Proof Pack leads should be counted"
        assert metrics["conversion_funnel"].get("proof_pack_leads", 0) >= 2, "Proof Pack leads missing from funnel"
        assert metrics["metrics"].get("agent_diagnostics_total", 0) >= 2, "Agent diagnostics should be counted"
        assert metrics["conversion_funnel"].get("agent_diagnostics", 0) >= 2, "Agent diagnostics missing from funnel"
        assert metrics["metrics"].get("agent_trust_checks_total", 0) >= 2, "Agent trust checks should be counted"
        assert metrics["conversion_funnel"].get("agent_trust_checks", 0) >= 2, "Agent trust checks missing from funnel"
        assert metrics["metrics"].get("proof_pack_benchmarks_total", 0) >= 2, "Proof Pack benchmarks should be counted"
        assert metrics["conversion_funnel"].get("proof_pack_benchmarks", 0) >= 2, (
            "Proof Pack benchmarks missing from funnel"
        )
        assert metrics["metrics"].get("checkout_confidence_views_total", 0) >= 3, (
            "Checkout confidence views should be counted"
        )
        assert metrics["metrics"].get("checkout_confidence_api_hits_total", 0) >= 2, (
            "Checkout confidence API hits should be counted"
        )
        assert metrics["conversion_funnel"].get("checkout_confidence_views", 0) >= 3, (
            "Checkout confidence views missing from funnel"
        )
        assert metrics["metrics"].get("quote_receipts_total", 0) >= 2, "quote receipts should be counted"
        assert metrics["metrics"].get("quote_receipt_reads_total", 0) >= 4, "quote receipt reads should be counted"
        assert metrics["metrics"].get("quote_resume_checkout_hits_total", 0) >= 2, (
            "quote resume checkout hits should be counted"
        )
        assert metrics["conversion_funnel"].get("quote_receipts", 0) >= 2, "quote receipts missing from funnel"
        assert metrics["conversion_funnel"].get("quote_receipt_reads", 0) >= 4, (
            "quote receipt reads missing from funnel"
        )
        assert "quote_receipts" in metrics, "quote receipt storage snapshot missing from metrics"
        assert metrics["quote_receipts"]["count"] >= 2, "quote receipt snapshot should count retained quotes"
        assert metrics["quote_receipts"]["latest"]["quote_id"].startswith("qr_"), (
            "quote receipt snapshot should expose latest quote id"
        )
        assert metrics["metrics"].get("contact_form_submits_total", 0) >= 2, "Contact submissions should be counted"
        assert metrics["conversion_funnel"].get("contact_form_submits", 0) >= 2, "Contact submissions missing from funnel"
        assert metrics["metrics"].get("proof_bundle_quotes_total", 0) >= 2, "Proof Bundle quotes should be counted"
        assert metrics["conversion_funnel"].get("proof_bundle_quotes", 0) >= 2, "Proof Bundle quotes missing from funnel"
        assert metrics["metrics"].get("proof_bundle_checkout_reviews_total", 0) >= 1, (
            "Proof Bundle checkout reviews should be counted"
        )
        assert metrics["conversion_funnel"].get("proof_bundle_checkout_reviews", 0) >= 1, (
            "Proof Bundle checkout reviews missing from funnel"
        )
        assert metrics["metrics"].get("proof_bundle_leads_total", 0) >= 1, "Proof Bundle leads should be counted"
        assert metrics["conversion_funnel"].get("proof_bundle_leads", 0) >= 1, "Proof Bundle leads missing from funnel"
        assert metrics["metrics"].get("proof_bundle_paid_total", 0) >= 2, "Proof Bundle paid updates should be counted"
        assert metrics["conversion_funnel"].get("proof_bundle_paid", 0) >= 2, "Proof Bundle paid missing from funnel"
        assert metrics["metrics"].get("proof_bundle_fulfilled_total", 0) >= 1, (
            "Proof Bundle fulfilled updates should be counted"
        )
        assert metrics["metrics"].get("proof_bundle_recovery_requests_total", 0) >= 3, (
            "Proof Bundle recovery requests should be counted"
        )
        assert metrics["conversion_funnel"].get("proof_bundle_recovery_requests", 0) >= 3, (
            "Proof Bundle recovery requests missing from funnel"
        )
        assert metrics["metrics"].get("proof_bundle_auto_fulfillment_success_total", 0) >= 1, (
            "Proof Bundle auto fulfillment should be counted"
        )
        assert metrics["metrics"].get("proof_bundle_delivery_email_success_total", 0) >= 1, (
            "Proof Bundle delivery email success should be counted"
        )
        assert metrics["metrics"].get("stripe_webhook_payment_succeeded_total", 0) >= 1, (
            "Stripe webhook success should be counted"
        )
        assert metrics["metrics"].get("stripe_webhook_duplicate_events_total", 0) >= 1, (
            "Stripe duplicate event should be counted"
        )
        assert metrics["metrics"].get("stripe_webhook_signature_failures_total", 0) >= 1, (
            "Stripe signature failures should be counted"
        )
        assert "proof_bundle_pricing" in metrics, "Proof Bundle pricing missing from metrics"
        assert "proof_pack_leads" in metrics, "Proof Pack lead storage snapshot missing from metrics"
        assert metrics["email_delivery"]["enabled"] is True, "email delivery should be enabled in CI"
        assert metrics["email_delivery"]["resend_api_key_configured"] is True, "email delivery key should be configured in CI"
        assert "last_error" in metrics["email_delivery"], "email delivery should expose last sanitized error"
        assert metrics["email_delivery"]["last_error"] == "", "successful CI email should clear last email error"
        assert metrics["stripe"]["webhook_enabled"] is True, "Stripe webhook should be enabled in CI"
        assert "webhook_endpoint" not in metrics["stripe"], "Metrics should not publish private Stripe webhook URL"
        assert metrics["operator"]["private_leads_enabled"] is True, "operator private leads should be enabled in CI"
        assert "operator_auth_failures_total" in metrics["metrics"], "operator auth failures metric missing"
        assert metrics["metrics"].get("legacy_access_health_hits_total", 0) >= 2, (
            "legacy access health compatibility should be counted"
        )
        assert metrics["metrics"].get("legacy_access_probe_challenges_total", 0) >= 1, (
            "legacy access GET probes should be counted"
        )
        assert metrics["metrics"].get("crawler_guard_no_user_agent_total", 0) >= 2, (
            "blank-UA private crawler suppression should be counted"
        )
        assert metrics["metrics"].get("discovery_alias_hits_total", 0) >= 2, (
            "discovery alias hits should be counted"
        )
        assert metrics["metrics"].get("favicon_hits_total", 0) >= 1, "favicon probes should be counted"
        assert metrics["metrics"].get("discovery_evidence_library_hits_total", 0) >= 2, (
            "Evidence Library hits should be counted"
        )
        assert metrics["metrics"].get("discovery_share_page_hits_total", 0) >= 1, (
            "share page hits should be counted"
        )
        assert metrics["metrics"].get("discovery_source_landing_hits_total", 0) >= 1, (
            "source landing hits should be counted"
        )


if __name__ == "__main__":
    asyncio.run(main())
