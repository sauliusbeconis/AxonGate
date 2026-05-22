import fs from "node:fs";
import crypto from "node:crypto";
import { parseArgs } from "node:util";
import { config as loadEnv } from "dotenv";
import { privateKeyToAccount } from "viem/accounts";
import { registerExactEvmScheme } from "@x402/evm/exact/client";
import { x402Client, x402HTTPClient } from "@x402/fetch";

loadEnv();

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

const { values } = parseArgs({
  options: {
    "base-url": { type: "string" },
    product: { type: "string" },
    "target-url": { type: "string" },
    tier: { type: "string" },
    pack: { type: "string" },
    question: { type: "string" },
    "force-refresh": { type: "boolean", default: false },
    "wallet-file": { type: "string" },
    "confirm-spend": { type: "string" },
    source: { type: "string" },
    "payment-id": { type: "string" },
    replay: { type: "boolean", default: false },
    "print-markdown": { type: "boolean", default: false },
    timeout: { type: "string" },
  },
  allowPositionals: false,
});

const product = (values.product || process.env.AXONGATE_PRODUCT || "clean-context").toLowerCase();
if (!["clean-context", "proof-pack"].includes(product)) {
  throw new Error('Unsupported product. Use "clean-context" or "proof-pack".');
}

const tier = (values.tier || process.env.AXONGATE_TIER || "starter").toLowerCase();
const pack = (values.pack || process.env.AXONGATE_PROOF_PACK || "standard").toLowerCase();
const expectedSpend = product === "proof-pack" ? proofPackPrices[pack] : prices[tier];
if (!expectedSpend && product === "clean-context") {
  throw new Error(`Unsupported tier "${tier}". Use one of: ${Object.keys(prices).join(", ")}.`);
}
if (!expectedSpend && product === "proof-pack") {
  throw new Error(`Unsupported Proof Pack "${pack}". Use one of: ${Object.keys(proofPackPrices).join(", ")}.`);
}

const confirmSpend = values["confirm-spend"] || process.env.AXONGATE_CONFIRM_SPEND;
if (confirmSpend !== expectedSpend) {
  throw new Error(`Set AXONGATE_CONFIRM_SPEND=${expectedSpend} or pass --confirm-spend ${expectedSpend}.`);
}

const walletFile = values["wallet-file"] || process.env.AXONGATE_WALLET_FILE;
if (!walletFile) {
  throw new Error("Pass --wallet-file or set AXONGATE_WALLET_FILE to a JSON file with private_key.");
}

const wallet = JSON.parse(fs.readFileSync(walletFile, "utf8"));
const rawPrivateKey = wallet.private_key || wallet.privateKey;
if (!rawPrivateKey) {
  throw new Error("Wallet JSON must include private_key or privateKey.");
}

const privateKey = rawPrivateKey.startsWith("0x") ? rawPrivateKey : `0x${rawPrivateKey}`;
const account = privateKeyToAccount(privateKey);
const baseUrl = (values["base-url"] || process.env.AXONGATE_BASE_URL || "https://api.axongate.one").replace(/\/$/, "");
const targetUrl = values["target-url"] || process.env.AXONGATE_TARGET_URL || "https://example.com";
const forceRefresh = Boolean(values["force-refresh"] || process.env.AXONGATE_FORCE_REFRESH === "true");
const retryEndpoint = `${baseUrl}/v1/x402/retry`;
const timeoutMs = Number(values.timeout || process.env.AXONGATE_CLIENT_TIMEOUT_MS || 45000);
const source = normalizeSource(values.source || process.env.AXONGATE_SOURCE || "paid_buyer");
const question = values.question || process.env.AXONGATE_PROOF_QUESTION || "What does this source establish?";
const paymentId = normalizePaymentId(
  values["payment-id"] || process.env.AXONGATE_PAYMENT_ID || `axongate_${source}_${crypto.randomUUID().replaceAll("-", "")}`,
);
const query = product === "proof-pack" ? new URLSearchParams({ pack, source }) : new URLSearchParams({ tier, source });
const endpointPath = product === "proof-pack" ? "/v1/x402/proof-pack" : "/v1/x402/access";
const endpoint = `${baseUrl}${endpointPath}?${query.toString()}`;

const payload = product === "proof-pack"
  ? {
      target_url: targetUrl,
      question,
      pack,
      force_refresh: forceRefresh,
    }
  : {
      target_url: targetUrl,
      tier,
      force_refresh: forceRefresh,
    };

function decimalUsdcToUnits(value) {
  const [whole, fraction = ""] = value.split(".");
  return `${whole}${fraction.padEnd(6, "0").slice(0, 6)}`.replace(/^0+(?=\d)/, "");
}

function normalizeSource(value) {
  const normalized = String(value || "paid_buyer")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_.:-]+/g, "-")
    .replace(/^[-._:]+|[-._:]+$/g, "")
    .slice(0, 48);
  return normalized || "paid_buyer";
}

function normalizePaymentId(value) {
  const normalized = String(value || "")
    .trim()
    .replace(/[^a-zA-Z0-9_-]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .slice(0, 128);
  if (normalized.length < 16) {
    throw new Error("Payment id must be 16-128 characters after normalization.");
  }
  return normalized;
}

async function fetchWithTimeout(url, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } finally {
    clearTimeout(timer);
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

function summarizeBody(body) {
  if (!body.json) {
    return { body: body.text.slice(0, 1000) };
  }

  return {
    status: body.json.status,
    target_url: body.json.target_url,
    tier: body.json.tier,
    pack: body.json.pack,
    markdown_chars: typeof body.json.markdown === "string" ? body.json.markdown.length : 0,
    answer_chars: typeof body.json.answer === "string" ? body.json.answer.length : 0,
    citation_count: Array.isArray(body.json.citations) ? body.json.citations.length : 0,
    confidence_score: body.json.confidence_score,
    llm_used: body.json.llm_used,
    llm_model: body.json.llm_model,
    fallback_reason: body.json.fallback_reason,
    cache: body.json.cache,
    payment: body.json.payment,
    ueg_receipt: body.json.ueg_receipt,
    detail: body.json.detail,
  };
}

function printMarkdownIfRequested(body) {
  if (!values["print-markdown"] && process.env.AXONGATE_PRINT_MARKDOWN !== "true") {
    return;
  }
  if (typeof body.json?.markdown === "string") {
    console.log("MARKDOWN");
    console.log(body.json.markdown);
  }
}

function paymentSummary(paymentRequired) {
  const requirement = paymentRequired.accepts[0];
  return {
    amount: requirement.amount,
    asset: requirement.asset,
    payTo: requirement.payTo,
    network: requirement.network,
    extra: requirement.extra,
    extensions: Object.keys(paymentRequired.extensions || {}),
  };
}

function productHeaders() {
  return product === "proof-pack"
    ? {
        "X-AxonGate-Pack": pack,
        "X-AxonGate-Source": source,
      }
    : {
        "X-AxonGate-Tier": tier,
        "X-AxonGate-Source": source,
      };
}

async function submitWithRetryCreditIfNeeded(response, body) {
  const retryCredit = response.headers.get("X-AxonGate-Retry-Credit");
  if (response.status !== 503 || !retryCredit) {
    return { response, body, usedRetryCredit: false };
  }

  const retryResponse = await fetchWithTimeout(retryEndpoint, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-AxonGate-Retry-Credit": retryCredit,
      "X-AxonGate-Source": source,
    },
    body: JSON.stringify(payload),
  });
  return {
    response: retryResponse,
    body: await readJsonOrText(retryResponse),
    usedRetryCredit: true,
  };
}

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
            id: paymentId,
          },
        },
      },
    };
  },
});
const httpClient = new x402HTTPClient(client);

console.log(JSON.stringify({
  buyer: account.address,
  product,
  endpoint,
  target_url: targetUrl,
  tier: product === "clean-context" ? tier : undefined,
  pack: product === "proof-pack" ? pack : undefined,
  question: product === "proof-pack" ? question : undefined,
  force_refresh: forceRefresh,
  source,
  payment_id: paymentId,
  authorized_spend_usdc: expectedSpend,
}, null, 2));

const challengeResponse = await fetchWithTimeout(endpoint, {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    ...productHeaders(),
  },
  body: JSON.stringify(payload),
});
const challengeBody = await readJsonOrText(challengeResponse);
const paymentRequired = httpClient.getPaymentRequiredResponse(
  (name) => challengeResponse.headers.get(name),
  challengeBody.json,
);
const summary = paymentSummary(paymentRequired);
console.log("CHALLENGE");
console.log(JSON.stringify({ status: challengeResponse.status, ...summary }, null, 2));

const expectedUnits = decimalUsdcToUnits(expectedSpend);
if (product === "proof-pack" && (summary.amount !== expectedUnits || summary.extra?.pack !== pack)) {
  throw new Error(`Challenge mismatch. Expected ${expectedUnits} for ${pack}, got ${summary.amount} for ${summary.extra?.pack}.`);
}
if (product === "clean-context" && (summary.amount !== expectedUnits || summary.extra?.tier !== tier)) {
  throw new Error(`Challenge mismatch. Expected ${expectedUnits} for ${tier}, got ${summary.amount} for ${summary.extra?.tier}.`);
}

const paymentPayload = await client.createPaymentPayload(paymentRequired);
const paymentHeaders = httpClient.encodePaymentSignatureHeader(paymentPayload);

async function submitPaid(label) {
  const response = await fetchWithTimeout(endpoint, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...productHeaders(),
      ...paymentHeaders,
    },
    body: JSON.stringify(payload),
  });
  const body = await readJsonOrText(response);
  const delivered = await submitWithRetryCreditIfNeeded(response, body);
  console.log(label);
  console.log(JSON.stringify({
    http_status: delivered.response.status,
    used_retry_credit: delivered.usedRetryCredit,
    ...summarizeBody(delivered.body),
  }, null, 2));
  printMarkdownIfRequested(delivered.body);

  if (delivered.response.ok) {
    const settleResponse = httpClient.getPaymentSettleResponse((name) => delivered.response.headers.get(name));
    console.log("PAYMENT_RESPONSE");
    console.log(JSON.stringify(settleResponse, null, 2));
  }

  return delivered;
}

const paid = await submitPaid("PAID");
if (!paid.response.ok) {
  process.exitCode = 1;
}

if (values.replay || process.env.AXONGATE_REPLAY === "true") {
  const replay = await submitPaid("REPLAY");
  if (replay.response.ok) {
    throw new Error("Replay unexpectedly succeeded.");
  }
}
