# Distribution

Where AxonGate is listed, and what each listing should point at after the v2
relaunch. Prices and descriptions come from `axongate/config.py`; the live
catalog at `https://api.axongate.one/.well-known/x402` is always authoritative.

## Canonical endpoints

| Product | Endpoint | Price |
| --- | --- | --- |
| Safe Read | `GET/POST https://api.axongate.one/v1/read` | $0.002 |
| Web Search | `GET/POST https://api.axongate.one/v1/search` | $0.004 |
| Injection Scan | `POST https://api.axongate.one/v1/scan` | $0.002 |
| Grounded Answer | `POST https://api.axongate.one/v1/answer` | $0.015 |
| Claim Verify | `POST https://api.axongate.one/v1/verify` | $0.02 |

Legacy URLs that directories already list keep working and are counted per
path in `/metrics` (`counters.challenge_paths`):

- `/v1/x402/access`, `/v1/x402/starter` → Safe Read
- `/v1/x402/proof-pack` → Claim Verify
- `/from/{source}/v1/x402/...` → the same, attributed to `{source}`

## Coinbase x402 Bazaar (highest priority)

Bazaar is the index that agents built on CDP and agentic.market search. It only
lists an endpoint after a payment for it **settles through the CDP
facilitator**, and drops it after 30 days without a settlement.

1. Production uses the CDP facilitator whenever `CDP_API_KEY_ID` and
   `CDP_API_KEY_SECRET` are set (`/metrics` → `facilitator: coinbase-cdp`).
2. Make one paid call per canonical endpoint from a wallet that is **not** the
   payTo address (CDP rejects self-payment):

   ```bash
   python scripts/paid_smoke.py --account axongate-smoke-buyer --max-spend 0.05
   ```

   The script prints the `EXTENSION-RESPONSES` header; `bazaar.status` should be
   `success` or `processing`. Indexing can take 15 minutes.
3. Check the listing:

   ```bash
   curl -s "https://api.cdp.coinbase.com/platform/v2/x402/discovery/resources?limit=1000&offset=0" | grep -c api.axongate.one
   ```

4. Validate metadata at any time (no key needed):

   ```bash
   curl -s -X POST https://api.cdp.coinbase.com/platform/v2/x402/validate \
     -H 'content-type: application/json' \
     -d '{"resource":"https://api.axongate.one/v1/read","method":"GET"}'
   ```

To stay listed, at least one call per endpoint must settle every 30 days. Real
buyers keep it alive; otherwise re-run the smoke script monthly.

## Crawled directories

x402scan, x402-list, 402explorer, x402lens, Agent402 and similar indexers crawl
`/.well-known/x402`, `/openapi.json` and the 402 challenges on their own. They
pick up v2 automatically. Directories with a manual listing (x402-list,
PayAnAgent, Agora402, the402, agentic.market) should have their description and
price updated to the v2 catalog above.
