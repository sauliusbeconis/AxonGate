# AxonGate

AxonGate answers one question for an autonomous caller: **do the public sources
actually support this claim, well enough to rely on?** It fetches the live
sources behind a claim, extracts clean context, and returns a citation-backed
verdict — so an AI agent or RAG pipeline can decide whether to trust a statement
before repeating it to a user.

It is a running commercial service, not a demo.

**Live:** [api.axongate.one](https://api.axongate.one) ·
[interactive docs](https://api.axongate.one/docs) ·
[OpenAPI contract](https://api.axongate.one/openapi.json) ·
[agent brief](https://api.axongate.one/llms.txt)

## Why the payment layer is the interesting part

Most APIs assume a human signed up, got a key, and attached a card. AxonGate
assumes nobody did. An agent that has never seen this service before can
discover it, learn the price, pay, and get an answer — in one request cycle,
with no account, no key, and no human.

That is the **x402** flow, and it works like this:

1. The agent `POST`s to a paid route with no payment attached.
2. AxonGate replies `402 Payment Required` with a machine-readable quote: the
   exact USDC amount, the receiving address, the chain (`eip155:8453` — Base
   mainnet), and an expiry.
3. The agent signs a USDC transfer authorisation and retries with it in the
   `X-PAYMENT` header (`PAYMENT-SIGNATURE` and `X-402-PAYMENT` are also
   accepted).
4. A facilitator settles on Base; the response carries the work product.

Details worth noting if you are reading the code:

- **The quote is selected per request, not fixed per route.**
  `x402_dynamic_price` reads a `tier` or `pack` value from the request's query
  string or headers and resolves it against a price table configured by
  environment variables — roughly $0.012 to $0.05 for extraction tiers and $0.10
  to $1.00 for Proof Packs.
- **The service refuses work it would lose money on.** Before doing paid work it
  computes projected profit as revenue minus live Base gas — the current base fee
  times a gas-unit estimate, converted through a live ETH/USD quote with a
  configured floor and cache — minus bounded supplier cost per attempt. If the
  projected margin falls to or below `AXONGATE_PROFIT_MARGIN_USDC` (default
  `0.01`), the paid request is rejected with a payment validation error and
  `ueg_rejections_total` is incremented. A per-request unit-economics check
  matters more at this price point than at normal API prices: a few cents of
  revenue does not survive an unexamined gas spike.
- **Two rails, one product.** Agents pay per call over x402. Humans buying
  multi-source Proof Bundles pay through Stripe checkout. Webhook handling
  verifies an HMAC-SHA256 signature over the timestamped raw body with a
  constant-time compare, rejects timestamps outside a tolerance window, and
  de-duplicates by Stripe event ID in Redis so a replayed event cannot deliver
  twice. Both rails converge on the same evidence generation path.
- **Attribution is built into the payment route.** Paid routes are also mounted
  under `POST /from/{source}/v1/x402/access`, so the marketplace that sent a
  paying agent is recorded at settlement time rather than guessed from a
  referrer header.
- **Free routes are genuinely free.** Preview, sample, and quote endpoints make
  no paid supplier call and return `supplier_spend: false`, so an agent can
  evaluate the service before committing funds.

## Endpoints at a glance

| Route | Cost | Purpose |
| --- | --- | --- |
| `POST /v1/x402/access` | paid (x402) | Paid web-to-markdown clean context extraction |
| `POST /v1/x402/proof-pack` | paid (x402) | Citation-backed evidence report |
| `GET /v1/proof-pack/preview` | free | No-spend mini preview |
| `GET /v1/proof-pack/quote` | free | Supplier-free price quote |
| `GET /v1/proof-pack/reports/{id}/verify` | free | Verify a retained report receipt |
| `GET /manifest.json`, `/agent_manifest.json` | free | Agent and payment discovery contracts |
| `GET /health`, `/metrics` | operator | Readiness and operational metrics |

`GET` quote endpoints are preview-only and retain nothing. Create a resumable
quote deliberately with `POST /v1/quotes`. The `/operator` and `/metrics` routes
require `AXONGATE_OPERATOR_TOKEN`, sent as `X-AxonGate-Operator-Token`, a bearer
token, or the browser-only `operator_token` query parameter.

## Local setup

Prerequisites:

- Python 3.11+
- Node.js 20+ for the MCP and paid-buyer examples
- Railway CLI only when inspecting or deploying the production service

Create the Python environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Install the JavaScript dependencies:

```bash
npm ci
```

Copy the environment template for local overrides. Never commit the resulting
`.env` file.

```bash
cp .env.example .env
```

```powershell
Copy-Item .env.example .env
```

The checked-in manifests contain the public AxonGate wallet address, so the app
starts without private wallet credentials. Supplier, Redis, email, LLM, Stripe,
and operator features stay disabled or use local fallbacks until their
environment variables are configured.

## Run locally

```bash
PORT=8000 uvicorn axongate_gateway:app --host 127.0.0.1 --port 8000 --reload
```

```powershell
$env:PORT = "8000"
uvicorn axongate_gateway:app --host 127.0.0.1 --port $env:PORT --reload
```

Open `http://127.0.0.1:8000/` for the product page, `/docs` for the service
documentation, and `/openapi.json` for the agent contract.

## Verify changes

The smoke suite uses an in-process ASGI client, fake Stripe signatures, and fake
email delivery. It does not spend USDC or call paid suppliers. The x402
middleware may make a no-spend capability request to the configured facilitator.

```bash
python -m py_compile axongate_gateway.py examples/python_client.py scripts/ci_smoke.py scripts/secret_scan.py
python scripts/ci_smoke.py
python scripts/secret_scan.py
```

The same three steps run in CI on every push and pull request to `main`.

## Production

Railway runs:

```text
uvicorn axongate_gateway:app --host 0.0.0.0 --port $PORT
```

`railway.toml` configures `/health` as the deployment health check. The endpoint
returns only service readiness information and also schedules throttled
operational alert evaluation. Alerts are written as structured Railway logs by
default and can additionally be sent to `AXONGATE_ALERT_WEBHOOK_URL`.

The production project contains the `web` and `Redis` services. Link a local
checkout without changing production using the project ID from your Railway
dashboard:

```bash
railway link --project <project-id> --environment production --service web
railway status
```

Do not run `railway up`, `redeploy`, `restart`, or variable mutation commands
unless a production change is intentional.

## Current structure

- `axongate_gateway.py` — FastAPI application, payments, evidence generation, delivery, analytics, and embedded UI
- `scripts/ci_smoke.py` — end-to-end in-process smoke suite
- `scripts/secret_scan.py` — tracked-file secret check
- `examples/` — Python, MCP, curl, and paid-buyer examples
- `manifest.json` and `agent_manifest.json` — public agent and payment discovery contracts
- `scripts/submit_x402_list.py` — marketplace listing submission helper
- `docs/` — custom-domain setup, marketplace distribution notes, and the listing submission kit

The application is currently a large monolith: `axongate_gateway.py` is ~20,500
lines, of which roughly a fifth is embedded HTML for the human-facing pages. New
work should avoid adding more embedded UI or unrelated responsibilities to it;
extracting billing, analytics, evidence generation, and page templates into
modules is the current priority.

## Licence

MIT — see [LICENSE](LICENSE).
