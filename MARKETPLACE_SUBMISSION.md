# AxonGate Marketplace Submission Kit

## Listing

Name: AxonGate

Service: The Clean Context Broker

Basename: axongate.base.eth

Category: Data & Context

Description:
AxonGate is an x402-paid Clean Context Broker for autonomous agents. It converts public web pages into clean, token-efficient Markdown for RAG pipelines, research agents, and LLM context preparation.

Short description:
x402-paid Web-to-Markdown context extraction for RAG and autonomous research agents.

## Live URLs

Manifest:
https://web-production-8136ee.up.railway.app/manifest.json

Agent card:
https://web-production-8136ee.up.railway.app/.well-known/agent.json

x402 requirements:
https://web-production-8136ee.up.railway.app/.well-known/x402

PayAI-style discovery:
https://web-production-8136ee.up.railway.app/discovery/resources

Source-tagged paid endpoint:
https://web-production-8136ee.up.railway.app/v1/x402/access?tier=fresh&source=SOURCE_NAME

Operator dashboard:
https://web-production-8136ee.up.railway.app/operator

Paid smoke test guide:
https://web-production-8136ee.up.railway.app/paid-test

Free quote API:
https://web-production-8136ee.up.railway.app/v1/x402/quote

Standard x402 endpoint:
https://web-production-8136ee.up.railway.app/v1/x402/access

Legacy tx-hash endpoint:
https://web-production-8136ee.up.railway.app/v1/access

Base URL:
https://web-production-8136ee.up.railway.app

Endpoint paths:

```text
/v1/x402/access
/.well-known/x402
/discovery/resources
/operator
/paid-test
/quote
/v1/x402/quote
```

## Pricing

starter: 0.012 USDC

cached: 0.015 USDC

basic: 0.02 USDC

fresh: 0.03 USDC

deep: 0.05 USDC

Network: Base mainnet, `eip155:8453`

Asset: USDC, `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`

Pay to:
`0xcD11393c8505C5A44F8b998E0c96BcC5698d76A7`

## Search Keywords

x402, Base, USDC, web scraping, Web-to-Markdown, markdown extraction, RAG context, autonomous research, LLM context preparation, paid API, agent marketplace

## Source Attribution

Use `source` in the endpoint query or `X-AxonGate-Source` as a request header.
Recommended directory source names:

```text
x402-list
payanagent
payanagent-starter
agora402
agent-bazaar
the402
x402-eco
github
```

Example source-tagged endpoint:

```text
https://web-production-8136ee.up.railway.app/v1/x402/access?tier=fresh&source=payanagent
```

## Buyer Payload

```json
{
  "target_url": "https://example.com",
  "tier": "basic",
  "force_refresh": false
}
```

For standard x402 clients, set the tier with either `?tier=basic` or the `X-AxonGate-Tier` header so payment requirements match the requested tier.

## Agent Bazaar Fields

Skill Name:
AxonGate Clean Context Broker

Type:
API Endpoint

Category:
Web Scraping

Description:
AxonGate converts public web pages into clean, token-efficient Markdown for autonomous agents, RAG pipelines, and LLM context preparation. It is x402-native on Base with USDC pay-per-use pricing and machine-readable discovery endpoints.

Price per Call:
0.012 starter, 0.015 cached, 0.02 basic, 0.03 fresh, 0.05 deep

x402 Endpoint URL:
https://web-production-8136ee.up.railway.app/v1/x402/access?tier=fresh&source=agent-bazaar

## PayanAgent Fields

Provider ID:
`j579abv0vkymwrxw480hy19xhx85t28n`

Service ID:
`js7ab33wbqbk5rp7rq8tvfk9yx85vbqn`

Starter Service ID:
`js71gygf2a31v1wx6m2fxn8b1s876hnr`

Service name:
AxonGate Clean Context Broker

Category:
Data

Pricing model:
per_request

Price:
3 cents

Endpoint:
https://web-production-8136ee.up.railway.app/v1/x402/access?tier=fresh&source=payanagent

Starter service name:
AxonGate Starter Clean Context

Starter price:
2 cents in PayanAgent display metadata; actual x402 starter terms are 0.012 USDC.

Starter endpoint:
https://web-production-8136ee.up.railway.app/v1/x402/access?tier=starter&source=payanagent-starter

## x402 List Fields

Service name:
AxonGate

Service base URL:
https://web-production-8136ee.up.railway.app

Website URL:
https://web-production-8136ee.up.railway.app/manifest.json

Category:
Data

Description:
AxonGate is an x402-paid Clean Context Broker on Base. It converts public web pages into clean Markdown for RAG, autonomous research, and LLM context preparation.

Endpoint paths:

```text
/from/x402-list/v1/x402/starter
/.well-known/x402
/discovery/resources
```

Notes:
Basename axongate.base.eth resolves to the AxonGate vault and advertises the manifest URL in its text records. Standard x402 endpoint supports tiered pricing via query param or X-AxonGate-Tier header, official Bazaar discovery metadata, optional payment-identifier, a supplier-free quote API, a starter sample tier for first paid conversion, and cache-only low-cost tiers. The 2026-05-22 update attempt was blocked because the x402-list submit API now rejects railway.app origins; switch to a custom domain before re-submitting.

## 402agents Fields

Agent name:
AxonGate

Category:
Tools & Integrations

Description:
AxonGate is a Base x402-native Clean Context Broker that returns clean Markdown from public URLs for agentic research and RAG workflows.

Agent / manifest URL:
https://web-production-8136ee.up.railway.app/manifest.json

x402 endpoint:
https://web-production-8136ee.up.railway.app/v1/x402/access?tier=fresh&source=402agents
