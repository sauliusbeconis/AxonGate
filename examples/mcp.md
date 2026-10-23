# AxonGate MCP server

`axongate_mcp.mjs` exposes AxonGate as five MCP tools and pays for each call
with x402 from a local wallet:

| Tool | Price |
| --- | --- |
| `axongate_read` — URL → clean Markdown + injection scan | $0.002 |
| `axongate_search` — live web search, optional top-result reads | $0.004 |
| `axongate_scan` — prompt-injection scan of untrusted text | $0.002 |
| `axongate_answer` — grounded answer with verbatim-checked citations | $0.015 |
| `axongate_verify` — claim verdict with verbatim-checked evidence | $0.02 |

Failed calls are never charged. `AXONGATE_MAX_SPEND_USDC` caps total spend per
process (default 0.50).

## Setup

```bash
npm install
```

Use a dedicated wallet holding a few dollars of USDC on Base (no ETH needed).

```json
{
  "mcpServers": {
    "axongate": {
      "command": "node",
      "args": ["/absolute/path/to/AxonGate/examples/axongate_mcp.mjs"],
      "env": {
        "AXONGATE_PRIVATE_KEY": "0x...",
        "AXONGATE_MAX_SPEND_USDC": "1.00"
      }
    }
  }
}
```

Claude Code:

```bash
claude mcp add axongate -e AXONGATE_PRIVATE_KEY=0x... -e AXONGATE_MAX_SPEND_USDC=1.00 \
  -- node /absolute/path/to/AxonGate/examples/axongate_mcp.mjs
```
