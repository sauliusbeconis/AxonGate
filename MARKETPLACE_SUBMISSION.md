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

Operator dashboard:
https://web-production-8136ee.up.railway.app/operator

Paid smoke test guide:
https://web-production-8136ee.up.railway.app/paid-test

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
```

## Pricing

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
0.015 cached, 0.02 basic, 0.03 fresh, 0.05 deep

x402 Endpoint URL:
https://web-production-8136ee.up.railway.app/v1/x402/access

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
/v1/x402/access
/.well-known/x402
/discovery/resources
```

Notes:
Basename axongate.base.eth resolves to the AxonGate vault and advertises the manifest URL in its text records. Standard x402 endpoint supports tiered pricing via query param or X-AxonGate-Tier header, official Bazaar discovery metadata, optional payment-identifier, and a cache-only low-cost tier.

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
https://web-production-8136ee.up.railway.app/v1/x402/access
