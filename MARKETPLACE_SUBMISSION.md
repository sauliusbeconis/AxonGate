# AxonGate Marketplace Submission Kit

## Listing

Name: AxonGate

Service: The Clean Context Broker

Basename: axongate.base.eth

Category: Data & Context

Description:
AxonGate is an x402-paid Clean Context Broker for autonomous agents. It converts public web pages into clean, token-efficient Markdown for RAG pipelines, research agents, and LLM context preparation.

## Live URLs

Manifest:
https://web-production-8136ee.up.railway.app/manifest.json

Agent card:
https://web-production-8136ee.up.railway.app/.well-known/agent.json

x402 requirements:
https://web-production-8136ee.up.railway.app/.well-known/x402

PayAI-style discovery:
https://web-production-8136ee.up.railway.app/discovery/resources

Standard x402 endpoint:
https://web-production-8136ee.up.railway.app/v1/x402/access

Legacy tx-hash endpoint:
https://web-production-8136ee.up.railway.app/v1/access

## Pricing

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
