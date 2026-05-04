# AxonGate MCP Server

This server exposes AxonGate as MCP tools for agent clients.

It has two tools:

- `probe_payment_terms`: fetches the x402 challenge without spending USDC.
- `fetch_clean_context`: pays AxonGate with x402 and returns clean Markdown.

## Install

```bash
npm install
```

## Configure

Use a burner wallet JSON with `private_key` or `privateKey`.

```json
{
  "mcpServers": {
    "axongate": {
      "command": "node",
      "args": ["C:/path/to/AxonGate-Vault/examples/axongate_mcp.mjs"],
      "env": {
        "AXONGATE_BASE_URL": "https://web-production-8136ee.up.railway.app",
        "AXONGATE_WALLET_FILE": "C:/path/to/burner_wallet.json",
        "AXONGATE_CONFIRM_SPEND": "0.03"
      }
    }
  }
}
```

## First Calls

Probe first:

```json
{
  "tier": "fresh",
  "source": "mcp"
}
```

Paid fetch:

```json
{
  "target_url": "https://www.iana.org/domains/reserved",
  "tier": "fresh",
  "force_refresh": true,
  "confirm_spend_usdc": "0.03",
  "source": "mcp",
  "max_markdown_chars": 12000
}
```

The paid tool refuses to spend unless `confirm_spend_usdc` or
`AXONGATE_CONFIRM_SPEND` exactly matches the selected tier price.
