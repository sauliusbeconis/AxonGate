"""Runtime configuration, read once from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal

from dotenv import load_dotenv

load_dotenv()

VERSION = "2.1.0"


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name, "true" if default else "false").lower()
    return raw in {"1", "true", "yes", "on"}


PUBLIC_BASE_URL = _env("AXONGATE_PUBLIC_BASE_URL", "https://api.axongate.one").rstrip("/")
GITHUB_REPO_URL = _env("AXONGATE_GITHUB_REPO_URL", "https://github.com/sauliusbeconis/AxonGate")
CONTACT_EMAIL = _env("AXONGATE_CONTACT_EMAIL", "hello@axongate.one")

# Payments
NETWORK = "eip155:8453"
BASE_USDC_ADDRESS = _env("BASE_USDC_ADDRESS", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")
DEFAULT_PAY_TO = "0xcD11393c8505C5A44F8b998E0c96BcC5698d76A7"
PAY_TO = _env("AXONGATE_VAULT_ADDRESS", DEFAULT_PAY_TO)
if not PAY_TO.startswith("0x") or len(PAY_TO) != 42:
    PAY_TO = DEFAULT_PAY_TO

CDP_API_KEY_ID = _env("CDP_API_KEY_ID")
CDP_API_KEY_SECRET = _env("CDP_API_KEY_SECRET")
# "cdp" settles through Coinbase (required for Bazaar indexing); "payai" is the keyless fallback.
FACILITATOR = _env("AXONGATE_FACILITATOR", "cdp" if CDP_API_KEY_ID and CDP_API_KEY_SECRET else "payai").lower()
PAYAI_FACILITATOR_URL = _env("PAYAI_FACILITATOR_URL", "https://facilitator.payai.network")
X402_ENABLED = _env_bool("AXONGATE_X402_ENABLED", True)


@dataclass(frozen=True)
class Rail:
    """One way to pay: a network, its USDC asset and the address that receives it."""

    network: str  # CAIP-2
    label: str
    asset: str
    pay_to: str
    extra: tuple[tuple[str, str], ...] = ()  # EIP-712 domain for EVM USDC


_USDC_DOMAIN = (("name", "USD Coin"), ("version", "2"))
_EVM_RAILS = {
    "polygon": Rail("eip155:137", "Polygon", "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", PAY_TO, _USDC_DOMAIN),
    "arbitrum": Rail("eip155:42161", "Arbitrum", "0xaf88d065e77c8cC2239327C5EDb3A432268e5831", PAY_TO, _USDC_DOMAIN),
}
SOLANA_MAINNET = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
SOLANA_USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
# The Solana payTo must already own a USDC token account: x402 clients transfer into it but never create it.
SOLANA_PAY_TO = _env("AXONGATE_SOLANA_PAY_TO")

# Base is always first (it is what Bazaar and most buyers use). The other rails need the CDP facilitator.
RAILS: tuple[Rail, ...] = (Rail(NETWORK, "Base", BASE_USDC_ADDRESS, PAY_TO, _USDC_DOMAIN),)
if FACILITATOR == "cdp":
    for _name in (n.strip().lower() for n in _env("AXONGATE_EXTRA_NETWORKS", "polygon,arbitrum").split(",")):
        if _name in _EVM_RAILS:
            RAILS += (_EVM_RAILS[_name],)
    if SOLANA_PAY_TO:
        RAILS += (Rail(SOLANA_MAINNET, "Solana", SOLANA_USDC_MINT, SOLANA_PAY_TO),)
NETWORK_LABELS = ", ".join(r.label for r in RAILS)


@dataclass(frozen=True)
class Product:
    key: str
    path: str
    price: Decimal
    title: str
    summary: str
    tags: tuple[str, ...]


PRODUCTS: dict[str, Product] = {
    "read": Product(
        key="read",
        path="/v1/read",
        price=Decimal(_env("AXONGATE_PRICE_READ_USDC", "0.002")),
        title="Safe Read",
        summary=(
            "Fetch one public web page and return clean, LLM-ready Markdown with title, word count, "
            "content hash and a prompt-injection risk scan of the page text. Use it when an agent needs "
            "to read a URL without ingesting scripts, boilerplate or hidden instructions."
        ),
        tags=("web-read", "markdown", "scrape", "prompt-injection", "rag"),
    ),
    "search": Product(
        key="search",
        path="/v1/search",
        price=Decimal(_env("AXONGATE_PRICE_SEARCH_USDC", "0.004")),
        title="Web Search",
        summary=(
            "Live web search for agents. Returns ranked results with title, URL and snippet; optionally "
            "reads the top results into clean Markdown in the same call. Use it to find current sources "
            "before answering or citing."
        ),
        tags=("search", "web-search", "serp", "research", "rag"),
    ),
    "answer": Product(
        key="answer",
        path="/v1/answer",
        price=Decimal(_env("AXONGATE_PRICE_ANSWER_USDC", "0.015")),
        title="Grounded Answer",
        summary=(
            "Answer a question from live web sources. AxonGate searches, reads the best pages, and returns "
            "a concise answer with numbered citations whose quotes are checked verbatim against the fetched "
            "pages. Use it when an agent needs a sourced answer, not just links."
        ),
        tags=("answer", "citations", "research", "grounding", "rag"),
    ),
    "verify": Product(
        key="verify",
        path="/v1/verify",
        price=Decimal(_env("AXONGATE_PRICE_VERIFY_USDC", "0.02")),
        title="Claim Verify",
        summary=(
            "Fact-check one claim against live web sources (or URLs you supply). Returns a verdict "
            "(supported, refuted, mixed, insufficient), a confidence score and supporting or contradicting "
            "quotes that are checked verbatim against the fetched pages. Use it before an agent repeats a claim."
        ),
        tags=("fact-check", "verify", "claims", "citations", "trust"),
    ),
    "scan": Product(
        key="scan",
        path="/v1/scan",
        price=Decimal(_env("AXONGATE_PRICE_SCAN_USDC", "0.002")),
        title="Injection Scan",
        summary=(
            "Scan untrusted text (a web page, email, tool output or document) for prompt-injection and "
            "agent-hijacking patterns before it reaches an LLM. Returns a risk level, score, findings with "
            "offsets and a sanitized copy. Deterministic, sub-50ms."
        ),
        tags=("prompt-injection", "security", "agent-safety", "guardrails", "scan"),
    ),
    "token": Product(
        key="token",
        path="/v1/token-check",
        price=Decimal(_env("AXONGATE_PRICE_TOKEN_USDC", "0.01")),
        title="Token Check",
        summary=(
            "Safety verdict for a crypto token before an agent buys, swaps or accepts it: honeypot, sell and "
            "buy taxes, owner powers (mint, blacklist, balance changes), holder concentration and DEX "
            "liquidity, combined into avoid / caution / ok with reasons. EVM chains and Solana."
        ),
        tags=("crypto", "token-security", "honeypot", "rug-check", "agent-safety"),
    ),
    "batch": Product(
        key="batch",
        path="/v1/read/batch",
        price=Decimal(_env("AXONGATE_PRICE_BATCH_USDC", "0.03")),
        title="Batch Read",
        summary=(
            "Read up to 20 public URLs in one paid call. Each page comes back as clean Markdown with its own "
            "prompt-injection scan; pages that fail are reported per URL without failing the batch. Use it "
            "to load a reading list or search results in one round trip."
        ),
        tags=("web-read", "markdown", "batch", "scrape", "rag"),
    ),
}

# Legacy x402 URLs that marketplaces still list; they now serve the v2 products.
LEGACY_ALIASES: dict[str, str] = {
    "/v1/x402/access": "read",
    "/v1/x402/starter": "read",
    "/v1/x402/proof-pack": "verify",
}
SOURCE_ALIAS_PATHS = (
    "x402-list",
    "payanagent",
    "payanagent-starter",
    "agora402",
    "agent-bazaar",
    "the402",
    "x402-eco",
    "402agents",
    "github",
)

# Suppliers
JINA_API_KEY = _env("JINA_API_KEY")
JINA_READER_BASE_URL = _env("JINA_READER_BASE_URL", "https://r.jina.ai").rstrip("/")
JINA_SEARCH_BASE_URL = _env("JINA_SEARCH_BASE_URL", "https://s.jina.ai").rstrip("/")
JINA_TIMEOUT_SECONDS = _env_float("JINA_TIMEOUT_SECONDS", 20.0)

LLM_API_KEY = _env("AXONGATE_LLM_API_KEY")
LLM_ENABLED = _env_bool("AXONGATE_LLM_ENABLED", bool(LLM_API_KEY)) and bool(LLM_API_KEY)
LLM_MODEL = _env("AXONGATE_LLM_MODEL", "gpt-5-mini")
LLM_BASE_URL = _env("AXONGATE_LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
LLM_TIMEOUT_SECONDS = _env_float("AXONGATE_LLM_TIMEOUT_SECONDS", 40.0)

# Fetching
FETCH_TIMEOUT_SECONDS = _env_float("AXONGATE_FETCH_TIMEOUT_SECONDS", 12.0)
FETCH_MAX_BYTES = _env_int("AXONGATE_FETCH_MAX_BYTES", 5 * 1024 * 1024)
FETCH_MAX_REDIRECTS = _env_int("AXONGATE_FETCH_MAX_REDIRECTS", 4)
ALLOWED_PORTS = {int(p) for p in _env("AXONGATE_ALLOWED_TARGET_PORTS", "80,443").split(",") if p.strip().isdigit()}
USER_AGENT = f"AxonGateBot/{VERSION.split('.')[0]}.0 (+{PUBLIC_BASE_URL}/bot)"

# Storage and limits
REDIS_URL = _env("REDIS_URL")
REDIS_MAX_CONNECTIONS = _env_int("AXONGATE_REDIS_MAX_CONNECTIONS", 16)
CACHE_TTL_SECONDS = _env_int("AXONGATE_CACHE_TTL_SECONDS", 3600)
RATE_LIMIT_PER_MINUTE = _env_int("AXONGATE_RATE_LIMIT_PER_MINUTE", 120)
PAID_CONCURRENCY = _env_int("AXONGATE_PAID_CONCURRENCY", 24)
OPERATOR_TOKEN = _env("AXONGATE_OPERATOR_TOKEN")
# Optional GoPlus Security app key for /v1/token-check (keyless access is heavily rate-limited).
GOPLUS_APP_KEY = _env("GOPLUS_APP_KEY")
GOPLUS_APP_SECRET = _env("GOPLUS_APP_SECRET")
# One-time domain-proof token for x402-list.com owner updates (served at /.well-known/x402list.txt).
X402LIST_TOKEN = _env("AXONGATE_X402LIST_TOKEN")
