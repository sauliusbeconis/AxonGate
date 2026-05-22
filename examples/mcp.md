# AxonGate MCP Server

This server exposes AxonGate as MCP tools for agent clients.

It has three tools:

- `quote_clean_context`: gets no-spend tier guidance and a ready buyer command.
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
        "AXONGATE_CONFIRM_SPEND": "0.012"
      }
    }
  }
}
```

## First Calls

Quote first:

```json
{
  "target_url": "https://www.iana.org/domains/reserved",
  "source": "mcp"
}
```

Probe first:

```json
{
  "tier": "starter",
  "source": "mcp"
}
```

Paid fetch:

```json
{
  "target_url": "https://www.iana.org/domains/reserved",
  "tier": "starter",
  "force_refresh": false,
  "confirm_spend_usdc": "0.012",
  "source": "mcp",
  "max_markdown_chars": 12000
}
```

The `starter` tier is for first conversion on the sample target or existing
cache. Use `fresh` with `confirm_spend_usdc: "0.03"` for live public web
context.

The paid tool refuses to spend unless `confirm_spend_usdc` or
`AXONGATE_CONFIRM_SPEND` exactly matches the selected tier price.
