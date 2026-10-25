# AxonGate

**Safe web access for AI agents.** Read any public page as clean Markdown,
search the live web, get grounded answers, fact-check claims and check a crypto
token before trading it. Every page is screened for prompt injection, and every
quote in an answer or verdict is checked verbatim against the fetched source.
Agents pay per call in USDC on Base, Polygon or Arbitrum (Solana when
configured) over [x402](https://x402.org): no account, no API key, charged only
on success.

**Live:** [api.axongate.one](https://api.axongate.one) ·
[docs](https://api.axongate.one/docs) ·
[x402 catalog](https://api.axongate.one/.well-known/x402) ·
[llms.txt](https://api.axongate.one/llms.txt) ·
[OpenAPI](https://api.axongate.one/openapi.json)

| Product | Route | Price |
| --- | --- | --- |
| Safe Read | `GET/POST /v1/read` | $0.002 |
| Batch Read (up to 20 URLs) | `POST /v1/read/batch` | $0.03 |
| Web Search | `GET/POST /v1/search` | $0.004 |
| Injection Scan | `POST /v1/scan` | $0.002 |
| Grounded Answer | `POST /v1/answer` | $0.015 |
| Claim Verify | `POST /v1/verify` | $0.02 |
| Token Check | `GET/POST /v1/token-check` | $0.01 |

MCP server: [`mcp/`](mcp) (`npx axongate-mcp`).

## Why v2

v1 (Proof Packs, Stripe bundles, tiered extraction at $0.012–$1.00) logged
~40k payment challenges and zero outside paid calls in six months. Most of that
traffic was x402 directories crawling each other. The October 2026 relaunch is
built from what agents actually pay for in the Coinbase Bazaar (30-day paid
calls by category):

- **Price to market.** Web reads sell at $0.001–0.002 and searches at
  $0.007–0.025, so AxonGate's prices match or undercut them. Margins hold
  because reads are extracted locally (no supplier cost) and search costs about
  $0.0005.
- **Get indexed where buyers look.** Bazaar only lists endpoints that settle
  through the CDP facilitator. v1 used PayAI and was never listed. v2 settles
  through CDP and declares full Bazaar metadata on every product.
- **Differentiate on safety.** Prompt-injection screening is built into every
  read, a standalone scan endpoint is offered, and verbatim quote checks stop a
  model from inventing evidence.
- **Keep the existing listings.** The `/v1/x402/access` and
  `/v1/x402/proof-pack` URLs that directories already list now serve Safe Read
  and Claim Verify.

## How a call works

1. The agent calls an endpoint and gets `402` with `PAYMENT-REQUIRED`: one
   exact-scheme USDC entry per network (Base `eip155:8453` first, then Polygon
   and Arbitrum to payTo `0xcD11…76A7`, and Solana when
   `AXONGATE_SOLANA_PAY_TO` is set).
2. Its x402 client signs an EIP-3009 authorization and retries with
   `PAYMENT-SIGNATURE`.
3. AxonGate does the work. The x402 middleware settles only if the response is
   2xx, so errors are never charged.

`/v1/answer` and `/v1/verify` run search, then concurrent reads (cached in
Redis), then passage selection, then one structured `gpt-5-mini` call. Quotes
the model returns are matched against the fetched text. Unmatched quotes are
dropped, and a supported or refuted verdict without a matched quote is
downgraded to `insufficient`.

## Layout

```text
axongate/
  app.py         FastAPI app, product handlers, operator metrics, retired-route handling
  config.py      environment, product catalog and prices
  payments.py    x402 routes, CDP/PayAI facilitator, Bazaar metadata
  fetcher.py     SSRF-safe fetch, trafilatura extraction, Jina rendered fallback
  search.py      Jina search with caching
  research.py    answer/verify pipeline and quote verification
  safety.py      rule-based prompt-injection scanner
  tokens.py      token-check verdicts (GoPlus Security + honeypot.is simulation)
  llm.py         structured-output chat client
  store.py       Redis cache/counters with in-memory fallback, rate limiter
  telemetry.py   ASGI middleware: rate limits, challenges, settlements, revenue
  discovery.py   /.well-known/x402, manifest, agent card, llms.txt, pricing
  pages.py       home, docs and bot pages
axongate_gateway.py   entry point kept for `uvicorn axongate_gateway:app`
scripts/ci_smoke.py   offline test suite (x402 surface + mocked handlers)
scripts/paid_smoke.py real paid calls from a CDP account (operator tool)
mcp/                  MCP server, published to npm as axongate-mcp
examples/             Python client, curl
```

## Run locally

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in what you need; everything degrades gracefully
uvicorn axongate_gateway:app --reload --port 8000
```

Set `AXONGATE_X402_ENABLED=false` to call the handlers without paying.

## Test

```bash
python scripts/ci_smoke.py      # no network suppliers, no USDC
python scripts/secret_scan.py
```

## Operate

- `GET /metrics` with `X-AxonGate-Operator-Token` shows revenue, supplier cost,
  settled calls, 402 challenges per product and per path, and top paying
  clients.
- `python scripts/paid_smoke.py --account <buyer> --max-spend 0.05` makes real
  paid calls. It's needed once per endpoint for Bazaar indexing, and again
  every 30 days to keep a listing alive when there is no buyer traffic.
- Railway runs `uvicorn axongate_gateway:app --host 0.0.0.0 --port $PORT` with
  `/health` as the health check. `web` and `Redis` services are in the
  production environment.

See [docs/distribution.md](docs/distribution.md) for listings.

## Licence

MIT — see [LICENSE](LICENSE).
