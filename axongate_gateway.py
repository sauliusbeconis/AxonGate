import asyncio
import base64
import hashlib
import html
import ipaddress
import inspect
import json
import os
import re
import secrets
import socket
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote as url_quote, urljoin, urlparse

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from jsonschema import ValidationError as JsonSchemaValidationError
from jsonschema import validate as validate_json_schema
from pydantic import BaseModel, Field
from web3 import Web3
from web3.exceptions import TransactionNotFound

try:
    from cdp import CdpClient
    from cdp.evm_client import EvmClient
except ImportError:  # pragma: no cover - Railway installs cdp-sdk from requirements.txt.
    CdpClient = None
    EvmClient = None

try:
    import redis.asyncio as redis
except ImportError:  # pragma: no cover - optional when REDIS_URL is unset.
    redis = None

try:
    from x402.http import FacilitatorConfig, HTTPFacilitatorClient, PaymentOption
    from x402.http.middleware.fastapi import PaymentMiddlewareASGI
    from x402.http.types import HTTPRequestContext, RouteConfig
    from x402.mechanisms.evm.exact import ExactEvmServerScheme
    from x402.schemas import AssetAmount
    from x402.server import x402ResourceServer
except ImportError:  # pragma: no cover - Railway installs x402 from requirements.txt.
    FacilitatorConfig = None
    HTTPFacilitatorClient = None
    PaymentOption = None
    PaymentMiddlewareASGI = None
    HTTPRequestContext = None
    RouteConfig = None
    ExactEvmServerScheme = None
    AssetAmount = None
    x402ResourceServer = None

try:
    from x402.extensions.bazaar import OutputConfig, declare_discovery_extension
    from x402.extensions.payment_identifier import (
        PAYMENT_IDENTIFIER,
        declare_payment_identifier_extension,
        extract_payment_identifier,
        payment_identifier_resource_server_extension,
    )
except ImportError:  # pragma: no cover - extension support depends on x402 package version.
    OutputConfig = None
    declare_discovery_extension = None
    PAYMENT_IDENTIFIER = None
    declare_payment_identifier_extension = None
    extract_payment_identifier = None
    payment_identifier_resource_server_extension = None

load_dotenv()

app = FastAPI(
    title="AxonGate Sovereign Gateway",
    description="x402-paid Clean Context Broker for Web-to-Markdown extraction on Base.",
    version="1.2.0",
    docs_url="/swagger",
    redoc_url="/redoc",
)

DEFAULT_PUBLIC_BASE_URL = "https://api.axongate.one"
PUBLIC_BASE_URL = os.getenv("AXONGATE_PUBLIC_BASE_URL", DEFAULT_PUBLIC_BASE_URL).rstrip("/")
GITHUB_REPO_URL = os.getenv("AXONGATE_GITHUB_REPO_URL", "https://github.com/sauliusbeconis/AxonGate").rstrip("/")
BASE_MAINNET_RPC_URL = os.getenv("BASE_RPC_URL", "https://mainnet.base.org")
BASE_RPC_TIMEOUT_SECONDS = float(os.getenv("BASE_RPC_TIMEOUT_SECONDS", "5"))
BASE_USDC_ADDRESS = Web3.to_checksum_address(
    os.getenv("BASE_USDC_ADDRESS", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")
)
BASE_USDC_TOKEN_NAME = os.getenv("BASE_USDC_TOKEN_NAME", "USD Coin")
BASE_USDC_TOKEN_VERSION = os.getenv("BASE_USDC_TOKEN_VERSION", "2")
PAYAI_FACILITATOR_URL = os.getenv("PAYAI_FACILITATOR_URL", "https://facilitator.payai.network")
REDIS_URL = os.getenv("REDIS_URL")
DEFAULT_CACHE_TTL_SECONDS = int(os.getenv("AXONGATE_CACHE_TTL_SECONDS", "3600"))
DELIVERY_CREDIT_TTL_SECONDS = int(os.getenv("AXONGATE_DELIVERY_CREDIT_TTL_SECONDS", "900"))
DELIVERY_CREDIT_MAX_ATTEMPTS = int(os.getenv("AXONGATE_DELIVERY_CREDIT_MAX_ATTEMPTS", "2"))
RATE_LIMIT_ENABLED = os.getenv("AXONGATE_RATE_LIMIT_ENABLED", "true").lower() not in {"0", "false", "no"}
RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("AXONGATE_RATE_LIMIT_WINDOW_SECONDS", "60"))
RATE_LIMIT_PROBE_PER_IP = int(os.getenv("AXONGATE_RATE_LIMIT_PROBE_PER_IP", "120"))
RATE_LIMIT_UNPAID_PER_IP = int(os.getenv("AXONGATE_RATE_LIMIT_UNPAID_PER_IP", "60"))
RATE_LIMIT_PAID_PER_IP = int(os.getenv("AXONGATE_RATE_LIMIT_PAID_PER_IP", "120"))
RATE_LIMIT_TARGET_DOMAIN = int(os.getenv("AXONGATE_RATE_LIMIT_TARGET_DOMAIN", "120"))
RATE_LIMIT_RETRY_PER_IP = int(os.getenv("AXONGATE_RATE_LIMIT_RETRY_PER_IP", "60"))
RATE_LIMIT_RETRY_PER_CREDIT = int(os.getenv("AXONGATE_RATE_LIMIT_RETRY_PER_CREDIT", "10"))
RATE_LIMIT_LEGACY_PAYMENT_HASH = int(os.getenv("AXONGATE_RATE_LIMIT_LEGACY_PAYMENT_HASH", "20"))
RATE_LIMIT_COMPUTE_PER_IP = int(os.getenv("AXONGATE_RATE_LIMIT_COMPUTE_PER_IP", "30"))
METRICS_PERSISTENCE_ENABLED = os.getenv("AXONGATE_METRICS_PERSISTENCE_ENABLED", "true").lower() not in {
    "0",
    "false",
    "no",
}
METRICS_REDIS_KEY = os.getenv("AXONGATE_METRICS_REDIS_KEY", "axongate:metrics")
ATTRIBUTION_REDIS_KEY = os.getenv("AXONGATE_ATTRIBUTION_REDIS_KEY", "axongate:attribution")
ATTRIBUTION_EVENTS_REDIS_KEY = os.getenv("AXONGATE_ATTRIBUTION_EVENTS_REDIS_KEY", "axongate:attribution:events")
ATTRIBUTION_EVENT_RETENTION_SECONDS = int(os.getenv("AXONGATE_ATTRIBUTION_EVENT_RETENTION_SECONDS", str(7 * 24 * 60 * 60)))
ATTRIBUTION_EVENT_MEMORY_MAX = int(os.getenv("AXONGATE_ATTRIBUTION_EVENT_MEMORY_MAX", "10000"))
ALERT_WEBHOOK_URL = os.getenv("AXONGATE_ALERT_WEBHOOK_URL")
ALERT_WEBHOOK_TOKEN = os.getenv("AXONGATE_ALERT_WEBHOOK_TOKEN")
ALERT_MIN_INTERVAL_SECONDS = int(os.getenv("AXONGATE_ALERT_MIN_INTERVAL_SECONDS", "300"))
ALERT_WEBHOOK_TIMEOUT_SECONDS = float(os.getenv("AXONGATE_ALERT_WEBHOOK_TIMEOUT_SECONDS", "5"))
ALERT_MIN_SAMPLE_SIZE = int(os.getenv("AXONGATE_ALERT_MIN_SAMPLE_SIZE", "20"))
ALERT_RETRYABLE_OUTAGE_RATE = float(os.getenv("AXONGATE_ALERT_RETRYABLE_OUTAGE_RATE", "0.15"))
ALERT_UEG_REJECTION_RATE = float(os.getenv("AXONGATE_ALERT_UEG_REJECTION_RATE", "0.20"))
ALERT_PAYMENT_VALIDATION_REJECTION_RATE = float(os.getenv("AXONGATE_ALERT_PAYMENT_VALIDATION_REJECTION_RATE", "0.25"))
ALERT_SUPPLIER_SUCCESS_MIN_RATE = float(os.getenv("AXONGATE_ALERT_SUPPLIER_SUCCESS_MIN_RATE", "0.85"))
ALERT_BASE_RPC_ERROR_RATE = float(os.getenv("AXONGATE_ALERT_BASE_RPC_ERROR_RATE", "0.10"))
ALERT_JINA_ERROR_RATE = float(os.getenv("AXONGATE_ALERT_JINA_ERROR_RATE", "0.10"))

JINA_API_KEY = os.getenv("JINA_API_KEY")
JINA_READER_BASE_URL = os.getenv("JINA_READER_BASE_URL", "https://r.jina.ai")
JINA_TIMEOUT_SECONDS = float(os.getenv("JINA_TIMEOUT_SECONDS", "20"))
PREFLIGHT_ENABLED = os.getenv("AXONGATE_PREFLIGHT_ENABLED", "true").lower() not in {"0", "false", "no"}
PREFLIGHT_TIMEOUT_SECONDS = float(os.getenv("AXONGATE_PREFLIGHT_TIMEOUT_SECONDS", "5"))
PREFLIGHT_MAX_REDIRECTS = int(os.getenv("AXONGATE_PREFLIGHT_MAX_REDIRECTS", "3"))
PREFLIGHT_MAX_CONTENT_BYTES = int(os.getenv("AXONGATE_PREFLIGHT_MAX_CONTENT_BYTES", "5242880"))
PREFLIGHT_ALLOWED_CONTENT_TYPES = {
    item.strip().lower()
    for item in os.getenv(
        "AXONGATE_PREFLIGHT_ALLOWED_CONTENT_TYPES",
        "text/html,application/xhtml+xml,text/plain,text/markdown,application/json,application/xml,text/xml",
    ).split(",")
    if item.strip()
}
ALLOWED_TARGET_PORTS = {
    int(item.strip())
    for item in os.getenv("AXONGATE_ALLOWED_TARGET_PORTS", "80,443").split(",")
    if item.strip().isdigit()
}

USDC_DECIMALS = 6
REQUIRED_USDC_FEE = Decimal(os.getenv("AXONGATE_BASE_FEE_USDC", "0.02"))

ATTRIBUTION_ROLLING_WINDOWS: dict[str, int] = {
    "1h": 60 * 60,
    "24h": 24 * 60 * 60,
    "7d": 7 * 24 * 60 * 60,
}
ATTRIBUTION_FUNNEL_STAGES = (
    "discovery_hits",
    "payment_challenges",
    "paid_attempts",
    "payments_accepted",
    "delivery_success",
    "payment_replay_rejections",
    "retry_credit_attempts",
    "proof_pack_quotes",
    "proof_pack_requests",
    "proof_pack_delivery_success",
)
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
REQUIRED_USDC_AMOUNT = int(REQUIRED_USDC_FEE * (Decimal(10) ** USDC_DECIMALS))
STARTER_TIER = "starter"
CACHE_ONLY_TIER = "cached"
TIER_PRICING_USDC = {
    STARTER_TIER: Decimal(os.getenv("AXONGATE_STARTER_PRICE_USDC", "0.012")),
    CACHE_ONLY_TIER: Decimal(os.getenv("AXONGATE_CACHED_PRICE_USDC", "0.015")),
    "basic": Decimal(os.getenv("AXONGATE_BASIC_PRICE_USDC", "0.02")),
    "fresh": Decimal(os.getenv("AXONGATE_FRESH_PRICE_USDC", "0.03")),
    "deep": Decimal(os.getenv("AXONGATE_DEEP_PRICE_USDC", "0.05")),
}
DEFAULT_PROOF_PACK = "standard"
PROOF_PACK_PRICING_USDC = {
    "quick": Decimal(os.getenv("AXONGATE_PROOF_QUICK_PRICE_USDC", "0.10")),
    DEFAULT_PROOF_PACK: Decimal(os.getenv("AXONGATE_PROOF_STANDARD_PRICE_USDC", "0.25")),
    "deep": Decimal(os.getenv("AXONGATE_PROOF_DEEP_PRICE_USDC", "1.00")),
}
PROOF_PACK_INTERNAL_TIERS = {
    "quick": "basic",
    DEFAULT_PROOF_PACK: "basic",
    "deep": "deep",
}
PROOF_PRO_PAYMENT_URL = os.getenv("AXONGATE_PROOF_PRO_PAYMENT_URL", "").strip()
PROOF_TEAM_PAYMENT_URL = os.getenv("AXONGATE_PROOF_TEAM_PAYMENT_URL", "").strip()
LLM_ENABLED = os.getenv("AXONGATE_LLM_ENABLED", "false").lower() in {"1", "true", "yes"}
LLM_API_KEY = os.getenv("AXONGATE_LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
LLM_BASE_URL = os.getenv("AXONGATE_LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
LLM_MODEL = os.getenv("AXONGATE_LLM_MODEL", "gpt-5-mini")
LLM_FAST_MODEL = os.getenv("AXONGATE_LLM_FAST_MODEL", "gpt-5-nano")
LLM_TIMEOUT_SECONDS = float(os.getenv("AXONGATE_LLM_TIMEOUT_SECONDS", "20"))
LLM_MAX_INPUT_CHARS = int(os.getenv("AXONGATE_LLM_MAX_INPUT_CHARS", "24000"))
CACHE_ONLY_TIERS = {STARTER_TIER, CACHE_ONLY_TIER}
CACHED_TIER_CACHE_SOURCES = (STARTER_TIER, CACHE_ONLY_TIER, "basic", "deep")
RECOMMENDED_TIER = os.getenv("AXONGATE_RECOMMENDED_TIER", "fresh").strip().lower()
if RECOMMENDED_TIER not in TIER_PRICING_USDC:
    RECOMMENDED_TIER = "fresh"

JINA_API_COST_USDC = Decimal(
    os.getenv("AXONGATE_JINA_API_COST_USDC", os.getenv("AXONGATE_FIXED_API_OVERHEAD_USDC", "0.0005"))
)
MIN_PROFIT_MARGIN_USDC = max(
    Decimal(os.getenv("AXONGATE_PROFIT_MARGIN_USDC", "0.01")),
    Decimal("0.01"),
)
UEG_GAS_UNITS = int(os.getenv("AXONGATE_UEG_GAS_UNITS", "65000"))
ETH_USDC_PRICE_FLOOR = Decimal(
    os.getenv("AXONGATE_ETH_USDC_PRICE_FLOOR", os.getenv("AXONGATE_ETH_USDC_PRICE", "3500"))
)
ETH_USDC_PRICE_URL = os.getenv("AXONGATE_ETH_USDC_PRICE_URL", "https://api.coinbase.com/v2/prices/ETH-USD/spot")
ETH_USDC_PRICE_TIMEOUT_SECONDS = float(os.getenv("AXONGATE_ETH_USDC_PRICE_TIMEOUT_SECONDS", "5"))
ETH_USDC_PRICE_CACHE_TTL_SECONDS = int(os.getenv("AXONGATE_ETH_USDC_PRICE_CACHE_TTL_SECONDS", "60"))
ETH_USDC_PRICE_STALE_TTL_SECONDS = int(os.getenv("AXONGATE_ETH_USDC_PRICE_STALE_TTL_SECONDS", "3600"))
ETH_USDC_ALLOW_STATIC_FALLBACK = os.getenv("AXONGATE_ETH_USDC_ALLOW_STATIC_FALLBACK", "false").lower() in {
    "1",
    "true",
    "yes",
}

TRANSFER_TOPIC = Web3.keccak(text="Transfer(address,address,uint256)").hex()

web3 = Web3(
    Web3.HTTPProvider(
        BASE_MAINNET_RPC_URL,
        request_kwargs={"timeout": BASE_RPC_TIMEOUT_SECONDS},
    )
)

processed_txs: set[str] = set()
processed_txs_lock = asyncio.Lock()
standard_payment_references: set[str] = set()
standard_payment_references_lock = asyncio.Lock()
markdown_cache: dict[str, tuple[float, str]] = {}
markdown_cache_lock = asyncio.Lock()
delivery_credits: dict[str, dict[str, Any]] = {}
delivery_credits_lock = asyncio.Lock()
eth_usdc_price_cache: dict[str, Any] = {}
eth_usdc_price_lock = asyncio.Lock()
rate_limit_windows: dict[str, tuple[int, int]] = {}
rate_limit_lock = asyncio.Lock()
redis_client = redis.from_url(REDIS_URL, decode_responses=True) if redis and REDIS_URL else None
alert_windows: dict[str, float] = {}
alert_lock = asyncio.Lock()
attribution_counts: dict[str, int] = {}
attribution_events: dict[str, tuple[int, str, str]] = {}
metrics: dict[str, int] = {
    "requests_total": 0,
    "legacy_access_requests_total": 0,
    "x402_access_requests_total": 0,
    "delivery_credit_retries_total": 0,
    "payment_required_total": 0,
    "payment_challenges_total": 0,
    "paid_attempts_total": 0,
    "payments_accepted_total": 0,
    "payment_verified_total": 0,
    "payment_identifier_seen_total": 0,
    "payment_validation_rejections_total": 0,
    "payment_replay_rejections_total": 0,
    "delivery_success_total": 0,
    "standard_delivery_success_total": 0,
    "legacy_delivery_success_total": 0,
    "retry_delivery_success_total": 0,
    "jina_requests_total": 0,
    "jina_success_total": 0,
    "jina_errors_total": 0,
    "cache_hits_total": 0,
    "cache_misses_total": 0,
    "discovery_hits_total": 0,
    "discovery_root_hits_total": 0,
    "discovery_llms_hits_total": 0,
    "discovery_docs_hits_total": 0,
    "discovery_operator_hits_total": 0,
    "discovery_quickstart_hits_total": 0,
    "discovery_paid_test_hits_total": 0,
    "discovery_quote_hits_total": 0,
    "discovery_proof_pack_hits_total": 0,
    "discovery_demo_hits_total": 0,
    "discovery_robots_hits_total": 0,
    "discovery_sitemap_hits_total": 0,
    "discovery_manifest_hits_total": 0,
    "discovery_agent_card_hits_total": 0,
    "discovery_x402_hits_total": 0,
    "discovery_resources_hits_total": 0,
    "target_preflight_total": 0,
    "target_preflight_rejections_total": 0,
    "ssrf_rejections_total": 0,
    "eth_price_fetches_total": 0,
    "eth_price_cache_hits_total": 0,
    "eth_price_stale_hits_total": 0,
    "eth_price_errors_total": 0,
    "base_rpc_errors_total": 0,
    "ueg_checks_total": 0,
    "ueg_rejections_total": 0,
    "rate_limit_checks_total": 0,
    "rate_limit_rejections_total": 0,
    "supplier_rejections_total": 0,
    "retryable_outages_total": 0,
    "delivery_credits_issued_total": 0,
    "retry_credit_attempts_total": 0,
    "delivery_credit_success_total": 0,
    "delivery_credit_exhausted_total": 0,
    "alert_checks_total": 0,
    "alerts_sent_total": 0,
    "alert_errors_total": 0,
    "proof_pack_quotes_total": 0,
    "proof_pack_requests_total": 0,
    "proof_pack_llm_success_total": 0,
    "proof_pack_llm_fallback_total": 0,
    "proof_pack_delivery_success_total": 0,
    "errors_total": 0,
}
CDP_TRANSACTION_METHOD_NAMES = ("get_transaction", "get_evm_transaction", "get_transaction_receipt")
PAYMENT_PROOF_HEADERS = (
    "PAYMENT-SIGNATURE",
    "X-PAYMENT",
    "X-402-PAYMENT",
    "X-Payment",
    "X-Payment-Signature",
)


class AccessRequest(BaseModel):
    target_url: str = Field(..., description="HTTP or HTTPS URL to convert into clean markdown")
    tier: str = Field(RECOMMENDED_TIER, description="Pricing tier: starter, cached, basic, fresh, or deep")
    force_refresh: bool = Field(False, description="Bypass cache when true")

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "target_url": "https://example.com/reference",
                    "tier": "cached",
                    "force_refresh": False,
                },
                {
                    "target_url": "https://example.com/research/source",
                    "tier": "fresh",
                    "force_refresh": True,
                },
                {
                    "target_url": "https://example.com/reference",
                    "tier": "basic",
                    "force_refresh": False,
                },
            ]
        }
    }


class ProofPackRequest(BaseModel):
    target_url: str = Field(..., description="HTTP or HTTPS source URL to turn into a citation-backed report")
    question: str = Field(
        "What does this source establish?",
        description="Buyer question or evidence objective for this proof pack",
    )
    pack: str = Field(DEFAULT_PROOF_PACK, description="Proof Pack level: quick, standard, or deep")
    force_refresh: bool = Field(False, description="Bypass source cache when true; deep packs refresh by default")

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "target_url": "https://example.com/source",
                    "question": "What does this source establish?",
                    "pack": DEFAULT_PROOF_PACK,
                    "force_refresh": False,
                },
                {
                    "target_url": "https://example.com/research",
                    "question": "Which claims can our agent cite from this page?",
                    "pack": "deep",
                    "force_refresh": True,
                },
            ]
        }
    }


class ComputeRequest(BaseModel):
    agent_id: str
    task_payload: dict[str, Any]
    offered_fee: float = Field(..., description="Fee offered by the client agent in USDC")

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "agent_id": "research-agent-001",
                    "task_payload": {"operation": "summarize", "input": "clean markdown context"},
                    "offered_fee": 0.05,
                }
            ]
        }
    }


@dataclass(frozen=True)
class UEGReceipt:
    revenue_usdc: Decimal
    dynamic_gas_cost_usdc: Decimal
    jina_api_cost_usdc: Decimal
    projected_profit_usdc: Decimal
    base_fee_wei: int
    gas_units: int
    supplier_attempts: int = 1
    eth_usdc_price: Decimal = Decimal("0")
    eth_usdc_price_source: str = "unknown"
    eth_usdc_floor_applied: bool = False


@dataclass(frozen=True)
class PaymentVerification:
    tx_hash: str
    vault_address: str
    token_address: str
    amount_usdc: Decimal


@dataclass(frozen=True)
class DeliveryCreditReservation:
    token: str
    key: str
    record: dict[str, Any]
    remaining_attempts: int
    total_supplier_attempts: int


@dataclass(frozen=True)
class TargetPreflight:
    requested_url: str
    final_url: str
    status_code: int
    content_type: Optional[str]
    content_length: Optional[int]
    redirects_followed: int


@dataclass(frozen=True)
class EthUsdQuote:
    price: Decimal
    source: str
    fetched_at: int
    floor_applied: bool


class PaymentValidationError(Exception):
    def __init__(self, detail: str, *, consume_credit: bool = False):
        self.detail = detail
        self.consume_credit = consume_credit
        super().__init__(detail)


class NetworkUnavailableError(Exception):
    def __init__(
        self,
        detail: str,
        *,
        source: str = "network",
        creditable: bool = True,
        supplier_attempt_charged: bool = False,
    ):
        self.detail = detail
        self.source = source
        self.creditable = creditable
        self.supplier_attempt_charged = supplier_attempt_charged
        super().__init__(detail)


class RateLimitExceeded(Exception):
    def __init__(self, detail: str, *, retry_after_seconds: int, bucket: str, limit: int):
        self.detail = detail
        self.retry_after_seconds = retry_after_seconds
        self.bucket = bucket
        self.limit = limit
        super().__init__(detail)


def load_vault_address() -> str:
    env_address = os.getenv("AXONGATE_VAULT_ADDRESS") or os.getenv("VAULT_ADDRESS")
    if env_address:
        return Web3.to_checksum_address(env_address)

    manifest_path = Path(__file__).with_name("manifest.json")
    if manifest_path.exists():
        manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_address = manifest_data.get("identity", {}).get("vault_address")
        if manifest_address:
            return Web3.to_checksum_address(manifest_address)

    manifest_path = Path(__file__).with_name("agent_manifest.json")
    if manifest_path.exists():
        manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_address = manifest_data.get("vault_address")
        if manifest_address:
            return Web3.to_checksum_address(manifest_address)

    raise RuntimeError("Vault address is not configured")


def load_agent_card() -> dict[str, Any]:
    """Load the canonical AxonGate discovery card and rewrite public URLs."""
    manifest_path = Path(__file__).with_name("manifest.json")
    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    return rewrite_public_base_urls(manifest_data)


def rewrite_public_base_urls(value: Any) -> Any:
    """Make manifest URLs follow AXONGATE_PUBLIC_BASE_URL for custom domains."""
    if isinstance(value, dict):
        return {key: rewrite_public_base_urls(item) for key, item in value.items()}
    if isinstance(value, list):
        return [rewrite_public_base_urls(item) for item in value]
    if isinstance(value, str) and value.startswith(DEFAULT_PUBLIC_BASE_URL):
        return value.replace(DEFAULT_PUBLIC_BASE_URL, PUBLIC_BASE_URL, 1)
    return value


def schedule_background(coro) -> None:
    """Schedule non-critical background work when running inside an event loop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        return
    loop.create_task(coro)


async def persist_metric_increment(name: str, amount: int) -> None:
    """Persist a metric increment to Redis without blocking request handlers."""
    if not redis_client or not METRICS_PERSISTENCE_ENABLED:
        return

    try:
        await redis_client.hincrby(METRICS_REDIS_KEY, name, amount)
    except Exception as exc:
        print(f"[METRICS] Redis persistence failed for {name}: {exc}")


def inc_metric(name: str, amount: int = 1) -> None:
    metrics[name] = metrics.get(name, 0) + amount
    if redis_client and METRICS_PERSISTENCE_ENABLED:
        schedule_background(persist_metric_increment(name, amount))


async def durable_metrics_snapshot() -> dict[str, int]:
    """Return Redis-backed metrics when available, with memory as fallback."""
    snapshot = dict(metrics)
    if not redis_client or not METRICS_PERSISTENCE_ENABLED:
        return snapshot

    try:
        persisted = await redis_client.hgetall(METRICS_REDIS_KEY)
    except Exception as exc:
        print(f"[METRICS] Redis snapshot failed: {exc}")
        return snapshot

    for key, value in persisted.items():
        try:
            snapshot[key] = int(value)
        except (TypeError, ValueError):
            continue

    return snapshot


def normalize_attribution_source(value: Optional[str]) -> str:
    """Return a bounded, low-cardinality source label for public metrics."""
    raw = (value or "direct").strip().lower()
    normalized = re.sub(r"[^a-z0-9_.:-]+", "-", raw)[:48].strip("-._:")
    return normalized or "direct"


def attribution_source_from_request(request: Request) -> str:
    """Infer where a buyer or crawler came from without storing raw user data."""
    for query_name in ("source", "utm_source", "ref"):
        query_value = request.query_params.get(query_name)
        if query_value:
            return normalize_attribution_source(query_value)

    for header_name in ("X-AxonGate-Source", "X-Source", "X-Client-Name"):
        header_value = request.headers.get(header_name)
        if header_value:
            return normalize_attribution_source(header_value)

    path_match = re.match(r"^/from/([^/]+)/", request.url.path)
    if path_match:
        return normalize_attribution_source(path_match.group(1))

    referer = request.headers.get("referer")
    if referer:
        hostname = urlparse(referer).hostname
        if hostname:
            return normalize_attribution_source(f"referer:{hostname}")

    return "direct"


async def persist_attribution_increment(key: str, amount: int) -> None:
    """Persist a source-attribution increment to Redis without blocking handlers."""
    if not redis_client or not METRICS_PERSISTENCE_ENABLED:
        return

    try:
        await redis_client.hincrby(ATTRIBUTION_REDIS_KEY, key, amount)
    except Exception as exc:
        print(f"[METRICS] Redis attribution persistence failed for {key}: {exc}")


def prune_memory_attribution_events(now: int) -> None:
    cutoff = now - ATTRIBUTION_EVENT_RETENTION_SECONDS
    for event_id, (timestamp, _, _) in list(attribution_events.items()):
        if timestamp < cutoff:
            attribution_events.pop(event_id, None)

    overflow = len(attribution_events) - ATTRIBUTION_EVENT_MEMORY_MAX
    if overflow > 0:
        oldest = sorted(attribution_events.items(), key=lambda item: item[1][0])[:overflow]
        for event_id, _ in oldest:
            attribution_events.pop(event_id, None)


async def persist_attribution_event(event_id: str, timestamp: int, stage: str, source: str) -> None:
    """Persist a timestamped source event for rolling conversion windows."""
    if not redis_client or not METRICS_PERSISTENCE_ENABLED:
        return

    member = json.dumps(
        {"id": event_id, "ts": timestamp, "stage": stage, "source": source},
        separators=(",", ":"),
        sort_keys=True,
    )
    try:
        await redis_client.zadd(ATTRIBUTION_EVENTS_REDIS_KEY, {member: timestamp})
        await redis_client.zremrangebyscore(
            ATTRIBUTION_EVENTS_REDIS_KEY,
            0,
            timestamp - ATTRIBUTION_EVENT_RETENTION_SECONDS,
        )
    except Exception as exc:
        print(f"[METRICS] Redis attribution event persistence failed for {stage}:{source}: {exc}")


def record_attribution_event(stage: str, source: str) -> None:
    normalized_source = normalize_attribution_source(source)
    normalized_stage = re.sub(r"[^a-z0-9_]+", "_", stage.strip().lower())[:64].strip("_") or "unknown"
    timestamp = int(time.time())
    event_id = f"{time.time_ns()}:{secrets.token_hex(4)}"
    attribution_events[event_id] = (timestamp, normalized_stage, normalized_source)
    prune_memory_attribution_events(timestamp)
    if redis_client and METRICS_PERSISTENCE_ENABLED:
        schedule_background(persist_attribution_event(event_id, timestamp, normalized_stage, normalized_source))


def inc_attribution(stage: str, source: str, amount: int = 1) -> None:
    normalized_source = normalize_attribution_source(source)
    key = f"{stage}:{normalized_source}"
    attribution_counts[key] = attribution_counts.get(key, 0) + amount
    for _ in range(max(0, amount)):
        record_attribution_event(stage, normalized_source)
    if redis_client and METRICS_PERSISTENCE_ENABLED:
        schedule_background(persist_attribution_increment(key, amount))


async def durable_attribution_snapshot() -> dict[str, dict[str, int]]:
    """Return source attribution counters grouped by funnel stage."""
    flat_counts = dict(attribution_counts)
    if redis_client and METRICS_PERSISTENCE_ENABLED:
        try:
            persisted = await redis_client.hgetall(ATTRIBUTION_REDIS_KEY)
        except Exception as exc:
            print(f"[METRICS] Redis attribution snapshot failed: {exc}")
        else:
            for key, value in persisted.items():
                try:
                    flat_counts[key] = int(value)
                except (TypeError, ValueError):
                    continue

    grouped: dict[str, dict[str, int]] = {}
    for key, value in flat_counts.items():
        stage, _, source = key.partition(":")
        if not stage or not source:
            continue
        grouped.setdefault(stage, {})[source] = value

    return {stage: dict(sorted(sources.items())) for stage, sources in sorted(grouped.items())}


def attribution_event_from_redis_member(member: str) -> tuple[str, int, str, str] | None:
    try:
        data = json.loads(member)
        event_id = str(data.get("id") or stable_hash(member))
        timestamp = int(data["ts"])
        stage = str(data["stage"])
        source = normalize_attribution_source(str(data["source"]))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None

    if timestamp <= 0 or not stage:
        return None
    return event_id, timestamp, stage, source


def attribution_rates(stage_counts: dict[str, int]) -> dict[str, float]:
    discovery_hits = stage_counts.get("discovery_hits", 0)
    challenges = stage_counts.get("payment_challenges", 0)
    paid_attempts = stage_counts.get("paid_attempts", 0)
    accepted = stage_counts.get("payments_accepted", 0)
    delivered = stage_counts.get("delivery_success", 0)
    return {
        "challenge_per_discovery": conversion_rate(challenges, discovery_hits),
        "paid_attempt_per_challenge": conversion_rate(paid_attempts, challenges),
        "accepted_per_paid_attempt": conversion_rate(accepted, paid_attempts),
        "delivered_per_accepted": conversion_rate(delivered, accepted),
    }


def rolling_attribution_snapshot(
    events: dict[str, tuple[int, str, str]],
    now: Optional[int] = None,
) -> dict[str, Any]:
    """Group source-attribution events into operator-friendly time windows."""
    current_time = int(now or time.time())
    snapshot: dict[str, Any] = {
        "generated_at": current_time,
        "retention_seconds": ATTRIBUTION_EVENT_RETENTION_SECONDS,
        "windows": {},
    }

    for label, seconds in ATTRIBUTION_ROLLING_WINDOWS.items():
        started_at = current_time - seconds
        totals = {stage: 0 for stage in ATTRIBUTION_FUNNEL_STAGES}
        sources: dict[str, dict[str, int]] = {}
        event_count = 0

        for timestamp, stage, source in events.values():
            if timestamp < started_at or timestamp > current_time:
                continue
            event_count += 1
            totals[stage] = totals.get(stage, 0) + 1
            source_counts = sources.setdefault(source, {stage_name: 0 for stage_name in ATTRIBUTION_FUNNEL_STAGES})
            source_counts[stage] = source_counts.get(stage, 0) + 1

        sorted_sources = sorted(
            sources.items(),
            key=lambda item: (
                -item[1].get("delivery_success", 0),
                -item[1].get("paid_attempts", 0),
                -item[1].get("payment_challenges", 0),
                -item[1].get("discovery_hits", 0),
                item[0],
            ),
        )
        snapshot["windows"][label] = {
            "seconds": seconds,
            "started_at": started_at,
            "ended_at": current_time,
            "event_count": event_count,
            "stages": totals,
            "rates": attribution_rates(totals),
            "sources": {
                source: {**counts, "rates": attribution_rates(counts)}
                for source, counts in sorted_sources
            },
        }

    return snapshot


async def durable_rolling_attribution_snapshot() -> dict[str, Any]:
    """Return Redis-backed rolling attribution windows, with memory as fallback."""
    now = int(time.time())
    prune_memory_attribution_events(now)
    events = dict(attribution_events)

    if redis_client and METRICS_PERSISTENCE_ENABLED:
        cutoff = now - ATTRIBUTION_EVENT_RETENTION_SECONDS
        try:
            await redis_client.zremrangebyscore(ATTRIBUTION_EVENTS_REDIS_KEY, 0, cutoff)
            persisted = await redis_client.zrangebyscore(ATTRIBUTION_EVENTS_REDIS_KEY, cutoff, now)
        except Exception as exc:
            print(f"[METRICS] Redis attribution event snapshot failed: {exc}")
        else:
            for member in persisted:
                parsed = attribution_event_from_redis_member(member)
                if parsed is None:
                    continue
                event_id, timestamp, stage, source = parsed
                events[event_id] = (timestamp, stage, source)

    return rolling_attribution_snapshot(events, now)


def inc_discovery_hit(metric_name: str, source: Optional[str] = None) -> None:
    """Count discovery traffic as a separate conversion funnel stage."""
    inc_metric("discovery_hits_total")
    inc_metric(metric_name)
    inc_attribution("discovery_hits", source or "direct")


def conversion_rate(numerator: int, denominator: int) -> float:
    """Return a compact ratio for the metrics endpoint."""
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 4)


def conversion_funnel_snapshot(metric_values: Optional[dict[str, int]] = None) -> dict[str, Any]:
    """Summarize discovery-to-delivery conversion without exposing client data."""
    values = metric_values or metrics
    discovery_hits = values.get("discovery_hits_total", 0)
    challenges = values.get("payment_challenges_total", 0)
    paid_attempts = values.get("paid_attempts_total", 0)
    accepted = values.get("payments_accepted_total", 0)
    delivered = values.get("delivery_success_total", 0)
    supplier_requests = values.get("jina_requests_total", 0)

    return {
        "discovery_hits": discovery_hits,
        "payment_challenges": challenges,
        "paid_attempts": paid_attempts,
        "payments_accepted": accepted,
        "delivery_success": delivered,
        "retry_credit_issued": values.get("delivery_credits_issued_total", 0),
        "retry_delivery_success": values.get("retry_delivery_success_total", 0),
        "proof_pack_quotes": values.get("proof_pack_quotes_total", 0),
        "proof_pack_requests": values.get("proof_pack_requests_total", 0),
        "proof_pack_llm_success": values.get("proof_pack_llm_success_total", 0),
        "proof_pack_llm_fallback": values.get("proof_pack_llm_fallback_total", 0),
        "proof_pack_delivery_success": values.get("proof_pack_delivery_success_total", 0),
        "ueg_checks": values.get("ueg_checks_total", 0),
        "ueg_rejections": values.get("ueg_rejections_total", 0),
        "payment_replay_rejections": values.get("payment_replay_rejections_total", 0),
        "rate_limit_rejections": values.get("rate_limit_rejections_total", 0),
        "supplier_requests": supplier_requests,
        "supplier_success": values.get("jina_success_total", 0),
        "supplier_errors": values.get("jina_errors_total", 0),
        "supplier_rejections": values.get("supplier_rejections_total", 0),
        "retryable_outages": values.get("retryable_outages_total", 0),
        "rates": {
            "challenge_per_discovery": conversion_rate(challenges, discovery_hits),
            "paid_attempt_per_challenge": conversion_rate(paid_attempts, challenges),
            "accepted_per_paid_attempt": conversion_rate(accepted, paid_attempts),
            "delivered_per_accepted": conversion_rate(delivered, accepted),
            "supplier_success_per_request": conversion_rate(values.get("jina_success_total", 0), supplier_requests),
        },
    }


async def should_send_alert(alert_key: str) -> bool:
    """Throttle alert webhooks by alert key across Redis-backed deployments."""
    now = time.time()
    if redis_client:
        try:
            claimed = await redis_client.set(
                f"axongate:alert:{alert_key}",
                str(int(now)),
                nx=True,
                ex=ALERT_MIN_INTERVAL_SECONDS,
            )
            return bool(claimed)
        except Exception as exc:
            print(f"[ALERT] Redis throttle failed for {alert_key}: {exc}")

    async with alert_lock:
        last_sent = alert_windows.get(alert_key, 0)
        if now - last_sent < ALERT_MIN_INTERVAL_SECONDS:
            return False
        alert_windows[alert_key] = now
        return True


async def send_alert(alert_key: str, severity: str, message: str, metric_values: dict[str, int]) -> None:
    """Send an operations alert to the configured webhook, if enabled."""
    if not ALERT_WEBHOOK_URL:
        return
    if not await should_send_alert(alert_key):
        return

    payload = {
        "agent": "AxonGate",
        "alert_key": alert_key,
        "severity": severity,
        "message": message,
        "public_base_url": PUBLIC_BASE_URL,
        "timestamp": int(time.time()),
        "metrics": {
            "requests_total": metric_values.get("requests_total", 0),
            "retryable_outages_total": metric_values.get("retryable_outages_total", 0),
            "ueg_rejections_total": metric_values.get("ueg_rejections_total", 0),
            "payment_validation_rejections_total": metric_values.get("payment_validation_rejections_total", 0),
            "jina_errors_total": metric_values.get("jina_errors_total", 0),
            "base_rpc_errors_total": metric_values.get("base_rpc_errors_total", 0),
            "delivery_success_total": metric_values.get("delivery_success_total", 0),
            "payments_accepted_total": metric_values.get("payments_accepted_total", 0),
        },
    }
    headers = {"Content-Type": "application/json"}
    if ALERT_WEBHOOK_TOKEN:
        headers["Authorization"] = f"Bearer {ALERT_WEBHOOK_TOKEN}"

    try:
        async with httpx.AsyncClient(timeout=ALERT_WEBHOOK_TIMEOUT_SECONDS) as client:
            response = await client.post(ALERT_WEBHOOK_URL, json=payload, headers=headers)
            response.raise_for_status()
        inc_metric("alerts_sent_total")
    except Exception as exc:
        inc_metric("alert_errors_total")
        print(f"[ALERT] Webhook delivery failed for {alert_key}: {exc}")


async def evaluate_alerts(metric_values: dict[str, int]) -> list[str]:
    """Evaluate coarse operational alert thresholds against durable counters."""
    inc_metric("alert_checks_total")
    triggered: list[str] = []
    requests_total = metric_values.get("requests_total", 0)
    paid_attempts = metric_values.get("paid_attempts_total", 0)
    accepted = metric_values.get("payments_accepted_total", 0)
    supplier_requests = metric_values.get("jina_requests_total", 0)
    ueg_checks = metric_values.get("ueg_checks_total", 0)

    if requests_total >= ALERT_MIN_SAMPLE_SIZE:
        retryable_rate = metric_values.get("retryable_outages_total", 0) / requests_total
        if retryable_rate >= ALERT_RETRYABLE_OUTAGE_RATE:
            triggered.append("high_retryable_outage_rate")
            await send_alert(
                "high_retryable_outage_rate",
                "warning",
                f"Retryable outage rate is {retryable_rate:.2%}.",
                metric_values,
            )

    if ueg_checks >= ALERT_MIN_SAMPLE_SIZE:
        ueg_rate = metric_values.get("ueg_rejections_total", 0) / ueg_checks
        base_rpc_rate = metric_values.get("base_rpc_errors_total", 0) / ueg_checks
        if ueg_rate >= ALERT_UEG_REJECTION_RATE:
            triggered.append("high_ueg_rejection_rate")
            await send_alert("high_ueg_rejection_rate", "warning", f"UEG rejection rate is {ueg_rate:.2%}.", metric_values)
        if base_rpc_rate >= ALERT_BASE_RPC_ERROR_RATE:
            triggered.append("high_base_rpc_error_rate")
            await send_alert(
                "high_base_rpc_error_rate",
                "critical",
                f"Base RPC error rate is {base_rpc_rate:.2%}.",
                metric_values,
            )

    if paid_attempts >= ALERT_MIN_SAMPLE_SIZE:
        payment_rejection_rate = metric_values.get("payment_validation_rejections_total", 0) / paid_attempts
        if payment_rejection_rate >= ALERT_PAYMENT_VALIDATION_REJECTION_RATE:
            triggered.append("high_payment_validation_rejection_rate")
            await send_alert(
                "high_payment_validation_rejection_rate",
                "warning",
                f"Payment validation rejection rate is {payment_rejection_rate:.2%}.",
                metric_values,
            )

    if accepted >= ALERT_MIN_SAMPLE_SIZE:
        delivery_success_rate = metric_values.get("delivery_success_total", 0) / accepted
        if delivery_success_rate < ALERT_SUPPLIER_SUCCESS_MIN_RATE:
            triggered.append("low_delivery_success_rate")
            await send_alert(
                "low_delivery_success_rate",
                "critical",
                f"Delivery success after accepted payment is {delivery_success_rate:.2%}.",
                metric_values,
            )

    if supplier_requests >= ALERT_MIN_SAMPLE_SIZE:
        supplier_success_rate = metric_values.get("jina_success_total", 0) / supplier_requests
        jina_error_rate = metric_values.get("jina_errors_total", 0) / supplier_requests
        if supplier_success_rate < ALERT_SUPPLIER_SUCCESS_MIN_RATE:
            triggered.append("low_jina_success_rate")
            await send_alert("low_jina_success_rate", "warning", f"Jina success rate is {supplier_success_rate:.2%}.", metric_values)
        if jina_error_rate >= ALERT_JINA_ERROR_RATE:
            triggered.append("high_jina_error_rate")
            await send_alert("high_jina_error_rate", "warning", f"Jina error rate is {jina_error_rate:.2%}.", metric_values)

    return triggered


def stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def tier_names() -> str:
    return ", ".join(TIER_PRICING_USDC.keys())


def proof_pack_names() -> str:
    return ", ".join(PROOF_PACK_PRICING_USDC.keys())


def normalize_tier(tier: Optional[str]) -> str:
    normalized = (tier or RECOMMENDED_TIER).strip().lower()
    if normalized not in TIER_PRICING_USDC:
        raise PaymentValidationError(f"Unsupported tier. Use {tier_names()}.")
    return normalized


def normalize_proof_pack(pack: Optional[str]) -> str:
    normalized = (pack or DEFAULT_PROOF_PACK).strip().lower()
    if normalized not in PROOF_PACK_PRICING_USDC:
        raise PaymentValidationError(f"Unsupported Proof Pack. Use {proof_pack_names()}.")
    return normalized


def is_cache_only_tier(tier: str) -> bool:
    return normalize_tier(tier) in CACHE_ONLY_TIERS


def usdc_units(amount: Decimal) -> int:
    return int(amount * (Decimal(10) ** USDC_DECIMALS))


def price_for_tier(tier: Optional[str]) -> Decimal:
    return TIER_PRICING_USDC[normalize_tier(tier)]


def price_for_proof_pack(pack: Optional[str]) -> Decimal:
    return PROOF_PACK_PRICING_USDC[normalize_proof_pack(pack)]


def proof_pack_internal_tier(pack: Optional[str]) -> str:
    return PROOF_PACK_INTERNAL_TIERS[normalize_proof_pack(pack)]


def proof_pack_cache_policy(pack: str) -> str:
    normalized_pack = normalize_proof_pack(pack)
    if normalized_pack == "quick":
        return "cache-friendly source read with deterministic fallback"
    if normalized_pack == "deep":
        return "deep evidence pack with short-cache source material and fresh-by-default refresh"
    return "cache-aware source read with LLM-assisted evidence synthesis when configured"


def cache_ttl_for_tier(tier: str, force_refresh: bool = False) -> int:
    normalized_tier = normalize_tier(tier)
    if force_refresh or normalized_tier == "fresh":
        return 0
    if normalized_tier == "deep":
        return max(DEFAULT_CACHE_TTL_SECONDS // 2, 300)
    return DEFAULT_CACHE_TTL_SECONDS


def cache_policy_for_tier(tier: str) -> str:
    normalized_tier = normalize_tier(tier)
    if normalized_tier == STARTER_TIER:
        return "starter sample or cache-only; no upstream fetch on miss"
    if normalized_tier == CACHE_ONLY_TIER:
        return "cache-only; no upstream fetch on miss"
    if normalized_tier == "fresh":
        return "bypass cache"
    if normalized_tier == "deep":
        return f"short cache, {cache_ttl_for_tier(normalized_tier)} seconds"
    return f"standard cache, {cache_ttl_for_tier(normalized_tier)} seconds"


STARTER_SAMPLE_MARKDOWN = """# IANA-managed Reserved Domains

This starter sample demonstrates AxonGate's paid delivery shape without spending
supplier budget. The source page describes domains reserved for documentation
and examples, including `example.com`, `example.net`, `example.org`, and the
`invalid`, `localhost`, `test`, and `example` top-level domains.

## Why Agents Use This

- Validate x402 payment plumbing with a tiny paid call.
- Confirm AxonGate returns clean Markdown in a stable JSON contract.
- Verify source attribution and replay protection before production spend.

For live, current web context, use the `fresh` tier with the target URL your
agent actually needs to read.
"""

STARTER_SAMPLE_TARGETS = {
    "https://www.iana.org/domains/reserved",
    "https://www.iana.org/domains/reserved/",
    "https://example.com",
    "https://example.com/",
}


def starter_sample_markdown_for_target(target_url: str) -> Optional[str]:
    normalized = target_url.strip().lower().rstrip("/")
    normalized_targets = {item.rstrip("/") for item in STARTER_SAMPLE_TARGETS}
    if normalized in normalized_targets:
        return STARTER_SAMPLE_MARKDOWN
    return None


def build_access_input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "target_url": {
                "type": "string",
                "format": "uri",
                "description": "Absolute public HTTP/HTTPS URL to convert into clean markdown.",
            },
            "tier": {
                "type": "string",
                "enum": list(TIER_PRICING_USDC.keys()),
                "default": RECOMMENDED_TIER,
                "description": (
                    "Payment tier. Standard x402 clients should also pass this as ?tier= "
                    "or X-AxonGate-Tier so the challenge amount matches the request."
                ),
            },
            "force_refresh": {
                "type": "boolean",
                "default": False,
                "description": "Bypass cache for cacheable tiers. Not supported for cached tier.",
            },
        },
        "required": ["target_url"],
        "additionalProperties": False,
    }


def build_access_output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "target_url": {"type": "string"},
            "tier": {"type": "string"},
            "markdown": {"type": "string"},
            "cache": {"type": "object"},
            "payment": {"type": "object"},
            "ueg_receipt": {"type": "object"},
        },
        "required": ["status", "target_url", "tier", "markdown", "cache", "payment", "ueg_receipt"],
    }


def build_access_request_example(tier: str = RECOMMENDED_TIER) -> dict[str, Any]:
    normalized_tier = normalize_tier(tier)
    target_url = "https://www.iana.org/domains/reserved" if normalized_tier == STARTER_TIER else "https://example.com/source"
    return {
        "target_url": target_url,
        "tier": normalized_tier,
        "force_refresh": normalized_tier == "fresh",
    }


def build_access_response_example(tier: str = RECOMMENDED_TIER) -> dict[str, Any]:
    normalized_tier = normalize_tier(tier)
    target_url = "https://www.iana.org/domains/reserved" if normalized_tier == STARTER_TIER else "https://example.com/source"
    return {
        "status": "success",
        "target_url": target_url,
        "tier": normalized_tier,
        "markdown": "# Example Domain\n\nExample clean markdown...",
        "cache": {"hit": is_cache_only_tier(normalized_tier)},
        "payment": {
            "mode": "x402-facilitator",
            "network": "eip155:8453",
            "vault_address": load_vault_address(),
            "token_address": BASE_USDC_ADDRESS,
            "amount_usdc": float(price_for_tier(normalized_tier)),
        },
        "ueg_receipt": {
            "revenue_usdc": float(price_for_tier(normalized_tier)),
            "projected_profit_usdc": 0.01,
        },
    }


def build_proof_pack_input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "target_url": {
                "type": "string",
                "format": "uri",
                "description": "Absolute public HTTP/HTTPS URL to evaluate.",
            },
            "question": {
                "type": "string",
                "default": "What does this source establish?",
                "description": "Evidence objective or buyer question.",
            },
            "pack": {
                "type": "string",
                "enum": list(PROOF_PACK_PRICING_USDC.keys()),
                "default": DEFAULT_PROOF_PACK,
                "description": (
                    "Proof Pack level. Standard x402 clients should also pass this as ?pack= "
                    "or X-AxonGate-Pack so the payment challenge amount matches the request."
                ),
            },
            "force_refresh": {
                "type": "boolean",
                "default": False,
                "description": "Bypass source cache. Deep packs refresh by default.",
            },
        },
        "required": ["target_url"],
        "additionalProperties": False,
    }


def build_proof_pack_output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "target_url": {"type": "string"},
            "question": {"type": "string"},
            "pack": {"type": "string"},
            "answer": {"type": "string"},
            "executive_summary": {"type": "string"},
            "confidence_score": {"type": "number"},
            "key_claims": {"type": "array"},
            "citations": {"type": "array"},
            "risks": {"type": "array"},
            "source_profile": {"type": "object"},
            "cache": {"type": "object"},
            "payment": {"type": "object"},
            "ueg_receipt": {"type": "object"},
        },
        "required": [
            "status",
            "target_url",
            "question",
            "pack",
            "answer",
            "executive_summary",
            "confidence_score",
            "key_claims",
            "citations",
            "risks",
            "source_profile",
            "cache",
            "payment",
            "ueg_receipt",
        ],
    }


def build_proof_pack_request_example(pack: str = DEFAULT_PROOF_PACK) -> dict[str, Any]:
    normalized_pack = normalize_proof_pack(pack)
    return {
        "target_url": "https://example.com/source",
        "question": "What does this source establish?",
        "pack": normalized_pack,
        "force_refresh": normalized_pack == "deep",
    }


def build_proof_pack_response_example(pack: str = DEFAULT_PROOF_PACK) -> dict[str, Any]:
    normalized_pack = normalize_proof_pack(pack)
    return {
        "status": "success",
        "target_url": "https://example.com/source",
        "question": "What does this source establish?",
        "pack": normalized_pack,
        "answer": "The source establishes the cited facts below, with each key claim tied to extracted evidence.",
        "executive_summary": "A concise evidence summary derived from the cited source material.",
        "confidence_score": 0.72,
        "key_claims": [
            {"claim": "Example claim supported by the cited excerpt.", "citation_ids": ["c1"], "confidence": 0.72}
        ],
        "citations": [{"id": "c1", "url": "https://example.com/source", "excerpt": "Example evidence excerpt."}],
        "risks": ["Only one public source was evaluated."],
        "source_profile": {
            "final_url": "https://example.com/source",
            "content_sha256": stable_hash("# Example source"),
        },
        "cache": {"hit": False},
        "payment": {
            "mode": "x402-facilitator",
            "network": "eip155:8453",
            "vault_address": load_vault_address(),
            "token_address": BASE_USDC_ADDRESS,
            "amount_usdc": float(price_for_proof_pack(normalized_pack)),
        },
        "ueg_receipt": {
            "revenue_usdc": float(price_for_proof_pack(normalized_pack)),
            "projected_profit_usdc": 0.2,
        },
    }


def enrich_bazaar_method(extension_payload: dict[str, Any], method: str = "POST") -> dict[str, Any]:
    bazaar = extension_payload.get("bazaar")
    if isinstance(bazaar, dict):
        input_info = bazaar.get("info", {}).get("input")
        if isinstance(input_info, dict):
            input_info["method"] = method
    return extension_payload


def build_x402_extensions(method: str = "POST", tier: str = RECOMMENDED_TIER) -> dict[str, Any]:
    extensions: dict[str, Any] = {}
    normalized_tier = normalize_tier(tier)

    if declare_discovery_extension is not None and OutputConfig is not None:
        extensions.update(
            enrich_bazaar_method(
                declare_discovery_extension(
                    input=build_access_request_example(normalized_tier),
                    input_schema=build_access_input_schema(),
                    body_type="json",
                    output=OutputConfig(
                        example=build_access_response_example(normalized_tier),
                        schema=build_access_output_schema(),
                    ),
                ),
                method,
            )
        )

    if (
        PAYMENT_IDENTIFIER is not None
        and declare_payment_identifier_extension is not None
    ):
        extensions[PAYMENT_IDENTIFIER] = declare_payment_identifier_extension(required=False)

    return extensions


def build_proof_pack_x402_extensions(method: str = "POST", pack: str = DEFAULT_PROOF_PACK) -> dict[str, Any]:
    extensions: dict[str, Any] = {}
    normalized_pack = normalize_proof_pack(pack)

    if declare_discovery_extension is not None and OutputConfig is not None:
        extensions.update(
            enrich_bazaar_method(
                declare_discovery_extension(
                    input=build_proof_pack_request_example(normalized_pack),
                    input_schema=build_proof_pack_input_schema(),
                    body_type="json",
                    output=OutputConfig(
                        example=build_proof_pack_response_example(normalized_pack),
                        schema=build_proof_pack_output_schema(),
                    ),
                ),
                method,
            )
        )

    if (
        PAYMENT_IDENTIFIER is not None
        and declare_payment_identifier_extension is not None
    ):
        extensions[PAYMENT_IDENTIFIER] = declare_payment_identifier_extension(required=False)

    return extensions


def build_x402_accepts(tier: str = RECOMMENDED_TIER) -> list[dict[str, Any]]:
    """Return PayAI/x402-compatible payment requirements for AxonGate."""
    normalized_tier = normalize_tier(tier)
    price = price_for_tier(normalized_tier)
    return [
        {
            "scheme": "exact",
            "network": "eip155:8453",
            "amount": str(usdc_units(price)),
            "asset": BASE_USDC_ADDRESS,
            "payTo": load_vault_address(),
            "maxTimeoutSeconds": 300,
            "extra": {
                "name": BASE_USDC_TOKEN_NAME,
                "version": BASE_USDC_TOKEN_VERSION,
                "decimals": USDC_DECIMALS,
                "price": f"${price}",
                "tier": normalized_tier,
                "mimeType": "application/json",
                "resource": f"{PUBLIC_BASE_URL}/v1/x402/access",
                "description": "Clean Web-to-Markdown context extraction for autonomous agents.",
            },
        }
    ]


def build_proof_pack_x402_accepts(pack: str = DEFAULT_PROOF_PACK) -> list[dict[str, Any]]:
    """Return x402 payment requirements for Proof Packs."""
    normalized_pack = normalize_proof_pack(pack)
    price = price_for_proof_pack(normalized_pack)
    return [
        {
            "scheme": "exact",
            "network": "eip155:8453",
            "amount": str(usdc_units(price)),
            "asset": BASE_USDC_ADDRESS,
            "payTo": load_vault_address(),
            "maxTimeoutSeconds": 300,
            "extra": {
                "name": BASE_USDC_TOKEN_NAME,
                "version": BASE_USDC_TOKEN_VERSION,
                "decimals": USDC_DECIMALS,
                "price": f"${price}",
                "pack": normalized_pack,
                "mimeType": "application/json",
                "resource": f"{PUBLIC_BASE_URL}/v1/x402/proof-pack",
                "description": "Citation-backed Proof Pack for agent builders.",
            },
        }
    ]


def build_x402_resource() -> dict[str, Any]:
    """Build the resource object used by PayAI-style discovery endpoints."""
    return {
        "resource": f"{PUBLIC_BASE_URL}/v1/x402/access",
        "type": "http",
        "x402Version": 2,
        "method": "POST",
        "accepts": build_x402_accepts(),
        "lastUpdated": int(time.time()),
        "metadata": {
            "provider": "AxonGate",
            "basename": "axongate.base.eth",
            "category": "data-context",
            "service": "The Clean Context Broker",
            "description": "x402-paid Web-to-Markdown extraction for RAG and autonomous research agents.",
            "tags": ["x402", "base", "usdc", "web-to-markdown", "rag", "context-broker"],
            "manifest": f"{PUBLIC_BASE_URL}/manifest.json",
            "agentCard": f"{PUBLIC_BASE_URL}/.well-known/agent.json",
            "docs": f"{PUBLIC_BASE_URL}/docs",
            "operatorDashboard": f"{PUBLIC_BASE_URL}/operator",
            "quickstart": f"{PUBLIC_BASE_URL}/quickstart",
            "paidTestGuide": f"{PUBLIC_BASE_URL}/paid-test",
            "quote": f"{PUBLIC_BASE_URL}/quote",
            "quoteApi": f"{PUBLIC_BASE_URL}/v1/x402/quote",
            "proofPack": f"{PUBLIC_BASE_URL}/proof-pack",
            "proofPackQuoteApi": f"{PUBLIC_BASE_URL}/v1/proof-pack/quote",
            "proofPackEndpoint": f"{PUBLIC_BASE_URL}/v1/x402/proof-pack",
            "demo": f"{PUBLIC_BASE_URL}/demo",
            "llmsTxt": f"{PUBLIC_BASE_URL}/llms.txt",
            "openapi": f"{PUBLIC_BASE_URL}/openapi.json",
            "swagger": f"{PUBLIC_BASE_URL}/swagger",
            "sitemap": f"{PUBLIC_BASE_URL}/sitemap.xml",
            "facilitator": PAYAI_FACILITATOR_URL,
            "legacyTxHashEndpoint": f"{PUBLIC_BASE_URL}/v1/access",
            "retryEndpoint": f"{PUBLIC_BASE_URL}/v1/x402/retry",
            "sourceAliasPattern": f"{PUBLIC_BASE_URL}/from/{{source}}/v1/x402/access",
            "proofPackSourceAliasPattern": f"{PUBLIC_BASE_URL}/from/{{source}}/v1/x402/proof-pack",
            "recommendedTier": RECOMMENDED_TIER,
            "supplyGuards": {
                "dnsSsrfProtection": True,
                "targetPreflight": PREFLIGHT_ENABLED,
                "maxContentBytes": PREFLIGHT_MAX_CONTENT_BYTES,
                "allowedTargetPorts": sorted(ALLOWED_TARGET_PORTS),
                "redisRateLimits": RATE_LIMIT_ENABLED,
            },
            "pricing": {
                tier: {
                    "amount": str(usdc_units(price)),
                    "price": f"${price}",
                    "currency": "USDC",
                    "cachePolicy": cache_policy_for_tier(tier),
                }
                for tier, price in TIER_PRICING_USDC.items()
            },
        },
        "inputSchema": {
            "type": "http",
            "method": "POST",
            "contentType": "application/json",
            "body": build_access_input_schema(),
        },
        "outputSchema": build_access_output_schema(),
        "discoverable": True,
    }


def build_proof_pack_resource() -> dict[str, Any]:
    """Build the Proof Pack resource object used by PayAI-style discovery endpoints."""
    return {
        "resource": f"{PUBLIC_BASE_URL}/v1/x402/proof-pack",
        "type": "http",
        "x402Version": 2,
        "method": "POST",
        "accepts": build_proof_pack_x402_accepts(DEFAULT_PROOF_PACK),
        "lastUpdated": int(time.time()),
        "metadata": {
            "provider": "AxonGate",
            "basename": "axongate.base.eth",
            "category": "evidence-reports",
            "service": "AxonGate Proof Packs",
            "description": "Paid, citation-backed evidence reports for agent builders and RAG evaluators.",
            "tags": ["x402", "base", "usdc", "proof-pack", "citations", "agent-builders", "evidence"],
            "manifest": f"{PUBLIC_BASE_URL}/manifest.json",
            "docs": f"{PUBLIC_BASE_URL}/proof-pack",
            "quoteApi": f"{PUBLIC_BASE_URL}/v1/proof-pack/quote",
            "sourceAliasPattern": f"{PUBLIC_BASE_URL}/from/{{source}}/v1/x402/proof-pack",
            "packHeader": "X-AxonGate-Pack",
            "defaultPack": DEFAULT_PROOF_PACK,
            "llm": {
                "enabled": bool(LLM_ENABLED and LLM_API_KEY),
                "model": LLM_MODEL,
                "fastModel": LLM_FAST_MODEL,
                "deterministicFallback": True,
            },
            "pricing": {
                pack: {
                    "amount": str(usdc_units(price)),
                    "price": f"${price}",
                    "currency": "USDC",
                    "cachePolicy": proof_pack_cache_policy(pack),
                }
                for pack, price in PROOF_PACK_PRICING_USDC.items()
            },
        },
        "inputSchema": {
            "type": "http",
            "method": "POST",
            "contentType": "application/json",
            "body": build_proof_pack_input_schema(),
        },
        "outputSchema": build_proof_pack_output_schema(),
        "discoverable": True,
    }


def build_payment_required_payload(error: str, tier: Optional[str] = None) -> dict[str, Any]:
    """
    Build a strict x402 PAYMENT-REQUIRED header payload for agent clients.

    Only official x402 extensions are included. Informal metadata belongs in
    manifest, llms.txt, docs, and discovery resources.
    """
    normalized_tier = normalize_tier(tier or RECOMMENDED_TIER)
    payload = {
        "x402Version": 2,
        "error": error,
        "resource": {
            "url": f"{PUBLIC_BASE_URL}/v1/x402/access",
            "description": "AxonGate Clean Context Broker: paid Web-to-Markdown extraction.",
            "mimeType": "application/json",
        },
        "accepts": build_x402_accepts(normalized_tier),
    }
    extensions = build_x402_extensions("POST", normalized_tier)
    if extensions:
        payload["extensions"] = extensions
    return payload


def build_proof_pack_payment_required_payload(error: str, pack: Optional[str] = None) -> dict[str, Any]:
    """Build an x402 PAYMENT-REQUIRED payload for Proof Packs."""
    normalized_pack = normalize_proof_pack(pack or DEFAULT_PROOF_PACK)
    payload = {
        "x402Version": 2,
        "error": error,
        "resource": {
            "url": f"{PUBLIC_BASE_URL}/v1/x402/proof-pack",
            "description": "AxonGate Proof Pack: citation-backed evidence report.",
            "mimeType": "application/json",
        },
        "accepts": build_proof_pack_x402_accepts(normalized_pack),
    }
    extensions = build_proof_pack_x402_extensions("POST", normalized_pack)
    if extensions:
        payload["extensions"] = extensions
    return payload


def build_x402_public_discovery() -> dict[str, Any]:
    """Return x402 discovery with official extensions and metadata."""
    payload = build_payment_required_payload("Payment required to access AxonGate Clean Context Broker")
    payload["metadata"] = {
        "provider": "AxonGate",
        "service": "The Clean Context Broker",
        "basename": "axongate.base.eth",
        "agentManifest": f"{PUBLIC_BASE_URL}/manifest.json",
        "agentCard": f"{PUBLIC_BASE_URL}/.well-known/agent.json",
        "agentCardAlias": f"{PUBLIC_BASE_URL}/.well-known/agent-card.json",
        "discovery": f"{PUBLIC_BASE_URL}/discovery/resources",
        "docs": f"{PUBLIC_BASE_URL}/docs",
        "operatorDashboard": f"{PUBLIC_BASE_URL}/operator",
        "quickstart": f"{PUBLIC_BASE_URL}/quickstart",
        "paidTestGuide": f"{PUBLIC_BASE_URL}/paid-test",
        "quote": f"{PUBLIC_BASE_URL}/quote",
        "quoteApi": f"{PUBLIC_BASE_URL}/v1/x402/quote",
        "proofPack": f"{PUBLIC_BASE_URL}/proof-pack",
        "proofPackQuoteApi": f"{PUBLIC_BASE_URL}/v1/proof-pack/quote",
        "proofPackEndpoint": f"{PUBLIC_BASE_URL}/v1/x402/proof-pack",
        "demo": f"{PUBLIC_BASE_URL}/demo",
        "llmsTxt": f"{PUBLIC_BASE_URL}/llms.txt",
        "openapi": f"{PUBLIC_BASE_URL}/openapi.json",
        "paymentHashHeader": "X-AxonGate-Payment-Hash",
        "standardPaymentHeader": "PAYMENT-SIGNATURE",
        "tierHeader": "X-AxonGate-Tier",
        "proofPackHeader": "X-AxonGate-Pack",
        "retryCreditHeader": "X-AxonGate-Retry-Credit",
        "retryEndpoint": f"{PUBLIC_BASE_URL}/v1/x402/retry",
        "facilitator": PAYAI_FACILITATOR_URL,
        "sourceAliasPattern": f"{PUBLIC_BASE_URL}/from/{{source}}/v1/x402/access",
        "proofPackSourceAliasPattern": f"{PUBLIC_BASE_URL}/from/{{source}}/v1/x402/proof-pack",
        "resources": [
            f"{PUBLIC_BASE_URL}/v1/x402/access",
            f"{PUBLIC_BASE_URL}/v1/x402/proof-pack",
        ],
        "tiers": {
            tier: {
                "price": f"${price}",
                "amount": str(usdc_units(price)),
                "currency": "USDC",
                "cachePolicy": cache_policy_for_tier(tier),
            }
            for tier, price in TIER_PRICING_USDC.items()
        },
        "proofPacks": {
            pack: {
                "price": f"${price}",
                "amount": str(usdc_units(price)),
                "currency": "USDC",
                "cachePolicy": proof_pack_cache_policy(pack),
            }
            for pack, price in PROOF_PACK_PRICING_USDC.items()
        },
    }
    return payload


def payment_required_headers(error: str, tier: Optional[str] = None) -> dict[str, str]:
    try:
        normalized_tier = normalize_tier(tier)
    except PaymentValidationError:
        normalized_tier = RECOMMENDED_TIER
    payload = build_payment_required_payload(error, normalized_tier)
    encoded = base64.b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii")
    return {
        "PAYMENT-REQUIRED": encoded,
        "X-Payment-Required": encoded,
        "X-AxonGate-Payment-Asset": BASE_USDC_ADDRESS,
        "X-AxonGate-Payment-Amount": str(price_for_tier(normalized_tier)),
        "X-AxonGate-Payment-Tier": normalized_tier,
        **buyer_guidance_headers(),
    }


def proof_pack_payment_required_headers(error: str, pack: Optional[str] = None) -> dict[str, str]:
    try:
        normalized_pack = normalize_proof_pack(pack)
    except PaymentValidationError:
        normalized_pack = DEFAULT_PROOF_PACK
    payload = build_proof_pack_payment_required_payload(error, normalized_pack)
    encoded = base64.b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii")
    return {
        "PAYMENT-REQUIRED": encoded,
        "X-Payment-Required": encoded,
        "X-AxonGate-Payment-Asset": BASE_USDC_ADDRESS,
        "X-AxonGate-Payment-Amount": str(price_for_proof_pack(normalized_pack)),
        "X-AxonGate-Payment-Pack": normalized_pack,
        **buyer_guidance_headers(),
    }


def buyer_guidance_headers() -> dict[str, str]:
    return {
        "X-AxonGate-Next-Step": "Create an x402 payment proof, then POST JSON with PAYMENT-SIGNATURE.",
        "X-AxonGate-Docs": f"{PUBLIC_BASE_URL}/docs",
        "X-AxonGate-Quickstart": f"{PUBLIC_BASE_URL}/quickstart",
        "X-AxonGate-Paid-Test": f"{PUBLIC_BASE_URL}/paid-test",
        "X-AxonGate-Quote": f"{PUBLIC_BASE_URL}/v1/x402/quote",
        "X-AxonGate-Proof-Pack": f"{PUBLIC_BASE_URL}/proof-pack",
        "X-AxonGate-Proof-Pack-Quote": f"{PUBLIC_BASE_URL}/v1/proof-pack/quote",
        "X-AxonGate-Demo": f"{PUBLIC_BASE_URL}/demo",
        "X-AxonGate-Buyer-Example": f"{GITHUB_REPO_URL}/blob/main/examples/paid_buyer.mjs",
        "Link": (
            f'<{PUBLIC_BASE_URL}/docs>; rel="help", '
            f'<{PUBLIC_BASE_URL}/quickstart>; rel="quickstart", '
            f'<{PUBLIC_BASE_URL}/paid-test>; rel="payment-test", '
            f'<{PUBLIC_BASE_URL}/v1/x402/quote>; rel="quote", '
            f'<{PUBLIC_BASE_URL}/proof-pack>; rel="service", '
            f'<{PUBLIC_BASE_URL}/v1/proof-pack/quote>; rel="proof-pack-quote", '
            f'<{GITHUB_REPO_URL}/blob/main/examples/paid_buyer.mjs>; rel="example"'
        ),
    }


def payment_required_detail(error: str, tier: Optional[str] = None) -> dict[str, Any]:
    try:
        normalized_tier = normalize_tier(tier)
    except PaymentValidationError:
        normalized_tier = RECOMMENDED_TIER

    return {
        "message": error,
        "next_steps": [
            "Decode the PAYMENT-REQUIRED header for Base USDC payment terms.",
            "Create an x402 payment proof for the selected tier.",
            "Retry with POST /v1/x402/access, PAYMENT-SIGNATURE, and a JSON target_url body.",
        ],
        "payment": {
            "protocol": "x402",
            "network": "eip155:8453",
            "asset": "USDC",
            "asset_address": BASE_USDC_ADDRESS,
            "amount_usdc": float(price_for_tier(normalized_tier)),
            "tier": normalized_tier,
            "pay_to": load_vault_address(),
            "payment_header": "PAYMENT-SIGNATURE",
        },
        "request": {
            "method": "POST",
            "url": f"{PUBLIC_BASE_URL}/v1/x402/access",
            "body": build_access_request_example(normalized_tier),
        },
        "links": {
            "docs": f"{PUBLIC_BASE_URL}/docs",
            "quickstart": f"{PUBLIC_BASE_URL}/quickstart",
            "paid_test": f"{PUBLIC_BASE_URL}/paid-test",
            "quote": f"{PUBLIC_BASE_URL}/v1/x402/quote",
            "demo": f"{PUBLIC_BASE_URL}/demo",
            "buyer_example": f"{GITHUB_REPO_URL}/blob/main/examples/paid_buyer.mjs",
            "curl_examples": f"{GITHUB_REPO_URL}/blob/main/examples/curl.md",
        },
    }


def proof_pack_payment_required_detail(error: str, pack: Optional[str] = None) -> dict[str, Any]:
    try:
        normalized_pack = normalize_proof_pack(pack)
    except PaymentValidationError:
        normalized_pack = DEFAULT_PROOF_PACK

    return {
        "message": error,
        "next_steps": [
            "Decode the PAYMENT-REQUIRED header for Base USDC payment terms.",
            "Create an x402 payment proof for the selected Proof Pack.",
            "Retry with POST /v1/x402/proof-pack, PAYMENT-SIGNATURE, and a matching ?pack= or X-AxonGate-Pack value.",
        ],
        "payment": {
            "protocol": "x402",
            "network": "eip155:8453",
            "asset": "USDC",
            "asset_address": BASE_USDC_ADDRESS,
            "amount_usdc": float(price_for_proof_pack(normalized_pack)),
            "pack": normalized_pack,
            "pay_to": load_vault_address(),
            "payment_header": "PAYMENT-SIGNATURE",
        },
        "request": {
            "method": "POST",
            "url": f"{PUBLIC_BASE_URL}/v1/x402/proof-pack?pack={normalized_pack}",
            "body": build_proof_pack_request_example(normalized_pack),
        },
        "links": {
            "proof_pack": f"{PUBLIC_BASE_URL}/proof-pack",
            "quote": f"{PUBLIC_BASE_URL}/v1/proof-pack/quote",
            "docs": f"{PUBLIC_BASE_URL}/docs",
            "buyer_example": f"{GITHUB_REPO_URL}/blob/main/examples/paid_buyer.mjs",
            "curl_examples": f"{GITHUB_REPO_URL}/blob/main/examples/curl.md",
        },
    }


def build_openapi_payment_info() -> dict[str, Any]:
    """Return an OpenAPI vendor extension describing AxonGate's x402 contract."""
    return {
        "protocol": "x402",
        "x402Version": 2,
        "endpoint": f"{PUBLIC_BASE_URL}/v1/x402/access",
        "proofPackEndpoint": f"{PUBLIC_BASE_URL}/v1/x402/proof-pack",
        "paymentHeader": "PAYMENT-SIGNATURE",
        "tierHeader": "X-AxonGate-Tier",
        "tierQueryParam": "tier",
        "proofPackHeader": "X-AxonGate-Pack",
        "proofPackQueryParam": "pack",
        "network": "eip155:8453",
        "asset": {
            "symbol": "USDC",
            "address": BASE_USDC_ADDRESS,
            "name": BASE_USDC_TOKEN_NAME,
            "version": BASE_USDC_TOKEN_VERSION,
            "decimals": USDC_DECIMALS,
        },
        "payTo": load_vault_address(),
        "facilitator": PAYAI_FACILITATOR_URL,
        "recommendedTier": RECOMMENDED_TIER,
        "tiers": {
            tier: {
                "amount": str(usdc_units(price)),
                "price_usdc": float(price),
                "cache_policy": cache_policy_for_tier(tier),
            }
            for tier, price in TIER_PRICING_USDC.items()
        },
        "proofPacks": {
            pack: {
                "amount": str(usdc_units(price)),
                "price_usdc": float(price),
                "cache_policy": proof_pack_cache_policy(pack),
            }
            for pack, price in PROOF_PACK_PRICING_USDC.items()
        },
        "challengeDiscovery": f"{PUBLIC_BASE_URL}/.well-known/x402",
        "bazaarDiscovery": f"{PUBLIC_BASE_URL}/discovery/resources",
        "retryEndpoint": f"{PUBLIC_BASE_URL}/v1/x402/retry",
    }


def client_rate_identifier(request: Request) -> str:
    """
    Return a privacy-preserving client identifier for rate-limit buckets.

    Railway/edge proxies commonly provide X-Forwarded-For. We only store a hash
    of the selected address in Redis so raw IPs are not persisted in app keys.
    """
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        client_ip = forwarded_for.split(",", 1)[0].strip()
    else:
        client_ip = request.headers.get("x-real-ip") or (request.client.host if request.client else "unknown")

    return f"ip:{stable_hash(client_ip or 'unknown')}"


def target_domain_identifier(target_url: str) -> str:
    hostname = (urlparse(target_url).hostname or "unknown").lower()
    return f"domain:{stable_hash(hostname)}"


def payment_hash_identifier(tx_hash: str) -> str:
    return f"payment-hash:{stable_hash(tx_hash.strip().lower())}"


def retry_credit_identifier(token: str) -> str:
    return f"retry-credit:{stable_hash(token.strip())}"


async def enforce_rate_limit(bucket: str, identifier: str, limit: int, window_seconds: int = RATE_LIMIT_WINDOW_SECONDS) -> None:
    """Fixed-window rate limiter backed by Redis, with an in-memory fallback."""
    if not RATE_LIMIT_ENABLED or limit <= 0 or window_seconds <= 0:
        return

    inc_metric("rate_limit_checks_total")
    now = int(time.time())
    window_start = now - (now % window_seconds)
    reset_at = window_start + window_seconds
    retry_after = max(1, reset_at - now)
    key = f"axongate:rate:{bucket}:{stable_hash(identifier)}:{window_start}"

    if redis_client:
        count = await redis_client.incr(key)
        if count == 1:
            await redis_client.expire(key, window_seconds + 5)
    else:
        async with rate_limit_lock:
            expires_at, current_count = rate_limit_windows.get(key, (reset_at, 0))
            if expires_at <= now:
                expires_at, current_count = reset_at, 0
            count = current_count + 1
            rate_limit_windows[key] = (expires_at, count)

            expired_keys = [cache_key for cache_key, (expires, _) in rate_limit_windows.items() if expires <= now]
            for cache_key in expired_keys[:100]:
                rate_limit_windows.pop(cache_key, None)

    if count > limit:
        inc_metric("rate_limit_rejections_total")
        raise RateLimitExceeded(
            "Rate limit exceeded. Client agent should retry after the indicated window.",
            retry_after_seconds=retry_after,
            bucket=bucket,
            limit=limit,
        )


def payment_reference_from_request(request: Request) -> str:
    """Build a non-secret reference for the settled x402 payment proof."""
    for header_name in PAYMENT_PROOF_HEADERS:
        header_value = request.headers.get(header_name)
        if header_value:
            return f"{header_name.lower()}:{stable_hash(header_value)}"

    payment_payload = getattr(request.state, "payment_payload", None)
    if payment_payload is not None:
        payload_json = json.dumps(payment_payload, sort_keys=True, default=str)
        return f"payment-payload:{stable_hash(payload_json)}"

    return f"payment-state:{stable_hash(str(time.time_ns()))}"


def payment_identifier_from_request(request: Request) -> Optional[str]:
    """Extract an optional x402 payment identifier from the verified payload."""
    payment_payload = getattr(request.state, "payment_payload", None)
    if payment_payload is None:
        return None

    if extract_payment_identifier is not None:
        try:
            return extract_payment_identifier(payment_payload)
        except Exception:
            pass

    if hasattr(payment_payload, "model_dump"):
        payload_dict = payment_payload.model_dump(by_alias=True)
    elif isinstance(payment_payload, dict):
        payload_dict = payment_payload
    else:
        return None

    extension = payload_dict.get("extensions", {}).get("payment-identifier")
    if not isinstance(extension, dict):
        return None

    info = extension.get("info")
    if not isinstance(info, dict):
        return None

    payment_identifier = info.get("id")
    return payment_identifier if isinstance(payment_identifier, str) else None


def configure_standard_x402_middleware() -> None:
    """Attach PayAI-compatible x402 settlement middleware for standard clients."""
    if not all(
        [
            FacilitatorConfig,
            HTTPFacilitatorClient,
            PaymentOption,
            PaymentMiddlewareASGI,
            RouteConfig,
            ExactEvmServerScheme,
            x402ResourceServer,
        ]
    ):
        print("[X402] Python x402 package unavailable; standard middleware disabled.")
        return

    facilitator = HTTPFacilitatorClient(FacilitatorConfig(url=PAYAI_FACILITATOR_URL, timeout=30.0))
    server = x402ResourceServer(facilitator)
    server.register("eip155:8453", ExactEvmServerScheme())
    if payment_identifier_resource_server_extension is not None:
        server.register_extension(payment_identifier_resource_server_extension)

    route_config = RouteConfig(
        accepts=PaymentOption(
            scheme="exact",
            pay_to=load_vault_address(),
            price=x402_dynamic_price,
            network="eip155:8453",
            max_timeout_seconds=300,
            extra={
                "name": BASE_USDC_TOKEN_NAME,
                "version": BASE_USDC_TOKEN_VERSION,
                "decimals": USDC_DECIMALS,
            },
        ),
        resource=f"{PUBLIC_BASE_URL}/v1/x402/access",
        description="AxonGate Clean Context Broker: paid Web-to-Markdown extraction.",
        mime_type="application/json",
        extensions=build_x402_extensions("POST") or None,
    )
    proof_pack_route_config = RouteConfig(
        accepts=PaymentOption(
            scheme="exact",
            pay_to=load_vault_address(),
            price=x402_dynamic_price,
            network="eip155:8453",
            max_timeout_seconds=300,
            extra={
                "name": BASE_USDC_TOKEN_NAME,
                "version": BASE_USDC_TOKEN_VERSION,
                "decimals": USDC_DECIMALS,
            },
        ),
        resource=f"{PUBLIC_BASE_URL}/v1/x402/proof-pack",
        description="AxonGate Proof Pack: paid citation-backed evidence report.",
        mime_type="application/json",
        extensions=build_proof_pack_x402_extensions("POST") or None,
    )
    routes = {"POST /v1/x402/access": route_config}
    routes["POST /v1/x402/proof-pack"] = proof_pack_route_config
    routes.update(
        {
            f"POST /from/{source}/v1/x402/access": route_config
            for source in SOURCE_ALIAS_PATHS
        }
    )
    routes.update(
        {
            f"POST /from/{source}/v1/x402/starter": route_config
            for source in SOURCE_ALIAS_PATHS
        }
    )
    routes.update(
        {
            f"POST /from/{source}/v1/x402/proof-pack": proof_pack_route_config
            for source in SOURCE_ALIAS_PATHS
        }
    )

    app.add_middleware(PaymentMiddlewareASGI, routes=routes, server=server)


def as_0x_hex(value: Any) -> str:
    if isinstance(value, str):
        return value if value.startswith("0x") else f"0x{value}"
    if hasattr(value, "hex"):
        hex_value = value.hex()
        return hex_value if hex_value.startswith("0x") else f"0x{hex_value}"
    return str(value)


def normalize_tx_hash(tx_hash: str) -> str:
    normalized = tx_hash.strip().lower()
    if not re.fullmatch(r"0x[a-fA-F0-9]{64}", normalized):
        raise PaymentValidationError("Invalid payment hash format")
    return normalized


def validate_target_url(target_url: str) -> str:
    parsed = urlparse(target_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        inc_metric("target_preflight_rejections_total")
        raise PaymentValidationError("target_url must be an absolute http or https URL")

    hostname = parsed.hostname or ""
    if hostname.lower() in {"localhost", "ip6-localhost"} or hostname.lower().endswith((".localhost", ".local")):
        inc_metric("ssrf_rejections_total")
        inc_metric("target_preflight_rejections_total")
        raise PaymentValidationError("target_url cannot point to localhost")

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if ALLOWED_TARGET_PORTS and port not in ALLOWED_TARGET_PORTS:
        inc_metric("target_preflight_rejections_total")
        raise PaymentValidationError("target_url uses a blocked port")

    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        ip = None

    if ip and not is_public_ip(ip):
        inc_metric("ssrf_rejections_total")
        inc_metric("target_preflight_rejections_total")
        raise PaymentValidationError("target_url cannot point to a private or local IP address")

    return target_url


def is_public_ip(ip: ipaddress._BaseAddress) -> bool:
    return bool(
        ip.is_global
        and not ip.is_private
        and not ip.is_loopback
        and not ip.is_link_local
        and not ip.is_multicast
        and not ip.is_reserved
        and not ip.is_unspecified
    )


async def resolve_public_hostname(hostname: str) -> list[str]:
    """Resolve a hostname and reject anything that can reach non-public networks."""
    try:
        addrinfo = await asyncio.wait_for(
            asyncio.to_thread(socket.getaddrinfo, hostname, None, type=socket.SOCK_STREAM),
            timeout=PREFLIGHT_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        raise NetworkUnavailableError(
            "Target DNS resolution timed out",
            source="target_dns",
            creditable=False,
            supplier_attempt_charged=False,
        ) from exc
    except socket.gaierror as exc:
        inc_metric("target_preflight_rejections_total")
        raise PaymentValidationError("target_url hostname could not be resolved", consume_credit=True) from exc

    resolved_ips: list[str] = []
    for entry in addrinfo:
        ip_text = entry[4][0]
        try:
            ip = ipaddress.ip_address(ip_text)
        except ValueError:
            inc_metric("ssrf_rejections_total")
            raise PaymentValidationError("target_url resolved to an invalid IP address", consume_credit=True)

        if not is_public_ip(ip):
            inc_metric("ssrf_rejections_total")
            raise PaymentValidationError("target_url resolved to a private or non-routable IP address", consume_credit=True)

        if ip_text not in resolved_ips:
            resolved_ips.append(ip_text)

    if not resolved_ips:
        inc_metric("target_preflight_rejections_total")
        raise PaymentValidationError("target_url did not resolve to any address", consume_credit=True)

    return resolved_ips


async def assert_public_target_url(target_url: str) -> str:
    """Validate URL shape and DNS before AxonGate connects to or pays for supply."""
    normalized_url = validate_target_url(target_url)
    hostname = urlparse(normalized_url).hostname
    if not hostname:
        raise PaymentValidationError("target_url hostname is missing")

    await resolve_public_hostname(hostname)
    return normalized_url


def content_type_allowed(content_type: Optional[str]) -> bool:
    if not content_type:
        return True

    media_type = content_type.split(";", 1)[0].strip().lower()
    return media_type in PREFLIGHT_ALLOWED_CONTENT_TYPES


def parse_content_length(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def reject_target_preflight(detail: str) -> None:
    inc_metric("target_preflight_rejections_total")
    inc_metric("supplier_rejections_total")
    raise PaymentValidationError(detail, consume_credit=True)


async def request_target_preflight(client: httpx.AsyncClient, url: str, method: str) -> httpx.Response:
    headers = {
        "Accept": "text/html, text/plain, application/xhtml+xml, application/json;q=0.5, */*;q=0.1",
        "User-Agent": "AxonGate-Preflight/1.0 (+https://api.axongate.one/manifest.json)",
    }
    if method == "GET":
        headers["Range"] = "bytes=0-2047"

    return await client.request(method, url, headers=headers, follow_redirects=False)


def validate_preflight_response(response: httpx.Response) -> None:
    content_length = parse_content_length(response.headers.get("content-length"))
    content_type = response.headers.get("content-type")

    if content_length is not None and content_length > PREFLIGHT_MAX_CONTENT_BYTES:
        reject_target_preflight("target_url content is too large for AxonGate context extraction")

    if not content_type_allowed(content_type):
        reject_target_preflight("target_url content type is not supported for Web-to-Markdown extraction")

    if 400 <= response.status_code < 500:
        reject_target_preflight(f"target_url origin rejected preflight with HTTP {response.status_code}")
    if response.status_code >= 500:
        reject_target_preflight(f"target_url origin is unavailable with HTTP {response.status_code}")


async def preflight_target_url(target_url: str) -> TargetPreflight:
    """
    Cheaply probe a target before spending a Jina request.

    Every redirect hop is URL-validated and DNS-resolved before the next request.
    Origin failures and unsupported supply are treated as client/supplier quality
    problems, not AxonGate availability failures, so they do not create retry
    credits and do not spend a Jina API call.
    """
    inc_metric("target_preflight_total")
    current_url = await assert_public_target_url(target_url)

    try:
        async with httpx.AsyncClient(timeout=PREFLIGHT_TIMEOUT_SECONDS) as client:
            for redirect_count in range(PREFLIGHT_MAX_REDIRECTS + 1):
                response = await request_target_preflight(client, current_url, "HEAD")
                if response.status_code in {403, 405}:
                    response = await request_target_preflight(client, current_url, "GET")

                if 300 <= response.status_code < 400:
                    location = response.headers.get("location")
                    if not location:
                        reject_target_preflight("target_url redirect is missing a Location header")

                    if redirect_count >= PREFLIGHT_MAX_REDIRECTS:
                        reject_target_preflight("target_url redirects too many times")

                    current_url = await assert_public_target_url(urljoin(str(response.url), location))
                    continue

                validate_preflight_response(response)
                return TargetPreflight(
                    requested_url=target_url,
                    final_url=str(response.url),
                    status_code=response.status_code,
                    content_type=response.headers.get("content-type"),
                    content_length=parse_content_length(response.headers.get("content-length")),
                    redirects_followed=redirect_count,
                )
    except PaymentValidationError:
        raise
    except httpx.TimeoutException as exc:
        reject_target_preflight("target_url preflight timed out")
    except httpx.RequestError as exc:
        reject_target_preflight("target_url preflight request failed")

    reject_target_preflight("target_url preflight did not complete")


def cache_key(target_url: str, tier: str) -> str:
    digest = hashlib.sha256(f"{tier}:{target_url}".encode("utf-8")).hexdigest()
    return f"axongate:markdown:{digest}"


async def get_cached_markdown(target_url: str, tier: str) -> Optional[str]:
    key = cache_key(target_url, tier)

    if redis_client:
        return await redis_client.get(key)

    async with markdown_cache_lock:
        cached = markdown_cache.get(key)
        if not cached:
            return None
        expires_at, markdown = cached
        if expires_at <= time.time():
            markdown_cache.pop(key, None)
            return None
        return markdown


async def set_cached_markdown(target_url: str, tier: str, markdown: str, ttl_seconds: int) -> None:
    if ttl_seconds <= 0:
        return

    key = cache_key(target_url, tier)

    if redis_client:
        await redis_client.setex(key, ttl_seconds, markdown)
        return

    async with markdown_cache_lock:
        markdown_cache[key] = (time.time() + ttl_seconds, markdown)


def delivery_credit_key(token: str) -> str:
    return f"axongate:delivery-credit:{stable_hash(token)}"


def delivery_request_fingerprint(target_url: str, tier: str, force_refresh: bool) -> str:
    payload = {
        "target_url": target_url,
        "tier": normalize_tier(tier),
        "force_refresh": bool(force_refresh),
    }
    return stable_hash(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def delivery_credit_response(token: str, remaining_attempts: int) -> dict[str, Any]:
    return {
        "token": token,
        "retry_endpoint": f"{PUBLIC_BASE_URL}/v1/x402/retry",
        "expires_in_seconds": DELIVERY_CREDIT_TTL_SECONDS,
        "remaining_attempts": remaining_attempts,
    }


def delivery_credit_headers(token: str, remaining_attempts: int) -> dict[str, str]:
    return {
        "X-AxonGate-Retry-Credit": token,
        "X-AxonGate-Retry-Endpoint": f"{PUBLIC_BASE_URL}/v1/x402/retry",
        "X-AxonGate-Retry-Attempts": str(remaining_attempts),
    }


async def create_delivery_credit(
    *,
    payment_reference: str,
    target_url: str,
    tier: str,
    force_refresh: bool,
    amount_usdc: Decimal,
    mode: str,
    reason: str,
    supplier_attempts_used: int,
) -> Optional[dict[str, Any]]:
    """
    Create a narrow post-payment delivery credit.

    Credits are not refunds and are not transferable. They are short-lived bearer
    tokens bound to the exact paid target URL, tier, force_refresh flag, and
    payment reference. This keeps a bad upstream/source from becoming an
    open-ended cost for AxonGate while still letting honest buyers retry a paid
    request that failed because of temporary infrastructure.
    """
    if DELIVERY_CREDIT_TTL_SECONDS <= 0 or DELIVERY_CREDIT_MAX_ATTEMPTS <= 0:
        return None

    token = secrets.token_urlsafe(32)
    key = delivery_credit_key(token)
    now = int(time.time())
    normalized_tier = normalize_tier(tier)
    record = {
        "payment_reference": payment_reference,
        "request_fingerprint": delivery_request_fingerprint(target_url, normalized_tier, force_refresh),
        "target_url": target_url,
        "tier": normalized_tier,
        "force_refresh": bool(force_refresh),
        "amount_usdc": str(amount_usdc),
        "mode": mode,
        "reason": reason,
        "supplier_attempts_used": max(0, int(supplier_attempts_used)),
        "remaining_attempts": DELIVERY_CREDIT_MAX_ATTEMPTS,
        "created_at": now,
        "expires_at": now + DELIVERY_CREDIT_TTL_SECONDS,
    }

    if redis_client:
        await redis_client.setex(key, DELIVERY_CREDIT_TTL_SECONDS, json.dumps(record, separators=(",", ":")))
    else:
        async with delivery_credits_lock:
            delivery_credits[key] = record

    inc_metric("delivery_credits_issued_total")
    return delivery_credit_response(token, DELIVERY_CREDIT_MAX_ATTEMPTS)


def validate_delivery_credit_record(record: dict[str, Any], target_url: str, tier: str, force_refresh: bool) -> None:
    if int(record.get("expires_at", 0)) <= int(time.time()):
        raise PaymentValidationError("Retry credit has expired")
    if int(record.get("remaining_attempts", 0)) <= 0:
        raise PaymentValidationError("Retry credit is exhausted")

    expected_fingerprint = delivery_request_fingerprint(target_url, tier, force_refresh)
    if record.get("request_fingerprint") != expected_fingerprint:
        raise PaymentValidationError("Retry credit does not match this target URL, tier, and cache mode")


async def reserve_delivery_credit(
    token: str,
    *,
    target_url: str,
    tier: str,
    force_refresh: bool,
) -> DeliveryCreditReservation:
    """
    Atomically reserve one retry attempt before spending upstream work.

    The reservation decrements the remaining attempt count before Jina is called,
    which prevents concurrent clients from stretching one credit into many free
    supplier calls.
    """
    if not token:
        raise PaymentValidationError("Missing retry credit")

    key = delivery_credit_key(token)
    normalized_tier = normalize_tier(tier)

    if redis_client:
        watch_error = getattr(redis, "WatchError", None)
        if watch_error is None and hasattr(redis, "exceptions"):
            watch_error = getattr(redis.exceptions, "WatchError", RuntimeError)

        for _ in range(3):
            async with redis_client.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(key)
                    raw_record = await pipe.get(key)
                    if not raw_record:
                        raise PaymentValidationError("Retry credit is invalid or expired")

                    record = json.loads(raw_record)
                    validate_delivery_credit_record(record, target_url, normalized_tier, force_refresh)
                    record["remaining_attempts"] = int(record["remaining_attempts"]) - 1
                    record["supplier_attempts_used"] = int(record.get("supplier_attempts_used", 0)) + 1
                    ttl_seconds = max(1, int(record["expires_at"]) - int(time.time()))

                    pipe.multi()
                    pipe.setex(key, ttl_seconds, json.dumps(record, separators=(",", ":")))
                    await pipe.execute()
                    return DeliveryCreditReservation(
                        token=token,
                        key=key,
                        record=record,
                        remaining_attempts=int(record["remaining_attempts"]),
                        total_supplier_attempts=int(record["supplier_attempts_used"]),
                    )
                except Exception as exc:
                    if watch_error is not None and isinstance(exc, watch_error):
                        continue
                    raise

        raise NetworkUnavailableError("Retry credit store was busy", source="redis", supplier_attempt_charged=False)

    async with delivery_credits_lock:
        record = delivery_credits.get(key)
        if not record:
            raise PaymentValidationError("Retry credit is invalid or expired")
        validate_delivery_credit_record(record, target_url, normalized_tier, force_refresh)
        record = dict(record)
        record["remaining_attempts"] = int(record["remaining_attempts"]) - 1
        record["supplier_attempts_used"] = int(record.get("supplier_attempts_used", 0)) + 1
        delivery_credits[key] = record

    return DeliveryCreditReservation(
        token=token,
        key=key,
        record=record,
        remaining_attempts=int(record["remaining_attempts"]),
        total_supplier_attempts=int(record["supplier_attempts_used"]),
    )


async def restore_delivery_credit_attempt(reservation: DeliveryCreditReservation) -> None:
    """Put a reserved attempt back when no supplier work was attempted."""
    record = dict(reservation.record)
    record["remaining_attempts"] = min(
        DELIVERY_CREDIT_MAX_ATTEMPTS,
        int(record.get("remaining_attempts", 0)) + 1,
    )
    record["supplier_attempts_used"] = max(0, int(record.get("supplier_attempts_used", 1)) - 1)
    ttl_seconds = max(1, int(record["expires_at"]) - int(time.time()))

    if redis_client:
        await redis_client.setex(reservation.key, ttl_seconds, json.dumps(record, separators=(",", ":")))
        return

    async with delivery_credits_lock:
        delivery_credits[reservation.key] = record


async def delete_delivery_credit(token: str) -> None:
    key = delivery_credit_key(token)
    if redis_client:
        await redis_client.delete(key)
        return

    async with delivery_credits_lock:
        delivery_credits.pop(key, None)


async def has_processed_tx(tx_hash: str) -> bool:
    if redis_client:
        return bool(await redis_client.exists(f"axongate:processed:{tx_hash}"))

    async with processed_txs_lock:
        return tx_hash in processed_txs


async def reserve_processed_tx(tx_hash: str) -> bool:
    if redis_client:
        return bool(await redis_client.set(f"axongate:processed:{tx_hash}", "1", nx=True))

    async with processed_txs_lock:
        if tx_hash in processed_txs:
            return False
        processed_txs.add(tx_hash)
        return True


def standard_payment_reference_key(payment_reference: str) -> str:
    return f"axongate:standard-payment:{stable_hash(payment_reference)}"


async def has_standard_payment_reference(payment_reference: str) -> bool:
    key = standard_payment_reference_key(payment_reference)
    if redis_client:
        try:
            return bool(await redis_client.exists(key))
        except Exception as exc:
            print(f"[PAYMENT] Redis standard replay lookup failed for {key}: {exc}")

    async with standard_payment_references_lock:
        return key in standard_payment_references


async def mark_standard_payment_reference(payment_reference: str) -> None:
    key = standard_payment_reference_key(payment_reference)
    if redis_client:
        try:
            await redis_client.set(key, "1")
        except Exception as exc:
            print(f"[PAYMENT] Redis standard replay marker failed for {key}: {exc}")
        else:
            return

    async with standard_payment_references_lock:
        standard_payment_references.add(key)


async def call_base_rpc(label: str, rpc_call):
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(rpc_call),
            timeout=BASE_RPC_TIMEOUT_SECONDS + 1,
        )
    except TransactionNotFound:
        raise
    except asyncio.TimeoutError as exc:
        inc_metric("base_rpc_errors_total")
        raise NetworkUnavailableError(f"Base RPC timed out during {label}") from exc
    except Exception as exc:
        inc_metric("base_rpc_errors_total")
        raise NetworkUnavailableError(f"Base RPC failed during {label}") from exc


async def maybe_query_cdp_transaction(tx_hash: str) -> Any | None:
    """
    Attempt a CDP SDK transaction lookup when the installed SDK exposes one.

    Some CDP SDK versions do not include a read-only EVM transaction method on
    client.evm. AxonGate still performs strict on-chain verification with Web3
    receipt and log data, but this hook lets Railway deployments with a newer CDP
    SDK use Coinbase's client surface without changing the validation contract.
    """
    if CdpClient is None or EvmClient is None:
        return None

    method_name = next((name for name in CDP_TRANSACTION_METHOD_NAMES if hasattr(EvmClient, name)), None)
    if method_name is None:
        return None

    api_key_id = os.getenv("CDP_API_KEY_ID")
    api_key_secret = os.getenv("CDP_API_KEY_SECRET")
    wallet_secret = os.getenv("CDP_WALLET_SECRET")
    if not api_key_id or not api_key_secret or not wallet_secret:
        return None

    try:
        async with CdpClient(
            api_key_id=api_key_id,
            api_key_secret=api_key_secret,
            wallet_secret=wallet_secret,
        ) as client:
            method = getattr(client.evm, method_name, None)
            if method is None:
                return None

            for kwargs in (
                {"transaction_hash": tx_hash, "network": "base"},
                {"tx_hash": tx_hash, "network": "base"},
                {"hash": tx_hash, "network": "base"},
            ):
                try:
                    result = method(**kwargs)
                    if inspect.isawaitable(result):
                        return await asyncio.wait_for(result, timeout=BASE_RPC_TIMEOUT_SECONDS + 1)
                    return result
                except TypeError:
                    continue
            return None
    except asyncio.TimeoutError as exc:
        raise NetworkUnavailableError("CDP transaction lookup timed out") from exc
    except Exception as exc:
        raise NetworkUnavailableError("CDP transaction lookup failed") from exc


async def ensure_base_rpc_ready() -> None:
    connected = await call_base_rpc("connectivity check", web3.is_connected)
    if not connected:
        inc_metric("base_rpc_errors_total")
        raise NetworkUnavailableError("Base RPC is not reachable")


async def fetch_current_base_fee_wei() -> int:
    """Fetch the latest Base EIP-1559 base fee for the dynamic UEG model."""
    await ensure_base_rpc_ready()
    latest_block = await call_base_rpc("latest block lookup", lambda: web3.eth.get_block("latest"))
    base_fee = latest_block.get("baseFeePerGas")
    if base_fee is None:
        gas_price = await call_base_rpc("gas price lookup", lambda: web3.eth.gas_price)
        return int(gas_price)
    return int(base_fee)


def eth_usdc_price_cache_key() -> str:
    return "axongate:price:eth-usdc"


def apply_eth_price_floor(price: Decimal, source: str, fetched_at: int) -> EthUsdQuote:
    """Use live/stale ETH price, but never below the configured conservative floor."""
    floor_applied = price < ETH_USDC_PRICE_FLOOR
    guarded_price = max(price, ETH_USDC_PRICE_FLOOR)
    return EthUsdQuote(
        price=guarded_price,
        source=f"{source}:floor" if floor_applied else source,
        fetched_at=fetched_at,
        floor_applied=floor_applied,
    )


def parse_eth_usdc_quote(payload: dict[str, Any]) -> Decimal:
    try:
        amount = payload["data"]["amount"]
    except (KeyError, TypeError) as exc:
        raise NetworkUnavailableError(
            "ETH/USD price feed response did not include data.amount",
            source="price_feed",
            supplier_attempt_charged=False,
        ) from exc

    try:
        price = Decimal(str(amount))
    except Exception as exc:
        raise NetworkUnavailableError(
            "ETH/USD price feed returned an invalid amount",
            source="price_feed",
            supplier_attempt_charged=False,
        ) from exc

    if price <= 0:
        raise NetworkUnavailableError(
            "ETH/USD price feed returned a non-positive amount",
            source="price_feed",
            supplier_attempt_charged=False,
        )

    return price


async def get_cached_eth_usdc_quote(max_age_seconds: int) -> Optional[EthUsdQuote]:
    now = int(time.time())

    if redis_client:
        raw_quote = await redis_client.get(eth_usdc_price_cache_key())
        if not raw_quote:
            return None
        quote_data = json.loads(raw_quote)
    else:
        async with eth_usdc_price_lock:
            if not eth_usdc_price_cache:
                return None
            quote_data = dict(eth_usdc_price_cache)

    fetched_at = int(quote_data.get("fetched_at", 0))
    if now - fetched_at > max_age_seconds:
        return None

    return apply_eth_price_floor(Decimal(str(quote_data["price"])), str(quote_data["source"]), fetched_at)


async def set_cached_eth_usdc_quote(price: Decimal, source: str) -> EthUsdQuote:
    fetched_at = int(time.time())
    quote_data = {
        "price": str(price),
        "source": source,
        "fetched_at": fetched_at,
    }

    if redis_client:
        await redis_client.setex(
            eth_usdc_price_cache_key(),
            ETH_USDC_PRICE_STALE_TTL_SECONDS,
            json.dumps(quote_data, separators=(",", ":")),
        )
    else:
        async with eth_usdc_price_lock:
            eth_usdc_price_cache.clear()
            eth_usdc_price_cache.update(quote_data)

    return apply_eth_price_floor(price, source, fetched_at)


async def fetch_live_eth_usdc_quote() -> EthUsdQuote:
    """Fetch live ETH/USD spot price from Coinbase's unauthenticated Prices API."""
    try:
        async with httpx.AsyncClient(timeout=ETH_USDC_PRICE_TIMEOUT_SECONDS) as client:
            response = await client.get(ETH_USDC_PRICE_URL)
            response.raise_for_status()
            price = parse_eth_usdc_quote(response.json())
    except NetworkUnavailableError:
        inc_metric("eth_price_errors_total")
        raise
    except (httpx.TimeoutException, httpx.HTTPError, json.JSONDecodeError) as exc:
        inc_metric("eth_price_errors_total")
        raise NetworkUnavailableError(
            "ETH/USD price feed is temporarily unavailable",
            source="price_feed",
            supplier_attempt_charged=False,
        ) from exc

    inc_metric("eth_price_fetches_total")
    return await set_cached_eth_usdc_quote(price, "coinbase_spot")


async def fetch_eth_usdc_quote() -> EthUsdQuote:
    """
    Return the ETH/USD quote used by UEG.

    The service uses a fresh Redis/memory cache first, then Coinbase live spot,
    then stale cache. Static fallback is disabled by default so AxonGate fails
    closed when it cannot price gas with either live or recently cached data.
    """
    fresh_quote = await get_cached_eth_usdc_quote(ETH_USDC_PRICE_CACHE_TTL_SECONDS)
    if fresh_quote:
        inc_metric("eth_price_cache_hits_total")
        return fresh_quote

    try:
        return await fetch_live_eth_usdc_quote()
    except NetworkUnavailableError:
        stale_quote = await get_cached_eth_usdc_quote(ETH_USDC_PRICE_STALE_TTL_SECONDS)
        if stale_quote:
            inc_metric("eth_price_stale_hits_total")
            return EthUsdQuote(
                price=stale_quote.price,
                source=f"stale_{stale_quote.source}",
                fetched_at=stale_quote.fetched_at,
                floor_applied=stale_quote.floor_applied,
            )

        if ETH_USDC_ALLOW_STATIC_FALLBACK:
            return EthUsdQuote(
                price=ETH_USDC_PRICE_FLOOR,
                source="static_floor",
                fetched_at=int(time.time()),
                floor_applied=True,
            )

        raise


async def calculate_profitability() -> UEGReceipt:
    """Calculate recommended-tier revenue minus live Base gas estimate and Jina cost."""
    return await calculate_profitability_for_price(price_for_tier(RECOMMENDED_TIER))


async def calculate_profitability_for_price(revenue_usdc: Decimal, supplier_attempts: int = 1) -> UEGReceipt:
    """Calculate tier revenue minus live Base gas estimate and bounded supplier cost."""
    inc_metric("ueg_checks_total")
    base_fee_wei, eth_quote = await asyncio.gather(fetch_current_base_fee_wei(), fetch_eth_usdc_quote())
    gas_cost_eth = Decimal(base_fee_wei * UEG_GAS_UNITS) / Decimal(10**18)
    dynamic_gas_cost_usdc = gas_cost_eth * eth_quote.price
    bounded_supplier_attempts = max(0, int(supplier_attempts))
    total_jina_cost_usdc = JINA_API_COST_USDC * Decimal(bounded_supplier_attempts)
    projected_profit = revenue_usdc - (dynamic_gas_cost_usdc + total_jina_cost_usdc)

    return UEGReceipt(
        revenue_usdc=revenue_usdc,
        dynamic_gas_cost_usdc=dynamic_gas_cost_usdc,
        jina_api_cost_usdc=total_jina_cost_usdc,
        projected_profit_usdc=projected_profit,
        base_fee_wei=base_fee_wei,
        gas_units=UEG_GAS_UNITS,
        supplier_attempts=bounded_supplier_attempts,
        eth_usdc_price=eth_quote.price,
        eth_usdc_price_source=eth_quote.source,
        eth_usdc_floor_applied=eth_quote.floor_applied,
    )


async def check_profitability() -> bool:
    """Return True only when the Clean Context Broker margin is > 0.01 USDC."""
    receipt = await calculate_profitability()
    return receipt.projected_profit_usdc > MIN_PROFIT_MARGIN_USDC


def x402_dynamic_price(context: HTTPRequestContext):
    """Return tier-aware x402 price for PayAI middleware."""
    tier = None
    pack = None
    if context:
        if context.path.endswith("/v1/x402/proof-pack"):
            if context.adapter:
                pack = context.adapter.get_query_param("pack") or context.adapter.get_header("x-axongate-pack")
        elif context.path.endswith("/v1/x402/starter"):
            tier = STARTER_TIER
        elif context.adapter:
            tier = context.adapter.get_query_param("tier") or context.adapter.get_header("x-axongate-tier")

    if pack is not None or (context and context.path.endswith("/v1/x402/proof-pack")):
        normalized_pack = normalize_proof_pack(pack)
        price = price_for_proof_pack(normalized_pack)
        return AssetAmount(
            amount=str(usdc_units(price)),
            asset=BASE_USDC_ADDRESS,
            extra={
                "name": BASE_USDC_TOKEN_NAME,
                "version": BASE_USDC_TOKEN_VERSION,
                "decimals": USDC_DECIMALS,
                "pack": normalized_pack,
            },
        )

    normalized_tier = normalize_tier(tier)
    price = price_for_tier(normalized_tier)
    return AssetAmount(
        amount=str(usdc_units(price)),
        asset=BASE_USDC_ADDRESS,
        extra={
            "name": BASE_USDC_TOKEN_NAME,
            "version": BASE_USDC_TOKEN_VERSION,
            "decimals": USDC_DECIMALS,
            "tier": normalized_tier,
        },
    )


configure_standard_x402_middleware()


@app.middleware("http")
async def enrich_402_buyer_guidance(request: Request, call_next):
    response = await call_next(request)
    has_payment_terms = response.headers.get("PAYMENT-REQUIRED") or response.headers.get("X-Payment-Required")
    if response.status_code == 402 and has_payment_terms:
        for name, value in buyer_guidance_headers().items():
            if name not in response.headers:
                response.headers[name] = value
    return response


async def verify_x402_payment(tx_hash: str, expected_fee_usdc: Decimal = REQUIRED_USDC_FEE) -> PaymentVerification:
    """
    Verify the AxonGate x402 payment hash against Base mainnet.

    The Clean Context Broker accepts one paid request per successful transaction.
    This validator normalizes and replay-checks the hash, probes CDP if the SDK
    provides a read-only transaction method, then uses Base RPC receipt data to
    enforce the economic facts: the transaction succeeded, it called the Base USDC
    contract, and its ERC-20 Transfer event sent the expected tier amount to the
    AxonGate vault. Only after those checks pass is the hash stored in processed_txs.
    """
    normalized_hash = normalize_tx_hash(tx_hash)

    if await has_processed_tx(normalized_hash):
        raise PaymentValidationError("Payment hash has already been processed")

    vault_address = load_vault_address()
    expected_amount = usdc_units(expected_fee_usdc)
    await maybe_query_cdp_transaction(normalized_hash)
    await ensure_base_rpc_ready()

    try:
        transaction, receipt = await asyncio.gather(
            call_base_rpc("transaction lookup", lambda: web3.eth.get_transaction(normalized_hash)),
            call_base_rpc("transaction receipt lookup", lambda: web3.eth.get_transaction_receipt(normalized_hash)),
        )
    except TransactionNotFound as exc:
        raise PaymentValidationError("Payment transaction was not found on Base") from exc

    if receipt.get("status") != 1:
        raise PaymentValidationError("Payment transaction was not successful")

    transaction_to = transaction.get("to")
    if not transaction_to or Web3.to_checksum_address(transaction_to) != BASE_USDC_ADDRESS:
        raise PaymentValidationError("Payment transaction must call the Base USDC contract")

    vault_topic = "0x" + vault_address.lower().replace("0x", "").rjust(64, "0")
    total_transferred_to_vault = 0

    for log in receipt.get("logs", []):
        log_address = Web3.to_checksum_address(log.get("address"))
        topics = [as_0x_hex(topic) for topic in log.get("topics", [])]

        if log_address != BASE_USDC_ADDRESS:
            continue
        if len(topics) < 3 or topics[0].lower() != TRANSFER_TOPIC.lower():
            continue
        if topics[2].lower() != vault_topic:
            continue

        total_transferred_to_vault += int(as_0x_hex(log.get("data", "0x0")), 16)

    if total_transferred_to_vault != expected_amount:
        raise PaymentValidationError(f"Payment must transfer exactly {expected_fee_usdc} USDC to AxonGate")

    if not await reserve_processed_tx(normalized_hash):
        inc_metric("payment_replay_rejections_total")
        raise PaymentValidationError("Payment hash has already been processed")

    return PaymentVerification(
        tx_hash=normalized_hash,
        vault_address=vault_address,
        token_address=BASE_USDC_ADDRESS,
        amount_usdc=expected_fee_usdc,
    )


async def fetch_clean_markdown(target_url: str) -> str:
    """Call Jina Reader and return the upstream markdown body."""
    if not JINA_API_KEY:
        raise NetworkUnavailableError(
            "Jina API key is not configured",
            source="jina_config",
            supplier_attempt_charged=False,
        )

    if PREFLIGHT_ENABLED:
        await preflight_target_url(target_url)

    inc_metric("jina_requests_total")
    reader_url = f"{JINA_READER_BASE_URL.rstrip('/')}/{target_url}"
    headers = {"Authorization": f"Bearer {JINA_API_KEY}"}

    try:
        async with httpx.AsyncClient(timeout=JINA_TIMEOUT_SECONDS, follow_redirects=True) as client:
            response = await client.get(reader_url, headers=headers)
            response.raise_for_status()
            inc_metric("jina_success_total")
            return response.text
    except httpx.TimeoutException as exc:
        inc_metric("jina_errors_total")
        raise NetworkUnavailableError(
            "Jina Reader timed out",
            source="jina",
            supplier_attempt_charged=True,
        ) from exc
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        if status_code == 408 or status_code == 429 or status_code >= 500:
            inc_metric("jina_errors_total")
            raise NetworkUnavailableError(
                f"Jina Reader temporarily failed with HTTP {status_code}",
                source="jina",
                supplier_attempt_charged=True,
            ) from exc

        inc_metric("supplier_rejections_total")
        raise PaymentValidationError(
            f"Jina Reader rejected target_url with HTTP {status_code}; no retry credit issued",
            consume_credit=True,
        ) from exc
    except httpx.RequestError as exc:
        inc_metric("jina_errors_total")
        raise NetworkUnavailableError(
            "Jina Reader request failed",
            source="jina",
            supplier_attempt_charged=False,
        ) from exc


async def get_clean_markdown(target_url: str, tier: str, force_refresh: bool = False) -> tuple[str, bool]:
    """Return cleaned markdown, using cache when the selected tier allows it."""
    normalized_tier = normalize_tier(tier)
    cached = await get_cache_candidate_for_tier(target_url, normalized_tier, force_refresh)
    if cached is not None:
        inc_metric("cache_hits_total")
        return cached, True

    inc_metric("cache_misses_total")
    if is_cache_only_tier(normalized_tier):
        raise PaymentValidationError(
            "Starter and cached tiers require the starter sample or an existing AxonGate cache entry. Use basic, fresh, or deep for a live fetch."
        )

    markdown = await fetch_clean_markdown(target_url)
    await set_cached_markdown(target_url, normalized_tier, markdown, cache_ttl_for_tier(normalized_tier, force_refresh))
    return markdown, False


async def get_cache_candidate_for_tier(target_url: str, tier: str, force_refresh: bool = False) -> Optional[str]:
    """Return a cache entry that the requested tier is allowed to consume."""
    normalized_tier = normalize_tier(tier)
    if force_refresh or normalized_tier == "fresh":
        return None

    if normalized_tier == STARTER_TIER:
        sample_markdown = starter_sample_markdown_for_target(target_url)
        if sample_markdown is not None:
            return sample_markdown

    if is_cache_only_tier(normalized_tier):
        for source_tier in CACHED_TIER_CACHE_SOURCES:
            cached = await get_cached_markdown(target_url, source_tier)
            if cached is not None:
                return cached
        return None

    return await get_cached_markdown(target_url, normalized_tier)


async def deliver_paid_markdown(
    *,
    target_url: str,
    tier: str,
    force_refresh: bool,
    amount_usdc: Decimal,
    supplier_attempts_on_hit: int = 0,
    supplier_attempts_on_miss: int = 1,
) -> tuple[str, bool, UEGReceipt]:
    """Check cache-aware economics, then deliver markdown without upstream work on cached misses."""
    normalized_tier = normalize_tier(tier)
    if is_cache_only_tier(normalized_tier) and force_refresh:
        raise PaymentValidationError("Starter and cached tiers cannot force refresh. Use basic, fresh, or deep for a live fetch.")

    cached = await get_cache_candidate_for_tier(target_url, normalized_tier, force_refresh)
    if cached is not None:
        inc_metric("cache_hits_total")
        profitability = await calculate_profitability_for_price(amount_usdc, supplier_attempts_on_hit)
        if profitability.projected_profit_usdc <= MIN_PROFIT_MARGIN_USDC:
            inc_metric("ueg_rejections_total")
            raise PaymentValidationError("Dynamic UEG rejected request; projected margin is too low")
        return cached, True, profitability

    inc_metric("cache_misses_total")
    if is_cache_only_tier(normalized_tier):
        raise PaymentValidationError(
            "Starter and cached tiers require the starter sample or an existing AxonGate cache entry. Use basic, fresh, or deep for a live fetch."
        )

    profitability = await calculate_profitability_for_price(amount_usdc, supplier_attempts_on_miss)
    if profitability.projected_profit_usdc <= MIN_PROFIT_MARGIN_USDC:
        inc_metric("ueg_rejections_total")
        raise PaymentValidationError("Dynamic UEG rejected request; projected margin is too low")

    markdown = await fetch_clean_markdown(target_url)
    await set_cached_markdown(target_url, normalized_tier, markdown, cache_ttl_for_tier(normalized_tier, force_refresh))
    return markdown, False, profitability


async def maybe_issue_delivery_credit(
    *,
    exc: NetworkUnavailableError,
    payment_reference: str,
    target_url: str,
    tier: str,
    force_refresh: bool,
    amount_usdc: Decimal,
    mode: str,
) -> Optional[dict[str, Any]]:
    """Issue a bounded retry credit only for post-payment retryable failures."""
    if not exc.creditable:
        return None

    supplier_attempts_used = 1 if exc.supplier_attempt_charged else 0
    projected_retry_attempts = supplier_attempts_used + 1

    try:
        retry_receipt = await calculate_profitability_for_price(amount_usdc, projected_retry_attempts)
        if retry_receipt.projected_profit_usdc <= MIN_PROFIT_MARGIN_USDC:
            inc_metric("delivery_credit_exhausted_total")
            return None
    except NetworkUnavailableError:
        # If Base RPC is the outage, the retry endpoint will enforce economics
        # before spending supplier work. Do not strand a paid buyer here.
        pass

    return await create_delivery_credit(
        payment_reference=payment_reference,
        target_url=target_url,
        tier=tier,
        force_refresh=force_refresh,
        amount_usdc=amount_usdc,
        mode=mode,
        reason=exc.detail,
        supplier_attempts_used=supplier_attempts_used,
    )


def retry_later_503(exc: Exception, credit: Optional[dict[str, Any]] = None) -> HTTPException:
    inc_metric("errors_total")
    inc_metric("retryable_outages_total")
    print(f"[UPSTREAM] Temporary failure: {exc}")
    headers = {"Retry-After": "5"}
    detail: Any = {
        "message": "Upstream service temporarily unavailable. Client agent should retry in 5 seconds.",
        "source": getattr(exc, "source", "network"),
        "retry_after_seconds": 5,
    }
    if credit:
        headers.update(delivery_credit_headers(credit["token"], int(credit["remaining_attempts"])))
        detail["retry_credit"] = credit

    return HTTPException(
        status_code=503,
        detail=detail,
        headers=headers,
    )


def rate_limit_429(exc: RateLimitExceeded, credit: Optional[dict[str, Any]] = None) -> HTTPException:
    headers = {"Retry-After": str(exc.retry_after_seconds)}
    detail: Any = {
        "message": exc.detail,
        "bucket": exc.bucket,
        "limit": exc.limit,
        "retry_after_seconds": exc.retry_after_seconds,
    }
    if credit:
        headers.update(delivery_credit_headers(credit["token"], int(credit["remaining_attempts"])))
        detail["retry_credit"] = credit

    return HTTPException(status_code=429, detail=detail, headers=headers)


def public_url(path: str) -> str:
    """Return an absolute URL for public discovery documents."""
    return f"{PUBLIC_BASE_URL}{path}"


def quote_payment_probe_url(tier: str, source: str) -> str:
    normalized_tier = normalize_tier(tier)
    normalized_source = normalize_attribution_source(source)
    return f"{PUBLIC_BASE_URL}/v1/x402/access?tier={normalized_tier}&source={normalized_source}"


def quote_buyer_command(target_url: str, tier: str, source: str) -> str:
    normalized_tier = normalize_tier(tier)
    normalized_source = normalize_attribution_source(source)
    force_flag = " --force-refresh" if normalized_tier == "fresh" else ""
    return (
        "npm run paid:buyer -- "
        '--wallet-file "C:/path/to/buyer_wallet.json" '
        f'--target-url "{target_url}" '
        f"--tier {normalized_tier}{force_flag} "
        f"--confirm-spend {TIER_PRICING_USDC[normalized_tier]} "
        f"--source {normalized_source}"
    )


async def build_conversion_quote(target_url: str, source: str = "quote") -> dict[str, Any]:
    """Return supplier-free pricing and next-step guidance for a target URL."""
    normalized_target = await assert_public_target_url(target_url)
    normalized_source = normalize_attribution_source(source)
    starter_available = await get_cache_candidate_for_tier(normalized_target, STARTER_TIER, False) is not None
    cached_available = await get_cache_candidate_for_tier(normalized_target, CACHE_ONLY_TIER, False) is not None
    recommended_tier = STARTER_TIER if starter_available else "fresh"

    tiers: dict[str, dict[str, Any]] = {}
    for tier, price in TIER_PRICING_USDC.items():
        cache_only = is_cache_only_tier(tier)
        if tier == STARTER_TIER:
            available_now = starter_available
        elif tier == CACHE_ONLY_TIER:
            available_now = cached_available
        else:
            available_now = True
        tiers[tier] = {
            "price_usdc": float(price),
            "amount_units": str(usdc_units(price)),
            "cache_policy": cache_policy_for_tier(tier),
            "available_now": available_now,
            "requires_supplier_on_miss": not cache_only,
            "force_refresh_supported": not cache_only,
            "payment_probe_url": quote_payment_probe_url(tier, normalized_source),
        }

    recommended_price = TIER_PRICING_USDC[recommended_tier]
    return {
        "status": "quote",
        "supplier_spend": False,
        "target_url": normalized_target,
        "source": normalized_source,
        "recommended_tier": recommended_tier,
        "recommended_reason": (
            "starter is available for this sample or cached target"
            if recommended_tier == STARTER_TIER
            else "use fresh for live public web context because starter/cache is not available for this target"
        ),
        "starter_available": starter_available,
        "cached_available": cached_available,
        "tiers": tiers,
        "next_steps": {
            "probe_payment_terms": quote_payment_probe_url(recommended_tier, normalized_source),
            "paid_endpoint": f"{PUBLIC_BASE_URL}/v1/x402/access?tier={recommended_tier}&source={normalized_source}",
            "confirm_spend_usdc": str(recommended_price),
            "buyer_command": quote_buyer_command(normalized_target, recommended_tier, normalized_source),
            "mcp_tool": "fetch_clean_context",
            "quickstart": public_url("/quickstart"),
            "paid_test": public_url("/paid-test"),
        },
    }


PROOF_PACK_LLM_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string", "minLength": 1},
        "executive_summary": {"type": "string", "minLength": 1},
        "confidence_score": {"type": "number", "minimum": 0, "maximum": 1},
        "key_claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string", "minLength": 1},
                    "citation_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                    },
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["claim", "citation_ids", "confidence"],
                "additionalProperties": False,
            },
        },
        "risks": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["answer", "executive_summary", "confidence_score", "key_claims", "risks"],
    "additionalProperties": False,
}


def proof_pack_payment_probe_url(pack: str, source: str) -> str:
    normalized_pack = normalize_proof_pack(pack)
    normalized_source = normalize_attribution_source(source)
    return f"{PUBLIC_BASE_URL}/v1/x402/proof-pack?pack={normalized_pack}&source={normalized_source}"


def proof_pack_buyer_command(target_url: str, question: str, pack: str, source: str) -> str:
    normalized_pack = normalize_proof_pack(pack)
    normalized_source = normalize_attribution_source(source)
    return (
        "npm run paid:buyer -- "
        "--product proof-pack "
        '--wallet-file "C:/path/to/buyer_wallet.json" '
        f'--target-url "{target_url}" '
        f'--question "{question}" '
        f"--pack {normalized_pack} "
        f"--confirm-spend {PROOF_PACK_PRICING_USDC[normalized_pack]} "
        f"--source {normalized_source}"
    )


def proof_pack_question(value: Optional[str]) -> str:
    question = re.sub(r"\s+", " ", (value or "What does this source establish?").strip())
    return question[:600] or "What does this source establish?"


def proof_pack_evidence_limit(pack: str) -> int:
    normalized_pack = normalize_proof_pack(pack)
    return {"quick": 5, DEFAULT_PROOF_PACK: 8, "deep": 12}[normalized_pack]


def proof_pack_llm_input_limit(pack: str) -> int:
    normalized_pack = normalize_proof_pack(pack)
    if normalized_pack == "quick":
        return min(LLM_MAX_INPUT_CHARS, 12000)
    if normalized_pack == "deep":
        return min(max(LLM_MAX_INPUT_CHARS, 24000), 50000)
    return LLM_MAX_INPUT_CHARS


def clean_evidence_excerpt(value: str, max_chars: int = 420) -> str:
    cleaned = re.sub(r"\s+", " ", value).strip(" -\t\r\n")
    cleaned = re.sub(r"^#+\s*", "", cleaned)
    if len(cleaned) <= max_chars:
        return cleaned
    clipped = cleaned[:max_chars].rsplit(" ", 1)[0].strip()
    return f"{clipped}..."


def split_markdown_evidence(markdown: str) -> list[str]:
    """Extract stable paragraph-like evidence candidates from markdown."""
    candidates: list[str] = []
    buffer: list[str] = []
    in_code = False

    def flush() -> None:
        if not buffer:
            return
        text = clean_evidence_excerpt(" ".join(buffer))
        buffer.clear()
        if len(text) >= 45 and not text.lower().startswith(("image:", "title:")):
            candidates.append(text)

    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if line.startswith("```"):
            flush()
            in_code = not in_code
            continue
        if in_code:
            continue
        if not line:
            flush()
            continue
        if line.startswith("#"):
            flush()
            heading = clean_evidence_excerpt(line)
            if len(heading) >= 12:
                candidates.append(heading)
            continue
        if line.startswith("|") and line.endswith("|"):
            flush()
            table_text = clean_evidence_excerpt(line.replace("|", " "))
            if len(table_text) >= 45:
                candidates.append(table_text)
            continue
        if re.match(r"^[-*+]\s+", line) or re.match(r"^\d+[.)]\s+", line):
            flush()
            bullet = clean_evidence_excerpt(re.sub(r"^([-*+]|\d+[.)])\s+", "", line))
            if len(bullet) >= 35:
                candidates.append(bullet)
            continue

        buffer.append(line)
        if sum(len(item) for item in buffer) > 520:
            flush()

    flush()
    return candidates


def extract_proof_pack_evidence(markdown: str, target_url: str, pack: str = DEFAULT_PROOF_PACK) -> list[dict[str, Any]]:
    """Create stable citation IDs from extracted source material."""
    max_items = proof_pack_evidence_limit(pack)
    seen: set[str] = set()
    citations: list[dict[str, Any]] = []

    for candidate in split_markdown_evidence(markdown):
        fingerprint = stable_hash(candidate.lower())[:16]
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        citations.append(
            {
                "id": f"c{len(citations) + 1}",
                "url": target_url,
                "excerpt": candidate,
                "fingerprint": fingerprint,
            }
        )
        if len(citations) >= max_items:
            break

    if not citations:
        fallback_excerpt = clean_evidence_excerpt(markdown[:600] or "No extractable source text was returned.")
        citations.append(
            {
                "id": "c1",
                "url": target_url,
                "excerpt": fallback_excerpt,
                "fingerprint": stable_hash(fallback_excerpt.lower())[:16],
            }
        )

    return citations


def claim_from_excerpt(excerpt: str) -> str:
    first_sentence = re.split(r"(?<=[.!?])\s+", excerpt, maxsplit=1)[0]
    return clean_evidence_excerpt(first_sentence, 220)


def clamp_confidence(value: Any, fallback: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = fallback
    return round(max(0.0, min(1.0, number)), 2)


def deterministic_proof_pack(
    *,
    target_url: str,
    question: str,
    pack: str,
    markdown: str,
    citations: list[dict[str, Any]],
    cache_hit: bool,
    fallback_reason: str,
) -> dict[str, Any]:
    normalized_pack = normalize_proof_pack(pack)
    confidence = {
        "quick": 0.46,
        DEFAULT_PROOF_PACK: 0.54,
        "deep": 0.6,
    }[normalized_pack]
    confidence = clamp_confidence(confidence + min(len(citations), 8) * 0.015)
    cited_summary = " ".join(citation["excerpt"] for citation in citations[:3])
    executive_summary = clean_evidence_excerpt(cited_summary, 620)
    key_claims = [
        {
            "claim": claim_from_excerpt(citation["excerpt"]),
            "citation_ids": [citation["id"]],
            "confidence": confidence,
        }
        for citation in citations[: min(5, len(citations))]
    ]
    risks = [
        "Deterministic extractive fallback was used; no generative cross-check was applied.",
        "Only one source URL was evaluated.",
    ]
    if cache_hit:
        risks.append("Source material may come from cache; inspect source_profile.content_sha256 for repeatability.")
    if fallback_reason != "llm_disabled":
        risks.append(f"LLM generation fallback reason: {fallback_reason}.")

    return {
        "answer": (
            f"Evidence for '{question}' is limited to the cited excerpts from {target_url}. "
            f"The strongest extractive support is: {executive_summary}"
        ),
        "executive_summary": executive_summary,
        "confidence_score": confidence,
        "key_claims": key_claims,
        "risks": risks,
        "llm_used": False,
        "llm_model": None,
        "fallback_reason": fallback_reason,
        "source_profile": {
            "final_url": target_url,
            "content_sha256": stable_hash(markdown),
            "markdown_chars": len(markdown),
            "citation_count": len(citations),
        },
    }


def extract_response_output_text(payload: dict[str, Any]) -> str:
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text

    chunks: list[str] = []
    for item in payload.get("output", []) if isinstance(payload.get("output"), list) else []:
        content = item.get("content") if isinstance(item, dict) else None
        if not isinstance(content, list):
            continue
        for content_item in content:
            if not isinstance(content_item, dict):
                continue
            text = content_item.get("text")
            if isinstance(text, str):
                chunks.append(text)
    return "\n".join(chunks).strip()


def parse_llm_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return json.loads(cleaned)


async def call_proof_pack_llm(
    *,
    target_url: str,
    question: str,
    pack: str,
    citations: list[dict[str, Any]],
    markdown: str,
) -> dict[str, Any]:
    normalized_pack = normalize_proof_pack(pack)
    model = LLM_FAST_MODEL if normalized_pack == "quick" else LLM_MODEL
    evidence_payload = {
        "target_url": target_url,
        "question": question,
        "pack": normalized_pack,
        "citations": [
            {"id": citation["id"], "url": citation["url"], "excerpt": citation["excerpt"]}
            for citation in citations
        ],
        "source_excerpt": markdown[: proof_pack_llm_input_limit(normalized_pack)],
    }
    system_prompt = (
        "You create concise evidence reports for agent builders. "
        "Return only JSON matching the requested schema. "
        "Every key_claim must cite one or more provided citation ids. "
        "Do not invent citations or facts outside the evidence."
    )
    user_prompt = json.dumps(
        {
            "task": "Create an AxonGate Proof Pack from the cited evidence.",
            "schema": PROOF_PACK_LLM_SCHEMA,
            "evidence": evidence_payload,
        },
        ensure_ascii=True,
    )
    response_payload = {
        "model": model,
        "input": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=LLM_TIMEOUT_SECONDS) as client:
        response = await client.post(f"{LLM_BASE_URL}/responses", headers=headers, json=response_payload)
        response.raise_for_status()
        output_text = extract_response_output_text(response.json())
    if not output_text:
        raise ValueError("OpenAI Responses API returned no output text")
    data = parse_llm_json(output_text)
    validate_json_schema(instance=data, schema=PROOF_PACK_LLM_SCHEMA)
    data["llm_model"] = model
    return data


def sanitize_llm_proof_pack(
    llm_data: dict[str, Any],
    citations: list[dict[str, Any]],
    fallback: dict[str, Any],
) -> dict[str, Any]:
    allowed_ids = {citation["id"] for citation in citations}
    supported_claims: list[dict[str, Any]] = []
    for claim in llm_data.get("key_claims", []):
        citation_ids = [
            citation_id
            for citation_id in claim.get("citation_ids", [])
            if isinstance(citation_id, str) and citation_id in allowed_ids
        ]
        if not citation_ids:
            continue
        supported_claims.append(
            {
                "claim": clean_evidence_excerpt(str(claim.get("claim", "")), 260),
                "citation_ids": citation_ids,
                "confidence": clamp_confidence(claim.get("confidence"), 0.45),
            }
        )

    if not supported_claims:
        return fallback

    risks = [clean_evidence_excerpt(str(risk), 260) for risk in llm_data.get("risks", []) if str(risk).strip()]
    if len(supported_claims) < len(llm_data.get("key_claims", [])):
        risks.append("Unsupported LLM claims were dropped because they did not cite extracted evidence IDs.")

    return {
        **fallback,
        "answer": clean_evidence_excerpt(str(llm_data["answer"]), 1800),
        "executive_summary": clean_evidence_excerpt(str(llm_data["executive_summary"]), 900),
        "confidence_score": clamp_confidence(llm_data.get("confidence_score"), fallback["confidence_score"]),
        "key_claims": supported_claims,
        "risks": risks or fallback["risks"],
        "llm_used": True,
        "llm_model": str(llm_data.get("llm_model") or LLM_MODEL),
        "fallback_reason": None,
    }


async def generate_proof_pack_content(
    *,
    target_url: str,
    question: str,
    pack: str,
    markdown: str,
    cache_hit: bool,
) -> dict[str, Any]:
    normalized_pack = normalize_proof_pack(pack)
    citations = extract_proof_pack_evidence(markdown, target_url, normalized_pack)
    fallback_reason = "llm_disabled"
    fallback = deterministic_proof_pack(
        target_url=target_url,
        question=question,
        pack=normalized_pack,
        markdown=markdown,
        citations=citations,
        cache_hit=cache_hit,
        fallback_reason=fallback_reason,
    )

    if LLM_ENABLED and LLM_API_KEY:
        try:
            llm_data = await call_proof_pack_llm(
                target_url=target_url,
                question=question,
                pack=normalized_pack,
                citations=citations,
                markdown=markdown,
            )
            proof_content = sanitize_llm_proof_pack(llm_data, citations, fallback)
            if proof_content.get("llm_used"):
                inc_metric("proof_pack_llm_success_total")
                return {**proof_content, "citations": citations}
            fallback_reason = "llm_unsupported_claims"
        except (httpx.HTTPError, JsonSchemaValidationError, json.JSONDecodeError, ValueError) as exc:
            fallback_reason = exc.__class__.__name__
        except Exception as exc:
            fallback_reason = exc.__class__.__name__

    inc_metric("proof_pack_llm_fallback_total")
    fallback = deterministic_proof_pack(
        target_url=target_url,
        question=question,
        pack=normalized_pack,
        markdown=markdown,
        citations=citations,
        cache_hit=cache_hit,
        fallback_reason=fallback_reason,
    )
    return {**fallback, "citations": citations}


def ueg_receipt_payload(profitability: UEGReceipt) -> dict[str, Any]:
    return {
        "revenue_usdc": float(profitability.revenue_usdc),
        "dynamic_gas_cost_usdc": float(profitability.dynamic_gas_cost_usdc),
        "jina_api_cost_usdc": float(profitability.jina_api_cost_usdc),
        "projected_profit_usdc": float(profitability.projected_profit_usdc),
        "minimum_margin_usdc": float(MIN_PROFIT_MARGIN_USDC),
        "base_fee_wei": profitability.base_fee_wei,
        "gas_units": profitability.gas_units,
        "supplier_attempts": profitability.supplier_attempts,
        "eth_usdc_price": float(profitability.eth_usdc_price),
        "eth_usdc_price_source": profitability.eth_usdc_price_source,
        "eth_usdc_floor_applied": profitability.eth_usdc_floor_applied,
    }


async def build_proof_pack_quote(
    target_url: str,
    question: Optional[str] = None,
    pack: Optional[str] = None,
    source: str = "proof-pack",
) -> dict[str, Any]:
    """Return no-spend Proof Pack pricing and payment guidance for a target URL."""
    normalized_target = await assert_public_target_url(target_url)
    normalized_source = normalize_attribution_source(source)
    normalized_pack = normalize_proof_pack(pack)
    normalized_question = proof_pack_question(question)
    internal_tier = proof_pack_internal_tier(normalized_pack)
    cached_available = await get_cache_candidate_for_tier(normalized_target, internal_tier, False) is not None

    packs: dict[str, dict[str, Any]] = {}
    for pack_name, price in PROOF_PACK_PRICING_USDC.items():
        pack_internal_tier = proof_pack_internal_tier(pack_name)
        pack_cached_available = await get_cache_candidate_for_tier(normalized_target, pack_internal_tier, False) is not None
        packs[pack_name] = {
            "price_usdc": float(price),
            "amount_units": str(usdc_units(price)),
            "cache_policy": proof_pack_cache_policy(pack_name),
            "cached_source_available": pack_cached_available,
            "internal_context_tier": pack_internal_tier,
            "payment_probe_url": proof_pack_payment_probe_url(pack_name, normalized_source),
        }

    return {
        "status": "proof_pack_quote",
        "supplier_spend": False,
        "target_url": normalized_target,
        "question": normalized_question,
        "source": normalized_source,
        "pack": normalized_pack,
        "price_usdc": float(price_for_proof_pack(normalized_pack)),
        "amount_units": str(usdc_units(price_for_proof_pack(normalized_pack))),
        "cached_source_available": cached_available,
        "packs": packs,
        "next_steps": {
            "probe_payment_terms": proof_pack_payment_probe_url(normalized_pack, normalized_source),
            "paid_endpoint": f"{PUBLIC_BASE_URL}/v1/x402/proof-pack?pack={normalized_pack}&source={normalized_source}",
            "confirm_spend_usdc": str(price_for_proof_pack(normalized_pack)),
            "buyer_command": proof_pack_buyer_command(
                normalized_target,
                normalized_question,
                normalized_pack,
                normalized_source,
            ),
            "proof_pack_page": public_url("/proof-pack"),
        },
    }


def resolve_proof_pack_selection(
    request: Request,
    proof_request: ProofPackRequest,
    x_axongate_pack: Optional[str],
    *,
    require_challenge_selector: bool,
) -> str:
    query_pack = request.query_params.get("pack")
    body_pack = normalize_proof_pack(proof_request.pack)
    challenge_pack = x_axongate_pack or query_pack
    if require_challenge_selector and challenge_pack is None and body_pack != DEFAULT_PROOF_PACK:
        raise PaymentValidationError(
            "Set pack with ?pack= or X-AxonGate-Pack so the x402 payment challenge matches the requested Proof Pack."
        )
    normalized_pack = normalize_proof_pack(challenge_pack or body_pack)
    if body_pack != normalized_pack:
        raise PaymentValidationError("Proof Pack body pack must match ?pack= or X-AxonGate-Pack.")
    return normalized_pack


def build_quote_html(quote: dict[str, Any]) -> str:
    """Render a compact quote page that turns discovery into a paid test."""
    public = html.escape(PUBLIC_BASE_URL)
    target = html.escape(str(quote["target_url"]))
    recommended_tier = html.escape(str(quote["recommended_tier"]))
    recommended_reason = html.escape(str(quote["recommended_reason"]))
    starter_state = "available" if quote["starter_available"] else "not available"
    cached_state = "available" if quote["cached_available"] else "not available"
    api_url = html.escape(
        f'{PUBLIC_BASE_URL}/v1/x402/quote?target_url={url_quote(str(quote["target_url"]), safe="")}&source=quote'
    )
    tiers_rows = "\n".join(
        "<tr>"
        f"<td><code>{html.escape(tier)}</code></td>"
        f"<td>{html.escape(str(info['price_usdc']))} USDC</td>"
        f"<td>{html.escape(info['amount_units'])}</td>"
        f"<td>{'yes' if info['available_now'] else 'no'}</td>"
        f"<td>{html.escape(info['cache_policy'])}</td>"
        "</tr>"
        for tier, info in quote["tiers"].items()
    )
    buyer_command = html.escape(quote["next_steps"]["buyer_command"])
    probe_url = html.escape(quote["next_steps"]["probe_payment_terms"])

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AxonGate Quote</title>
  <style>
    :root {{
      color-scheme: light dark;
      --bg: #101318;
      --panel: #181d24;
      --text: #f5f7fb;
      --muted: #b8c2cf;
      --line: #303844;
      --accent: #78d6b6;
      --code: #0b0f14;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.55;
      background: var(--bg);
      color: var(--text);
    }}
    main {{ max-width: 980px; margin: 0 auto; padding: 44px 22px 72px; }}
    h1 {{ font-size: 2.4rem; line-height: 1.05; margin: 0 0 12px; }}
    h2 {{ margin: 34px 0 12px; font-size: 1.25rem; }}
    p, td {{ color: var(--muted); }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    form {{ display: flex; gap: 10px; margin: 24px 0; flex-wrap: wrap; }}
    input {{
      flex: 1 1 420px;
      min-width: 0;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 11px 12px;
      background: var(--panel);
      color: var(--text);
      font: inherit;
    }}
    button {{
      border: 1px solid var(--accent);
      border-radius: 6px;
      padding: 11px 14px;
      background: transparent;
      color: var(--text);
      font: inherit;
      cursor: pointer;
    }}
    .grid {{ display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); }}
    .box {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 15px; }}
    code, pre {{ font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace; background: var(--code); color: var(--text); }}
    code {{ padding: 2px 5px; border-radius: 4px; }}
    pre {{ overflow-x: auto; padding: 16px; border: 1px solid var(--line); border-radius: 8px; }}
    table {{ width: 100%; border-collapse: collapse; background: var(--panel); border: 1px solid var(--line); }}
    th, td {{ padding: 10px 11px; border-bottom: 1px solid var(--line); text-align: left; }}
    .links a {{ display: inline-block; margin: 0 14px 10px 0; }}
  </style>
</head>
<body>
  <main>
    <h1>AxonGate Quote</h1>
    <p>Preview the right paid path before spending. This validates the URL, checks starter/cache availability, and returns exact x402 amounts without supplier work.</p>
    <nav class="links" aria-label="Quote links">
      <a href="{public}/quickstart">Quickstart</a>
      <a href="{public}/paid-test">Paid Test</a>
      <a href="{public}/docs">Docs</a>
      <a href="{public}/operator">Operator</a>
    </nav>
    <form method="get" action="/quote">
      <input name="target_url" value="{target}" aria-label="Target URL">
      <button type="submit">Quote</button>
    </form>
    <div class="grid">
      <div class="box"><strong>Recommended</strong><br><code>{recommended_tier}</code><br>{recommended_reason}</div>
      <div class="box"><strong>Starter</strong><br>{starter_state}</div>
      <div class="box"><strong>Cached</strong><br>{cached_state}</div>
      <div class="box"><strong>JSON API</strong><br><a href="{api_url}">Open quote</a></div>
    </div>
    <h2>Payment Probe</h2>
    <pre>{probe_url}</pre>
    <h2>Buyer Command</h2>
    <pre>{buyer_command}</pre>
    <h2>Tiers</h2>
    <table>
      <thead><tr><th>Tier</th><th>Price</th><th>USDC Units</th><th>Available Now</th><th>Policy</th></tr></thead>
      <tbody>{tiers_rows}</tbody>
    </table>
  </main>
</body>
</html>"""


def build_proof_pack_html() -> str:
    """Render the human-facing Proof Pack product page."""
    public = html.escape(PUBLIC_BASE_URL)
    pro_url = html.escape(PROOF_PRO_PAYMENT_URL or f"{PUBLIC_BASE_URL}/v1/proof-pack/quote?target_url=https%3A%2F%2Fexample.com&pack=standard")
    team_url = html.escape(PROOF_TEAM_PAYMENT_URL or f"{PUBLIC_BASE_URL}/v1/proof-pack/quote?target_url=https%3A%2F%2Fexample.com&pack=deep")
    quote_url = html.escape(f"{PUBLIC_BASE_URL}/v1/proof-pack/quote?target_url=https%3A%2F%2Fexample.com&pack=standard")
    request_json = html.escape(json.dumps(build_proof_pack_request_example(DEFAULT_PROOF_PACK), indent=2))
    response_json = html.escape(json.dumps(build_proof_pack_response_example(DEFAULT_PROOF_PACK), indent=2))
    pack_rows = "\n".join(
        "<tr>"
        f"<td><code>{html.escape(pack)}</code></td>"
        f"<td>{html.escape(str(price))} USDC</td>"
        f"<td>{html.escape(str(usdc_units(price)))}</td>"
        f"<td>{html.escape(proof_pack_cache_policy(pack))}</td>"
        "</tr>"
        for pack, price in PROOF_PACK_PRICING_USDC.items()
    )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AxonGate Proof Packs</title>
  <style>
    :root {{
      color-scheme: light dark;
      --bg: #0f1117;
      --panel: #171a22;
      --text: #f2f4f8;
      --muted: #b7c0cf;
      --line: #303542;
      --accent: #73daca;
      --code: #0a0d13;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.55;
      background: var(--bg);
      color: var(--text);
    }}
    main {{ max-width: 1040px; margin: 0 auto; padding: 44px 22px 72px; }}
    h1 {{ font-size: clamp(2.15rem, 4vw, 3.5rem); line-height: 1.05; margin: 0 0 12px; }}
    h2 {{ margin: 38px 0 12px; font-size: 1.3rem; }}
    p, li, td {{ color: var(--muted); }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .summary {{ max-width: 800px; font-size: 1.08rem; }}
    .links a, .cta a {{ display: inline-block; margin: 0 12px 10px 0; }}
    .cta a {{ border: 1px solid var(--accent); border-radius: 6px; padding: 10px 12px; }}
    .grid {{ display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); }}
    .box {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 15px; }}
    code, pre {{ font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace; background: var(--code); color: var(--text); }}
    code {{ padding: 2px 5px; border-radius: 4px; }}
    pre {{ overflow-x: auto; padding: 16px; border: 1px solid var(--line); border-radius: 8px; }}
    table {{ width: 100%; border-collapse: collapse; background: var(--panel); border: 1px solid var(--line); }}
    th, td {{ padding: 10px 11px; border-bottom: 1px solid var(--line); text-align: left; }}
  </style>
</head>
<body>
  <main>
    <h1>AxonGate Proof Packs</h1>
    <p class="summary">Paid, citation-backed evidence reports for agent builders. Send a public source URL and a question; AxonGate returns a compact answer, executive summary, key claims, citations, risks, source hash, payment metadata, and UEG receipt.</p>
    <nav class="links" aria-label="Proof Pack links">
      <a href="{public}/v1/proof-pack/quote?target_url=https%3A%2F%2Fexample.com&pack=standard">Quote API</a>
      <a href="{public}/docs">Docs</a>
      <a href="{public}/quickstart">Quickstart</a>
      <a href="{public}/operator">Operator</a>
      <a href="{public}/.well-known/x402">x402 Discovery</a>
      <a href="{public}/discovery/resources">Resources</a>
    </nav>
    <div class="cta">
      <a href="{quote_url}">Get API Quote</a>
      <a href="{pro_url}">Proof Pro</a>
      <a href="{team_url}">Proof Team</a>
    </div>

    <div class="grid">
      <div class="box"><strong>Buyer</strong><br>Agent builders who need source-backed claims.</div>
      <div class="box"><strong>Protocol</strong><br>x402 on Base USDC.</div>
      <div class="box"><strong>Fallback</strong><br>Deterministic extractive pack if LLM generation is off or fails.</div>
      <div class="box"><strong>Validation</strong><br>Unsupported LLM claims are dropped unless they cite extracted evidence IDs.</div>
    </div>

    <h2>Pricing</h2>
    <table>
      <thead><tr><th>Pack</th><th>Price</th><th>USDC Units</th><th>Policy</th></tr></thead>
      <tbody>{pack_rows}</tbody>
    </table>

    <h2>Quote</h2>
    <pre>curl "{public}/v1/proof-pack/quote?target_url=https%3A%2F%2Fexample.com&amp;question=What%20does%20this%20source%20establish%3F&amp;pack=standard&amp;source=docs"</pre>

    <h2>Paid Endpoint</h2>
    <pre>POST {public}/v1/x402/proof-pack?pack=standard
Header: PAYMENT-SIGNATURE: &lt;x402-payment-proof&gt;
Header: X-AxonGate-Pack: standard</pre>

    <h2>Request</h2>
    <pre>{request_json}</pre>

    <h2>Response Shape</h2>
    <pre>{response_json}</pre>
  </main>
</body>
</html>"""


def build_llms_txt() -> str:
    """
    Return a compact agent-readable service brief.

    The goal is to help crawler agents, planners, and LLM tool routers decide
    when AxonGate is useful without scraping a human docs page. It intentionally
    avoids secrets and includes only public contract details.
    """
    request_example = json.dumps(
        {
            "target_url": "https://example.com/source",
            "tier": RECOMMENDED_TIER,
            "force_refresh": RECOMMENDED_TIER == "fresh",
        },
        indent=2,
    )
    tier_lines = "\n".join(
        f"- {tier}: {price} USDC, {cache_policy_for_tier(tier)}"
        for tier, price in TIER_PRICING_USDC.items()
    )
    proof_pack_lines = "\n".join(
        f"- {pack}: {price} USDC, {proof_pack_cache_policy(pack)}"
        for pack, price in PROOF_PACK_PRICING_USDC.items()
    )
    proof_pack_request_example = json.dumps(
        build_proof_pack_request_example(DEFAULT_PROOF_PACK),
        indent=2,
    )

    return f"""# AxonGate

Name: AxonGate
Basename: axongate.base.eth
Summary: x402-paid Clean Context Broker that converts public web pages into clean markdown for RAG, research, and autonomous agents.
Canonical base URL: {PUBLIC_BASE_URL}
Human docs: {public_url("/docs")}
Operator dashboard: {public_url("/operator")}
Quickstart: {public_url("/quickstart")}
Paid smoke test guide: {public_url("/paid-test")}
Quote API: {public_url("/v1/x402/quote")}
Quote page: {public_url("/quote")}
Proof Pack page: {public_url("/proof-pack")}
Proof Pack quote API: {public_url("/v1/proof-pack/quote")}
Proof Pack x402 endpoint: {public_url("/v1/x402/proof-pack")}
Interactive demo: {public_url("/demo")}
OpenAPI JSON: {public_url("/openapi.json")}
Swagger UI: {public_url("/swagger")}
Manifest: {public_url("/manifest.json")}
Agent card: {public_url("/.well-known/agent.json")}
Agent card alias: {public_url("/.well-known/agent-card.json")}
x402 discovery: {public_url("/.well-known/x402")}
x402 JSON alias: {public_url("/.well-known/x402.json")}
Resource listing: {public_url("/discovery/resources")}
Sitemap: {public_url("/sitemap.xml")}
Python client example: {GITHUB_REPO_URL}/blob/main/examples/python_client.py
cURL examples: {GITHUB_REPO_URL}/blob/main/examples/curl.md
Paid buyer example: {GITHUB_REPO_URL}/blob/main/examples/paid_buyer.mjs
MCP server example: {GITHUB_REPO_URL}/blob/main/examples/axongate_mcp.mjs
MCP guide: {GITHUB_REPO_URL}/blob/main/examples/mcp.md

## Payment

Protocol: x402
Network: Base mainnet, eip155:8453
Accepted asset: USDC, {BASE_USDC_ADDRESS}
Vault address: {load_vault_address()}
Preferred payment header: PAYMENT-SIGNATURE
Legacy transaction hash header: X-AxonGate-Payment-Hash
Retry credit header: X-AxonGate-Retry-Credit
Source attribution header: X-AxonGate-Source
Facilitator: {PAYAI_FACILITATOR_URL}

## Paid Endpoint

POST {public_url("/v1/x402/access")}
Content-Type: application/json
Body example:
{request_example}

Tiers:
{tier_lines}
Recommended tier for uncached public web context: {RECOMMENDED_TIER}
Use GET /v1/x402/quote?target_url=<url> before payment to receive supplier-free tier guidance and a ready buyer command.

Successful response shape:
- status: success
- target_url: requested source URL
- tier: resolved price tier
- markdown: cleaned markdown returned from the upstream reader
- cache: cache hit metadata
- payment: network, vault, token, and amount metadata
- ueg_receipt: revenue, dynamic gas, supplier cost, and projected margin

## Retry Endpoint

POST {public_url("/v1/x402/retry")}
Use only when AxonGate returns a retryable 503 with X-AxonGate-Retry-Credit after payment was accepted but upstream delivery failed.

## Proof Packs

GET {public_url("/proof-pack")}
GET {public_url("/v1/proof-pack/quote")}?target_url=<url>&question=<question>&pack=quick|standard|deep
POST {public_url("/v1/x402/proof-pack")}?pack=standard
Header: X-AxonGate-Pack: standard
Body example:
{proof_pack_request_example}

Proof Pack prices:
{proof_pack_lines}

Successful Proof Pack response shape:
- status: success
- target_url, question, pack
- answer and executive_summary
- confidence_score
- key_claims with citation_ids
- citations with source excerpts
- risks
- source_profile with final_url and content_sha256
- cache, payment, and ueg_receipt metadata

## Safety And Supply Guards

AxonGate rejects private, loopback, multicast, and link-local target hosts; performs DNS and redirect preflight checks; enforces allowed target ports; caps supplier content size; rate-limits probes, unpaid requests, paid requests, retry credits, and target domains; and runs a dynamic Unit Economic Guardian before supplier work. Bad upstream supply should not be charged to AxonGate beyond the bounded retry credit policy.
"""


def build_docs_html() -> str:
    """Return a small self-contained human docs page for agent operators."""
    public = html.escape(PUBLIC_BASE_URL)
    vault = html.escape(load_vault_address())
    usdc_address = html.escape(BASE_USDC_ADDRESS)
    facilitator = html.escape(PAYAI_FACILITATOR_URL)
    request_json = html.escape(
        json.dumps(
            {
                "target_url": "https://example.com/source",
                "tier": RECOMMENDED_TIER,
                "force_refresh": RECOMMENDED_TIER == "fresh",
            },
            indent=2,
        )
    )
    curl_example = html.escape(
        f"""curl -X POST {PUBLIC_BASE_URL}/v1/x402/access \\
  -H "Content-Type: application/json" \\
  -H "PAYMENT-SIGNATURE: <x402-payment-proof>" \\
  -d '{{"target_url":"https://example.com/source","tier":"{RECOMMENDED_TIER}","force_refresh":{str(RECOMMENDED_TIER == "fresh").lower()}}}'"""
    )
    tiers_rows = "\n".join(
        "<tr>"
        f"<td>{html.escape(tier)}</td>"
        f"<td>{html.escape(str(price))} USDC</td>"
        f"<td>{html.escape(cache_policy_for_tier(tier))}</td>"
        "</tr>"
        for tier, price in TIER_PRICING_USDC.items()
    )
    proof_pack_rows = "\n".join(
        "<tr>"
        f"<td>{html.escape(pack)}</td>"
        f"<td>{html.escape(str(price))} USDC</td>"
        f"<td>{html.escape(proof_pack_cache_policy(pack))}</td>"
        "</tr>"
        for pack, price in PROOF_PACK_PRICING_USDC.items()
    )
    proof_request_json = html.escape(json.dumps(build_proof_pack_request_example(DEFAULT_PROOF_PACK), indent=2))
    proof_curl_example = html.escape(
        f"""curl -X POST {PUBLIC_BASE_URL}/v1/x402/proof-pack?pack=standard \\
  -H "Content-Type: application/json" \\
  -H "PAYMENT-SIGNATURE: <x402-payment-proof>" \\
  -H "X-AxonGate-Pack: standard" \\
  -d '{{"target_url":"https://example.com/source","question":"What does this source establish?","pack":"standard","force_refresh":false}}'"""
    )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AxonGate Docs</title>
  <style>
    :root {{
      color-scheme: light dark;
      --bg: #0f1117;
      --panel: #171a22;
      --text: #f2f4f8;
      --muted: #b7c0cf;
      --line: #303542;
      --accent: #73daca;
      --code: #0a0d13;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.55;
      background: var(--bg);
      color: var(--text);
    }}
    main {{
      max-width: 980px;
      margin: 0 auto;
      padding: 44px 22px 72px;
    }}
    h1 {{ font-size: clamp(2rem, 4vw, 3.3rem); line-height: 1.05; margin: 0 0 12px; }}
    h2 {{ margin: 38px 0 12px; font-size: 1.35rem; }}
    p, li {{ color: var(--muted); }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .summary {{ font-size: 1.08rem; max-width: 780px; }}
    .grid {{ display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); }}
    .box {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
    }}
    code, pre {{
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      background: var(--code);
      color: var(--text);
    }}
    code {{ padding: 2px 5px; border-radius: 4px; }}
    pre {{
      overflow-x: auto;
      padding: 16px;
      border-radius: 8px;
      border: 1px solid var(--line);
    }}
    table {{ width: 100%; border-collapse: collapse; background: var(--panel); border: 1px solid var(--line); }}
    th, td {{ padding: 11px 12px; border-bottom: 1px solid var(--line); text-align: left; }}
    th {{ color: var(--text); }}
    td {{ color: var(--muted); }}
    .links a {{ display: inline-block; margin: 0 14px 10px 0; }}
  </style>
</head>
<body>
  <main>
    <h1>AxonGate</h1>
    <p class="summary">The Clean Context Broker is an x402-paid Web-to-Markdown API for agents that need token-efficient public web context. It runs on Base mainnet, accepts USDC, and checks dynamic unit economics before supplier work.</p>

    <section class="links" aria-label="Discovery links">
      <a href="{public}/manifest.json">Manifest</a>
      <a href="{public}/.well-known/agent.json">Agent card</a>
      <a href="{public}/.well-known/agent-card.json">Agent card alias</a>
      <a href="{public}/.well-known/x402">x402 discovery</a>
      <a href="{public}/.well-known/x402.json">x402 JSON</a>
      <a href="{public}/discovery/resources">Resource listing</a>
      <a href="{public}/llms.txt">llms.txt</a>
      <a href="{public}/operator">Operator dashboard</a>
      <a href="{public}/quickstart">Quickstart</a>
      <a href="{public}/paid-test">Paid test guide</a>
      <a href="{public}/quote">Quote</a>
      <a href="{public}/proof-pack">Proof Packs</a>
      <a href="{public}/demo">Demo</a>
      <a href="{public}/openapi.json">OpenAPI JSON</a>
      <a href="{public}/swagger">Swagger UI</a>
      <a href="{html.escape(GITHUB_REPO_URL)}/blob/main/examples/python_client.py">Python client</a>
      <a href="{html.escape(GITHUB_REPO_URL)}/blob/main/examples/curl.md">cURL examples</a>
      <a href="{html.escape(GITHUB_REPO_URL)}/blob/main/examples/paid_buyer.mjs">Paid buyer</a>
      <a href="{html.escape(GITHUB_REPO_URL)}/blob/main/examples/axongate_mcp.mjs">MCP server</a>
      <a href="{html.escape(GITHUB_REPO_URL)}/blob/main/examples/mcp.md">MCP guide</a>
    </section>

    <h2>Service Contract</h2>
    <div class="grid">
      <div class="box"><strong>Paid endpoint</strong><br><code>POST /v1/x402/access</code></div>
      <div class="box"><strong>Retry endpoint</strong><br><code>POST /v1/x402/retry</code></div>
      <div class="box"><strong>Network</strong><br>Base mainnet, <code>eip155:8453</code></div>
      <div class="box"><strong>Vault</strong><br><code>{vault}</code></div>
      <div class="box"><strong>Asset</strong><br>USDC <code>{usdc_address}</code></div>
      <div class="box"><strong>Facilitator</strong><br><code>{facilitator}</code></div>
    </div>

    <h2>Pricing</h2>
    <p>The recommended tier for production agent calls is <code>{html.escape(RECOMMENDED_TIER)}</code>. Starter is available for first paid conversion on the sample target or existing cache; cached, basic, fresh, and deep cover repeat reads and live supplier-backed workloads.</p>
    <table>
      <thead><tr><th>Tier</th><th>Price</th><th>Cache policy</th></tr></thead>
      <tbody>{tiers_rows}</tbody>
    </table>

    <h2>Proof Packs</h2>
    <p>Proof Packs are paid, citation-backed evidence reports for agent builders. Use the quote API before spending, then POST to the x402 endpoint with <code>?pack=</code> or <code>X-AxonGate-Pack</code> so the payment challenge matches the requested pack.</p>
    <table>
      <thead><tr><th>Pack</th><th>Price</th><th>Policy</th></tr></thead>
      <tbody>{proof_pack_rows}</tbody>
    </table>
    <pre>curl "{public}/v1/proof-pack/quote?target_url=https%3A%2F%2Fexample.com&amp;pack=standard&amp;source=docs"</pre>
    <pre>{proof_request_json}</pre>
    <pre>{proof_curl_example}</pre>

    <h2>Standard x402 Flow</h2>
    <ol>
      <li>Use <code>/v1/x402/quote?target_url=...</code> to choose the cheapest safe tier before spending.</li>
      <li>Probe <code>/v1/x402/access</code> or read <code>/.well-known/x402</code> to discover payment requirements.</li>
      <li>Create an x402 payment proof for the selected tier and Base USDC amount.</li>
      <li>POST a JSON body with <code>target_url</code>, optional <code>tier</code>, and optional <code>force_refresh</code>.</li>
      <li>Send the proof in <code>PAYMENT-SIGNATURE</code>. AxonGate verifies payment, margin, target safety, and then fetches clean markdown.</li>
    </ol>

    <h2>Free Quote</h2>
    <p>The quote endpoint validates a public target URL, checks whether the starter sample or cache is immediately available, returns exact x402 amounts for every tier, and emits a ready buyer command. It does not trigger supplier work or spend USDC.</p>
    <pre>curl "{public}/v1/x402/quote?target_url=https%3A%2F%2Fwww.iana.org%2Fdomains%2Freserved&amp;source=docs"</pre>

    <h2>Request Body</h2>
    <pre>{request_json}</pre>

    <h2>Example</h2>
    <pre>{curl_example}</pre>

    <h2>Retry Credits</h2>
    <p>If payment succeeds but a retryable supplier or network outage prevents delivery, AxonGate can return <code>503</code> with <code>X-AxonGate-Retry-Credit</code>. The client can call <code>/v1/x402/retry</code> with the same request body and the retry credit, without paying twice.</p>

    <h2>Supply Guards</h2>
    <p>AxonGate rejects unsafe targets before payment-funded supplier work, blocks private and loopback address space, follows bounded redirects, caps content size, rate-limits abuse patterns, and fails closed when dynamic gas pricing or supplier availability would make delivery uneconomic.</p>
  </main>
</body>
</html>"""


def build_operator_dashboard_html(
    metric_values: dict[str, int],
    attribution: dict[str, dict[str, int]],
    rolling_attribution: dict[str, Any],
    triggered_alerts: list[str],
) -> str:
    """Render a compact operator view from public metrics."""
    public = html.escape(PUBLIC_BASE_URL)
    funnel = conversion_funnel_snapshot(metric_values)
    rates = funnel.get("rates", {})

    def metric(name: str) -> int:
        return int(metric_values.get(name, 0))

    def count(value: Any) -> str:
        try:
            return f"{int(value):,}"
        except (TypeError, ValueError):
            return "0"

    def percent(value: Any) -> str:
        try:
            return f"{float(value) * 100:.2f}%"
        except (TypeError, ValueError):
            return "0.00%"

    def card(label: str, value: Any, note: str = "") -> str:
        return (
            '<div class="card">'
            f"<span>{html.escape(label)}</span>"
            f"<strong>{html.escape(str(value))}</strong>"
            f"<small>{html.escape(note)}</small>"
            "</div>"
        )

    challenge_rate = rates.get("paid_attempt_per_challenge", 0)
    accepted_rate = rates.get("accepted_per_paid_attempt", 0)
    delivered_rate = rates.get("delivered_per_accepted", 0)
    supplier_rate = rates.get("supplier_success_per_request", 0)

    cards = "\n".join(
        [
            card("Requests", count(metric("requests_total")), "All public app requests"),
            card("Discovery Hits", count(metric("discovery_hits_total")), "Root, x402, docs, cards, sitemap"),
            card("Payment Challenges", count(metric("payment_challenges_total")), "402 requirements served"),
            card("Paid Attempts", count(metric("paid_attempts_total")), percent(challenge_rate)),
            card("Accepted Payments", count(metric("payments_accepted_total")), percent(accepted_rate)),
            card("Delivered", count(metric("delivery_success_total")), percent(delivered_rate)),
            card("Proof Quotes", count(metric("proof_pack_quotes_total")), "No-spend report quotes"),
            card("Proof Requests", count(metric("proof_pack_requests_total")), "Paid Proof Pack posts"),
            card("Proof Delivered", count(metric("proof_pack_delivery_success_total")), "Citation reports delivered"),
            card("Cache Hits", count(metric("cache_hits_total")), f'{count(metric("cache_misses_total"))} misses'),
            card("Supplier Calls", count(metric("jina_requests_total")), f'{percent(supplier_rate)} success'),
        ]
    )

    source_names = sorted({source for stages in attribution.values() for source in stages})
    source_rows = []
    for source in source_names:
        paid = attribution.get("paid_attempts", {}).get(source, 0)
        accepted = attribution.get("payments_accepted", {}).get(source, 0)
        delivered = attribution.get("delivery_success", {}).get(source, 0)
        challenges = attribution.get("payment_challenges", {}).get(source, 0)
        replay_rejections = attribution.get("payment_replay_rejections", {}).get(source, 0)
        source_rows.append(
            (
                delivered,
                paid,
                "<tr>"
                f"<td>{html.escape(source)}</td>"
                f"<td>{count(challenges)}</td>"
                f"<td>{count(paid)}</td>"
                f"<td>{count(accepted)}</td>"
                f"<td>{count(delivered)}</td>"
                f"<td>{count(replay_rejections)}</td>"
                "</tr>",
            )
        )
    attribution_rows = "\n".join(row for _, _, row in sorted(source_rows, reverse=True)) or (
        '<tr><td colspan="6">No source-tagged paid traffic yet.</td></tr>'
    )

    rolling_windows = rolling_attribution.get("windows", {})
    rolling_rows = []
    for label in ATTRIBUTION_ROLLING_WINDOWS:
        window = rolling_windows.get(label, {})
        stages = window.get("stages", {})
        window_rates = window.get("rates", {})
        rolling_rows.append(
            "<tr>"
            f"<td>{html.escape(label)}</td>"
            f"<td>{count(stages.get('discovery_hits', 0))}</td>"
            f"<td>{count(stages.get('payment_challenges', 0))}</td>"
            f"<td>{count(stages.get('paid_attempts', 0))}</td>"
            f"<td>{count(stages.get('payments_accepted', 0))}</td>"
            f"<td>{count(stages.get('delivery_success', 0))}</td>"
            f"<td>{percent(window_rates.get('paid_attempt_per_challenge', 0))}</td>"
            f"<td>{percent(window_rates.get('delivered_per_accepted', 0))}</td>"
            "</tr>"
        )
    rolling_funnel_rows = "\n".join(rolling_rows)

    rolling_source_rows = []
    for source, counts_by_stage in rolling_windows.get("24h", {}).get("sources", {}).items():
        source_rates = counts_by_stage.get("rates", {})
        rolling_source_rows.append(
            "<tr>"
            f"<td>{html.escape(source)}</td>"
            f"<td>{count(counts_by_stage.get('discovery_hits', 0))}</td>"
            f"<td>{count(counts_by_stage.get('payment_challenges', 0))}</td>"
            f"<td>{count(counts_by_stage.get('paid_attempts', 0))}</td>"
            f"<td>{count(counts_by_stage.get('payments_accepted', 0))}</td>"
            f"<td>{count(counts_by_stage.get('delivery_success', 0))}</td>"
            f"<td>{percent(source_rates.get('paid_attempt_per_challenge', 0))}</td>"
            "</tr>"
        )
    rolling_24h_source_rows = "\n".join(rolling_source_rows) or (
        '<tr><td colspan="7">No source events in the last 24 hours.</td></tr>'
    )

    discovery_rows = "\n".join(
        [
            f"<tr><td>Root</td><td>{count(metric('discovery_root_hits_total'))}</td></tr>",
            f"<tr><td>x402 Discovery</td><td>{count(metric('discovery_x402_hits_total'))}</td></tr>",
            f"<tr><td>Docs</td><td>{count(metric('discovery_docs_hits_total'))}</td></tr>",
            f"<tr><td>Operator</td><td>{count(metric('discovery_operator_hits_total'))}</td></tr>",
            f"<tr><td>Quickstart</td><td>{count(metric('discovery_quickstart_hits_total'))}</td></tr>",
            f"<tr><td>Paid Test Guide</td><td>{count(metric('discovery_paid_test_hits_total'))}</td></tr>",
            f"<tr><td>Quote</td><td>{count(metric('discovery_quote_hits_total'))}</td></tr>",
            f"<tr><td>Proof Packs</td><td>{count(metric('discovery_proof_pack_hits_total'))}</td></tr>",
            f"<tr><td>Demo</td><td>{count(metric('discovery_demo_hits_total'))}</td></tr>",
            f"<tr><td>Agent Cards</td><td>{count(metric('discovery_agent_card_hits_total'))}</td></tr>",
            f"<tr><td>Manifest</td><td>{count(metric('discovery_manifest_hits_total'))}</td></tr>",
            f"<tr><td>llms.txt</td><td>{count(metric('discovery_llms_hits_total'))}</td></tr>",
            f"<tr><td>Robots</td><td>{count(metric('discovery_robots_hits_total'))}</td></tr>",
            f"<tr><td>Sitemap</td><td>{count(metric('discovery_sitemap_hits_total'))}</td></tr>",
            f"<tr><td>Resource Listing</td><td>{count(metric('discovery_resources_hits_total'))}</td></tr>",
        ]
    )

    health_rows = "\n".join(
        [
            f"<tr><td>Payment validation rejections</td><td>{count(metric('payment_validation_rejections_total'))}</td></tr>",
            f"<tr><td>Replay rejections</td><td>{count(metric('payment_replay_rejections_total'))}</td></tr>",
            f"<tr><td>Retryable outages</td><td>{count(metric('retryable_outages_total'))}</td></tr>",
            f"<tr><td>Retry credits issued</td><td>{count(metric('delivery_credits_issued_total'))}</td></tr>",
            f"<tr><td>UEG checks</td><td>{count(metric('ueg_checks_total'))}</td></tr>",
            f"<tr><td>UEG rejections</td><td>{count(metric('ueg_rejections_total'))}</td></tr>",
            f"<tr><td>Rate-limit rejections</td><td>{count(metric('rate_limit_rejections_total'))}</td></tr>",
            f"<tr><td>Base RPC errors</td><td>{count(metric('base_rpc_errors_total'))}</td></tr>",
            f"<tr><td>Jina errors</td><td>{count(metric('jina_errors_total'))}</td></tr>",
            f"<tr><td>Alerts sent</td><td>{count(metric('alerts_sent_total'))}</td></tr>",
        ]
    )

    tier_rows = "\n".join(
        "<tr>"
        f"<td>{html.escape(tier)}</td>"
        f"<td>{html.escape(str(price))} USDC</td>"
        f"<td>{html.escape(str(usdc_units(price)))}</td>"
        f"<td>{html.escape(cache_policy_for_tier(tier))}</td>"
        "</tr>"
        for tier, price in TIER_PRICING_USDC.items()
    )
    proof_pack_rows = "\n".join(
        "<tr>"
        f"<td>{html.escape(pack)}</td>"
        f"<td>{html.escape(str(price))} USDC</td>"
        f"<td>{html.escape(str(usdc_units(price)))}</td>"
        f"<td>{html.escape(proof_pack_cache_policy(pack))}</td>"
        "</tr>"
        for pack, price in PROOF_PACK_PRICING_USDC.items()
    )
    alert_text = ", ".join(triggered_alerts) if triggered_alerts else "No active alerts"

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="60">
  <title>AxonGate Operator</title>
  <style>
    :root {{
      color-scheme: light dark;
      --bg: #12110f;
      --panel: #1d1b18;
      --panel-2: #23211d;
      --text: #f5f2ea;
      --muted: #c4beb2;
      --line: #39352e;
      --accent: #6fd3bd;
      --warn: #f4be62;
      --danger: #f07f7f;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.45;
    }}
    main {{ max-width: 1220px; margin: 0 auto; padding: 28px 18px 56px; }}
    header {{ display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; margin-bottom: 20px; }}
    h1 {{ margin: 0; font-size: 1.85rem; line-height: 1.1; }}
    h2 {{ margin: 26px 0 10px; font-size: 1rem; }}
    p {{ margin: 6px 0 0; color: var(--muted); }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .links {{ display: flex; flex-wrap: wrap; gap: 10px; justify-content: flex-end; }}
    .links a {{ border: 1px solid var(--line); border-radius: 6px; padding: 7px 9px; background: var(--panel); }}
    .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 10px; }}
    .card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 13px;
      min-height: 96px;
    }}
    .card span, .card small {{ display: block; color: var(--muted); }}
    .card strong {{ display: block; margin: 5px 0; font-size: 1.55rem; line-height: 1.1; }}
    .split {{ display: grid; grid-template-columns: minmax(0, 1.25fr) minmax(320px, 0.75fr); gap: 14px; }}
    table {{ width: 100%; border-collapse: collapse; background: var(--panel); border: 1px solid var(--line); }}
    th, td {{ padding: 10px 11px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }}
    th {{ color: var(--text); background: var(--panel-2); font-size: 0.9rem; }}
    td {{ color: var(--muted); }}
    code {{ background: #0d0c0b; border: 1px solid var(--line); border-radius: 4px; padding: 1px 5px; color: var(--text); }}
    .notice {{ border: 1px solid var(--line); border-radius: 8px; padding: 12px; background: var(--panel); color: var(--muted); }}
    .notice strong {{ color: var(--text); }}
    .ok {{ color: var(--accent); }}
    .warn {{ color: var(--warn); }}
    .danger {{ color: var(--danger); }}
    @media (max-width: 860px) {{
      header {{ display: block; }}
      .links {{ justify-content: flex-start; margin-top: 14px; }}
      .split {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>AxonGate Operator</h1>
        <p>Live public metrics from <code>/metrics</code>. Page refreshes every 60 seconds.</p>
      </div>
      <nav class="links" aria-label="Operator links">
        <a href="{public}/metrics">Metrics JSON</a>
        <a href="{public}/paid-test">Paid Test</a>
        <a href="{public}/docs">Docs</a>
        <a href="{public}/demo">Demo</a>
      </nav>
    </header>

    <section class="cards">{cards}</section>

    <h2>Rolling Funnel</h2>
    <table>
      <thead><tr><th>Window</th><th>Discovery</th><th>Challenges</th><th>Paid</th><th>Accepted</th><th>Delivered</th><th>Paid / Challenge</th><th>Delivered / Accepted</th></tr></thead>
      <tbody>{rolling_funnel_rows}</tbody>
    </table>

    <h2>24h Source Funnel</h2>
    <table>
      <thead><tr><th>Source</th><th>Discovery</th><th>Challenges</th><th>Paid</th><th>Accepted</th><th>Delivered</th><th>Paid / Challenge</th></tr></thead>
      <tbody>{rolling_24h_source_rows}</tbody>
    </table>

    <h2>Cumulative Funnel By Source</h2>
    <table>
      <thead><tr><th>Source</th><th>Challenges</th><th>Paid</th><th>Accepted</th><th>Delivered</th><th>Replay Rejected</th></tr></thead>
      <tbody>{attribution_rows}</tbody>
    </table>

    <div class="split">
      <section>
        <h2>Discovery Surfaces</h2>
        <table><thead><tr><th>Surface</th><th>Hits</th></tr></thead><tbody>{discovery_rows}</tbody></table>
      </section>
      <section>
        <h2>Reliability And Guardrails</h2>
        <table><thead><tr><th>Signal</th><th>Count</th></tr></thead><tbody>{health_rows}</tbody></table>
      </section>
    </div>

    <h2>Pricing And Unit Control</h2>
    <table>
      <thead><tr><th>Tier</th><th>Price</th><th>USDC Units</th><th>Cache Policy</th></tr></thead>
      <tbody>{tier_rows}</tbody>
    </table>

    <h2>Proof Pack Pricing</h2>
    <table>
      <thead><tr><th>Pack</th><th>Price</th><th>USDC Units</th><th>Policy</th></tr></thead>
      <tbody>{proof_pack_rows}</tbody>
    </table>

    <p class="notice"><strong>Alert state:</strong> <span class="{'warn' if triggered_alerts else 'ok'}">{html.escape(alert_text)}</span></p>
  </main>
</body>
</html>"""


def build_quickstart_html() -> str:
    """Render the shortest path from discovery to a first paid AxonGate result."""
    public = html.escape(PUBLIC_BASE_URL)
    github = html.escape(GITHUB_REPO_URL)
    starter_price = html.escape(str(TIER_PRICING_USDC[STARTER_TIER]))
    cached_price = html.escape(str(TIER_PRICING_USDC[CACHE_ONLY_TIER]))
    fresh_price = html.escape(str(TIER_PRICING_USDC["fresh"]))
    mcp_config = html.escape(
        json.dumps(
            {
                "mcpServers": {
                    "axongate": {
                        "command": "node",
                        "args": ["C:/path/to/AxonGate-Vault/examples/axongate_mcp.mjs"],
                        "env": {
                            "AXONGATE_BASE_URL": PUBLIC_BASE_URL,
                            "AXONGATE_WALLET_FILE": "C:/path/to/burner_wallet.json",
                            "AXONGATE_CONFIRM_SPEND": str(TIER_PRICING_USDC[STARTER_TIER]),
                        },
                    }
                }
            },
            indent=2,
        )
    )
    mcp_probe = html.escape(
        """Tool: probe_payment_terms
Input:
{
  "tier": "starter",
  "source": "quickstart-mcp"
}"""
    )
    mcp_paid = html.escape(
        f"""Tool: fetch_clean_context
Input:
{{
  "target_url": "https://www.iana.org/domains/reserved",
  "tier": "starter",
  "force_refresh": false,
  "confirm_spend_usdc": "{TIER_PRICING_USDC[STARTER_TIER]}",
  "source": "quickstart-mcp",
  "max_markdown_chars": 12000
}}"""
    )
    terminal_commands = html.escape(
        f"""npm install
npm run paid:buyer -- \\
  --wallet-file "C:/path/to/burner_wallet.json" \\
  --target-url "https://www.iana.org/domains/reserved" \\
  --tier starter \\
  --confirm-spend {TIER_PRICING_USDC[STARTER_TIER]} \\
  --source quickstart"""
    )
    expected_output = html.escape(
        """PAID
{
  "http_status": 200,
  "status": "success",
  "target_url": "https://www.iana.org/domains/reserved",
  "tier": "starter",
  "markdown_chars": 1000,
  "payment": {
    "mode": "x402-facilitator",
    "amount_usdc": 0.012,
    "source": "quickstart"
  }
}"""
    )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AxonGate Quickstart</title>
  <style>
    :root {{
      color-scheme: light dark;
      --bg: #0f1117;
      --panel: #171a22;
      --text: #f2f4f8;
      --muted: #b7c0cf;
      --line: #303542;
      --accent: #73daca;
      --warn: #f4be62;
      --code: #0a0d13;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.55;
    }}
    main {{ max-width: 1040px; margin: 0 auto; padding: 36px 18px 68px; }}
    h1 {{ margin: 0 0 10px; font-size: 2.2rem; line-height: 1.1; }}
    h2 {{ margin: 32px 0 10px; font-size: 1.18rem; }}
    h3 {{ margin: 18px 0 8px; font-size: 1rem; }}
    p, li {{ color: var(--muted); }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .links a {{ display: inline-block; margin: 0 12px 10px 0; }}
    .steps {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; margin: 18px 0; }}
    .step, .callout {{
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      background: var(--panel);
    }}
    .step strong {{ display: block; color: var(--text); margin-bottom: 4px; }}
    .callout {{ border-left: 4px solid var(--warn); }}
    pre {{
      overflow-x: auto;
      white-space: pre;
      background: var(--code);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 15px;
      color: var(--text);
    }}
    code {{ background: var(--code); border: 1px solid var(--line); border-radius: 4px; padding: 1px 5px; color: var(--text); }}
    table {{ width: 100%; border-collapse: collapse; background: var(--panel); border: 1px solid var(--line); }}
    th, td {{ padding: 10px 11px; border-bottom: 1px solid var(--line); text-align: left; }}
    th {{ color: var(--text); }}
    td {{ color: var(--muted); }}
  </style>
</head>
<body>
  <main>
    <h1>AxonGate Quickstart</h1>
    <p>Turn a public URL into clean RAG-ready markdown with one paid x402 call. Use the terminal buyer for a direct smoke test, or attach the MCP server so an agent can call AxonGate as a tool.</p>
    <nav class="links" aria-label="Quickstart links">
      <a href="{public}/demo">Demo</a>
      <a href="{public}/paid-test">Paid Test</a>
      <a href="{public}/quote">Quote</a>
      <a href="{public}/proof-pack">Proof Packs</a>
      <a href="{public}/docs">Docs</a>
      <a href="{public}/operator">Operator</a>
      <a href="{github}/blob/main/examples/paid_buyer.mjs">Buyer Script</a>
      <a href="{github}/blob/main/examples/axongate_mcp.mjs">MCP Server</a>
    </nav>

    <div class="steps">
      <div class="step"><strong>1. Fund burner wallet</strong>Use Base USDC. Starter costs <code>{starter_price} USDC</code>; fresh costs <code>{fresh_price} USDC</code>.</div>
      <div class="step"><strong>2. Run the buyer</strong>The script probes payment terms, signs x402, pays, and returns markdown.</div>
      <div class="step"><strong>3. Watch attribution</strong>Use <code>source=quickstart</code> so `/metrics` shows the conversion path.</div>
      <div class="step"><strong>4. Sell proof</strong>Use <code>/proof-pack</code> when a buyer needs cited claims instead of raw markdown.</div>
    </div>

    <p class="callout"><strong>This can spend real USDC.</strong> Every paid path requires an explicit <code>confirm-spend</code> or <code>confirm_spend_usdc</code> value that must match the selected tier.</p>

    <h2>Fast Starter Path</h2>
    <pre>{terminal_commands}</pre>

    <h2>Expected Shape</h2>
    <pre>{expected_output}</pre>

    <h2>MCP Agent Path</h2>
    <p>Add the server to an MCP-capable client, then call the probe tool first. The paid tool refuses to run unless the confirmed spend matches the selected tier price.</p>
    <h3>Client Config</h3>
    <pre>{mcp_config}</pre>
    <h3>Probe Tool</h3>
    <pre>{mcp_probe}</pre>
    <h3>Paid Tool</h3>
    <pre>{mcp_paid}</pre>

    <h2>Pricing</h2>
    <table>
      <thead><tr><th>Tier</th><th>Price</th><th>Best For</th></tr></thead>
      <tbody>
        <tr><td><code>starter</code></td><td>{starter_price} USDC</td><td>First paid conversion using the starter sample or existing cache</td></tr>
        <tr><td><code>cached</code></td><td>{cached_price} USDC</td><td>Repeat reads when AxonGate already has a cached copy</td></tr>
        <tr><td><code>basic</code></td><td>{html.escape(str(TIER_PRICING_USDC["basic"]))} USDC</td><td>Cache-friendly production calls</td></tr>
        <tr><td><code>fresh</code></td><td>{fresh_price} USDC</td><td>Current public web context and first real tests</td></tr>
        <tr><td><code>deep</code></td><td>{html.escape(str(TIER_PRICING_USDC["deep"]))} USDC</td><td>Higher-value calls with a short cache window</td></tr>
      </tbody>
    </table>
  </main>
</body>
</html>"""


def build_paid_test_html() -> str:
    """Render a focused guide for running a real paid smoke test."""
    public = html.escape(PUBLIC_BASE_URL)
    github = html.escape(GITHUB_REPO_URL)
    wallet_command = html.escape(
        f"""npm install
npm run paid:buyer -- \\
  --wallet-file "C:/path/to/buyer_wallet.json" \\
  --target-url "https://www.iana.org/domains/reserved" \\
  --tier starter \\
  --confirm-spend {TIER_PRICING_USDC[STARTER_TIER]} \\
  --source manual-smoke \\
  --replay"""
    )
    env_command = html.escape(
        f"""set AXONGATE_WALLET_FILE=C:\\path\\to\\buyer_wallet.json
set AXONGATE_TARGET_URL=https://www.iana.org/domains/reserved
set AXONGATE_TIER=starter
set AXONGATE_CONFIRM_SPEND={TIER_PRICING_USDC[STARTER_TIER]}
set AXONGATE_SOURCE=manual-smoke
npm run paid:buyer -- --replay"""
    )
    expected_result = html.escape(
        """PAID
{
  "http_status": 200,
  "status": "success",
  "tier": "starter",
  "cache": {"hit": true},
  "payment": {
    "amount_usdc": 0.012,
    "source": "manual-smoke"
  },
  "ueg_receipt": {
    "supplier_attempts": 0,
    "projected_profit_usdc": 0.0138625
  }
}
REPLAY
{
  "http_status": 402,
  "detail": "Payment proof has already been processed..."
}"""
    )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AxonGate Paid Test</title>
  <style>
    :root {{
      color-scheme: light dark;
      --bg: #12110f;
      --panel: #1d1b18;
      --text: #f5f2ea;
      --muted: #c4beb2;
      --line: #39352e;
      --accent: #6fd3bd;
      --warn: #f4be62;
      --code: #0d0c0b;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.55;
    }}
    main {{ max-width: 980px; margin: 0 auto; padding: 36px 18px 64px; }}
    h1 {{ margin: 0 0 10px; font-size: 2rem; line-height: 1.1; }}
    h2 {{ margin: 30px 0 10px; font-size: 1.15rem; }}
    p, li {{ color: var(--muted); }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .links a {{ display: inline-block; margin: 0 12px 10px 0; }}
    .callout {{
      border: 1px solid var(--line);
      border-left: 4px solid var(--warn);
      border-radius: 8px;
      padding: 12px 14px;
      background: var(--panel);
    }}
    pre {{
      overflow-x: auto;
      white-space: pre;
      background: var(--code);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 15px;
      color: var(--text);
    }}
    code {{ background: var(--code); border: 1px solid var(--line); border-radius: 4px; padding: 1px 5px; color: var(--text); }}
    table {{ width: 100%; border-collapse: collapse; background: var(--panel); border: 1px solid var(--line); }}
    th, td {{ padding: 10px 11px; border-bottom: 1px solid var(--line); text-align: left; }}
    th {{ color: var(--text); }}
    td {{ color: var(--muted); }}
  </style>
</head>
<body>
  <main>
    <h1>AxonGate Paid Test</h1>
    <p>Run a real Base USDC x402 smoke test against production, with a bounded spend confirmation and replay check.</p>
    <nav class="links" aria-label="Paid test links">
      <a href="{public}/operator">Operator Dashboard</a>
      <a href="{public}/metrics">Metrics JSON</a>
      <a href="{public}/quickstart">Quickstart</a>
      <a href="{public}/docs">Docs</a>
      <a href="{github}/blob/main/examples/paid_buyer.mjs">Buyer Script</a>
    </nav>

    <p class="callout"><strong>This spends real USDC.</strong> The starter tier currently authorizes <code>{html.escape(str(TIER_PRICING_USDC[STARTER_TIER]))} USDC</code>. Use a burner wallet and keep the explicit <code>--confirm-spend</code> value in the command.</p>

    <h2>Command</h2>
    <pre>{wallet_command}</pre>

    <h2>Environment Variant</h2>
    <pre>{env_command}</pre>

    <h2>Expected Result</h2>
    <pre>{expected_result}</pre>

    <h2>What To Check</h2>
    <table>
      <thead><tr><th>Signal</th><th>Expected</th></tr></thead>
      <tbody>
        <tr><td>Challenge amount</td><td><code>{html.escape(str(usdc_units(TIER_PRICING_USDC[STARTER_TIER])))}</code> for starter tier</td></tr>
        <tr><td>Paid response</td><td><code>200</code>, markdown present, payment response transaction present</td></tr>
        <tr><td>Starter behavior</td><td><code>cache.hit=true</code> and <code>supplier_attempts=0</code> for this sample target</td></tr>
        <tr><td>Replay behavior</td><td>Second submission returns <code>402</code></td></tr>
        <tr><td>Attribution</td><td><code>/metrics</code> shows the selected source under paid, accepted, delivered, and replay rejection stages</td></tr>
      </tbody>
    </table>
  </main>
</body>
</html>"""


def build_demo_html() -> str:
    """Return a self-contained buyer console that never bypasses x402 payment."""
    public = html.escape(PUBLIC_BASE_URL)
    price_options = "\n".join(
        f'<option value="{html.escape(tier)}"{" selected" if tier == RECOMMENDED_TIER else ""}>'
        f'{html.escape(tier)} - {html.escape(str(price))} USDC</option>'
        for tier, price in TIER_PRICING_USDC.items()
    )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AxonGate Demo</title>
  <style>
    :root {{
      color-scheme: light dark;
      --bg: #0f1117;
      --panel: #171a22;
      --text: #f2f4f8;
      --muted: #b7c0cf;
      --line: #303542;
      --accent: #73daca;
      --warn: #f6c177;
      --code: #0a0d13;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
    }}
    main {{ max-width: 1080px; margin: 0 auto; padding: 34px 18px 60px; }}
    h1 {{ font-size: clamp(2rem, 4vw, 3.1rem); line-height: 1.05; margin: 0 0 10px; }}
    h2 {{ font-size: 1.05rem; margin: 0 0 14px; }}
    p {{ color: var(--muted); margin: 0 0 18px; }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .grid {{ display: grid; grid-template-columns: minmax(0, 420px) minmax(0, 1fr); gap: 16px; align-items: start; }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
    }}
    label {{ display: block; margin: 12px 0 6px; color: var(--muted); font-size: 0.92rem; }}
    input, select, textarea, button {{
      width: 100%;
      font: inherit;
      border-radius: 6px;
      border: 1px solid var(--line);
      background: #10131a;
      color: var(--text);
    }}
    input, select, textarea {{ padding: 10px 11px; }}
    textarea {{ min-height: 92px; resize: vertical; }}
    button {{
      margin-top: 12px;
      padding: 10px 12px;
      cursor: pointer;
      background: var(--accent);
      color: #07100f;
      border-color: transparent;
      font-weight: 700;
    }}
    button.secondary {{ background: transparent; color: var(--text); border-color: var(--line); }}
    pre {{
      min-height: 420px;
      overflow: auto;
      white-space: pre-wrap;
      word-break: break-word;
      background: var(--code);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      margin: 0;
      color: var(--text);
    }}
    .meta {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px; margin: 18px 0; }}
    .pill {{ border: 1px solid var(--line); border-radius: 8px; padding: 10px; color: var(--muted); }}
    .pill strong {{ display: block; color: var(--text); }}
    .warning {{ color: var(--warn); }}
    @media (max-width: 860px) {{ .grid {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
  <main>
    <h1>AxonGate Demo</h1>
    <p>Probe payment terms, prepare a paid request, and inspect the response contract. Supplier work only runs after a valid x402 proof is submitted.</p>
    <div class="meta">
      <div class="pill"><strong>Endpoint</strong>{public}/v1/x402/access</div>
      <div class="pill"><strong>Network</strong>Base mainnet, eip155:8453</div>
      <div class="pill"><strong>Asset</strong>USDC on Base</div>
      <div class="pill"><strong>Docs</strong><a href="{public}/docs">Open docs</a></div>
      <div class="pill"><strong>Quickstart</strong><a href="{public}/quickstart">First paid call</a></div>
      <div class="pill"><strong>Quote</strong><a href="{public}/quote">Preview spend</a></div>
      <div class="pill"><strong>Paid test</strong><a href="{public}/paid-test">Run smoke</a></div>
      <div class="pill"><strong>Operator</strong><a href="{public}/operator">Open dashboard</a></div>
    </div>
    <div class="grid">
      <section class="panel">
        <h2>Request</h2>
        <label for="targetUrl">Target URL</label>
        <input id="targetUrl" value="https://example.com" autocomplete="url">
        <label for="tier">Tier</label>
        <select id="tier">{price_options}</select>
        <label for="paymentSignature">PAYMENT-SIGNATURE</label>
        <textarea id="paymentSignature" spellcheck="false" placeholder="Paste x402 payment proof here"></textarea>
        <button id="probeButton" class="secondary" type="button">Fetch Payment Terms</button>
        <button id="submitButton" type="button">Submit Paid Request</button>
        <p class="warning">A valid payment proof is required for supplier delivery. Empty probes return the x402 challenge only.</p>
      </section>
      <section class="panel">
        <h2>Response</h2>
        <pre id="output">Ready.</pre>
      </section>
    </div>
  </main>
  <script>
    const output = document.getElementById("output");
    const targetUrl = document.getElementById("targetUrl");
    const tier = document.getElementById("tier");
    const paymentSignature = document.getElementById("paymentSignature");

    function show(value) {{
      output.textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
    }}

    function decodeHeader(value) {{
      if (!value) return null;
      try {{
        return JSON.parse(atob(value));
      }} catch (error) {{
        return {{"raw": value, "decode_error": String(error)}};
      }}
    }}

    async function readResponse(response) {{
      const text = await response.text();
      let body = text;
      try {{
        body = JSON.parse(text);
      }} catch (_) {{}}
      return {{
        status: response.status,
        payment_required: decodeHeader(response.headers.get("PAYMENT-REQUIRED") || response.headers.get("X-Payment-Required")),
        retry_credit: response.headers.get("X-AxonGate-Retry-Credit"),
        retry_after: response.headers.get("Retry-After"),
        body
      }};
    }}

    document.getElementById("probeButton").addEventListener("click", async () => {{
      show("Fetching payment terms...");
      try {{
        const response = await fetch("/v1/x402/access?tier=" + encodeURIComponent(tier.value), {{
          headers: {{"X-AxonGate-Tier": tier.value}}
        }});
        show(await readResponse(response));
      }} catch (error) {{
        show({{"error": String(error)}});
      }}
    }});

    document.getElementById("submitButton").addEventListener("click", async () => {{
      const proof = paymentSignature.value.trim();
      if (!proof) {{
        show("Paste a PAYMENT-SIGNATURE before submitting a paid request.");
        return;
      }}
      show("Submitting paid request...");
      try {{
        const response = await fetch("/v1/x402/access", {{
          method: "POST",
          headers: {{
            "Content-Type": "application/json",
            "PAYMENT-SIGNATURE": proof,
            "X-AxonGate-Tier": tier.value
          }},
          body: JSON.stringify({{
            target_url: targetUrl.value.trim(),
            tier: tier.value,
            force_refresh: tier.value === "fresh"
          }})
        }});
        show(await readResponse(response));
      }} catch (error) {{
        show({{"error": String(error)}});
      }}
    }});
  </script>
</body>
</html>"""


def build_robots_txt() -> str:
    """Return permissive crawler hints for public discovery surfaces."""
    return f"""User-agent: *
Allow: /

Sitemap: {public_url("/sitemap.xml")}
"""


def build_sitemap_xml() -> str:
    """Return a small XML sitemap for human and agent discovery URLs."""
    now = time.strftime("%Y-%m-%d", time.gmtime())
    entries = [
        ("/", "1.0"),
        ("/docs", "0.9"),
        ("/operator", "0.9"),
        ("/quickstart", "0.95"),
        ("/paid-test", "0.9"),
        ("/quote", "0.9"),
        ("/v1/x402/quote", "0.8"),
        ("/proof-pack", "0.95"),
        ("/v1/proof-pack/quote", "0.85"),
        ("/v1/x402/proof-pack", "0.85"),
        ("/demo", "0.9"),
        ("/llms.txt", "0.8"),
        ("/manifest.json", "0.8"),
        ("/.well-known/agent.json", "0.8"),
        ("/.well-known/agent-card.json", "0.8"),
        ("/.well-known/x402", "0.8"),
        ("/.well-known/x402.json", "0.8"),
        ("/discovery/resources", "0.8"),
        ("/openapi.json", "0.7"),
        ("/swagger", "0.7"),
    ]
    url_entries = "\n".join(
        "  <url>"
        f"<loc>{html.escape(public_url(path), quote=True)}</loc>"
        f"<lastmod>{now}</lastmod>"
        "<changefreq>daily</changefreq>"
        f"<priority>{priority}</priority>"
        "</url>"
        for path, priority in entries
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{url_entries}
</urlset>
"""


@app.get("/llms.txt", response_class=PlainTextResponse, tags=["discovery"], summary="Agent-readable service brief")
async def llms_txt(request: Request):
    """Expose a concise machine-readable brief for LLM routers and crawlers."""
    inc_discovery_hit("discovery_llms_hits_total", attribution_source_from_request(request))
    return build_llms_txt()


@app.get("/docs", response_class=HTMLResponse, tags=["discovery"], summary="Human-readable AxonGate docs")
async def human_docs(request: Request):
    """Serve a lightweight docs page; Swagger remains available at /swagger."""
    inc_discovery_hit("discovery_docs_hits_total", attribution_source_from_request(request))
    return build_docs_html()


@app.get("/operator", response_class=HTMLResponse, tags=["operations"], summary="Operator conversion dashboard")
async def operator_dashboard(request: Request):
    """Serve a public operator view backed by the metrics endpoint data."""
    inc_discovery_hit("discovery_operator_hits_total", attribution_source_from_request(request))
    metric_values = await durable_metrics_snapshot()
    attribution = await durable_attribution_snapshot()
    rolling_attribution = await durable_rolling_attribution_snapshot()
    triggered_alerts = await evaluate_alerts(metric_values)
    return build_operator_dashboard_html(metric_values, attribution, rolling_attribution, triggered_alerts)


@app.get("/quickstart", response_class=HTMLResponse, tags=["discovery"], summary="First paid AxonGate conversion quickstart")
async def quickstart(request: Request):
    """Serve the shortest path from discovery to a first paid result."""
    inc_discovery_hit("discovery_quickstart_hits_total", attribution_source_from_request(request))
    return build_quickstart_html()


@app.get("/paid-test", response_class=HTMLResponse, tags=["discovery"], summary="Real paid x402 smoke test guide")
async def paid_test_guide(request: Request):
    """Serve a concise paid-test guide for burner-wallet smoke checks."""
    inc_discovery_hit("discovery_paid_test_hits_total", attribution_source_from_request(request))
    return build_paid_test_html()


@app.get("/proof-pack", response_class=HTMLResponse, tags=["discovery"], summary="AxonGate Proof Pack product page")
async def proof_pack_page(request: Request):
    """Serve the Proof Pack product page for human buyers and agent builders."""
    inc_discovery_hit("discovery_proof_pack_hits_total", attribution_source_from_request(request))
    return build_proof_pack_html()


@app.get("/v1/x402/quote", tags=["discovery"], summary="Supplier-free x402 tier quote")
async def quote_api(request: Request, target_url: str = "https://www.iana.org/domains/reserved"):
    """Return no-spend tier guidance and buyer commands for a public target."""
    source = attribution_source_from_request(request)
    inc_discovery_hit("discovery_quote_hits_total", source)
    try:
        await enforce_rate_limit("quote_ip", client_rate_identifier(request), RATE_LIMIT_UNPAID_PER_IP)
        return await build_conversion_quote(target_url, source)
    except RateLimitExceeded as exc:
        raise rate_limit_429(exc) from exc
    except PaymentValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.detail) from exc


@app.get("/v1/proof-pack/quote", tags=["discovery"], summary="Supplier-free Proof Pack quote")
async def proof_pack_quote_api(
    request: Request,
    target_url: str = "https://example.com",
    question: Optional[str] = None,
    pack: str = DEFAULT_PROOF_PACK,
):
    """Return no-spend Proof Pack pricing and buyer commands for a public target."""
    source = attribution_source_from_request(request)
    inc_metric("proof_pack_quotes_total")
    inc_attribution("proof_pack_quotes", source)
    inc_discovery_hit("discovery_proof_pack_hits_total", source)
    try:
        await enforce_rate_limit("proof_pack_quote_ip", client_rate_identifier(request), RATE_LIMIT_UNPAID_PER_IP)
        return await build_proof_pack_quote(target_url, question, pack, source)
    except RateLimitExceeded as exc:
        raise rate_limit_429(exc) from exc
    except PaymentValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.detail) from exc


@app.get("/quote", response_class=HTMLResponse, tags=["discovery"], summary="Human-readable AxonGate quote")
async def quote_page(request: Request, target_url: str = "https://www.iana.org/domains/reserved"):
    """Serve a no-spend quote page that points buyers to the right paid tier."""
    source = attribution_source_from_request(request)
    inc_discovery_hit("discovery_quote_hits_total", source)
    try:
        await enforce_rate_limit("quote_ip", client_rate_identifier(request), RATE_LIMIT_UNPAID_PER_IP)
        quote = await build_conversion_quote(target_url, source)
    except RateLimitExceeded as exc:
        raise rate_limit_429(exc) from exc
    except PaymentValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.detail) from exc
    return build_quote_html(quote)


@app.get("/demo", response_class=HTMLResponse, tags=["discovery"], summary="Interactive AxonGate buyer demo")
async def demo(request: Request):
    """Serve a safe buyer console that preserves the x402 payment boundary."""
    inc_discovery_hit("discovery_demo_hits_total", attribution_source_from_request(request))
    return build_demo_html()


@app.get("/robots.txt", response_class=PlainTextResponse, tags=["discovery"], summary="Crawler hints")
async def robots_txt(request: Request):
    """Expose crawler hints for public discovery URLs."""
    inc_discovery_hit("discovery_robots_hits_total", attribution_source_from_request(request))
    return build_robots_txt()


@app.get("/sitemap.xml", tags=["discovery"], summary="XML sitemap")
async def sitemap_xml(request: Request):
    """Expose a small sitemap for search and agent crawlers."""
    inc_discovery_hit("discovery_sitemap_hits_total", attribution_source_from_request(request))
    return Response(content=build_sitemap_xml(), media_type="application/xml")


@app.get("/", tags=["discovery"], summary="Discovery index")
async def root(request: Request):
    """Return a lightweight discovery index for crawlers and agent clients."""
    inc_discovery_hit("discovery_root_hits_total", attribution_source_from_request(request))
    return {
        "status": "alive",
        "agent": "AxonGate",
        "version": app.version,
        "service": "The Clean Context Broker",
        "basename": "axongate.base.eth",
        "manifest": f"{PUBLIC_BASE_URL}/manifest.json",
        "agent_card": f"{PUBLIC_BASE_URL}/.well-known/agent.json",
        "agent_card_alias": f"{PUBLIC_BASE_URL}/.well-known/agent-card.json",
        "x402": f"{PUBLIC_BASE_URL}/.well-known/x402",
        "x402_json": f"{PUBLIC_BASE_URL}/.well-known/x402.json",
        "discovery": f"{PUBLIC_BASE_URL}/discovery/resources",
        "docs": f"{PUBLIC_BASE_URL}/docs",
        "operator_dashboard": f"{PUBLIC_BASE_URL}/operator",
        "quickstart": f"{PUBLIC_BASE_URL}/quickstart",
        "paid_test_guide": f"{PUBLIC_BASE_URL}/paid-test",
        "quote": f"{PUBLIC_BASE_URL}/quote",
        "quote_api": f"{PUBLIC_BASE_URL}/v1/x402/quote",
        "proof_pack": f"{PUBLIC_BASE_URL}/proof-pack",
        "proof_pack_quote_api": f"{PUBLIC_BASE_URL}/v1/proof-pack/quote",
        "proof_pack_x402_endpoint": f"{PUBLIC_BASE_URL}/v1/x402/proof-pack",
        "demo": f"{PUBLIC_BASE_URL}/demo",
        "llms_txt": f"{PUBLIC_BASE_URL}/llms.txt",
        "robots": f"{PUBLIC_BASE_URL}/robots.txt",
        "sitemap": f"{PUBLIC_BASE_URL}/sitemap.xml",
        "openapi": f"{PUBLIC_BASE_URL}/openapi.json",
        "swagger": f"{PUBLIC_BASE_URL}/swagger",
        "python_client_example": f"{GITHUB_REPO_URL}/blob/main/examples/python_client.py",
        "curl_examples": f"{GITHUB_REPO_URL}/blob/main/examples/curl.md",
        "mcp_server_example": f"{GITHUB_REPO_URL}/blob/main/examples/axongate_mcp.mjs",
        "standard_x402_endpoint": f"{PUBLIC_BASE_URL}/v1/x402/access",
        "legacy_tx_hash_endpoint": f"{PUBLIC_BASE_URL}/v1/access",
        "retry_endpoint": f"{PUBLIC_BASE_URL}/v1/x402/retry",
    }


@app.get("/health", tags=["operations"], summary="Railway health check")
async def health():
    return {"status": "alive", "vault_address": load_vault_address()}


@app.get("/manifest.json", tags=["discovery"], summary="Canonical agent manifest")
async def manifest(request: Request):
    """Return the full AxonGate agent card used by other agents for discovery."""
    inc_discovery_hit("discovery_manifest_hits_total", attribution_source_from_request(request))
    return load_agent_card()


@app.get("/.well-known/agent.json", tags=["discovery"], summary="Well-known agent card")
async def well_known_agent(request: Request):
    """Expose the agent card at a common agent-discovery well-known path."""
    inc_discovery_hit("discovery_agent_card_hits_total", attribution_source_from_request(request))
    return load_agent_card()


@app.get("/.well-known/agent-card.json", tags=["discovery"], summary="Agent card compatibility alias")
async def well_known_agent_card_alias(request: Request):
    """Expose an AgentCard-compatible alias used by some registries."""
    inc_discovery_hit("discovery_agent_card_hits_total", attribution_source_from_request(request))
    return load_agent_card()


@app.get("/.well-known/x402", tags=["discovery"], summary="x402 payment discovery")
async def well_known_x402(request: Request):
    """Expose AxonGate's x402 resource advertisement for crawler discovery."""
    inc_discovery_hit("discovery_x402_hits_total", attribution_source_from_request(request))
    return build_x402_public_discovery()


@app.get("/.well-known/x402.json", tags=["discovery"], summary="x402 discovery compatibility alias")
async def well_known_x402_json(request: Request):
    """Expose an x402 JSON alias for crawlers that require a file extension."""
    inc_discovery_hit("discovery_x402_hits_total", attribution_source_from_request(request))
    return build_x402_public_discovery()


@app.get("/discovery/resources", tags=["discovery"], summary="PayAI-style resource listing")
async def discovery_resources(request: Request, type: Optional[str] = None, limit: int = 20, offset: int = 0):
    """Return a PayAI-style Bazaar resource listing for AxonGate."""
    inc_discovery_hit("discovery_resources_hits_total", attribution_source_from_request(request))
    if type not in (None, "http"):
        items: list[dict[str, Any]] = []
    else:
        items = [build_x402_resource(), build_proof_pack_resource()]

    bounded_limit = max(1, min(limit, 100))
    start = max(offset, 0)
    paged_items = items[start : start + bounded_limit]

    return {
        "x402Version": 2,
        "items": paged_items,
        "pagination": {
            "limit": bounded_limit,
            "offset": start,
            "total": len(items),
        },
    }


@app.get("/metrics", tags=["operations"], summary="Operational metrics")
async def metrics_snapshot():
    """Expose lightweight operational counters for conversion and margin tuning."""
    metric_values = await durable_metrics_snapshot()
    attribution = await durable_attribution_snapshot()
    rolling_attribution = await durable_rolling_attribution_snapshot()
    triggered_alerts = await evaluate_alerts(metric_values)
    return {
        "status": "ok",
        "metrics": metric_values,
        "conversion_funnel": conversion_funnel_snapshot(metric_values),
        "attribution": attribution,
        "rolling_attribution": rolling_attribution,
        "metrics_backend": {
            "persistent": bool(redis_client and METRICS_PERSISTENCE_ENABLED),
            "redis_key": METRICS_REDIS_KEY if redis_client and METRICS_PERSISTENCE_ENABLED else None,
            "attribution_redis_key": ATTRIBUTION_REDIS_KEY if redis_client and METRICS_PERSISTENCE_ENABLED else None,
            "attribution_events_redis_key": (
                ATTRIBUTION_EVENTS_REDIS_KEY if redis_client and METRICS_PERSISTENCE_ENABLED else None
            ),
        },
        "alerts": {
            "enabled": bool(ALERT_WEBHOOK_URL),
            "triggered": triggered_alerts,
            "min_interval_seconds": ALERT_MIN_INTERVAL_SECONDS,
            "min_sample_size": ALERT_MIN_SAMPLE_SIZE,
            "thresholds": {
                "retryable_outage_rate": ALERT_RETRYABLE_OUTAGE_RATE,
                "ueg_rejection_rate": ALERT_UEG_REJECTION_RATE,
                "payment_validation_rejection_rate": ALERT_PAYMENT_VALIDATION_REJECTION_RATE,
                "supplier_success_min_rate": ALERT_SUPPLIER_SUCCESS_MIN_RATE,
                "base_rpc_error_rate": ALERT_BASE_RPC_ERROR_RATE,
                "jina_error_rate": ALERT_JINA_ERROR_RATE,
            },
        },
        "cache": {
            "backend": "redis" if redis_client else "memory",
            "default_ttl_seconds": DEFAULT_CACHE_TTL_SECONDS,
            "memory_entries": len(markdown_cache),
        },
        "delivery_credits": {
            "ttl_seconds": DELIVERY_CREDIT_TTL_SECONDS,
            "max_attempts": DELIVERY_CREDIT_MAX_ATTEMPTS,
            "memory_entries": len(delivery_credits),
        },
        "preflight": {
            "enabled": PREFLIGHT_ENABLED,
            "timeout_seconds": PREFLIGHT_TIMEOUT_SECONDS,
            "max_redirects": PREFLIGHT_MAX_REDIRECTS,
            "max_content_bytes": PREFLIGHT_MAX_CONTENT_BYTES,
            "allowed_target_ports": sorted(ALLOWED_TARGET_PORTS),
        },
        "price_feed": {
            "eth_usdc_url": ETH_USDC_PRICE_URL,
            "cache_ttl_seconds": ETH_USDC_PRICE_CACHE_TTL_SECONDS,
            "stale_ttl_seconds": ETH_USDC_PRICE_STALE_TTL_SECONDS,
            "price_floor_usdc": float(ETH_USDC_PRICE_FLOOR),
            "static_fallback_enabled": ETH_USDC_ALLOW_STATIC_FALLBACK,
        },
        "rate_limits": {
            "enabled": RATE_LIMIT_ENABLED,
            "window_seconds": RATE_LIMIT_WINDOW_SECONDS,
            "probe_per_ip": RATE_LIMIT_PROBE_PER_IP,
            "unpaid_per_ip": RATE_LIMIT_UNPAID_PER_IP,
            "paid_per_ip": RATE_LIMIT_PAID_PER_IP,
            "target_domain": RATE_LIMIT_TARGET_DOMAIN,
            "retry_per_ip": RATE_LIMIT_RETRY_PER_IP,
            "retry_per_credit": RATE_LIMIT_RETRY_PER_CREDIT,
            "legacy_payment_hash": RATE_LIMIT_LEGACY_PAYMENT_HASH,
            "compute_per_ip": RATE_LIMIT_COMPUTE_PER_IP,
        },
        "pricing": {tier: float(price) for tier, price in TIER_PRICING_USDC.items()},
        "proof_pack_pricing": {pack: float(price) for pack, price in PROOF_PACK_PRICING_USDC.items()},
    }


@app.head("/v1/x402/access", tags=["x402"], summary="Probe x402 payment requirements")
@app.get("/v1/x402/access", tags=["x402"], summary="Probe x402 payment requirements")
@app.head("/from/{source}/v1/x402/access", include_in_schema=False)
@app.get("/from/{source}/v1/x402/access", include_in_schema=False)
@app.head("/from/{source}/v1/x402/starter", include_in_schema=False)
@app.get("/from/{source}/v1/x402/starter", include_in_schema=False)
async def access_context_broker_x402_probe(request: Request, source: Optional[str] = None):
    """
    Return a machine-readable x402 challenge for directory probes.

    The paid Clean Context Broker operation is POST-only because it requires a
    target_url body. Some service directories probe submitted paths with GET, so
    this companion route advertises the same payment requirements without
    triggering upstream work or accepting payment for a body-less request.
    """
    inc_metric("requests_total")
    inc_metric("payment_required_total")
    inc_metric("payment_challenges_total")
    inc_attribution("payment_challenges", attribution_source_from_request(request))
    try:
        await enforce_rate_limit("x402_probe_ip", client_rate_identifier(request), RATE_LIMIT_PROBE_PER_IP)
    except RateLimitExceeded as exc:
        raise rate_limit_429(exc) from exc

    source_starter_path = request.url.path.endswith("/v1/x402/starter")
    requested_tier = (
        STARTER_TIER
        if source_starter_path
        else request.query_params.get("tier") or request.headers.get("X-AxonGate-Tier")
    )
    detail = "Payment Required. Use POST with PAYMENT-SIGNATURE and a JSON target_url body."
    raise HTTPException(
        status_code=402,
        detail=payment_required_detail(detail, requested_tier),
        headers=payment_required_headers(detail, requested_tier),
    )


@app.post(
    "/v1/x402/access",
    tags=["x402"],
    summary="Paid Web-to-Markdown context extraction",
    responses={
        200: {"description": "Clean markdown delivered"},
        400: {"description": "Invalid target, payment, or unit economics guard rejection"},
        402: {"description": "x402 payment required"},
        429: {"description": "Rate limit exceeded"},
        503: {"description": "Temporary upstream or network outage; retry after 5 seconds"},
    },
)
@app.post("/from/{source}/v1/x402/access", include_in_schema=False)
@app.post("/from/{source}/v1/x402/starter", include_in_schema=False)
async def access_context_broker_x402(
    request: Request,
    access_request: AccessRequest,
    source: Optional[str] = None,
    x_axongate_tier: Optional[str] = Header(None, alias="X-AxonGate-Tier"),
):
    """
    Standard PayAI/x402 endpoint.

    PaymentMiddlewareASGI verifies PAYMENT-SIGNATURE before this handler and
    settles only after a successful response. Error paths must not issue retry
    credits here because the payment has not been settled yet.
    """
    inc_metric("requests_total")
    inc_metric("x402_access_requests_total")
    source_starter_path = request.url.path.endswith("/v1/x402/starter")

    if not hasattr(request.state, "payment_payload"):
        source = attribution_source_from_request(request)
        inc_metric("payment_required_total")
        inc_metric("payment_challenges_total")
        inc_attribution("payment_challenges", source)
        try:
            await enforce_rate_limit("x402_unpaid_ip", client_rate_identifier(request), RATE_LIMIT_UNPAID_PER_IP)
        except RateLimitExceeded as exc:
            raise rate_limit_429(exc) from exc

        detail = "Payment Required. Retry with PAYMENT-SIGNATURE for the selected x402 requirement."
        requested_tier = (
            STARTER_TIER
            if source_starter_path
            else x_axongate_tier or request.query_params.get("tier") or access_request.tier
        )
        raise HTTPException(
            status_code=402,
            detail=payment_required_detail(detail, requested_tier),
            headers=payment_required_headers(detail, requested_tier),
        )

    payment_reference = payment_reference_from_request(request)
    payment_identifier = payment_identifier_from_request(request)
    source = attribution_source_from_request(request)
    inc_metric("paid_attempts_total")
    inc_attribution("paid_attempts", source)
    if payment_identifier:
        inc_metric("payment_identifier_seen_total")
    target_url: Optional[str] = None
    tier: Optional[str] = None
    try:
        target_url = await assert_public_target_url(access_request.target_url)
        requested_tier = (
            STARTER_TIER
            if source_starter_path
            else x_axongate_tier or request.query_params.get("tier") or access_request.tier
        )
        tier = normalize_tier(requested_tier)
        try:
            await enforce_rate_limit("x402_paid_ip", client_rate_identifier(request), RATE_LIMIT_PAID_PER_IP)
            await enforce_rate_limit("target_domain", target_domain_identifier(target_url), RATE_LIMIT_TARGET_DOMAIN)
            await enforce_rate_limit("payment_reference", payment_reference, RATE_LIMIT_PAID_PER_IP)
        except RateLimitExceeded as exc:
            raise rate_limit_429(exc) from exc

        if await has_standard_payment_reference(payment_reference):
            inc_metric("payment_replay_rejections_total")
            inc_attribution("payment_replay_rejections", source)
            raise HTTPException(
                status_code=402,
                detail="Payment proof has already been processed. Create a fresh x402 payment for a new delivery.",
            )

        inc_metric("payments_accepted_total")
        inc_attribution("payments_accepted", source)
        markdown, cache_hit, profitability = await deliver_paid_markdown(
            target_url=target_url,
            tier=tier,
            force_refresh=access_request.force_refresh,
            amount_usdc=price_for_tier(tier),
        )
        await mark_standard_payment_reference(payment_reference)
        inc_metric("payment_verified_total")
        inc_metric("delivery_success_total")
        inc_attribution("delivery_success", source)
        inc_metric("standard_delivery_success_total")
    except NetworkUnavailableError as exc:
        raise retry_later_503(exc) from exc
    except PaymentValidationError as exc:
        inc_metric("errors_total")
        inc_metric("payment_validation_rejections_total")
        raise HTTPException(status_code=400, detail=exc.detail) from exc

    return {
        "status": "success",
        "target_url": target_url,
        "tier": tier,
        "markdown": markdown,
        "cache": {"hit": cache_hit},
        "payment": {
            "mode": "x402-facilitator",
            "network": "eip155:8453",
            "vault_address": load_vault_address(),
            "token_address": BASE_USDC_ADDRESS,
            "amount_usdc": float(price_for_tier(tier)),
            "payment_identifier": payment_identifier,
            "source": source,
        },
        "ueg_receipt": {
            "revenue_usdc": float(profitability.revenue_usdc),
            "dynamic_gas_cost_usdc": float(profitability.dynamic_gas_cost_usdc),
            "jina_api_cost_usdc": float(profitability.jina_api_cost_usdc),
            "projected_profit_usdc": float(profitability.projected_profit_usdc),
            "minimum_margin_usdc": float(MIN_PROFIT_MARGIN_USDC),
            "base_fee_wei": profitability.base_fee_wei,
            "gas_units": profitability.gas_units,
            "supplier_attempts": profitability.supplier_attempts,
            "eth_usdc_price": float(profitability.eth_usdc_price),
            "eth_usdc_price_source": profitability.eth_usdc_price_source,
            "eth_usdc_floor_applied": profitability.eth_usdc_floor_applied,
        },
    }


@app.head("/v1/x402/proof-pack", tags=["x402"], summary="Probe Proof Pack x402 payment requirements")
@app.get("/v1/x402/proof-pack", tags=["x402"], summary="Probe Proof Pack x402 payment requirements")
@app.head("/from/{source}/v1/x402/proof-pack", include_in_schema=False)
@app.get("/from/{source}/v1/x402/proof-pack", include_in_schema=False)
async def proof_pack_x402_probe(
    request: Request,
    source: Optional[str] = None,
    pack: Optional[str] = None,
    x_axongate_pack: Optional[str] = Header(None, alias="X-AxonGate-Pack"),
):
    """Return a machine-readable x402 challenge for Proof Pack probes."""
    inc_metric("requests_total")
    inc_metric("payment_required_total")
    inc_metric("payment_challenges_total")
    inc_attribution("payment_challenges", attribution_source_from_request(request))
    try:
        await enforce_rate_limit("x402_probe_ip", client_rate_identifier(request), RATE_LIMIT_PROBE_PER_IP)
    except RateLimitExceeded as exc:
        raise rate_limit_429(exc) from exc

    requested_pack = x_axongate_pack or pack or request.query_params.get("pack")
    detail = "Payment Required. Use POST with PAYMENT-SIGNATURE and a JSON Proof Pack body."
    raise HTTPException(
        status_code=402,
        detail=proof_pack_payment_required_detail(detail, requested_pack),
        headers=proof_pack_payment_required_headers(detail, requested_pack),
    )


@app.post(
    "/v1/x402/proof-pack",
    tags=["x402"],
    summary="Paid citation-backed Proof Pack",
    responses={
        200: {"description": "Proof Pack delivered"},
        400: {"description": "Invalid target, pack, payment, or unit economics guard rejection"},
        402: {"description": "x402 payment required"},
        429: {"description": "Rate limit exceeded"},
        503: {"description": "Temporary upstream or network outage; retry after 5 seconds"},
    },
)
@app.post("/from/{source}/v1/x402/proof-pack", include_in_schema=False)
async def proof_pack_x402(
    request: Request,
    proof_request: ProofPackRequest,
    source: Optional[str] = None,
    x_axongate_pack: Optional[str] = Header(None, alias="X-AxonGate-Pack"),
):
    """
    Standard PayAI/x402 endpoint for Proof Packs.

    The paid challenge amount is selected by ?pack= or X-AxonGate-Pack.
    The JSON body must match that pack so clients do not pay for one report
    level while requesting another.
    """
    inc_metric("requests_total")
    inc_metric("proof_pack_requests_total")

    if not hasattr(request.state, "payment_payload"):
        source = attribution_source_from_request(request)
        inc_metric("payment_required_total")
        inc_metric("payment_challenges_total")
        inc_attribution("payment_challenges", source)
        try:
            await enforce_rate_limit("x402_unpaid_ip", client_rate_identifier(request), RATE_LIMIT_UNPAID_PER_IP)
        except RateLimitExceeded as exc:
            raise rate_limit_429(exc) from exc

        detail = "Payment Required. Retry with PAYMENT-SIGNATURE for the selected Proof Pack requirement."
        requested_pack = x_axongate_pack or request.query_params.get("pack") or proof_request.pack
        raise HTTPException(
            status_code=402,
            detail=proof_pack_payment_required_detail(detail, requested_pack),
            headers=proof_pack_payment_required_headers(detail, requested_pack),
        )

    payment_reference = payment_reference_from_request(request)
    payment_identifier = payment_identifier_from_request(request)
    source = attribution_source_from_request(request)
    inc_metric("paid_attempts_total")
    inc_attribution("paid_attempts", source)
    inc_attribution("proof_pack_requests", source)
    if payment_identifier:
        inc_metric("payment_identifier_seen_total")

    target_url: Optional[str] = None
    pack: Optional[str] = None
    try:
        target_url = await assert_public_target_url(proof_request.target_url)
        question = proof_pack_question(proof_request.question)
        pack = resolve_proof_pack_selection(
            request,
            proof_request,
            x_axongate_pack,
            require_challenge_selector=True,
        )
        try:
            await enforce_rate_limit("x402_paid_ip", client_rate_identifier(request), RATE_LIMIT_PAID_PER_IP)
            await enforce_rate_limit("target_domain", target_domain_identifier(target_url), RATE_LIMIT_TARGET_DOMAIN)
            await enforce_rate_limit("payment_reference", payment_reference, RATE_LIMIT_PAID_PER_IP)
        except RateLimitExceeded as exc:
            raise rate_limit_429(exc) from exc

        if await has_standard_payment_reference(payment_reference):
            inc_metric("payment_replay_rejections_total")
            inc_attribution("payment_replay_rejections", source)
            raise HTTPException(
                status_code=402,
                detail="Payment proof has already been processed. Create a fresh x402 payment for a new Proof Pack.",
            )

        amount_usdc = price_for_proof_pack(pack)
        internal_tier = proof_pack_internal_tier(pack)
        force_refresh = bool(proof_request.force_refresh or pack == "deep")
        inc_metric("payments_accepted_total")
        inc_attribution("payments_accepted", source)
        markdown, cache_hit, profitability = await deliver_paid_markdown(
            target_url=target_url,
            tier=internal_tier,
            force_refresh=force_refresh,
            amount_usdc=amount_usdc,
        )
        proof_content = await generate_proof_pack_content(
            target_url=target_url,
            question=question,
            pack=pack,
            markdown=markdown,
            cache_hit=cache_hit,
        )
        await mark_standard_payment_reference(payment_reference)
        inc_metric("payment_verified_total")
        inc_metric("delivery_success_total")
        inc_metric("proof_pack_delivery_success_total")
        inc_attribution("delivery_success", source)
        inc_attribution("proof_pack_delivery_success", source)
    except NetworkUnavailableError as exc:
        raise retry_later_503(exc) from exc
    except PaymentValidationError as exc:
        inc_metric("errors_total")
        inc_metric("payment_validation_rejections_total")
        raise HTTPException(status_code=400, detail=exc.detail) from exc

    return {
        "status": "success",
        "target_url": target_url,
        "question": question,
        "pack": pack,
        "answer": proof_content["answer"],
        "executive_summary": proof_content["executive_summary"],
        "confidence_score": proof_content["confidence_score"],
        "key_claims": proof_content["key_claims"],
        "citations": [
            {key: value for key, value in citation.items() if key != "fingerprint"}
            for citation in proof_content["citations"]
        ],
        "risks": proof_content["risks"],
        "source_profile": proof_content["source_profile"],
        "cache": {"hit": cache_hit},
        "llm_used": proof_content["llm_used"],
        "llm_model": proof_content["llm_model"],
        "fallback_reason": proof_content["fallback_reason"],
        "payment": {
            "mode": "x402-facilitator",
            "network": "eip155:8453",
            "vault_address": load_vault_address(),
            "token_address": BASE_USDC_ADDRESS,
            "amount_usdc": float(price_for_proof_pack(pack)),
            "payment_identifier": payment_identifier,
            "source": source,
        },
        "ueg_receipt": ueg_receipt_payload(profitability),
    }


@app.post(
    "/v1/x402/retry",
    tags=["x402"],
    summary="Retry a paid delivery with an AxonGate retry credit",
    responses={
        200: {"description": "Clean markdown delivered using retry credit"},
        400: {"description": "Invalid or exhausted retry credit"},
        402: {"description": "Retry credit required"},
        429: {"description": "Rate limit exceeded"},
        503: {"description": "Temporary upstream or network outage; retry after 5 seconds"},
    },
)
async def retry_context_broker_delivery(
    request: Request,
    access_request: AccessRequest,
    x_axongate_retry_credit: Optional[str] = Header(None, alias="X-AxonGate-Retry-Credit"),
):
    """
    Retry a paid delivery without requiring a second payment.

    This endpoint is intentionally separate from /v1/x402/access because the
    standard x402 middleware may reject a replayed payment proof before the
    application handler can inspect it. Instead, AxonGate returns a short-lived
    X-AxonGate-Retry-Credit after retryable post-payment failures. The credit is
    scoped to the original target URL, tier, and cache mode, and every retry is
    still checked by the Unit Economic Guardian before Jina supplier work occurs.
    """
    inc_metric("requests_total")
    inc_metric("delivery_credit_retries_total")
    source = attribution_source_from_request(request)

    if not x_axongate_retry_credit:
        inc_metric("payment_required_total")
        inc_metric("payment_challenges_total")
        inc_attribution("payment_challenges", source)
        try:
            await enforce_rate_limit("retry_missing_credit_ip", client_rate_identifier(request), RATE_LIMIT_UNPAID_PER_IP)
        except RateLimitExceeded as exc:
            raise rate_limit_429(exc) from exc

        detail = "Payment Required. Provide PAYMENT-SIGNATURE or a valid X-AxonGate-Retry-Credit."
        raise HTTPException(
            status_code=402,
            detail=payment_required_detail(detail, access_request.tier),
            headers=payment_required_headers(detail, access_request.tier),
        )

    reservation: DeliveryCreditReservation | None = None
    try:
        inc_metric("retry_credit_attempts_total")
        inc_attribution("retry_credit_attempts", source)
        target_url = await assert_public_target_url(access_request.target_url)
        tier = normalize_tier(access_request.tier)
        try:
            await enforce_rate_limit("retry_ip", client_rate_identifier(request), RATE_LIMIT_RETRY_PER_IP)
            await enforce_rate_limit(
                "retry_credit",
                retry_credit_identifier(x_axongate_retry_credit),
                RATE_LIMIT_RETRY_PER_CREDIT,
            )
            await enforce_rate_limit("target_domain", target_domain_identifier(target_url), RATE_LIMIT_TARGET_DOMAIN)
        except RateLimitExceeded as exc:
            raise rate_limit_429(exc) from exc

        reservation = await reserve_delivery_credit(
            x_axongate_retry_credit,
            target_url=target_url,
            tier=tier,
            force_refresh=access_request.force_refresh,
        )

        amount_usdc = Decimal(str(reservation.record["amount_usdc"]))
        try:
            markdown, cache_hit, profitability = await deliver_paid_markdown(
                target_url=target_url,
                tier=tier,
                force_refresh=access_request.force_refresh,
                amount_usdc=amount_usdc,
                supplier_attempts_on_hit=max(0, reservation.total_supplier_attempts - 1),
                supplier_attempts_on_miss=reservation.total_supplier_attempts,
            )
        except PaymentValidationError as exc:
            if "Dynamic UEG" in exc.detail:
                exc.detail = "Retry credit rejected; projected margin is below AxonGate guard"
            raise

        await delete_delivery_credit(reservation.token)
        inc_metric("delivery_credit_success_total")
        inc_metric("payment_verified_total")
        inc_metric("delivery_success_total")
        inc_attribution("delivery_success", source)
        inc_metric("retry_delivery_success_total")
    except NetworkUnavailableError as exc:
        credit = None
        if reservation is not None:
            remaining_attempts = reservation.remaining_attempts
            if not exc.supplier_attempt_charged:
                await restore_delivery_credit_attempt(reservation)
                remaining_attempts = min(DELIVERY_CREDIT_MAX_ATTEMPTS, remaining_attempts + 1)

            if exc.creditable and remaining_attempts > 0:
                credit = delivery_credit_response(reservation.token, remaining_attempts)
            else:
                await delete_delivery_credit(reservation.token)
                inc_metric("delivery_credit_exhausted_total")

        raise retry_later_503(exc, credit) from exc
    except PaymentValidationError as exc:
        if reservation is not None:
            if exc.consume_credit:
                await delete_delivery_credit(reservation.token)
                inc_metric("delivery_credit_exhausted_total")
            else:
                await restore_delivery_credit_attempt(reservation)
        inc_metric("errors_total")
        inc_metric("payment_validation_rejections_total")
        raise HTTPException(status_code=400, detail=exc.detail) from exc

    return {
        "status": "success",
        "target_url": target_url,
        "tier": tier,
        "markdown": markdown,
        "cache": {"hit": cache_hit},
        "payment": {
            "mode": "delivery-credit",
            "original_mode": reservation.record.get("mode"),
            "network": "eip155:8453",
            "vault_address": load_vault_address(),
            "token_address": BASE_USDC_ADDRESS,
            "amount_usdc": float(amount_usdc),
            "source": source,
        },
        "ueg_receipt": {
            "revenue_usdc": float(profitability.revenue_usdc),
            "dynamic_gas_cost_usdc": float(profitability.dynamic_gas_cost_usdc),
            "jina_api_cost_usdc": float(profitability.jina_api_cost_usdc),
            "projected_profit_usdc": float(profitability.projected_profit_usdc),
            "minimum_margin_usdc": float(MIN_PROFIT_MARGIN_USDC),
            "base_fee_wei": profitability.base_fee_wei,
            "gas_units": profitability.gas_units,
            "supplier_attempts": profitability.supplier_attempts,
            "eth_usdc_price": float(profitability.eth_usdc_price),
            "eth_usdc_price_source": profitability.eth_usdc_price_source,
            "eth_usdc_floor_applied": profitability.eth_usdc_floor_applied,
        },
    }


@app.post(
    "/v1/access",
    tags=["legacy"],
    summary="Legacy tx-hash paid Web-to-Markdown context extraction",
    responses={
        200: {"description": "Clean markdown delivered"},
        400: {"description": "Invalid payment hash, replay, target, or unit economics guard rejection"},
        402: {"description": "Payment hash required"},
        429: {"description": "Rate limit exceeded"},
        503: {"description": "Temporary upstream or network outage; retry after 5 seconds"},
    },
)
async def access_context_broker(
    access_request: AccessRequest,
    http_request: Request,
    x_axongate_payment_hash: Optional[str] = Header(None, alias="X-AxonGate-Payment-Hash"),
):
    """
    Paid Clean Context Broker endpoint.

    Clients post a target URL and provide X-AxonGate-Payment-Hash. AxonGate first
    verifies the on-chain x402 payment and the dynamic UEG margin. Only then does
    it spend the upstream Jina Reader call and return cleaned markdown.
    """
    inc_metric("requests_total")
    inc_metric("legacy_access_requests_total")
    source = attribution_source_from_request(http_request)

    if not x_axongate_payment_hash:
        inc_metric("payment_required_total")
        inc_metric("payment_challenges_total")
        inc_attribution("payment_challenges", source)
        detail = "Payment Required. Provide X-AxonGate-Payment-Hash with a Base USDC transaction hash."
        raise HTTPException(
            status_code=402,
            detail=payment_required_detail(detail, access_request.tier),
            headers=payment_required_headers(detail, access_request.tier),
        )

    payment: PaymentVerification | None = None
    target_url: Optional[str] = None
    tier: Optional[str] = None
    cached_markdown: Optional[str] = None
    try:
        inc_metric("paid_attempts_total")
        inc_attribution("paid_attempts", source)
        target_url = await assert_public_target_url(access_request.target_url)
        tier = normalize_tier(access_request.tier)
        try:
            await enforce_rate_limit("legacy_ip", client_rate_identifier(http_request), RATE_LIMIT_PAID_PER_IP)
            await enforce_rate_limit("target_domain", target_domain_identifier(target_url), RATE_LIMIT_TARGET_DOMAIN)
            await enforce_rate_limit(
                "legacy_payment_hash",
                payment_hash_identifier(x_axongate_payment_hash),
                RATE_LIMIT_LEGACY_PAYMENT_HASH,
            )
        except RateLimitExceeded as exc:
            raise rate_limit_429(exc) from exc

        price = price_for_tier(tier)
        if is_cache_only_tier(tier) and access_request.force_refresh:
            raise PaymentValidationError("Starter and cached tiers cannot force refresh. Use basic, fresh, or deep for a live fetch.")
        if is_cache_only_tier(tier):
            cached_markdown = await get_cache_candidate_for_tier(target_url, tier, False)
            if cached_markdown is None:
                inc_metric("cache_misses_total")
                raise PaymentValidationError(
                    "Starter and cached tiers require the starter sample or an existing AxonGate cache entry. Use basic, fresh, or deep for a live fetch."
                )
        payment = await verify_x402_payment(x_axongate_payment_hash, price)
        inc_metric("payments_accepted_total")
        inc_attribution("payments_accepted", source)
        if cached_markdown is not None:
            inc_metric("cache_hits_total")
            profitability = await calculate_profitability_for_price(payment.amount_usdc, supplier_attempts=0)
            if profitability.projected_profit_usdc <= MIN_PROFIT_MARGIN_USDC:
                inc_metric("ueg_rejections_total")
                raise PaymentValidationError("Dynamic UEG rejected request; projected margin is too low")
            markdown, cache_hit = cached_markdown, True
        else:
            markdown, cache_hit, profitability = await deliver_paid_markdown(
                target_url=target_url,
                tier=tier,
                force_refresh=access_request.force_refresh,
                amount_usdc=payment.amount_usdc,
            )
        inc_metric("payment_verified_total")
        inc_metric("delivery_success_total")
        inc_attribution("delivery_success", source)
        inc_metric("legacy_delivery_success_total")
    except NetworkUnavailableError as exc:
        credit = None
        if payment is not None and target_url and tier:
            credit = await maybe_issue_delivery_credit(
                exc=exc,
                payment_reference=f"legacy-tx:{payment.tx_hash}",
                target_url=target_url,
                tier=tier,
                force_refresh=access_request.force_refresh,
                amount_usdc=payment.amount_usdc,
                mode="legacy-tx-hash",
            )
        raise retry_later_503(exc, credit) from exc
    except RuntimeError as exc:
        inc_metric("errors_total")
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except PaymentValidationError as exc:
        inc_metric("errors_total")
        inc_metric("payment_validation_rejections_total")
        raise HTTPException(status_code=400, detail=exc.detail) from exc

    return {
        "status": "success",
        "target_url": target_url,
        "tier": tier,
        "markdown": markdown,
        "cache": {"hit": cache_hit},
        "payment": {
            "tx_hash": payment.tx_hash,
            "network": "base-mainnet",
            "vault_address": payment.vault_address,
            "token_address": payment.token_address,
            "amount_usdc": float(payment.amount_usdc),
            "source": source,
        },
        "ueg_receipt": {
            "revenue_usdc": float(profitability.revenue_usdc),
            "dynamic_gas_cost_usdc": float(profitability.dynamic_gas_cost_usdc),
            "jina_api_cost_usdc": float(profitability.jina_api_cost_usdc),
            "projected_profit_usdc": float(profitability.projected_profit_usdc),
            "minimum_margin_usdc": float(MIN_PROFIT_MARGIN_USDC),
            "base_fee_wei": profitability.base_fee_wei,
            "gas_units": profitability.gas_units,
            "supplier_attempts": profitability.supplier_attempts,
            "eth_usdc_price": float(profitability.eth_usdc_price),
            "eth_usdc_price_source": profitability.eth_usdc_price_source,
            "eth_usdc_floor_applied": profitability.eth_usdc_floor_applied,
        },
    }


@app.post("/v1/broker/compute", tags=["legacy"], summary="Legacy simulated brokerage endpoint")
async def process_task(payload: ComputeRequest, request: Request, x402_token: str = Header(None)):
    print(f"\n[INBOUND REQUEST] Agent: {payload.agent_id}")

    try:
        await enforce_rate_limit("compute_ip", client_rate_identifier(request), RATE_LIMIT_COMPUTE_PER_IP)
    except RateLimitExceeded as exc:
        raise rate_limit_429(exc) from exc

    if not x402_token:
        print("[REJECTED] Missing x402 Payment Header")
        raise HTTPException(status_code=401, detail="Missing x402 Payment Token")

    try:
        profitability = await calculate_profitability()
        offered_fee_usdc = Decimal(str(payload.offered_fee))
        projected_profit = offered_fee_usdc - (
            profitability.dynamic_gas_cost_usdc + profitability.jina_api_cost_usdc
        )
        if projected_profit <= MIN_PROFIT_MARGIN_USDC:
            inc_metric("ueg_rejections_total")
            raise PaymentValidationError("Dynamic UEG rejected transaction; offered fee margin is too low")
    except NetworkUnavailableError as exc:
        raise retry_later_503(exc) from exc
    except PaymentValidationError as exc:
        raise HTTPException(status_code=402, detail=exc.detail) from exc

    print(
        "[UEG CHECK] "
        f"Offered: {offered_fee_usdc:.6f} USDC | "
        f"Dynamic Gas: {profitability.dynamic_gas_cost_usdc:.6f} USDC | "
        f"Jina Cost: {profitability.jina_api_cost_usdc:.6f} USDC | "
        f"Profit: {projected_profit:.6f} USDC"
    )
    print("[UEG PASSED] Executing API Brokerage...")

    await asyncio.sleep(1.5)

    response_payload = {
        "status": "success",
        "message": "Task processed successfully via AxonGate Brokerage",
        "result": "simulated_llm_output_data",
        "ueg_receipt": {
            "fee_collected_usdc": float(offered_fee_usdc),
            "dynamic_gas_cost_usdc": float(profitability.dynamic_gas_cost_usdc),
            "net_profit_usdc": float(projected_profit),
            "eth_usdc_price": float(profitability.eth_usdc_price),
            "eth_usdc_price_source": profitability.eth_usdc_price_source,
            "eth_usdc_floor_applied": profitability.eth_usdc_floor_applied,
            "timestamp": time.time(),
        },
    }

    print("[DISPATCHING RESPONSE] Task complete.")
    return response_payload


def custom_openapi() -> dict[str, Any]:
    """Attach payment-discovery metadata to the generated OpenAPI document."""
    if app.openapi_schema:
        return app.openapi_schema

    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    payment_info = build_openapi_payment_info()
    access_post = schema.get("paths", {}).get("/v1/x402/access", {}).get("post")
    if isinstance(access_post, dict):
        access_post["x-payment-info"] = payment_info
        responses = access_post.setdefault("responses", {})
        payment_required = responses.setdefault("402", {"description": "x402 payment required"})
        payment_required.setdefault("headers", {}).update(
            {
                "PAYMENT-REQUIRED": {
                    "description": "Base64-encoded x402 PaymentRequired payload.",
                    "schema": {"type": "string"},
                },
                "X-Payment-Required": {
                    "description": "Compatibility alias for PAYMENT-REQUIRED.",
                    "schema": {"type": "string"},
                },
                "X-AxonGate-Payment-Tier": {
                    "description": "Resolved pricing tier for the challenge.",
                    "schema": {"type": "string", "enum": list(TIER_PRICING_USDC.keys())},
                },
                "X-AxonGate-Paid-Test": {
                    "description": "Human/operator guide for running a real paid smoke test.",
                    "schema": {"type": "string", "format": "uri"},
                },
                "X-AxonGate-Quickstart": {
                    "description": "Shortest path for a first paid AxonGate result and MCP setup.",
                    "schema": {"type": "string", "format": "uri"},
                },
                "X-AxonGate-Quote": {
                    "description": "Supplier-free tier quote endpoint for choosing the right paid path.",
                    "schema": {"type": "string", "format": "uri"},
                },
                "X-AxonGate-Buyer-Example": {
                    "description": "Repository example for creating and sending an x402 payment proof.",
                    "schema": {"type": "string", "format": "uri"},
                },
            }
        )

    proof_post = schema.get("paths", {}).get("/v1/x402/proof-pack", {}).get("post")
    if isinstance(proof_post, dict):
        proof_post["x-payment-info"] = {
            **payment_info,
            "endpoint": f"{PUBLIC_BASE_URL}/v1/x402/proof-pack",
            "packHeader": "X-AxonGate-Pack",
            "packQueryParam": "pack",
            "proofPacks": payment_info.get("proofPacks", {}),
        }
        responses = proof_post.setdefault("responses", {})
        payment_required = responses.setdefault("402", {"description": "x402 payment required"})
        payment_required.setdefault("headers", {}).update(
            {
                "PAYMENT-REQUIRED": {
                    "description": "Base64-encoded x402 PaymentRequired payload.",
                    "schema": {"type": "string"},
                },
                "X-Payment-Required": {
                    "description": "Compatibility alias for PAYMENT-REQUIRED.",
                    "schema": {"type": "string"},
                },
                "X-AxonGate-Payment-Pack": {
                    "description": "Resolved Proof Pack level for the challenge.",
                    "schema": {"type": "string", "enum": list(PROOF_PACK_PRICING_USDC.keys())},
                },
                "X-AxonGate-Proof-Pack": {
                    "description": "Human-facing Proof Pack product page.",
                    "schema": {"type": "string", "format": "uri"},
                },
                "X-AxonGate-Proof-Pack-Quote": {
                    "description": "Supplier-free Proof Pack quote endpoint.",
                    "schema": {"type": "string", "format": "uri"},
                },
            }
        )

    access_request_schema = schema.get("components", {}).get("schemas", {}).get("AccessRequest")
    if isinstance(access_request_schema, dict):
        tier_property = access_request_schema.get("properties", {}).get("tier")
        if isinstance(tier_property, dict):
            tier_property["enum"] = list(TIER_PRICING_USDC.keys())
            tier_property["default"] = RECOMMENDED_TIER

    proof_request_schema = schema.get("components", {}).get("schemas", {}).get("ProofPackRequest")
    if isinstance(proof_request_schema, dict):
        pack_property = proof_request_schema.get("properties", {}).get("pack")
        if isinstance(pack_property, dict):
            pack_property["enum"] = list(PROOF_PACK_PRICING_USDC.keys())
            pack_property["default"] = DEFAULT_PROOF_PACK

    schema["x-payment-info"] = payment_info
    app.openapi_schema = schema
    return app.openapi_schema


app.openapi = custom_openapi


if __name__ == "__main__":
    print("Booting AxonGate Revenue Server...")
    print("Listening for Agent-to-Agent (A2A) traffic on port 8000")
    uvicorn.run(app, host="0.0.0.0", port=8000)
