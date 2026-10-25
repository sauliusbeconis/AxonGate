# axongate-mcp

MCP server for [AxonGate](https://api.axongate.one): safe web access and token checks for AI agents,
paid per call in USDC on Base over [x402](https://x402.org). No account or API key; failed calls are
never charged.

| Tool | What it does | Price |
| --- | --- | --- |
| `axongate_read` | URL → clean Markdown + prompt-injection scan | $0.002 |
| `axongate_read_batch` | Up to 20 URLs in one call, each scanned | $0.03 |
| `axongate_search` | Live web search, optionally reading the top results | $0.004 |
| `axongate_scan` | Prompt-injection scan of untrusted text | $0.002 |
| `axongate_answer` | Answer with verbatim-checked citations | $0.015 |
| `axongate_verify` | Claim verdict with verbatim-checked evidence | $0.02 |
| `axongate_token_check` | Crypto token safety verdict (honeypot, taxes, owner powers, liquidity) | $0.01 |

## Setup

Use a dedicated wallet holding a few dollars of USDC on Base. No ETH is needed: the facilitator pays gas.
`AXONGATE_MAX_SPEND_USDC` caps total spend per server process (default 0.50).

Claude Code:

```bash
claude mcp add axongate -e AXONGATE_PRIVATE_KEY=0x... -e AXONGATE_MAX_SPEND_USDC=1.00 -- npx -y axongate-mcp
```

Claude Desktop, Cursor and other MCP clients:

```json
{
  "mcpServers": {
    "axongate": {
      "command": "npx",
      "args": ["-y", "axongate-mcp"],
      "env": { "AXONGATE_PRIVATE_KEY": "0x...", "AXONGATE_MAX_SPEND_USDC": "1.00" }
    }
  }
}
```

Token Check data is powered by GoPlus Security, with honeypot.is simulations where available.
Its verdicts are automated risk signals, not financial advice.
