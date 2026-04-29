# AxonGate Distribution Checklist

Canonical URLs:

```text
https://web-production-8136ee.up.railway.app/manifest.json
https://web-production-8136ee.up.railway.app/.well-known/agent.json
https://web-production-8136ee.up.railway.app/.well-known/agent-card.json
https://web-production-8136ee.up.railway.app/.well-known/x402
https://web-production-8136ee.up.railway.app/.well-known/x402.json
https://web-production-8136ee.up.railway.app/discovery/resources
https://web-production-8136ee.up.railway.app/llms.txt
https://web-production-8136ee.up.railway.app/sitemap.xml
https://web-production-8136ee.up.railway.app/docs
https://web-production-8136ee.up.railway.app/demo
```

## Submission Targets

| Target | Status | Notes |
| --- | --- | --- |
| x402 List | Listed | AxonGate is already present at `https://x402-list.com` as `axongate`, status online. |
| Coinbase x402 Bazaar | Prepared | Bazaar indexing requires CDP Facilitator integration and at least one successful settlement through CDP verify/settle before resources appear in the catalog. |
| Agora402 | Prepared | Registration requires a one-time USDC listing fee and transaction hash. Native x402 mode is the right fit for AxonGate. |
| x402.eco | Prepared | Submit a PR to `x402eco/website` adding AxonGate under `data/ecosystem/services-endpoints/`. |
| PayanAgent | Prepared | Provider registration is API-driven; service listing requires provider registration/API key. |

## Suggested x402.eco Entry

```json
{
  "name": "AxonGate",
  "description": "x402-paid Clean Context Broker that converts public web pages into clean markdown for RAG and autonomous agents.",
  "url": "https://web-production-8136ee.up.railway.app",
  "category": "services-endpoints",
  "logo": "/logos/axongate.png"
}
```

## Suggested Agora402 Native Listing Payload

Use after paying the selected Agora402 listing fee and receiving the listing
transaction hash.

```json
{
  "tx_hash": "0xLISTING_PAYMENT_TX",
  "tier": "starter",
  "name": "AxonGate",
  "description": "x402-paid Clean Context Broker for public Web-to-Markdown extraction.",
  "category": "research",
  "endpoint_url": "https://web-production-8136ee.up.railway.app/v1/x402/access",
  "chain": "base",
  "wallet_base": "0xcD11393c8505C5A44F8b998E0c96BcC5698d76A7",
  "price_usdc": 0.03,
  "payment_mode": "native",
  "is_open_source": true,
  "github_url": "https://github.com/sauliusbeconis/AxonGate",
  "llm_model": "Jina Reader upstream plus AxonGate UEG",
  "input_format": "json",
  "output_format": "json",
  "avg_latency_ms": 1500,
  "rate_limit_rpm": 120,
  "capabilities": ["web_scraping", "html_to_markdown", "rag_context_generation"],
  "tags": ["x402", "base", "usdc", "web-to-markdown", "context-broker"]
}
```

## Suggested PayanAgent Provider Data

```json
{
  "name": "AxonGate",
  "description": "Clean public web context as markdown, paid per request over x402 on Base.",
  "walletAddress": "0xcD11393c8505C5A44F8b998E0c96BcC5698d76A7",
  "providerType": "service",
  "tags": ["x402", "base", "rag", "web-to-markdown"]
}
```

After provider registration, list the service endpoint:

```json
{
  "name": "Clean Context Broker",
  "serviceType": "api",
  "priceInCents": 3,
  "endpoint": "https://web-production-8136ee.up.railway.app/v1/x402/access"
}
```
