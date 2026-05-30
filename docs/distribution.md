# AxonGate Distribution Checklist

Canonical base URL:

```text
https://api.axongate.one
```

## Source-Tagged URLs

Use source tags on submitted endpoint URLs so `/metrics.rolling_attribution`
and `/metrics.attribution` can show which directory creates discovery hits,
payment challenges, paid attempts, accepted payments, and deliveries.

| Source | Paid endpoint | Docs URL | Paid test URL |
| --- | --- | --- | --- |
| `x402-list` | `https://api.axongate.one/from/x402-list/v1/x402/starter` | `https://api.axongate.one/docs?source=x402-list` | `https://api.axongate.one/paid-test?source=x402-list` |
| `payanagent-starter` | `https://api.axongate.one/v1/x402/access?tier=starter&source=payanagent-starter` | `https://api.axongate.one/docs?source=payanagent-starter` | `https://api.axongate.one/paid-test?source=payanagent-starter` |
| `payanagent` | `https://api.axongate.one/v1/x402/access?tier=fresh&source=payanagent` | `https://api.axongate.one/docs?source=payanagent` | `https://api.axongate.one/paid-test?source=payanagent` |
| `payanagent-proof` | `https://api.axongate.one/v1/x402/proof-pack?pack=standard&source=payanagent` | `https://api.axongate.one/proof-pack?source=payanagent` | `https://api.axongate.one/v1/proof-pack/quote?target_url=https%3A%2F%2Fexample.com&pack=standard&source=payanagent` |
| `payanagent-bundle` | `https://api.axongate.one/v1/proof-pack/bundle/quote?target_urls=https%3A%2F%2Fwww.iana.org%2Fdomains%2Freserved%0Ahttps%3A%2F%2Fexample.com&bundle=builder&source=payanagent` | `https://api.axongate.one/proof-pack/bundle?source=payanagent` | `https://api.axongate.one/proof-pack/bundle/quote?target_urls=https%3A%2F%2Fwww.iana.org%2Fdomains%2Freserved%0Ahttps%3A%2F%2Fexample.com&bundle=scout&source=payanagent` |
| `agora402` | `https://api.axongate.one/v1/x402/access?tier=fresh&source=agora402` | `https://api.axongate.one/docs?source=agora402` | `https://api.axongate.one/paid-test?source=agora402` |
| `agent-bazaar` | `https://api.axongate.one/v1/x402/access?tier=fresh&source=agent-bazaar` | `https://api.axongate.one/docs?source=agent-bazaar` | `https://api.axongate.one/paid-test?source=agent-bazaar` |
| `the402` | `https://api.axongate.one/v1/x402/access?tier=fresh&source=the402` | `https://api.axongate.one/docs?source=the402` | `https://api.axongate.one/paid-test?source=the402` |
| `x402-eco` | `https://api.axongate.one/v1/x402/access?tier=fresh&source=x402-eco` | `https://api.axongate.one/docs?source=x402-eco` | `https://api.axongate.one/paid-test?source=x402-eco` |
| `402agents` | `https://api.axongate.one/v1/x402/access?tier=fresh&source=402agents` | `https://api.axongate.one/docs?source=402agents` | `https://api.axongate.one/paid-test?source=402agents` |
| `github` | `https://api.axongate.one/v1/x402/access?tier=fresh&source=github` | `https://api.axongate.one/docs?source=github` | `https://api.axongate.one/paid-test?source=github` |

No-spend Proof Pack sample URLs for marketplace reviewers:

```text
https://api.axongate.one/proof-pack/sample?source=reviewer
https://api.axongate.one/v1/proof-pack/sample?source=reviewer
https://api.axongate.one/proof-pack/preview?target_url=https%3A%2F%2Fwww.iana.org%2Fdomains%2Freserved&pack=quick&source=reviewer
https://api.axongate.one/v1/proof-pack/preview?target_url=https%3A%2F%2Fwww.iana.org%2Fdomains%2Freserved&pack=quick&source=reviewer
https://api.axongate.one/proof-pack/quote?target_url=https%3A%2F%2Fexample.com&pack=standard&source=reviewer
https://api.axongate.one/proof-pack/request?target_url=https%3A%2F%2Fexample.com&pack=quick&source=reviewer
https://api.axongate.one/proof-pack/bundle?source=reviewer
https://api.axongate.one/v1/proof-pack/bundle/quote?target_urls=https%3A%2F%2Fwww.iana.org%2Fdomains%2Freserved%0Ahttps%3A%2F%2Fexample.com&bundle=scout&source=reviewer
```

Private lead inbox for operators:

```bash
curl -H "X-AxonGate-Operator-Token: <token>" "https://api.axongate.one/v1/operator/leads?limit=25"
```

Set `AXONGATE_OPERATOR_TOKEN` to enable raw contact access and
`AXONGATE_PROOF_PACK_LEAD_WEBHOOK_URL` to notify an external inbox or workflow
whenever a request is captured.

Stripe Proof Bundle fulfillment:

```text
Webhook URL: https://api.axongate.one/v1/stripe/webhook
Post-payment redirect URL: https://api.axongate.one/proof-pack/bundle/delivery?session_id={CHECKOUT_SESSION_ID}
Delivery recovery URL: https://api.axongate.one/proof-pack/bundle/recover
Required env: AXONGATE_STRIPE_WEBHOOK_SECRET=whsec_...
Events: checkout.session.completed, checkout.session.async_payment_succeeded, checkout.session.async_payment_failed
```

The webhook verifies Stripe signatures, dedupes event IDs, creates or updates a
paid Proof Bundle lead from Checkout custom fields, generates the cited delivery
report, and increments the paid and fulfilled bundle funnel metrics. The redirect
page lets buyers retrieve the report immediately by Stripe Checkout Session ID.
If Stripe shows a confirmation page instead of redirecting, buyers can recover a
paid report with their checkout email and one submitted target URL. If Stripe
passes a different email or no real customer email, recovery can still match the
paid bundle when one submitted target URL identifies exactly one paid order.

## Current Status

| Target | Status | Action |
| --- | --- | --- |
| x402 List | Review-window cooldown | AxonGate is present as slug `axongate`. Custom-domain starter source-alias update submitted on 2026-05-22; submission ID `86803de6-73b5-48cb-b7b4-0dd62f4d5702`; Proof Pack resubmission on 2026-05-22 returned HTTP 429 because the last submission is still within the 7-day review window. |
| PayanAgent | Custom-domain replacements live; Proof Pack added | Provider ID `j579abv0vkymwrxw480hy19xhx85t28n`. Custom-domain fresh service ID `js7ccna62pvxbnte7t18g797wx876gbw`; custom-domain starter service ID `js74m86sxk7rbasa56cnq6w1xh876912`; Proof Pack service ID `js7f9xfyvxqk0kyfea54h36hfx876jp3`. Legacy Railway service records remain active because PayanAgent exposes create/list/invoke but not service update/delete. |
| Agora402 | Ready, gated | Requires a one-time listing fee: starter 1 USDC, pro 5 USDC, featured 25 USDC. Use the prepared payload below after paying the chosen listing tier. |
| Agent Bazaar | Ready, manual | Submission form requires sign-in/review. Use the prepared fields below. |
| the402 | Ready, manual | Provider onboarding requires a site account. Use the prepared fields below. |
| x402.eco | PR branch prepared | Local branch `ecosystem-add-axongate` in `x402eco/website` clone, commit `8d8ab3a`. Direct push was denied to `sauliusbeconis`; create a fork or add GitHub CLI/token auth to open the PR. |

## PayanAgent

Public service registry check:

```bash
curl -s "https://payanagent.com/api/v1/services" | jq '.services[] | select(.name | contains("AxonGate"))'
```

Custom-domain replacement service payload:

```json
{
  "name": "AxonGate Clean Context Broker",
  "description": "Paid Web-to-Markdown context extraction for autonomous agents. Returns clean markdown plus payment and unit-economics metadata.",
  "serviceType": "api",
  "category": "Data",
  "pricingModel": "per_request",
  "priceInCents": 3,
  "endpoint": "https://api.axongate.one/v1/x402/access?tier=fresh&source=payanagent",
  "tags": ["x402", "base", "usdc", "web-to-markdown", "rag", "context-broker"]
}
```

Proof Pack service payload:

```json
{
  "name": "AxonGate Proof Packs",
  "description": "Paid citation-backed evidence reports for agent builders. Returns answer, executive summary, key claims, citations, risks, source hash, payment metadata, and UEG receipt.",
  "serviceType": "api",
  "category": "Data",
  "pricingModel": "per_request",
  "priceInCents": 25,
  "endpoint": "https://api.axongate.one/v1/x402/proof-pack?pack=standard&source=payanagent",
  "tags": ["x402", "base", "usdc", "proof-pack", "citations", "evidence", "agent-builders"]
}
```

Proof Bundle service payload:

```json
{
  "name": "AxonGate Proof Bundles",
  "description": "Higher-value multi-source evidence bundle quotes and lead capture for agent builders. Validates public URLs, returns exact USDC units, and routes buyers to payment/request follow-up.",
  "serviceType": "api",
  "category": "Data",
  "pricingModel": "per_request",
  "priceInCents": 700,
  "endpoint": "https://api.axongate.one/v1/proof-pack/bundle/quote?target_urls=https%3A%2F%2Fwww.iana.org%2Fdomains%2Freserved%0Ahttps%3A%2F%2Fexample.com&bundle=builder&source=payanagent",
  "tags": ["proof-bundle", "proof-pack", "citations", "evidence", "agent-builders"]
}
```

Custom-domain starter replacement service payload:

```json
{
  "name": "AxonGate Starter Clean Context",
  "description": "Lowest-friction paid Web-to-Markdown smoke test for autonomous agents. Actual x402 starter terms are 0.012 USDC and serve the sample target or existing cache with no supplier spend; use AxonGate fresh for live extraction.",
  "serviceType": "api",
  "category": "Data",
  "pricingModel": "per_request",
  "priceInCents": 2,
  "endpoint": "https://api.axongate.one/v1/x402/access?tier=starter&source=payanagent-starter",
  "tags": ["x402", "base", "usdc", "web-to-markdown", "rag", "context-broker", "starter"]
}
```

Replacement service IDs:

```text
Fresh: js7ccna62pvxbnte7t18g797wx876gbw
Starter: js74m86sxk7rbasa56cnq6w1xh876912
```

Legacy service IDs still visible in the public registry because service update/delete endpoints currently return `405`:

```text
Fresh legacy: js7ab33wbqbk5rp7rq8tvfk9yx85vbqn
Starter legacy: js71gygf2a31v1wx6m2fxn8b1s876hnr
```

## x402 List

AxonGate is listed and online. The custom-domain starter source-alias update was submitted on 2026-05-22 and is pending review:

```bash
curl -s "https://x402-list.com/api/v1/services" | jq '.data[] | select(.slug == "axongate")'
```

```json
{
  "submission_id": "86803de6-73b5-48cb-b7b4-0dd62f4d5702",
  "status": "pending",
  "probe_result": {
    "endpoints_found": 1,
    "errors": []
  }
}
```

Suggested update payload:

```json
{
  "url": "https://api.axongate.one",
  "service_name": "AxonGate",
  "website_url": "https://api.axongate.one/manifest.json?source=x402-list",
  "email": "<operator email>",
  "category": "Data",
  "description": "AxonGate is an x402-paid Clean Context Broker and Proof Pack service on Base. It converts public web pages into clean Markdown and citation-backed evidence reports for RAG, autonomous research, and LLM context preparation.",
  "endpoint_paths": ["/from/x402-list/v1/x402/starter", "/from/x402-list/v1/x402/proof-pack"],
  "endpoints": ["/from/x402-list/v1/x402/starter", "/from/x402-list/v1/x402/proof-pack"],
  "notes": "Basename axongate.base.eth resolves to the AxonGate vault. Submitted endpoints are source-attribution aliases that serve canonical x402 terms for starter context and standard Proof Packs. Standard x402 endpoint supports tiered pricing via ?tier= or X-AxonGate-Tier; Proof Packs support pack pricing via ?pack= or X-AxonGate-Pack. Discovery includes Bazaar metadata, payment-identifier, source attribution, starter sample pricing, cache-only pricing, no-spend Proof Pack mini previews, supplier-free quote APIs, and Proof Pack request capture."
}
```

Re-submit if the listing needs another metadata refresh:

```bash
python scripts/submit_x402_list.py --email "operator@example.com" --submit
```

## Agora402

Agora402 supports native x402 listings but activation requires a listing fee.
Listing tiers observed from `/api/listings/tiers`:

| Tier | Fee | Notes |
| --- | --- | --- |
| starter | 1 USDC | Live in registry and discoverable via API |
| pro | 5 USDC | Verified badge and priority ranking |
| featured | 25 USDC | Featured placement |

Prepared native listing payload:

```json
{
  "tier": "starter",
  "name": "AxonGate",
  "description": "x402-paid Clean Context Broker for public Web-to-Markdown extraction.",
  "category": "web-scraping",
  "endpoint_url": "https://api.axongate.one/v1/x402/access?tier=fresh&source=agora402",
  "chain": "base",
  "wallet_base": "0xcD11393c8505C5A44F8b998E0c96BcC5698d76A7",
  "price_usdc": 0.03,
  "payment_mode": "native",
  "is_open_source": true,
  "github_url": "https://github.com/sauliusbeconis/AxonGate",
  "llm_model": "Not LLM",
  "input_format": "json",
  "output_format": "json",
  "avg_latency_ms": 1500,
  "rate_limit_rpm": 120,
  "capabilities": ["web_scraping", "html_to_markdown", "rag_context_generation"],
  "tags": ["x402", "base", "usdc", "web-to-markdown", "context-broker"]
}
```

Do not run the paid listing step without explicitly approving the selected
Agora402 tier spend.

## Agent Bazaar

Form URL:

```text
https://www.agent-bazaar.com/dev
```

Suggested fields:

```text
Skill Name: AxonGate Clean Context Broker
Type: API Endpoint
Category: Web Scraping
Description: AxonGate converts public web pages into clean, token-efficient Markdown for autonomous agents, RAG pipelines, and LLM context preparation. It is x402-native on Base with USDC pay-per-use pricing and machine-readable discovery endpoints.
Price per Call: 0.03
x402 Endpoint URL: https://api.axongate.one/v1/x402/access?tier=fresh&source=agent-bazaar
```

## the402

Provider guide:

```text
https://the402.ai/docs/providers/
```

Suggested fields:

```text
Provider: AxonGate
Service: Clean Context Broker
Category: Data APIs
Price: 0.03 USDC per request
Endpoint: https://api.axongate.one/v1/x402/access?tier=fresh&source=the402
Docs: https://api.axongate.one/docs?source=the402
Paid test: https://api.axongate.one/paid-test?source=the402
Wallet: 0xcD11393c8505C5A44F8b998E0c96BcC5698d76A7
```

## x402.eco

Suggested ecosystem entry:

```json
{
  "name": "AxonGate",
  "description": "x402-paid clean Markdown extraction and citation-backed Proof Packs for RAG and autonomous agents.",
  "url": "https://api.axongate.one/proof-pack?source=x402-eco",
  "category": "services-endpoints",
  "logo": "/logos/axongate.svg",
  "tags": ["x402", "base", "usdc", "web-to-markdown", "rag", "proof-pack", "citations"]
}
```

Prepared PR branch:

```text
Repo: https://github.com/x402eco/website
Branch: ecosystem-add-axongate
Commit: 8d8ab3a ecosystem: add AxonGate
Files:
- data/ecosystem/services-endpoints/axongate.json
- public/logos/axongate.svg
```

Backup patch saved in this repo:

```text
docs/x402eco-axongate.patch
```

## Follow-Up Checks

After each listing goes live:

```bash
curl -s "https://api.axongate.one/metrics" | jq '.attribution'
curl -s "https://api.axongate.one/operator"
```

Look for each source under `payment_challenges`, `paid_attempts`,
`payments_accepted`, and `delivery_success`.
