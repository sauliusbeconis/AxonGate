# AxonGate Marketplace Submission Kit

## Listing

Name: AxonGate

Service: The Clean Context Broker

Basename: axongate.base.eth

Category: Data & Context

Description:
AxonGate is an x402-paid Clean Context Broker and Proof Pack service for autonomous agents. It converts public web pages into clean, token-efficient Markdown and can return citation-backed evidence reports for agent builders.

Short description:
x402-paid Web-to-Markdown context extraction and cited Proof Packs for agent builders.

## Live URLs

Manifest:
https://api.axongate.one/manifest.json

Agent card:
https://api.axongate.one/.well-known/agent.json

x402 requirements:
https://api.axongate.one/.well-known/x402

PayAI-style discovery:
https://api.axongate.one/discovery/resources

Source-tagged paid endpoint:
https://api.axongate.one/v1/x402/access?tier=fresh&source=SOURCE_NAME

Operator dashboard:
https://api.axongate.one/operator

Paid smoke test guide:
https://api.axongate.one/paid-test

Free quote API:
https://api.axongate.one/v1/x402/quote

Proof Pack page:
https://api.axongate.one/proof-pack

Proof Pack sample page:
https://api.axongate.one/proof-pack/sample

Proof Pack sample API:
https://api.axongate.one/v1/proof-pack/sample

Proof Pack mini preview page:
https://api.axongate.one/proof-pack/preview

Proof Pack mini preview API:
https://api.axongate.one/v1/proof-pack/preview

Proof Pack quote page:
https://api.axongate.one/proof-pack/quote

Proof Pack quote API:
https://api.axongate.one/v1/proof-pack/quote

Proof Pack request page:
https://api.axongate.one/proof-pack/request

Proof Pack lead API:
https://api.axongate.one/v1/proof-pack/leads

Proof Pack x402 endpoint:
https://api.axongate.one/v1/x402/proof-pack

Standard x402 endpoint:
https://api.axongate.one/v1/x402/access

Legacy tx-hash endpoint:
https://api.axongate.one/v1/access

Base URL:
https://api.axongate.one

Endpoint paths:

```text
/v1/x402/access
/.well-known/x402
/discovery/resources
/operator
/paid-test
/quote
/v1/x402/quote
/proof-pack
/proof-pack/sample
/proof-pack/preview
/proof-pack/quote
/proof-pack/request
/v1/proof-pack/sample
/v1/proof-pack/preview
/v1/proof-pack/quote
/v1/proof-pack/leads
/v1/x402/proof-pack
```

## Pricing

starter: 0.012 USDC

cached: 0.015 USDC

basic: 0.02 USDC

fresh: 0.03 USDC

deep: 0.05 USDC

Proof Pack quick: 0.10 USDC

Proof Pack standard: 0.25 USDC

Proof Pack deep: 1.00 USDC

Network: Base mainnet, `eip155:8453`

Asset: USDC, `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`

Pay to:
`0xcD11393c8505C5A44F8b998E0c96BcC5698d76A7`

## Search Keywords

x402, Base, USDC, web scraping, Web-to-Markdown, markdown extraction, RAG context, autonomous research, LLM context preparation, Proof Pack, citations, evidence report, paid API, agent marketplace

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
https://api.axongate.one/v1/x402/access?tier=fresh&source=payanagent
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

## Proof Pack Payload

```json
{
  "target_url": "https://example.com",
  "question": "What does this source establish?",
  "pack": "standard",
  "force_refresh": false
}
```

For standard x402 clients, set the Proof Pack with either `?pack=standard` or the `X-AxonGate-Pack` header so payment requirements match the requested pack.

## Agent Bazaar Fields

Skill Name:
AxonGate Clean Context Broker

Type:
API Endpoint

Category:
Web Scraping

Description:
AxonGate converts public web pages into clean, token-efficient Markdown and citation-backed Proof Packs for autonomous agents, RAG pipelines, and LLM context preparation. It is x402-native on Base with USDC pay-per-use pricing and machine-readable discovery endpoints.

Price per Call:
0.012 starter, 0.015 cached, 0.02 basic, 0.03 fresh, 0.05 deep; Proof Packs are 0.10 quick, 0.25 standard, 1.00 deep

x402 Endpoint URL:
https://api.axongate.one/v1/x402/access?tier=fresh&source=agent-bazaar

## PayanAgent Fields

Provider ID:
`j579abv0vkymwrxw480hy19xhx85t28n`

Service ID:
`js7ccna62pvxbnte7t18g797wx876gbw`

Proof Pack Service ID:
`js7f9xfyvxqk0kyfea54h36hfx876jp3`

Starter Service ID:
`js74m86sxk7rbasa56cnq6w1xh876912`

Legacy Railway service IDs still visible because PayanAgent service PATCH/PUT/DELETE return 405:
`js7ab33wbqbk5rp7rq8tvfk9yx85vbqn`, `js71gygf2a31v1wx6m2fxn8b1s876hnr`

Service name:
AxonGate Clean Context Broker

Category:
Data

Pricing model:
per_request

Price:
3 cents

Endpoint:
https://api.axongate.one/v1/x402/access?tier=fresh&source=payanagent

Proof Pack service name:
AxonGate Proof Packs

Proof Pack price:
25 cents for standard Proof Pack display metadata; actual x402 standard terms are 0.25 USDC.

Proof Pack endpoint:
https://api.axongate.one/v1/x402/proof-pack?pack=standard&source=payanagent

Starter service name:
AxonGate Starter Clean Context

Starter price:
2 cents in PayanAgent display metadata; actual x402 starter terms are 0.012 USDC.

Starter endpoint:
https://api.axongate.one/v1/x402/access?tier=starter&source=payanagent-starter

## x402 List Fields

Service name:
AxonGate

Service base URL:
https://api.axongate.one

Website URL:
https://api.axongate.one/manifest.json

Category:
Data

Description:
AxonGate is an x402-paid Clean Context Broker and Proof Pack service on Base. It converts public web pages into clean Markdown and citation-backed evidence reports for RAG, autonomous research, and LLM context preparation.

Endpoint paths:

```text
/from/x402-list/v1/x402/starter
/from/x402-list/v1/x402/proof-pack
/.well-known/x402
/discovery/resources
```

Notes:
Basename axongate.base.eth resolves to the AxonGate vault and advertises the manifest URL in its text records. Standard x402 endpoint supports tiered pricing via query param or X-AxonGate-Tier header, and Proof Packs support pack pricing via query param or X-AxonGate-Pack header. AxonGate includes official Bazaar discovery metadata, optional payment-identifier, supplier-free quote APIs, no-spend Proof Pack mini previews, request capture for Proof Pack demand, a starter sample tier for first paid conversion, cache-only low-cost tiers, and citation-backed Proof Packs. The custom domain is now connected at api.axongate.one, so use the custom-domain URLs for re-submission.

## 402agents Fields

Agent name:
AxonGate

Category:
Tools & Integrations

Description:
AxonGate is a Base x402-native Clean Context Broker and Proof Pack service that returns clean Markdown or cited evidence reports from public URLs for agentic research and RAG workflows.

Agent / manifest URL:
https://api.axongate.one/manifest.json

x402 endpoint:
https://api.axongate.one/v1/x402/access?tier=fresh&source=402agents
