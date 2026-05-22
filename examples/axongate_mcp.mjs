import fs from "node:fs";
import crypto from "node:crypto";
import { config as loadEnv } from "dotenv";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { privateKeyToAccount } from "viem/accounts";
import { registerExactEvmScheme } from "@x402/evm/exact/client";
import { x402Client, x402HTTPClient } from "@x402/fetch";

loadEnv();

const DEFAULT_BASE_URL = "https://api.axongate.one";
const prices = {
  starter: "0.012",
  cached: "0.015",
  basic: "0.02",
  fresh: "0.03",
  deep: "0.05",
};
const tierNames = Object.keys(prices);
const sourceDefault = "mcp";

function baseUrl() {
  return (process.env.AXONGATE_BASE_URL || DEFAULT_BASE_URL).replace(/\/$/, "");
}

function decimalUsdcToUnits(value) {
  const [whole, fraction = ""] = value.split(".");
  return `${whole}${fraction.padEnd(6, "0").slice(0, 6)}`.replace(/^0+(?=\d)/, "") || "0";
}

function normalizeSource(value) {
  const normalized = String(value || sourceDefault)
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_.:-]+/g, "-")
    .replace(/^[-._:]+|[-._:]+$/g, "")
    .slice(0, 48);
  return normalized || sourceDefault;
}

function normalizePaymentId(value, source) {
  const normalized = String(value || `axongate_${source}_${crypto.randomUUID().replaceAll("-", "")}`)
    .trim()
    .replace(/[^a-zA-Z0-9_-]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .slice(0, 128);
  if (normalized.length < 16) {
    throw new Error("payment_id must be 16-128 characters after normalization.");
  }
  return normalized;
}

function readWallet(walletFile) {
  const filePath = walletFile || process.env.AXONGATE_WALLET_FILE;
  if (!filePath) {
    throw new Error("Set AXONGATE_WALLET_FILE or pass wallet_file before using fetch_clean_context.");
  }
  const wallet = JSON.parse(fs.readFileSync(filePath, "utf8"));
  const rawPrivateKey = wallet.private_key || wallet.privateKey;
  if (!rawPrivateKey) {
    throw new Error("Wallet JSON must include private_key or privateKey.");
  }
  return privateKeyToAccount(rawPrivateKey.startsWith("0x") ? rawPrivateKey : `0x${rawPrivateKey}`);
}

function assertConfirmedSpend(tier, confirmSpendUsdc) {
  const expected = prices[tier];
  const confirmed = String(confirmSpendUsdc || process.env.AXONGATE_CONFIRM_SPEND || "");
  if (confirmed !== expected) {
    throw new Error(`Refusing to spend. Confirm exactly ${expected} USDC for tier "${tier}".`);
  }
}

async function readJsonOrText(response) {
  const text = await response.text();
  try {
    return { text, json: JSON.parse(text) };
  } catch {
    return { text, json: null };
  }
}

function summarizePaymentRequired(paymentRequired) {
  const requirement = paymentRequired.accepts?.[0] || {};
  return {
    x402_version: paymentRequired.x402Version,
    error: paymentRequired.error,
    amount_units: requirement.amount,
    amount_usdc: requirement.extra?.price || null,
    tier: requirement.extra?.tier || null,
    asset: requirement.asset,
    pay_to: requirement.payTo,
    network: requirement.network,
    extensions: Object.keys(paymentRequired.extensions || {}),
  };
}

function makeX402HttpClient(account, paymentId, source) {
  const client = new x402Client();
  registerExactEvmScheme(client, { signer: account });
  client.registerExtension({
    key: "payment-identifier",
    async enrichPaymentPayload(paymentPayload, paymentRequired) {
      const declared = paymentRequired.extensions?.["payment-identifier"];
      if (!declared) {
        return paymentPayload;
      }
      return {
        ...paymentPayload,
        extensions: {
          ...(paymentPayload.extensions || {}),
          "payment-identifier": {
            ...declared,
            info: {
              ...(declared.info || {}),
              id: paymentId || normalizePaymentId(null, source),
            },
          },
        },
      };
    },
  });
  return { client, httpClient: new x402HTTPClient(client) };
}

function truncateMarkdown(markdown, maxChars) {
  if (typeof markdown !== "string") {
    return { markdown: "", truncated: false, markdown_chars: 0 };
  }
  const limit = Math.max(1000, Math.min(Number(maxChars || 12000), 50000));
  return {
    markdown: markdown.length > limit ? markdown.slice(0, limit) : markdown,
    truncated: markdown.length > limit,
    markdown_chars: markdown.length,
  };
}

const server = new McpServer({
  name: "axongate",
  version: "1.2.0",
});

server.registerTool(
  "quote_clean_context",
  {
    title: "Quote AxonGate Clean Context",
    description: "Get no-spend tier guidance, exact x402 amounts, and a buyer command for a public URL.",
    inputSchema: {
      target_url: z.string().url().default("https://www.iana.org/domains/reserved"),
      source: z.string().max(48).default(sourceDefault),
    },
  },
  async ({ target_url, source }) => {
    const normalizedSource = normalizeSource(source);
    const endpoint = `${baseUrl()}/v1/x402/quote?${new URLSearchParams({ target_url, source: normalizedSource })}`;
    const response = await fetch(endpoint);
    const body = await readJsonOrText(response);
    const result = {
      endpoint,
      http_status: response.status,
      quote: body.json || body.text.slice(0, 1200),
    };
    return {
      isError: !response.ok,
      content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
    };
  },
);

server.registerTool(
  "probe_payment_terms",
  {
    title: "Probe AxonGate Payment Terms",
    description: "Fetch AxonGate's x402 payment requirement without spending USDC.",
    inputSchema: {
      tier: z.enum(tierNames).default("starter"),
      source: z.string().max(48).default(sourceDefault),
    },
  },
  async ({ tier, source }) => {
    const normalizedSource = normalizeSource(source);
    const endpoint = `${baseUrl()}/v1/x402/access?${new URLSearchParams({ tier, source: normalizedSource })}`;
    const response = await fetch(endpoint, {
      headers: {
        "X-AxonGate-Tier": tier,
        "X-AxonGate-Source": normalizedSource,
      },
    });
    const body = await readJsonOrText(response);
    const paymentRequiredHeader = response.headers.get("PAYMENT-REQUIRED") || response.headers.get("X-Payment-Required");
    const paymentRequired = paymentRequiredHeader
      ? JSON.parse(Buffer.from(paymentRequiredHeader, "base64").toString("utf8"))
      : null;
    const result = {
      endpoint,
      http_status: response.status,
      payment_required: paymentRequired ? summarizePaymentRequired(paymentRequired) : null,
      next_step: response.headers.get("X-AxonGate-Next-Step"),
      paid_test: response.headers.get("X-AxonGate-Paid-Test"),
      buyer_example: response.headers.get("X-AxonGate-Buyer-Example"),
      body: body.json || body.text.slice(0, 1200),
    };
    return {
      content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
    };
  },
);

server.registerTool(
  "fetch_clean_context",
  {
    title: "Fetch Clean Context",
    description: "Pay AxonGate with x402 and return clean Markdown for a public URL.",
    inputSchema: {
      target_url: z.string().url(),
      tier: z.enum(tierNames).default("fresh"),
      force_refresh: z.boolean().default(false),
      wallet_file: z.string().optional(),
      confirm_spend_usdc: z.string().optional(),
      source: z.string().max(48).default(sourceDefault),
      payment_id: z.string().min(16).max(128).optional(),
      max_markdown_chars: z.number().int().min(1000).max(50000).default(12000),
    },
  },
  async ({ target_url, tier, force_refresh, wallet_file, confirm_spend_usdc, source, payment_id, max_markdown_chars }) => {
    assertConfirmedSpend(tier, confirm_spend_usdc);

    const normalizedSource = normalizeSource(source);
    const normalizedPaymentId = normalizePaymentId(payment_id, normalizedSource);
    const account = readWallet(wallet_file);
    const { client, httpClient } = makeX402HttpClient(account, normalizedPaymentId, normalizedSource);
    const endpoint = `${baseUrl()}/v1/x402/access?${new URLSearchParams({ tier, source: normalizedSource })}`;
    const payload = { target_url, tier, force_refresh };

    const challengeResponse = await fetch(endpoint, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-AxonGate-Tier": tier,
        "X-AxonGate-Source": normalizedSource,
      },
      body: JSON.stringify(payload),
    });
    const challengeBody = await readJsonOrText(challengeResponse);
    const paymentRequired = httpClient.getPaymentRequiredResponse(
      (name) => challengeResponse.headers.get(name),
      challengeBody.json,
    );
    const challenge = summarizePaymentRequired(paymentRequired);
    const expectedUnits = decimalUsdcToUnits(prices[tier]);
    if (challenge.amount_units !== expectedUnits || challenge.tier !== tier) {
      throw new Error(`Challenge mismatch. Expected ${expectedUnits} for ${tier}, got ${challenge.amount_units} for ${challenge.tier}.`);
    }

    const paymentPayload = await client.createPaymentPayload(paymentRequired);
    const paymentHeaders = httpClient.encodePaymentSignatureHeader(paymentPayload);
    const paidResponse = await fetch(endpoint, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-AxonGate-Tier": tier,
        "X-AxonGate-Source": normalizedSource,
        ...paymentHeaders,
      },
      body: JSON.stringify(payload),
    });
    const paidBody = await readJsonOrText(paidResponse);
    if (!paidResponse.ok) {
      return {
        isError: true,
        content: [
          {
            type: "text",
            text: JSON.stringify(
              {
                http_status: paidResponse.status,
                challenge,
                detail: paidBody.json || paidBody.text.slice(0, 1200),
              },
              null,
              2,
            ),
          },
        ],
      };
    }

    const settle = httpClient.getPaymentSettleResponse((name) => paidResponse.headers.get(name));
    const markdown = truncateMarkdown(paidBody.json?.markdown, max_markdown_chars);
    const result = {
      status: paidBody.json?.status,
      target_url: paidBody.json?.target_url,
      tier: paidBody.json?.tier,
      cache: paidBody.json?.cache,
      payment: paidBody.json?.payment,
      ueg_receipt: paidBody.json?.ueg_receipt,
      payment_response: settle,
      buyer: account.address,
      source: normalizedSource,
      payment_id: normalizedPaymentId,
      ...markdown,
    };
    return {
      content: [
        {
          type: "text",
          text: JSON.stringify(result, null, 2),
        },
      ],
    };
  },
);

await server.connect(new StdioServerTransport());
console.error("AxonGate MCP server running on stdio.");
