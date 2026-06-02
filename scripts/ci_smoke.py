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
            "/proof-pack/preview",
            "/proof-pack/quote",
            "/proof-pack/request",
            "/proof-pack/bundle",
            "/proof-pack/bundle/quote",
            "/v1/proof-pack/sample",
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
        assert proof_quote["next_steps"]["proof_pack_sample_api"].endswith(
            "/v1/proof-pack/sample"
        ), "Proof Pack quote should point to sample JSON"
        assert "proof_pack_preview_page" in proof_quote["next_steps"], "Proof Pack quote should point to mini preview"
        assert proof_quote["next_steps"]["proof_pack_quote_page"].endswith(
            "/proof-pack/quote"
        ), "Proof Pack quote should point to the human quote page"

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
        assert "POST /v1/x402/proof-pack" in proof_quote_page.text, "Proof Pack quote page missing short endpoint"
        assert "Full Paid Endpoint" in proof_quote_page.text, "Proof Pack quote page should keep full URL in a scroll-safe block"

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

        proof_bundle_page = await client.get(f"/proof-pack/bundle/quote?{bundle_query}")
        assert proof_bundle_page.status_code == 200, "Proof Bundle quote page should render"
        assert "Proof Bundle Quote" in proof_bundle_page.text, "Proof Bundle quote page missing heading"
        assert "Request Bundle" in proof_bundle_page.text, "Proof Bundle quote page missing request CTA"
        assert "Checkout" in proof_bundle_page.text, "Proof Bundle quote page missing checkout CTA"
        assert "POST https://api.axongate.one/v1/x402/proof-pack?pack=standard" in proof_bundle_page.text, (
            "Proof Bundle quote should keep immediate x402 fallback in a scroll-safe block"
        )
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

        operator_leads_unauthorized = await client.get("/operator/leads")
        assert operator_leads_unauthorized.status_code == 401, "operator leads should require a token"

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
        assert sent_delivery_emails, "Stripe delivery should send a customer report email when configured"
        assert sent_delivery_emails[-1]["to"] == ["stripe-buyer@example.invalid"], "Delivery email recipient mismatch"
        assert "Open report" in sent_delivery_emails[-1]["html"], "Delivery email should include report link"

        stripe_delivery_page = await client.get(
            "/proof-pack/bundle/delivery?session_id=cs_ci_axongate_paid"
        )
        assert stripe_delivery_page.status_code == 200, "Stripe delivery page should render"
        assert "Proof Bundle Delivery" in stripe_delivery_page.text, "Stripe delivery page missing heading"

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
        assert "proofPackPreview" in x402_discovery["metadata"], "Proof Pack preview page missing from public discovery"
        assert "proofPackPreviewApi" in x402_discovery["metadata"], "Proof Pack preview API missing from public discovery"
        assert "proofPackQuote" in x402_discovery["metadata"], "Proof Pack quote page missing from public discovery"
        assert "proofPackRequest" in x402_discovery["metadata"], "Proof Pack request page missing from public discovery"
        assert "proofPackLeadApi" in x402_discovery["metadata"], "Proof Pack lead API missing from public discovery"
        assert "proofBundleQuote" in x402_discovery["metadata"], "Proof Bundle quote missing from public discovery"
        assert "proofBundles" in x402_discovery["metadata"], "Proof Bundle pricing missing from public discovery"

        openapi = (await client.get("/openapi.json")).json()
        schemas = openapi.get("components", {}).get("schemas", {})
        assert schemas.get("AccessRequest", {}).get("examples"), "AccessRequest examples missing"
        assert schemas.get("ProofPackRequest", {}).get("examples"), "ProofPackRequest examples missing"
        assert schemas.get("ProofPackLeadRequest", {}).get("examples"), "ProofPackLeadRequest examples missing"
        assert schemas.get("ProofBundleLeadRequest", {}).get("examples"), "ProofBundleLeadRequest examples missing"
        post_operation = openapi.get("paths", {}).get("/v1/x402/access", {}).get("post", {})
        assert post_operation.get("x-payment-info"), "OpenAPI payment extension missing from paid endpoint"
        proof_operation = openapi.get("paths", {}).get("/v1/x402/proof-pack", {}).get("post", {})
        assert proof_operation.get("x-payment-info"), "OpenAPI payment extension missing from Proof Pack endpoint"
        operator_operation = openapi.get("paths", {}).get("/v1/operator/leads", {}).get("get", {})
        assert operator_operation.get("security"), "OpenAPI operator leads security missing"
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
        assert metrics["metrics"].get("proof_pack_previews_total", 0) >= 3, "Proof Pack previews should be counted"
        assert metrics["conversion_funnel"].get("proof_pack_previews", 0) >= 3, "Proof Pack previews missing from funnel"
        assert metrics["metrics"].get("proof_pack_leads_total", 0) >= 2, "Proof Pack leads should be counted"
        assert metrics["conversion_funnel"].get("proof_pack_leads", 0) >= 2, "Proof Pack leads missing from funnel"
        assert metrics["metrics"].get("proof_bundle_quotes_total", 0) >= 2, "Proof Bundle quotes should be counted"
        assert metrics["conversion_funnel"].get("proof_bundle_quotes", 0) >= 2, "Proof Bundle quotes missing from funnel"
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
        assert metrics["stripe"]["webhook_enabled"] is True, "Stripe webhook should be enabled in CI"
        assert metrics["operator"]["private_leads_enabled"] is True, "operator private leads should be enabled in CI"
        assert "operator_auth_failures_total" in metrics["metrics"], "operator auth failures metric missing"


if __name__ == "__main__":
    asyncio.run(main())
