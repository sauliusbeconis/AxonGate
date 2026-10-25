#!/usr/bin/env node
// AxonGate MCP server: exposes read / batch read / search / answer / verify / scan /
// token check as MCP tools and pays for each call with x402 (USDC on Base) from a local wallet.
//
//   AXONGATE_PRIVATE_KEY=0x... AXONGATE_MAX_SPEND_USDC=1.00 npx axongate-mcp
//
// AXONGATE_MAX_SPEND_USDC caps total spend for the life of the process (default 0.50).

import { config as loadEnv } from "dotenv";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { privateKeyToAccount } from "viem/accounts";
import { registerExactEvmScheme } from "@x402/evm/exact/client";
import { wrapFetchWithPayment, x402Client } from "@x402/fetch";

loadEnv({ quiet: true });

const BASE_URL = (process.env.AXONGATE_BASE_URL || "https://api.axongate.one").replace(/\/$/, "");
const MAX_SPEND = Number(process.env.AXONGATE_MAX_SPEND_USDC || "0.50");
const PRICES = { read: 0.002, batch: 0.03, search: 0.004, scan: 0.002, answer: 0.015, verify: 0.02, token: 0.01 };

const rawKey = process.env.AXONGATE_PRIVATE_KEY || "";
if (!rawKey) {
  console.error("Set AXONGATE_PRIVATE_KEY to a Base wallet that holds a little USDC.");
  process.exit(1);
}
const account = privateKeyToAccount(rawKey.startsWith("0x") ? rawKey : `0x${rawKey}`);
const client = new x402Client();
registerExactEvmScheme(client, { signer: account });
const payFetch = wrapFetchWithPayment(fetch, client);

let spent = 0;

async function call(product, method, path, params) {
  const price = PRICES[product];
  if (spent + price > MAX_SPEND + 1e-9) {
    throw new Error(`Spend cap reached: ${spent.toFixed(3)} of ${MAX_SPEND} USDC used.`);
  }
  let url = `${BASE_URL}${path}`;
  const init = { method, headers: { "content-type": "application/json" } };
  if (method === "GET") {
    url += `?${new URLSearchParams(Object.entries(params).filter(([, v]) => v !== undefined).map(([k, v]) => [k, String(v)]))}`;
  } else {
    init.body = JSON.stringify(params);
  }
  const response = await payFetch(url, init);
  const body = await response.json().catch(() => ({}));
  if (response.ok && response.headers.get("payment-response")) {
    spent += price;
  }
  if (!response.ok) {
    throw new Error(`AxonGate ${response.status}: ${body.message || body.error || "request failed"} (not charged)`);
  }
  return { content: [{ type: "text", text: JSON.stringify(body, null, 2) }] };
}

const server = new McpServer({ name: "axongate", version: "2.1.0" });

server.registerTool(
  "axongate_read",
  { description: "Read a public web page as clean Markdown with a prompt-injection risk scan ($0.002).", inputSchema: { url: z.url(), max_chars: z.number().int().optional() } },
  (args) => call("read", "GET", "/v1/read", args),
);
server.registerTool(
  "axongate_read_batch",
  { description: "Read up to 20 public web pages in one call, each as clean Markdown with its own prompt-injection scan ($0.03).", inputSchema: { urls: z.array(z.url()).min(1).max(20), max_chars: z.number().int().optional() } },
  (args) => call("batch", "POST", "/v1/read/batch", args),
);
server.registerTool(
  "axongate_search",
  { description: "Search the live web; optionally read the top results ($0.004).", inputSchema: { q: z.string(), limit: z.number().int().min(1).max(10).optional(), read_top: z.number().int().min(0).max(3).optional() } },
  (args) => call("search", "GET", "/v1/search", args),
);
server.registerTool(
  "axongate_answer",
  { description: "Answer a question from live web sources with verbatim-checked citations ($0.015).", inputSchema: { question: z.string(), urls: z.array(z.url()).max(5).optional() } },
  (args) => call("answer", "POST", "/v1/answer", args),
);
server.registerTool(
  "axongate_verify",
  { description: "Fact-check a claim: verdict, confidence and verbatim-checked evidence ($0.02).", inputSchema: { claim: z.string(), urls: z.array(z.url()).max(5).optional() } },
  (args) => call("verify", "POST", "/v1/verify", args),
);
server.registerTool(
  "axongate_scan",
  { description: "Scan untrusted text for prompt injection before passing it to a model ($0.002).", inputSchema: { text: z.string() } },
  (args) => call("scan", "POST", "/v1/scan", args),
);

server.registerTool(
  "axongate_token_check",
  {
    description: "Check a crypto token before buying, swapping or accepting it: honeypot, taxes, owner powers, holder concentration and liquidity, as an avoid / caution / ok verdict ($0.01).",
    inputSchema: { address: z.string(), chain: z.enum(["base", "ethereum", "bsc", "arbitrum", "polygon", "optimism", "avalanche", "solana"]).optional() },
  },
  (args) => call("token", "GET", "/v1/token-check", args),
);

await server.connect(new StdioServerTransport());
console.error(`AxonGate MCP ready (payer ${account.address}, cap ${MAX_SPEND} USDC)`);
