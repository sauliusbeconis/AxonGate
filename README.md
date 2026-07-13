# AxonGate

AxonGate is a FastAPI service that checks whether public web sources support a claim well enough for an AI agent, RAG pipeline, or customer-facing answer to rely on them. It exposes human-facing evidence reports, an x402-paid agent API on Base, Stripe checkout for multi-source bundles, retained report verification, and operator diagnostics.

## Local setup

Prerequisites:

- Python 3.11+
- Node.js 20+ for the MCP and paid-buyer examples
- Railway CLI only when inspecting or deploying the production service

Create the Python environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Install the JavaScript dependencies:

```powershell
npm ci
```

Copy the environment template for local overrides. Never commit the resulting `.env` file.

```powershell
Copy-Item .env.example .env
```

The checked-in manifests contain the public AxonGate wallet address, so the app can start without private wallet credentials. Supplier, Redis, email, LLM, Stripe, and operator features remain disabled or use local fallbacks until their environment variables are configured.

## Run locally

```powershell
$env:PORT = "8000"
uvicorn axongate_gateway:app --host 127.0.0.1 --port $env:PORT --reload
```

Open `http://127.0.0.1:8000/` for the product page, `/docs` for the service documentation, and `/openapi.json` for the agent contract.

`GET` quote endpoints are preview-only and do not retain data. Create a resumable quote intentionally with `POST /v1/quotes`. The `/operator` and `/metrics` routes require `AXONGATE_OPERATOR_TOKEN`; send it as `X-AxonGate-Operator-Token`, a bearer token, or the browser-only `operator_token` query parameter.

## Verify changes

The smoke suite uses an in-process ASGI client, fake Stripe signatures, and fake email delivery. It does not spend USDC or call paid suppliers. The x402 middleware may make a no-spend capability request to the configured facilitator.

```powershell
python -m py_compile axongate_gateway.py examples/python_client.py scripts/ci_smoke.py scripts/secret_scan.py
python scripts/ci_smoke.py
python scripts/secret_scan.py
```

## Production

Railway runs:

```text
uvicorn axongate_gateway:app --host 0.0.0.0 --port $PORT
```

`railway.toml` configures `/health` as the deployment health check. The endpoint returns only service readiness information and also schedules throttled operational alert evaluation. Alerts are written as structured Railway logs by default and can additionally be sent to `AXONGATE_ALERT_WEBHOOK_URL`.

The production project contains the `web` and `Redis` services. Link a local checkout without changing production:

```powershell
railway link --project 6346de42-9fe6-4fb3-957d-5c5bde1989c9 --environment production --service web
railway status
```

Do not run `railway up`, `redeploy`, `restart`, or variable mutation commands unless a production change is intentional.

## Current structure

- `axongate_gateway.py` — FastAPI application, payments, evidence generation, delivery, analytics, and embedded UI
- `scripts/ci_smoke.py` — end-to-end in-process smoke suite
- `scripts/secret_scan.py` — tracked-file secret check
- `examples/` — Python, MCP, curl, and paid-buyer examples
- `manifest.json` and `agent_manifest.json` — public agent and payment discovery contracts
- `docs/` — custom-domain and marketplace distribution notes

The application is currently a large monolith. New work should avoid adding more embedded UI or unrelated responsibilities to `axongate_gateway.py`; extracting analytics, billing, evidence generation, and page templates into modules is a priority.
