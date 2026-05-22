# AxonGate cURL Examples

These examples show the request shape for agents and operators. They do not
generate a payment proof. Use your x402 wallet or facilitator client to create
the `PAYMENT-SIGNATURE` value, then pass it as an environment variable.

```bash
export AXONGATE_BASE_URL="https://web-production-8136ee.up.railway.app"
export TARGET_URL="https://example.com"
export AXONGATE_SOURCE="docs"
```

## Discover Payment Requirements

```bash
curl -i "$AXONGATE_BASE_URL/v1/x402/access?source=$AXONGATE_SOURCE"
```

The response should be `402 Payment Required` and include `PAYMENT-REQUIRED`
and `X-Payment-Required` headers. Those headers contain the x402 payment
requirements for Base USDC, plus official Bazaar discovery and optional
payment-identifier extensions. The response also includes low-friction buyer
headers such as `X-AxonGate-Docs`, `X-AxonGate-Quickstart`, `X-AxonGate-Paid-Test`,
`X-AxonGate-Demo`, and `X-AxonGate-Buyer-Example`.

Directories that cannot submit query-string source tags can use a path alias:

```bash
curl -i "$AXONGATE_BASE_URL/from/x402-list/v1/x402/access"
```

That alias serves the same canonical x402 payment terms while attributing the
probe to `x402-list`.

## Standard x402 Paid Request

For a real Base USDC smoke test from a burner wallet, use the repo-native buyer:

```bash
npm install
npm run paid:buyer -- \
  --wallet-file "C:/path/to/buyer_wallet.json" \
  --target-url "https://www.iana.org/domains/reserved" \
  --tier starter \
  --confirm-spend 0.012 \
  --source docs \
  --replay
```

```bash
export PAYMENT_SIGNATURE="<x402 payment proof from your wallet or facilitator>"

curl -sS -X POST "$AXONGATE_BASE_URL/v1/x402/access" \
  -H "Content-Type: application/json" \
  -H "PAYMENT-SIGNATURE: $PAYMENT_SIGNATURE" \
  -H "X-AxonGate-Tier: fresh" \
  -H "X-AxonGate-Source: $AXONGATE_SOURCE" \
  -d "{
    \"target_url\": \"$TARGET_URL\",
    \"tier\": \"fresh\",
    \"force_refresh\": true
  }"
```

Successful responses include:

```json
{
  "status": "success",
  "target_url": "https://example.com",
  "tier": "fresh",
  "markdown": "...",
  "cache": {"hit": false},
  "payment": {
    "mode": "x402-facilitator",
    "network": "eip155:8453"
  },
  "ueg_receipt": {
    "projected_profit_usdc": 0.01
  }
}
```

## Starter And Cached Tiers

The `starter` tier is the cheapest first-conversion path. It only serves the
starter sample target or already cached content, so it does not trigger supplier
work on a cache miss.

```bash
curl -i "$AXONGATE_BASE_URL/v1/x402/access?tier=starter"
```

The `cached` tier is cheaper and only succeeds when AxonGate already has a
cached copy from a previous `basic` or `deep` request. It will not trigger Jina
supplier work on a cache miss.

```bash
curl -i "$AXONGATE_BASE_URL/v1/x402/access?tier=cached"
```

## Retry A Paid Delivery

If AxonGate accepts payment but a retryable supplier or network outage prevents
delivery, it returns `503` with `X-AxonGate-Retry-Credit`.

```bash
export RETRY_CREDIT="<value from X-AxonGate-Retry-Credit>"

curl -sS -X POST "$AXONGATE_BASE_URL/v1/x402/retry" \
  -H "Content-Type: application/json" \
  -H "X-AxonGate-Retry-Credit: $RETRY_CREDIT" \
  -H "X-AxonGate-Source: $AXONGATE_SOURCE" \
  -d "{
    \"target_url\": \"$TARGET_URL\",
    \"tier\": \"basic\",
    \"force_refresh\": false
  }"
```

## Legacy Transaction Hash Route

Prefer the standard x402 route above. The legacy route exists for clients that
can only submit a settled Base USDC transaction hash.

```bash
export BASE_TX_HASH="<Base transaction hash>"

curl -sS -X POST "$AXONGATE_BASE_URL/v1/access" \
  -H "Content-Type: application/json" \
  -H "X-AxonGate-Payment-Hash: $BASE_TX_HASH" \
  -H "X-AxonGate-Source: $AXONGATE_SOURCE" \
  -d "{
    \"target_url\": \"$TARGET_URL\",
    \"tier\": \"basic\",
    \"force_refresh\": false
  }"
```

## Discovery And Telemetry

```bash
curl -sS "$AXONGATE_BASE_URL/manifest.json"
curl -sS "$AXONGATE_BASE_URL/quickstart"
curl -sS "$AXONGATE_BASE_URL/llms.txt"
curl -sS "$AXONGATE_BASE_URL/.well-known/x402"
curl -sS "$AXONGATE_BASE_URL/discovery/resources"
curl -sS "$AXONGATE_BASE_URL/metrics"
```

The `/metrics` response includes `conversion_funnel` and source-level
`attribution`, which track discovery, payment challenges, paid attempts,
accepted payments, successful deliveries, retry credits, Unit Economic
Guardian rejections, and supplier outcomes.
