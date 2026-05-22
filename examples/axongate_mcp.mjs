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
const proofPackPrices = {
  quick: "0.10",
  standard: "0.25",
  deep: "1.00",
};
const tierNames = Object.keys(prices);
const proofPackNames = Object.keys(proofPackPrices);
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
    throw new Error("Set AXONGATE_WALLET_FILE or pass wallet_file before using a paid AxonGate tool.");
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

function assertConfirmedProofPackSpend(pack, confirmSpendUsdc) {
  const expected = proofPackPrices[pack];
  const confirmed = String(confirmSpendUsdc || process.env.AXONGATE_PROOF_CONFIRM_SPEND || process.env.AXONGATE_CONFIRM_SPEND || "");
  if (confirmed !== expected) {
    throw new Error(`Refusing to spend. Confirm exactly ${expected} USDC for Proof Pack "${pack}".`);
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
    pack: requirement.extra?.pack || null,
    asset: requirement.asset,
    pay_to: requirement.payTo,
    network: requirement.network,
    extensions: Object.keys(paymentRequired.extensions || {}),
  };
}

function summarizeProofPack(body, maxAnswerChars, maxExcerptChars) {
  const json = body?.json || {};
  const answerLimit = Math.max(300, Math.min(Number(maxAnswerChars || 1800), 8000));
  const excerptLimit = Math.max(80, Math.min(Number(maxExcerptChars || 360), 1200));
  const citations = Array.isArray(json.citations)
    ? json.citations.map((citation) => ({
        id: citation.id,
        url: citation.url,
        excerpt:
          typeof citation.excerpt === "string" && citation.excerpt.length > excerptLimit
            ? `${citation.excerpt.slice(0, excerptLimit)}...`
            : citation.excerpt,
      }))
    : [];
  return {
    status: json.status,
    target_url: json.target_url,
    question: json.question,
    pack: json.pack,
    answer:
      typeof json.answer === "string" && json.answer.length > answerLimit
        ? `${json.answer.slice(0, answerLimit)}...`
        : json.answer,
    executive_summary: json.executive_summary,
    confidence_score: json.confidence_score,
    key_claims: json.key_claims,
    citations,
    citation_count: citations.length,
    risks: json.risks,
    source_profile: json.source_profile,
    cache: json.cache,
    llm_used: json.llm_used,
    llm_model: json.llm_model,
    fallback_reason: json.fallback_reason,
    payment: json.payment,
    ueg_receipt: json.ueg_receipt,
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
  "quote_proof_pack",
  {
    title: "Quote AxonGate Proof Pack",
    description: "Get no-spend Proof Pack pricing, exact x402 amount, cache availability, and buyer command for a public URL.",
    inputSchema: {
      target_url: z.string().url().default("https://www.iana.org/domains/reserved"),
      question: z.string().max(600).default("What does this source establish?"),
      pack: z.enum(proofPackNames).default("quick"),
      source: z.string().max(48).default(sourceDefault),
    },
  },
  async ({ target_url, question, pack, source }) => {
    const normalizedSource = normalizeSource(source);
    const endpoint = `${baseUrl()}/v1/proof-pack/quote?${new URLSearchParams({
      target_url,
      question,
      pack,
      source: normalizedSource,
    })}`;
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
  "probe_proof_pack_terms",
  {
    title: "Probe Proof Pack Payment Terms",
    description: "Fetch AxonGate's Proof Pack x402 payment requirement without spending USDC.",
    inputSchema: {
      pack: z.enum(proofPackNames).default("quick"),
      source: z.string().max(48).default(sourceDefault),
    },
  },
  async ({ pack, source }) => {
    const normalizedSource = normalizeSource(source);
    const endpoint = `${baseUrl()}/v1/x402/proof-pack?${new URLSearchParams({ pack, source: normalizedSource })}`;
    const response = await fetch(endpoint, {
      headers: {
        "X-AxonGate-Pack": pack,
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
      proof_pack: response.headers.get("X-AxonGate-Proof-Pack"),
      proof_pack_quote: response.headers.get("X-AxonGate-Proof-Pack-Quote"),
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

server.registerTool(
  "fetch_proof_pack",
  {
    title: "Fetch Proof Pack",
    description: "Pay AxonGate with x402 and return a citation-backed Proof Pack for a public URL.",
    inputSchema: {
      target_url: z.string().url(),
      question: z.string().max(600).default("What does this source establish?"),
      pack: z.enum(proofPackNames).default("quick"),
      force_refresh: z.boolean().default(false),
      wallet_file: z.string().optional(),
      confirm_spend_usdc: z.string().optional(),
      source: z.string().max(48).default(sourceDefault),
      payment_id: z.string().min(16).max(128).optional(),
      max_answer_chars: z.number().int().min(300).max(8000).default(1800),
      max_citation_excerpt_chars: z.number().int().min(80).max(1200).default(360),
    },
  },
  async ({
    target_url,
    question,
    pack,
    force_refresh,
    wallet_file,
    confirm_spend_usdc,
    source,
    payment_id,
    max_answer_chars,
    max_citation_excerpt_chars,
  }) => {
    assertConfirmedProofPackSpend(pack, confirm_spend_usdc);

    const normalizedSource = normalizeSource(source);
    const normalizedPaymentId = normalizePaymentId(payment_id, normalizedSource);
    const account = readWallet(wallet_file);
    const { client, httpClient } = makeX402HttpClient(account, normalizedPaymentId, normalizedSource);
    const endpoint = `${baseUrl()}/v1/x402/proof-pack?${new URLSearchParams({ pack, source: normalizedSource })}`;
    const payload = { target_url, question, pack, force_refresh };

    const challengeResponse = await fetch(endpoint, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-AxonGate-Pack": pack,
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
    const expectedUnits = decimalUsdcToUnits(proofPackPrices[pack]);
    if (challenge.amount_units !== expectedUnits || challenge.pack !== pack) {
      throw new Error(`Challenge mismatch. Expected ${expectedUnits} for ${pack}, got ${challenge.amount_units} for ${challenge.pack}.`);
    }

    const paymentPayload = await client.createPaymentPayload(paymentRequired);
    const paymentHeaders = httpClient.encodePaymentSignatureHeader(paymentPayload);
    const paidResponse = await fetch(endpoint, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-AxonGate-Pack": pack,
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
    const result = {
      ...summarizeProofPack(paidBody, max_answer_chars, max_citation_excerpt_chars),
      payment_response: settle,
      buyer: account.address,
      source: normalizedSource,
      payment_id: normalizedPaymentId,
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
