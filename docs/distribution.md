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
| `agora402` | `https://api.axongate.one/v1/x402/access?tier=fresh&source=agora402` | `https://api.axongate.one/docs?source=agora402` | `https://api.axongate.one/paid-test?source=agora402` |
| `agent-bazaar` | `https://api.axongate.one/v1/x402/access?tier=fresh&source=agent-bazaar` | `https://api.axongate.one/docs?source=agent-bazaar` | `https://api.axongate.one/paid-test?source=agent-bazaar` |
| `the402` | `https://api.axongate.one/v1/x402/access?tier=fresh&source=the402` | `https://api.axongate.one/docs?source=the402` | `https://api.axongate.one/paid-test?source=the402` |
| `x402-eco` | `https://api.axongate.one/v1/x402/access?tier=fresh&source=x402-eco` | `https://api.axongate.one/docs?source=x402-eco` | `https://api.axongate.one/paid-test?source=x402-eco` |
| `402agents` | `https://api.axongate.one/v1/x402/access?tier=fresh&source=402agents` | `https://api.axongate.one/docs?source=402agents` | `https://api.axongate.one/paid-test?source=402agents` |
| `github` | `https://api.axongate.one/v1/x402/access?tier=fresh&source=github` | `https://api.axongate.one/docs?source=github` | `https://api.axongate.one/paid-test?source=github` |

## Current Status

| Target | Status | Action |
| --- | --- | --- |
| x402 List | Update submitted, pending review | AxonGate is present as slug `axongate`. Custom-domain starter source-alias update submitted on 2026-05-22; submission ID `86803de6-73b5-48cb-b7b4-0dd62f4d5702`; probe found 1 endpoint and 0 errors. |
| PayanAgent | Submitted, starter added | Provider ID `j579abv0vkymwrxw480hy19xhx85t28n`. Fresh service ID `js7ab33wbqbk5rp7rq8tvfk9yx85vbqn`; starter service ID `js71gygf2a31v1wx6m2fxn8b1s876hnr`. API key is stored only in local ignored `.env`. |
| Agora402 | Ready, gated | Requires a one-time listing fee: starter 1 USDC, pro 5 USDC, featured 25 USDC. Use the prepared payload below after paying the chosen listing tier. |
| Agent Bazaar | Ready, manual | Submission form requires sign-in/review. Use the prepared fields below. |
| the402 | Ready, manual | Provider onboarding requires a site account. Use the prepared fields below. |
| x402.eco | Ready, PR needed | Submit a GitHub PR adding AxonGate under the services/endpoints ecosystem data. |

## PayanAgent

Public service registry check:

```bash
curl -s "https://payanagent.com/api/v1/services" | jq '.services[] | select(.name | contains("AxonGate"))'
```

Submitted service payload:

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

Starter service payload:

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
  "description": "AxonGate is an x402-paid Clean Context Broker on Base. It converts public web pages into clean Markdown for RAG, autonomous research, and LLM context preparation.",
  "endpoint_paths": ["/from/x402-list/v1/x402/starter"],
  "endpoints": ["/from/x402-list/v1/x402/starter"],
  "notes": "Basename axongate.base.eth resolves to the AxonGate vault. Submitted endpoint is a source-attribution alias that serves the same canonical x402 terms as /v1/x402/access. Standard x402 endpoint supports tiered pricing via ?tier= or X-AxonGate-Tier, official Bazaar discovery metadata, optional payment-identifier, source attribution, starter sample pricing, and cache-only pricing."
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
  "description": "x402-paid Clean Context Broker that converts public pages into clean markdown for RAG and autonomous agents.",
  "url": "https://api.axongate.one/docs?source=x402-eco",
  "category": "services-endpoints",
  "logo": "/logos/axongate.png",
  "tags": ["x402", "base", "usdc", "web-to-markdown", "rag"]
}
```

## Follow-Up Checks

After each listing goes live:

```bash
curl -s "https://api.axongate.one/metrics" | jq '.attribution'
curl -s "https://api.axongate.one/operator"
```

Look for each source under `payment_challenges`, `paid_attempts`,
`payments_accepted`, and `delivery_success`.
