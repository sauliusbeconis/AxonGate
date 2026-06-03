import asyncio
import base64
import hashlib
import hmac
import html
import ipaddress
import inspect
import json
import os
import re
import secrets
import socket
import textwrap
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, quote as url_quote, urljoin, urlparse

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
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
    description="x402-paid evidence trust layer for AI agents that need source support, citation quality, and clean context on Base.",
    version="1.3.0",
    docs_url="/swagger",
    redoc_url="/redoc",
)

DEFAULT_PUBLIC_BASE_URL = "https://api.axongate.one"
PUBLIC_BASE_URL = os.getenv("AXONGATE_PUBLIC_BASE_URL", DEFAULT_PUBLIC_BASE_URL).rstrip("/")
PUBLIC_BASE_PARSED = urlparse(PUBLIC_BASE_URL)
PUBLIC_BASE_SCHEME = PUBLIC_BASE_PARSED.scheme.lower()
PUBLIC_BASE_HOST = (PUBLIC_BASE_PARSED.hostname or "").lower()
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
PROOF_PACK_LEADS_REDIS_KEY = os.getenv("AXONGATE_PROOF_PACK_LEADS_REDIS_KEY", "axongate:proof_pack_leads")
PROOF_PACK_LEADS_MEMORY_MAX = int(os.getenv("AXONGATE_PROOF_PACK_LEADS_MEMORY_MAX", "200"))
ATTRIBUTION_EVENT_RETENTION_SECONDS = int(os.getenv("AXONGATE_ATTRIBUTION_EVENT_RETENTION_SECONDS", str(7 * 24 * 60 * 60)))
ATTRIBUTION_EVENT_MEMORY_MAX = int(os.getenv("AXONGATE_ATTRIBUTION_EVENT_MEMORY_MAX", "10000"))
ALERT_WEBHOOK_URL = os.getenv("AXONGATE_ALERT_WEBHOOK_URL")
ALERT_WEBHOOK_TOKEN = os.getenv("AXONGATE_ALERT_WEBHOOK_TOKEN")
ALERT_MIN_INTERVAL_SECONDS = int(os.getenv("AXONGATE_ALERT_MIN_INTERVAL_SECONDS", "300"))
ALERT_WEBHOOK_TIMEOUT_SECONDS = float(os.getenv("AXONGATE_ALERT_WEBHOOK_TIMEOUT_SECONDS", "5"))
OPERATOR_TOKEN = (os.getenv("AXONGATE_OPERATOR_TOKEN") or os.getenv("AXONGATE_ALERT_WEBHOOK_TOKEN") or "").strip()
PROOF_PACK_LEAD_WEBHOOK_URL = os.getenv("AXONGATE_PROOF_PACK_LEAD_WEBHOOK_URL", "").strip()
PROOF_PACK_LEAD_WEBHOOK_TOKEN = os.getenv("AXONGATE_PROOF_PACK_LEAD_WEBHOOK_TOKEN", "").strip()
PROOF_PACK_LEAD_WEBHOOK_TIMEOUT_SECONDS = float(os.getenv("AXONGATE_PROOF_PACK_LEAD_WEBHOOK_TIMEOUT_SECONDS", "5"))
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
FORCE_HTTPS_ENABLED = os.getenv("AXONGATE_FORCE_HTTPS", "true").lower() not in {"0", "false", "no"}
SECURITY_HEADERS_ENABLED = os.getenv("AXONGATE_SECURITY_HEADERS_ENABLED", "true").lower() not in {
    "0",
    "false",
    "no",
}
HSTS_MAX_AGE_SECONDS = int(os.getenv("AXONGATE_HSTS_MAX_AGE_SECONDS", "31536000"))
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
    "proof_pack_previews",
    "proof_pack_quotes",
    "proof_pack_leads",
    "proof_bundle_quotes",
    "proof_bundle_leads",
    "proof_bundle_payment_clicks",
    "proof_bundle_paid",
    "proof_bundle_fulfilled",
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
DEFAULT_PROOF_BUNDLE = "builder"
PROOF_BUNDLE_PRICING_USDC = {
    "scout": Decimal(os.getenv("AXONGATE_PROOF_BUNDLE_SCOUT_PRICE_USDC", "2.00")),
    DEFAULT_PROOF_BUNDLE: Decimal(os.getenv("AXONGATE_PROOF_BUNDLE_BUILDER_PRICE_USDC", "7.00")),
    "audit": Decimal(os.getenv("AXONGATE_PROOF_BUNDLE_AUDIT_PRICE_USDC", "20.00")),
}
PROOF_BUNDLE_SOURCE_LIMITS = {
    "scout": int(os.getenv("AXONGATE_PROOF_BUNDLE_SCOUT_SOURCE_LIMIT", "3")),
    DEFAULT_PROOF_BUNDLE: int(os.getenv("AXONGATE_PROOF_BUNDLE_BUILDER_SOURCE_LIMIT", "10")),
    "audit": int(os.getenv("AXONGATE_PROOF_BUNDLE_AUDIT_SOURCE_LIMIT", "25")),
}
PROOF_BUNDLE_PAYMENT_URLS = {
    "scout": os.getenv("AXONGATE_PROOF_BUNDLE_SCOUT_PAYMENT_URL", "").strip(),
    DEFAULT_PROOF_BUNDLE: os.getenv("AXONGATE_PROOF_BUNDLE_BUILDER_PAYMENT_URL", "").strip(),
    "audit": os.getenv("AXONGATE_PROOF_BUNDLE_AUDIT_PAYMENT_URL", "").strip(),
}
PROOF_BUNDLE_LEAD_STATUSES = ("new", "contacted", "paid", "fulfilled", "lost")
STRIPE_WEBHOOK_SECRET = os.getenv("AXONGATE_STRIPE_WEBHOOK_SECRET", "").strip()
STRIPE_WEBHOOK_TOLERANCE_SECONDS = int(os.getenv("AXONGATE_STRIPE_WEBHOOK_TOLERANCE_SECONDS", "300"))
STRIPE_EVENTS_REDIS_KEY = os.getenv("AXONGATE_STRIPE_EVENTS_REDIS_KEY", "axongate:stripe_events")
STRIPE_EVENTS_RETENTION_SECONDS = int(os.getenv("AXONGATE_STRIPE_EVENTS_RETENTION_SECONDS", str(30 * 24 * 60 * 60)))
EMAIL_DELIVERY_ENABLED = os.getenv("AXONGATE_EMAIL_DELIVERY_ENABLED", "false").lower() in {"1", "true", "yes"}
EMAIL_PROVIDER = os.getenv("AXONGATE_EMAIL_PROVIDER", "resend").strip().lower()
EMAIL_FROM = os.getenv("AXONGATE_EMAIL_FROM", "AxonGate <reports@axongate.one>").strip()
EMAIL_REPLY_TO = os.getenv("AXONGATE_EMAIL_REPLY_TO", "").strip()
PUBLIC_CONTACT_EMAIL = os.getenv("AXONGATE_PUBLIC_CONTACT_EMAIL", EMAIL_REPLY_TO or "reports@axongate.one").strip()
CONTACT_NOTIFY_EMAIL = os.getenv("AXONGATE_CONTACT_NOTIFY_EMAIL", EMAIL_REPLY_TO or PUBLIC_CONTACT_EMAIL).strip()
RESEND_API_KEY = os.getenv("AXONGATE_RESEND_API_KEY", "").strip()
RESEND_API_URL = os.getenv("AXONGATE_RESEND_API_URL", "https://api.resend.com/emails").strip()
RESEND_TIMEOUT_SECONDS = float(os.getenv("AXONGATE_RESEND_TIMEOUT_SECONDS", "10"))
email_delivery_last_error = ""
email_delivery_last_error_at = 0
email_delivery_last_status_code: Optional[int] = None
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
proof_pack_leads: list[dict[str, Any]] = []
proof_pack_leads_lock = asyncio.Lock()
processed_stripe_events: set[str] = set()
processed_stripe_events_lock = asyncio.Lock()
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
    "discovery_about_hits_total": 0,
    "discovery_faq_hits_total": 0,
    "discovery_contact_hits_total": 0,
    "discovery_operator_hits_total": 0,
    "discovery_operator_leads_hits_total": 0,
    "discovery_quickstart_hits_total": 0,
    "discovery_paid_test_hits_total": 0,
    "discovery_quote_hits_total": 0,
    "discovery_proof_pack_hits_total": 0,
    "discovery_proof_pack_sample_hits_total": 0,
    "discovery_proof_pack_preview_hits_total": 0,
    "discovery_proof_pack_quote_hits_total": 0,
    "discovery_proof_pack_request_hits_total": 0,
    "discovery_proof_bundle_hits_total": 0,
    "discovery_proof_bundle_quote_hits_total": 0,
    "discovery_proof_bundle_request_hits_total": 0,
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
    "proof_pack_previews_total": 0,
    "proof_pack_preview_cache_hits_total": 0,
    "proof_pack_quotes_total": 0,
    "proof_pack_leads_total": 0,
    "proof_pack_lead_errors_total": 0,
    "proof_pack_lead_notifications_total": 0,
    "proof_pack_lead_notification_errors_total": 0,
    "contact_form_submits_total": 0,
    "contact_form_errors_total": 0,
    "contact_notifications_total": 0,
    "contact_notification_errors_total": 0,
    "proof_bundle_quotes_total": 0,
    "proof_bundle_leads_total": 0,
    "proof_bundle_lead_errors_total": 0,
    "proof_bundle_payment_clicks_total": 0,
    "proof_bundle_payment_configured_clicks_total": 0,
    "proof_bundle_payment_missing_clicks_total": 0,
    "proof_bundle_status_updates_total": 0,
    "proof_bundle_paid_total": 0,
    "proof_bundle_fulfilled_total": 0,
    "proof_bundle_delivery_requests_total": 0,
    "proof_bundle_recovery_requests_total": 0,
    "proof_bundle_auto_fulfillment_started_total": 0,
    "proof_bundle_auto_fulfillment_success_total": 0,
    "proof_bundle_auto_fulfillment_errors_total": 0,
    "proof_bundle_delivery_email_attempts_total": 0,
    "proof_bundle_delivery_email_success_total": 0,
    "proof_bundle_delivery_email_errors_total": 0,
    "proof_bundle_delivery_email_missing_recipient_total": 0,
    "proof_bundle_delivery_email_disabled_total": 0,
    "stripe_webhook_events_total": 0,
    "stripe_webhook_verified_total": 0,
    "stripe_webhook_signature_failures_total": 0,
    "stripe_webhook_misconfigured_total": 0,
    "stripe_webhook_duplicate_events_total": 0,
    "stripe_webhook_unsupported_events_total": 0,
    "stripe_webhook_payment_succeeded_total": 0,
    "stripe_webhook_payment_failed_total": 0,
    "stripe_webhook_pending_payment_total": 0,
    "stripe_webhook_fulfillment_errors_total": 0,
    "proof_pack_requests_total": 0,
    "proof_pack_llm_success_total": 0,
    "proof_pack_llm_fallback_total": 0,
    "proof_pack_delivery_success_total": 0,
    "errors_total": 0,
    "operator_auth_failures_total": 0,
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
    target_url: str = Field(..., description="Public URL to convert into clean markdown; bare domains are prefixed with https")
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
    target_url: str = Field(..., description="Public source URL to turn into a citation-backed report; bare domains are prefixed with https")
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


class ProofPackLeadRequest(BaseModel):
    contact: str = Field(..., description="Email, Telegram, X handle, or other reply path")
    target_url: str = Field(..., description="Public source URL the buyer wants converted into a Proof Pack; bare domains are prefixed with https")
    question: Optional[str] = Field(None, description="Buyer question or evidence objective")
    pack: str = Field("quick", description="Proof Pack level: quick, standard, or deep")
    use_case: Optional[str] = Field(None, description="How the buyer plans to use the report")
    budget_usdc: Optional[str] = Field(None, description="Optional budget or subscription intent")
    source: Optional[str] = Field(None, description="Attribution source for this request")
    notes: Optional[str] = Field(None, description="Optional buyer context")

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "contact": "builder@example.com",
                    "target_url": "https://example.com/source",
                    "question": "Which claims can my agent cite from this source?",
                    "pack": "quick",
                    "use_case": "RAG evaluation",
                    "budget_usdc": "10/month",
                    "source": "proof-pack-request",
                    "notes": "Need a first report before setting up x402 payment.",
                }
            ]
        }
    }


class ProofBundleLeadRequest(BaseModel):
    contact: str = Field(..., description="Email, Telegram, X handle, or other reply path")
    target_urls: list[str] = Field(..., description="Public source URLs to bundle into one evidence package; bare domains are prefixed with https")
    question: Optional[str] = Field(None, description="Bundle-level evidence objective")
    bundle: str = Field(DEFAULT_PROOF_BUNDLE, description="Bundle level: scout, builder, or audit")
    use_case: Optional[str] = Field(None, description="How the buyer plans to use the bundle")
    budget_usdc: Optional[str] = Field(None, description="Optional budget or subscription intent")
    source: Optional[str] = Field(None, description="Attribution source for this request")
    notes: Optional[str] = Field(None, description="Optional buyer context")

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "contact": "builder@example.com",
                    "target_urls": [
                        "https://www.iana.org/domains/reserved",
                        "https://example.com",
                        "https://example.org",
                    ],
                    "question": "Which claims can our agent safely cite across these sources?",
                    "bundle": DEFAULT_PROOF_BUNDLE,
                    "use_case": "Agent launch due diligence",
                    "budget_usdc": "20/month",
                    "source": "proof-bundle-request",
                    "notes": "Need a multi-source evidence pack before wiring recurring calls.",
                }
            ]
        }
    }


class ContactRequest(BaseModel):
    name: Optional[str] = Field(None, description="Sender name")
    email: str = Field(..., description="Reply email address")
    company: Optional[str] = Field(None, description="Optional company, team, or project name")
    use_case: Optional[str] = Field(None, description="What the sender wants to use AxonGate for")
    message: str = Field(..., description="Question, partnership note, support request, or custom report request")
    source: Optional[str] = Field(None, description="Attribution source for this inquiry")

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "name": "Agent Builder",
                    "email": "builder@example.com",
                    "company": "Example Labs",
                    "use_case": "Agent citation checks before RAG ingestion",
                    "message": "We want to evaluate 50 public sources per month and need a reliable evidence report workflow.",
                    "source": "contact-page",
                }
            ]
        }
    }


class OperatorLeadStatusUpdate(BaseModel):
    status: str = Field(..., description="Pipeline status: new, contacted, paid, fulfilled, or lost")
    note: Optional[str] = Field(None, description="Private operator note for the status history")
    fulfillment_url: Optional[str] = Field(None, description="Optional delivery URL for paid or fulfilled work")
    delivery_note: Optional[str] = Field(None, description="Optional private fulfillment note")


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
        "proof_pack_sample_hits": values.get("discovery_proof_pack_sample_hits_total", 0),
        "proof_pack_previews": values.get("proof_pack_previews_total", 0),
        "proof_pack_preview_cache_hits": values.get("proof_pack_preview_cache_hits_total", 0),
        "proof_pack_quotes": values.get("proof_pack_quotes_total", 0),
        "proof_pack_leads": values.get("proof_pack_leads_total", 0),
        "contact_form_submits": values.get("contact_form_submits_total", 0),
        "proof_bundle_quotes": values.get("proof_bundle_quotes_total", 0),
        "proof_bundle_leads": values.get("proof_bundle_leads_total", 0),
        "proof_bundle_payment_clicks": values.get("proof_bundle_payment_clicks_total", 0),
        "proof_bundle_paid": values.get("proof_bundle_paid_total", 0),
        "proof_bundle_fulfilled": values.get("proof_bundle_fulfilled_total", 0),
        "proof_bundle_delivery_requests": values.get("proof_bundle_delivery_requests_total", 0),
        "proof_bundle_recovery_requests": values.get("proof_bundle_recovery_requests_total", 0),
        "proof_bundle_auto_fulfillment_success": values.get("proof_bundle_auto_fulfillment_success_total", 0),
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
            "bundle_lead_per_quote": conversion_rate(values.get("proof_bundle_leads_total", 0), values.get("proof_bundle_quotes_total", 0)),
            "bundle_payment_click_per_quote": conversion_rate(
                values.get("proof_bundle_payment_clicks_total", 0),
                values.get("proof_bundle_quotes_total", 0),
            ),
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


def proof_bundle_names() -> str:
    return ", ".join(PROOF_BUNDLE_PRICING_USDC.keys())


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


def normalize_proof_bundle(bundle: Optional[str]) -> str:
    normalized = (bundle or DEFAULT_PROOF_BUNDLE).strip().lower()
    if normalized not in PROOF_BUNDLE_PRICING_USDC:
        raise PaymentValidationError(f"Unsupported Proof Bundle. Use {proof_bundle_names()}.")
    return normalized


def is_cache_only_tier(tier: str) -> bool:
    return normalize_tier(tier) in CACHE_ONLY_TIERS


def usdc_units(amount: Decimal) -> int:
    return int(amount * (Decimal(10) ** USDC_DECIMALS))


def price_for_tier(tier: Optional[str]) -> Decimal:
    return TIER_PRICING_USDC[normalize_tier(tier)]


def price_for_proof_pack(pack: Optional[str]) -> Decimal:
    return PROOF_PACK_PRICING_USDC[normalize_proof_pack(pack)]


def price_for_proof_bundle(bundle: Optional[str]) -> Decimal:
    return PROOF_BUNDLE_PRICING_USDC[normalize_proof_bundle(bundle)]


def proof_bundle_source_limit(bundle: Optional[str]) -> int:
    return PROOF_BUNDLE_SOURCE_LIMITS[normalize_proof_bundle(bundle)]


def proof_bundle_payment_url(bundle: Optional[str]) -> str:
    return PROOF_BUNDLE_PAYMENT_URLS.get(normalize_proof_bundle(bundle), "")


def proof_pack_internal_tier(pack: Optional[str]) -> str:
    return PROOF_PACK_INTERNAL_TIERS[normalize_proof_pack(pack)]


def proof_pack_cache_policy(pack: str) -> str:
    normalized_pack = normalize_proof_pack(pack)
    if normalized_pack == "quick":
        return "cache-friendly source read with deterministic fallback"
    if normalized_pack == "deep":
        return "deep evidence pack with short-cache source material and fresh-by-default refresh"
    return "cache-aware source read with LLM-assisted evidence synthesis when configured"


def proof_bundle_policy(bundle: str) -> str:
    normalized_bundle = normalize_proof_bundle(bundle)
    limit = proof_bundle_source_limit(normalized_bundle)
    if normalized_bundle == "scout":
        return f"up to {limit} public sources for a lightweight multi-source evidence scout"
    if normalized_bundle == "audit":
        return f"up to {limit} public sources for deeper agent-launch or vendor due diligence"
    return f"up to {limit} public sources for a builder-ready evidence bundle"


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

IANA explains that certain domain names and top-level domains are reserved for
documentation, examples, testing, and local use.

The reserved second-level domain names include `example.com`, `example.net`,
and `example.org`. These names are intended for documentation and examples.

The reserved top-level domains include `example`, `invalid`, `localhost`, and
`test`. These labels are not meant to be treated as ordinary production domains.

The source supports claims about reserved naming conventions, documentation
fixtures, and examples. It does not establish live DNS ownership, customer
identity, or the current production behavior of any external service.
"""

STARTER_SAMPLE_TARGETS = {
    "https://www.iana.org/domains/reserved",
    "https://www.iana.org/domains/reserved/",
    "https://example.com",
    "https://example.com/",
}
PROOF_PACK_SAMPLE_TARGET_URL = "https://www.iana.org/domains/reserved"
PROOF_PACK_SAMPLE_QUESTION = "What does this source establish about reserved domains?"
PROOF_PACK_SAMPLE_PACK = "quick"


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
            "service": "AxonGate Context Endpoint",
            "description": "Supporting x402 endpoint for clean web context. AxonGate's flagship product is the source trust check at /proof-pack.",
            "tags": ["x402", "base", "usdc", "source-trust", "web-to-markdown", "rag", "context-broker"],
            "manifest": f"{PUBLIC_BASE_URL}/manifest.json",
            "agentCard": f"{PUBLIC_BASE_URL}/.well-known/agent.json",
            "docs": f"{PUBLIC_BASE_URL}/docs",
            "about": f"{PUBLIC_BASE_URL}/about",
            "faq": f"{PUBLIC_BASE_URL}/faq",
            "contact": f"{PUBLIC_BASE_URL}/contact",
            "contactApi": f"{PUBLIC_BASE_URL}/v1/contact",
            "operatorDashboard": f"{PUBLIC_BASE_URL}/operator",
            "quickstart": f"{PUBLIC_BASE_URL}/quickstart",
            "paidTestGuide": f"{PUBLIC_BASE_URL}/paid-test",
            "quote": f"{PUBLIC_BASE_URL}/quote",
            "quoteApi": f"{PUBLIC_BASE_URL}/v1/x402/quote",
            "proofPack": f"{PUBLIC_BASE_URL}/proof-pack",
            "proofPackSample": f"{PUBLIC_BASE_URL}/proof-pack/sample",
            "proofPackSampleApi": f"{PUBLIC_BASE_URL}/v1/proof-pack/sample",
            "proofPackPreview": f"{PUBLIC_BASE_URL}/proof-pack/preview",
            "proofPackPreviewApi": f"{PUBLIC_BASE_URL}/v1/proof-pack/preview",
            "proofPackQuote": f"{PUBLIC_BASE_URL}/proof-pack/quote",
            "proofPackQuoteApi": f"{PUBLIC_BASE_URL}/v1/proof-pack/quote",
            "proofPackRequest": f"{PUBLIC_BASE_URL}/proof-pack/request",
            "proofPackLeadApi": f"{PUBLIC_BASE_URL}/v1/proof-pack/leads",
            "proofPackEndpoint": f"{PUBLIC_BASE_URL}/v1/x402/proof-pack",
            "proofBundle": f"{PUBLIC_BASE_URL}/proof-pack/bundle",
            "proofBundleQuote": f"{PUBLIC_BASE_URL}/proof-pack/bundle/quote",
            "proofBundleCheckoutReview": f"{PUBLIC_BASE_URL}/proof-pack/bundle/checkout",
            "proofBundleCheckout": f"{PUBLIC_BASE_URL}/proof-pack/bundle/pay",
            "proofBundleDelivery": f"{PUBLIC_BASE_URL}/proof-pack/bundle/delivery",
            "proofBundleDeliveryApi": f"{PUBLIC_BASE_URL}/v1/proof-pack/bundle/delivery",
            "proofBundleRecovery": f"{PUBLIC_BASE_URL}/proof-pack/bundle/recover",
            "proofBundleRecoveryApi": f"{PUBLIC_BASE_URL}/v1/proof-pack/bundle/recover",
            "proofBundleQuoteApi": f"{PUBLIC_BASE_URL}/v1/proof-pack/bundle/quote",
            "proofBundleLeadApi": f"{PUBLIC_BASE_URL}/v1/proof-pack/bundle/leads",
            "operatorLeadStatusApi": f"{PUBLIC_BASE_URL}/v1/operator/leads/{{lead_id}}/status",
            "stripeWebhook": f"{PUBLIC_BASE_URL}/v1/stripe/webhook",
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
            "service": "AxonGate Source Trust Check",
            "description": "Paid evidence trust decision for agent builders: supported, weak, unsupported, and citation-ready source findings.",
            "tags": ["x402", "base", "usdc", "source-trust", "proof-pack", "citations", "agent-builders", "evidence"],
            "manifest": f"{PUBLIC_BASE_URL}/manifest.json",
            "docs": f"{PUBLIC_BASE_URL}/proof-pack",
            "about": f"{PUBLIC_BASE_URL}/about",
            "faq": f"{PUBLIC_BASE_URL}/faq",
            "contact": f"{PUBLIC_BASE_URL}/contact",
            "sample": f"{PUBLIC_BASE_URL}/proof-pack/sample",
            "sampleApi": f"{PUBLIC_BASE_URL}/v1/proof-pack/sample",
            "preview": f"{PUBLIC_BASE_URL}/proof-pack/preview",
            "previewApi": f"{PUBLIC_BASE_URL}/v1/proof-pack/preview",
            "quote": f"{PUBLIC_BASE_URL}/proof-pack/quote",
            "quoteApi": f"{PUBLIC_BASE_URL}/v1/proof-pack/quote",
            "request": f"{PUBLIC_BASE_URL}/proof-pack/request",
            "leadApi": f"{PUBLIC_BASE_URL}/v1/proof-pack/leads",
            "bundle": f"{PUBLIC_BASE_URL}/proof-pack/bundle",
            "bundleQuote": f"{PUBLIC_BASE_URL}/proof-pack/bundle/quote",
            "bundleCheckoutReview": f"{PUBLIC_BASE_URL}/proof-pack/bundle/checkout",
            "bundleCheckout": f"{PUBLIC_BASE_URL}/proof-pack/bundle/pay",
            "bundleQuoteApi": f"{PUBLIC_BASE_URL}/v1/proof-pack/bundle/quote",
            "bundleLeadApi": f"{PUBLIC_BASE_URL}/v1/proof-pack/bundle/leads",
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
            "bundlePricing": {
                bundle: {
                    "amount": str(usdc_units(price)),
                    "price": f"${price}",
                    "currency": "USDC",
                    "sourceLimit": proof_bundle_source_limit(bundle),
                    "policy": proof_bundle_policy(bundle),
                }
                for bundle, price in PROOF_BUNDLE_PRICING_USDC.items()
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


def build_proof_bundle_resource() -> dict[str, Any]:
    """Build the unpaid Proof Bundle resource object used by discovery endpoints."""
    return {
        "resource": f"{PUBLIC_BASE_URL}/v1/proof-pack/bundle/quote",
        "type": "http",
        "x402Version": 2,
        "method": "GET",
        "accepts": [],
        "lastUpdated": int(time.time()),
        "metadata": {
            "provider": "AxonGate",
            "basename": "axongate.base.eth",
            "category": "evidence-reports",
            "service": "AxonGate Evidence Bundles",
            "description": "No-spend quote, tracked checkout, and delivery pipeline for multi-source claim support checks aimed at agent builders.",
            "tags": ["source-trust", "proof-bundle", "proof-pack", "citations", "agent-builders", "evidence", "lead-capture", "checkout"],
            "docs": f"{PUBLIC_BASE_URL}/proof-pack/bundle",
            "contact": f"{PUBLIC_BASE_URL}/contact",
            "quote": f"{PUBLIC_BASE_URL}/proof-pack/bundle/quote",
            "checkoutReview": f"{PUBLIC_BASE_URL}/proof-pack/bundle/checkout",
            "checkout": f"{PUBLIC_BASE_URL}/proof-pack/bundle/pay",
            "delivery": f"{PUBLIC_BASE_URL}/proof-pack/bundle/delivery",
            "recovery": f"{PUBLIC_BASE_URL}/proof-pack/bundle/recover",
            "quoteApi": f"{PUBLIC_BASE_URL}/v1/proof-pack/bundle/quote",
            "leadApi": f"{PUBLIC_BASE_URL}/v1/proof-pack/bundle/leads",
            "deliveryApi": f"{PUBLIC_BASE_URL}/v1/proof-pack/bundle/delivery",
            "recoveryApi": f"{PUBLIC_BASE_URL}/v1/proof-pack/bundle/recover",
            "operatorLeads": f"{PUBLIC_BASE_URL}/operator/leads",
            "operatorStatusApi": f"{PUBLIC_BASE_URL}/v1/operator/leads/{{lead_id}}/status",
            "defaultBundle": DEFAULT_PROOF_BUNDLE,
            "paymentLinkConfigured": any(bool(url) for url in PROOF_BUNDLE_PAYMENT_URLS.values()),
            "pipelineStatuses": list(PROOF_BUNDLE_LEAD_STATUSES),
            "pricing": {
                bundle: {
                    "amount": str(usdc_units(price)),
                    "price": f"${price}",
                    "currency": "USDC",
                    "sourceLimit": proof_bundle_source_limit(bundle),
                    "policy": proof_bundle_policy(bundle),
                }
                for bundle, price in PROOF_BUNDLE_PRICING_USDC.items()
            },
        },
        "inputSchema": {
            "type": "http",
            "method": "GET",
            "query": {
                "target_urls": "newline, comma, or space separated public URLs; bare domains are prefixed with https",
                "question": "optional evidence objective",
                "bundle": list(PROOF_BUNDLE_PRICING_USDC.keys()),
                "source": "optional attribution source",
            },
        },
        "outputSchema": {
            "type": "object",
            "required": ["status", "supplier_spend", "target_urls", "bundle", "amount_units", "next_steps"],
        },
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
            "description": "AxonGate context endpoint: paid clean Markdown extraction supporting the source trust layer.",
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
            "description": "AxonGate Source Trust Check: citation-backed decision on whether a source is safe to cite.",
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
    payload = build_payment_required_payload("Payment required to access AxonGate source trust and context endpoints")
    payload["metadata"] = {
        "provider": "AxonGate",
        "service": "AI Source Trust Layer",
        "description": "Checks whether public web evidence is safe for AI agents to cite or act on; clean context extraction is available as a supporting endpoint.",
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
        "proofPackSample": f"{PUBLIC_BASE_URL}/proof-pack/sample",
        "proofPackSampleApi": f"{PUBLIC_BASE_URL}/v1/proof-pack/sample",
        "proofPackPreview": f"{PUBLIC_BASE_URL}/proof-pack/preview",
        "proofPackPreviewApi": f"{PUBLIC_BASE_URL}/v1/proof-pack/preview",
        "proofPackQuote": f"{PUBLIC_BASE_URL}/proof-pack/quote",
        "proofPackQuoteApi": f"{PUBLIC_BASE_URL}/v1/proof-pack/quote",
        "proofPackRequest": f"{PUBLIC_BASE_URL}/proof-pack/request",
        "proofPackLeadApi": f"{PUBLIC_BASE_URL}/v1/proof-pack/leads",
        "proofPackEndpoint": f"{PUBLIC_BASE_URL}/v1/x402/proof-pack",
        "proofBundle": f"{PUBLIC_BASE_URL}/proof-pack/bundle",
        "proofBundleQuote": f"{PUBLIC_BASE_URL}/proof-pack/bundle/quote",
        "proofBundleCheckoutReview": f"{PUBLIC_BASE_URL}/proof-pack/bundle/checkout",
        "proofBundleCheckout": f"{PUBLIC_BASE_URL}/proof-pack/bundle/pay",
        "proofBundleDelivery": f"{PUBLIC_BASE_URL}/proof-pack/bundle/delivery",
        "proofBundleDeliveryApi": f"{PUBLIC_BASE_URL}/v1/proof-pack/bundle/delivery",
        "proofBundleRecovery": f"{PUBLIC_BASE_URL}/proof-pack/bundle/recover",
        "proofBundleRecoveryApi": f"{PUBLIC_BASE_URL}/v1/proof-pack/bundle/recover",
        "proofBundleQuoteApi": f"{PUBLIC_BASE_URL}/v1/proof-pack/bundle/quote",
        "proofBundleLeadApi": f"{PUBLIC_BASE_URL}/v1/proof-pack/bundle/leads",
        "operatorLeadStatusApi": f"{PUBLIC_BASE_URL}/v1/operator/leads/{{lead_id}}/status",
        "stripeWebhook": f"{PUBLIC_BASE_URL}/v1/stripe/webhook",
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
            f"{PUBLIC_BASE_URL}/v1/proof-pack/bundle/quote",
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
        "proofBundles": {
            bundle: {
                "price": f"${price}",
                "amount": str(usdc_units(price)),
                "currency": "USDC",
                "sourceLimit": proof_bundle_source_limit(bundle),
                "policy": proof_bundle_policy(bundle),
            }
            for bundle, price in PROOF_BUNDLE_PRICING_USDC.items()
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
        "X-AxonGate-Proof-Pack-Sample": f"{PUBLIC_BASE_URL}/v1/proof-pack/sample",
        "X-AxonGate-Proof-Pack-Preview": f"{PUBLIC_BASE_URL}/proof-pack/preview",
        "X-AxonGate-Proof-Pack-Quote-Page": f"{PUBLIC_BASE_URL}/proof-pack/quote",
        "X-AxonGate-Proof-Pack-Quote": f"{PUBLIC_BASE_URL}/v1/proof-pack/quote",
        "X-AxonGate-Proof-Pack-Request": f"{PUBLIC_BASE_URL}/proof-pack/request",
        "X-AxonGate-Proof-Bundle": f"{PUBLIC_BASE_URL}/proof-pack/bundle",
        "X-AxonGate-Proof-Bundle-Quote": f"{PUBLIC_BASE_URL}/v1/proof-pack/bundle/quote",
        "X-AxonGate-Demo": f"{PUBLIC_BASE_URL}/demo",
        "X-AxonGate-Buyer-Example": f"{GITHUB_REPO_URL}/blob/main/examples/paid_buyer.mjs",
        "Link": (
            f'<{PUBLIC_BASE_URL}/docs>; rel="help", '
            f'<{PUBLIC_BASE_URL}/quickstart>; rel="quickstart", '
            f'<{PUBLIC_BASE_URL}/paid-test>; rel="payment-test", '
            f'<{PUBLIC_BASE_URL}/v1/x402/quote>; rel="quote", '
            f'<{PUBLIC_BASE_URL}/proof-pack>; rel="service", '
            f'<{PUBLIC_BASE_URL}/v1/proof-pack/sample>; rel="proof-pack-sample", '
            f'<{PUBLIC_BASE_URL}/proof-pack/preview>; rel="proof-pack-preview", '
            f'<{PUBLIC_BASE_URL}/proof-pack/quote>; rel="proof-pack-quote-page", '
            f'<{PUBLIC_BASE_URL}/v1/proof-pack/quote>; rel="proof-pack-quote", '
            f'<{PUBLIC_BASE_URL}/proof-pack/request>; rel="proof-pack-request", '
            f'<{PUBLIC_BASE_URL}/proof-pack/bundle>; rel="proof-bundle", '
            f'<{PUBLIC_BASE_URL}/v1/proof-pack/bundle/quote>; rel="proof-bundle-quote", '
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
            "sample": f"{PUBLIC_BASE_URL}/proof-pack/sample",
            "sample_api": f"{PUBLIC_BASE_URL}/v1/proof-pack/sample",
            "preview": f"{PUBLIC_BASE_URL}/proof-pack/preview",
            "preview_api": f"{PUBLIC_BASE_URL}/v1/proof-pack/preview",
            "quote_page": f"{PUBLIC_BASE_URL}/proof-pack/quote",
            "quote": f"{PUBLIC_BASE_URL}/v1/proof-pack/quote",
            "request": f"{PUBLIC_BASE_URL}/proof-pack/request",
            "lead_api": f"{PUBLIC_BASE_URL}/v1/proof-pack/leads",
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
        "proofPackSample": f"{PUBLIC_BASE_URL}/v1/proof-pack/sample",
        "proofPackPreview": f"{PUBLIC_BASE_URL}/proof-pack/preview",
        "proofPackPreviewApi": f"{PUBLIC_BASE_URL}/v1/proof-pack/preview",
        "proofPackRequest": f"{PUBLIC_BASE_URL}/proof-pack/request",
        "proofPackLeadApi": f"{PUBLIC_BASE_URL}/v1/proof-pack/leads",
        "proofBundle": f"{PUBLIC_BASE_URL}/proof-pack/bundle",
        "proofBundleQuote": f"{PUBLIC_BASE_URL}/proof-pack/bundle/quote",
        "proofBundleCheckoutReview": f"{PUBLIC_BASE_URL}/proof-pack/bundle/checkout",
        "proofBundleCheckout": f"{PUBLIC_BASE_URL}/proof-pack/bundle/pay",
        "proofBundleDelivery": f"{PUBLIC_BASE_URL}/proof-pack/bundle/delivery",
        "proofBundleDeliveryApi": f"{PUBLIC_BASE_URL}/v1/proof-pack/bundle/delivery",
        "proofBundleRecovery": f"{PUBLIC_BASE_URL}/proof-pack/bundle/recover",
        "proofBundleRecoveryApi": f"{PUBLIC_BASE_URL}/v1/proof-pack/bundle/recover",
        "proofBundleQuoteApi": f"{PUBLIC_BASE_URL}/v1/proof-pack/bundle/quote",
        "proofBundleLeadApi": f"{PUBLIC_BASE_URL}/v1/proof-pack/bundle/leads",
        "operatorLeadStatusApi": f"{PUBLIC_BASE_URL}/v1/operator/leads/{{lead_id}}/status",
        "stripeWebhook": f"{PUBLIC_BASE_URL}/v1/stripe/webhook",
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
        "proofBundles": {
            bundle: {
                "amount": str(usdc_units(price)),
                "price_usdc": float(price),
                "source_limit": proof_bundle_source_limit(bundle),
                "policy": proof_bundle_policy(bundle),
            }
            for bundle, price in PROOF_BUNDLE_PRICING_USDC.items()
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


def normalize_target_url_input(target_url: Any, *, default_scheme: str = "https") -> str:
    """Normalize buyer-entered public URLs, accepting bare domains like www.example.com."""
    cleaned = clean_lead_text(str(target_url or ""), 2048)
    if not cleaned:
        return ""
    if re.search(r"\s", cleaned):
        return cleaned
    parsed = urlparse(cleaned)
    if parsed.scheme:
        return cleaned
    if cleaned.startswith("//"):
        return f"{default_scheme}:{cleaned}"
    return f"{default_scheme}://{cleaned}"


def validate_target_url(target_url: str) -> str:
    normalized_target_url = normalize_target_url_input(target_url)
    parsed = urlparse(normalized_target_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        inc_metric("target_preflight_rejections_total")
        raise PaymentValidationError("target_url must be a public URL. You can enter https://example.com or www.example.com.")

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

    return normalized_target_url


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


def first_header_value(value: Optional[str]) -> str:
    return (value or "").split(",", 1)[0].strip()


def request_effective_proto(request: Request) -> str:
    forwarded = request.headers.get("forwarded", "")
    proto_match = re.search(r'proto="?([^;,"]+)"?', forwarded, flags=re.IGNORECASE)
    if proto_match:
        return proto_match.group(1).lower()
    forwarded_proto = first_header_value(request.headers.get("x-forwarded-proto"))
    return (forwarded_proto or request.url.scheme).lower()


def request_effective_host(request: Request) -> str:
    forwarded_host = first_header_value(request.headers.get("x-forwarded-host"))
    host = forwarded_host or request.headers.get("host") or request.url.netloc
    return host.rsplit("@", 1)[-1].split(":", 1)[0].lower()


def should_redirect_to_https(request: Request) -> bool:
    if not (FORCE_HTTPS_ENABLED and PUBLIC_BASE_SCHEME == "https" and PUBLIC_BASE_HOST):
        return False
    return request_effective_proto(request) == "http" and request_effective_host(request) == PUBLIC_BASE_HOST


def https_redirect_url(request: Request) -> str:
    host = (
        first_header_value(request.headers.get("x-forwarded-host"))
        or request.headers.get("host")
        or request.url.netloc
    )
    query = f"?{request.url.query}" if request.url.query else ""
    return f"https://{host}{request.url.path}{query}"


def add_security_headers(response: Response, request: Request) -> None:
    if not SECURITY_HEADERS_ENABLED:
        return
    if PUBLIC_BASE_SCHEME == "https" and request_effective_proto(request) == "https":
        response.headers.setdefault(
            "Strict-Transport-Security",
            f"max-age={HSTS_MAX_AGE_SECONDS}; includeSubDomains",
        )
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")


@app.middleware("http")
async def enforce_security_and_enrich_402_guidance(request: Request, call_next):
    if should_redirect_to_https(request):
        response = RedirectResponse(https_redirect_url(request), status_code=308)
    else:
        response = await call_next(request)
    add_security_headers(response, request)
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


def proof_pack_preview_page_url(target_url: str, question: str, pack: str, source: str) -> str:
    normalized_pack = normalize_proof_pack(pack)
    normalized_source = normalize_attribution_source(source)
    return (
        f"{PUBLIC_BASE_URL}/proof-pack/preview?"
        f"target_url={url_quote(target_url, safe='')}"
        f"&question={url_quote(question, safe='')}"
        f"&pack={url_quote(normalized_pack, safe='')}"
        f"&source={url_quote(normalized_source, safe='')}"
    )


def proof_pack_preview_api_url(target_url: str, question: str, pack: str, source: str) -> str:
    normalized_pack = normalize_proof_pack(pack)
    normalized_source = normalize_attribution_source(source)
    return (
        f"{PUBLIC_BASE_URL}/v1/proof-pack/preview?"
        f"target_url={url_quote(target_url, safe='')}"
        f"&question={url_quote(question, safe='')}"
        f"&pack={url_quote(normalized_pack, safe='')}"
        f"&source={url_quote(normalized_source, safe='')}"
    )


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
        "instructions": system_prompt,
        "input": user_prompt,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "proof_pack",
                "schema": PROOF_PACK_LLM_SCHEMA,
                "strict": False,
            }
        },
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
            print(f"[PROOF_PACK_LLM] Falling back after {fallback_reason}: {str(exc)[:240]}")
        except Exception as exc:
            fallback_reason = exc.__class__.__name__
            print(f"[PROOF_PACK_LLM] Falling back after {fallback_reason}: {str(exc)[:240]}")

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


def build_proof_pack_sample_response(source: str = "direct") -> dict[str, Any]:
    """Return a no-spend Proof Pack preview from embedded sample evidence."""
    normalized_source = normalize_attribution_source(source)
    citations = extract_proof_pack_evidence(
        STARTER_SAMPLE_MARKDOWN,
        PROOF_PACK_SAMPLE_TARGET_URL,
        PROOF_PACK_SAMPLE_PACK,
    )
    proof_content = deterministic_proof_pack(
        target_url=PROOF_PACK_SAMPLE_TARGET_URL,
        question=PROOF_PACK_SAMPLE_QUESTION,
        pack=PROOF_PACK_SAMPLE_PACK,
        markdown=STARTER_SAMPLE_MARKDOWN,
        citations=citations,
        cache_hit=True,
        fallback_reason="public_sample",
    )
    live_price = price_for_proof_pack(PROOF_PACK_SAMPLE_PACK)
    quote_url = (
        f"{PUBLIC_BASE_URL}/v1/proof-pack/quote?"
        f"target_url={url_quote(PROOF_PACK_SAMPLE_TARGET_URL, safe='')}"
        f"&question={url_quote(PROOF_PACK_SAMPLE_QUESTION, safe='')}"
        f"&pack={PROOF_PACK_SAMPLE_PACK}&source={normalized_source}"
    )
    paid_endpoint = (
        f"{PUBLIC_BASE_URL}/v1/x402/proof-pack?"
        f"pack={PROOF_PACK_SAMPLE_PACK}&source={normalized_source}"
    )

    return {
        "status": "sample",
        "supplier_spend": False,
        "target_url": PROOF_PACK_SAMPLE_TARGET_URL,
        "question": PROOF_PACK_SAMPLE_QUESTION,
        "pack": PROOF_PACK_SAMPLE_PACK,
        "answer": proof_content["answer"],
        "executive_summary": proof_content["executive_summary"],
        "confidence_score": proof_content["confidence_score"],
        "key_claims": proof_content["key_claims"],
        "citations": [
            {key: value for key, value in citation.items() if key != "fingerprint"}
            for citation in citations
        ],
        "risks": proof_content["risks"],
        "source_profile": {
            **proof_content["source_profile"],
            "sample": True,
            "source_material": "embedded_starter_sample",
        },
        "report_card": {
            "decision_label": "Supported for documentation and test-domain claims",
            "decision_summary": (
                "The sample source supports claims that IANA reserves specific domain names and labels for documentation, "
                "examples, testing, and local use. It does not prove current DNS ownership or live service behavior."
            ),
            "what_this_establishes": [
                "example.com, example.net, and example.org are reserved for documentation and examples.",
                "example, invalid, localhost, and test are reserved labels, not ordinary production domains.",
                "The source is appropriate for agent documentation, test fixtures, and citation-backed explanations about reserved domains.",
            ],
            "what_it_does_not_establish": [
                "It does not verify a customer's current DNS setup.",
                "It does not prove ownership of a domain or the behavior of a live external service.",
            ],
            "buyer_value": [
                "A plain-English evidence decision instead of raw page text.",
                "Claim-to-citation mapping that an operator or agent can inspect.",
                "Risks, limits, cache metadata, source hash, and export-ready JSON.",
            ],
        },
        "cache": {
            "hit": True,
            "sample": True,
            "source": "embedded_starter_sample",
        },
        "llm_used": False,
        "llm_model": None,
        "fallback_reason": "public_sample",
        "payment": {
            "mode": "sample-no-payment",
            "required_for_live": True,
            "network": "eip155:8453",
            "vault_address": load_vault_address(),
            "token_address": BASE_USDC_ADDRESS,
            "amount_usdc": 0.0,
            "live_pack_amount_usdc": float(live_price),
            "live_pack_amount_units": str(usdc_units(live_price)),
            "source": normalized_source,
        },
        "ueg_receipt": {
            "sample": True,
            "revenue_usdc": 0.0,
            "dynamic_gas_cost_usdc": 0.0,
            "jina_api_cost_usdc": 0.0,
            "projected_profit_usdc": 0.0,
            "minimum_margin_usdc": float(MIN_PROFIT_MARGIN_USDC),
        },
        "next_steps": {
            "sample_page": public_url("/proof-pack/sample"),
            "sample_api": public_url("/v1/proof-pack/sample"),
            "preview_page": proof_pack_preview_page_url(
                PROOF_PACK_SAMPLE_TARGET_URL,
                PROOF_PACK_SAMPLE_QUESTION,
                PROOF_PACK_SAMPLE_PACK,
                normalized_source,
            ),
            "preview_api": proof_pack_preview_api_url(
                PROOF_PACK_SAMPLE_TARGET_URL,
                PROOF_PACK_SAMPLE_QUESTION,
                PROOF_PACK_SAMPLE_PACK,
                normalized_source,
            ),
            "quote_page": public_url("/proof-pack/quote"),
            "quote_api": quote_url,
            "request_page": proof_pack_request_page_url(
                PROOF_PACK_SAMPLE_TARGET_URL,
                PROOF_PACK_SAMPLE_QUESTION,
                PROOF_PACK_SAMPLE_PACK,
                normalized_source,
            ),
            "probe_payment_terms": proof_pack_payment_probe_url(PROOF_PACK_SAMPLE_PACK, normalized_source),
            "paid_endpoint": paid_endpoint,
            "confirm_spend_usdc": str(live_price),
            "buyer_command": proof_pack_buyer_command(
                PROOF_PACK_SAMPLE_TARGET_URL,
                PROOF_PACK_SAMPLE_QUESTION,
                PROOF_PACK_SAMPLE_PACK,
                normalized_source,
            ),
        },
    }


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
            "proof_pack_sample": public_url("/proof-pack/sample"),
            "proof_pack_sample_api": public_url("/v1/proof-pack/sample"),
            "proof_pack_preview_page": proof_pack_preview_page_url(
                normalized_target,
                normalized_question,
                normalized_pack,
                normalized_source,
            ),
            "proof_pack_preview_api": proof_pack_preview_api_url(
                normalized_target,
                normalized_question,
                normalized_pack,
                normalized_source,
            ),
            "proof_pack_quote_page": public_url("/proof-pack/quote"),
            "proof_pack_request_page": proof_pack_request_page_url(
                normalized_target,
                normalized_question,
                normalized_pack,
                normalized_source,
            ),
        },
    }


async def build_proof_pack_preview(
    target_url: str,
    question: Optional[str] = None,
    pack: Optional[str] = None,
    source: str = "proof-pack-preview",
) -> dict[str, Any]:
    """Return a no-spend mini preview from cached material when available."""
    normalized_target = await assert_public_target_url(target_url)
    normalized_source = normalize_attribution_source(source)
    normalized_pack = normalize_proof_pack(pack or PROOF_PACK_SAMPLE_PACK)
    normalized_question = proof_pack_question(question)
    internal_tier = proof_pack_internal_tier(normalized_pack)

    markdown = await get_cache_candidate_for_tier(normalized_target, internal_tier, False)
    preview_kind = "cached_source" if markdown is not None else "cache_miss"
    if markdown is None:
        starter_markdown = starter_sample_markdown_for_target(normalized_target)
        if starter_markdown is not None:
            markdown = starter_markdown
            preview_kind = "starter_sample"

    price = price_for_proof_pack(normalized_pack)
    next_steps = {
        "preview_page": proof_pack_preview_page_url(
            normalized_target,
            normalized_question,
            normalized_pack,
            normalized_source,
        ),
        "preview_api": proof_pack_preview_api_url(
            normalized_target,
            normalized_question,
            normalized_pack,
            normalized_source,
        ),
        "quote_page": proof_pack_quote_page_url(
            normalized_target,
            normalized_question,
            normalized_pack,
            normalized_source,
        ),
        "quote_api": (
            f"{PUBLIC_BASE_URL}/v1/proof-pack/quote?"
            f"target_url={url_quote(normalized_target, safe='')}"
            f"&question={url_quote(normalized_question, safe='')}"
            f"&pack={url_quote(normalized_pack, safe='')}"
            f"&source={url_quote(normalized_source, safe='')}"
        ),
        "request_page": proof_pack_request_page_url(
            normalized_target,
            normalized_question,
            normalized_pack,
            normalized_source,
        ),
        "sample_page": public_url("/proof-pack/sample"),
        "sample_api": public_url("/v1/proof-pack/sample"),
        "probe_payment_terms": proof_pack_payment_probe_url(normalized_pack, normalized_source),
        "paid_endpoint": f"{PUBLIC_BASE_URL}/v1/x402/proof-pack?pack={normalized_pack}&source={normalized_source}",
        "confirm_spend_usdc": str(price),
        "buyer_command": proof_pack_buyer_command(
            normalized_target,
            normalized_question,
            normalized_pack,
            normalized_source,
        ),
    }

    if markdown is None:
        return {
            "status": "proof_pack_preview",
            "supplier_spend": False,
            "preview_available": False,
            "preview_kind": preview_kind,
            "target_url": normalized_target,
            "question": normalized_question,
            "source": normalized_source,
            "pack": normalized_pack,
            "answer": (
                "No cached mini preview is available for this target yet. "
                "A paid Proof Pack can fetch the source and return cited claims."
            ),
            "executive_summary": "Use the quote or request path to continue without surprise spend.",
            "confidence_score": 0.0,
            "key_claims": [],
            "citations": [],
            "risks": [
                "No source content was fetched for this free preview.",
                "Preview misses do not indicate source quality; they only mean AxonGate has no reusable cached material yet.",
            ],
            "source_profile": {
                "final_url": normalized_target,
                "content_sha256": None,
                "markdown_chars": 0,
                "citation_count": 0,
            },
            "cache": {
                "hit": False,
                "source": None,
            },
            "payment": {
                "required_for_full_report": True,
                "network": "eip155:8453",
                "vault_address": load_vault_address(),
                "token_address": BASE_USDC_ADDRESS,
                "full_pack_amount_usdc": float(price),
                "full_pack_amount_units": str(usdc_units(price)),
                "source": normalized_source,
            },
            "next_steps": next_steps,
        }

    inc_metric("proof_pack_preview_cache_hits_total")
    citations = extract_proof_pack_evidence(markdown, normalized_target, normalized_pack)[:3]
    proof_content = deterministic_proof_pack(
        target_url=normalized_target,
        question=normalized_question,
        pack=normalized_pack,
        markdown=markdown,
        citations=citations,
        cache_hit=True,
        fallback_reason=f"free_preview_{preview_kind}",
    )
    mini_claims = proof_content["key_claims"][:2]
    mini_citations = [
        {key: value for key, value in citation.items() if key != "fingerprint"}
        for citation in citations[:2]
    ]
    preview_risks = [
        "Free preview is limited to cached/source-sample material and only shows the first cited claims.",
        "Paid Proof Packs can refresh source material, run the selected pack depth, and return the full cited report.",
    ]
    if preview_kind == "starter_sample":
        preview_risks.append("This preview uses the embedded starter sample for the reserved-domains target.")

    return {
        "status": "proof_pack_preview",
        "supplier_spend": False,
        "preview_available": True,
        "preview_kind": preview_kind,
        "target_url": normalized_target,
        "question": normalized_question,
        "source": normalized_source,
        "pack": normalized_pack,
        "answer": clean_evidence_excerpt(proof_content["answer"], 520),
        "executive_summary": clean_evidence_excerpt(proof_content["executive_summary"], 420),
        "confidence_score": max(0.0, round(float(proof_content["confidence_score"]) - 0.08, 2)),
        "key_claims": mini_claims,
        "citations": mini_citations,
        "risks": preview_risks,
        "source_profile": {
            **proof_content["source_profile"],
            "preview_limited": True,
        },
        "cache": {
            "hit": True,
            "source": preview_kind,
        },
        "payment": {
            "required_for_full_report": True,
            "network": "eip155:8453",
            "vault_address": load_vault_address(),
            "token_address": BASE_USDC_ADDRESS,
            "full_pack_amount_usdc": float(price),
            "full_pack_amount_units": str(usdc_units(price)),
            "source": normalized_source,
        },
        "next_steps": next_steps,
    }


def clean_lead_text(value: Optional[str], max_chars: int = 400) -> str:
    """Normalize buyer-entered lead text without trying to interpret it."""
    cleaned = re.sub(r"\s+", " ", str(value or "").strip())
    return cleaned[:max_chars]


def normalize_recovery_email(value: Any) -> str:
    """Normalize checkout email for paid delivery recovery matching."""
    normalized = clean_lead_text(str(value or ""), 180).lower()
    return normalized if "@" in normalized else ""


def normalize_recovery_url(value: Any) -> str:
    """Normalize a public target URL for recovery matching without changing meaning."""
    cleaned = normalize_target_url_input(value)
    if not cleaned:
        return ""
    parsed = urlparse(cleaned)
    if parsed.scheme and parsed.netloc:
        path = parsed.path.rstrip("/")
        query = f"?{parsed.query}" if parsed.query else ""
        return f"{parsed.netloc.lower()}{path}{query}"
    return cleaned.rstrip("/").lower()


def parse_urlencoded_payload(body: bytes) -> dict[str, str]:
    """Parse a small HTML form body without requiring python-multipart."""
    try:
        decoded = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PaymentValidationError("Request form must be UTF-8 encoded.") from exc
    return {key: values[-1] if values else "" for key, values in parse_qs(decoded, keep_blank_values=True).items()}


def normalize_form_key(value: Any) -> str:
    """Turn third-party form labels into stable, low-cardinality keys."""
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def stripe_signature_values(signature_header: Optional[str]) -> tuple[int, list[str]]:
    """Extract Stripe timestamp and v1 signatures from a Stripe-Signature header."""
    if not signature_header:
        raise PaymentValidationError("Missing Stripe-Signature header.")

    timestamp: Optional[int] = None
    signatures: list[str] = []
    for item in signature_header.split(","):
        key, _, value = item.strip().partition("=")
        if key == "t":
            try:
                timestamp = int(value)
            except ValueError as exc:
                raise PaymentValidationError("Invalid Stripe webhook timestamp.") from exc
        elif key == "v1" and value:
            signatures.append(value)

    if timestamp is None or not signatures:
        raise PaymentValidationError("Stripe webhook signature is incomplete.")
    return timestamp, signatures


def verify_stripe_webhook_payload(raw_body: bytes, signature_header: Optional[str], secret: str) -> dict[str, Any]:
    """Verify a Stripe webhook event using the signed raw request body."""
    if not secret:
        raise PaymentValidationError("Stripe webhook secret is not configured.")

    timestamp, signatures = stripe_signature_values(signature_header)
    if STRIPE_WEBHOOK_TOLERANCE_SECONDS > 0 and abs(int(time.time()) - timestamp) > STRIPE_WEBHOOK_TOLERANCE_SECONDS:
        raise PaymentValidationError("Stripe webhook timestamp is outside the allowed tolerance.")

    signed_payload = f"{timestamp}.".encode("utf-8") + raw_body
    expected = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, candidate) for candidate in signatures):
        raise PaymentValidationError("Stripe webhook signature verification failed.")

    try:
        event = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PaymentValidationError("Stripe webhook body must be valid UTF-8 JSON.") from exc
    if not isinstance(event, dict):
        raise PaymentValidationError("Stripe webhook body must be a JSON object.")
    return event


async def mark_stripe_event_processed(event_id: str) -> bool:
    """Return True only the first time a Stripe event ID is observed."""
    normalized = str(event_id or "").strip()
    if not normalized:
        return False

    if redis_client and METRICS_PERSISTENCE_ENABLED:
        try:
            added = await redis_client.sadd(STRIPE_EVENTS_REDIS_KEY, normalized)
            await redis_client.expire(STRIPE_EVENTS_REDIS_KEY, STRIPE_EVENTS_RETENTION_SECONDS)
            return int(added or 0) == 1
        except Exception as exc:
            print(f"[STRIPE_WEBHOOK] Redis event dedupe failed: {exc}")

    async with processed_stripe_events_lock:
        if normalized in processed_stripe_events:
            return False
        processed_stripe_events.add(normalized)
        if len(processed_stripe_events) > PROOF_PACK_LEADS_MEMORY_MAX * 10:
            for old_event_id in list(processed_stripe_events)[:PROOF_PACK_LEADS_MEMORY_MAX]:
                processed_stripe_events.discard(old_event_id)
        return True


async def stripe_event_already_processed(event_id: str) -> bool:
    """Check whether a Stripe event was already handled without mutating state."""
    normalized = str(event_id or "").strip()
    if not normalized:
        return False

    if redis_client and METRICS_PERSISTENCE_ENABLED:
        try:
            return bool(await redis_client.sismember(STRIPE_EVENTS_REDIS_KEY, normalized))
        except Exception as exc:
            print(f"[STRIPE_WEBHOOK] Redis event lookup failed: {exc}")

    async with processed_stripe_events_lock:
        return normalized in processed_stripe_events


def stripe_event_object(event: dict[str, Any]) -> dict[str, Any]:
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    obj = data.get("object") if isinstance(data.get("object"), dict) else {}
    if not obj:
        raise PaymentValidationError("Stripe webhook event is missing data.object.")
    return obj


def stripe_session_metadata(session: dict[str, Any]) -> dict[str, str]:
    metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    return {normalize_form_key(key): str(value) for key, value in metadata.items() if value is not None}


def stripe_custom_field_value(field: dict[str, Any]) -> str:
    field_type = str(field.get("type") or "").strip().lower()
    typed_value = field.get(field_type) if field_type else None
    if isinstance(typed_value, dict):
        value = typed_value.get("value")
        if value is not None:
            return str(value)
    for fallback_type in ("text", "numeric", "dropdown"):
        fallback_value = field.get(fallback_type)
        if isinstance(fallback_value, dict) and fallback_value.get("value") is not None:
            return str(fallback_value.get("value"))
    return ""


def stripe_custom_field_label(field: dict[str, Any]) -> str:
    label = field.get("label")
    if isinstance(label, dict):
        return str(label.get("custom") or label.get("type") or "")
    return str(label or "")


def stripe_checkout_custom_fields(session: dict[str, Any]) -> dict[str, str]:
    """Return Stripe Checkout custom fields keyed by both configured key and display label."""
    fields: dict[str, str] = {}
    raw_fields = session.get("custom_fields") if isinstance(session.get("custom_fields"), list) else []
    for raw_field in raw_fields:
        if not isinstance(raw_field, dict):
            continue
        value = clean_lead_text(stripe_custom_field_value(raw_field), 4000)
        if not value:
            continue
        keys = {
            normalize_form_key(raw_field.get("key")),
            normalize_form_key(stripe_custom_field_label(raw_field)),
        }
        label_key = normalize_form_key(stripe_custom_field_label(raw_field))
        if "target" in label_key and "url" in label_key:
            keys.add("target_urls")
        if "question" in label_key or "claim" in label_key or "verify" in label_key:
            keys.add("question")
        for key in keys:
            if key:
                fields[key] = value
    return fields


def stripe_session_amount_cents(session: dict[str, Any]) -> int:
    try:
        return int(session.get("amount_total") or session.get("amount_subtotal") or 0)
    except (TypeError, ValueError):
        return 0


def infer_stripe_bundle(session: dict[str, Any], metadata: dict[str, str], custom_fields: dict[str, str]) -> tuple[str, str]:
    """Infer the purchased bundle from metadata first, then unique configured prices."""
    for key in ("bundle", "axon_bundle", "proof_bundle", "pack"):
        candidate = metadata.get(key) or custom_fields.get(key)
        if candidate:
            try:
                return normalize_proof_bundle(candidate), f"metadata:{key}"
            except PaymentValidationError:
                pass

    currency = str(session.get("currency") or "").strip().lower()
    amount_cents = stripe_session_amount_cents(session)
    if currency in {"usd", "usdc"} and amount_cents > 0:
        matches = [
            bundle
            for bundle, price in PROOF_BUNDLE_PRICING_USDC.items()
            if int((price * Decimal("100")).to_integral_value()) == amount_cents
        ]
        if len(matches) == 1:
            return matches[0], "amount_total"

    return DEFAULT_PROOF_BUNDLE, "default"


def stripe_session_contact(session: dict[str, Any]) -> tuple[str, str, str]:
    details = session.get("customer_details") if isinstance(session.get("customer_details"), dict) else {}
    email = clean_lead_text(str(details.get("email") or session.get("customer_email") or ""), 180)
    name = clean_lead_text(str(details.get("name") or ""), 180)
    if email and name:
        return email, email, name
    if email:
        return email, email, name
    if name:
        return name, email, name
    customer = clean_lead_text(str(session.get("customer") or ""), 180)
    session_id = clean_lead_text(str(session.get("id") or ""), 180)
    return customer or f"stripe-session:{session_id}", email, name


def stripe_checkout_paid(event_type: str, session: dict[str, Any]) -> bool:
    payment_status = str(session.get("payment_status") or "").strip().lower()
    if event_type == "checkout.session.async_payment_succeeded":
        return True
    return payment_status in {"paid", "no_payment_required"}


def proof_bundle_delivery_url(lead_id: str = "", session_id: str = "") -> str:
    if session_id:
        return f"{PUBLIC_BASE_URL}/proof-pack/bundle/delivery?session_id={url_quote(session_id, safe='')}"
    return f"{PUBLIC_BASE_URL}/proof-pack/bundle/delivery?lead_id={url_quote(lead_id, safe='')}"


def proof_pack_for_bundle(bundle: str) -> str:
    normalized_bundle = normalize_proof_bundle(bundle)
    if normalized_bundle == "scout":
        return "quick"
    if normalized_bundle == "audit":
        return "deep"
    return DEFAULT_PROOF_PACK


async def find_stored_proof_pack_lead(lead_id: str) -> Optional[dict[str, Any]]:
    normalized_id = str(lead_id or "").strip()
    if not normalized_id:
        return None
    return next(
        (lead for lead in await durable_proof_pack_leads(PROOF_PACK_LEADS_MEMORY_MAX) if str(lead.get("id") or "") == normalized_id),
        None,
    )


async def find_stored_proof_bundle_lead_by_session(session_id: str) -> Optional[dict[str, Any]]:
    normalized_session_id = str(session_id or "").strip()
    if not normalized_session_id:
        return None
    leads = await durable_proof_pack_leads(PROOF_PACK_LEADS_MEMORY_MAX)
    return next(
        (
            lead
            for lead in leads
            if str((lead.get("stripe") or {}).get("session_id") or "") == normalized_session_id
            and str(lead.get("product") or "") == "proof_bundle"
        ),
        None,
    )


def proof_bundle_lead_recovery_emails(lead: dict[str, Any]) -> set[str]:
    """Return normalized buyer emails that may identify a paid bundle lead."""
    stripe = lead.get("stripe") if isinstance(lead.get("stripe"), dict) else {}
    candidates = {
        lead.get("contact"),
        stripe.get("customer_email"),
        ((stripe.get("customer_details") or {}).get("email") if isinstance(stripe.get("customer_details"), dict) else ""),
    }
    return {normalized for item in candidates if (normalized := normalize_recovery_email(item))}


def proof_bundle_lead_recovery_targets(lead: dict[str, Any]) -> set[str]:
    """Return normalized target URLs that can be used to recover a paid bundle."""
    targets: set[str] = set()
    for target in lead.get("target_urls") or []:
        normalized = normalize_recovery_url(target)
        if normalized:
            targets.add(normalized)
    target_url = normalize_recovery_url(lead.get("target_url"))
    if target_url:
        targets.add(target_url)
    return targets


def is_pending_stripe_bundle_recovery_candidate(lead: dict[str, Any]) -> bool:
    """Return true for a paid Stripe bundle that still needs customer recovery."""
    if str(lead.get("product") or "") != "proof_bundle":
        return False
    if normalize_lead_status(lead.get("status") or "new") != "paid":
        return False
    if isinstance(lead.get("proof_bundle_report"), dict):
        return False
    return isinstance(lead.get("stripe"), dict)


async def find_stored_proof_bundle_lead_for_recovery(email: str, target_url: str) -> Optional[dict[str, Any]]:
    """Find a paid Proof Bundle lead by checkout email and one submitted source URL."""
    normalized_email = normalize_recovery_email(email)
    normalized_target = normalize_recovery_url(target_url)
    if not normalized_target:
        return None

    leads = await durable_proof_pack_leads(PROOF_PACK_LEADS_MEMORY_MAX)
    paid_target_matches = [
        lead
        for lead in leads
        if str(lead.get("product") or "") == "proof_bundle"
        and normalize_lead_status(lead.get("status") or "new") in {"paid", "fulfilled"}
        and normalized_target in proof_bundle_lead_recovery_targets(lead)
    ]
    if normalized_email:
        exact_match = next(
            (lead for lead in paid_target_matches if normalized_email in proof_bundle_lead_recovery_emails(lead)),
            None,
        )
        if exact_match:
            return exact_match

    if len(paid_target_matches) == 1:
        return paid_target_matches[0]

    no_email_matches = [lead for lead in paid_target_matches if not proof_bundle_lead_recovery_emails(lead)]
    if len(no_email_matches) == 1:
        return no_email_matches[0]

    single_pending_stripe_match = [
        lead
        for lead in leads
        if is_pending_stripe_bundle_recovery_candidate(lead)
    ]
    if len(single_pending_stripe_match) == 1:
        return single_pending_stripe_match[0]
    return None


async def apply_recovery_target_override(lead: dict[str, Any], target_url: str) -> dict[str, Any]:
    """Use the customer-submitted recovery URL when Stripe stored malformed target data."""
    if not is_pending_stripe_bundle_recovery_candidate(lead):
        return lead
    normalized_target = normalize_recovery_url(target_url)
    if not normalized_target or normalized_target in proof_bundle_lead_recovery_targets(lead):
        return lead
    try:
        safe_target = validate_target_url(clean_lead_text(target_url, 2048))
    except PaymentValidationError:
        return lead
    updated = await update_stored_proof_pack_lead(
        str(lead.get("id") or ""),
        {
            "target_url": safe_target,
            "target_urls": [safe_target],
            "target_urls_raw": safe_target,
            "target_count": 1,
            "delivery_note": "Payment recovered with the customer-submitted target URL.",
        },
    )
    return updated or lead


async def build_stripe_proof_bundle_lead(event: dict[str, Any], session: dict[str, Any]) -> dict[str, Any]:
    """Convert a paid Stripe Checkout Session into AxonGate's operator lead shape."""
    metadata = stripe_session_metadata(session)
    custom_fields = stripe_checkout_custom_fields(session)
    bundle, bundle_source = infer_stripe_bundle(session, metadata, custom_fields)
    source = normalize_attribution_source(metadata.get("source") or metadata.get("utm_source") or "stripe")
    question = proof_bundle_question(
        custom_fields.get("question")
        or custom_fields.get("question_or_claim_to_verify")
        or custom_fields.get("claim_to_verify")
        or metadata.get("question")
    )
    target_text = (
        custom_fields.get("target_urls")
        or custom_fields.get("target_url")
        or custom_fields.get("urls")
        or metadata.get("target_urls")
        or metadata.get("target_url")
        or ""
    )
    raw_targets = split_bundle_target_urls(target_text)
    normalized_targets: list[str] = []
    quote: Optional[dict[str, Any]] = None
    validation_note = ""
    try:
        normalized_targets = await validate_proof_bundle_targets(raw_targets, bundle)
        quote = await build_proof_bundle_quote(normalized_targets, question, bundle, source)
    except PaymentValidationError as exc:
        validation_note = f" Target URL validation needs follow-up: {exc.detail}"
        normalized_targets = raw_targets[: proof_bundle_source_limit(bundle)]

    link_targets = [target for target in normalized_targets if str(target).startswith(("http://", "https://"))]
    if quote:
        request_page = quote["next_steps"]["request_page"]
        quote_page = quote["next_steps"]["quote_page"]
        quote_api = quote["next_steps"]["quote_api"]
        checkout_url = quote["next_steps"]["checkout_url"]
        payment_url = quote["next_steps"]["payment_url"]
        source_profiles = quote["source_profiles"]
    else:
        query_targets = link_targets
        request_page = proof_bundle_request_page_url(query_targets, question, bundle, source) if query_targets else (
            f"{PUBLIC_BASE_URL}/proof-pack/bundle?bundle={url_quote(bundle, safe='')}&source={url_quote(source, safe='')}"
        )
        quote_page = proof_bundle_quote_page_url(query_targets, question, bundle, source) if query_targets else ""
        quote_api = ""
        checkout_url = proof_bundle_checkout_url(query_targets, question, bundle, source) if query_targets else ""
        payment_url = checkout_url
        source_profiles = []

    session_id = clean_lead_text(str(session.get("id") or event.get("id") or ""), 120)
    lead_id = f"stripe_{stable_hash(session_id)[:12]}"
    delivery_url = proof_bundle_delivery_url(lead_id, session_id)
    contact, customer_email, customer_name = stripe_session_contact(session)
    price = price_for_proof_bundle(bundle)
    amount_cents = stripe_session_amount_cents(session)
    currency = clean_lead_text(str(session.get("currency") or "usd").lower(), 16)
    payment_link = clean_lead_text(str(session.get("payment_link") or ""), 120)
    payment_intent = clean_lead_text(str(session.get("payment_intent") or ""), 120)
    notes = clean_lead_text(
        (
            f"Stripe checkout payment received. Bundle inferred from {bundle_source}. "
            f"Session {session_id}; payment intent {payment_intent or 'n/a'}."
            f"{validation_note}"
        ),
        500,
    )
    created_at = int(time.time())
    target_urls = normalized_targets or raw_targets

    return {
        "id": lead_id,
        "created_at": created_at,
        "product": "proof_bundle",
        "contact": contact,
        "target_url": target_urls[0] if target_urls else "",
        "target_urls": target_urls,
        "target_urls_raw": target_text,
        "target_count": len(target_urls),
        "question": question,
        "bundle": bundle,
        "pack": bundle,
        "use_case": "Stripe checkout purchase",
        "budget_usdc": "",
        "notes": notes,
        "source": source,
        "price_usdc": float(price),
        "amount_units": str(usdc_units(price)),
        "request_page": request_page,
        "quote_page": quote_page,
        "quote_api": quote_api,
        "checkout_url": checkout_url,
        "payment_url": payment_url,
        "payment_link_configured": bool(proof_bundle_payment_url(bundle)),
        "paid_endpoint": f"{PUBLIC_BASE_URL}/v1/x402/proof-pack?pack=standard&source={source}",
        "buyer_command": "Stripe payment received. Fulfill this Proof Bundle from the operator inbox.",
        "fulfillment_url": delivery_url,
        "delivery_note": "Payment received. Delivery report generation is queued.",
        "source_profiles": source_profiles,
        "stripe": {
            "event_id": event.get("id"),
            "event_type": event.get("type"),
            "session_id": session_id,
            "payment_status": session.get("payment_status"),
            "payment_link": payment_link,
            "payment_intent": payment_intent,
            "amount_total": amount_cents,
            "currency": currency,
            "customer_email": customer_email,
            "customer_name": customer_name,
            "bundle_inference": bundle_source,
            "custom_fields": custom_fields,
        },
    }


async def fulfill_stripe_checkout_session(event: dict[str, Any], session: dict[str, Any]) -> dict[str, Any]:
    """Create or update a paid Proof Bundle lead from a verified Stripe session."""
    candidate = await build_stripe_proof_bundle_lead(event, session)
    existing = await find_stored_proof_pack_lead(candidate["id"])
    if existing:
        merged = dict(candidate)
        merged["created_at"] = existing.get("created_at") or candidate["created_at"]
        merged["status"] = existing.get("status") or "new"
        merged["status_history"] = existing.get("status_history")
        merged["fulfillment_url"] = existing.get("fulfillment_url")
        merged["delivery_note"] = existing.get("delivery_note")
        await update_stored_proof_pack_lead(candidate["id"], merged)
    else:
        candidate["storage_backend"] = await store_proof_pack_lead(candidate)
        inc_metric("proof_bundle_leads_total")
        inc_attribution("proof_bundle_leads", candidate["source"])

    updated = await update_proof_pack_lead_status(
        candidate["id"],
        "paid",
        note=f"Stripe {event.get('type')} verified for session {candidate['stripe']['session_id']}.",
        fulfillment_url=candidate.get("fulfillment_url"),
        delivery_note=candidate.get("delivery_note"),
    )
    return updated or candidate


async def generate_proof_bundle_report(lead: dict[str, Any]) -> dict[str, Any]:
    """Generate a multi-source report for a paid Proof Bundle lead."""
    normalized_bundle = normalize_proof_bundle(str(lead.get("bundle") or DEFAULT_PROOF_BUNDLE))
    pack = proof_pack_for_bundle(normalized_bundle)
    question = proof_bundle_question(lead.get("question"))
    target_urls = [
        str(target).strip()
        for target in (lead.get("target_urls") if isinstance(lead.get("target_urls"), list) else [])
        if str(target).strip()
    ]
    if not target_urls and lead.get("target_url"):
        target_urls = [str(lead.get("target_url"))]
    if not target_urls:
        raise PaymentValidationError("Paid Proof Bundle has no target URLs to deliver.")

    bounded_targets = target_urls[: proof_bundle_source_limit(normalized_bundle)]
    price = price_for_proof_bundle(normalized_bundle)
    profitability = await calculate_profitability_for_price(price, supplier_attempts=len(bounded_targets))
    if profitability.projected_profit_usdc <= MIN_PROFIT_MARGIN_USDC:
        inc_metric("ueg_rejections_total")
        raise PaymentValidationError("Dynamic UEG rejected Proof Bundle delivery; projected margin is too low.")

    source_reports: list[dict[str, Any]] = []
    combined_claims: list[dict[str, Any]] = []
    combined_citations: list[dict[str, Any]] = []
    risks: list[str] = []
    source_profiles: list[dict[str, Any]] = []
    cache_hits = 0
    failures = 0

    for index, raw_target in enumerate(bounded_targets, start=1):
        source_id = f"s{index}"
        try:
            target_url = await assert_public_target_url(raw_target)
            markdown, cache_hit = await get_clean_markdown(
                target_url,
                proof_pack_internal_tier(pack),
                force_refresh=pack == "deep",
            )
            if cache_hit:
                cache_hits += 1
            proof_content = await generate_proof_pack_content(
                target_url=target_url,
                question=question,
                pack=pack,
                markdown=markdown,
                cache_hit=cache_hit,
            )
            citations = []
            citation_id_map: dict[str, str] = {}
            for citation in proof_content["citations"]:
                public_citation = {key: value for key, value in citation.items() if key != "fingerprint"}
                original_id = str(public_citation.get("id") or f"c{len(citations) + 1}")
                bundle_citation_id = f"{source_id}-{original_id}"
                citation_id_map[original_id] = bundle_citation_id
                public_citation["id"] = bundle_citation_id
                public_citation["source_id"] = source_id
                citations.append(public_citation)
                combined_citations.append(public_citation)

            key_claims = []
            for claim in proof_content["key_claims"]:
                mapped_claim = {
                    **claim,
                    "source_id": source_id,
                    "target_url": target_url,
                    "citation_ids": [
                        citation_id_map.get(str(citation_id), str(citation_id))
                        for citation_id in claim.get("citation_ids", [])
                    ],
                }
                key_claims.append(mapped_claim)
                combined_claims.append(mapped_claim)

            source_report = {
                "source_id": source_id,
                "status": "success",
                "target_url": target_url,
                "answer": proof_content["answer"],
                "executive_summary": proof_content["executive_summary"],
                "confidence_score": proof_content["confidence_score"],
                "key_claims": key_claims,
                "citations": citations,
                "risks": proof_content["risks"],
                "source_profile": proof_content["source_profile"],
                "cache": {"hit": cache_hit},
                "llm_used": proof_content["llm_used"],
                "llm_model": proof_content["llm_model"],
                "fallback_reason": proof_content["fallback_reason"],
            }
            source_reports.append(source_report)
            source_profiles.append({"source_id": source_id, **proof_content["source_profile"]})
            risks.extend(proof_content["risks"])
        except Exception as exc:
            failures += 1
            source_reports.append(
                {
                    "source_id": source_id,
                    "status": "error",
                    "target_url": raw_target,
                    "error": str(getattr(exc, "detail", None) or exc),
                }
            )

    successful_reports = [report for report in source_reports if report.get("status") == "success"]
    if not successful_reports:
        raise PaymentValidationError("Proof Bundle delivery could not generate any source reports.")

    report = {
        "status": "success" if failures == 0 else "partial_success",
        "product": "proof_bundle",
        "lead_id": lead.get("id"),
        "generated_at": int(time.time()),
        "bundle": normalized_bundle,
        "pack_used": pack,
        "question": question,
        "target_count": len(bounded_targets),
        "successful_sources": len(successful_reports),
        "failed_sources": failures,
        "answer": (
            f"AxonGate generated {len(successful_reports)} cited source report"
            f"{'' if len(successful_reports) == 1 else 's'} for this {normalized_bundle} Proof Bundle."
        ),
        "executive_summary": clean_evidence_excerpt(
            " ".join(str(report.get("executive_summary") or "") for report in successful_reports),
            1200,
        ),
        "confidence_score": clamp_confidence(
            sum(float(report.get("confidence_score") or 0.0) for report in successful_reports)
            / max(1, len(successful_reports)),
            0.5,
        ),
        "key_claims": combined_claims[:12],
        "citations": combined_citations[:40],
        "risks": list(dict.fromkeys(risks))[:10],
        "source_reports": source_reports,
        "source_profiles": source_profiles,
        "cache": {"hits": cache_hits, "misses": max(0, len(successful_reports) - cache_hits)},
        "payment": {
            "mode": "stripe-checkout",
            "amount_usdc": float(price),
            "amount_units": str(usdc_units(price)),
            "source": lead.get("source"),
            "stripe": lead.get("stripe"),
        },
        "ueg_receipt": ueg_receipt_payload(profitability),
    }
    return report


async def generate_and_store_proof_bundle_delivery(lead_id: str) -> Optional[dict[str, Any]]:
    """Generate and persist the delivery report for a paid Proof Bundle lead."""
    lead = await find_stored_proof_pack_lead(lead_id)
    if not lead or str(lead.get("product") or "") != "proof_bundle":
        return None
    if isinstance(lead.get("proof_bundle_report"), dict):
        return lead
    if normalize_lead_status(lead.get("status") or "new") not in {"paid", "fulfilled"}:
        return lead

    inc_metric("proof_bundle_auto_fulfillment_started_total")
    try:
        report = await generate_proof_bundle_report(lead)
        session_id = str((lead.get("stripe") or {}).get("session_id") or "")
        fulfillment_url = proof_bundle_delivery_url(str(lead.get("id") or ""), session_id)
        await update_stored_proof_pack_lead(
            str(lead.get("id")),
            {
                "proof_bundle_report": report,
                "fulfillment_url": fulfillment_url,
                "delivery_note": "Proof Bundle report generated automatically.",
                "delivered_at": int(time.time()),
            },
        )
        updated = await update_proof_pack_lead_status(
            str(lead.get("id")),
            "fulfilled",
            note="Proof Bundle report generated automatically.",
            fulfillment_url=fulfillment_url,
            delivery_note="Proof Bundle report generated automatically.",
        )
        updated = updated or await find_stored_proof_pack_lead(str(lead.get("id")))
        if updated:
            emailed = await send_proof_bundle_delivery_email(updated)
            updated = emailed or updated
        inc_metric("proof_bundle_auto_fulfillment_success_total")
        return updated or await find_stored_proof_pack_lead(str(lead.get("id")))
    except Exception as exc:
        inc_metric("proof_bundle_auto_fulfillment_errors_total")
        await update_stored_proof_pack_lead(
            str(lead.get("id")),
            {
                "delivery_note": f"Automatic delivery failed: {str(getattr(exc, 'detail', None) or exc)[:260]}",
                "delivery_error_at": int(time.time()),
            },
        )
        print(f"[PROOF_BUNDLE_DELIVERY] Auto fulfillment failed for {lead.get('id')}: {exc}")
        return await find_stored_proof_pack_lead(str(lead.get("id")))


PROOF_BUNDLE_EMAIL_TEMPLATE_VERSION = "proof-bundle-delivery-v2"
REPORT_NAVIGATION_TERMS = (
    "toggle menu",
    "login",
    "join for free",
    "premium",
    "language",
    "your location",
    "best videos",
    "menu",
    "account",
    "sign in",
    "sign up",
    "cookie",
    "privacy",
    "terms",
    "url source",
    "markdown content",
    "published time",
    "cached snapshot",
    "learn more",
    "warning:",
)
REPORT_MARKDOWN_NOISE_RE = re.compile(r"!\[[^\]]*\]\([^)]+\)|\[[^\]]*\]\([^)]+\)|https?://\S+")


def proof_bundle_delivery_lookup_query(lead: dict[str, Any]) -> str:
    """Return the most stable delivery lookup query for action links."""
    stripe = lead.get("stripe") if isinstance(lead.get("stripe"), dict) else {}
    session_id = str(stripe.get("session_id") or "").strip()
    if session_id:
        return f"session_id={url_quote(session_id, safe='')}"
    return f"lead_id={url_quote(str(lead.get('id') or ''), safe='')}"


def proof_bundle_delivery_action_urls(lead: dict[str, Any], report: Optional[dict[str, Any]]) -> dict[str, str]:
    """Build customer actions that add value after a paid delivery."""
    query = proof_bundle_delivery_lookup_query(lead)
    target_urls = [
        str(target).strip()
        for target in (lead.get("target_urls") if isinstance(lead.get("target_urls"), list) else [])
        if str(target).strip()
    ]
    if not target_urls and lead.get("target_url"):
        target_urls = [str(lead.get("target_url"))]
    question = proof_bundle_question(lead.get("question"))
    current_bundle = normalize_proof_bundle(str(lead.get("bundle") or DEFAULT_PROOF_BUNDLE))
    upgrade_bundle = DEFAULT_PROOF_BUNDLE if current_bundle == "scout" else "audit"
    return {
        "open_report": f"{PUBLIC_BASE_URL}/proof-pack/bundle/delivery?{query}",
        "download_json": f"{PUBLIC_BASE_URL}/proof-pack/bundle/delivery.json?{query}",
        "download_pdf": f"{PUBLIC_BASE_URL}/proof-pack/bundle/delivery.pdf?{query}",
        "print": f"{PUBLIC_BASE_URL}/proof-pack/bundle/delivery/print?{query}",
        "refine": proof_bundle_request_page_url(target_urls, f"Refine this report: {question}", current_bundle, "delivery-refine")
        if target_urls
        else public_url("/proof-pack/bundle"),
        "analyze_another": public_url("/proof-pack/bundle"),
        "upgrade": proof_bundle_quote_page_url(target_urls, question, upgrade_bundle, "delivery-upgrade")
        if target_urls
        else public_url("/proof-pack/bundle"),
    }


def report_text_from_url(value: str) -> str:
    parsed = urlparse(value)
    host = parsed.hostname or value
    path = parsed.path.strip("/")
    return f"{host}/{path}" if path else host


def clean_report_text(value: Any, max_chars: int = 420) -> str:
    """Convert markdown-heavy extracted evidence into readable report text."""
    text = html.unescape(str(value or ""))
    replacements = {
        "â": "\"",
        "â": "\"",
        "â": "'",
        "â": "-",
        "â": "-",
        "â¦": "...",
    }
    for bad, good in replacements.items():
        text = text.replace(bad, good)
    text = re.sub(r"\bURL Source:\s*https?://[^\s]+", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\bMarkdown Content:\s*", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\bPublished Time:\s*[^.|\n]+(?:GMT|UTC)?", " ", text, flags=re.IGNORECASE)
    text = re.sub(
        r"\bWarning:\s*This is a cached snapshot[^.|\n]*\.?",
        " ",
        text,
        flags=re.IGNORECASE,
    )

    def replace_markdown_link(match: re.Match[str]) -> str:
        label = clean_evidence_excerpt(match.group(1), 160)
        url = match.group(2).split(" ", 1)[0].strip()
        if not label or label.lower().startswith(("http://", "https://")):
            return report_text_from_url(url)
        return label

    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", text)
    text = re.sub(r"\[([^\]]*)\]\((https?://[^)]+)\)", replace_markdown_link, text)
    text = re.sub(r"https?://[^\s)\]]+", lambda match: report_text_from_url(match.group(0)), text)
    text = re.sub(r"\bImage\s*:\s*[^.|\n]+", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"#+\s*", " ", text)
    text = re.sub(r"[\[\]()`*_]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" -:;,.")
    text = re.sub(r"\b([A-Za-z0-9.-]+\.[A-Za-z]{2,})(?:\s+\1\b)+", r"\1", text)
    return clean_evidence_excerpt(text, max_chars)


def report_noise_score(value: Any) -> float:
    """Estimate whether evidence is useful prose or mostly navigation/markdown noise."""
    raw = str(value or "")
    lower = raw.lower()
    marker_count = len(REPORT_MARKDOWN_NOISE_RE.findall(raw))
    nav_hits = sum(1 for term in REPORT_NAVIGATION_TERMS if term in lower)
    cleaned = clean_report_text(raw, 500)
    word_count = len(re.findall(r"[A-Za-z0-9]{3,}", cleaned))
    score = 0.0
    if marker_count >= 2:
        score += 0.35
    elif marker_count == 1:
        score += 0.15
    score += min(0.35, nav_hits * 0.08)
    if word_count < 8:
        score += 0.3
    elif word_count < 18:
        score += 0.15
    if len(cleaned) < 55:
        score += 0.15
    return round(min(1.0, score), 2)


def source_quality_level(score: float, substantive_count: int) -> str:
    if score >= 0.72 and substantive_count >= 2:
        return "strong"
    if score >= 0.48 and substantive_count >= 1:
        return "usable"
    return "low"


def source_quality_label(level: str) -> str:
    return {
        "strong": "Strong evidence",
        "usable": "Usable evidence",
        "low": "Low evidence quality",
    }.get(level, "Low evidence quality")


def citation_lookup(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    citations = report.get("citations") if isinstance(report.get("citations"), list) else []
    return {str(citation.get("id") or ""): citation for citation in citations if isinstance(citation, dict)}


def clean_claim_with_quality(claim: dict[str, Any], citations_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    citation_ids = [str(item) for item in claim.get("citation_ids", []) if str(item)]
    citation_scores = [
        max(0.0, 1.0 - report_noise_score(citations_by_id.get(citation_id, {}).get("excerpt", "")))
        for citation_id in citation_ids
    ]
    raw_claim = str(claim.get("claim") or "")
    cleaned = clean_report_text(raw_claim, 300)
    if not cleaned:
        cleaned = "Evidence excerpt was captured, but no readable claim could be extracted."
    own_score = max(0.0, 1.0 - report_noise_score(raw_claim))
    support_score = sum(citation_scores) / max(1, len(citation_scores))
    quality_score = clamp_confidence((own_score + support_score) / 2, 0.0)
    return {
        "claim": cleaned,
        "citation_ids": citation_ids,
        "confidence": clamp_confidence(claim.get("confidence"), 0.0),
        "source_id": claim.get("source_id"),
        "target_url": claim.get("target_url"),
        "quality_score": quality_score,
        "is_substantive": quality_score >= 0.48 and len(cleaned) >= 55,
    }


def proof_bundle_source_quality(source_report: dict[str, Any]) -> dict[str, Any]:
    citations = source_report.get("citations") if isinstance(source_report.get("citations"), list) else []
    claims = source_report.get("key_claims") if isinstance(source_report.get("key_claims"), list) else []
    source_citations_by_id = {
        str(citation.get("id") or ""): citation
        for citation in citations
        if isinstance(citation, dict)
    }
    clean_claims = [
        clean_claim_with_quality(claim, source_citations_by_id)
        for claim in claims
        if isinstance(claim, dict)
    ]
    clean_citations = [
        {
            "id": str(citation.get("id") or ""),
            "url": str(citation.get("url") or source_report.get("target_url") or ""),
            "excerpt": clean_report_text(citation.get("excerpt"), 420),
            "quality_score": clamp_confidence(1.0 - report_noise_score(citation.get("excerpt")), 0.0),
        }
        for citation in citations
        if isinstance(citation, dict)
    ]
    substantive_claims = [claim for claim in clean_claims if claim["is_substantive"]]
    if clean_citations:
        quality_score = sum(item["quality_score"] for item in clean_citations) / len(clean_citations)
    else:
        quality_score = 0.0
    if clean_claims:
        quality_score = (quality_score + sum(item["quality_score"] for item in clean_claims) / len(clean_claims)) / 2
    quality_score = clamp_confidence(quality_score, 0.0)
    level = source_quality_level(quality_score, len(substantive_claims))
    return {
        "source_id": source_report.get("source_id"),
        "target_url": source_report.get("target_url"),
        "status": source_report.get("status"),
        "quality_score": quality_score,
        "quality_level": level,
        "quality_label": source_quality_label(level),
        "summary": clean_report_text(source_report.get("executive_summary") or source_report.get("answer") or "", 620),
        "clean_claims": clean_claims,
        "clean_citations": clean_citations,
        "substantive_claim_count": len(substantive_claims),
        "citation_count": len(clean_citations),
        "cache_hit": bool((source_report.get("cache") or {}).get("hit")) if isinstance(source_report.get("cache"), dict) else False,
        "content_sha256": str((source_report.get("source_profile") or {}).get("content_sha256") or "")
        if isinstance(source_report.get("source_profile"), dict)
        else "",
        "risks": [
            clean_report_text(risk, 240)
            for risk in (source_report.get("risks") if isinstance(source_report.get("risks"), list) else [])
            if str(risk).strip()
        ],
    }


def proof_bundle_report_view_model(lead: dict[str, Any]) -> dict[str, Any]:
    """Build the polished, customer-facing Delivery v2 model from a stored report."""
    report = lead.get("proof_bundle_report") if isinstance(lead.get("proof_bundle_report"), dict) else {}
    if not report:
        return {
            "version": "delivery-v2",
            "quality_level": "processing",
            "quality_label": "Generating report",
            "decision_label": "Generating evidence decision",
            "decision_summary": "Payment received. AxonGate is still checking the submitted public sources before issuing a verdict.",
            "summary": "Payment received. AxonGate is generating your cited Proof Bundle report.",
            "what_this_establishes": [],
            "clean_claims": [],
            "clean_citations": [],
            "source_quality": [],
            "risks": [],
            "recommended_next_actions": [
                "Refresh the delivery page in a moment.",
                "Use the recovery page if the Stripe redirect was closed before delivery completed.",
            ],
            "report_deliverables": [
                "Evidence decision",
                "Citation-backed findings",
                "Risk notes",
                "PDF and JSON exports",
            ],
            "actions": proof_bundle_delivery_action_urls(lead, None),
            "copy_text": "",
        }

    sources = [
        proof_bundle_source_quality(source_report)
        for source_report in (report.get("source_reports") if isinstance(report.get("source_reports"), list) else [])
        if isinstance(source_report, dict)
    ]
    if not sources:
        source_report = {
            "source_id": "s1",
            "status": "success",
            "target_url": (report.get("target_url") or lead.get("target_url") or ""),
            "executive_summary": report.get("executive_summary") or report.get("answer") or "",
            "key_claims": report.get("key_claims") if isinstance(report.get("key_claims"), list) else [],
            "citations": report.get("citations") if isinstance(report.get("citations"), list) else [],
            "source_profile": {},
            "cache": {},
            "risks": report.get("risks") if isinstance(report.get("risks"), list) else [],
        }
        sources = [proof_bundle_source_quality(source_report)]

    clean_claims: list[dict[str, Any]] = []
    for source in sources:
        clean_claims.extend(source["clean_claims"])
    clean_claims = sorted(clean_claims, key=lambda item: item.get("quality_score", 0), reverse=True)
    clean_citations: list[dict[str, Any]] = []
    for source in sources:
        clean_citations.extend(source["clean_citations"])

    average_quality = (
        sum(source["quality_score"] for source in sources) / max(1, len(sources))
        if sources
        else 0.0
    )
    substantive_count = sum(source["substantive_claim_count"] for source in sources)
    quality_level = source_quality_level(average_quality, substantive_count)
    quality_label = source_quality_label(quality_level)
    if quality_level == "low":
        summary = (
            "AxonGate received payment and inspected the submitted public source material, but the extracted evidence is mostly "
            "navigation, boilerplate, or low-context text. Treat this as a weak evidence result rather than a substantive proof report."
        )
        decision_label = "Weak evidence - do not cite as proof"
        decision_summary = (
            "The source was reachable, but the useful public evidence is too thin or noisy to support the claim without better source material."
        )
        what_this_establishes = [
            "The submitted source was reachable and returned public text.",
            "The available public text did not establish a clean, substantive answer to the buyer question.",
            "A better result likely needs a more specific source URL, article page, documentation page, transcript, or public evidence page.",
        ]
        recommended_next_actions = [
            "Replace broad homepages with exact articles, docs, changelogs, public filings, or transcripts.",
            "Use this report as a rejection record before adding the source to an agent knowledge base.",
            "Download the JSON or PDF to keep the citation IDs, source hash, and risk notes together.",
        ]
    else:
        summary = clean_report_text(report.get("executive_summary") or report.get("answer") or "", 900)
        if not summary:
            summary = "AxonGate generated a cited Proof Bundle report from the submitted public sources."
        if quality_level == "strong":
            decision_label = "Cite-ready with review"
            decision_summary = (
                "The submitted source material contains substantive, citation-backed support. Use the cited findings with the listed risks."
            )
        else:
            decision_label = "Usable evidence - verify context"
            decision_summary = (
                "The submitted source material supports part of the question, but the report should be reviewed before a customer-facing agent cites it."
            )
        what_this_establishes = [
            claim["claim"]
            for claim in clean_claims
            if claim.get("is_substantive")
        ][:5]
        if not what_this_establishes:
            what_this_establishes = [summary]
        recommended_next_actions = [
            "Use the cited findings in prompts, RAG metadata, or customer-facing notes only with the citation IDs attached.",
            "Keep the source hash with your internal record so future checks can detect source drift.",
            "Download the PDF for review and the JSON for agent or workflow ingestion.",
        ]

    risks = [
        clean_report_text(risk, 260)
        for risk in (report.get("risks") if isinstance(report.get("risks"), list) else [])
        if str(risk).strip()
    ]
    if quality_level == "low":
        risks.insert(0, "Low evidence quality: extracted text is mostly boilerplate, navigation, or link-heavy markup.")
    if report.get("failed_sources"):
        risks.append(f"{report.get('failed_sources')} submitted source failed during delivery.")
    risks = list(dict.fromkeys(risk for risk in risks if risk))[:8]
    top_claims = clean_claims[:8] if quality_level != "low" else clean_claims[:3]
    copy_lines = [
        "AxonGate Proof Bundle Report",
        f"Decision: {decision_label}",
        f"Quality: {quality_label}",
        f"Summary: {summary}",
        "",
        "What this establishes:",
        *[f"- {item}" for item in what_this_establishes],
        "",
        "Recommended next actions:",
        *[f"- {item}" for item in recommended_next_actions],
    ]
    return {
        "version": "delivery-v2",
        "generated_at": report.get("generated_at"),
        "generated_at_label": lead_created_at_label(report.get("generated_at")),
        "bundle": report.get("bundle") or lead.get("bundle"),
        "question": report.get("question") or lead.get("question"),
        "quality_score": clamp_confidence(average_quality, 0.0),
        "quality_level": quality_level,
        "quality_label": quality_label,
        "decision_label": decision_label,
        "decision_summary": decision_summary,
        "confidence_score": clamp_confidence(report.get("confidence_score"), 0.0),
        "summary": summary,
        "what_this_establishes": what_this_establishes,
        "clean_claims": top_claims,
        "clean_citations": clean_citations[:20],
        "source_quality": sources,
        "risks": risks,
        "recommended_next_actions": recommended_next_actions,
        "report_deliverables": [
            "Evidence decision",
            "Claim-to-citation map",
            "Source quality audit",
            "Risks and gaps",
            "Reproducible source hashes",
            "PDF and JSON exports",
        ],
        "actions": proof_bundle_delivery_action_urls(lead, report),
        "copy_text": "\n".join(copy_lines),
    }


def build_proof_bundle_delivery_payload(lead: dict[str, Any]) -> dict[str, Any]:
    lead = lead_with_pipeline_defaults(lead)
    report = lead.get("proof_bundle_report") if isinstance(lead.get("proof_bundle_report"), dict) else None
    delivery_view = proof_bundle_report_view_model(lead)
    return {
        "status": "ready" if report else "processing",
        "lead_id": lead.get("id"),
        "product": "proof_bundle",
        "bundle": lead.get("bundle"),
        "lead_status": lead.get("status"),
        "target_count": lead.get("target_count"),
        "question": lead.get("question"),
        "delivery_note": lead.get("delivery_note"),
        "fulfillment_url": lead.get("fulfillment_url"),
        "report": report,
        "delivery": delivery_view,
    }


def proof_bundle_delivery_email_recipient(lead: dict[str, Any]) -> str:
    """Choose the best customer email for Proof Bundle report delivery."""
    stripe = lead.get("stripe") if isinstance(lead.get("stripe"), dict) else {}
    candidates = [
        stripe.get("customer_email"),
        lead.get("contact"),
    ]
    for candidate in candidates:
        normalized = normalize_recovery_email(candidate)
        if normalized:
            return normalized
    return ""


def proof_bundle_delivery_email_url(lead: dict[str, Any]) -> str:
    """Return the best stable customer-facing delivery URL for a bundle lead."""
    existing = clean_lead_text(str(lead.get("fulfillment_url") or ""), 600)
    if existing:
        return existing
    stripe = lead.get("stripe") if isinstance(lead.get("stripe"), dict) else {}
    return proof_bundle_delivery_url(str(lead.get("id") or ""), str(stripe.get("session_id") or ""))


def build_proof_bundle_delivery_email(lead: dict[str, Any]) -> dict[str, str]:
    """Build a concise report delivery email payload."""
    payload = build_proof_bundle_delivery_payload(lead)
    report = payload.get("report") if isinstance(payload.get("report"), dict) else {}
    delivery = payload.get("delivery") if isinstance(payload.get("delivery"), dict) else {}
    delivery_url = proof_bundle_delivery_email_url(lead)
    bundle = str(payload.get("bundle") or "proof").title()
    summary = clean_evidence_excerpt(str(delivery.get("summary") or report.get("answer") or "Your AxonGate report is ready."), 900)
    quality_label = str(delivery.get("quality_label") or "Evidence reviewed")
    quality_score = delivery.get("quality_score")
    decision_label = str(delivery.get("decision_label") or quality_label)
    decision_summary = clean_evidence_excerpt(str(delivery.get("decision_summary") or ""), 500)
    actions = delivery.get("actions") if isinstance(delivery.get("actions"), dict) else {}
    claims = delivery.get("clean_claims") if isinstance(delivery.get("clean_claims"), list) else []
    establishes = delivery.get("what_this_establishes") if isinstance(delivery.get("what_this_establishes"), list) else []
    claim_lines = [
        f"- {clean_evidence_excerpt(str(claim.get('claim') or ''), 240)}"
        for claim in claims[:5]
        if isinstance(claim, dict) and claim.get("claim")
    ]
    if not claim_lines:
        claim_lines = [
            f"- {clean_evidence_excerpt(str(item), 240)}"
            for item in establishes[:3]
            if str(item).strip()
        ]
    claim_text = "\n".join(claim_lines) if claim_lines else "- The report is ready to review."
    claim_html = "".join(f"<li>{html.escape(line[2:])}</li>" for line in claim_lines) or "<li>The report is ready to review.</li>"
    subject = f"Your AxonGate {bundle} Proof Bundle report is ready"
    text = f"""Your AxonGate Proof Bundle report is ready.

Open report:
{delivery_url}

Evidence decision:
{decision_label}
{decision_summary}

Evidence quality:
{quality_label}{f" ({quality_score})" if quality_score is not None else ""}

Summary:
{summary}

What this establishes:
{claim_text}

Downloads:
JSON: {actions.get("download_json") or ""}
PDF: {actions.get("download_pdf") or ""}

AxonGate
"""
    html_body = f"""<!doctype html>
<html>
<body style="font-family:Arial,sans-serif;line-height:1.55;color:#111827;margin:0;background:#f7f8fb">
  <div style="max-width:680px;margin:0 auto;padding:24px">
  <div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:10px;padding:22px">
  <h1 style="font-size:22px;margin:0 0 12px">Your AxonGate Proof Bundle report is ready</h1>
  <p style="margin:0 0 8px;color:#111827">Evidence decision: <strong>{html.escape(decision_label)}</strong></p>
  <p style="margin:0 0 16px;color:#374151">{html.escape(decision_summary or 'AxonGate separated supported findings from weak or noisy source text.')}</p>
  <p style="margin:0 0 16px;color:#374151">Evidence quality: <strong>{html.escape(quality_label)}</strong>{html.escape(f" ({quality_score})" if quality_score is not None else "")}</p>
  <p><a href="{html.escape(delivery_url, quote=True)}" style="display:inline-block;background:#0f766e;color:white;padding:10px 14px;border-radius:6px;text-decoration:none">Open report</a></p>
  <h2 style="font-size:16px;margin-top:22px">Summary</h2>
  <p style="color:#374151">{html.escape(summary)}</p>
  <h2 style="font-size:16px;margin-top:22px">What this establishes</h2>
  <ul>{claim_html}</ul>
  <p style="margin-top:22px">
    <a href="{html.escape(str(actions.get('download_json') or delivery_url), quote=True)}" style="color:#0f766e">Download JSON</a>
    &nbsp;|&nbsp;
    <a href="{html.escape(str(actions.get('download_pdf') or delivery_url), quote=True)}" style="color:#0f766e">Download PDF</a>
  </p>
  <p style="color:#6b7280;font-size:13px">AxonGate generated this report from public source URLs submitted at checkout. Open the report for source hashes, citations, risks, and next actions.</p>
  </div>
  </div>
</body>
</html>"""
    return {"subject": subject, "text": text, "html": html_body, "delivery_url": delivery_url}


async def send_resend_email(payload: dict[str, Any]) -> dict[str, Any]:
    """Send an email through Resend."""
    async with httpx.AsyncClient(timeout=RESEND_TIMEOUT_SECONDS) as client:
        response = await client.post(
            RESEND_API_URL,
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        response.raise_for_status()
        try:
            parsed = response.json()
        except json.JSONDecodeError:
            parsed = {}
        return parsed if isinstance(parsed, dict) else {}


def sanitize_email_delivery_error(value: Any, limit: int = 360) -> str:
    """Return a compact email-provider error without addresses or secrets."""
    text = clean_lead_text(str(value or ""), limit * 2)
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[email]", text)
    text = text.replace(RESEND_API_KEY, "[redacted]") if RESEND_API_KEY else text
    return text[:limit]


def describe_email_delivery_exception(exc: Exception) -> tuple[str, Optional[int]]:
    """Extract a useful, sanitized provider error from httpx exceptions."""
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if response is not None:
        detail = ""
        try:
            parsed = response.json()
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            parts = []
            for key in ("name", "message", "error", "code", "status"):
                value = parsed.get(key)
                if value:
                    parts.append(f"{key}={value}")
            detail = "; ".join(parts)
        if not detail:
            detail = getattr(response, "text", "") or str(response)
        prefix = f"HTTP {status_code}" if status_code else "HTTP error"
        return sanitize_email_delivery_error(f"{prefix}: {detail}"), status_code
    return sanitize_email_delivery_error(exc), status_code


def record_email_delivery_error(detail: str, status_code: Optional[int] = None) -> None:
    """Remember the latest provider error for operator diagnostics."""
    global email_delivery_last_error, email_delivery_last_error_at, email_delivery_last_status_code
    email_delivery_last_error = sanitize_email_delivery_error(detail)
    email_delivery_last_error_at = int(time.time())
    email_delivery_last_status_code = status_code


def clear_email_delivery_error() -> None:
    """Clear the last provider error after a confirmed successful send."""
    global email_delivery_last_error, email_delivery_last_error_at, email_delivery_last_status_code
    email_delivery_last_error = ""
    email_delivery_last_error_at = 0
    email_delivery_last_status_code = None


async def send_proof_bundle_delivery_email(lead: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Email a fulfilled Proof Bundle report link when delivery email is configured."""
    if not isinstance(lead.get("proof_bundle_report"), dict):
        return None
    if lead.get("delivery_email_sent_at") and lead.get("delivery_email_template_version") == PROOF_BUNDLE_EMAIL_TEMPLATE_VERSION:
        return lead
    if not EMAIL_DELIVERY_ENABLED:
        inc_metric("proof_bundle_delivery_email_disabled_total")
        return lead

    recipient = proof_bundle_delivery_email_recipient(lead)
    if not recipient:
        inc_metric("proof_bundle_delivery_email_missing_recipient_total")
        return lead
    if EMAIL_PROVIDER != "resend" or not RESEND_API_KEY or not EMAIL_FROM:
        detail = "Email delivery is enabled but Resend is not fully configured."
        inc_metric("proof_bundle_delivery_email_errors_total")
        record_email_delivery_error(detail)
        await update_stored_proof_pack_lead(
            str(lead.get("id") or ""),
            {"delivery_email_error": detail},
        )
        return lead

    inc_metric("proof_bundle_delivery_email_attempts_total")
    email_content = build_proof_bundle_delivery_email(lead)
    resend_payload: dict[str, Any] = {
        "from": EMAIL_FROM,
        "to": [recipient],
        "subject": email_content["subject"],
        "text": email_content["text"],
        "html": email_content["html"],
    }
    if EMAIL_REPLY_TO:
        resend_payload["reply_to"] = EMAIL_REPLY_TO

    try:
        result = await send_resend_email(resend_payload)
    except Exception as exc:
        detail, status_code = describe_email_delivery_exception(exc)
        inc_metric("proof_bundle_delivery_email_errors_total")
        record_email_delivery_error(detail, status_code)
        await update_stored_proof_pack_lead(
            str(lead.get("id") or ""),
            {"delivery_email_error": detail},
        )
        print(f"[PROOF_BUNDLE_EMAIL] Delivery email failed for {lead.get('id')}: {detail}")
        return await find_stored_proof_pack_lead(str(lead.get("id") or "")) or lead

    updates = {
        "delivery_email_sent_at": int(time.time()),
        "delivery_email_to": recipient,
        "delivery_email_provider": EMAIL_PROVIDER,
        "delivery_email_template_version": PROOF_BUNDLE_EMAIL_TEMPLATE_VERSION,
        "delivery_email_id": clean_lead_text(str(result.get("id") or ""), 180),
        "delivery_email_error": "",
    }
    inc_metric("proof_bundle_delivery_email_success_total")
    clear_email_delivery_error()
    return await update_stored_proof_pack_lead(str(lead.get("id") or ""), updates)


def delivery_quality_class(level: str) -> str:
    return {
        "strong": "quality-strong",
        "usable": "quality-usable",
        "low": "quality-low",
        "processing": "quality-processing",
    }.get(level, "quality-low")


def delivery_html_list(items: list[Any], empty: str) -> str:
    rows = [f"<li>{html.escape(clean_report_text(item, 320))}</li>" for item in items if str(item).strip()]
    return f"<ul>{''.join(rows)}</ul>" if rows else f"<p class=\"muted\">{html.escape(empty)}</p>"


def delivery_claim_cards(claims: list[dict[str, Any]]) -> str:
    if not claims:
        return '<p class="muted">No clean customer-facing claims were strong enough to show as primary findings.</p>'
    cards = []
    for claim in claims:
        citations = " ".join(f"<code>{html.escape(str(item))}</code>" for item in claim.get("citation_ids", [])[:4])
        confidence = clamp_confidence(claim.get("confidence"), 0.0)
        quality = clamp_confidence(claim.get("quality_score"), 0.0)
        cards.append(
            f"""
        <article class="claim-item">
          <p>{html.escape(str(claim.get('claim') or ''))}</p>
          <div class="mini-meta">
            <span>confidence {html.escape(str(confidence))}</span>
            <span>evidence {html.escape(str(quality))}</span>
            <span>{citations}</span>
          </div>
        </article>
            """
        )
    return "\n".join(cards)


def delivery_citation_cards(citations: list[dict[str, Any]]) -> str:
    if not citations:
        return '<p class="muted">No readable citations were extracted.</p>'
    cards = []
    for citation in citations[:20]:
        url = str(citation.get("url") or "")
        cards.append(
            f"""
        <article class="citation-item">
          <div class="citation-head">
            <code>{html.escape(str(citation.get('id') or ''))}</code>
            <a href="{html.escape(url, quote=True)}" rel="noopener noreferrer">{html.escape(report_text_from_url(url))}</a>
          </div>
          <p>{html.escape(str(citation.get('excerpt') or ''))}</p>
        </article>
            """
        )
    return "\n".join(cards)


def delivery_source_cards(sources: list[dict[str, Any]]) -> str:
    if not sources:
        return '<p class="muted">Source audit is not available yet.</p>'
    cards = []
    for source in sources:
        level = str(source.get("quality_level") or "low")
        url = str(source.get("target_url") or "")
        risks = source.get("risks") if isinstance(source.get("risks"), list) else []
        risks_html = delivery_html_list(risks[:3], "No source-specific risks were recorded.")
        cards.append(
            f"""
        <article class="source-item">
          <div class="source-top">
            <div>
              <h3>{html.escape(str(source.get('source_id') or 'Source'))}: {html.escape(report_text_from_url(url))}</h3>
              <p>{html.escape(str(source.get('summary') or 'No readable summary extracted.'))}</p>
            </div>
            <span class="quality-pill {delivery_quality_class(level)}">{html.escape(str(source.get('quality_label') or source_quality_label(level)))}</span>
          </div>
          <div class="mini-meta">
            <span>quality {html.escape(str(source.get('quality_score')))}</span>
            <span>{html.escape(str(source.get('citation_count')))} citations</span>
            <span>{'cache hit' if source.get('cache_hit') else 'fresh or uncached'}</span>
          </div>
          <details>
            <summary>Risks and source hash</summary>
            {risks_html}
            <p><code>{html.escape(str(source.get('content_sha256') or 'hash unavailable'))}</code></p>
          </details>
        </article>
            """
        )
    return "\n".join(cards)


def delivery_value_cards(delivery: dict[str, Any], report: dict[str, Any]) -> str:
    citations = delivery.get("clean_citations") if isinstance(delivery.get("clean_citations"), list) else []
    sources = delivery.get("source_quality") if isinstance(delivery.get("source_quality"), list) else []
    deliverables = delivery.get("report_deliverables") if isinstance(delivery.get("report_deliverables"), list) else []
    cards = [
        (
            "Decision",
            str(delivery.get("decision_label") or delivery.get("quality_label") or "Evidence reviewed"),
            str(delivery.get("decision_summary") or "AxonGate separates supported findings from weak or noisy source text."),
        ),
        (
            "Traceability",
            f"{len(citations)} cited excerpts",
            "Each finding points back to evidence IDs so a buyer or agent can inspect the source trail.",
        ),
        (
            "Source Audit",
            f"{len(sources)} sources scored",
            "The report calls out source quality, cache status, content hashes, and source-specific risks.",
        ),
        (
            "Exports",
            ", ".join(str(item) for item in deliverables[-2:]) or "PDF and JSON",
            "Use the PDF for review and the JSON for agent ingestion, logging, or workflow automation.",
        ),
    ]
    if report.get("failed_sources"):
        cards.append(
            (
                "Coverage",
                f"{report.get('failed_sources')} source failed",
                "Failed sources are shown as gaps instead of being silently ignored.",
            )
        )
    return "\n".join(
        f"""
        <article class="value-card">
          <span>{html.escape(label)}</span>
          <strong>{html.escape(value)}</strong>
          <p>{html.escape(description)}</p>
        </article>
        """
        for label, value, description in cards
    )


def delivery_json_script(value: Any) -> str:
    return json.dumps(str(value or ""), ensure_ascii=True).replace("</", "<\\/")


def build_proof_bundle_delivery_html(lead: Optional[dict[str, Any]], *, error: str = "", print_mode: bool = False) -> str:
    nav = site_nav_html("Proof Bundles")
    if not lead:
        content = f"""
    <section class="panel">
      <h1>Proof Bundle Delivery</h1>
      <p>{html.escape(error or "Delivery was not found yet. If payment just completed, refresh this page in a moment.")}</p>
      <a class="button" href="{html.escape(public_url('/proof-pack/bundle'))}">Proof Bundles</a>
    </section>
        """
    else:
        payload = build_proof_bundle_delivery_payload(lead)
        report = payload.get("report") if isinstance(payload.get("report"), dict) else None
        delivery = payload.get("delivery") if isinstance(payload.get("delivery"), dict) else {}
        if report:
            actions = delivery.get("actions") if isinstance(delivery.get("actions"), dict) else {}
            quality_level = str(delivery.get("quality_level") or "low")
            quality_class = delivery_quality_class(quality_level)
            establishes = delivery.get("what_this_establishes") if isinstance(delivery.get("what_this_establishes"), list) else []
            clean_claims = delivery.get("clean_claims") if isinstance(delivery.get("clean_claims"), list) else []
            clean_citations = delivery.get("clean_citations") if isinstance(delivery.get("clean_citations"), list) else []
            source_quality = delivery.get("source_quality") if isinstance(delivery.get("source_quality"), list) else []
            risks = delivery.get("risks") if isinstance(delivery.get("risks"), list) else []
            recommended_next_actions = (
                delivery.get("recommended_next_actions")
                if isinstance(delivery.get("recommended_next_actions"), list)
                else []
            )
            value_cards = delivery_value_cards(delivery, report)
            copy_text = delivery_json_script(delivery.get("copy_text"))
            action_bar = "" if print_mode else f"""
      <div class="actions" aria-label="Report actions">
        <a class="button primary" href="{html.escape(str(actions.get('download_pdf') or '#'), quote=True)}">Download PDF</a>
        <a class="button" href="{html.escape(str(actions.get('download_json') or '#'), quote=True)}">Download JSON</a>
        <button class="button" type="button" data-copy-summary>Copy summary</button>
        <details class="action-menu">
          <summary class="button">More actions</summary>
          <div class="action-menu-panel">
            <a href="{html.escape(str(actions.get('refine') or public_url('/proof-pack/bundle')), quote=True)}">Request refinement</a>
            <a href="{html.escape(str(actions.get('analyze_another') or public_url('/proof-pack/bundle')), quote=True)}">Analyze another source</a>
            <a href="{html.escape(str(actions.get('upgrade') or public_url('/proof-pack/bundle')), quote=True)}">Upgrade bundle</a>
            <a href="{html.escape(str(actions.get('print') or '#'), quote=True)}">Print view</a>
          </div>
        </details>
      </div>
            """
            content = f"""
    <section class="panel hero">
      <p class="eyebrow">Payment received</p>
      <h1>Proof Bundle Report</h1>
      <p class="lead">{html.escape(str(delivery.get('summary') or report.get('answer') or 'Report ready.'))}</p>
      <div class="hero-grid">
        <div>
          <span class="label">Bundle</span>
          <strong>{html.escape(str(delivery.get('bundle') or report.get('bundle') or payload.get('bundle') or 'proof'))}</strong>
        </div>
        <div>
          <span class="label">Sources</span>
          <strong>{html.escape(str(report.get('successful_sources') or 0))} delivered</strong>
        </div>
        <div>
          <span class="label">Confidence</span>
          <strong>{html.escape(str(delivery.get('confidence_score')))}</strong>
        </div>
        <div>
          <span class="label">Generated</span>
          <strong>{html.escape(str(delivery.get('generated_at_label') or 'unknown'))}</strong>
        </div>
      </div>
      <div class="quality-banner {quality_class}">
        <strong>{html.escape(str(delivery.get('quality_label') or 'Evidence reviewed'))}</strong>
        <span>Evidence quality {html.escape(str(delivery.get('quality_score')))}. AxonGate shows weak evidence honestly instead of turning noisy page text into inflated claims.</span>
      </div>
      {action_bar}
    </section>
    <section class="panel decision-panel">
      <div>
        <p class="eyebrow">Evidence decision</p>
        <h2>{html.escape(str(delivery.get('decision_label') or 'Evidence reviewed'))}</h2>
        <p>{html.escape(str(delivery.get('decision_summary') or 'AxonGate separated supported findings from weak or noisy source text.'))}</p>
      </div>
    </section>
    <section class="panel">
      <h2>What You Paid For</h2>
      <div class="value-grid">{value_cards}</div>
    </section>
    <section class="panel">
      <h2>What This Establishes</h2>
      {delivery_html_list(establishes, "The submitted material did not establish a clean substantive finding.")}
    </section>
    <section class="panel">
      <h2>Clean Findings</h2>
      <div class="claim-list">{delivery_claim_cards(clean_claims)}</div>
    </section>
    <section class="panel">
      <h2>Risks and Gaps</h2>
      {delivery_html_list(risks, "No delivery risks were recorded.")}
    </section>
    <section class="panel">
      <h2>Recommended Next Actions</h2>
      {delivery_html_list(recommended_next_actions, "No next actions were recorded.")}
    </section>
    <section class="panel">
      <h2>Evidence Citations</h2>
      <div class="citation-list">{delivery_citation_cards(clean_citations)}</div>
    </section>
    <section class="panel">
      <h2>Source Quality Audit</h2>
      <div class="source-list">{delivery_source_cards(source_quality)}</div>
    </section>
    <script>
      const axonGateCopyText = {copy_text};
      const copyButton = document.querySelector("[data-copy-summary]");
      if (copyButton && navigator.clipboard) {{
        copyButton.addEventListener("click", async () => {{
          await navigator.clipboard.writeText(axonGateCopyText);
          copyButton.textContent = "Copied";
          window.setTimeout(() => copyButton.textContent = "Copy summary", 1400);
        }});
      }}
    </script>
            """
        else:
            status_text = "Payment received. AxonGate is generating your cited Proof Bundle report."
            if payload.get("delivery_note"):
                status_text = str(payload["delivery_note"])
            content = f"""
    <section class="panel hero">
      <p class="eyebrow">Payment received</p>
      <h1>Proof Bundle Delivery</h1>
      <p>{html.escape(status_text)}</p>
      <p>Refresh this page shortly. If the source URL needs attention, the operator inbox will show the delivery note.</p>
    </section>
            """

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AxonGate Proof Bundle Delivery</title>
  <style>
    :root {{
      color-scheme: light dark;
      --bg: #0f1117;
      --panel: #171a22;
      --text: #f2f4f8;
      --muted: #b7c0cf;
      --line: #303542;
      --accent: #73daca;
      --accent-strong: #0f766e;
      --warn: #f6c177;
      --danger: #fca5a5;
      --good: #86efac;
      --code: #0a0d13;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--bg); color: var(--text); line-height: 1.55; }}
    main {{ max-width: 1120px; margin: 0 auto; padding: 34px 18px 64px; }}
    h1 {{ margin: 0 0 10px; font-size: clamp(2rem, 4vw, 3.3rem); line-height: 1.05; }}
    h2 {{ margin: 0 0 14px; font-size: 1.15rem; }}
    p, td {{ color: var(--muted); }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .panel {{ border: 1px solid var(--line); border-radius: 8px; background: var(--panel); padding: 18px; margin: 0 0 16px; }}
    .hero {{ padding: clamp(22px, 4vw, 34px); }}
    .eyebrow {{ color: var(--accent); text-transform: uppercase; letter-spacing: 0; font-size: .78rem; font-weight: 800; margin: 0 0 8px; }}
    .lead {{ max-width: 86ch; }}
    .muted {{ color: var(--muted); }}
    .hero-grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin: 18px 0; }}
    .hero-grid > div {{ border: 1px solid var(--line); border-radius: 8px; padding: 11px; min-width: 0; }}
    .label {{ display: block; color: var(--muted); font-size: .8rem; }}
    .quality-banner {{ display: grid; gap: 4px; border: 1px solid var(--line); border-left-width: 5px; border-radius: 8px; padding: 13px; margin: 18px 0; }}
    .quality-strong {{ border-left-color: var(--good); }}
    .quality-usable {{ border-left-color: var(--accent); }}
    .quality-low {{ border-left-color: var(--warn); }}
    .quality-processing {{ border-left-color: var(--muted); }}
    .actions {{ display: flex; flex-wrap: wrap; gap: 9px; margin-top: 18px; }}
    .button, button.button {{ display: inline-flex; align-items: center; justify-content: center; min-height: 40px; border: 1px solid var(--line); border-radius: 8px; background: #10141d; color: var(--text); padding: 9px 12px; font: inherit; font-weight: 700; text-decoration: none; cursor: pointer; }}
    .button.primary {{ background: var(--accent-strong); border-color: var(--accent-strong); color: #fff; }}
    .action-menu {{ position: relative; }}
    .action-menu summary {{ list-style: none; }}
    .action-menu summary::-webkit-details-marker {{ display: none; }}
    .action-menu-panel {{ position: absolute; right: 0; z-index: 5; min-width: 220px; margin-top: 8px; border: 1px solid var(--line); border-radius: 8px; background: #10141d; box-shadow: 0 18px 45px rgba(0,0,0,.28); padding: 8px; }}
    .action-menu-panel a {{ display: block; border-radius: 6px; padding: 9px 10px; color: var(--text); }}
    .action-menu-panel a:hover {{ background: rgba(115,218,202,.12); text-decoration: none; }}
    .decision-panel {{ border-left: 5px solid var(--accent); }}
    .decision-panel h2 {{ font-size: clamp(1.45rem, 3vw, 2rem); margin-bottom: 8px; }}
    .value-grid {{ display: grid; gap: 10px; grid-template-columns: repeat(4, minmax(0, 1fr)); }}
    .value-card {{ border: 1px solid var(--line); border-radius: 8px; padding: 13px; background: #131722; min-width: 0; }}
    .value-card span {{ display: block; color: var(--accent); font-size: .78rem; font-weight: 800; text-transform: uppercase; }}
    .value-card strong {{ display: block; margin: 4px 0 6px; overflow-wrap: anywhere; }}
    .value-card p {{ margin: 0; }}
    .claim-list, .citation-list, .source-list {{ display: grid; gap: 10px; }}
    .claim-item, .citation-item, .source-item {{ border: 1px solid var(--line); border-radius: 8px; padding: 13px; overflow-wrap: anywhere; }}
    .claim-item p, .citation-item p, .source-item p {{ margin: 0 0 9px; }}
    .mini-meta {{ display: flex; flex-wrap: wrap; gap: 7px; color: var(--muted); font-size: .88rem; }}
    .mini-meta span {{ border: 1px solid var(--line); border-radius: 999px; padding: 3px 8px; }}
    .citation-head, .source-top {{ display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; margin-bottom: 8px; }}
    .source-top h3 {{ margin: 0 0 5px; font-size: 1rem; overflow-wrap: anywhere; }}
    .quality-pill {{ display: inline-flex; white-space: nowrap; border: 1px solid var(--line); border-left-width: 5px; border-radius: 999px; padding: 5px 9px; color: var(--text); }}
    details {{ margin-top: 10px; }}
    summary {{ cursor: pointer; color: var(--accent); font-weight: 700; }}
    ul {{ padding-left: 1.2rem; }}
    li {{ margin: 6px 0; color: var(--muted); }}
    code {{ background: var(--code); border: 1px solid var(--line); border-radius: 4px; padding: 1px 5px; color: var(--text); }}
    @media (max-width: 760px) {{
      .hero-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .value-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .citation-head, .source-top {{ display: grid; }}
      .quality-pill {{ width: fit-content; }}
      .action-menu {{ width: 100%; }}
      .action-menu-panel {{ position: static; min-width: 0; }}
    }}
    @media (max-width: 520px) {{
      main {{ padding: 20px 12px 44px; }}
      .hero-grid {{ grid-template-columns: 1fr; }}
      .value-grid {{ grid-template-columns: 1fr; }}
      .actions .button {{ width: 100%; }}
    }}
    @media print {{
      :root {{ color-scheme: light; --bg: #fff; --panel: #fff; --text: #111827; --muted: #374151; --line: #d1d5db; --code: #f3f4f6; }}
      nav, .actions, script {{ display: none !important; }}
      body {{ background: #fff; }}
      main {{ max-width: 100%; padding: 0; }}
      .panel {{ break-inside: avoid; }}
      a {{ color: #111827; }}
    }}
    {shared_ui_css()}
  </style>
</head>
<body>
  <main>
    {nav}
    {content}
  </main>
</body>
</html>"""


def pdf_escape_text(value: Any) -> str:
    return str(value or "").replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def proof_bundle_pdf_lines(lead: dict[str, Any]) -> list[str]:
    payload = build_proof_bundle_delivery_payload(lead)
    report = payload.get("report") if isinstance(payload.get("report"), dict) else {}
    delivery = payload.get("delivery") if isinstance(payload.get("delivery"), dict) else {}
    lines = [
        "AxonGate Proof Bundle Report",
        "",
        f"Bundle: {delivery.get('bundle') or payload.get('bundle') or 'proof'}",
        f"Generated: {delivery.get('generated_at_label') or 'unknown'}",
        f"Evidence decision: {delivery.get('decision_label') or 'Evidence reviewed'}",
        f"Decision summary: {delivery.get('decision_summary') or ''}",
        f"Evidence quality: {delivery.get('quality_label') or 'Evidence reviewed'} ({delivery.get('quality_score')})",
        f"Confidence: {delivery.get('confidence_score')}",
        f"Sources delivered: {report.get('successful_sources') or 0}",
        "",
        "Executive Summary",
        str(delivery.get("summary") or report.get("answer") or "Report ready."),
        "",
        "What This Establishes",
    ]
    establishes = delivery.get("what_this_establishes") if isinstance(delivery.get("what_this_establishes"), list) else []
    clean_claims = delivery.get("clean_claims") if isinstance(delivery.get("clean_claims"), list) else []
    risks = delivery.get("risks") if isinstance(delivery.get("risks"), list) else []
    source_quality = delivery.get("source_quality") if isinstance(delivery.get("source_quality"), list) else []
    for item in establishes:
        lines.append(f"- {clean_report_text(item, 260)}")
    lines.extend(["", "Clean Findings"])
    for claim in clean_claims:
        if isinstance(claim, dict):
            citations = ", ".join(str(item) for item in claim.get("citation_ids", [])[:4])
            lines.append(f"- {clean_report_text(claim.get('claim'), 260)} [{citations}]")
    lines.extend(["", "Risks and Gaps"])
    for risk in risks:
        lines.append(f"- {clean_report_text(risk, 260)}")
    next_actions = delivery.get("recommended_next_actions") if isinstance(delivery.get("recommended_next_actions"), list) else []
    lines.extend(["", "Recommended Next Actions"])
    for action in next_actions:
        lines.append(f"- {clean_report_text(action, 260)}")
    lines.extend(["", "Source Quality Audit"])
    for source in source_quality:
        if isinstance(source, dict):
            lines.append(
                f"- {source.get('source_id')}: {source.get('quality_label')} quality={source.get('quality_score')} "
                f"citations={source.get('citation_count')} url={report_text_from_url(str(source.get('target_url') or ''))}"
            )
            if source.get("content_sha256"):
                lines.append(f"  hash={source.get('content_sha256')}")
    return lines


def build_simple_pdf(title: str, lines: list[str]) -> bytes:
    """Build a small text PDF without external dependencies."""
    wrapped_lines: list[str] = []
    for line in lines:
        if not line:
            wrapped_lines.append("")
            continue
        wrapped_lines.extend(textwrap.wrap(str(line), width=92) or [""])
    pages = [wrapped_lines[index : index + 46] for index in range(0, len(wrapped_lines), 46)] or [[""]]

    objects: list[bytes] = []
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(b"")  # pages object filled after page ids are known
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_ids: list[int] = []
    content_ids: list[int] = []

    for page_lines in pages:
        page_id = len(objects) + 1
        content_id = page_id + 1
        page_ids.append(page_id)
        content_ids.append(content_id)
        commands = ["BT", "/F1 11 Tf", "50 760 Td", "14 TL"]
        for line_index, line in enumerate(page_lines):
            font_size = 16 if page_id == page_ids[0] and line_index == 0 else 11
            if font_size == 16:
                commands.append("/F1 16 Tf")
            elif line_index == 1 and page_id == page_ids[0]:
                commands.append("/F1 11 Tf")
            commands.append(f"({pdf_escape_text(line)}) Tj")
            commands.append("T*")
        commands.append("ET")
        stream = "\n".join(commands).encode("latin-1", errors="replace")
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>".encode(
                "ascii"
            )
        )
        objects.append(b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream")

    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    objects[1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode("ascii")

    output = bytearray()
    output.extend(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode("ascii"))
        output.extend(obj)
        output.extend(b"\nendobj\n")
    xref_start = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R /Title ({pdf_escape_text(title)}) >>\n"
            f"startxref\n{xref_start}\n%%EOF\n"
        ).encode("latin-1", errors="replace")
    )
    return bytes(output)


def build_proof_bundle_delivery_pdf(lead: dict[str, Any]) -> bytes:
    return build_simple_pdf("AxonGate Proof Bundle Report", proof_bundle_pdf_lines(lead))


def build_proof_bundle_recovery_html(email: str = "", target_url: str = "", error: str = "") -> str:
    nav = site_nav_html("Bundles")
    error_html = f'<p class="notice">{html.escape(error)}</p>' if error else ""
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Recover AxonGate Proof Bundle Delivery</title>
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
    body {{ margin: 0; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--bg); color: var(--text); line-height: 1.55; }}
    main {{ max-width: 900px; margin: 0 auto; padding: 34px 18px 64px; }}
    h1 {{ margin: 0 0 10px; font-size: clamp(2rem, 4vw, 3rem); line-height: 1.05; }}
    p {{ color: var(--muted); max-width: 72ch; }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .panel {{ border: 1px solid var(--line); border-radius: 8px; background: var(--panel); padding: clamp(18px, 3vw, 28px); margin: 0 0 16px; }}
    label {{ display: grid; gap: 7px; margin: 0 0 14px; color: var(--text); font-weight: 700; }}
    input {{ width: 100%; min-height: 44px; border: 1px solid var(--line); border-radius: 8px; background: #0a0d13; color: var(--text); padding: 10px 12px; font: inherit; }}
    .button, button {{ display: inline-flex; align-items: center; justify-content: center; min-height: 42px; border: 1px solid var(--line); border-radius: 8px; background: color-mix(in srgb, var(--accent), var(--panel) 78%); color: var(--text); padding: 10px 14px; font: inherit; font-weight: 700; cursor: pointer; }}
    .notice {{ border: 1px solid color-mix(in srgb, #f6c177, var(--line) 45%); border-radius: 8px; padding: 11px 12px; background: color-mix(in srgb, #f6c177, var(--panel) 88%); color: var(--text); }}
    {shared_ui_css()}
  </style>
</head>
<body>
  <main>
    {nav}
    <section class="panel">
      <h1>Recover Proof Bundle Delivery</h1>
      <p>Enter the email used at Stripe checkout and one target URL from the purchase. If Stripe passed a different email or malformed target data, AxonGate can still recover a single pending paid bundle.</p>
      {error_html}
      <form action="{html.escape(public_url('/proof-pack/bundle/recover'), quote=True)}" method="get">
        <label>
          Checkout email
          <input type="email" name="email" value="{html.escape(email, quote=True)}" autocomplete="email" required>
        </label>
        <label>
          Target URL
          <input type="text" name="target_url" value="{html.escape(target_url, quote=True)}" inputmode="url" placeholder="www.example.com or https://example.com" required>
          <small>Include http/https if you know it; otherwise AxonGate will try https automatically.</small>
        </label>
        <button type="submit">Recover delivery</button>
      </form>
    </section>
  </main>
</body>
</html>"""


def proof_pack_request_page_url(target_url: str, question: str, pack: str, source: str) -> str:
    normalized_pack = normalize_proof_pack(pack)
    normalized_source = normalize_attribution_source(source)
    return (
        f"{PUBLIC_BASE_URL}/proof-pack/request?"
        f"target_url={url_quote(target_url, safe='')}"
        f"&question={url_quote(question, safe='')}"
        f"&pack={url_quote(normalized_pack, safe='')}"
        f"&source={url_quote(normalized_source, safe='')}"
    )


def proof_pack_quote_page_url(target_url: str, question: str, pack: str, source: str) -> str:
    normalized_pack = normalize_proof_pack(pack)
    normalized_source = normalize_attribution_source(source)
    return (
        f"{PUBLIC_BASE_URL}/proof-pack/quote?"
        f"target_url={url_quote(target_url, safe='')}"
        f"&question={url_quote(question, safe='')}"
        f"&pack={url_quote(normalized_pack, safe='')}"
        f"&source={url_quote(normalized_source, safe='')}"
    )


def proof_bundle_question(value: Optional[str]) -> str:
    cleaned = clean_lead_text(value, 600)
    return cleaned or "Which source-backed claims should this bundle establish?"


def split_bundle_target_urls(value: Any) -> list[str]:
    """Accept newline, comma, space, or list-style target URLs for bundle forms and APIs."""
    raw_items: list[str] = []
    if isinstance(value, (list, tuple, set)):
        for item in value:
            raw_items.extend(split_bundle_target_urls(item))
    else:
        raw = str(value or "").strip()
        if raw:
            raw_items.extend(part.strip() for part in re.split(r"[\s,]+", raw) if part.strip())

    deduped: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        key = item.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(key)
    return deduped[:50]


def proof_bundle_target_query(target_urls: list[str]) -> str:
    return url_quote("\n".join(target_urls), safe="")


def proof_bundle_quote_page_url(target_urls: list[str], question: str, bundle: str, source: str) -> str:
    normalized_bundle = normalize_proof_bundle(bundle)
    normalized_source = normalize_attribution_source(source)
    return (
        f"{PUBLIC_BASE_URL}/proof-pack/bundle/quote?"
        f"target_urls={proof_bundle_target_query(target_urls)}"
        f"&question={url_quote(question, safe='')}"
        f"&bundle={url_quote(normalized_bundle, safe='')}"
        f"&source={url_quote(normalized_source, safe='')}"
    )


def proof_bundle_request_page_url(target_urls: list[str], question: str, bundle: str, source: str) -> str:
    normalized_bundle = normalize_proof_bundle(bundle)
    normalized_source = normalize_attribution_source(source)
    return (
        f"{PUBLIC_BASE_URL}/proof-pack/bundle?"
        f"target_urls={proof_bundle_target_query(target_urls)}"
        f"&question={url_quote(question, safe='')}"
        f"&bundle={url_quote(normalized_bundle, safe='')}"
        f"&source={url_quote(normalized_source, safe='')}"
    )


def proof_bundle_checkout_url(target_urls: list[str], question: str, bundle: str, source: str) -> str:
    """Return the tracked checkout redirect for a bundle quote."""
    normalized_bundle = normalize_proof_bundle(bundle)
    normalized_source = normalize_attribution_source(source)
    query = (
        f"target_urls={proof_bundle_target_query(target_urls)}"
        f"&question={url_quote(question, safe='')}"
        f"&bundle={url_quote(normalized_bundle, safe='')}"
        f"&source={url_quote(normalized_source, safe='')}"
    )
    return f"{PUBLIC_BASE_URL}/proof-pack/bundle/pay?{query}"


def proof_bundle_payment_path(target_urls: list[str], question: str, bundle: str, source: str) -> str:
    """Return the same-host tracked payment redirect path."""
    normalized_bundle = normalize_proof_bundle(bundle)
    normalized_source = normalize_attribution_source(source)
    parts = [
        f"bundle={url_quote(normalized_bundle, safe='')}",
        f"source={url_quote(normalized_source, safe='')}",
    ]
    if target_urls:
        parts.insert(0, f"target_urls={proof_bundle_target_query(target_urls)}")
    if question:
        parts.insert(-2, f"question={url_quote(question, safe='')}")
    return f"/proof-pack/bundle/pay?{'&'.join(parts)}"


def proof_bundle_checkout_path(target_urls: list[str], question: str, bundle: str, source: str) -> str:
    """Return a same-host checkout review path for customer-facing Stripe buttons."""
    normalized_bundle = normalize_proof_bundle(bundle)
    normalized_source = normalize_attribution_source(source)
    parts = [
        f"bundle={url_quote(normalized_bundle, safe='')}",
        f"source={url_quote(normalized_source, safe='')}",
    ]
    if target_urls:
        parts.insert(0, f"target_urls={proof_bundle_target_query(target_urls)}")
    if question:
        parts.insert(-2, f"question={url_quote(question, safe='')}")
    return f"/proof-pack/bundle/checkout?{'&'.join(parts)}"


def proof_bundle_report_preview(bundle: str, target_count: int = 0) -> dict[str, Any]:
    """Return buyer-facing value promises for checkout and quote pages."""
    normalized_bundle = normalize_proof_bundle(bundle)
    source_limit = proof_bundle_source_limit(normalized_bundle)
    count_label = f"{target_count} submitted source{'s' if target_count != 1 else ''}" if target_count else (
        f"up to {source_limit} sources collected at Stripe"
    )
    return {
        "decision_label": "Evidence decision before your agent cites",
        "decision_summary": (
            "AxonGate turns public URLs into a report that separates supported findings, weak evidence, and source risks before an AI workflow relies on them."
        ),
        "source_coverage": count_label,
        "deliverables": [
            "Plain-English evidence decision",
            "Claim-to-citation map",
            "Source quality audit",
            "Risks and gaps",
            "Source hashes for repeatability",
            "PDF, JSON, browser, and email delivery",
        ],
        "after_checkout": [
            "Stripe confirms payment and passes the checkout email, source URLs, and question.",
            "AxonGate fetches public source material, removes provider noise, and scores evidence quality.",
            "The report opens in-browser and is emailed when the checkout email is available.",
        ],
        "trust_notes": [
            "No supplier or LLM spend happens on the quote page.",
            "Weak evidence is shown as weak instead of inflated into a claim.",
            "Delivery can be recovered by checkout email and one submitted target URL.",
        ],
    }


async def validate_proof_bundle_targets(target_urls: list[str], bundle: str) -> list[str]:
    normalized_bundle = normalize_proof_bundle(bundle)
    limit = proof_bundle_source_limit(normalized_bundle)
    if not target_urls:
        raise PaymentValidationError("Add at least one public target URL for the Proof Bundle.")
    if len(target_urls) > limit:
        raise PaymentValidationError(f"{normalized_bundle} bundles accept up to {limit} source URLs.")

    normalized_targets: list[str] = []
    seen: set[str] = set()
    for target_url in target_urls:
        normalized_target = await assert_public_target_url(target_url)
        if normalized_target in seen:
            continue
        seen.add(normalized_target)
        normalized_targets.append(normalized_target)

    if not normalized_targets:
        raise PaymentValidationError("Add at least one public target URL for the Proof Bundle.")
    return normalized_targets


async def proof_bundle_cache_profiles(target_urls: list[str]) -> list[dict[str, Any]]:
    profiles: list[dict[str, Any]] = []
    for target_url in target_urls:
        starter_available = await get_cache_candidate_for_tier(target_url, STARTER_TIER, False) is not None
        basic_available = await get_cache_candidate_for_tier(target_url, "basic", False) is not None
        profiles.append(
            {
                "target_url": target_url,
                "cached_source_available": bool(starter_available or basic_available),
                "starter_sample_available": starter_available,
                "basic_cache_available": basic_available,
            }
        )
    return profiles


async def build_proof_bundle_quote(
    target_urls: list[str],
    question: Optional[str] = None,
    bundle: Optional[str] = None,
    source: str = "proof-bundle",
) -> dict[str, Any]:
    """Return no-spend pricing and next steps for a multi-source Proof Bundle."""
    normalized_source = normalize_attribution_source(source)
    normalized_bundle = normalize_proof_bundle(bundle)
    normalized_targets = await validate_proof_bundle_targets(target_urls, normalized_bundle)
    normalized_question = proof_bundle_question(question)
    source_profiles = await proof_bundle_cache_profiles(normalized_targets)
    cached_sources_count = sum(1 for profile in source_profiles if profile["cached_source_available"])
    selected_price = price_for_proof_bundle(normalized_bundle)
    quote_page = proof_bundle_quote_page_url(normalized_targets, normalized_question, normalized_bundle, normalized_source)
    request_page = proof_bundle_request_page_url(normalized_targets, normalized_question, normalized_bundle, normalized_source)
    checkout_url = proof_bundle_checkout_url(normalized_targets, normalized_question, normalized_bundle, normalized_source)
    checkout_review_url = f"{PUBLIC_BASE_URL}{proof_bundle_checkout_path(normalized_targets, normalized_question, normalized_bundle, normalized_source)}"
    quote_api = (
        f"{PUBLIC_BASE_URL}/v1/proof-pack/bundle/quote?"
        f"target_urls={proof_bundle_target_query(normalized_targets)}"
        f"&question={url_quote(normalized_question, safe='')}"
        f"&bundle={url_quote(normalized_bundle, safe='')}"
        f"&source={url_quote(normalized_source, safe='')}"
    )
    payment_url = proof_bundle_payment_url(normalized_bundle)

    bundles: dict[str, dict[str, Any]] = {}
    for bundle_name, price in PROOF_BUNDLE_PRICING_USDC.items():
        bundle_quote_page = proof_bundle_quote_page_url(normalized_targets, normalized_question, bundle_name, normalized_source)
        bundle_request_page = proof_bundle_request_page_url(normalized_targets, normalized_question, bundle_name, normalized_source)
        bundle_checkout_url = proof_bundle_checkout_url(normalized_targets, normalized_question, bundle_name, normalized_source)
        bundle_external_payment_url = proof_bundle_payment_url(bundle_name)
        bundles[bundle_name] = {
            "price_usdc": float(price),
            "amount_units": str(usdc_units(price)),
            "source_limit": proof_bundle_source_limit(bundle_name),
            "policy": proof_bundle_policy(bundle_name),
            "checkout_url": bundle_checkout_url,
            "payment_url": bundle_checkout_url,
            "external_payment_url_configured": bool(bundle_external_payment_url),
            "quote_page": bundle_quote_page,
            "request_page": bundle_request_page,
        }

    return {
        "status": "proof_bundle_quote",
        "supplier_spend": False,
        "target_urls": normalized_targets,
        "target_count": len(normalized_targets),
        "question": normalized_question,
        "source": normalized_source,
        "bundle": normalized_bundle,
        "price_usdc": float(selected_price),
        "amount_units": str(usdc_units(selected_price)),
        "source_limit": proof_bundle_source_limit(normalized_bundle),
        "cached_sources_count": cached_sources_count,
        "source_profiles": source_profiles,
        "report_preview": proof_bundle_report_preview(normalized_bundle, len(normalized_targets)),
        "bundles": bundles,
        "next_steps": {
            "bundle_page": public_url("/proof-pack/bundle"),
            "quote_page": quote_page,
            "quote_api": quote_api,
            "request_page": request_page,
            "checkout_review_url": checkout_review_url,
            "checkout_url": checkout_url,
            "payment_url": checkout_url,
            "payment_link_configured": bool(payment_url),
            "external_payment_url_configured": bool(payment_url),
            "single_source_proof_pack_endpoint": f"{PUBLIC_BASE_URL}/v1/x402/proof-pack",
            "single_source_quote_api": public_url("/v1/proof-pack/quote"),
            "operator_leads": public_url("/operator/leads"),
            "note": "Proof Bundle checkout clicks are tracked; configured payment links redirect to checkout, otherwise AxonGate falls back to request capture.",
        },
    }


def normalize_lead_status(status: Any) -> str:
    normalized = str(status or "new").strip().lower().replace(" ", "_")
    if normalized not in PROOF_BUNDLE_LEAD_STATUSES:
        raise PaymentValidationError(f"Unsupported lead status. Use {', '.join(PROOF_BUNDLE_LEAD_STATUSES)}.")
    return normalized


def lead_with_pipeline_defaults(lead: dict[str, Any]) -> dict[str, Any]:
    """Backfill pipeline fields for older stored leads without changing the raw record."""
    updated = dict(lead)
    created_at = int(updated.get("created_at") or time.time())
    try:
        status = normalize_lead_status(updated.get("status") or "new")
    except PaymentValidationError:
        status = "new"
    updated["status"] = status
    updated.setdefault("status_updated_at", created_at)
    history = updated.get("status_history")
    if not isinstance(history, list) or not history:
        updated["status_history"] = [
            {
                "status": status,
                "at": int(updated.get("status_updated_at") or created_at),
                "note": "Lead captured.",
            }
        ]
    updated.setdefault("fulfillment_url", "")
    updated.setdefault("delivery_note", "")
    return updated


async def store_proof_pack_lead(lead: dict[str, Any]) -> str:
    """Store a Proof Pack lead in Redis with an in-memory fallback."""
    lead.update(lead_with_pipeline_defaults(lead))
    payload = json.dumps(lead, separators=(",", ":"), sort_keys=True)
    if redis_client:
        try:
            await redis_client.lpush(PROOF_PACK_LEADS_REDIS_KEY, payload)
            await redis_client.ltrim(PROOF_PACK_LEADS_REDIS_KEY, 0, PROOF_PACK_LEADS_MEMORY_MAX - 1)
            return "redis"
        except Exception as exc:
            inc_metric("proof_pack_lead_errors_total")
            print(f"[PROOF_PACK_LEADS] Redis lead storage failed: {exc}")

    async with proof_pack_leads_lock:
        proof_pack_leads.insert(0, lead)
        del proof_pack_leads[PROOF_PACK_LEADS_MEMORY_MAX:]
    return "memory"


async def proof_pack_leads_public_snapshot() -> dict[str, Any]:
    """Return public-safe lead storage health without exposing buyer contact data."""
    snapshot: dict[str, Any] = {
        "backend": "redis" if redis_client else "memory",
        "max_retained": PROOF_PACK_LEADS_MEMORY_MAX,
        "count": 0,
        "latest": None,
    }

    latest: Optional[dict[str, Any]] = None
    if redis_client:
        snapshot["redis_key"] = PROOF_PACK_LEADS_REDIS_KEY
        try:
            snapshot["count"] = int(await redis_client.llen(PROOF_PACK_LEADS_REDIS_KEY))
            latest_raw = await redis_client.lindex(PROOF_PACK_LEADS_REDIS_KEY, 0)
            if latest_raw:
                latest = json.loads(latest_raw)
        except Exception as exc:
            print(f"[PROOF_PACK_LEADS] Redis lead snapshot failed: {exc}")

    if latest is None:
        async with proof_pack_leads_lock:
            snapshot["count"] = max(snapshot["count"], len(proof_pack_leads))
            latest = dict(proof_pack_leads[0]) if proof_pack_leads else None

    if latest:
        snapshot["latest"] = {
            "created_at": latest.get("created_at"),
            "product": latest.get("product") or "proof_pack",
            "pack": latest.get("pack"),
            "bundle": latest.get("bundle"),
            "target_count": latest.get("target_count") or (len(latest.get("target_urls") or []) if latest.get("target_urls") else 1),
            "source": latest.get("source"),
            "amount_units": latest.get("amount_units"),
            "status": latest.get("status") or "new",
            "fulfillment_url_configured": bool(latest.get("fulfillment_url")),
            "has_contact": bool(latest.get("contact")),
        }
    return snapshot


def operator_token_candidates(request: Request) -> list[str]:
    """Read operator token candidates from safe headers and browser fallback query params."""
    candidates: list[str] = []
    authorization = request.headers.get("authorization", "").strip()
    if authorization.lower().startswith("bearer "):
        candidates.append(authorization[7:].strip())
    header_token = request.headers.get("X-AxonGate-Operator-Token")
    if header_token:
        candidates.append(header_token.strip())
    for query_name in ("operator_token", "token"):
        query_token = request.query_params.get(query_name)
        if query_token:
            candidates.append(query_token.strip())
    return [candidate for candidate in candidates if candidate]


def require_operator_access(request: Request) -> None:
    """Gate raw lead data behind an operator token."""
    if not OPERATOR_TOKEN:
        raise HTTPException(
            status_code=503,
            detail={
                "message": "Private operator lead access is disabled until AXONGATE_OPERATOR_TOKEN is configured.",
                "env": "AXONGATE_OPERATOR_TOKEN",
            },
        )

    for candidate in operator_token_candidates(request):
        if secrets.compare_digest(candidate, OPERATOR_TOKEN):
            return

    inc_metric("operator_auth_failures_total")
    raise HTTPException(
        status_code=401,
        detail="Operator token required.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def parse_proof_pack_lead_record(raw: Any) -> Optional[dict[str, Any]]:
    if isinstance(raw, dict):
        return dict(raw)
    if not isinstance(raw, str):
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


async def durable_proof_pack_leads(limit: int = 50) -> list[dict[str, Any]]:
    """Return stored Proof Pack leads for private operator views."""
    bounded_limit = max(1, min(int(limit or 50), PROOF_PACK_LEADS_MEMORY_MAX, 200))
    leads: list[dict[str, Any]] = []

    if redis_client:
        try:
            records = await redis_client.lrange(PROOF_PACK_LEADS_REDIS_KEY, 0, bounded_limit - 1)
        except Exception as exc:
            print(f"[PROOF_PACK_LEADS] Redis lead read failed: {exc}")
        else:
            for record in records:
                parsed = parse_proof_pack_lead_record(record)
                if parsed:
                    leads.append(lead_with_pipeline_defaults(parsed))

    if not leads:
        async with proof_pack_leads_lock:
            leads = [lead_with_pipeline_defaults(lead) for lead in proof_pack_leads[:bounded_limit]]

    return leads[:bounded_limit]


async def update_stored_proof_pack_lead(lead_id: str, updates: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Update one stored lead in Redis or memory and return the updated record."""
    normalized_id = str(lead_id or "").strip()
    if not normalized_id:
        return None

    def apply_updates(record: dict[str, Any]) -> dict[str, Any]:
        updated = lead_with_pipeline_defaults(record)
        updated.update({key: value for key, value in updates.items() if value is not None})
        return lead_with_pipeline_defaults(updated)

    if redis_client:
        try:
            records = await redis_client.lrange(PROOF_PACK_LEADS_REDIS_KEY, 0, PROOF_PACK_LEADS_MEMORY_MAX - 1)
            parsed_records = [parse_proof_pack_lead_record(record) for record in records]
            updated_records: list[dict[str, Any]] = []
            updated_lead: Optional[dict[str, Any]] = None
            for parsed in parsed_records:
                if not parsed:
                    continue
                if str(parsed.get("id") or "") == normalized_id:
                    parsed = apply_updates(parsed)
                    updated_lead = parsed
                else:
                    parsed = lead_with_pipeline_defaults(parsed)
                updated_records.append(parsed)
            if updated_lead is not None:
                pipe = redis_client.pipeline()
                pipe.delete(PROOF_PACK_LEADS_REDIS_KEY)
                if updated_records:
                    pipe.rpush(
                        PROOF_PACK_LEADS_REDIS_KEY,
                        *[
                            json.dumps(record, separators=(",", ":"), sort_keys=True)
                            for record in updated_records[:PROOF_PACK_LEADS_MEMORY_MAX]
                        ],
                    )
                    pipe.ltrim(PROOF_PACK_LEADS_REDIS_KEY, 0, PROOF_PACK_LEADS_MEMORY_MAX - 1)
                await pipe.execute()
                return updated_lead
        except Exception as exc:
            inc_metric("proof_pack_lead_errors_total")
            print(f"[PROOF_PACK_LEADS] Redis lead update failed: {exc}")

    async with proof_pack_leads_lock:
        for index, lead in enumerate(proof_pack_leads):
            if str(lead.get("id") or "") == normalized_id:
                proof_pack_leads[index] = apply_updates(lead)
                return dict(proof_pack_leads[index])
    return None


async def update_proof_pack_lead_status(
    lead_id: str,
    status: str,
    *,
    note: Optional[str] = None,
    fulfillment_url: Optional[str] = None,
    delivery_note: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Move a lead through the lightweight operator pipeline."""
    normalized_status = normalize_lead_status(status)
    now = int(time.time())
    clean_note = clean_lead_text(note, 500)
    clean_fulfillment_url = clean_lead_text(fulfillment_url, 2048)
    clean_delivery_note = clean_lead_text(delivery_note, 800)
    existing = next((lead for lead in await durable_proof_pack_leads(PROOF_PACK_LEADS_MEMORY_MAX) if lead.get("id") == lead_id), None)
    if not existing:
        return None
    try:
        previous_status = normalize_lead_status(existing.get("status") or "new")
    except PaymentValidationError:
        previous_status = "new"
    history = existing.get("status_history") if isinstance(existing.get("status_history"), list) else []
    history = list(history)[-20:]
    history.append(
        {
            "status": normalized_status,
            "at": now,
            "note": clean_note or f"Marked {normalized_status}.",
        }
    )
    updates: dict[str, Any] = {
        "status": normalized_status,
        "status_updated_at": now,
        "status_history": history,
    }
    if clean_fulfillment_url:
        updates["fulfillment_url"] = clean_fulfillment_url
    if clean_delivery_note:
        updates["delivery_note"] = clean_delivery_note
    if normalized_status == "fulfilled":
        updates["fulfilled_at"] = now
    updated = await update_stored_proof_pack_lead(lead_id, updates)
    if updated:
        inc_metric("proof_bundle_status_updates_total")
        if str(updated.get("product") or "") == "proof_bundle":
            source = normalize_attribution_source(str(updated.get("source") or "direct"))
            if normalized_status == "paid" and previous_status not in {"paid", "fulfilled"}:
                inc_metric("proof_bundle_paid_total")
                inc_attribution("proof_bundle_paid", source)
            if normalized_status == "fulfilled" and previous_status != "fulfilled":
                inc_metric("proof_bundle_fulfilled_total")
                inc_attribution("proof_bundle_fulfilled", source)
    return updated


def proof_pack_lead_stats(leads: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize private lead rows without changing the stored records."""
    by_product: dict[str, int] = {}
    by_pack: dict[str, int] = {}
    by_source: dict[str, int] = {}
    by_status: dict[str, int] = {}
    latest_created_at = 0
    total_value_usdc = Decimal("0")
    open_value_usdc = Decimal("0")
    paid_value_usdc = Decimal("0")
    fulfilled_value_usdc = Decimal("0")
    for lead in leads:
        product = str(lead.get("product") or "proof_pack")
        pack = str(lead.get("pack") or "unknown")
        source = normalize_attribution_source(str(lead.get("source") or "direct"))
        status = normalize_lead_status(lead.get("status") or "new")
        by_product[product] = by_product.get(product, 0) + 1
        by_pack[pack] = by_pack.get(pack, 0) + 1
        by_source[source] = by_source.get(source, 0) + 1
        by_status[status] = by_status.get(status, 0) + 1
        try:
            latest_created_at = max(latest_created_at, int(lead.get("created_at") or 0))
        except (TypeError, ValueError):
            pass
        try:
            lead_value = Decimal(str(lead.get("price_usdc") or "0"))
            total_value_usdc += lead_value
            if status in {"new", "contacted"}:
                open_value_usdc += lead_value
            if status in {"paid", "fulfilled"}:
                paid_value_usdc += lead_value
            if status == "fulfilled":
                fulfilled_value_usdc += lead_value
        except Exception:
            pass
    return {
        "retained": len(leads),
        "latest_created_at": latest_created_at or None,
        "by_product": dict(sorted(by_product.items())),
        "by_pack": dict(sorted(by_pack.items())),
        "by_source": dict(sorted(by_source.items())),
        "by_status": {status: by_status.get(status, 0) for status in PROOF_BUNDLE_LEAD_STATUSES},
        "indicative_value_usdc": float(total_value_usdc),
        "open_value_usdc": float(open_value_usdc),
        "paid_value_usdc": float(paid_value_usdc),
        "fulfilled_value_usdc": float(fulfilled_value_usdc),
    }


def lead_created_at_label(value: Any) -> str:
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return "unknown"
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(timestamp))


async def send_proof_pack_lead_notification(lead: dict[str, Any]) -> None:
    """Notify the operator about a new Proof Pack lead when a webhook is configured."""
    if not PROOF_PACK_LEAD_WEBHOOK_URL:
        return

    product = str(lead.get("product") or "proof_pack")
    payload = {
        "agent": "AxonGate",
        "event": "proof_bundle_lead_received" if product == "proof_bundle" else "proof_pack_lead_received",
        "public_base_url": PUBLIC_BASE_URL,
        "timestamp": int(time.time()),
        "lead": {
            "id": lead.get("id"),
            "created_at": lead.get("created_at"),
            "product": product,
            "contact": lead.get("contact"),
            "target_url": lead.get("target_url"),
            "target_urls": lead.get("target_urls"),
            "target_count": lead.get("target_count"),
            "question": lead.get("question"),
            "pack": lead.get("pack"),
            "bundle": lead.get("bundle"),
            "use_case": lead.get("use_case"),
            "budget_usdc": lead.get("budget_usdc"),
            "notes": lead.get("notes"),
            "source": lead.get("source"),
            "price_usdc": lead.get("price_usdc"),
            "amount_units": lead.get("amount_units"),
        },
        "next_steps": {
            "operator_leads": f"{PUBLIC_BASE_URL}/operator/leads",
            "preview_page": lead.get("preview_page"),
            "quote_page": lead.get("quote_page"),
            "request_page": lead.get("request_page"),
            "payment_url": lead.get("payment_url"),
            "payment_probe_url": lead.get("payment_probe_url"),
            "paid_endpoint": lead.get("paid_endpoint"),
            "buyer_command": lead.get("buyer_command"),
        },
    }
    headers = {"Content-Type": "application/json"}
    if PROOF_PACK_LEAD_WEBHOOK_TOKEN:
        headers["Authorization"] = f"Bearer {PROOF_PACK_LEAD_WEBHOOK_TOKEN}"

    try:
        async with httpx.AsyncClient(timeout=PROOF_PACK_LEAD_WEBHOOK_TIMEOUT_SECONDS) as client:
            response = await client.post(PROOF_PACK_LEAD_WEBHOOK_URL, json=payload, headers=headers)
            response.raise_for_status()
        inc_metric("proof_pack_lead_notifications_total")
    except Exception as exc:
        inc_metric("proof_pack_lead_notification_errors_total")
        print(f"[PROOF_PACK_LEADS] Lead webhook delivery failed: {exc}")


def proof_pack_lead_public_response(lead: dict[str, Any]) -> dict[str, Any]:
    """Return the lead acknowledgement without echoing contact details."""
    return {
        "status": "received",
        "lead_id": lead["id"],
        "product": lead.get("product") or "proof_pack",
        "target_url": lead["target_url"],
        "question": lead["question"],
        "pack": lead["pack"],
        "price_usdc": lead["price_usdc"],
        "amount_units": lead["amount_units"],
        "source": lead["source"],
        "contact_received": bool(lead.get("contact")),
        "next_steps": {
            "preview_page": lead.get("preview_page"),
            "preview_api": lead.get("preview_api"),
            "quote_page": lead["quote_page"],
            "quote_api": lead["quote_api"],
            "request_page": lead["request_page"],
            "payment_probe_url": lead["payment_probe_url"],
            "paid_endpoint": lead["paid_endpoint"],
            "buyer_command": lead["buyer_command"],
        },
    }


async def create_proof_pack_lead(payload: ProofPackLeadRequest | dict[str, Any], request: Request) -> dict[str, Any]:
    """Validate and store buyer intent for a Proof Pack without payment or supplier spend."""
    if isinstance(payload, BaseModel):
        raw = payload.model_dump() if hasattr(payload, "model_dump") else payload.dict()
    else:
        raw = dict(payload)

    contact = clean_lead_text(str(raw.get("contact") or ""), 180)
    if len(contact) < 3:
        raise PaymentValidationError("Add an email, handle, or reply path so we can follow up.")

    normalized_target = await assert_public_target_url(str(raw.get("target_url") or ""))
    await enforce_rate_limit("proof_pack_lead_target_domain", target_domain_identifier(normalized_target), RATE_LIMIT_TARGET_DOMAIN)
    normalized_pack = normalize_proof_pack(str(raw.get("pack") or PROOF_PACK_SAMPLE_PACK))
    question = proof_pack_question(raw.get("question"))
    source = normalize_attribution_source(str(raw.get("source") or attribution_source_from_request(request)))
    price = price_for_proof_pack(normalized_pack)
    created_at = int(time.time())
    lead_id = stable_hash(
        json.dumps(
            {
                "created_at": created_at,
                "contact": contact,
                "target_url": normalized_target,
                "question": question,
                "nonce": secrets.token_hex(4),
            },
            sort_keys=True,
        )
    )[:16]

    quote_page = proof_pack_quote_page_url(normalized_target, question, normalized_pack, source)
    preview_page = proof_pack_preview_page_url(normalized_target, question, normalized_pack, source)
    preview_api = proof_pack_preview_api_url(normalized_target, question, normalized_pack, source)
    quote_api = (
        f"{PUBLIC_BASE_URL}/v1/proof-pack/quote?"
        f"target_url={url_quote(normalized_target, safe='')}"
        f"&question={url_quote(question, safe='')}"
        f"&pack={url_quote(normalized_pack, safe='')}"
        f"&source={url_quote(source, safe='')}"
    )
    lead = {
        "id": lead_id,
        "created_at": created_at,
        "contact": contact,
        "target_url": normalized_target,
        "question": question,
        "pack": normalized_pack,
        "use_case": clean_lead_text(raw.get("use_case"), 240),
        "budget_usdc": clean_lead_text(raw.get("budget_usdc"), 120),
        "notes": clean_lead_text(raw.get("notes"), 500),
        "source": source,
        "price_usdc": float(price),
        "amount_units": str(usdc_units(price)),
        "request_page": proof_pack_request_page_url(normalized_target, question, normalized_pack, source),
        "preview_page": preview_page,
        "preview_api": preview_api,
        "quote_page": quote_page,
        "quote_api": quote_api,
        "payment_probe_url": proof_pack_payment_probe_url(normalized_pack, source),
        "paid_endpoint": f"{PUBLIC_BASE_URL}/v1/x402/proof-pack?pack={normalized_pack}&source={source}",
        "buyer_command": proof_pack_buyer_command(normalized_target, question, normalized_pack, source),
    }
    lead["storage_backend"] = await store_proof_pack_lead(lead)
    inc_metric("proof_pack_leads_total")
    inc_attribution("proof_pack_leads", source)
    schedule_background(send_proof_pack_lead_notification(lead))
    return lead


def proof_bundle_lead_public_response(lead: dict[str, Any]) -> dict[str, Any]:
    """Return the bundle lead acknowledgement without echoing contact details."""
    return {
        "status": "received",
        "lead_id": lead["id"],
        "product": "proof_bundle",
        "target_urls": lead["target_urls"],
        "target_count": lead["target_count"],
        "question": lead["question"],
        "bundle": lead["bundle"],
        "price_usdc": lead["price_usdc"],
        "amount_units": lead["amount_units"],
        "source": lead["source"],
        "contact_received": bool(lead.get("contact")),
        "next_steps": {
            "bundle_page": public_url("/proof-pack/bundle"),
            "quote_page": lead["quote_page"],
            "quote_api": lead["quote_api"],
            "request_page": lead["request_page"],
            "payment_url": lead["payment_url"],
            "checkout_url": lead.get("checkout_url") or lead["payment_url"],
            "payment_link_configured": lead["payment_link_configured"],
            "operator_leads": public_url("/operator/leads"),
            "single_source_proof_pack_endpoint": lead["paid_endpoint"],
        },
    }


async def create_proof_bundle_lead(payload: ProofBundleLeadRequest | dict[str, Any], request: Request) -> dict[str, Any]:
    """Validate and store buyer intent for a multi-source Proof Bundle."""
    if isinstance(payload, BaseModel):
        raw = payload.model_dump() if hasattr(payload, "model_dump") else payload.dict()
    else:
        raw = dict(payload)

    contact = clean_lead_text(str(raw.get("contact") or ""), 180)
    if len(contact) < 3:
        raise PaymentValidationError("Add an email, handle, or reply path so we can follow up.")

    normalized_bundle = normalize_proof_bundle(str(raw.get("bundle") or DEFAULT_PROOF_BUNDLE))
    raw_targets = split_bundle_target_urls(raw.get("target_urls") or raw.get("target_url"))
    normalized_targets = await validate_proof_bundle_targets(raw_targets, normalized_bundle)
    for normalized_target in normalized_targets:
        await enforce_rate_limit(
            "proof_bundle_lead_target_domain",
            target_domain_identifier(normalized_target),
            RATE_LIMIT_TARGET_DOMAIN,
        )

    question = proof_bundle_question(raw.get("question"))
    source = normalize_attribution_source(str(raw.get("source") or attribution_source_from_request(request)))
    quote = await build_proof_bundle_quote(normalized_targets, question, normalized_bundle, source)
    created_at = int(time.time())
    lead_id = stable_hash(
        json.dumps(
            {
                "created_at": created_at,
                "contact": contact,
                "target_urls": normalized_targets,
                "question": question,
                "nonce": secrets.token_hex(4),
            },
            sort_keys=True,
        )
    )[:16]

    lead = {
        "id": lead_id,
        "created_at": created_at,
        "product": "proof_bundle",
        "contact": contact,
        "target_url": normalized_targets[0],
        "target_urls": normalized_targets,
        "target_count": len(normalized_targets),
        "question": question,
        "bundle": normalized_bundle,
        "pack": normalized_bundle,
        "use_case": clean_lead_text(raw.get("use_case"), 240),
        "budget_usdc": clean_lead_text(raw.get("budget_usdc"), 120),
        "notes": clean_lead_text(raw.get("notes"), 500),
        "source": source,
        "price_usdc": quote["price_usdc"],
        "amount_units": quote["amount_units"],
        "request_page": quote["next_steps"]["request_page"],
        "quote_page": quote["next_steps"]["quote_page"],
        "quote_api": quote["next_steps"]["quote_api"],
        "checkout_url": quote["next_steps"]["checkout_url"],
        "payment_url": quote["next_steps"]["payment_url"],
        "payment_link_configured": quote["next_steps"]["payment_link_configured"],
        "paid_endpoint": quote["next_steps"]["single_source_proof_pack_endpoint"],
        "buyer_command": (
            "Proof Bundle request captured. Use the payment URL when configured, "
            "or run single-source Proof Packs through the x402 endpoint for immediate paid delivery."
        ),
        "source_profiles": quote["source_profiles"],
    }
    lead["storage_backend"] = await store_proof_pack_lead(lead)
    inc_metric("proof_bundle_leads_total")
    inc_attribution("proof_bundle_leads", source)
    schedule_background(send_proof_pack_lead_notification(lead))
    return lead


def contact_public_response(lead: dict[str, Any]) -> dict[str, Any]:
    """Acknowledge a contact inquiry without echoing private message text."""
    return {
        "status": "received",
        "lead_id": lead["id"],
        "product": "contact",
        "contact_received": bool(lead.get("contact")),
        "source": lead.get("source"),
        "next_steps": {
            "contact_page": public_url("/contact"),
            "proof_pack": public_url("/proof-pack"),
            "proof_bundle": public_url("/proof-pack/bundle"),
            "docs": public_url("/docs"),
        },
    }


async def send_contact_notification(lead: dict[str, Any]) -> None:
    """Email the operator when a contact inquiry arrives and email is configured."""
    if not CONTACT_NOTIFY_EMAIL:
        return
    if not EMAIL_DELIVERY_ENABLED or EMAIL_PROVIDER != "resend" or not RESEND_API_KEY or not EMAIL_FROM:
        return

    name = clean_lead_text(str(lead.get("name") or ""), 120) or "AxonGate visitor"
    email_address = clean_lead_text(str(lead.get("contact") or ""), 180)
    company = clean_lead_text(str(lead.get("company") or ""), 160)
    use_case = clean_lead_text(str(lead.get("use_case") or ""), 240)
    message = clean_lead_text(str(lead.get("notes") or ""), 1200)
    subject = f"AxonGate contact: {name}"
    text = (
        f"New AxonGate contact inquiry\n\n"
        f"Name: {name}\n"
        f"Email: {email_address}\n"
        f"Company: {company or 'not provided'}\n"
        f"Use case: {use_case or 'not provided'}\n"
        f"Source: {lead.get('source')}\n"
        f"Lead ID: {lead.get('id')}\n\n"
        f"Message:\n{message}\n\n"
        f"Operator inbox: {public_url('/operator/leads')}"
    )
    html_body = f"""<!doctype html>
<html lang="en">
<body style="margin:0;background:#f8fafc;color:#111827;font-family:Arial,sans-serif">
  <div style="max-width:640px;margin:0 auto;padding:24px">
    <h1 style="font-size:22px;margin:0 0 16px">New AxonGate contact inquiry</h1>
    <p><strong>Name:</strong> {html.escape(name)}</p>
    <p><strong>Email:</strong> {html.escape(email_address)}</p>
    <p><strong>Company:</strong> {html.escape(company or "not provided")}</p>
    <p><strong>Use case:</strong> {html.escape(use_case or "not provided")}</p>
    <p><strong>Source:</strong> {html.escape(str(lead.get("source") or ""))}</p>
    <p><strong>Lead ID:</strong> {html.escape(str(lead.get("id") or ""))}</p>
    <h2 style="font-size:16px;margin-top:22px">Message</h2>
    <p style="white-space:pre-wrap;color:#374151">{html.escape(message)}</p>
    <p><a href="{html.escape(public_url('/operator/leads'), quote=True)}" style="color:#0f766e">Open operator inbox</a></p>
  </div>
</body>
</html>"""
    resend_payload: dict[str, Any] = {
        "from": EMAIL_FROM,
        "to": [CONTACT_NOTIFY_EMAIL],
        "subject": subject,
        "text": text,
        "html": html_body,
    }
    if email_address:
        resend_payload["reply_to"] = email_address

    try:
        await send_resend_email(resend_payload)
        clear_email_delivery_error()
        inc_metric("contact_notifications_total")
    except Exception as exc:
        detail, status_code = describe_email_delivery_exception(exc)
        record_email_delivery_error(detail, status_code)
        inc_metric("contact_notification_errors_total")
        print(f"[CONTACT] Notification delivery failed: {detail}")


async def create_contact_lead(payload: ContactRequest | dict[str, Any], request: Request) -> dict[str, Any]:
    """Validate and store a general public inquiry."""
    if isinstance(payload, BaseModel):
        raw = payload.model_dump() if hasattr(payload, "model_dump") else payload.dict()
    else:
        raw = dict(payload)

    email_address = normalize_recovery_email(raw.get("email"))
    if not email_address:
        raise PaymentValidationError("Enter a valid email address so we can reply.")
    message = clean_lead_text(raw.get("message"), 1200)
    if len(message) < 8:
        raise PaymentValidationError("Add a short message so we know how to help.")

    source = normalize_attribution_source(str(raw.get("source") or attribution_source_from_request(request) or "contact"))
    name = clean_lead_text(raw.get("name"), 120)
    company = clean_lead_text(raw.get("company"), 160)
    use_case = clean_lead_text(raw.get("use_case"), 240)
    created_at = int(time.time())
    lead_id = stable_hash(
        json.dumps(
            {
                "created_at": created_at,
                "email": email_address,
                "message": message,
                "nonce": secrets.token_hex(4),
            },
            sort_keys=True,
        )
    )[:16]
    lead = {
        "id": lead_id,
        "created_at": created_at,
        "product": "contact",
        "contact": email_address,
        "name": name,
        "company": company,
        "target_url": "",
        "target_urls": [],
        "target_count": 0,
        "question": "General inquiry",
        "pack": "contact",
        "use_case": use_case,
        "budget_usdc": "",
        "notes": message,
        "source": source,
        "price_usdc": 0,
        "amount_units": "0",
        "request_page": public_url("/contact"),
        "quote_page": public_url("/proof-pack/quote"),
        "quote_api": public_url("/v1/proof-pack/quote"),
        "payment_url": "",
        "buyer_command": "Reply to this inquiry and route the buyer to a Source Trust Report or Evidence Bundle if relevant.",
    }
    lead["storage_backend"] = await store_proof_pack_lead(lead)
    inc_metric("contact_form_submits_total")
    inc_attribution("contact_form_submits", source)
    schedule_background(send_contact_notification(lead))
    return lead


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


def shared_ui_css() -> str:
    """Return shared styling for the public HTML surfaces."""
    return """
    :root {
      --focus: #9ee7d5;
      --shadow: 0 18px 42px rgba(0, 0, 0, 0.22);
    }
    html { min-width: 0; }
    body {
      text-rendering: optimizeLegibility;
      -webkit-font-smoothing: antialiased;
    }
    main, section, article, header, nav, .grid, .row, .box, .card, .panel, .notice {
      min-width: 0;
    }
    main {
      width: min(100%, var(--page-max, 1080px));
      padding-inline: clamp(16px, 4vw, 28px);
    }
    h1, h2, h3, p, li, td, th, label, small, a, code, pre {
      overflow-wrap: anywhere;
    }
    h1 { letter-spacing: 0; }
    p { max-width: 78ch; }
    a { text-underline-offset: 3px; }
    a:focus-visible, button:focus-visible, input:focus-visible, select:focus-visible, textarea:focus-visible, summary:focus-visible {
      outline: 2px solid var(--focus);
      outline-offset: 3px;
    }
    .site-nav {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      margin: 0 0 30px;
      padding-bottom: 16px;
      border-bottom: 1px solid var(--line);
    }
    .brand {
      display: inline-grid;
      gap: 1px;
      color: var(--text);
      font-weight: 700;
      line-height: 1.1;
      text-decoration: none;
    }
    .brand small {
      color: var(--muted);
      font-size: 0.78rem;
      font-weight: 500;
    }
    .nav-core {
      display: flex;
      flex-wrap: wrap;
      justify-content: flex-end;
      gap: 8px;
      min-width: 0;
    }
    .nav-core a,
    .button,
    button {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 40px;
      max-width: 100%;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 9px 12px;
      background: color-mix(in srgb, var(--panel), transparent 8%);
      color: var(--text);
      font: inherit;
      font-weight: 600;
      line-height: 1.15;
      text-decoration: none;
      cursor: pointer;
      white-space: normal;
      text-align: center;
    }
    .nav-core a:hover,
    .button:hover,
    button:hover {
      border-color: color-mix(in srgb, var(--accent), var(--line) 35%);
      background: color-mix(in srgb, var(--accent), var(--panel) 88%);
      text-decoration: none;
    }
    .nav-core a[aria-current="page"],
    .button.primary {
      border-color: color-mix(in srgb, var(--accent), var(--line) 25%);
      background: color-mix(in srgb, var(--accent), var(--panel) 78%);
      color: var(--text);
    }
    .button.secondary {
      color: var(--text);
    }
    .stripe-button {
      border-color: #635bff !important;
      background: #635bff !important;
      color: #ffffff !important;
      font-weight: 800;
    }
    .stripe-button:hover {
      border-color: #7a73ff !important;
      background: #5147e8 !important;
      color: #ffffff !important;
    }
    .stripe-note {
      display: block;
      margin-top: 8px;
      color: var(--muted);
      font-size: 0.88rem;
    }
    .action-row {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 10px;
      margin: 22px 0;
    }
    .ui-menu {
      position: relative;
      min-width: 180px;
    }
    .ui-menu summary {
      list-style: none;
    }
    .ui-menu summary::-webkit-details-marker {
      display: none;
    }
    .ui-menu summary::after {
      content: "v";
      margin-left: 8px;
      color: var(--muted);
      font-size: 0.8rem;
    }
    .menu-panel {
      position: absolute;
      z-index: 10;
      right: 0;
      top: calc(100% + 8px);
      display: grid;
      gap: 6px;
      width: min(320px, calc(100vw - 32px));
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      box-shadow: var(--shadow);
    }
    .menu-panel a {
      display: block;
      padding: 9px 10px;
      border-radius: 6px;
      color: var(--text);
      text-decoration: none;
    }
    .menu-panel a:hover {
      background: color-mix(in srgb, var(--accent), var(--panel) 86%);
      text-decoration: none;
    }
    .link-cluster {
      display: grid;
      gap: 10px;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      margin: 18px 0;
    }
    .link-cluster details {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      padding: 0;
    }
    .link-cluster summary {
      cursor: pointer;
      padding: 12px 14px;
      color: var(--text);
      font-weight: 700;
    }
    .link-cluster .menu-panel {
      position: static;
      width: auto;
      border: 0;
      border-top: 1px solid var(--line);
      border-radius: 0;
      box-shadow: none;
      background: transparent;
    }
    input, select, textarea {
      max-width: 100%;
    }
    button {
      width: auto;
    }
    .box, .card, .panel, .notice {
      box-shadow: none;
    }
    code {
      white-space: normal;
      word-break: break-word;
    }
    .endpoint-card code {
      overflow: visible;
      text-overflow: clip;
      white-space: normal;
    }
    pre {
      max-width: 100%;
      white-space: pre-wrap;
      word-break: break-word;
      overflow-x: auto;
    }
    .table-wrap {
      max-width: 100%;
      overflow-x: auto;
      border: 1px solid var(--line);
      border-radius: 8px;
    }
    .table-wrap table {
      min-width: 620px;
      border: 0;
    }
    table {
      max-width: 100%;
    }
    th, td {
      vertical-align: top;
    }
    .compact-copy {
      max-width: 68ch;
    }
    .developer-details {
      margin-top: 28px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      padding: 0 16px 16px;
    }
    .developer-details summary {
      cursor: pointer;
      color: var(--text);
      font-weight: 800;
      padding: 14px 0;
    }
    .developer-details h2 {
      margin-top: 22px;
    }
    .ag-proof-section {
      margin: 38px 0;
    }
    .ag-proof-section h2 {
      margin-top: 0;
    }
    .ag-section-head {
      display: grid;
      gap: 8px;
      margin-bottom: 16px;
    }
    .ag-section-head p {
      margin: 0;
      color: var(--muted);
    }
    .ag-decision-section {
      display: grid;
      gap: 18px;
      grid-template-columns: minmax(0, 1fr) minmax(300px, 0.82fr);
      align-items: stretch;
    }
    .ag-metric-grid {
      display: grid;
      gap: 10px;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      margin-top: 18px;
    }
    .ag-metric {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      background: color-mix(in srgb, var(--panel), var(--bg) 18%);
      min-width: 0;
    }
    .ag-metric strong {
      display: block;
      color: var(--text);
      font-size: 1.4rem;
      line-height: 1.1;
    }
    .ag-metric span {
      color: var(--muted);
      font-size: 0.86rem;
    }
    .ag-mini-report,
    .ag-sample-card,
    .ag-compare-card,
    .ag-promise-card {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: color-mix(in srgb, var(--panel), var(--bg) 12%);
      padding: 16px;
      min-width: 0;
    }
    .ag-mini-report {
      display: grid;
      gap: 13px;
    }
    .ag-report-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }
    .ag-report-head strong,
    .ag-sample-card strong,
    .ag-compare-card strong,
    .ag-promise-card strong {
      color: var(--text);
    }
    .ag-score {
      display: inline-grid;
      place-items: center;
      width: 58px;
      aspect-ratio: 1;
      border: 6px solid color-mix(in srgb, var(--accent), var(--panel) 45%);
      border-right-color: var(--line);
      border-radius: 50%;
      color: var(--text);
      font-weight: 900;
    }
    .ag-status {
      display: inline-flex;
      align-items: center;
      min-height: 28px;
      width: fit-content;
      border: 1px solid color-mix(in srgb, var(--accent), var(--line) 25%);
      border-radius: 999px;
      padding: 4px 9px;
      color: var(--accent);
      font-size: 0.76rem;
      font-weight: 900;
      text-transform: uppercase;
    }
    .ag-mini-list {
      display: grid;
      gap: 8px;
      margin: 0;
      padding: 0;
      list-style: none;
    }
    .ag-mini-list li {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 9px 10px;
      background: color-mix(in srgb, var(--bg), var(--panel) 28%);
    }
    .ag-comparison-grid {
      display: grid;
      gap: 12px;
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
    .ag-compare-card {
      border-left: 5px solid var(--line);
    }
    .ag-compare-card.ag-strong {
      border-left-color: var(--accent);
    }
    .ag-compare-card span,
    .ag-sample-card span,
    .ag-promise-card span {
      display: block;
      margin-bottom: 6px;
      color: var(--accent);
      font-size: 0.75rem;
      font-weight: 900;
      text-transform: uppercase;
    }
    .ag-sample-gallery,
    .ag-promise-grid {
      display: grid;
      gap: 12px;
      grid-template-columns: repeat(3, minmax(0, 1fr));
    }
    .ag-sample-card p,
    .ag-compare-card p,
    .ag-promise-card p {
      margin: 8px 0 0;
    }
    .ag-sample-card a {
      display: inline-flex;
      margin-top: 12px;
      font-weight: 800;
    }
    .site-footer {
      margin-top: 52px;
      padding-top: 28px;
      border-top: 1px solid var(--line);
      color: var(--muted);
    }
    .footer-grid {
      display: grid;
      gap: 22px;
      grid-template-columns: minmax(220px, 1.2fr) repeat(3, minmax(150px, 1fr));
      align-items: start;
    }
    .footer-brand {
      display: grid;
      gap: 8px;
    }
    .footer-brand strong,
    .footer-group strong {
      color: var(--text);
    }
    .footer-brand p {
      margin: 0;
      color: var(--muted);
    }
    .footer-group {
      display: grid;
      gap: 8px;
    }
    .footer-group a {
      color: var(--muted);
      text-decoration: none;
    }
    .footer-group a:hover {
      color: var(--accent);
      text-decoration: underline;
    }
    .footer-fine {
      margin-top: 22px;
      color: var(--muted);
      font-size: 0.86rem;
    }
    @media (max-width: 760px) {
      .site-nav {
        align-items: flex-start;
        flex-direction: column;
      }
      .nav-core {
        justify-content: flex-start;
        width: 100%;
      }
      .nav-core a,
      .action-row > .button,
      .action-row > .ui-menu {
        flex: 1 1 150px;
      }
      .ui-menu {
        position: static;
      }
      .action-row > .ui-menu,
      .nav-core > .ui-menu {
        flex-basis: 100%;
        width: 100%;
      }
      .menu-panel {
        position: static;
        left: 0;
        right: auto;
        width: 100%;
        margin-top: 8px;
        box-shadow: none;
      }
      .footer-grid {
        grid-template-columns: 1fr;
      }
      .ag-decision-section,
      .ag-comparison-grid,
      .ag-sample-gallery,
      .ag-promise-grid {
        grid-template-columns: 1fr;
      }
      .ag-metric-grid {
        grid-template-columns: 1fr;
      }
    }
    """


def seo_meta_html(title: str, description: str, path: str = "/proof-pack", *, schema_type: str = "Service") -> str:
    """Render indexable metadata for public marketing and docs pages."""
    clean_title = clean_lead_text(title, 120)
    clean_description = clean_lead_text(description, 240)
    canonical = public_url(path)
    schema: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": schema_type,
        "name": clean_title,
        "url": canonical,
        "description": clean_description,
        "isPartOf": {
            "@type": "WebSite",
            "name": "AxonGate",
            "url": public_url("/proof-pack"),
        },
    }
    if schema_type == "Service":
        schema.update(
            {
                "provider": {
                    "@type": "Organization",
                    "name": "AxonGate",
                    "url": public_url("/about"),
                },
                "applicationCategory": "DeveloperApplication",
                "offers": {
                    "@type": "AggregateOffer",
                    "priceCurrency": "USD",
                    "lowPrice": str(PROOF_PACK_PRICING_USDC.get("quick", Decimal("0.10"))),
                    "highPrice": str(PROOF_BUNDLE_PRICING_USDC.get("audit", Decimal("20.00"))),
                },
            }
        )
    schema_json = json.dumps(schema, separators=(",", ":")).replace("</", "<\\/")
    return f"""<title>{html.escape(clean_title)}</title>
  <meta name="description" content="{html.escape(clean_description, quote=True)}">
  <meta name="robots" content="index,follow,max-image-preview:large">
  <link rel="canonical" href="{html.escape(canonical, quote=True)}">
  <meta property="og:site_name" content="AxonGate">
  <meta property="og:type" content="website">
  <meta property="og:title" content="{html.escape(clean_title, quote=True)}">
  <meta property="og:description" content="{html.escape(clean_description, quote=True)}">
  <meta property="og:url" content="{html.escape(canonical, quote=True)}">
  <meta name="twitter:card" content="summary">
  <meta name="twitter:title" content="{html.escape(clean_title, quote=True)}">
  <meta name="twitter:description" content="{html.escape(clean_description, quote=True)}">
  <script type="application/ld+json">{schema_json}</script>"""


def site_footer_html() -> str:
    """Render shared footer links for public pages."""
    contact_email = PUBLIC_CONTACT_EMAIL or "reports@axongate.one"
    return f"""
    <footer class="site-footer">
      <div class="footer-grid">
        <div class="footer-brand">
          <strong>AxonGate</strong>
          <p>Fast evidence checks for teams that need AI agents to cite public sources with confidence.</p>
        </div>
        <div class="footer-group">
          <strong>Buy</strong>
          <a href="{html.escape(public_url('/'), quote=True)}">Start Here</a>
          <a href="{html.escape(public_url('/proof-pack/quote'), quote=True)}">Instant Quote</a>
          <a href="{html.escape(public_url('/proof-pack/bundle'), quote=True)}">Evidence Bundles</a>
          <a href="{html.escape(public_url('/contact'), quote=True)}">Talk To Us</a>
        </div>
        <div class="footer-group">
          <strong>Learn</strong>
          <a href="{html.escape(public_url('/proof-pack/sample'), quote=True)}">Sample Report</a>
          <a href="{html.escape(public_url('/faq'), quote=True)}">FAQ</a>
          <a href="{html.escape(public_url('/about'), quote=True)}">About</a>
          <a href="{html.escape(public_url('/proof-pack/bundle/recover'), quote=True)}">Recover Delivery</a>
        </div>
        <div class="footer-group">
          <strong>Build</strong>
          <a href="{html.escape(public_url('/docs'), quote=True)}">API Docs</a>
          <a href="{html.escape(public_url('/quickstart'), quote=True)}">x402 Quickstart</a>
          <a href="{html.escape(public_url('/discovery/resources'), quote=True)}">Discovery JSON</a>
          <a href="mailto:{html.escape(contact_email, quote=True)}">Email</a>
        </div>
      </div>
      <div class="footer-fine">AxonGate checks public source evidence before agents cite, ingest, or act on it.</div>
    </footer>"""


def ui_link(label: str, href: Any, *, class_name: str = "button secondary", current: bool = False) -> str:
    """Render a safely escaped link button."""
    safe_href = html.escape(html.unescape(str(href or "#")), quote=True)
    safe_label = html.escape(str(label))
    current_attr = ' aria-current="page"' if current else ""
    class_attr = f' class="{html.escape(class_name)}"' if class_name else ""
    return f'<a href="{safe_href}"{class_attr}{current_attr}>{safe_label}</a>'


def stripe_button_html(href: Any, label: str = "Pay with Stripe") -> str:
    """Render a Stripe checkout button for bundle purchases."""
    return ui_link(label, href, class_name="button stripe-button")


def site_nav_html(active: str = "") -> str:
    """Render the compact shared top navigation."""
    nav_items = [
        ("Home", public_url("/"), "Home"),
        ("Evidence Bundles", public_url("/proof-pack/bundle"), "Bundles"),
        ("Sample", public_url("/proof-pack/sample"), "Sample"),
        ("Contact", public_url("/contact"), "Contact"),
        ("Developers", public_url("/docs"), "Docs"),
    ]
    nav_links = "\n        ".join(
        ui_link(
            label,
            href,
            class_name="",
            current=label.lower() == active.lower() or key.lower() == active.lower(),
        )
        for label, href, key in nav_items
    )
    more_links = "\n          ".join(
        ui_link(label, href, class_name="")
        for label, href in [
            ("Instant Quote", public_url("/proof-pack/quote")),
            ("Request Report", public_url("/proof-pack/request")),
            ("Recover Delivery", public_url("/proof-pack/bundle/recover")),
            ("Agent API / x402", public_url("/proof-pack")),
            ("Quickstart", public_url("/quickstart")),
            ("About", public_url("/about")),
            ("FAQ", public_url("/faq")),
            ("Discovery JSON", public_url("/discovery/resources")),
        ]
    )
    return f"""
    <nav class="site-nav" aria-label="Main navigation">
      <a class="brand" href="{html.escape(public_url('/'), quote=True)}">AxonGate<small>source evidence checks</small></a>
      <div class="nav-core">
        {nav_links}
        <details class="ui-menu">
          <summary class="button secondary">More</summary>
          <div class="menu-panel">{more_links}</div>
        </details>
      </div>
    </nav>"""


def action_bar_html(
    primary: list[tuple[str, Any]],
    secondary: Optional[list[tuple[str, Any]]] = None,
    *,
    menu_label: str = "More actions",
) -> str:
    """Render primary actions with secondary links tucked into a dropdown."""
    secondary = secondary or []
    links = []
    for index, (label, href) in enumerate(primary):
        links.append(ui_link(label, href, class_name="button primary" if index == 0 else "button secondary"))
    if secondary:
        menu_links = "\n          ".join(ui_link(label, href, class_name="") for label, href in secondary)
        links.append(
            f"""
        <details class="ui-menu">
          <summary class="button secondary">{html.escape(menu_label)}</summary>
          <div class="menu-panel">{menu_links}</div>
        </details>"""
        )
    return '<div class="action-row">' + "\n      ".join(links) + "</div>"


def link_cluster_html(groups: list[tuple[str, list[tuple[str, Any]]]]) -> str:
    """Render grouped documentation links without flooding the page."""
    rendered_groups = []
    for title, links in groups:
        menu_links = "\n          ".join(ui_link(label, href, class_name="") for label, href in links)
        rendered_groups.append(
            f"""
      <details>
        <summary>{html.escape(title)}</summary>
        <div class="menu-panel">{menu_links}</div>
      </details>"""
        )
    return '<section class="link-cluster" aria-label="Discovery links">' + "\n".join(rendered_groups) + "\n    </section>"


def premium_decision_section_html() -> str:
    """Render the high-value evidence decision framing used by buyer pages."""
    return f"""
    <section class="ag-proof-section ag-decision-section" aria-label="Evidence decision preview">
      <div>
        <p class="eyebrow">Evidence decision</p>
        <h2>Not scraped text. A decision your team can act on.</h2>
        <p>AxonGate answers the question a parser skips: can these public sources actually support the claim well enough for an agent, launch page, or RAG workflow to rely on them?</p>
        <div class="ag-metric-grid" aria-label="Evidence bundle outputs">
          <div class="ag-metric"><strong>Support</strong><span>supported, weak, or unsupported</span></div>
          <div class="ag-metric"><strong>Trace</strong><span>citation IDs, excerpts, and source hashes</span></div>
          <div class="ag-metric"><strong>Deliver</strong><span>email, browser report, PDF, and JSON</span></div>
        </div>
      </div>
      <aside class="ag-mini-report" aria-label="Sample AxonGate decision card">
        <div class="ag-report-head">
          <div>
            <span class="ag-status">Decision-ready</span>
            <strong>Launch claim has usable support</strong>
          </div>
          <div class="ag-score">82</div>
        </div>
        <ul class="ag-mini-list">
          <li><strong>Evidence:</strong> two sources directly support the claim.</li>
          <li><strong>Risk:</strong> one page is mostly boilerplate and should not be cited alone.</li>
          <li><strong>Output:</strong> buyer gets a cited report plus PDF and JSON exports.</li>
        </ul>
      </aside>
    </section>
    """


def parser_comparison_html() -> str:
    """Render the parser-vs-AxonGate buyer comparison."""
    return """
    <section class="ag-proof-section" aria-label="Parser comparison">
      <div class="ag-section-head">
        <p class="eyebrow">Why buyers pay</p>
        <h2>Parser returns text. AxonGate returns a decision.</h2>
        <p>Extraction is only the raw material. The paid value is the judgment layer: support, citation quality, source noise, risks, and a delivery artifact the buyer can share.</p>
      </div>
      <div class="ag-comparison-grid">
        <article class="ag-compare-card">
          <span>Plain parser</span>
          <strong>Raw page text</strong>
          <p>Leaves the buyer to decide whether navigation, boilerplate, ads, or vague copy are evidence.</p>
        </article>
        <article class="ag-compare-card ag-strong">
          <span>AxonGate</span>
          <strong>Claim-support decision</strong>
          <p>Labels the evidence as supported, weak, or unsupported and keeps every useful claim tied to citations.</p>
        </article>
        <article class="ag-compare-card">
          <span>Plain parser</span>
          <strong>No buyer artifact</strong>
          <p>A text dump is hard to send to a teammate, customer, or agent workflow without more work.</p>
        </article>
        <article class="ag-compare-card ag-strong">
          <span>AxonGate</span>
          <strong>Ready-to-use report</strong>
          <p>Delivers browser view, email, PDF, JSON, recovery link, source hashes, and risk notes after checkout.</p>
        </article>
      </div>
    </section>
    """


def sample_gallery_html() -> str:
    """Render concrete buyer examples for the public funnel."""
    launch_quote = public_url(
        "/proof-pack/bundle/quote?"
        "target_urls=https%3A%2F%2Fwww.iana.org%2Fdomains%2Freserved%0Ahttps%3A%2F%2Fexample.com"
        "&question=Can%20these%20sources%20support%20our%20launch%20claim%3F"
        "&bundle=scout&source=sample-gallery"
    )
    audit_checkout = public_url(
        "/proof-pack/bundle/checkout?"
        "target_urls=https%3A%2F%2Fwww.iana.org%2Fdomains%2Freserved%0Ahttps%3A%2F%2Fexample.com"
        "&question=Which%20sources%20are%20safe%20to%20cite%3F"
        "&bundle=audit&source=sample-gallery"
    )
    return f"""
    <section class="ag-proof-section" aria-label="Evidence report examples">
      <div class="ag-section-head">
        <p class="eyebrow">Report examples</p>
        <h2>Make the deliverable visible before checkout.</h2>
        <p>Buyers should see that AxonGate sells a defensible evidence workflow, not a hidden scrape. These examples show where the report fits.</p>
      </div>
      <div class="ag-sample-gallery">
        <article class="ag-sample-card">
          <span>Example 1</span>
          <strong>Launch claim check</strong>
          <p>Can our public sources support the claim we are about to publish?</p>
          <a href="{html.escape(launch_quote, quote=True)}">Open quote</a>
        </article>
        <article class="ag-sample-card">
          <span>Example 2</span>
          <strong>RAG source audit</strong>
          <p>Which URLs are substantive enough to ingest, cite, or hand to an agent?</p>
          <a href="{html.escape(audit_checkout, quote=True)}">Review checkout</a>
        </article>
        <article class="ag-sample-card">
          <span>Example 3</span>
          <strong>Single-source sample</strong>
          <p>See the citation IDs, risks, source hash, confidence, and JSON shape.</p>
          <a href="{html.escape(public_url('/proof-pack/sample'), quote=True)}">View sample report</a>
        </article>
      </div>
    </section>
    """


def delivery_promise_html() -> str:
    """Render the checkout delivery promise and buyer reassurance."""
    return """
    <section class="ag-proof-section" aria-label="Delivery promise">
      <div class="ag-section-head">
        <p class="eyebrow">Delivery promise</p>
        <h2>What happens after payment is concrete.</h2>
        <p>The buyer gets a report they can open, recover, export, and forward. Weak evidence stays labeled weak instead of being inflated into a confident answer.</p>
      </div>
      <div class="ag-promise-grid">
        <article class="ag-promise-card">
          <span>1</span>
          <strong>Stripe confirms payment</strong>
          <p>AxonGate stores the paid request and starts the report from the submitted public URLs.</p>
        </article>
        <article class="ag-promise-card">
          <span>2</span>
          <strong>Email and recovery link</strong>
          <p>The report is delivered by email when available, and the buyer can recover it from the site.</p>
        </article>
        <article class="ag-promise-card">
          <span>3</span>
          <strong>PDF and JSON exports</strong>
          <p>Human readers get a clean report, and technical teams get structured output for workflows.</p>
        </article>
      </div>
    </section>
    """


def build_home_html() -> str:
    """Render the customer-facing homepage at the root URL."""
    nav = site_nav_html("Home")
    scout_price = html.escape(str(price_for_proof_bundle("scout")))
    builder_price = html.escape(str(price_for_proof_bundle(DEFAULT_PROOF_BUNDLE)))
    audit_price = html.escape(str(price_for_proof_bundle("audit")))
    scout_limit = html.escape(str(proof_bundle_source_limit("scout")))
    builder_limit = html.escape(str(proof_bundle_source_limit(DEFAULT_PROOF_BUNDLE)))
    audit_limit = html.escape(str(proof_bundle_source_limit("audit")))
    bundle_url = html.escape(public_url("/proof-pack/bundle"), quote=True)
    contact_url = html.escape(public_url("/contact"), quote=True)
    sample_url = html.escape(public_url("/proof-pack/sample"), quote=True)
    docs_url = html.escape(public_url("/docs"), quote=True)
    agent_api_url = html.escape(public_url("/proof-pack"), quote=True)
    scout_stripe = proof_bundle_checkout_path([], "", "scout", "home-stripe")
    builder_stripe = proof_bundle_checkout_path([], "", DEFAULT_PROOF_BUNDLE, "home-stripe")
    audit_stripe = proof_bundle_checkout_path([], "", "audit", "home-stripe")
    bundle_options = "\n".join(
        f'<option value="{html.escape(bundle_name)}"{" selected" if bundle_name == DEFAULT_PROOF_BUNDLE else ""}>'
        f'{html.escape(bundle_name.title())} - ${html.escape(str(price_for_proof_bundle(bundle_name)))}</option>'
        for bundle_name in PROOF_BUNDLE_PRICING_USDC
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  {seo_meta_html("AxonGate Evidence Bundles", "Buy cited evidence checks that show whether public sources support an AI product claim before your agent cites, ingests, or acts on them.", "/")}
  <style>
    :root {{
      color-scheme: light dark;
      --bg: #101218;
      --panel: #181b23;
      --panel-strong: #202530;
      --text: #f5f7fb;
      --muted: #b9c2cf;
      --line: #333947;
      --accent: #6ee7b7;
      --price: #f8fafc;
      --price-muted: #a7f3d0;
      --stripe: #635bff;
      --code: #0a0d13;
      --good: #7ee787;
      --bad: #ff9b9b;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.55;
      background:
        linear-gradient(180deg, color-mix(in srgb, var(--panel), var(--bg) 28%) 0, var(--bg) 430px),
        var(--bg);
      color: var(--text);
    }}
    main {{ max-width: 1120px; margin: 0 auto; padding: 44px 22px 72px; }}
    h1 {{ font-size: clamp(2.3rem, 5vw, 4.1rem); line-height: 1.02; margin: 0 0 14px; max-width: 13ch; }}
    h2 {{ margin: 42px 0 14px; font-size: clamp(1.45rem, 3vw, 2rem); line-height: 1.12; }}
    h3 {{ margin: 0 0 8px; font-size: 1.05rem; }}
    p, li, label, small {{ color: var(--muted); }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .hero {{
      display: grid;
      gap: clamp(22px, 4vw, 42px);
      grid-template-columns: minmax(0, 1.05fr) minmax(320px, .95fr);
      align-items: center;
      margin: 0 0 34px;
    }}
    .eyebrow {{
      margin: 0 0 10px;
      color: var(--accent);
      font-size: .78rem;
      font-weight: 800;
      text-transform: uppercase;
    }}
    .summary {{ max-width: 680px; font-size: 1.11rem; }}
    .hero-actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin: 22px 0;
    }}
    .bundle-form {{
      display: grid;
      gap: 10px;
      grid-template-columns: minmax(0, 1fr) minmax(220px, .72fr);
      margin: 22px 0 0;
      padding: 14px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: color-mix(in srgb, var(--panel), var(--bg) 18%);
    }}
    .bundle-form label {{ display: grid; gap: 6px; font-size: .9rem; }}
    .bundle-form input, .bundle-form select, .bundle-form textarea {{
      min-height: 44px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px 12px;
      background: var(--bg);
      color: var(--text);
      font: inherit;
    }}
    .bundle-form textarea {{ min-height: 92px; resize: vertical; }}
    .bundle-form .wide {{ grid-column: 1 / -1; }}
    .bundle-form button {{
      grid-column: 1 / -1;
      border-color: color-mix(in srgb, var(--accent), var(--line) 20%);
      background: color-mix(in srgb, var(--accent), var(--panel) 78%);
      font-weight: 800;
    }}
    .report-visual {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: linear-gradient(145deg, var(--panel-strong), var(--panel));
      padding: clamp(16px, 3vw, 24px);
      box-shadow: var(--shadow);
    }}
    .report-top {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 18px;
    }}
    .status-pill {{
      display: inline-flex;
      align-items: center;
      min-height: 30px;
      border: 1px solid color-mix(in srgb, var(--good), var(--line));
      border-radius: 999px;
      padding: 5px 10px;
      color: var(--good);
      font-size: .82rem;
      font-weight: 800;
    }}
    .score-ring {{
      display: grid;
      place-items: center;
      width: 74px;
      aspect-ratio: 1;
      border: 8px solid color-mix(in srgb, var(--accent), var(--panel) 45%);
      border-right-color: var(--line);
      border-radius: 50%;
      color: var(--text);
      font-weight: 900;
    }}
    .report-lines {{ display: grid; gap: 10px; }}
    .evidence-row {{
      display: grid;
      gap: 10px;
      grid-template-columns: 92px minmax(0, 1fr);
      align-items: center;
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: color-mix(in srgb, var(--bg), var(--panel) 32%);
    }}
    .evidence-row strong {{ color: var(--text); }}
    .bar {{
      height: 9px;
      border-radius: 999px;
      background: var(--line);
      overflow: hidden;
    }}
    .bar span {{ display: block; height: 100%; background: var(--accent); }}
    .package-grid, .price-grid, .proof-steps {{
      display: grid;
      gap: 14px;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      margin: 18px 0;
    }}
    .path-card, .price-card, .step {{
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
      background: var(--panel);
    }}
    .path-card strong, .price-card strong, .step strong {{
      display: block;
      margin-bottom: 6px;
      color: var(--text);
      font-size: 1rem;
    }}
    .path-card a, .price-card a {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 38px;
      margin-top: 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 8px 10px;
      color: var(--text);
      font-weight: 750;
    }}
    .path-card:first-child a {{
      border-color: color-mix(in srgb, var(--accent), var(--line) 25%);
      background: color-mix(in srgb, var(--accent), var(--panel) 82%);
    }}
    .price {{
      color: var(--price);
      font-size: 2rem;
      font-weight: 900;
      line-height: 1;
    }}
    .price .currency {{
      color: var(--price-muted);
      font-size: 1.1rem;
      vertical-align: .25rem;
    }}
    .price small {{
      color: var(--muted);
      font-size: .86rem;
      font-weight: 700;
    }}
    .price-card.featured {{
      border-color: color-mix(in srgb, var(--accent), var(--line) 25%);
      background: color-mix(in srgb, var(--panel), var(--accent) 8%);
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      min-height: 28px;
      margin-bottom: 10px;
      border: 1px solid color-mix(in srgb, var(--accent), var(--line) 30%);
      border-radius: 999px;
      padding: 4px 9px;
      color: var(--accent);
      font-size: .78rem;
      font-weight: 900;
    }}
    .agent-strip {{
      display: grid;
      gap: 14px;
      grid-template-columns: minmax(0, 1fr) repeat(3, auto);
      align-items: center;
      margin-top: 28px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
      background: color-mix(in srgb, var(--panel), var(--bg) 20%);
    }}
    code {{
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      background: var(--code);
      color: var(--text);
      border-radius: 4px;
      padding: 2px 5px;
    }}
    @media (max-width: 860px) {{
      .hero, .agent-strip {{ grid-template-columns: 1fr; }}
      .bundle-form, .package-grid, .price-grid, .proof-steps {{ grid-template-columns: 1fr; }}
      h1 {{ max-width: 14ch; }}
    }}
    {shared_ui_css()}
  </style>
</head>
<body>
  <main>
    {nav}
    <section class="hero">
      <div>
        <p class="eyebrow">Evidence checks for AI products</p>
        <h1>Can these sources prove your claim?</h1>
        <p class="summary">AxonGate turns a list of public URLs into a cited evidence report. Use it before your agent cites a page, adds sources to RAG, or repeats a claim to customers.</p>
        <div class="hero-actions">
          {stripe_button_html(builder_stripe, "Start Builder - $7")}
          <a class="button secondary" href="{bundle_url}">Quote With URLs</a>
          <a class="button secondary" href="{sample_url}">View Sample Report</a>
        </div>
        <form class="bundle-form" method="get" action="/proof-pack/bundle/quote" accept-charset="utf-8">
          <label class="wide">Source URLs
            <textarea name="target_urls" placeholder="https://example.com/source&#10;https://example.org/context" required></textarea>
          </label>
          <label>Claim or question to support
            <input name="question" placeholder="Can these sources support our launch claim?">
          </label>
          <label>Bundle
            <select name="bundle" aria-label="Evidence Bundle">{bundle_options}</select>
          </label>
          <input type="hidden" name="source" value="homepage">
          <button type="submit">Get Evidence Quote</button>
        </form>
      </div>
      <aside class="report-visual" aria-label="Sample evidence report preview">
        <div class="report-top">
          <div>
            <p class="eyebrow">Bundle output</p>
            <strong>Claim supported across sources</strong>
          </div>
          <div class="score-ring">82</div>
        </div>
        <div class="report-lines">
          <div class="evidence-row"><strong>Evidence</strong><div class="bar"><span style="width:82%"></span></div></div>
          <div class="evidence-row"><strong>Citations</strong><div class="bar"><span style="width:74%"></span></div></div>
          <div class="evidence-row"><strong>Noise</strong><div class="bar"><span style="width:31%; background:var(--bad)"></span></div></div>
        </div>
        <p><span class="status-pill">Citation-ready</span></p>
        <p>Reports include source quality, risk notes, evidence IDs, JSON, PDF, and recovery links.</p>
      </aside>
    </section>

    {premium_decision_section_html()}
    {parser_comparison_html()}

    <section>
      <h2>Choose the evidence check</h2>
      <div class="package-grid">
        <div class="path-card">
          <strong>Scout</strong>
          <p>Quickly screen a few URLs and see whether the source material is usable.</p>
          <a href="{bundle_url}?bundle=scout&source=homepage">Add URLs</a>
        </div>
        <div class="path-card">
          <strong>Builder</strong>
          <p>Best first purchase for teams preparing source-backed agent workflows.</p>
          <a href="{bundle_url}?bundle={html.escape(DEFAULT_PROOF_BUNDLE)}&source=homepage">Start Builder</a>
        </div>
        <div class="path-card">
          <strong>Audit</strong>
          <p>Use this for launch reviews, higher-risk claims, and broader source sets.</p>
          <a href="{contact_url}">Contact</a>
        </div>
      </div>
    </section>

    <section>
      <h2>Stripe pricing</h2>
      <div class="price-grid">
        <div class="price-card">
          <strong>Scout</strong>
          <div class="price"><span class="currency">$</span>{scout_price} <small>USD</small></div>
          <p>Up to {scout_limit} public URLs. Quick claim-support and source-quality check.</p>
          {stripe_button_html(scout_stripe, "Pay $2 with Stripe")}
          <a href="{bundle_url}?bundle=scout&source=homepage">Quote first</a>
        </div>
        <div class="price-card featured">
          <span class="badge">Best first buy</span>
          <strong>Builder</strong>
          <div class="price"><span class="currency">$</span>{builder_price} <small>USD</small></div>
          <p>Up to {builder_limit} public URLs. Cited evidence bundle for product and agent teams.</p>
          {stripe_button_html(builder_stripe, "Pay $7 with Stripe")}
          <a href="{bundle_url}?bundle={html.escape(DEFAULT_PROOF_BUNDLE)}&source=homepage">Quote first</a>
        </div>
        <div class="price-card">
          <strong>Audit</strong>
          <div class="price"><span class="currency">$</span>{audit_price} <small>USD</small></div>
          <p>Up to {audit_limit} public URLs. Deeper review for launches, vendors, and sensitive claims.</p>
          {stripe_button_html(audit_stripe, "Pay $20 with Stripe")}
          <a href="{bundle_url}?bundle=audit&source=homepage">Quote first</a>
        </div>
      </div>
      <p>Checkout opens Stripe. If you want to add URLs first, use the quote path and the selected bundle stays attached to your request.</p>
    </section>

    {sample_gallery_html()}

    <section>
      <h2>What happens after Stripe</h2>
      <div class="proof-steps">
        <div class="step"><strong>1. Pay or quote</strong><p>Choose Scout, Builder, or Audit. Add URLs before payment or provide them through Stripe custom fields.</p></div>
        <div class="step"><strong>2. Evidence is generated</strong><p>AxonGate checks source support, citation quality, risk, and weak evidence patterns.</p></div>
        <div class="step"><strong>3. Delivery is recoverable</strong><p>Reports are delivered with recovery links, JSON/PDF output, and source metadata.</p></div>
      </div>
    </section>

    <section class="agent-strip" aria-label="Agent API access">
      <div>
        <strong>Need the agent API?</strong>
        <p>Autonomous agents can buy single-source Source Trust Reports through the x402 Proof Pack endpoint.</p>
      </div>
      <a class="button secondary" href="{agent_api_url}">Agent API / x402</a>
      <a class="button secondary" href="{docs_url}">Docs</a>
      <a class="button secondary" href="{sample_url}">Sample</a>
    </section>
    {site_footer_html()}
  </main>
</body>
</html>"""


def build_about_html() -> str:
    """Render the public About page."""
    nav = site_nav_html("About")
    actions = action_bar_html(
        [
            ("Run Trust Check", public_url("/proof-pack")),
            ("Contact", public_url("/contact")),
        ],
        [
            ("Evidence Bundles", public_url("/proof-pack/bundle")),
            ("Docs", public_url("/docs")),
            ("Quickstart", public_url("/quickstart")),
        ],
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  {seo_meta_html("About AxonGate", "AxonGate is an evidence trust layer for AI agents, helping builders decide whether public web sources safely support claims before citation or ingestion.", "/about", schema_type="WebPage")}
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
    h1 {{ font-size: clamp(2.2rem, 4vw, 3.5rem); line-height: 1.05; margin: 0 0 12px; }}
    h2 {{ margin: 38px 0 12px; font-size: 1.3rem; }}
    p, li {{ color: var(--muted); }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .summary {{ max-width: 820px; font-size: 1.08rem; }}
    .grid {{ display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); margin: 22px 0; }}
    .box {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 16px; }}
    .box strong {{ color: var(--text); }}
    {shared_ui_css()}
  </style>
</head>
<body>
  <main>
    {nav}
    <h1>AxonGate helps agents know what they can safely cite.</h1>
    <p class="summary">AI systems increasingly read the public web, but raw page extraction does not tell an agent whether a source is useful, noisy, weak, or unsupported. AxonGate sits between the web and the agent as a trust check for public-source evidence.</p>
    {actions}
    <div class="grid">
      <div class="box"><strong>For agent builders</strong><br><p>Use AxonGate before RAG ingestion, autonomous web actions, customer-facing citations, and launch audits.</p></div>
      <div class="box"><strong>For evidence workflows</strong><br><p>Get cited findings, source hashes, confidence, risks, JSON, PDF, and delivery links instead of unjudged page text.</p></div>
      <div class="box"><strong>For paid automation</strong><br><p>x402 endpoints let agents buy source checks directly, while Stripe links capture bundle demand.</p></div>
    </div>
    <h2>Why this exists</h2>
    <p>Most web tooling answers, "What text is on this page?" AxonGate answers the more valuable question: "Can my agent rely on this source for this claim?" That difference matters when an agent is about to cite a page, update a knowledge base, or produce a customer-visible answer.</p>
    <h2>What AxonGate returns</h2>
    <p>Proof Packs and Evidence Bundles return a trust decision with supported claims, citation IDs, excerpts, confidence, source quality notes, delivery metadata, and risks. The output is built for buyers, operators, and agent workflows.</p>
    {site_footer_html()}
  </main>
</body>
</html>"""


def build_faq_html() -> str:
    """Render a public FAQ page."""
    nav = site_nav_html("FAQ")
    faq_items = [
        (
            "Is AxonGate just a page parser?",
            "No. Page extraction is only the input. AxonGate checks whether the extracted evidence supports a claim, identifies weak or noisy source material, and returns cited findings with confidence and risks.",
        ),
        (
            "Who is AxonGate for?",
            "Agent builders, RAG teams, AI product operators, and technical buyers who need a defensible source check before an agent cites, ingests, or acts on public web evidence.",
        ),
        (
            "What is a Proof Pack?",
            "A Proof Pack is a single-source evidence report. It answers a claim or question using one public URL and returns summary, claims, citations, risks, confidence, source hash, and API metadata.",
        ),
        (
            "What is an Evidence Bundle?",
            "An Evidence Bundle checks several public URLs together. It is better for launch audits, vendor claims, source lists, and buyer requests where one page is not enough.",
        ),
        (
            "How does payment work?",
            "Agent calls use x402 on Base USDC. Bundle requests use Stripe payment links and then deliver the report through a secure delivery page and email when email delivery is configured.",
        ),
        (
            "Can I try it without paying?",
            "Yes. The sample report, quote endpoints, and mini preview help buyers inspect the report shape and price before paid work.",
        ),
        (
            "What URLs are accepted?",
            "AxonGate accepts public HTTP and HTTPS URLs and blocks private, loopback, local, multicast, and unsafe target hosts before supplier work.",
        ),
        (
            "How do I contact AxonGate?",
            "Use the contact form for support, partnerships, custom bundle requests, or API questions. Paid delivery recovery is available from the Evidence Bundle recovery page.",
        ),
    ]
    details = "\n".join(
        f"""
      <details>
        <summary>{html.escape(question)}</summary>
        <p>{html.escape(answer)}</p>
      </details>"""
        for question, answer in faq_items
    )
    actions = action_bar_html(
        [
            ("Run Trust Check", public_url("/proof-pack")),
            ("Contact", public_url("/contact")),
        ],
        [
            ("Sample Report", public_url("/proof-pack/sample")),
            ("Evidence Bundles", public_url("/proof-pack/bundle")),
            ("Docs", public_url("/docs")),
        ],
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  {seo_meta_html("AxonGate FAQ", "Answers about AxonGate source trust reports, Proof Packs, Evidence Bundles, x402 payments, Stripe delivery, accepted URLs, and contact options.", "/faq", schema_type="WebPage")}
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
    main {{ max-width: 980px; margin: 0 auto; padding: 44px 22px 72px; }}
    h1 {{ font-size: clamp(2.2rem, 4vw, 3.5rem); line-height: 1.05; margin: 0 0 12px; }}
    p {{ color: var(--muted); }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .summary {{ max-width: 760px; font-size: 1.08rem; }}
    .faq-list {{ display: grid; gap: 10px; margin: 24px 0; }}
    .faq-list details {{ border: 1px solid var(--line); border-radius: 8px; background: var(--panel); padding: 0; }}
    .faq-list summary {{ cursor: pointer; padding: 14px 16px; color: var(--text); font-weight: 700; }}
    .faq-list p {{ margin: 0; padding: 0 16px 16px; }}
    {shared_ui_css()}
  </style>
</head>
<body>
  <main>
    {nav}
    <h1>FAQ</h1>
    <p class="summary">Short answers for buyers and builders evaluating AxonGate as a source trust layer for AI agents.</p>
    {actions}
    <section class="faq-list" aria-label="Frequently asked questions">
      {details}
    </section>
    {site_footer_html()}
  </main>
</body>
</html>"""


def build_contact_html(
    values: Optional[dict[str, Any]] = None,
    submitted: Optional[dict[str, Any]] = None,
    error: Optional[str] = None,
) -> str:
    """Render a public contact form."""
    values = values or {}
    nav = site_nav_html("Contact")
    contact_email = PUBLIC_CONTACT_EMAIL or "reports@axongate.one"
    name = clean_lead_text(values.get("name"), 120)
    email_address = clean_lead_text(values.get("email"), 180)
    company = clean_lead_text(values.get("company"), 160)
    use_case = clean_lead_text(values.get("use_case"), 240)
    message = clean_lead_text(values.get("message"), 1200)
    source = normalize_attribution_source(str(values.get("source") or "contact-page"))
    success_html = ""
    if submitted:
        success_html = f"""
    <div class="notice success">
      <strong>Message received</strong><br>
      Thanks. Your inquiry was saved as <code>{html.escape(str(submitted.get("lead_id") or ""))}</code>. We will reply to the email you entered.
    </div>"""
    error_html = (
        f'<div class="notice error"><strong>Message not sent</strong><br>{html.escape(error)}</div>'
        if error
        else ""
    )
    actions = action_bar_html(
        [
            ("Run Trust Check", public_url("/proof-pack")),
            ("Evidence Bundles", public_url("/proof-pack/bundle")),
        ],
        [
            ("FAQ", public_url("/faq")),
            ("Docs", public_url("/docs")),
            ("Recover Delivery", public_url("/proof-pack/bundle/recover")),
        ],
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  {seo_meta_html("Contact AxonGate", "Contact AxonGate for source trust reports, Evidence Bundles, API questions, delivery help, partnerships, or custom agent evidence workflows.", "/contact", schema_type="WebPage")}
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
      --good: #7ee787;
      --bad: #ff9b9b;
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
    h1 {{ font-size: clamp(2.2rem, 4vw, 3.5rem); line-height: 1.05; margin: 0 0 12px; }}
    h2 {{ margin: 34px 0 12px; font-size: 1.25rem; }}
    p, label, small {{ color: var(--muted); }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .summary {{ max-width: 760px; font-size: 1.08rem; }}
    .contact-layout {{ display: grid; grid-template-columns: minmax(0, 1.1fr) minmax(240px, .8fr); gap: 18px; align-items: start; margin-top: 22px; }}
    form {{ display: grid; gap: 13px; }}
    .row {{ display: grid; gap: 12px; grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    label {{ display: grid; gap: 6px; font-size: .9rem; }}
    input, textarea {{
      min-width: 0;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 11px 12px;
      background: var(--panel);
      color: var(--text);
      font: inherit;
    }}
    textarea {{ min-height: 150px; resize: vertical; }}
    button {{
      justify-self: start;
      border: 1px solid var(--accent);
      border-radius: 8px;
      padding: 11px 14px;
      background: color-mix(in srgb, var(--accent), var(--panel) 78%);
      color: var(--text);
      font: inherit;
      font-weight: 700;
      cursor: pointer;
    }}
    .panel, .notice {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 16px; }}
    .success {{ border-color: color-mix(in srgb, var(--good), var(--line)); }}
    .error {{ border-color: color-mix(in srgb, var(--bad), var(--line)); }}
    code {{ font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace; background: var(--code); color: var(--text); border-radius: 4px; padding: 2px 5px; }}
    @media (max-width: 760px) {{
      .contact-layout, .row {{ grid-template-columns: 1fr; }}
    }}
    {shared_ui_css()}
  </style>
</head>
<body>
  <main>
    {nav}
    <h1>Contact AxonGate</h1>
    <p class="summary">Ask about source trust reports, custom Evidence Bundles, delivery recovery, API use, partnerships, or a higher-volume workflow.</p>
    {actions}
    {error_html}
    {success_html}
    <div class="contact-layout">
      <form method="post" action="/contact" accept-charset="utf-8">
        <input type="hidden" name="source" value="{html.escape(source)}">
        <div class="row">
          <label>Name
            <input name="name" value="{html.escape(name)}" autocomplete="name">
          </label>
          <label>Email
            <input name="email" type="email" value="{html.escape(email_address)}" autocomplete="email" required>
          </label>
        </div>
        <div class="row">
          <label>Company or project
            <input name="company" value="{html.escape(company)}" autocomplete="organization">
          </label>
          <label>Use case
            <input name="use_case" value="{html.escape(use_case)}" placeholder="RAG, agent eval, launch audit">
          </label>
        </div>
        <label>Message
          <textarea name="message" required>{html.escape(message)}</textarea>
        </label>
        <button type="submit">Send message</button>
      </form>
      <aside class="panel">
        <h2>Useful links</h2>
        <p>Email: <a href="mailto:{html.escape(contact_email, quote=True)}">{html.escape(contact_email)}</a></p>
        <p>For paid report delivery, use the same email entered at Stripe checkout on the recovery page.</p>
        <p><a href="{html.escape(public_url('/proof-pack/bundle/recover'), quote=True)}">Recover a delivery</a></p>
        <p><a href="{html.escape(public_url('/docs'), quote=True)}">Read API docs</a></p>
      </aside>
    </div>
    {site_footer_html()}
  </main>
</body>
</html>"""


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
    nav = site_nav_html("Docs")
    actions = action_bar_html(
        [
            ("Paid Test", f"{PUBLIC_BASE_URL}/paid-test"),
            ("Proof Packs", f"{PUBLIC_BASE_URL}/proof-pack"),
            ("Docs", f"{PUBLIC_BASE_URL}/docs"),
        ],
        [
            ("Quickstart", f"{PUBLIC_BASE_URL}/quickstart"),
            ("Operator", f"{PUBLIC_BASE_URL}/operator"),
            ("Open Quote JSON", api_url),
        ],
    )

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
    {shared_ui_css()}
  </style>
</head>
<body>
  <main>
    {nav}
    <h1>AxonGate Quote</h1>
    <p>Preview the right paid path before spending. This validates the URL, checks starter/cache availability, and returns exact x402 amounts without supplier work.</p>
    {actions}
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
    <div class="table-wrap">
    <table>
      <thead><tr><th>Tier</th><th>Price</th><th>USDC Units</th><th>Available Now</th><th>Policy</th></tr></thead>
      <tbody>{tiers_rows}</tbody>
    </table>
    </div>
  </main>
</body>
</html>"""


def build_proof_pack_preview_html(preview: dict[str, Any]) -> str:
    """Render a no-spend Proof Pack mini preview page."""
    public = html.escape(PUBLIC_BASE_URL)
    target = html.escape(str(preview["target_url"]))
    question = html.escape(str(preview["question"]))
    pack = str(preview["pack"])
    source = str(preview["source"])
    preview_available = bool(preview["preview_available"])
    status_label = "available from cache" if preview_available else "not cached yet"
    amount_units = html.escape(str(preview["payment"]["full_pack_amount_units"]))
    price = html.escape(str(preview["payment"]["full_pack_amount_usdc"]))
    steps = preview["next_steps"]
    quote_url = html.escape(str(steps["quote_page"]))
    request_url = html.escape(str(steps["request_page"]))
    probe_url = html.escape(str(steps["probe_payment_terms"]))
    preview_api = html.escape(str(steps["preview_api"]))
    paid_endpoint = html.escape(str(steps["paid_endpoint"]))
    buyer_command = html.escape(str(steps["buyer_command"]))
    answer = html.escape(str(preview["answer"]))
    executive_summary = html.escape(str(preview["executive_summary"]))
    pack_options = "\n".join(
        f'<option value="{html.escape(pack_name)}"{" selected" if pack_name == pack else ""}>{html.escape(pack_name)}</option>'
        for pack_name in PROOF_PACK_PRICING_USDC
    )
    claim_rows = "\n".join(
        "<tr>"
        f"<td>{html.escape(str(claim['claim']))}</td>"
        f"<td>{html.escape(', '.join(claim.get('citation_ids', [])))}</td>"
        f"<td>{html.escape(str(claim.get('confidence', 0)))}</td>"
        "</tr>"
        for claim in preview["key_claims"]
    ) or '<tr><td colspan="3">No cached claims yet. Use the paid or request path to continue.</td></tr>'
    citation_rows = "\n".join(
        "<tr>"
        f"<td><code>{html.escape(str(citation['id']))}</code></td>"
        f"<td>{html.escape(str(citation['excerpt']))}</td>"
        "</tr>"
        for citation in preview["citations"]
    ) or '<tr><td colspan="2">No cached citations available for this free preview.</td></tr>'
    risk_items = "\n".join(f"<li>{html.escape(str(risk))}</li>" for risk in preview["risks"])
    source_hash = preview["source_profile"].get("content_sha256") or "none"
    source_hash_display = html.escape(str(source_hash)[:20] + ("..." if source_hash and source_hash != "none" else ""))
    nav = site_nav_html("Proof Packs")
    actions = action_bar_html(
        [
            ("Get Quote", quote_url),
            ("Request This Report", request_url),
            ("Probe Payment Terms", probe_url),
        ],
        [
            ("Open Preview JSON", preview_api),
            ("Sample Report", f"{PUBLIC_BASE_URL}/proof-pack/sample"),
            ("Docs", f"{PUBLIC_BASE_URL}/docs"),
        ],
    )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AxonGate Proof Pack Preview</title>
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
    h1 {{ font-size: clamp(2rem, 4vw, 3.2rem); line-height: 1.05; margin: 0 0 12px; }}
    h2 {{ margin: 36px 0 12px; font-size: 1.25rem; }}
    p, label, li, td {{ color: var(--muted); }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    form {{ display: grid; gap: 12px; grid-template-columns: minmax(0, 1.25fr) minmax(0, 1fr) 150px auto; margin: 24px 0; align-items: end; }}
    label {{ display: grid; gap: 6px; font-size: .9rem; }}
    input, select {{
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
    .summary {{ max-width: 780px; font-size: 1.06rem; }}
    .links a, .cta a {{ display: inline-block; margin: 0 12px 10px 0; }}
    .cta a {{ border: 1px solid var(--accent); border-radius: 6px; padding: 10px 12px; }}
    .grid {{ display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); margin: 18px 0; }}
    .box {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 15px; }}
    code, pre {{ font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace; background: var(--code); color: var(--text); }}
    code {{ display: inline-block; max-width: 100%; padding: 2px 5px; border-radius: 4px; overflow-wrap: anywhere; }}
    pre {{ max-width: 100%; overflow-x: auto; white-space: pre; padding: 16px; border: 1px solid var(--line); border-radius: 8px; }}
    table {{ width: 100%; border-collapse: collapse; background: var(--panel); border: 1px solid var(--line); }}
    th, td {{ padding: 10px 11px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }}
    th {{ color: var(--text); }}
    @media (max-width: 760px) {{
      form {{ grid-template-columns: 1fr; }}
    }}
    {shared_ui_css()}
  </style>
</head>
<body>
  <main>
    {nav}
    <h1>Proof Pack Preview</h1>
    <p class="summary">A free, no-spend mini preview. If AxonGate already has reusable source material, this page shows a few cited claims; otherwise it validates the target and gives the fastest paid or request path.</p>
    <form method="get" action="/proof-pack/preview">
      <label>Target URL
        <input name="target_url" value="{target}" aria-label="Target URL">
      </label>
      <label>Question
        <input name="question" value="{question}" aria-label="Question">
      </label>
      <label>Pack
        <select name="pack" aria-label="Proof Pack">{pack_options}</select>
      </label>
      <button type="submit">Preview</button>
    </form>
    {actions}

    <div class="grid">
      <div class="box"><strong>Preview</strong><br>{html.escape(status_label)}<br><code>{html.escape(str(preview["preview_kind"]))}</code></div>
      <div class="box"><strong>Selected Pack</strong><br><code>{html.escape(pack)}</code></div>
      <div class="box"><strong>Full Report</strong><br>{price} USDC<br><code>{amount_units}</code> units</div>
      <div class="box"><strong>Source Hash</strong><br><code>{source_hash_display}</code></div>
    </div>

    <h2>Mini Answer</h2>
    <p>{answer}</p>
    <h2>Mini Summary</h2>
    <p>{executive_summary}</p>
    <h2>Preview Claims</h2>
    <div class="table-wrap">
    <table>
      <thead><tr><th>Claim</th><th>Citations</th><th>Confidence</th></tr></thead>
      <tbody>{claim_rows}</tbody>
    </table>
    </div>
    <h2>Preview Citations</h2>
    <div class="table-wrap">
    <table>
      <thead><tr><th>ID</th><th>Excerpt</th></tr></thead>
      <tbody>{citation_rows}</tbody>
    </table>
    </div>
    <h2>Risks</h2>
    <ul>{risk_items}</ul>
    <h2>Paid Endpoint</h2>
    <pre>{paid_endpoint}</pre>
    <h2>Buyer Command</h2>
    <pre>{buyer_command}</pre>
  </main>
</body>
</html>"""


def build_proof_pack_quote_html(quote: dict[str, Any]) -> str:
    """Render a Proof Pack quote page that turns interest into a paid x402 attempt."""
    public = html.escape(PUBLIC_BASE_URL)
    target = html.escape(str(quote["target_url"]))
    question = html.escape(str(quote["question"]))
    pack = str(quote["pack"])
    price = html.escape(str(quote["price_usdc"]))
    amount_units = html.escape(str(quote["amount_units"]))
    cached_state = "available" if quote["cached_source_available"] else "not available"
    api_url = html.escape(
        f"{PUBLIC_BASE_URL}/v1/proof-pack/quote?"
        f"target_url={url_quote(str(quote['target_url']), safe='')}"
        f"&question={url_quote(str(quote['question']), safe='')}"
        f"&pack={url_quote(pack, safe='')}&source=proof-pack-quote"
    )
    pack_options = "\n".join(
        f'<option value="{html.escape(pack_name)}"{" selected" if pack_name == pack else ""}>{html.escape(pack_name)}</option>'
        for pack_name in PROOF_PACK_PRICING_USDC
    )
    pack_rows = "\n".join(
        "<tr>"
        f"<td><code>{html.escape(pack_name)}</code></td>"
        f"<td>{html.escape(str(info['price_usdc']))} USDC</td>"
        f"<td>{html.escape(str(info['amount_units']))}</td>"
        f"<td>{'yes' if info['cached_source_available'] else 'no'}</td>"
        f"<td>{html.escape(str(info['cache_policy']))}</td>"
        f"<td><a href=\"{html.escape(info['payment_probe_url'])}\">Probe</a></td>"
        "</tr>"
        for pack_name, info in quote["packs"].items()
    )
    buyer_command = html.escape(quote["next_steps"]["buyer_command"])
    probe_url = html.escape(quote["next_steps"]["probe_payment_terms"])
    paid_endpoint = html.escape(quote["next_steps"]["paid_endpoint"])
    short_paid_endpoint = html.escape("POST /v1/x402/proof-pack")
    selector_hint = html.escape(f"pack={pack}, source={quote['source']}")
    request_url = html.escape(proof_pack_request_page_url(str(quote["target_url"]), str(quote["question"]), pack, str(quote["source"])))
    preview_url = html.escape(proof_pack_preview_page_url(str(quote["target_url"]), str(quote["question"]), pack, str(quote["source"])))
    nav = site_nav_html("Proof Packs")
    actions = action_bar_html(
        [
            ("Request This Report", request_url),
            ("Try Mini Preview", preview_url),
            ("Probe Payment Terms", probe_url),
        ],
        [
            ("Open JSON Quote", api_url),
            ("View Sample", f"{PUBLIC_BASE_URL}/proof-pack/sample"),
            ("Docs", f"{PUBLIC_BASE_URL}/docs"),
        ],
    )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AxonGate Proof Pack Quote</title>
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
    h1 {{ font-size: clamp(2rem, 4vw, 3.2rem); line-height: 1.05; margin: 0 0 12px; }}
    h2 {{ margin: 36px 0 12px; font-size: 1.25rem; }}
    p, label, td {{ color: var(--muted); }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    form {{ display: grid; gap: 12px; grid-template-columns: minmax(0, 1.25fr) minmax(0, 1fr) 150px auto; margin: 24px 0; align-items: end; }}
    label {{ display: grid; gap: 6px; font-size: .9rem; }}
    input, select {{
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
    .summary {{ max-width: 780px; font-size: 1.06rem; }}
    .links a, .cta a {{ display: inline-block; margin: 0 12px 10px 0; }}
    .cta a {{ border: 1px solid var(--accent); border-radius: 6px; padding: 10px 12px; }}
    .grid {{ display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); margin: 18px 0; }}
    .box {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 15px; }}
    .endpoint-card span {{ display: block; margin-top: 7px; color: var(--muted); font-size: .92rem; }}
    code, pre {{ font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace; background: var(--code); color: var(--text); }}
    code {{ display: inline-block; max-width: 100%; padding: 2px 5px; border-radius: 4px; overflow-wrap: anywhere; }}
    .endpoint-card code {{ white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
    pre {{ max-width: 100%; overflow-x: auto; white-space: pre; padding: 16px; border: 1px solid var(--line); border-radius: 8px; }}
    table {{ width: 100%; border-collapse: collapse; background: var(--panel); border: 1px solid var(--line); }}
    th, td {{ padding: 10px 11px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }}
    th {{ color: var(--text); }}
    @media (max-width: 760px) {{
      form {{ grid-template-columns: 1fr; }}
    }}
    {shared_ui_css()}
  </style>
</head>
<body>
  <main>
    {nav}
    <h1>Proof Pack Quote</h1>
    <p class="summary">Price a cited evidence report before spending. This page validates the public target, shows exact x402 USDC units, and gives you the next paid command without calling suppliers or the LLM.</p>
    <form method="get" action="/proof-pack/quote">
      <label>Target URL
        <input name="target_url" value="{target}" aria-label="Target URL">
      </label>
      <label>Question
        <input name="question" value="{question}" aria-label="Question">
      </label>
      <label>Pack
        <select name="pack" aria-label="Proof Pack">{pack_options}</select>
      </label>
      <button type="submit">Quote</button>
    </form>
    {actions}

    <div class="grid">
      <div class="box"><strong>Selected Pack</strong><br><code>{html.escape(pack)}</code></div>
      <div class="box"><strong>Price</strong><br>{price} USDC<br><code>{amount_units}</code> units</div>
      <div class="box"><strong>Cached Source</strong><br>{cached_state}</div>
      <div class="box endpoint-card"><strong>Paid Endpoint</strong><br><code>{short_paid_endpoint}</code><span>{selector_hint}</span></div>
    </div>

    <h2>Payment Probe</h2>
    <pre>{probe_url}</pre>
    <h2>Full Paid Endpoint</h2>
    <pre>{paid_endpoint}</pre>
    <h2>Buyer Command</h2>
    <pre>{buyer_command}</pre>
    <h2>Packs</h2>
    <div class="table-wrap">
    <table>
      <thead><tr><th>Pack</th><th>Price</th><th>USDC Units</th><th>Cached</th><th>Policy</th><th>Probe</th></tr></thead>
      <tbody>{pack_rows}</tbody>
    </table>
    </div>
  </main>
</body>
</html>"""


def build_proof_pack_request_html(
    values: Optional[dict[str, Any]] = None,
    submitted: Optional[dict[str, Any]] = None,
    error: Optional[str] = None,
) -> str:
    """Render a no-spend Proof Pack intent capture page."""
    values = values or {}
    public = html.escape(PUBLIC_BASE_URL)
    try:
        pack = normalize_proof_pack(str(values.get("pack") or PROOF_PACK_SAMPLE_PACK))
    except PaymentValidationError:
        pack = PROOF_PACK_SAMPLE_PACK
    source = normalize_attribution_source(str(values.get("source") or "proof-pack-request"))
    target_url = clean_lead_text(values.get("target_url") or PROOF_PACK_SAMPLE_TARGET_URL, 2048)
    question = clean_lead_text(values.get("question") or PROOF_PACK_SAMPLE_QUESTION, 600)
    contact = clean_lead_text(values.get("contact"), 180)
    use_case = clean_lead_text(values.get("use_case"), 240)
    budget_usdc = clean_lead_text(values.get("budget_usdc"), 120)
    notes = clean_lead_text(values.get("notes"), 500)
    price = price_for_proof_pack(pack)
    amount_units = str(usdc_units(price))
    preview_url = html.escape(proof_pack_preview_page_url(target_url, question, pack, source))
    quote_url = html.escape(proof_pack_quote_page_url(target_url, question, pack, source))
    probe_url = html.escape(proof_pack_payment_probe_url(pack, source))
    paid_endpoint = html.escape(f"{PUBLIC_BASE_URL}/v1/x402/proof-pack?pack={pack}&source={source}")
    api_example = html.escape(
        json.dumps(
            {
                "contact": contact or "builder@example.com",
                "target_url": target_url,
                "question": question,
                "pack": pack,
                "use_case": use_case or "RAG evaluation",
                "budget_usdc": budget_usdc or "10/month",
                "source": source,
                "notes": notes,
            },
            indent=2,
        )
    )
    pack_options = "\n".join(
        f'<option value="{html.escape(pack_name)}"{" selected" if pack_name == pack else ""}>{html.escape(pack_name)}</option>'
        for pack_name in PROOF_PACK_PRICING_USDC
    )
    error_html = (
        f'<div class="notice error"><strong>Request not saved</strong><br>{html.escape(error)}</div>'
        if error
        else ""
    )
    success_html = ""
    if submitted:
        steps = submitted.get("next_steps", {})
        success_html = f"""
    <div class="notice success">
      <strong>Request received</strong><br>
      Lead ID <code>{html.escape(str(submitted.get("lead_id")))}</code>. Contact was stored privately for follow-up.
      <div class="cta">
        <a href="{html.escape(str(steps.get("quote_page", quote_url)))}">Open Quote</a>
        <a href="{html.escape(str(steps.get("payment_probe_url", probe_url)))}">Probe Payment Terms</a>
      </div>
    </div>"""
    nav = site_nav_html("Proof Packs")
    actions = action_bar_html(
        [
            ("Open Quote", quote_url),
            ("Try Mini Preview", preview_url),
            ("View Sample", f"{PUBLIC_BASE_URL}/proof-pack/sample"),
        ],
        [
            ("Probe Payment Terms", probe_url),
            ("Docs", f"{PUBLIC_BASE_URL}/docs"),
        ],
    )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AxonGate Proof Pack Request</title>
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
      --good: #7ee787;
      --bad: #ff9b9b;
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
    h1 {{ font-size: clamp(2rem, 4vw, 3.2rem); line-height: 1.05; margin: 0 0 12px; }}
    h2 {{ margin: 36px 0 12px; font-size: 1.25rem; }}
    p, label, td, small {{ color: var(--muted); }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    form {{ display: grid; gap: 13px; margin: 24px 0; }}
    .row {{ display: grid; gap: 12px; grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    label {{ display: grid; gap: 6px; font-size: .9rem; }}
    input, select, textarea {{
      min-width: 0;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 11px 12px;
      background: var(--panel);
      color: var(--text);
      font: inherit;
    }}
    textarea {{ min-height: 96px; resize: vertical; }}
    button {{
      justify-self: start;
      border: 1px solid var(--accent);
      border-radius: 6px;
      padding: 11px 14px;
      background: transparent;
      color: var(--text);
      font: inherit;
      cursor: pointer;
    }}
    .summary {{ max-width: 800px; font-size: 1.06rem; }}
    .links a, .cta a {{ display: inline-block; margin: 0 12px 10px 0; }}
    .cta a {{ border: 1px solid var(--accent); border-radius: 6px; padding: 10px 12px; }}
    .grid {{ display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); margin: 18px 0; }}
    .box, .notice {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 15px; }}
    .success {{ border-color: color-mix(in srgb, var(--good), var(--line)); }}
    .error {{ border-color: color-mix(in srgb, var(--bad), var(--line)); }}
    code, pre {{ font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace; background: var(--code); color: var(--text); }}
    code {{ display: inline-block; max-width: 100%; padding: 2px 5px; border-radius: 4px; overflow-wrap: anywhere; }}
    pre {{ max-width: 100%; overflow-x: auto; white-space: pre; padding: 16px; border: 1px solid var(--line); border-radius: 8px; }}
    @media (max-width: 760px) {{
      .row {{ grid-template-columns: 1fr; }}
    }}
    {shared_ui_css()}
  </style>
</head>
<body>
  <main>
    {nav}
    <h1>Proof Pack Request</h1>
    <p class="summary">Save buyer intent before payment. This form does not call suppliers, run the LLM, or move USDC; it gives AxonGate a private follow-up path for converting demand into paid Proof Packs.</p>
    {error_html}
    {success_html}
    <div class="grid">
      <div class="box"><strong>Selected Pack</strong><br><code>{html.escape(pack)}</code></div>
      <div class="box"><strong>Indicative Price</strong><br>{html.escape(str(price))} USDC<br><code>{html.escape(amount_units)}</code> units</div>
      <div class="box"><strong>Payment Route</strong><br><code>POST /v1/x402/proof-pack</code><br><small>pack={html.escape(pack)}, source={html.escape(source)}</small></div>
      <div class="box"><strong>Storage</strong><br>Redis when configured; memory fallback.</div>
    </div>
    <form method="post" action="/proof-pack/request" accept-charset="utf-8">
      <input type="hidden" name="source" value="{html.escape(source)}">
      <div class="row">
        <label>Contact
          <input name="contact" value="{html.escape(contact)}" placeholder="email, Telegram, X, or wallet note" required>
        </label>
        <label>Pack
          <select name="pack" aria-label="Proof Pack">{pack_options}</select>
        </label>
      </div>
      <label>Target URL
        <input name="target_url" value="{html.escape(target_url)}" required>
      </label>
      <label>Question
        <input name="question" value="{html.escape(question)}">
      </label>
      <div class="row">
        <label>Use Case
          <input name="use_case" value="{html.escape(use_case)}" placeholder="agent eval, RAG citation check, due diligence">
        </label>
        <label>Budget
          <input name="budget_usdc" value="{html.escape(budget_usdc)}" placeholder="single report, 10/month, team plan">
        </label>
      </div>
      <label>Notes
        <textarea name="notes">{html.escape(notes)}</textarea>
      </label>
      <button type="submit">Send Request</button>
    </form>
    {actions}
    <details class="developer-details">
      <summary>Developer and API details</summary>
      <h2>Full Paid Endpoint</h2>
      <pre>{paid_endpoint}</pre>
      <h2>JSON Lead API</h2>
      <pre>curl -X POST {public}/v1/proof-pack/leads \\
  -H "Content-Type: application/json" \\
  -d '{api_example}'</pre>
    </details>
  </main>
</body>
</html>"""


def bundle_targets_text(value: Any) -> str:
    return "\n".join(split_bundle_target_urls(value))


def build_proof_bundle_html(
    values: Optional[dict[str, Any]] = None,
    submitted: Optional[dict[str, Any]] = None,
    error: Optional[str] = None,
) -> str:
    """Render the higher-ticket Proof Bundle demand-capture page."""
    values = values or {}
    public = html.escape(PUBLIC_BASE_URL)
    try:
        bundle = normalize_proof_bundle(str(values.get("bundle") or DEFAULT_PROOF_BUNDLE))
    except PaymentValidationError:
        bundle = DEFAULT_PROOF_BUNDLE
    source = normalize_attribution_source(str(values.get("source") or "proof-bundle"))
    default_targets = "\n".join(
        [
            "https://www.iana.org/domains/reserved",
            "https://example.com",
            "https://example.org",
        ]
    )
    target_urls = bundle_targets_text(values.get("target_urls") or values.get("target_url") or default_targets)
    question = clean_lead_text(values.get("question") or "Which claims can our agent safely cite across these sources?", 600)
    contact = clean_lead_text(values.get("contact"), 180)
    use_case = clean_lead_text(values.get("use_case"), 240)
    budget_usdc = clean_lead_text(values.get("budget_usdc"), 120)
    notes = clean_lead_text(values.get("notes"), 500)
    price = price_for_proof_bundle(bundle)
    amount_units = str(usdc_units(price))
    selected_targets = split_bundle_target_urls(target_urls)
    quote_url = html.escape(
        f"{PUBLIC_BASE_URL}/proof-pack/bundle/quote?"
        f"target_urls={url_quote(target_urls, safe='')}"
        f"&question={url_quote(question, safe='')}"
        f"&bundle={url_quote(bundle, safe='')}"
        f"&source={url_quote(source, safe='')}"
    )
    stripe_checkout_href = proof_bundle_checkout_path(selected_targets, question, bundle, source)
    payment_url = html.escape(stripe_checkout_href)
    payment_link_state = "configured" if proof_bundle_payment_url(bundle) else "routes to request capture"
    bundle_options = "\n".join(
        f'<option value="{html.escape(bundle_name)}"{" selected" if bundle_name == bundle else ""}>{html.escape(bundle_name)}</option>'
        for bundle_name in PROOF_BUNDLE_PRICING_USDC
    )
    bundle_rows = "\n".join(
        "<tr>"
        f"<td><code>{html.escape(bundle_name)}</code></td>"
        f"<td>{html.escape(str(price_usdc))} USDC</td>"
        f"<td>{html.escape(str(usdc_units(price_usdc)))}</td>"
        f"<td>{html.escape(str(proof_bundle_source_limit(bundle_name)))}</td>"
        f"<td>{html.escape(proof_bundle_policy(bundle_name))}</td>"
        "</tr>"
        for bundle_name, price_usdc in PROOF_BUNDLE_PRICING_USDC.items()
    )
    api_example = html.escape(
        json.dumps(
            {
                "contact": contact or "builder@example.com",
                "target_urls": split_bundle_target_urls(target_urls),
                "question": question,
                "bundle": bundle,
                "use_case": use_case or "Agent launch due diligence",
                "budget_usdc": budget_usdc or "20/month",
                "source": source,
                "notes": notes,
            },
            indent=2,
        )
    )
    error_html = (
        f'<div class="notice error"><strong>Request not saved</strong><br>{html.escape(error)}</div>'
        if error
        else ""
    )
    success_html = ""
    if submitted:
        steps = submitted.get("next_steps", {})
        success_html = f"""
    <div class="notice success">
      <strong>Request received</strong><br>
      Bundle lead <code>{html.escape(str(submitted.get("lead_id")))}</code> was stored privately for follow-up.
      <div class="cta">
        <a href="{html.escape(str(steps.get("quote_page", quote_url)))}">Open Quote</a>
        <a href="{html.escape(str(steps.get("payment_url", payment_url)))}">Payment Link</a>
      </div>
    </div>"""
    nav = site_nav_html("Bundles")
    actions = action_bar_html(
        [
            ("Quote Bundle", quote_url),
            ("Request Bundle", f"{PUBLIC_BASE_URL}/proof-pack/bundle"),
            ("Contact", f"{PUBLIC_BASE_URL}/contact"),
        ],
        [
            ("Single-Source Request", f"{PUBLIC_BASE_URL}/proof-pack/request"),
            ("Sample Report", f"{PUBLIC_BASE_URL}/proof-pack/sample"),
            ("Docs", f"{PUBLIC_BASE_URL}/docs"),
        ],
    )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  {seo_meta_html("AxonGate Evidence Bundles", "Multi-source evidence checks for AI agents. AxonGate verifies whether a set of public URLs can safely support a claim and delivers cited trust reports.", "/proof-pack/bundle")}
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
      --good: #7ee787;
      --bad: #ff9b9b;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.55;
      background: var(--bg);
      color: var(--text);
    }}
    main {{ max-width: 1080px; margin: 0 auto; padding: 44px 22px 72px; }}
    h1 {{ font-size: clamp(2.1rem, 4vw, 3.5rem); line-height: 1.05; margin: 0 0 12px; }}
    h2 {{ margin: 38px 0 12px; font-size: 1.28rem; }}
    p, label, td, small {{ color: var(--muted); }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    form {{ display: grid; gap: 13px; margin: 24px 0; }}
    .row {{ display: grid; gap: 12px; grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    label {{ display: grid; gap: 6px; font-size: .9rem; }}
    input, select, textarea {{
      min-width: 0;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 11px 12px;
      background: var(--panel);
      color: var(--text);
      font: inherit;
    }}
    textarea {{ min-height: 122px; resize: vertical; }}
    button {{
      justify-self: start;
      border: 1px solid var(--accent);
      border-radius: 6px;
      padding: 11px 14px;
      background: transparent;
      color: var(--text);
      font: inherit;
      cursor: pointer;
    }}
    .summary {{ max-width: 820px; font-size: 1.08rem; }}
    .decision-grid {{ display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); margin: 18px 0; }}
    .decision-card {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 15px; }}
    .decision-card strong {{ display: block; color: var(--text); margin-bottom: 4px; }}
    .links a, .cta a {{ display: inline-block; margin: 0 12px 10px 0; }}
    .cta a {{ border: 1px solid var(--accent); border-radius: 6px; padding: 10px 12px; }}
    .grid {{ display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); margin: 18px 0; }}
    .box, .notice {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 15px; }}
    .success {{ border-color: color-mix(in srgb, var(--good), var(--line)); }}
    .error {{ border-color: color-mix(in srgb, var(--bad), var(--line)); }}
    code, pre {{ font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace; background: var(--code); color: var(--text); }}
    code {{ display: inline-block; max-width: 100%; padding: 2px 5px; border-radius: 4px; overflow-wrap: anywhere; }}
    pre {{ max-width: 100%; overflow-x: auto; white-space: pre; padding: 16px; border: 1px solid var(--line); border-radius: 8px; }}
    table {{ width: 100%; table-layout: fixed; border-collapse: collapse; background: var(--panel); border: 1px solid var(--line); }}
    th, td {{ padding: 10px 11px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }}
    th {{ color: var(--text); }}
    td {{ overflow-wrap: anywhere; }}
    .eyebrow {{ margin: 0 0 8px; color: var(--accent); font-size: .78rem; font-weight: 800; text-transform: uppercase; }}
    .table-wrap {{ max-width: 100%; overflow-x: auto; border: 1px solid var(--line); border-radius: 8px; }}
    .table-wrap table {{ min-width: 560px; border: 0; }}
    @media (max-width: 760px) {{
      .row {{ grid-template-columns: 1fr; }}
    }}
    {shared_ui_css()}
  </style>
</head>
<body>
  <main>
    {nav}
    <p class="eyebrow">Multi-source trust decision</p>
    <h1>Can these sources actually prove the claim?</h1>
    <p class="summary">Use AxonGate when one URL is not enough. Evidence Bundles check a set of public sources, flag weak or noisy pages, and deliver a cited trust decision your agent can use before launch, RAG ingestion, or customer-facing citations.</p>
    <div class="decision-grid">
      <div class="decision-card"><strong>Claim support</strong><p>Does the source set support, partially support, or fail to support the buyer question?</p></div>
      <div class="decision-card"><strong>Evidence quality</strong><p>Which pages are substantive, and which are mostly boilerplate or navigation?</p></div>
      <div class="decision-card"><strong>Agent-safe output</strong><p>Return cited findings, risks, source hashes, PDF, and JSON for downstream workflows.</p></div>
    </div>
    {parser_comparison_html()}
    {sample_gallery_html()}
    {error_html}
    {success_html}
    {actions}
    <div class="action-row">
      {stripe_button_html(stripe_checkout_href)}
      <small class="stripe-note">Redirects to Stripe when the selected bundle has a configured Payment Link; otherwise it opens the request form with your details preserved.</small>
    </div>
    <div class="grid">
      <div class="box"><strong>Selected Bundle</strong><br><code>{html.escape(bundle)}</code></div>
      <div class="box"><strong>Indicative Price</strong><br>{html.escape(str(price))} USDC<br><code>{html.escape(amount_units)}</code> units</div>
      <div class="box"><strong>Source Limit</strong><br>{html.escape(str(proof_bundle_source_limit(bundle)))} public URLs</div>
      <div class="box"><strong>Checkout</strong><br>Payment link {html.escape(payment_link_state)}.</div>
    </div>
    <form method="get" action="/proof-pack/bundle/quote" accept-charset="utf-8">
      <input type="hidden" name="source" value="{html.escape(source)}">
      <label>Source URLs
        <textarea name="target_urls" required>{html.escape(target_urls)}</textarea>
      </label>
      <div class="row">
        <label>Question
          <input name="question" value="{html.escape(question)}">
        </label>
        <label>Bundle
          <select name="bundle" aria-label="Proof Bundle">{bundle_options}</select>
        </label>
      </div>
      <button type="submit">Quote Bundle</button>
    </form>
    <form method="post" action="/proof-pack/bundle/request" accept-charset="utf-8">
      <input type="hidden" name="source" value="{html.escape(source)}">
      <label>Contact
        <input name="contact" value="{html.escape(contact)}" placeholder="email, Telegram, X, or wallet note" required>
      </label>
      <label>Source URLs
        <textarea name="target_urls" required>{html.escape(target_urls)}</textarea>
      </label>
      <div class="row">
        <label>Question
          <input name="question" value="{html.escape(question)}">
        </label>
        <label>Bundle
          <select name="bundle" aria-label="Proof Bundle">{bundle_options}</select>
        </label>
      </div>
      <div class="row">
        <label>Use Case
          <input name="use_case" value="{html.escape(use_case)}" placeholder="agent eval, launch audit, vendor check">
        </label>
        <label>Budget
          <input name="budget_usdc" value="{html.escape(budget_usdc)}" placeholder="bundle now, 20/month, team plan">
        </label>
      </div>
      <label>Notes
        <textarea name="notes">{html.escape(notes)}</textarea>
      </label>
      <button type="submit">Request Bundle</button>
    </form>
    <h2>Bundle Pricing</h2>
    <div class="table-wrap">
    <table>
      <thead><tr><th>Bundle</th><th>Price</th><th>USDC Units</th><th>Sources</th><th>Policy</th></tr></thead>
      <tbody>{bundle_rows}</tbody>
    </table>
    </div>
    <details class="developer-details">
      <summary>Developer and API details</summary>
      <h2>JSON Lead API</h2>
      <pre>curl -X POST {public}/v1/proof-pack/bundle/leads \\
  -H "Content-Type: application/json" \\
  -d '{api_example}'</pre>
    </details>
    {site_footer_html()}
  </main>
</body>
</html>"""


def checkout_value_cards_html(preview: dict[str, Any]) -> str:
    deliverables = preview.get("deliverables") if isinstance(preview.get("deliverables"), list) else []
    cards = []
    for item in deliverables[:6]:
        cards.append(
            f"""
      <article class="value-card">
        <span>Included</span>
        <strong>{html.escape(str(item))}</strong>
      </article>
            """
        )
    return "\n".join(cards)


def checkout_steps_html(preview: dict[str, Any]) -> str:
    steps = preview.get("after_checkout") if isinstance(preview.get("after_checkout"), list) else []
    return "\n".join(
        f"""
      <article class="step-card">
        <span>{index}</span>
        <p>{html.escape(str(step))}</p>
      </article>
        """
        for index, step in enumerate(steps[:4], start=1)
        if str(step).strip()
    )


def checkout_trust_notes_html(preview: dict[str, Any]) -> str:
    notes = preview.get("trust_notes") if isinstance(preview.get("trust_notes"), list) else []
    items = "".join(f"<li>{html.escape(str(note))}</li>" for note in notes if str(note).strip())
    return f"<ul>{items}</ul>" if items else '<p class="muted">No checkout notes are available.</p>'


def checkout_source_rows_html(source_profiles: list[dict[str, Any]]) -> str:
    if not source_profiles:
        return (
            "<tr><td>Collected at Stripe</td><td>Target URLs and the question are collected by the Stripe checkout form.</td>"
            "<td>Review after payment</td></tr>"
        )
    return "\n".join(
        "<tr>"
        f"<td><a href=\"{html.escape(str(profile.get('target_url') or ''), quote=True)}\">{html.escape(str(profile.get('target_url') or ''))}</a></td>"
        f"<td>{'yes' if profile.get('cached_source_available') else 'no'}</td>"
        f"<td>{'sample/cache ready' if profile.get('starter_sample_available') or profile.get('basic_cache_available') else 'fresh fetch after payment'}</td>"
        "</tr>"
        for profile in source_profiles
    )


def build_proof_bundle_checkout_html(
    target_urls: list[str],
    question: str,
    bundle: str,
    source: str,
    quote: Optional[dict[str, Any]] = None,
) -> str:
    """Render the final customer review step before the tracked Stripe redirect."""
    normalized_bundle = normalize_proof_bundle(bundle)
    normalized_source = normalize_attribution_source(source)
    normalized_question = proof_bundle_question(question)
    selected_price = price_for_proof_bundle(normalized_bundle)
    amount_units = str(usdc_units(selected_price))
    source_limit = proof_bundle_source_limit(normalized_bundle)
    target_count = len(target_urls)
    preview = (
        quote.get("report_preview")
        if isinstance(quote, dict) and isinstance(quote.get("report_preview"), dict)
        else proof_bundle_report_preview(normalized_bundle, target_count)
    )
    source_profiles = (
        quote.get("source_profiles")
        if isinstance(quote, dict) and isinstance(quote.get("source_profiles"), list)
        else []
    )
    value_cards = checkout_value_cards_html(preview)
    checkout_steps = checkout_steps_html(preview)
    trust_notes = checkout_trust_notes_html(preview)
    source_rows = checkout_source_rows_html(source_profiles)
    payment_path = proof_bundle_payment_path(target_urls, normalized_question, normalized_bundle, normalized_source)
    checkout_state = "Stripe Payment Link is configured" if proof_bundle_payment_url(normalized_bundle) else "Stripe link is not configured; this continues to request capture"
    source_label = f"{target_count} of {source_limit} sources" if target_count else f"Stripe will collect up to {source_limit} source URLs"
    target_text = "\n".join(target_urls) if target_urls else "Collected in Stripe checkout custom fields"
    quote_page = (
        proof_bundle_quote_page_url(target_urls, normalized_question, normalized_bundle, normalized_source)
        if target_urls
        else public_url(f"/proof-pack/bundle?bundle={url_quote(normalized_bundle, safe='')}&source={url_quote(normalized_source, safe='')}")
    )
    nav = site_nav_html("Bundles")
    actions = action_bar_html(
        [
            ("Change Quote", quote_page),
            ("Sample Report", public_url("/proof-pack/sample")),
            ("Contact", public_url("/contact")),
        ],
        [
            ("Evidence Bundles", public_url("/proof-pack/bundle")),
            ("Recover Delivery", public_url("/proof-pack/bundle/recover")),
            ("Docs", public_url("/docs")),
        ],
    )
    continue_label = "Continue to Stripe" if proof_bundle_payment_url(normalized_bundle) else "Continue to request form"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Review Evidence Bundle Checkout | AxonGate</title>
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
    body {{ margin: 0; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; line-height: 1.55; background: var(--bg); color: var(--text); }}
    main {{ max-width: 1120px; margin: 0 auto; padding: 44px 22px 72px; }}
    h1 {{ margin: 0 0 12px; font-size: clamp(2.1rem, 4vw, 3.4rem); line-height: 1.05; }}
    h2 {{ margin: 0 0 12px; font-size: 1.3rem; }}
    p, li, td, small {{ color: var(--muted); }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .panel {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: clamp(16px, 3vw, 26px); margin: 0 0 16px; }}
    .hero {{ border-left: 5px solid var(--accent); }}
    .hero p {{ max-width: 82ch; }}
    .eyebrow {{ margin: 0 0 8px; color: var(--accent); font-size: .78rem; font-weight: 800; text-transform: uppercase; letter-spacing: 0; }}
    .checkout-actions {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin-top: 16px; }}
    .checkout-actions small {{ max-width: 66ch; }}
    .grid {{ display: grid; gap: 12px; grid-template-columns: repeat(4, minmax(0, 1fr)); margin-top: 18px; }}
    .box {{ border: 1px solid var(--line); border-radius: 8px; background: #131722; padding: 14px; min-width: 0; }}
    .box span, .value-card span {{ display: block; color: var(--accent); font-size: .75rem; font-weight: 800; text-transform: uppercase; }}
    .box strong, .value-card strong {{ display: block; margin-top: 5px; overflow-wrap: anywhere; }}
    .value-grid {{ display: grid; gap: 10px; grid-template-columns: repeat(3, minmax(0, 1fr)); }}
    .value-card {{ border: 1px solid var(--line); border-radius: 8px; background: #131722; padding: 13px; min-width: 0; }}
    .step-grid {{ display: grid; gap: 10px; grid-template-columns: repeat(3, minmax(0, 1fr)); }}
    .step-card {{ border: 1px solid var(--line); border-radius: 8px; padding: 13px; display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 10px; }}
    .step-card span {{ width: 28px; height: 28px; border-radius: 999px; display: inline-flex; align-items: center; justify-content: center; background: color-mix(in srgb, var(--accent), var(--panel) 72%); color: var(--text); font-weight: 800; }}
    .step-card p {{ margin: 0; }}
    code, pre {{ font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace; background: var(--code); color: var(--text); }}
    code {{ display: inline-block; max-width: 100%; padding: 2px 5px; border-radius: 4px; overflow-wrap: anywhere; }}
    pre {{ max-width: 100%; overflow-x: auto; white-space: pre-wrap; padding: 16px; border: 1px solid var(--line); border-radius: 8px; }}
    table {{ width: 100%; table-layout: fixed; border-collapse: collapse; background: var(--panel); }}
    th, td {{ padding: 10px 11px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; overflow-wrap: anywhere; }}
    th {{ color: var(--text); }}
    .table-wrap {{ max-width: 100%; overflow-x: auto; border: 1px solid var(--line); border-radius: 8px; }}
    .table-wrap table {{ min-width: 560px; border: 0; }}
    @media (max-width: 760px) {{
      main {{ padding: 24px 14px 52px; }}
      .grid, .value-grid, .step-grid {{ grid-template-columns: 1fr; }}
      .checkout-actions .button {{ width: 100%; }}
    }}
    {shared_ui_css()}
  </style>
</head>
<body>
  <main>
    {nav}
    <section class="panel hero">
      <p class="eyebrow">Checkout review</p>
      <h1>Review your Evidence Bundle</h1>
      <p>{html.escape(str(preview.get("decision_summary") or "AxonGate turns public URLs into a cited evidence report before your agent relies on them."))}</p>
      <div class="grid">
        <div class="box"><span>Bundle</span><strong>{html.escape(normalized_bundle)}</strong></div>
        <div class="box"><span>Price</span><strong>{html.escape(str(selected_price))} USDC</strong><small><code>{html.escape(amount_units)}</code> units</small></div>
        <div class="box"><span>Sources</span><strong>{html.escape(source_label)}</strong></div>
        <div class="box"><span>Checkout</span><strong>{html.escape(checkout_state)}</strong></div>
      </div>
      <div class="checkout-actions">
        {stripe_button_html(payment_path, continue_label)}
        <small>This button records checkout intent and then redirects through <code>/proof-pack/bundle/pay</code>. Supplier work starts only after Stripe confirms payment.</small>
      </div>
    </section>
    {actions}
    {parser_comparison_html()}
    {delivery_promise_html()}
    <section class="panel">
      <h2>What This Payment Buys</h2>
      <div class="value-grid">{value_cards}</div>
    </section>
    <section class="panel">
      <h2>After Checkout</h2>
      <div class="step-grid">{checkout_steps}</div>
    </section>
    <section class="panel">
      <h2>Source Review</h2>
      <pre>{html.escape(target_text)}</pre>
      <div class="table-wrap">
        <table>
          <thead><tr><th>URL</th><th>Cached</th><th>Delivery note</th></tr></thead>
          <tbody>{source_rows}</tbody>
        </table>
      </div>
    </section>
    <section class="panel">
      <h2>Trust Notes</h2>
      {trust_notes}
    </section>
    {site_footer_html()}
  </main>
</body>
</html>"""


def build_proof_bundle_quote_html(quote: dict[str, Any]) -> str:
    """Render a no-spend Proof Bundle quote page."""
    public = html.escape(PUBLIC_BASE_URL)
    bundle = str(quote["bundle"])
    target_text = "\n".join(str(target) for target in quote["target_urls"])
    question = str(quote["question"])
    source = str(quote["source"])
    preview = quote.get("report_preview") if isinstance(quote.get("report_preview"), dict) else proof_bundle_report_preview(bundle, int(quote.get("target_count") or 0))
    value_cards = checkout_value_cards_html(preview)
    checkout_steps = checkout_steps_html(preview)
    trust_notes = checkout_trust_notes_html(preview)
    bundle_options = "\n".join(
        f'<option value="{html.escape(bundle_name)}"{" selected" if bundle_name == bundle else ""}>{html.escape(bundle_name)}</option>'
        for bundle_name in PROOF_BUNDLE_PRICING_USDC
    )
    source_rows = checkout_source_rows_html(quote["source_profiles"])
    bundle_rows = "\n".join(
        "<tr>"
        f"<td><code>{html.escape(bundle_name)}</code></td>"
        f"<td>{html.escape(str(info['price_usdc']))} USDC</td>"
        f"<td>{html.escape(str(info['amount_units']))}</td>"
        f"<td>{html.escape(str(info['source_limit']))}</td>"
        f"<td>{html.escape(str(info['policy']))}</td>"
        f"<td><a href=\"{html.escape(str(info['checkout_url']))}\">Pay with Stripe</a></td>"
        f"<td><a href=\"{html.escape(str(info['quote_page']))}\">Quote</a></td>"
        "</tr>"
        for bundle_name, info in quote["bundles"].items()
    )
    request_page = html.escape(str(quote["next_steps"]["request_page"]))
    quote_api = html.escape(str(quote["next_steps"]["quote_api"]))
    payment_url = html.escape(str(quote["next_steps"]["payment_url"]))
    stripe_checkout_href = proof_bundle_checkout_path(list(quote["target_urls"]), question, bundle, source)
    checkout_state = "Stripe Payment Link configured" if quote["next_steps"].get("payment_link_configured") else "request capture fallback"
    single_source_endpoint = html.escape(str(quote["next_steps"]["single_source_proof_pack_endpoint"]))
    nav = site_nav_html("Bundles")
    actions = action_bar_html(
        [
            ("Request Bundle", request_page),
            ("Change Quote", f"{PUBLIC_BASE_URL}/proof-pack/bundle"),
            ("Contact", f"{PUBLIC_BASE_URL}/contact"),
        ],
        [
            ("JSON Quote", quote_api),
            ("Proof Bundles", f"{PUBLIC_BASE_URL}/proof-pack/bundle"),
            ("Proof Packs", f"{PUBLIC_BASE_URL}/proof-pack"),
            ("Docs", f"{PUBLIC_BASE_URL}/docs"),
        ],
    )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AxonGate Proof Bundle Quote</title>
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
    main {{ max-width: 1080px; margin: 0 auto; padding: 44px 22px 72px; }}
    h1 {{ font-size: clamp(2.05rem, 4vw, 3.4rem); line-height: 1.05; margin: 0 0 12px; }}
    h2 {{ margin: 38px 0 12px; font-size: 1.25rem; }}
    p, label, td {{ color: var(--muted); }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    form {{ display: grid; gap: 12px; margin: 24px 0; }}
    .row {{ display: grid; gap: 12px; grid-template-columns: minmax(0, 1fr) 170px auto; align-items: end; }}
    label {{ display: grid; gap: 6px; font-size: .9rem; }}
    input, select, textarea {{
      min-width: 0;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 11px 12px;
      background: var(--panel);
      color: var(--text);
      font: inherit;
    }}
    textarea {{ min-height: 118px; resize: vertical; }}
    button {{
      border: 1px solid var(--accent);
      border-radius: 6px;
      padding: 11px 14px;
      background: transparent;
      color: var(--text);
      font: inherit;
      cursor: pointer;
    }}
    .summary {{ max-width: 820px; font-size: 1.06rem; }}
    .eyebrow {{ margin: 0 0 8px; color: var(--accent); font-size: .78rem; font-weight: 800; text-transform: uppercase; letter-spacing: 0; }}
    .links a, .cta a {{ display: inline-block; margin: 0 12px 10px 0; }}
    .cta a {{ border: 1px solid var(--accent); border-radius: 6px; padding: 10px 12px; }}
    .panel {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: clamp(16px, 3vw, 24px); margin: 0 0 16px; }}
    .checkout-panel {{ border-left: 5px solid var(--accent); }}
    .checkout-panel h2 {{ margin-top: 0; font-size: clamp(1.35rem, 3vw, 2rem); }}
    .checkout-actions {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin-top: 14px; }}
    .checkout-actions small {{ max-width: 64ch; }}
    .value-grid {{ display: grid; gap: 10px; grid-template-columns: repeat(3, minmax(0, 1fr)); }}
    .value-card {{ border: 1px solid var(--line); border-radius: 8px; background: #131722; padding: 13px; min-width: 0; }}
    .value-card span {{ display: block; color: var(--accent); font-size: .75rem; font-weight: 800; text-transform: uppercase; }}
    .value-card strong {{ display: block; margin-top: 5px; overflow-wrap: anywhere; }}
    .step-grid {{ display: grid; gap: 10px; grid-template-columns: repeat(3, minmax(0, 1fr)); }}
    .step-card {{ border: 1px solid var(--line); border-radius: 8px; padding: 13px; display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 10px; }}
    .step-card span {{ width: 28px; height: 28px; border-radius: 999px; display: inline-flex; align-items: center; justify-content: center; background: color-mix(in srgb, var(--accent), var(--panel) 72%); color: var(--text); font-weight: 800; }}
    .step-card p {{ margin: 0; }}
    .grid {{ display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); margin: 18px 0; }}
    .box {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 15px; }}
    code, pre {{ font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace; background: var(--code); color: var(--text); }}
    code {{ display: inline-block; max-width: 100%; padding: 2px 5px; border-radius: 4px; overflow-wrap: anywhere; }}
    pre {{ max-width: 100%; overflow-x: auto; white-space: pre; padding: 16px; border: 1px solid var(--line); border-radius: 8px; }}
    table {{ width: 100%; table-layout: fixed; border-collapse: collapse; background: var(--panel); border: 1px solid var(--line); }}
    th, td {{ padding: 10px 11px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }}
    th {{ color: var(--text); }}
    td {{ overflow-wrap: anywhere; }}
    .table-wrap {{ max-width: 100%; overflow-x: auto; border: 1px solid var(--line); border-radius: 8px; }}
    .table-wrap table {{ min-width: 560px; border: 0; }}
    @media (max-width: 760px) {{
      .row {{ grid-template-columns: 1fr; }}
      .value-grid, .step-grid {{ grid-template-columns: 1fr; }}
      .checkout-actions .button {{ width: 100%; }}
    }}
    {shared_ui_css()}
  </style>
</head>
<body>
  <main>
    {nav}
    <h1>Proof Bundle Quote</h1>
    <p class="summary">A supplier-free quote for a multi-source evidence bundle. It validates public URLs, checks reusable source material, and returns exact USDC units before any paid work happens.</p>
    <form method="get" action="/proof-pack/bundle/quote">
      <input type="hidden" name="source" value="{html.escape(source)}">
      <label>Source URLs
        <textarea name="target_urls" required>{html.escape(target_text)}</textarea>
      </label>
      <div class="row">
        <label>Question
          <input name="question" value="{html.escape(question)}">
        </label>
        <label>Bundle
          <select name="bundle" aria-label="Proof Bundle">{bundle_options}</select>
        </label>
        <button type="submit">Quote</button>
      </div>
    </form>
    {actions}
    <section class="panel checkout-panel">
      <p class="eyebrow">Before you pay</p>
      <h2>{html.escape(str(preview.get("decision_label") or "Evidence decision before your agent cites"))}</h2>
      <p>{html.escape(str(preview.get("decision_summary") or "AxonGate turns public URLs into a cited evidence report."))}</p>
      <div class="checkout-actions">
        {stripe_button_html(stripe_checkout_href, "Review checkout")}
        <small>Review the selected bundle and delivery promise, then continue to Stripe. The final tracked payment redirect remains <code>/proof-pack/bundle/pay</code>.</small>
      </div>
    </section>
    {parser_comparison_html()}
    <section class="panel">
      <h2>What This Payment Buys</h2>
      <div class="value-grid">{value_cards}</div>
    </section>
    <section class="panel">
      <h2>After Checkout</h2>
      <div class="step-grid">{checkout_steps}</div>
    </section>
    <section class="panel">
      <h2>Trust Notes</h2>
      {trust_notes}
    </section>
    {delivery_promise_html()}
    {sample_gallery_html()}
    <div class="grid">
      <div class="box"><strong>Selected Bundle</strong><br><code>{html.escape(bundle)}</code></div>
      <div class="box"><strong>Price</strong><br>{html.escape(str(quote["price_usdc"]))} USDC<br><code>{html.escape(str(quote["amount_units"]))}</code> units</div>
      <div class="box"><strong>Sources</strong><br>{html.escape(str(quote["target_count"]))} of {html.escape(str(quote["source_limit"]))}</div>
      <div class="box"><strong>Checkout</strong><br>{html.escape(checkout_state)}<br><code>{html.escape(str(quote["next_steps"]["checkout_url"]))}</code></div>
    </div>
    <h2>Payment Or Request Path</h2>
    <pre>{payment_url}</pre>
    <h2>Immediate Single-Source Endpoint</h2>
    <pre>POST {single_source_endpoint}?pack=standard</pre>
    <h2>Source Checks</h2>
    <div class="table-wrap">
    <table>
      <thead><tr><th>URL</th><th>Cached</th><th>Starter Sample</th></tr></thead>
      <tbody>{source_rows}</tbody>
    </table>
    </div>
    <h2>Bundles</h2>
    <div class="table-wrap">
    <table>
      <thead><tr><th>Bundle</th><th>Price</th><th>USDC Units</th><th>Sources</th><th>Policy</th><th>Checkout</th><th>Quote</th></tr></thead>
      <tbody>{bundle_rows}</tbody>
    </table>
    </div>
    {site_footer_html()}
  </main>
</body>
</html>"""


def build_proof_pack_html() -> str:
    """Render the public Proof Pack product page."""
    public = html.escape(PUBLIC_BASE_URL)
    pro_url = html.escape(PROOF_PRO_PAYMENT_URL or f"{PUBLIC_BASE_URL}/proof-pack/quote?target_url=https%3A%2F%2Fexample.com&pack=standard")
    team_url = html.escape(PROOF_TEAM_PAYMENT_URL or f"{PUBLIC_BASE_URL}/proof-pack/quote?target_url=https%3A%2F%2Fexample.com&pack=deep")
    quote_url = html.escape(f"{PUBLIC_BASE_URL}/proof-pack/quote?target_url=https%3A%2F%2Fexample.com&pack=standard")
    preview_url = html.escape(f"{PUBLIC_BASE_URL}/proof-pack/preview?target_url=https%3A%2F%2Fwww.iana.org%2Fdomains%2Freserved&pack=quick")
    request_url = html.escape(f"{PUBLIC_BASE_URL}/proof-pack/request?target_url=https%3A%2F%2Fexample.com&pack=quick")
    bundle_url = html.escape(f"{PUBLIC_BASE_URL}/proof-pack/bundle?source=proof-pack")
    bundle_stripe_href = proof_bundle_checkout_path([], "", DEFAULT_PROOF_BUNDLE, "proof-pack-stripe")
    bundle_quote_url = html.escape(
        f"{PUBLIC_BASE_URL}/proof-pack/bundle/quote?target_urls=https%3A%2F%2Fwww.iana.org%2Fdomains%2Freserved%0Ahttps%3A%2F%2Fexample.com&bundle=scout"
    )
    sample_url = html.escape(f"{PUBLIC_BASE_URL}/proof-pack/sample")
    sample_api_url = html.escape(f"{PUBLIC_BASE_URL}/v1/proof-pack/sample")
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
    nav = site_nav_html("Proof Packs")
    actions = action_bar_html(
        [
            ("Get Instant Quote", quote_url),
            ("Request Report", request_url),
            ("View Sample", sample_url),
        ],
        [
            ("Try Mini Preview", preview_url),
            ("Evidence Bundles", bundle_url),
            ("Quote Bundle", bundle_quote_url),
            ("Proof Pro", pro_url),
            ("Proof Team", team_url),
            ("API Docs", f"{PUBLIC_BASE_URL}/docs"),
        ],
    )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  {seo_meta_html("AxonGate Source Trust Check", "Check whether a public URL safely supports a claim before an AI agent cites, ingests, or acts on it. Get cited source trust reports for agent builders.", "/proof-pack")}
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
    .trust-hero {{
      display: grid;
      gap: 22px;
      margin: 0 0 28px;
      padding: clamp(22px, 4vw, 34px);
      border: 1px solid var(--line);
      border-radius: 8px;
      background: color-mix(in srgb, var(--panel), var(--bg) 14%);
    }}
    .eyebrow {{
      margin: 0;
      color: var(--accent);
      font-size: .78rem;
      font-weight: 800;
      text-transform: uppercase;
    }}
    .trust-form {{
      display: grid;
      gap: 10px;
      grid-template-columns: minmax(0, 1.1fr) minmax(0, .9fr) auto;
      align-items: end;
    }}
    .trust-form label {{
      display: grid;
      gap: 6px;
      color: var(--muted);
      font-size: .9rem;
    }}
    .trust-form input {{
      min-height: 44px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px 12px;
      background: var(--bg);
      color: var(--text);
      font: inherit;
    }}
    .decision-grid {{
      display: grid;
      gap: 12px;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      margin: 20px 0;
    }}
    .decision-card {{
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      background: var(--panel);
    }}
    .decision-card strong {{ display: block; margin-bottom: 4px; color: var(--text); }}
    .contrast {{
      display: grid;
      gap: 12px;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      margin: 24px 0 8px;
    }}
    .contrast .box {{ border-left: 5px solid var(--line); }}
    .contrast .box:last-child {{ border-left-color: var(--accent); }}
    .proof-name {{ color: var(--muted); font-size: .95rem; }}
    .links a, .cta a {{ display: inline-block; margin: 0 12px 10px 0; }}
    .cta a {{ border: 1px solid var(--accent); border-radius: 6px; padding: 10px 12px; }}
    .grid {{ display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); }}
    .box {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 15px; }}
    code, pre {{ font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace; background: var(--code); color: var(--text); }}
    code {{ padding: 2px 5px; border-radius: 4px; }}
    pre {{ overflow-x: auto; padding: 16px; border: 1px solid var(--line); border-radius: 8px; }}
    table {{ width: 100%; border-collapse: collapse; background: var(--panel); border: 1px solid var(--line); }}
    th, td {{ padding: 10px 11px; border-bottom: 1px solid var(--line); text-align: left; }}
    @media (max-width: 840px) {{
      .trust-form, .contrast {{ grid-template-columns: 1fr; }}
      .decision-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    }}
    @media (max-width: 520px) {{
      .decision-grid {{ grid-template-columns: 1fr; }}
      .trust-form button {{ width: 100%; }}
    }}
    {shared_ui_css()}
  </style>
</head>
<body>
  <main>
    {nav}
    <section class="trust-hero">
      <div>
        <p class="eyebrow">Not a page parser</p>
        <h1>Can your agent safely cite this source?</h1>
        <p class="summary">AxonGate checks whether a public URL actually supports a claim, extracts citation-ready evidence, and flags weak or noisy sources before an AI agent relies on them.</p>
      </div>
      <form class="trust-form" method="get" action="/proof-pack/quote">
        <label>Source URL
          <input name="target_url" inputmode="url" placeholder="example.com/source" required>
        </label>
        <label>Claim or question
          <input name="question" placeholder="What can my agent safely say?">
        </label>
        <input type="hidden" name="pack" value="standard">
        <button type="submit">Run evidence check</button>
      </form>
    </section>
    <section>
      <h2>A parser returns text. AxonGate returns a trust decision.</h2>
      <div class="decision-grid">
        <div class="decision-card"><strong>Supported</strong><p>The source contains usable evidence for the claim.</p></div>
        <div class="decision-card"><strong>Weak</strong><p>The page is mostly navigation, boilerplate, or vague copy.</p></div>
        <div class="decision-card"><strong>Unsupported</strong><p>The submitted claim is not established by the cited source.</p></div>
        <div class="decision-card"><strong>Citation-ready</strong><p>Every usable finding points back to exact evidence IDs.</p></div>
      </div>
      <div class="contrast">
        <div class="box"><strong>Page parser</strong><br>Returns extracted text and leaves your agent to decide whether the evidence is useful.</div>
        <div class="box"><strong>AxonGate</strong><br>Scores evidence quality, exposes risks, and tells your agent whether the source is safe to cite.</div>
      </div>
    </section>
    <h2>Source Trust Reports <span class="proof-name">(Proof Packs)</span></h2>
    <p class="summary">Paid, citation-backed evidence checks for agent builders. Send a public source URL and a claim or question; AxonGate returns a compact answer, executive summary, key claims, citations, risks, source hash, payment metadata, and UEG receipt.</p>
    {actions}

    <div class="grid">
      <div class="box"><strong>Buyer</strong><br>Agent builders who need to know what a source can safely support.</div>
      <div class="box"><strong>Protocol</strong><br>x402 on Base USDC.</div>
      <div class="box"><strong>Fallback</strong><br>Deterministic evidence check if LLM generation is off or fails.</div>
      <div class="box"><strong>Validation</strong><br>Unsupported LLM claims are dropped unless they cite extracted evidence IDs.</div>
    </div>

    <h2>Pricing</h2>
    <div class="table-wrap">
    <table>
      <thead><tr><th>Pack</th><th>Price</th><th>USDC Units</th><th>Policy</th></tr></thead>
      <tbody>{pack_rows}</tbody>
    </table>
    </div>

    <h2>Proof Bundles</h2>
    <p>When a buyer has several sources or wants an agent-launch evidence set, route them to Proof Bundles. Bundles quote multi-source work clearly and capture demand before batch delivery is automated.</p>
    <div class="action-row">
      {stripe_button_html(bundle_stripe_href)}
      <a class="button secondary" href="{bundle_url}">Add source URLs first</a>
      <small class="stripe-note">Stripe checkout is for Evidence Bundles. Single-source agent calls remain x402.</small>
    </div>
    <div class="grid">
      <div class="box"><strong>Single source</strong><br>Use the instant quote when one URL needs a trust decision.</div>
      <div class="box"><strong>Several sources</strong><br>Use Evidence Bundles when a claim needs support across multiple pages.</div>
      <div class="box"><strong>Unsure</strong><br>Send a request and AxonGate will route it to the right report path.</div>
    </div>

    <details class="developer-details">
      <summary>Developer and API details</summary>
      <h2>Quote</h2>
      <pre>curl "{public}/v1/proof-pack/quote?target_url=https%3A%2F%2Fexample.com&amp;question=What%20does%20this%20source%20establish%3F&amp;pack=standard&amp;source=docs"</pre>

      <h2>Bundle Quote</h2>
      <pre>curl "{public}/v1/proof-pack/bundle/quote?target_urls=https%3A%2F%2Fwww.iana.org%2Fdomains%2Freserved%0Ahttps%3A%2F%2Fexample.com&amp;bundle=scout&amp;source=docs"</pre>

      <h2>No-Spend Sample</h2>
      <pre>curl "{sample_api_url}?source=docs"</pre>

      <h2>Paid Endpoint</h2>
      <pre>POST {public}/v1/x402/proof-pack?pack=standard
Header: PAYMENT-SIGNATURE: &lt;x402-payment-proof&gt;
Header: X-AxonGate-Pack: standard</pre>

      <h2>Request</h2>
      <pre>{request_json}</pre>

      <h2>Response Shape</h2>
      <pre>{response_json}</pre>
    </details>
    {site_footer_html()}
  </main>
</body>
</html>"""


def build_proof_pack_sample_html(source: str = "direct") -> str:
    """Render a public no-spend Proof Pack sample report."""
    sample = build_proof_pack_sample_response(source)
    public = html.escape(PUBLIC_BASE_URL)
    sample_api = html.escape(f"{PUBLIC_BASE_URL}/v1/proof-pack/sample?source={url_quote(sample['payment']['source'], safe='')}")
    quote_api = html.escape(sample["next_steps"]["quote_api"])
    preview_url = html.escape(sample["next_steps"]["preview_page"])
    request_url = html.escape(
        proof_pack_request_page_url(
            sample["target_url"],
            sample["question"],
            sample["pack"],
            sample["payment"]["source"],
        )
    )
    paid_endpoint = html.escape(sample["next_steps"]["paid_endpoint"])
    buyer_command = html.escape(sample["next_steps"]["buyer_command"])
    raw_json = html.escape(json.dumps(sample, indent=2))
    claim_rows = "\n".join(
        "<tr>"
        f"<td>{html.escape(claim['claim'])}</td>"
        f"<td>{html.escape(', '.join(claim['citation_ids']))}</td>"
        f"<td>{html.escape(str(claim['confidence']))}</td>"
        "</tr>"
        for claim in sample["key_claims"]
    )
    citation_rows = "\n".join(
        "<tr>"
        f"<td><code>{html.escape(citation['id'])}</code></td>"
        f"<td>{html.escape(citation['excerpt'])}</td>"
        "</tr>"
        for citation in sample["citations"]
    )
    risk_items = "\n".join(f"<li>{html.escape(risk)}</li>" for risk in sample["risks"])
    report_card = sample.get("report_card") if isinstance(sample.get("report_card"), dict) else {}
    establishes_items = "\n".join(
        f"<li>{html.escape(str(item))}</li>"
        for item in report_card.get("what_this_establishes", [])
        if str(item).strip()
    )
    limits_items = "\n".join(
        f"<li>{html.escape(str(item))}</li>"
        for item in report_card.get("what_it_does_not_establish", [])
        if str(item).strip()
    )
    buyer_value_cards = "\n".join(
        f"<article class=\"value-card\"><span>Value</span><strong>{html.escape(str(item))}</strong></article>"
        for item in report_card.get("buyer_value", [])
        if str(item).strip()
    )
    claim_cards = "\n".join(
        f"""
        <article class="claim-card">
          <p>{html.escape(str(claim.get('claim') or ''))}</p>
          <div class="mini-meta">
            <span>{html.escape(', '.join(str(item) for item in claim.get('citation_ids', [])))}</span>
            <span>confidence {html.escape(str(claim.get('confidence')))}</span>
          </div>
        </article>
        """
        for claim in sample["key_claims"]
    )
    citation_cards = "\n".join(
        f"""
        <article class="citation-card">
          <div><code>{html.escape(str(citation.get('id') or ''))}</code></div>
          <p>{html.escape(str(citation.get('excerpt') or ''))}</p>
        </article>
        """
        for citation in sample["citations"]
    )
    nav = site_nav_html("Proof Packs")
    actions = action_bar_html(
        [
            ("Get Live Quote", quote_api),
            ("Try Mini Preview", preview_url),
            ("Request This Report", request_url),
        ],
        [
            ("Sample JSON", sample_api),
            ("Probe Payment Terms", paid_endpoint),
            ("Docs", f"{PUBLIC_BASE_URL}/docs"),
            ("Quickstart", f"{PUBLIC_BASE_URL}/quickstart"),
            ("Resources", f"{PUBLIC_BASE_URL}/discovery/resources"),
        ],
    )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Sample Evidence Report | AxonGate</title>
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
    main {{ max-width: 1120px; margin: 0 auto; padding: 44px 22px 72px; }}
    h1 {{ font-size: clamp(2.1rem, 4vw, 3.3rem); line-height: 1.05; margin: 0 0 12px; }}
    h2 {{ margin: 0 0 12px; font-size: 1.3rem; }}
    p, li, td {{ color: var(--muted); }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .panel {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: clamp(18px, 3vw, 28px); margin: 0 0 16px; }}
    .hero {{ padding: clamp(22px, 4vw, 38px); }}
    .eyebrow {{ color: var(--accent); text-transform: uppercase; letter-spacing: 0; font-size: .78rem; font-weight: 800; margin: 0 0 8px; }}
    .summary {{ max-width: 820px; font-size: 1.08rem; }}
    .links a, .cta a {{ display: inline-block; margin: 0 12px 10px 0; }}
    .cta a {{ border: 1px solid var(--accent); border-radius: 6px; padding: 10px 12px; }}
    .grid {{ display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); margin: 18px 0 0; }}
    .box {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 15px; }}
    .decision {{ border-left: 5px solid var(--accent); }}
    .decision h2 {{ font-size: clamp(1.45rem, 3vw, 2.1rem); }}
    .value-grid {{ display: grid; gap: 12px; grid-template-columns: repeat(3, minmax(0, 1fr)); }}
    .value-card {{ border: 1px solid var(--line); border-radius: 8px; background: #131722; padding: 14px; min-width: 0; }}
    .value-card span {{ display: block; color: var(--accent); font-size: .75rem; font-weight: 800; text-transform: uppercase; }}
    .value-card strong {{ display: block; margin-top: 5px; overflow-wrap: anywhere; }}
    .split {{ display: grid; gap: 14px; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); }}
    .claim-list, .citation-list {{ display: grid; gap: 10px; }}
    .claim-card, .citation-card {{ border: 1px solid var(--line); border-radius: 8px; padding: 13px; overflow-wrap: anywhere; }}
    .claim-card p, .citation-card p {{ margin: 0 0 8px; }}
    .mini-meta {{ display: flex; flex-wrap: wrap; gap: 7px; color: var(--muted); font-size: .88rem; }}
    .mini-meta span {{ border: 1px solid var(--line); border-radius: 999px; padding: 3px 8px; }}
    details.technical {{ border: 1px solid var(--line); border-radius: 8px; background: var(--panel); padding: 14px; margin: 0 0 16px; }}
    details.technical summary {{ cursor: pointer; color: var(--accent); font-weight: 800; }}
    code, pre {{ font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace; background: var(--code); color: var(--text); }}
    code {{ padding: 2px 5px; border-radius: 4px; }}
    pre {{ overflow-x: auto; padding: 16px; border: 1px solid var(--line); border-radius: 8px; }}
    table {{ width: 100%; border-collapse: collapse; background: var(--panel); border: 1px solid var(--line); }}
    th, td {{ padding: 10px 11px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }}
    th {{ color: var(--text); }}
    @media (max-width: 760px) {{
      main {{ padding: 24px 14px 52px; }}
      .value-grid, .split {{ grid-template-columns: 1fr; }}
    }}
    {shared_ui_css()}
  </style>
</head>
<body>
  <main>
    {nav}
    <section class="panel hero">
      <p class="eyebrow">No-spend sample report</p>
      <h1>Sample Evidence Decision</h1>
      <p class="summary">See the report shape buyers get before spending: a plain-English decision, supported findings, citation IDs, risks, source metadata, and export-ready JSON. This sample uses embedded source material, skips supplier work, and never calls the LLM.</p>
      {actions}
      <div class="grid">
        <div class="box"><strong>Status</strong><br><code>{html.escape(sample["status"])}</code></div>
        <div class="box"><strong>Pack</strong><br><code>{html.escape(sample["pack"])}</code></div>
        <div class="box"><strong>Live Price</strong><br>{html.escape(str(sample["payment"]["live_pack_amount_usdc"]))} USDC</div>
        <div class="box"><strong>Source Hash</strong><br><code>{html.escape(sample["source_profile"]["content_sha256"][:16])}...</code></div>
      </div>
    </section>

    <section class="panel decision">
      <p class="eyebrow">Evidence decision</p>
      <h2>{html.escape(str(report_card.get("decision_label") or "Evidence reviewed"))}</h2>
      <p>{html.escape(str(report_card.get("decision_summary") or sample["executive_summary"]))}</p>
    </section>

    <section class="panel">
      <h2>Why This Is Worth Buying</h2>
      <div class="value-grid">{buyer_value_cards}</div>
    </section>

    <section class="split">
      <div class="panel">
        <h2>What This Establishes</h2>
        <ul>{establishes_items}</ul>
      </div>
      <div class="panel">
        <h2>What It Does Not Establish</h2>
        <ul>{limits_items}</ul>
      </div>
    </section>

    <section class="panel">
      <h2>Executive Summary</h2>
      <p>{html.escape(sample["executive_summary"])}</p>
    </section>

    <section class="panel">
      <h2>Key Findings</h2>
      <div class="claim-list">{claim_cards}</div>
    </section>

    <section class="panel">
      <h2>Evidence Citations</h2>
      <div class="citation-list">{citation_cards}</div>
    </section>

    <section class="panel">
      <h2>Risks and Limits</h2>
      <ul>{risk_items}</ul>
    </section>

    <details class="technical">
      <summary>View buyer command</summary>
      <pre>{buyer_command}</pre>
    </details>

    <details class="technical">
      <summary>View full API JSON</summary>
      <pre>{raw_json}</pre>
    </details>
  </main>
</body>
</html>"""


def build_llms_txt() -> str:
    """
    Return a compact agent-readable service brief.

    The goal is to help crawler agents, planners, and LLM tool routers decide
    when AxonGate is useful without scraping a docs page. It intentionally
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
    proof_bundle_lines = "\n".join(
        f"- {bundle}: {price} USDC, {proof_bundle_policy(bundle)}"
        for bundle, price in PROOF_BUNDLE_PRICING_USDC.items()
    )
    proof_pack_request_example = json.dumps(
        build_proof_pack_request_example(DEFAULT_PROOF_PACK),
        indent=2,
    )

    return f"""# AxonGate

Name: AxonGate
Basename: axongate.base.eth
Summary: x402-paid source trust layer that checks whether public web evidence is safe for AI agents to cite or act on. Clean markdown extraction is available, but the main value is evidence quality, claim support, and citation-ready trust decisions.
Canonical base URL: {PUBLIC_BASE_URL}
Public docs: {public_url("/docs")}
About: {public_url("/about")}
FAQ: {public_url("/faq")}
Contact: {public_url("/contact")}
Operator dashboard: {public_url("/operator")}
Private Proof Pack lead inbox: {public_url("/operator/leads")} (requires AXONGATE_OPERATOR_TOKEN)
Quickstart: {public_url("/quickstart")}
Paid smoke test guide: {public_url("/paid-test")}
Quote API: {public_url("/v1/x402/quote")}
Quote page: {public_url("/quote")}
Proof Pack page: {public_url("/proof-pack")}
Proof Pack sample page: {public_url("/proof-pack/sample")}
Proof Pack sample API: {public_url("/v1/proof-pack/sample")}
Proof Pack mini preview page: {public_url("/proof-pack/preview")}
Proof Pack mini preview API: {public_url("/v1/proof-pack/preview")}
Proof Pack quote page: {public_url("/proof-pack/quote")}
Proof Pack quote API: {public_url("/v1/proof-pack/quote")}
Proof Pack request page: {public_url("/proof-pack/request")}
Proof Pack lead API: {public_url("/v1/proof-pack/leads")}
Proof Pack x402 endpoint: {public_url("/v1/x402/proof-pack")}
Proof Bundle page: {public_url("/proof-pack/bundle")}
Proof Bundle quote page: {public_url("/proof-pack/bundle/quote")}
Proof Bundle quote API: {public_url("/v1/proof-pack/bundle/quote")}
Proof Bundle lead API: {public_url("/v1/proof-pack/bundle/leads")}
Proof Bundle delivery page: {public_url("/proof-pack/bundle/delivery")}?session_id={{CHECKOUT_SESSION_ID}}
Proof Bundle delivery API: {public_url("/v1/proof-pack/bundle/delivery")}?session_id={{CHECKOUT_SESSION_ID}}
Proof Bundle recovery page: {public_url("/proof-pack/bundle/recover")}
Proof Bundle recovery API: {public_url("/v1/proof-pack/bundle/recover")}?email=<checkout-email>&target_url=<submitted-url>
Stripe Proof Bundle fulfillment webhook: {public_url("/v1/stripe/webhook")}
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
Private operator token header: X-AxonGate-Operator-Token

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

## Source Trust Reports

GET {public_url("/proof-pack")}
GET {public_url("/proof-pack/sample")}
GET {public_url("/v1/proof-pack/sample")}
GET {public_url("/proof-pack/preview")}?target_url=<url>&question=<question>&pack=quick|standard|deep
GET {public_url("/v1/proof-pack/preview")}?target_url=<url>&question=<question>&pack=quick|standard|deep
GET {public_url("/proof-pack/quote")}?target_url=<url>&question=<question>&pack=quick|standard|deep
GET {public_url("/v1/proof-pack/quote")}?target_url=<url>&question=<question>&pack=quick|standard|deep
GET {public_url("/proof-pack/request")}?target_url=<url>&question=<question>&pack=quick|standard|deep
POST {public_url("/v1/proof-pack/leads")}
GET {public_url("/operator/leads")} (private operator token required)
GET {public_url("/v1/operator/leads")} (private operator token required)
POST {public_url("/v1/x402/proof-pack")}?pack=standard
Header: X-AxonGate-Pack: standard
Body example:
{proof_pack_request_example}

Proof Pack prices:
{proof_pack_lines}

Multi-source evidence checks:
GET {public_url("/proof-pack/bundle")}
GET {public_url("/proof-pack/bundle/quote")}?target_urls=<newline-separated-urls>&question=<question>&bundle=scout|builder|audit
GET {public_url("/proof-pack/bundle/pay")}?target_urls=<newline-separated-urls>&question=<question>&bundle=scout|builder|audit
GET {public_url("/proof-pack/bundle/delivery")}?session_id={{CHECKOUT_SESSION_ID}}
GET {public_url("/v1/proof-pack/bundle/delivery")}?session_id={{CHECKOUT_SESSION_ID}}
GET {public_url("/proof-pack/bundle/recover")}
GET {public_url("/v1/proof-pack/bundle/recover")}?email=<checkout-email>&target_url=<submitted-url>
GET {public_url("/v1/proof-pack/bundle/quote")}?target_urls=<newline-separated-urls>&question=<question>&bundle=scout|builder|audit
POST {public_url("/v1/proof-pack/bundle/leads")}
POST {public_url("/v1/operator/leads/<lead_id>/status")} (private operator token required; statuses: {", ".join(PROOF_BUNDLE_LEAD_STATUSES)})
POST {public_url("/v1/stripe/webhook")} (Stripe-signed Checkout events; set AXONGATE_STRIPE_WEBHOOK_SECRET)

Proof Bundle prices:
{proof_bundle_lines}

Stripe events:
- checkout.session.completed
- checkout.session.async_payment_succeeded
- checkout.session.async_payment_failed

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
    """Return a small self-contained docs page for agent operators."""
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
    proof_bundle_rows = "\n".join(
        "<tr>"
        f"<td>{html.escape(bundle)}</td>"
        f"<td>{html.escape(str(price))} USDC</td>"
        f"<td>{html.escape(str(proof_bundle_source_limit(bundle)))}</td>"
        f"<td>{html.escape(proof_bundle_policy(bundle))}</td>"
        "</tr>"
        for bundle, price in PROOF_BUNDLE_PRICING_USDC.items()
    )
    proof_request_json = html.escape(json.dumps(build_proof_pack_request_example(DEFAULT_PROOF_PACK), indent=2))
    proof_lead_json = html.escape(
        json.dumps(
            {
                "contact": "builder@example.com",
                "target_url": "https://example.com/source",
                "question": "Which claims can my agent cite from this source?",
                "pack": "quick",
                "use_case": "RAG evaluation",
                "budget_usdc": "10/month",
                "source": "docs",
            },
            indent=2,
        )
    )
    proof_curl_example = html.escape(
        f"""curl -X POST {PUBLIC_BASE_URL}/v1/x402/proof-pack?pack=standard \\
  -H "Content-Type: application/json" \\
  -H "PAYMENT-SIGNATURE: <x402-payment-proof>" \\
  -H "X-AxonGate-Pack: standard" \\
  -d '{{"target_url":"https://example.com/source","question":"What does this source establish?","pack":"standard","force_refresh":false}}'"""
    )
    proof_bundle_lead_json = html.escape(
        json.dumps(
            {
                "contact": "builder@example.com",
                "target_urls": [
                    "https://www.iana.org/domains/reserved",
                    "https://example.com",
                    "https://example.org",
                ],
                "question": "Which claims can our agent safely cite across these sources?",
                "bundle": DEFAULT_PROOF_BUNDLE,
                "use_case": "Agent launch due diligence",
                "budget_usdc": "20/month",
                "source": "docs",
            },
            indent=2,
        )
    )
    nav = site_nav_html("Docs")
    discovery_links = link_cluster_html(
        [
            (
                "Product",
                [
                    ("Source Trust Check", f"{PUBLIC_BASE_URL}/proof-pack"),
                    ("Evidence Bundles", f"{PUBLIC_BASE_URL}/proof-pack/bundle"),
                    ("About", f"{PUBLIC_BASE_URL}/about"),
                    ("FAQ", f"{PUBLIC_BASE_URL}/faq"),
                    ("Contact", f"{PUBLIC_BASE_URL}/contact"),
                    ("Bundle Checkout", f"{PUBLIC_BASE_URL}/proof-pack/bundle/pay"),
                    ("Proof Sample", f"{PUBLIC_BASE_URL}/proof-pack/sample"),
                    ("Proof Preview", f"{PUBLIC_BASE_URL}/proof-pack/preview"),
                    ("Proof Quote", f"{PUBLIC_BASE_URL}/proof-pack/quote"),
                    ("Proof Request", f"{PUBLIC_BASE_URL}/proof-pack/request"),
                ],
            ),
            (
                "Discovery",
                [
                    ("Manifest", f"{PUBLIC_BASE_URL}/manifest.json"),
                    ("Agent card", f"{PUBLIC_BASE_URL}/.well-known/agent.json"),
                    ("x402 discovery", f"{PUBLIC_BASE_URL}/.well-known/x402"),
                    ("Resource listing", f"{PUBLIC_BASE_URL}/discovery/resources"),
                    ("llms.txt", f"{PUBLIC_BASE_URL}/llms.txt"),
                    ("Sitemap", f"{PUBLIC_BASE_URL}/sitemap.xml"),
                ],
            ),
            (
                "Operators",
                [
                    ("Operator dashboard", f"{PUBLIC_BASE_URL}/operator"),
                    ("Private leads", f"{PUBLIC_BASE_URL}/operator/leads"),
                    ("Quickstart", f"{PUBLIC_BASE_URL}/quickstart"),
                    ("Paid test guide", f"{PUBLIC_BASE_URL}/paid-test"),
                    ("Quote", f"{PUBLIC_BASE_URL}/quote"),
                    ("Demo", f"{PUBLIC_BASE_URL}/demo"),
                ],
            ),
            (
                "Examples",
                [
                    ("OpenAPI JSON", f"{PUBLIC_BASE_URL}/openapi.json"),
                    ("Swagger UI", f"{PUBLIC_BASE_URL}/swagger"),
                    ("Python client", f"{GITHUB_REPO_URL}/blob/main/examples/python_client.py"),
                    ("cURL examples", f"{GITHUB_REPO_URL}/blob/main/examples/curl.md"),
                    ("Paid buyer", f"{GITHUB_REPO_URL}/blob/main/examples/paid_buyer.mjs"),
                    ("MCP guide", f"{GITHUB_REPO_URL}/blob/main/examples/mcp.md"),
                ],
            ),
        ]
    )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  {seo_meta_html("AxonGate Docs", "Developer docs for AxonGate source trust reports, Evidence Bundles, x402 payment endpoints, quote APIs, delivery recovery, and agent discovery resources.", "/docs", schema_type="TechArticle")}
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
    {shared_ui_css()}
  </style>
</head>
<body>
  <main>
    {nav}
    <h1>AxonGate</h1>
    <p class="summary">AxonGate is an evidence trust layer for AI agents. It checks whether public web sources actually support a claim, flags weak or noisy pages, and returns citation-ready trust decisions before an agent cites or acts.</p>

    {discovery_links}

    <h2>Service Contract</h2>
    <div class="grid">
      <div class="box"><strong>Trust endpoint</strong><br><code>POST /v1/x402/proof-pack</code></div>
      <div class="box"><strong>Context endpoint</strong><br><code>POST /v1/x402/access</code></div>
      <div class="box"><strong>Retry endpoint</strong><br><code>POST /v1/x402/retry</code></div>
      <div class="box"><strong>Network</strong><br>Base mainnet, <code>eip155:8453</code></div>
      <div class="box"><strong>Vault</strong><br><code>{vault}</code></div>
      <div class="box"><strong>Asset</strong><br>USDC <code>{usdc_address}</code></div>
      <div class="box"><strong>Facilitator</strong><br><code>{facilitator}</code></div>
    </div>

    <h2>Pricing</h2>
    <p>The recommended tier for production agent calls is <code>{html.escape(RECOMMENDED_TIER)}</code>. Starter is available for first paid conversion on the sample target or existing cache; cached, basic, fresh, and deep cover repeat reads and live supplier-backed workloads.</p>
    <div class="table-wrap">
    <table>
      <thead><tr><th>Tier</th><th>Price</th><th>Cache policy</th></tr></thead>
      <tbody>{tiers_rows}</tbody>
    </table>
    </div>

    <h2>Source Trust Reports</h2>
    <p>Source Trust Reports, still called Proof Packs in the API, are paid evidence checks for agent builders. Use them before RAG ingestion, web citations, autonomous actions, or customer-facing answers. They return whether the source supports the claim, what evidence is usable, and what risks make the source weak.</p>
    <div class="table-wrap">
    <table>
      <thead><tr><th>Pack</th><th>Price</th><th>Policy</th></tr></thead>
      <tbody>{proof_pack_rows}</tbody>
    </table>
    </div>
    <pre>curl "{public}/v1/proof-pack/sample?source=docs"</pre>
    <p>Mini preview page: <a href="{public}/proof-pack/preview?target_url=https%3A%2F%2Fwww.iana.org%2Fdomains%2Freserved&amp;pack=quick&amp;source=docs">{public}/proof-pack/preview</a></p>
    <pre>curl "{public}/v1/proof-pack/preview?target_url=https%3A%2F%2Fwww.iana.org%2Fdomains%2Freserved&amp;pack=quick&amp;source=docs"</pre>
    <p>Quote page: <a href="{public}/proof-pack/quote?target_url=https%3A%2F%2Fexample.com&amp;pack=standard&amp;source=docs">{public}/proof-pack/quote</a></p>
    <pre>curl "{public}/v1/proof-pack/quote?target_url=https%3A%2F%2Fexample.com&amp;pack=standard&amp;source=docs"</pre>
    <p>Request capture page: <a href="{public}/proof-pack/request?target_url=https%3A%2F%2Fexample.com&amp;pack=quick&amp;source=docs">{public}/proof-pack/request</a></p>
    <pre>curl -X POST "{public}/v1/proof-pack/leads" \\
  -H "Content-Type: application/json" \\
  -d '{proof_lead_json}'</pre>
    <p>Private lead inbox: <code>{public}/operator/leads</code> and <code>{public}/v1/operator/leads</code> require <code>AXONGATE_OPERATOR_TOKEN</code>. Optional notifications use <code>AXONGATE_PROOF_PACK_LEAD_WEBHOOK_URL</code>.</p>
    <pre>curl -H "X-AxonGate-Operator-Token: &lt;token&gt;" "{public}/v1/operator/leads?limit=25"</pre>
    <pre>{proof_request_json}</pre>
    <pre>{proof_curl_example}</pre>

    <h2>Evidence Bundles</h2>
    <p>Evidence Bundles, still called Proof Bundles in the API, answer a higher-value question: can this group of public sources prove the claim well enough for an agent to cite it? The quote page includes a tracked checkout redirect; configured payment links send buyers to payment, while unconfigured bundles fall back to request capture.</p>
    <div class="table-wrap">
    <table>
      <thead><tr><th>Bundle</th><th>Price</th><th>Sources</th><th>Policy</th></tr></thead>
      <tbody>{proof_bundle_rows}</tbody>
    </table>
    </div>
    <p>Bundle page: <a href="{public}/proof-pack/bundle?source=docs">{public}/proof-pack/bundle</a></p>
    <p>Tracked checkout route: <code>{public}/proof-pack/bundle/pay</code>. Configure payment URLs with <code>AXONGATE_PROOF_BUNDLE_SCOUT_PAYMENT_URL</code>, <code>AXONGATE_PROOF_BUNDLE_BUILDER_PAYMENT_URL</code>, and <code>AXONGATE_PROOF_BUNDLE_AUDIT_PAYMENT_URL</code>.</p>
    <p>Stripe fulfillment webhook: <code>{public}/v1/stripe/webhook</code>. Configure Stripe to send <code>checkout.session.completed</code>, <code>checkout.session.async_payment_succeeded</code>, and <code>checkout.session.async_payment_failed</code>, then set <code>AXONGATE_STRIPE_WEBHOOK_SECRET</code> from the Stripe signing secret.</p>
    <p>Stripe Payment Links should redirect after payment to <code>{public}/proof-pack/bundle/delivery?session_id={{CHECKOUT_SESSION_ID}}</code>. The webhook still handles fulfillment if the buyer closes Stripe before redirecting, and buyers can recover delivery at <code>{public}/proof-pack/bundle/recover</code> with their checkout email and one submitted target URL.</p>
    <p>Email delivery uses Resend when configured. Set <code>AXONGATE_EMAIL_DELIVERY_ENABLED=true</code>, <code>AXONGATE_EMAIL_FROM=AxonGate &lt;reports@axongate.one&gt;</code>, and <code>AXONGATE_RESEND_API_KEY</code>; fulfilled Proof Bundles email the customer a delivery link and concise report summary.</p>
    <pre>curl "{public}/v1/proof-pack/bundle/quote?target_urls=https%3A%2F%2Fwww.iana.org%2Fdomains%2Freserved%0Ahttps%3A%2F%2Fexample.com&amp;bundle=scout&amp;source=docs"</pre>
    <pre>curl -X POST "{public}/v1/proof-pack/bundle/leads" \\
  -H "Content-Type: application/json" \\
  -d '{proof_bundle_lead_json}'</pre>

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
    {site_footer_html()}
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
            card("Proof Previews", count(metric("proof_pack_previews_total")), f'{count(metric("proof_pack_preview_cache_hits_total"))} cache hits'),
            card("Proof Quotes", count(metric("proof_pack_quotes_total")), "No-spend report quotes"),
            card("Proof Leads", count(metric("proof_pack_leads_total")), "Request capture submits"),
            card("Contact Inquiries", count(metric("contact_form_submits_total")), "General buyer/support messages"),
            card("Bundle Quotes", count(metric("proof_bundle_quotes_total")), "Multi-source quote interest"),
            card("Bundle Leads", count(metric("proof_bundle_leads_total")), "Higher-ticket demand capture"),
            card("Bundle Checkout", count(metric("proof_bundle_payment_clicks_total")), "Tracked payment clicks"),
            card("Bundle Paid", count(metric("proof_bundle_paid_total")), "Operator-marked paid"),
            card("Bundle Fulfilled", count(metric("proof_bundle_fulfilled_total")), "Reports delivered"),
            card("Auto Delivery", count(metric("proof_bundle_auto_fulfillment_success_total")), "Stripe-triggered reports"),
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
            f"<tr><td>About</td><td>{count(metric('discovery_about_hits_total'))}</td></tr>",
            f"<tr><td>FAQ</td><td>{count(metric('discovery_faq_hits_total'))}</td></tr>",
            f"<tr><td>Contact</td><td>{count(metric('discovery_contact_hits_total'))}</td></tr>",
            f"<tr><td>Operator</td><td>{count(metric('discovery_operator_hits_total'))}</td></tr>",
            f"<tr><td>Quickstart</td><td>{count(metric('discovery_quickstart_hits_total'))}</td></tr>",
            f"<tr><td>Paid Test Guide</td><td>{count(metric('discovery_paid_test_hits_total'))}</td></tr>",
            f"<tr><td>Quote</td><td>{count(metric('discovery_quote_hits_total'))}</td></tr>",
            f"<tr><td>Proof Packs</td><td>{count(metric('discovery_proof_pack_hits_total'))}</td></tr>",
            f"<tr><td>Proof Pack Sample</td><td>{count(metric('discovery_proof_pack_sample_hits_total'))}</td></tr>",
            f"<tr><td>Proof Pack Preview</td><td>{count(metric('discovery_proof_pack_preview_hits_total'))}</td></tr>",
            f"<tr><td>Proof Pack Quote</td><td>{count(metric('discovery_proof_pack_quote_hits_total'))}</td></tr>",
            f"<tr><td>Proof Pack Request</td><td>{count(metric('discovery_proof_pack_request_hits_total'))}</td></tr>",
            f"<tr><td>Proof Bundle</td><td>{count(metric('discovery_proof_bundle_hits_total'))}</td></tr>",
            f"<tr><td>Proof Bundle Quote</td><td>{count(metric('discovery_proof_bundle_quote_hits_total'))}</td></tr>",
            f"<tr><td>Proof Bundle Request</td><td>{count(metric('discovery_proof_bundle_request_hits_total'))}</td></tr>",
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
    proof_bundle_rows = "\n".join(
        "<tr>"
        f"<td>{html.escape(bundle)}</td>"
        f"<td>{html.escape(str(price))} USDC</td>"
        f"<td>{html.escape(str(usdc_units(price)))}</td>"
        f"<td>{html.escape(str(proof_bundle_source_limit(bundle)))}</td>"
        f"<td>{html.escape(proof_bundle_policy(bundle))}</td>"
        "</tr>"
        for bundle, price in PROOF_BUNDLE_PRICING_USDC.items()
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
      table {{ display: block; max-width: 100%; overflow-x: auto; }}
      th, td {{ white-space: nowrap; }}
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
        <a href="{public}/operator/leads">Private Leads</a>
        <a href="{public}/paid-test">Paid Test</a>
        <a href="{public}/docs">Docs</a>
        <a href="{public}/proof-pack/bundle">Bundles</a>
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

    <h2>Proof Bundle Pricing</h2>
    <table>
      <thead><tr><th>Bundle</th><th>Price</th><th>USDC Units</th><th>Sources</th><th>Policy</th></tr></thead>
      <tbody>{proof_bundle_rows}</tbody>
    </table>

    <p class="notice"><strong>Alert state:</strong> <span class="{'warn' if triggered_alerts else 'ok'}">{html.escape(alert_text)}</span></p>
  </main>
</body>
</html>"""


def build_operator_leads_html(
    leads: list[dict[str, Any]],
    stats: dict[str, Any],
    limit: int,
    operator_token: str = "",
) -> str:
    """Render a private operator view containing raw Proof Pack lead details."""
    public = html.escape(PUBLIC_BASE_URL)

    def esc(value: Any) -> str:
        return html.escape(str(value or ""))

    operator_query = (
        f"?operator_token={url_quote(operator_token, safe='')}&limit={int(limit)}"
        if operator_token
        else f"?limit={int(limit)}"
    )
    operator_query_attr = html.escape(operator_query, quote=True)
    status_options_html = lambda current: "\n".join(
        f'<option value="{esc(status)}"{" selected" if status == current else ""}>{esc(status)}</option>'
        for status in PROOF_BUNDLE_LEAD_STATUSES
    )
    cards = "\n".join(
        [
            f'<div class="card"><span>Retained</span><strong>{esc(stats.get("retained", 0))}</strong><small>latest {esc(lead_created_at_label(stats.get("latest_created_at")))}</small></div>',
            f'<div class="card"><span>Open Pipeline</span><strong>{esc(stats.get("open_value_usdc", 0))}</strong><small>USDC in new/contacted leads</small></div>',
            f'<div class="card"><span>Paid Value</span><strong>{esc(stats.get("paid_value_usdc", 0))}</strong><small>USDC marked paid or fulfilled</small></div>',
            f'<div class="card"><span>Fulfilled Value</span><strong>{esc(stats.get("fulfilled_value_usdc", 0))}</strong><small>USDC marked fulfilled</small></div>',
            f'<div class="card"><span>Webhook</span><strong>{"on" if PROOF_PACK_LEAD_WEBHOOK_URL else "off"}</strong><small>AXONGATE_PROOF_PACK_LEAD_WEBHOOK_URL</small></div>',
            f'<div class="card"><span>Storage</span><strong>{"Redis" if redis_client else "Memory"}</strong><small>{esc(PROOF_PACK_LEADS_REDIS_KEY if redis_client else "process memory")}</small></div>',
        ]
    )
    pack_rows = "\n".join(
        f"<tr><td>{esc(pack)}</td><td>{esc(count)}</td></tr>"
        for pack, count in stats.get("by_pack", {}).items()
    ) or '<tr><td colspan="2">No retained leads.</td></tr>'
    product_rows = "\n".join(
        f"<tr><td>{esc(product)}</td><td>{esc(count)}</td></tr>"
        for product, count in stats.get("by_product", {}).items()
    ) or '<tr><td colspan="2">No retained leads.</td></tr>'
    source_rows = "\n".join(
        f"<tr><td>{esc(source)}</td><td>{esc(count)}</td></tr>"
        for source, count in stats.get("by_source", {}).items()
    ) or '<tr><td colspan="2">No retained leads.</td></tr>'
    status_rows = "\n".join(
        f"<tr><td>{esc(status)}</td><td>{esc(count)}</td></tr>"
        for status, count in stats.get("by_status", {}).items()
    ) or '<tr><td colspan="2">No retained leads.</td></tr>'

    lead_rows = []
    for lead in leads:
        lead = lead_with_pipeline_defaults(lead)
        product = str(lead.get("product") or "proof_pack")
        lead_status = normalize_lead_status(lead.get("status") or "new")
        quote_page = esc(lead.get("quote_page"))
        preview_page = esc(lead.get("preview_page"))
        request_page = esc(lead.get("request_page"))
        payment_url = esc(lead.get("payment_url"))
        probe_url = esc(lead.get("payment_probe_url"))
        paid_endpoint = esc(lead.get("paid_endpoint"))
        buyer_command = esc(lead.get("buyer_command"))
        payment_links = []
        if preview_page:
            payment_links.append(f'<a href="{preview_page}">Preview</a>')
        if quote_page:
            payment_links.append(f'<a href="{quote_page}">Quote</a>')
        if request_page:
            payment_links.append(f'<a href="{request_page}">Request</a>')
        if payment_url:
            payment_links.append(f'<a href="{payment_url}">Payment</a>')
        if probe_url:
            payment_links.append(f'<a href="{probe_url}">Probe</a>')
        if paid_endpoint:
            payment_links.append(f"<code>{paid_endpoint}</code>")
        payment_links_html = "<br>".join(payment_links) or "<small>No payment path stored.</small>"
        fulfillment_url = esc(lead.get("fulfillment_url"))
        fulfillment_link = f'<a href="{fulfillment_url}">Delivery</a><br>' if fulfillment_url else ""
        status_form = f"""
              <form class="status-form" method="post" action="/operator/leads/{esc(lead.get('id'))}/status{operator_query_attr}">
                <label>Status
                  <select name="status">{status_options_html(lead_status)}</select>
                </label>
                <label>Delivery URL
                  <input name="fulfillment_url" value="{fulfillment_url}" placeholder="https://...">
                </label>
                <label>Note
                  <input name="note" value="" placeholder="private update note">
                </label>
                <button type="submit">Update</button>
              </form>
              <small>{fulfillment_link}{esc(lead.get('delivery_note'))}</small>
        """
        target_urls = lead.get("target_urls") if isinstance(lead.get("target_urls"), list) else []
        if target_urls:
            target_links = "<br>".join(
                f'<a href="{esc(target)}">{esc(target)}</a>'
                for target in target_urls[:5]
            )
            remaining = len(target_urls) - 5
            if remaining > 0:
                target_links += f"<br><small>+{remaining} more</small>"
        else:
            target_links = f'<a href="{esc(lead.get("target_url"))}">{esc(lead.get("target_url"))}</a>'
        lead_rows.append(
            "<tr>"
            f"<td><code>{esc(lead.get('id'))}</code><br>{esc(lead_created_at_label(lead.get('created_at')))}</td>"
            f"<td>{esc(lead.get('contact'))}</td>"
            f"<td><code>{esc(product)}</code><br><code>{esc(lead.get('bundle') or lead.get('pack'))}</code><br>{esc(lead.get('price_usdc'))} USDC<br><code>{esc(lead.get('amount_units'))}</code></td>"
            f"<td><code>{esc(lead_status)}</code>{status_form}</td>"
            f"<td>{esc(lead.get('source'))}</td>"
            f"<td>{target_links}<br><small>{esc(lead.get('question'))}</small></td>"
            f"<td>{esc(lead.get('use_case'))}<br><small>{esc(lead.get('budget_usdc'))}</small><br><small>{esc(lead.get('notes'))}</small></td>"
            f"<td>{payment_links_html}</td>"
            f"<td><pre>{buyer_command}</pre></td>"
            "</tr>"
        )
    leads_table = "\n".join(lead_rows) or '<tr><td colspan="9">No Proof Pack leads retained yet.</td></tr>'
    json_export = html.escape(
        json.dumps(
            {
                "status": "ok",
                "limit": limit,
                "stats": stats,
                "leads": leads,
            },
            indent=2,
        )
    )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="60">
  <title>AxonGate Proof Pack Leads</title>
  <style>
    :root {{
      color-scheme: light dark;
      --bg: #101318;
      --panel: #181d24;
      --panel-2: #202630;
      --text: #f5f7fb;
      --muted: #b8c2cf;
      --line: #303844;
      --accent: #78d6b6;
      --code: #0a0d13;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.45;
    }}
    main {{ max-width: 1380px; margin: 0 auto; padding: 28px 18px 56px; }}
    header {{ display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; margin-bottom: 20px; }}
    h1 {{ margin: 0; font-size: 1.85rem; line-height: 1.1; }}
    h2 {{ margin: 26px 0 10px; font-size: 1rem; }}
    p, small {{ color: var(--muted); }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .links {{ display: flex; flex-wrap: wrap; gap: 10px; justify-content: flex-end; }}
    .links a {{ border: 1px solid var(--line); border-radius: 6px; padding: 7px 9px; background: var(--panel); }}
    input, select, button {{
      width: 100%;
      min-width: 0;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 7px 8px;
      background: var(--panel-2);
      color: var(--text);
      font: inherit;
    }}
    button {{ cursor: pointer; }}
    .status-form {{ display: grid; gap: 7px; min-width: 220px; margin-top: 8px; }}
    .status-form label {{ display: grid; gap: 4px; color: var(--muted); font-size: 0.82rem; }}
    .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px; }}
    .card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 13px;
      min-height: 96px;
    }}
    .card span, .card small {{ display: block; color: var(--muted); }}
    .card strong {{ display: block; margin: 5px 0; font-size: 1.45rem; line-height: 1.1; }}
    .split {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }}
    .table-wrap {{ overflow-x: auto; border: 1px solid var(--line); border-radius: 8px; }}
    table {{ width: 100%; min-width: 1100px; border-collapse: collapse; background: var(--panel); }}
    .split table {{ min-width: 0; }}
    th, td {{ padding: 10px 11px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }}
    th {{ color: var(--text); background: var(--panel-2); font-size: 0.9rem; white-space: nowrap; }}
    td {{ color: var(--muted); max-width: 360px; overflow-wrap: anywhere; }}
    code, pre {{
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      background: var(--code);
      color: var(--text);
      border: 1px solid var(--line);
      border-radius: 4px;
    }}
    code {{ display: inline-block; max-width: 100%; padding: 1px 5px; overflow-wrap: anywhere; }}
    pre {{ max-width: 360px; max-height: 160px; overflow: auto; white-space: pre-wrap; word-break: break-word; padding: 8px; margin: 0; }}
    .json pre {{ max-width: 100%; max-height: 460px; padding: 14px; }}
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
        <h1>Proof Pack Leads</h1>
        <p>Private operator view. Raw contact fields are only available after token authentication.</p>
      </div>
      <nav class="links" aria-label="Lead operator links">
        <a href="{public}/operator">Operator</a>
        <a href="{public}/metrics">Metrics JSON</a>
        <a href="{public}/proof-pack/request">Request Page</a>
        <a href="{public}/proof-pack/quote">Quote Page</a>
        <a href="{public}/v1/operator/leads?limit={int(limit)}">Lead JSON</a>
      </nav>
    </header>

    <section class="cards">{cards}</section>

    <div class="split">
      <section>
        <h2>By Product</h2>
        <table><thead><tr><th>Product</th><th>Leads</th></tr></thead><tbody>{product_rows}</tbody></table>
      </section>
      <section>
        <h2>By Pack</h2>
        <table><thead><tr><th>Pack</th><th>Leads</th></tr></thead><tbody>{pack_rows}</tbody></table>
      </section>
    </div>

    <div class="split">
      <section>
        <h2>By Source</h2>
        <table><thead><tr><th>Source</th><th>Leads</th></tr></thead><tbody>{source_rows}</tbody></table>
      </section>
      <section>
        <h2>By Status</h2>
        <table><thead><tr><th>Status</th><th>Leads</th></tr></thead><tbody>{status_rows}</tbody></table>
      </section>
    </div>

    <h2>Lead Inbox</h2>
    <div class="table-wrap">
      <table>
        <thead><tr><th>ID / Time</th><th>Contact</th><th>Pack</th><th>Status / Delivery</th><th>Source</th><th>Target / Question</th><th>Use Case</th><th>Payment Links</th><th>Buyer Command</th></tr></thead>
        <tbody>{leads_table}</tbody>
      </table>
    </div>

    <h2>Private JSON Snapshot</h2>
    <section class="json"><pre>{json_export}</pre></section>
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
                            "AXONGATE_PROOF_CONFIRM_SPEND": str(PROOF_PACK_PRICING_USDC["quick"]),
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
    mcp_proof_paid = html.escape(
        f"""Tool: fetch_proof_pack
Input:
{{
  "target_url": "https://www.iana.org/domains/reserved",
  "question": "What does this source establish about reserved domains?",
  "pack": "quick",
  "force_refresh": false,
  "confirm_spend_usdc": "{PROOF_PACK_PRICING_USDC["quick"]}",
  "source": "quickstart-mcp-proof",
  "max_answer_chars": 1800,
  "max_citation_excerpt_chars": 360
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
    nav = site_nav_html("Quickstart")
    actions = action_bar_html(
        [
            ("Paid Test", f"{PUBLIC_BASE_URL}/paid-test"),
            ("Get Quote", f"{PUBLIC_BASE_URL}/quote"),
            ("Proof Packs", f"{PUBLIC_BASE_URL}/proof-pack"),
        ],
        [
            ("Demo", f"{PUBLIC_BASE_URL}/demo"),
            ("Proof Sample", f"{PUBLIC_BASE_URL}/proof-pack/sample"),
            ("Proof Preview", f"{PUBLIC_BASE_URL}/proof-pack/preview"),
            ("Proof Quote", f"{PUBLIC_BASE_URL}/proof-pack/quote"),
            ("Proof Bundles", f"{PUBLIC_BASE_URL}/proof-pack/bundle"),
            ("Docs", f"{PUBLIC_BASE_URL}/docs"),
            ("Operator", f"{PUBLIC_BASE_URL}/operator"),
            ("Buyer Script", f"{GITHUB_REPO_URL}/blob/main/examples/paid_buyer.mjs"),
            ("MCP Server", f"{GITHUB_REPO_URL}/blob/main/examples/axongate_mcp.mjs"),
        ],
    )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  {seo_meta_html("AxonGate Quickstart", "Run your first AxonGate paid source trust check with x402 on Base USDC, burner wallet setup, buyer examples, and agent workflow guidance.", "/quickstart", schema_type="TechArticle")}
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
    {shared_ui_css()}
  </style>
</head>
<body>
  <main>
    {nav}
    <h1>AxonGate Quickstart</h1>
    <p>Run a paid evidence trust check before your agent cites a public source. AxonGate can also return clean RAG-ready markdown, but the higher-value path is the Source Trust Report: supported, weak, unsupported, and citation-ready evidence.</p>
    {actions}

    <div class="steps">
      <div class="step"><strong>1. Fund burner wallet</strong>Use Base USDC. Starter costs <code>{starter_price} USDC</code>; fresh costs <code>{fresh_price} USDC</code>.</div>
      <div class="step"><strong>2. Run the buyer</strong>The script probes payment terms, signs x402, pays, and returns a trust report or clean context.</div>
      <div class="step"><strong>3. Watch attribution</strong>Use <code>source=quickstart</code> so `/metrics` shows the conversion path.</div>
      <div class="step"><strong>4. Sell trust</strong>Use <code>/proof-pack</code> when a buyer needs to know whether a source is safe to cite.</div>
    </div>

    <p class="callout"><strong>This can spend real USDC.</strong> Every paid path requires an explicit <code>confirm-spend</code> or <code>confirm_spend_usdc</code> value that must match the selected tier.</p>

    <h2>Fast Starter Path</h2>
    <pre>{terminal_commands}</pre>

    <h2>Expected Shape</h2>
    <pre>{expected_output}</pre>

    <h2>MCP Agent Path</h2>
    <p>Add the server to an MCP-capable client, then call the probe tool first. Paid tools refuse to run unless the confirmed spend matches the selected tier or pack price.</p>
    <h3>Client Config</h3>
    <pre>{mcp_config}</pre>
    <h3>Probe Tool</h3>
    <pre>{mcp_probe}</pre>
    <h3>Paid Tool</h3>
    <pre>{mcp_paid}</pre>
    <h3>Proof Pack Tool</h3>
    <pre>{mcp_proof_paid}</pre>

    <h2>Pricing</h2>
    <div class="table-wrap">
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
    </div>
    {site_footer_html()}
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
    nav = site_nav_html("Quickstart")
    actions = action_bar_html(
        [
            ("Operator Dashboard", f"{PUBLIC_BASE_URL}/operator"),
            ("Metrics JSON", f"{PUBLIC_BASE_URL}/metrics"),
            ("Quickstart", f"{PUBLIC_BASE_URL}/quickstart"),
        ],
        [
            ("Docs", f"{PUBLIC_BASE_URL}/docs"),
            ("Buyer Script", f"{GITHUB_REPO_URL}/blob/main/examples/paid_buyer.mjs"),
        ],
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
    {shared_ui_css()}
  </style>
</head>
<body>
  <main>
    {nav}
    <h1>AxonGate Paid Test</h1>
    <p>Run a real Base USDC x402 smoke test against production, with a bounded spend confirmation and replay check.</p>
    {actions}

    <p class="callout"><strong>This spends real USDC.</strong> The starter tier currently authorizes <code>{html.escape(str(TIER_PRICING_USDC[STARTER_TIER]))} USDC</code>. Use a burner wallet and keep the explicit <code>--confirm-spend</code> value in the command.</p>

    <h2>Command</h2>
    <pre>{wallet_command}</pre>

    <h2>Environment Variant</h2>
    <pre>{env_command}</pre>

    <h2>Expected Result</h2>
    <pre>{expected_result}</pre>

    <h2>What To Check</h2>
    <div class="table-wrap">
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
    </div>
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
    """Return a small XML sitemap for public and agent discovery URLs."""
    now = time.strftime("%Y-%m-%d", time.gmtime())
    entries = [
        ("/", "1.0"),
        ("/docs", "0.9"),
        ("/about", "0.85"),
        ("/faq", "0.85"),
        ("/contact", "0.85"),
        ("/operator", "0.9"),
        ("/quickstart", "0.95"),
        ("/paid-test", "0.9"),
        ("/quote", "0.9"),
        ("/v1/x402/quote", "0.8"),
        ("/proof-pack", "0.95"),
        ("/proof-pack/sample", "0.9"),
        ("/proof-pack/preview", "0.9"),
        ("/proof-pack/quote", "0.9"),
        ("/proof-pack/request", "0.9"),
        ("/proof-pack/bundle", "0.9"),
        ("/proof-pack/bundle/quote", "0.9"),
        ("/proof-pack/bundle/checkout", "0.85"),
        ("/proof-pack/bundle/pay", "0.8"),
        ("/proof-pack/bundle/recover", "0.8"),
        ("/v1/proof-pack/sample", "0.85"),
        ("/v1/proof-pack/preview", "0.85"),
        ("/v1/proof-pack/quote", "0.85"),
        ("/v1/proof-pack/bundle/quote", "0.85"),
        ("/v1/proof-pack/bundle/leads", "0.75"),
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


@app.get("/docs", response_class=HTMLResponse, tags=["discovery"], summary="AxonGate docs")
async def docs_page(request: Request):
    """Serve a lightweight docs page; Swagger remains available at /swagger."""
    inc_discovery_hit("discovery_docs_hits_total", attribution_source_from_request(request))
    return build_docs_html()


@app.get("/about", response_class=HTMLResponse, tags=["discovery"], summary="About AxonGate")
async def about_page(request: Request):
    """Serve the public About page."""
    inc_discovery_hit("discovery_about_hits_total", attribution_source_from_request(request))
    return build_about_html()


@app.get("/faq", response_class=HTMLResponse, tags=["discovery"], summary="AxonGate FAQ")
async def faq_page(request: Request):
    """Serve a public FAQ page for buyers and builders."""
    inc_discovery_hit("discovery_faq_hits_total", attribution_source_from_request(request))
    return build_faq_html()


@app.get("/contact", response_class=HTMLResponse, tags=["discovery"], summary="Contact AxonGate")
async def contact_page(request: Request):
    """Serve the public contact form."""
    inc_discovery_hit("discovery_contact_hits_total", attribution_source_from_request(request))
    return build_contact_html({"source": attribution_source_from_request(request) or "contact-page"})


@app.post("/contact", response_class=HTMLResponse, tags=["discovery"], summary="Submit AxonGate contact form")
async def contact_form_submit(request: Request):
    """Accept a browser contact form and store the inquiry privately."""
    source = attribution_source_from_request(request)
    inc_discovery_hit("discovery_contact_hits_total", source)
    payload = parse_urlencoded_payload(await request.body())
    try:
        await enforce_rate_limit("contact_form_ip", client_rate_identifier(request), RATE_LIMIT_UNPAID_PER_IP)
        lead = await create_contact_lead(payload, request)
    except Exception as exc:
        inc_metric("contact_form_errors_total")
        detail = exc.detail if isinstance(exc, PaymentValidationError) else str(exc)
        return HTMLResponse(build_contact_html(payload, error=clean_lead_text(str(detail), 320)), status_code=400)
    return build_contact_html(payload, submitted=contact_public_response(lead))


@app.post("/v1/contact", tags=["discovery"], summary="Submit AxonGate contact inquiry")
async def contact_api(request: Request, contact_request: ContactRequest):
    """Store a public contact inquiry for operator follow-up."""
    source = attribution_source_from_request(request)
    inc_discovery_hit("discovery_contact_hits_total", source)
    try:
        await enforce_rate_limit("contact_form_ip", client_rate_identifier(request), RATE_LIMIT_UNPAID_PER_IP)
        lead = await create_contact_lead(contact_request, request)
    except PaymentValidationError as exc:
        inc_metric("contact_form_errors_total")
        raise HTTPException(status_code=400, detail=exc.detail) from exc
    return contact_public_response(lead)


@app.get("/operator", response_class=HTMLResponse, tags=["operations"], summary="Operator conversion dashboard")
async def operator_dashboard(request: Request):
    """Serve a public operator view backed by the metrics endpoint data."""
    inc_discovery_hit("discovery_operator_hits_total", attribution_source_from_request(request))
    metric_values = await durable_metrics_snapshot()
    attribution = await durable_attribution_snapshot()
    rolling_attribution = await durable_rolling_attribution_snapshot()
    triggered_alerts = await evaluate_alerts(metric_values)
    return build_operator_dashboard_html(metric_values, attribution, rolling_attribution, triggered_alerts)


@app.post("/v1/stripe/webhook", tags=["operations"], summary="Stripe Checkout fulfillment webhook")
async def stripe_webhook(request: Request, stripe_signature: Optional[str] = Header(None, alias="Stripe-Signature")):
    """Accept verified Stripe Checkout events and create paid Proof Bundle leads."""
    inc_metric("stripe_webhook_events_total")
    if not STRIPE_WEBHOOK_SECRET:
        inc_metric("stripe_webhook_misconfigured_total")
        raise HTTPException(
            status_code=503,
            detail="Stripe webhook secret is not configured. Set AXONGATE_STRIPE_WEBHOOK_SECRET.",
        )

    raw_body = await request.body()
    try:
        event = verify_stripe_webhook_payload(raw_body, stripe_signature, STRIPE_WEBHOOK_SECRET)
    except PaymentValidationError as exc:
        inc_metric("stripe_webhook_signature_failures_total")
        raise HTTPException(status_code=400, detail=exc.detail) from exc

    event_id = clean_lead_text(str(event.get("id") or ""), 120)
    event_type = clean_lead_text(str(event.get("type") or ""), 120)
    if not event_id or not event_type:
        inc_metric("stripe_webhook_fulfillment_errors_total")
        raise HTTPException(status_code=400, detail="Stripe event must include id and type.")

    if await stripe_event_already_processed(event_id):
        inc_metric("stripe_webhook_duplicate_events_total")
        return {"status": "duplicate", "event_id": event_id, "event_type": event_type}

    inc_metric("stripe_webhook_verified_total")

    if event_type == "checkout.session.async_payment_failed":
        inc_metric("stripe_webhook_payment_failed_total")
        await mark_stripe_event_processed(event_id)
        return {"status": "payment_failed", "event_id": event_id, "event_type": event_type}

    if event_type not in {"checkout.session.completed", "checkout.session.async_payment_succeeded"}:
        inc_metric("stripe_webhook_unsupported_events_total")
        await mark_stripe_event_processed(event_id)
        return {"status": "ignored", "event_id": event_id, "event_type": event_type}

    try:
        session = stripe_event_object(event)
        if not stripe_checkout_paid(event_type, session):
            inc_metric("stripe_webhook_pending_payment_total")
            await mark_stripe_event_processed(event_id)
            return {
                "status": "pending_payment",
                "event_id": event_id,
                "event_type": event_type,
                "payment_status": session.get("payment_status"),
            }
        lead = await fulfill_stripe_checkout_session(event, session)
    except Exception as exc:
        inc_metric("stripe_webhook_fulfillment_errors_total")
        print(f"[STRIPE_WEBHOOK] Fulfillment failed for {event_id}: {exc}")
        raise HTTPException(status_code=500, detail="Stripe webhook fulfillment failed.") from exc

    inc_metric("stripe_webhook_payment_succeeded_total")
    await mark_stripe_event_processed(event_id)
    schedule_background(generate_and_store_proof_bundle_delivery(str(lead.get("id") or "")))
    return {
        "status": "fulfilled",
        "event_id": event_id,
        "event_type": event_type,
        "lead_id": lead.get("id"),
        "bundle": lead.get("bundle"),
        "lead_status": lead.get("status"),
        "delivery_url": lead.get("fulfillment_url"),
        "operator_leads": public_url("/operator/leads"),
    }


@app.get("/operator/leads", response_class=HTMLResponse, tags=["operations"], summary="Private Proof Pack lead inbox")
async def operator_leads_page(request: Request, limit: int = 50):
    """Serve a token-protected operator inbox with raw Proof Pack lead contact data."""
    require_operator_access(request)
    inc_discovery_hit("discovery_operator_leads_hits_total", attribution_source_from_request(request))
    bounded_limit = max(1, min(limit, PROOF_PACK_LEADS_MEMORY_MAX, 200))
    leads = await durable_proof_pack_leads(bounded_limit)
    query_token = request.query_params.get("operator_token") or request.query_params.get("token") or ""
    return build_operator_leads_html(leads, proof_pack_lead_stats(leads), bounded_limit, query_token)


@app.get("/v1/operator/leads", tags=["operations"], summary="Private Proof Pack lead JSON")
async def operator_leads_api(request: Request, limit: int = 50):
    """Return token-protected Proof Pack leads with raw contact data for private automation."""
    require_operator_access(request)
    inc_discovery_hit("discovery_operator_leads_hits_total", attribution_source_from_request(request))
    bounded_limit = max(1, min(limit, PROOF_PACK_LEADS_MEMORY_MAX, 200))
    leads = await durable_proof_pack_leads(bounded_limit)
    return {
        "status": "ok",
        "limit": bounded_limit,
        "stats": proof_pack_lead_stats(leads),
        "leads": leads,
        "notification": {
            "webhook_enabled": bool(PROOF_PACK_LEAD_WEBHOOK_URL),
            "token_header": "X-AxonGate-Operator-Token",
        },
    }


@app.post("/v1/operator/leads/{lead_id}/status", tags=["operations"], summary="Update private lead pipeline status")
async def operator_lead_status_api(request: Request, lead_id: str, status_update: OperatorLeadStatusUpdate):
    """Move a private lead through the operator pipeline."""
    require_operator_access(request)
    try:
        updated = await update_proof_pack_lead_status(
            lead_id,
            status_update.status,
            note=status_update.note,
            fulfillment_url=status_update.fulfillment_url,
            delivery_note=status_update.delivery_note,
        )
    except PaymentValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.detail) from exc
    if not updated:
        raise HTTPException(status_code=404, detail="Lead not found.")
    return {"status": "ok", "lead": updated, "stats": proof_pack_lead_stats(await durable_proof_pack_leads())}


@app.post("/operator/leads/{lead_id}/status", response_class=HTMLResponse, tags=["operations"], summary="Update private lead pipeline status from operator page")
async def operator_lead_status_form(request: Request, lead_id: str, limit: int = 50):
    """Accept a browser form update for private lead status."""
    require_operator_access(request)
    payload = parse_urlencoded_payload(await request.body())
    try:
        updated = await update_proof_pack_lead_status(
            lead_id,
            str(payload.get("status") or "new"),
            note=payload.get("note"),
            fulfillment_url=payload.get("fulfillment_url"),
            delivery_note=payload.get("delivery_note"),
        )
    except PaymentValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.detail) from exc
    if not updated:
        raise HTTPException(status_code=404, detail="Lead not found.")
    bounded_limit = max(1, min(limit, PROOF_PACK_LEADS_MEMORY_MAX, 200))
    leads = await durable_proof_pack_leads(bounded_limit)
    query_token = request.query_params.get("operator_token") or request.query_params.get("token") or ""
    return build_operator_leads_html(leads, proof_pack_lead_stats(leads), bounded_limit, query_token)


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
    """Serve the Proof Pack product page for buyers and agent builders."""
    inc_discovery_hit("discovery_proof_pack_hits_total", attribution_source_from_request(request))
    return build_proof_pack_html()


@app.get("/proof-pack/bundle", response_class=HTMLResponse, tags=["discovery"], summary="AxonGate Proof Bundle product page")
async def proof_bundle_page(
    request: Request,
    target_urls: Optional[str] = None,
    target_url: Optional[str] = None,
    question: Optional[str] = None,
    bundle: str = DEFAULT_PROOF_BUNDLE,
    contact: Optional[str] = None,
    use_case: Optional[str] = None,
    budget_usdc: Optional[str] = None,
    notes: Optional[str] = None,
):
    """Serve the higher-ticket multi-source Proof Bundle page."""
    source = attribution_source_from_request(request)
    inc_discovery_hit("discovery_proof_bundle_hits_total", source)
    return build_proof_bundle_html(
        {
            "contact": contact,
            "target_urls": target_urls or target_url,
            "question": question,
            "bundle": bundle,
            "use_case": use_case,
            "budget_usdc": budget_usdc,
            "source": source,
            "notes": notes,
        }
    )


@app.get("/proof-pack/bundle/quote", response_class=HTMLResponse, tags=["discovery"], summary="Proof Bundle quote page")
async def proof_bundle_quote_page(
    request: Request,
    target_urls: str = "https://www.iana.org/domains/reserved\nhttps://example.com\nhttps://example.org",
    question: Optional[str] = None,
    bundle: str = DEFAULT_PROOF_BUNDLE,
):
    """Serve a no-spend multi-source bundle quote page for buyers."""
    source = attribution_source_from_request(request)
    inc_metric("proof_bundle_quotes_total")
    inc_attribution("proof_bundle_quotes", source)
    inc_discovery_hit("discovery_proof_bundle_quote_hits_total", source)
    try:
        await enforce_rate_limit("proof_bundle_quote_ip", client_rate_identifier(request), RATE_LIMIT_UNPAID_PER_IP)
        quote = await build_proof_bundle_quote(split_bundle_target_urls(target_urls), question, bundle, source)
    except RateLimitExceeded as exc:
        raise rate_limit_429(exc) from exc
    except PaymentValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.detail) from exc
    return build_proof_bundle_quote_html(quote)


@app.post("/proof-pack/bundle/request", response_class=HTMLResponse, tags=["discovery"], summary="Submit a Proof Bundle request")
async def proof_bundle_request_submit(request: Request):
    """Store a submitted Proof Bundle request without payment or supplier work."""
    source = attribution_source_from_request(request)
    inc_discovery_hit("discovery_proof_bundle_request_hits_total", source)
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    try:
        await enforce_rate_limit("proof_bundle_lead_ip", client_rate_identifier(request), RATE_LIMIT_UNPAID_PER_IP)
        if content_type == "application/json":
            payload = await request.json()
            if not isinstance(payload, dict):
                raise PaymentValidationError("JSON request body must be an object.")
        else:
            payload = parse_urlencoded_payload(await request.body())
        payload.setdefault("source", source)
        lead = await create_proof_bundle_lead(payload, request)
    except RateLimitExceeded as exc:
        raise rate_limit_429(exc) from exc
    except (json.JSONDecodeError, PaymentValidationError) as exc:
        inc_metric("proof_bundle_lead_errors_total")
        detail = exc.detail if isinstance(exc, PaymentValidationError) else "JSON request body is invalid."
        return HTMLResponse(build_proof_bundle_html(payload if "payload" in locals() else {}, error=detail), status_code=400)

    public_response = proof_bundle_lead_public_response(lead)
    return build_proof_bundle_html(lead, submitted=public_response)


@app.get("/v1/proof-pack/bundle/quote", tags=["discovery"], summary="Supplier-free Proof Bundle quote")
async def proof_bundle_quote_api(
    request: Request,
    target_urls: str = "https://www.iana.org/domains/reserved\nhttps://example.com\nhttps://example.org",
    question: Optional[str] = None,
    bundle: str = DEFAULT_PROOF_BUNDLE,
):
    """Return no-spend Proof Bundle pricing and buyer next steps for public targets."""
    source = attribution_source_from_request(request)
    inc_metric("proof_bundle_quotes_total")
    inc_attribution("proof_bundle_quotes", source)
    inc_discovery_hit("discovery_proof_bundle_quote_hits_total", source)
    try:
        await enforce_rate_limit("proof_bundle_quote_ip", client_rate_identifier(request), RATE_LIMIT_UNPAID_PER_IP)
        return await build_proof_bundle_quote(split_bundle_target_urls(target_urls), question, bundle, source)
    except RateLimitExceeded as exc:
        raise rate_limit_429(exc) from exc
    except PaymentValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.detail) from exc


@app.get("/proof-pack/bundle/checkout", response_class=HTMLResponse, tags=["discovery"], summary="Review Proof Bundle checkout")
async def proof_bundle_checkout_review(
    request: Request,
    target_urls: str = "",
    question: Optional[str] = None,
    bundle: str = DEFAULT_PROOF_BUNDLE,
):
    """Show a customer-facing review step before the tracked Stripe redirect."""
    source = attribution_source_from_request(request)
    inc_discovery_hit("discovery_proof_bundle_hits_total", source)
    raw_targets = split_bundle_target_urls(target_urls)
    try:
        normalized_bundle = normalize_proof_bundle(bundle)
        normalized_question = proof_bundle_question(question)
        quote = None
        normalized_targets = raw_targets
        if raw_targets:
            await enforce_rate_limit("proof_bundle_quote_ip", client_rate_identifier(request), RATE_LIMIT_UNPAID_PER_IP)
            quote = await build_proof_bundle_quote(raw_targets, normalized_question, normalized_bundle, source)
            normalized_targets = list(quote["target_urls"])
    except RateLimitExceeded as exc:
        raise rate_limit_429(exc) from exc
    except PaymentValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.detail) from exc
    return build_proof_bundle_checkout_html(normalized_targets, normalized_question, normalized_bundle, source, quote)


@app.get("/proof-pack/bundle/pay", response_class=RedirectResponse, tags=["discovery"], summary="Tracked Proof Bundle checkout")
async def proof_bundle_checkout(
    request: Request,
    target_urls: str = "",
    question: Optional[str] = None,
    bundle: str = DEFAULT_PROOF_BUNDLE,
):
    """Track checkout intent and redirect to configured payment link or request capture."""
    source = attribution_source_from_request(request)
    try:
        normalized_bundle = normalize_proof_bundle(bundle)
    except PaymentValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.detail) from exc
    raw_targets = split_bundle_target_urls(target_urls)
    normalized_question = proof_bundle_question(question)
    inc_metric("proof_bundle_payment_clicks_total")
    inc_attribution("proof_bundle_payment_clicks", source)
    external_payment_url = proof_bundle_payment_url(normalized_bundle)
    if external_payment_url:
        inc_metric("proof_bundle_payment_configured_clicks_total")
        return RedirectResponse(external_payment_url, status_code=302)

    inc_metric("proof_bundle_payment_missing_clicks_total")
    fallback_url = (
        proof_bundle_request_page_url(raw_targets, normalized_question, normalized_bundle, source)
        if raw_targets
        else f"{PUBLIC_BASE_URL}/proof-pack/bundle?bundle={url_quote(normalized_bundle, safe='')}&source={url_quote(source, safe='')}"
    )
    return RedirectResponse(fallback_url, status_code=302)


async def resolve_paid_bundle_delivery_lead(lead_id: str = "", session_id: str = "") -> Optional[dict[str, Any]]:
    if session_id:
        return await find_stored_proof_bundle_lead_by_session(session_id)
    if lead_id:
        lead = await find_stored_proof_pack_lead(lead_id)
        if lead and str(lead.get("product") or "") == "proof_bundle":
            return lead
    return None


async def ensure_proof_bundle_delivery_ready(lead: dict[str, Any]) -> dict[str, Any]:
    """Generate a paid bundle report synchronously when possible."""
    if not isinstance(lead.get("proof_bundle_report"), dict) and normalize_lead_status(lead.get("status") or "new") == "paid":
        try:
            generated = await asyncio.wait_for(generate_and_store_proof_bundle_delivery(str(lead.get("id") or "")), timeout=25)
            if generated:
                lead = generated
        except asyncio.TimeoutError:
            schedule_background(generate_and_store_proof_bundle_delivery(str(lead.get("id") or "")))
    ready = await find_stored_proof_pack_lead(str(lead.get("id") or "")) or lead
    if isinstance(ready.get("proof_bundle_report"), dict):
        emailed = await send_proof_bundle_delivery_email(ready)
        return emailed or await find_stored_proof_pack_lead(str(ready.get("id") or "")) or ready
    return ready


@app.get("/proof-pack/bundle/recover", response_class=HTMLResponse, tags=["discovery"], summary="Recover Proof Bundle delivery")
async def proof_bundle_recovery_page(request: Request, email: str = "", target_url: str = ""):
    """Recover a paid Proof Bundle delivery without a Stripe Checkout Session redirect."""
    inc_metric("proof_bundle_recovery_requests_total")
    if not email and not target_url:
        return build_proof_bundle_recovery_html()
    if not email or not target_url:
        return HTMLResponse(
            build_proof_bundle_recovery_html(email, target_url, "Enter both the Stripe checkout email and one target URL from the purchase."),
            status_code=400,
        )

    lead = await find_stored_proof_bundle_lead_for_recovery(email, target_url)
    if not lead:
        return HTMLResponse(
            build_proof_bundle_recovery_html(email, target_url, "No paid Proof Bundle matched that email and target URL yet."),
            status_code=404,
        )
    lead = await apply_recovery_target_override(lead, target_url)
    lead = await ensure_proof_bundle_delivery_ready(lead)
    return build_proof_bundle_delivery_html(lead)


@app.get("/v1/proof-pack/bundle/recover", tags=["discovery"], summary="Recover Proof Bundle delivery JSON")
async def proof_bundle_recovery_api(request: Request, email: str = "", target_url: str = ""):
    """Recover a paid Proof Bundle delivery by checkout email and one target URL."""
    inc_metric("proof_bundle_recovery_requests_total")
    if not email or not target_url:
        raise HTTPException(status_code=400, detail="email and target_url are required.")
    lead = await find_stored_proof_bundle_lead_for_recovery(email, target_url)
    if not lead:
        raise HTTPException(status_code=404, detail="No paid Proof Bundle matched that email and target URL.")
    lead = await apply_recovery_target_override(lead, target_url)
    lead = await ensure_proof_bundle_delivery_ready(lead)
    return build_proof_bundle_delivery_payload(lead)


@app.get("/proof-pack/bundle/delivery.json", tags=["discovery"], summary="Download Proof Bundle delivery JSON")
async def proof_bundle_delivery_json_download(request: Request, lead_id: str = "", session_id: str = ""):
    """Download the paid Proof Bundle delivery payload as JSON."""
    lead = await resolve_paid_bundle_delivery_lead(lead_id, session_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Delivery was not found. If payment just completed, retry shortly.")
    lead = await ensure_proof_bundle_delivery_ready(lead)
    payload = build_proof_bundle_delivery_payload(lead)
    filename = f"axongate-proof-bundle-{clean_lead_text(str(payload.get('lead_id') or 'report'), 80)}.json"
    return Response(
        content=json.dumps(payload, ensure_ascii=True, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/proof-pack/bundle/delivery.pdf", tags=["discovery"], summary="Download Proof Bundle delivery PDF")
async def proof_bundle_delivery_pdf_download(request: Request, lead_id: str = "", session_id: str = ""):
    """Download a customer-readable PDF version of the paid Proof Bundle report."""
    lead = await resolve_paid_bundle_delivery_lead(lead_id, session_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Delivery was not found. If payment just completed, retry shortly.")
    lead = await ensure_proof_bundle_delivery_ready(lead)
    payload = build_proof_bundle_delivery_payload(lead)
    filename = f"axongate-proof-bundle-{clean_lead_text(str(payload.get('lead_id') or 'report'), 80)}.pdf"
    return Response(
        content=build_proof_bundle_delivery_pdf(lead),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/proof-pack/bundle/delivery/print", response_class=HTMLResponse, tags=["discovery"], summary="Print Proof Bundle delivery")
async def proof_bundle_delivery_print_page(request: Request, lead_id: str = "", session_id: str = ""):
    """Serve a print-friendly paid Proof Bundle report page."""
    lead = await resolve_paid_bundle_delivery_lead(lead_id, session_id)
    if not lead:
        return HTMLResponse(
            build_proof_bundle_delivery_html(None, error="Delivery was not found. If payment just completed, refresh this page in a moment."),
            status_code=404,
        )
    lead = await ensure_proof_bundle_delivery_ready(lead)
    return build_proof_bundle_delivery_html(lead, print_mode=True)


@app.get("/proof-pack/bundle/delivery", response_class=HTMLResponse, tags=["discovery"], summary="Proof Bundle delivery page")
async def proof_bundle_delivery_page(request: Request, lead_id: str = "", session_id: str = ""):
    """Serve the customer-facing Proof Bundle delivery page after Stripe checkout."""
    inc_metric("proof_bundle_delivery_requests_total")
    lead = await resolve_paid_bundle_delivery_lead(lead_id, session_id)
    if not lead:
        return HTMLResponse(
            build_proof_bundle_delivery_html(None, error="Delivery was not found. If payment just completed, refresh this page in a moment."),
            status_code=404,
        )
    lead = await ensure_proof_bundle_delivery_ready(lead)
    return build_proof_bundle_delivery_html(lead)


@app.get("/v1/proof-pack/bundle/delivery", tags=["discovery"], summary="Proof Bundle delivery JSON")
async def proof_bundle_delivery_api(request: Request, lead_id: str = "", session_id: str = ""):
    """Return the paid Proof Bundle delivery report when ready."""
    inc_metric("proof_bundle_delivery_requests_total")
    lead = await resolve_paid_bundle_delivery_lead(lead_id, session_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Delivery was not found. If payment just completed, retry shortly.")
    lead = await ensure_proof_bundle_delivery_ready(lead)
    return build_proof_bundle_delivery_payload(lead)


@app.post("/v1/proof-pack/bundle/leads", tags=["discovery"], summary="No-spend Proof Bundle lead capture")
async def proof_bundle_lead_api(request: Request, lead_request: ProofBundleLeadRequest):
    """Store a multi-source Proof Bundle lead and return quote-ready next steps without spending."""
    inc_discovery_hit("discovery_proof_bundle_request_hits_total", attribution_source_from_request(request))
    try:
        await enforce_rate_limit("proof_bundle_lead_ip", client_rate_identifier(request), RATE_LIMIT_UNPAID_PER_IP)
        lead = await create_proof_bundle_lead(lead_request, request)
    except RateLimitExceeded as exc:
        raise rate_limit_429(exc) from exc
    except PaymentValidationError as exc:
        inc_metric("proof_bundle_lead_errors_total")
        raise HTTPException(status_code=400, detail=exc.detail) from exc
    return proof_bundle_lead_public_response(lead)


@app.get("/proof-pack/sample", response_class=HTMLResponse, tags=["discovery"], summary="No-spend Proof Pack sample page")
async def proof_pack_sample_page(request: Request):
    """Serve a readable no-spend Proof Pack sample."""
    source = attribution_source_from_request(request)
    inc_discovery_hit("discovery_proof_pack_sample_hits_total", source)
    return build_proof_pack_sample_html(source)


@app.get("/v1/proof-pack/sample", tags=["discovery"], summary="No-spend Proof Pack sample JSON")
async def proof_pack_sample_api(request: Request):
    """Return a deterministic Proof Pack sample without supplier, LLM, or payment spend."""
    source = attribution_source_from_request(request)
    inc_discovery_hit("discovery_proof_pack_sample_hits_total", source)
    return build_proof_pack_sample_response(source)


@app.get("/proof-pack/preview", response_class=HTMLResponse, tags=["discovery"], summary="No-spend Proof Pack mini preview")
async def proof_pack_preview_page(
    request: Request,
    target_url: str = PROOF_PACK_SAMPLE_TARGET_URL,
    question: Optional[str] = None,
    pack: str = PROOF_PACK_SAMPLE_PACK,
):
    """Serve a cached mini preview before a buyer commits to a paid Proof Pack."""
    source = attribution_source_from_request(request)
    inc_metric("proof_pack_previews_total")
    inc_attribution("proof_pack_previews", source)
    inc_discovery_hit("discovery_proof_pack_preview_hits_total", source)
    try:
        await enforce_rate_limit("proof_pack_preview_ip", client_rate_identifier(request), RATE_LIMIT_UNPAID_PER_IP)
        preview = await build_proof_pack_preview(target_url, question, pack, source)
    except RateLimitExceeded as exc:
        raise rate_limit_429(exc) from exc
    except PaymentValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.detail) from exc
    return build_proof_pack_preview_html(preview)


@app.get("/v1/proof-pack/preview", tags=["discovery"], summary="No-spend Proof Pack mini preview JSON")
async def proof_pack_preview_api(
    request: Request,
    target_url: str = PROOF_PACK_SAMPLE_TARGET_URL,
    question: Optional[str] = None,
    pack: str = PROOF_PACK_SAMPLE_PACK,
):
    """Return cached mini preview data without supplier, LLM, or payment spend."""
    source = attribution_source_from_request(request)
    inc_metric("proof_pack_previews_total")
    inc_attribution("proof_pack_previews", source)
    inc_discovery_hit("discovery_proof_pack_preview_hits_total", source)
    try:
        await enforce_rate_limit("proof_pack_preview_ip", client_rate_identifier(request), RATE_LIMIT_UNPAID_PER_IP)
        return await build_proof_pack_preview(target_url, question, pack, source)
    except RateLimitExceeded as exc:
        raise rate_limit_429(exc) from exc
    except PaymentValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.detail) from exc


@app.get("/proof-pack/request", response_class=HTMLResponse, tags=["discovery"], summary="Proof Pack request capture page")
async def proof_pack_request_page(
    request: Request,
    target_url: str = PROOF_PACK_SAMPLE_TARGET_URL,
    question: Optional[str] = None,
    pack: str = PROOF_PACK_SAMPLE_PACK,
    contact: Optional[str] = None,
    use_case: Optional[str] = None,
    budget_usdc: Optional[str] = None,
    notes: Optional[str] = None,
):
    """Serve a no-spend buyer-intent form for Proof Pack demand capture."""
    source = attribution_source_from_request(request)
    inc_discovery_hit("discovery_proof_pack_request_hits_total", source)
    return build_proof_pack_request_html(
        {
            "contact": contact,
            "target_url": target_url,
            "question": question,
            "pack": pack,
            "use_case": use_case,
            "budget_usdc": budget_usdc,
            "source": source,
            "notes": notes,
        }
    )


@app.post("/proof-pack/request", response_class=HTMLResponse, tags=["discovery"], summary="Submit a Proof Pack request")
async def proof_pack_request_submit(request: Request):
    """Store a submitted Proof Pack request without payment or supplier work."""
    source = attribution_source_from_request(request)
    inc_discovery_hit("discovery_proof_pack_request_hits_total", source)
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    try:
        await enforce_rate_limit("proof_pack_lead_ip", client_rate_identifier(request), RATE_LIMIT_UNPAID_PER_IP)
        if content_type == "application/json":
            payload = await request.json()
            if not isinstance(payload, dict):
                raise PaymentValidationError("JSON request body must be an object.")
        else:
            payload = parse_urlencoded_payload(await request.body())
        payload.setdefault("source", source)
        lead = await create_proof_pack_lead(payload, request)
    except RateLimitExceeded as exc:
        raise rate_limit_429(exc) from exc
    except (json.JSONDecodeError, PaymentValidationError) as exc:
        inc_metric("proof_pack_lead_errors_total")
        detail = exc.detail if isinstance(exc, PaymentValidationError) else "JSON request body is invalid."
        return HTMLResponse(build_proof_pack_request_html(payload if "payload" in locals() else {}, error=detail), status_code=400)

    public_response = proof_pack_lead_public_response(lead)
    return build_proof_pack_request_html(lead, submitted=public_response)


@app.post("/v1/proof-pack/leads", tags=["discovery"], summary="No-spend Proof Pack lead capture")
async def proof_pack_lead_api(request: Request, lead_request: ProofPackLeadRequest):
    """Store a Proof Pack lead and return payment-ready next steps without spending."""
    inc_discovery_hit("discovery_proof_pack_request_hits_total", attribution_source_from_request(request))
    try:
        await enforce_rate_limit("proof_pack_lead_ip", client_rate_identifier(request), RATE_LIMIT_UNPAID_PER_IP)
        lead = await create_proof_pack_lead(lead_request, request)
    except RateLimitExceeded as exc:
        raise rate_limit_429(exc) from exc
    except PaymentValidationError as exc:
        inc_metric("proof_pack_lead_errors_total")
        raise HTTPException(status_code=400, detail=exc.detail) from exc
    return proof_pack_lead_public_response(lead)


@app.get("/proof-pack/quote", response_class=HTMLResponse, tags=["discovery"], summary="Proof Pack quote page")
async def proof_pack_quote_page(
    request: Request,
    target_url: str = PROOF_PACK_SAMPLE_TARGET_URL,
    question: Optional[str] = None,
    pack: str = PROOF_PACK_SAMPLE_PACK,
):
    """Serve a no-spend Proof Pack quote page for buyers."""
    source = attribution_source_from_request(request)
    inc_metric("proof_pack_quotes_total")
    inc_attribution("proof_pack_quotes", source)
    inc_discovery_hit("discovery_proof_pack_quote_hits_total", source)
    try:
        await enforce_rate_limit("proof_pack_quote_ip", client_rate_identifier(request), RATE_LIMIT_UNPAID_PER_IP)
        quote = await build_proof_pack_quote(target_url, question, pack, source)
    except RateLimitExceeded as exc:
        raise rate_limit_429(exc) from exc
    except PaymentValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.detail) from exc
    return build_proof_pack_quote_html(quote)


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
    inc_discovery_hit("discovery_proof_pack_quote_hits_total", source)
    try:
        await enforce_rate_limit("proof_pack_quote_ip", client_rate_identifier(request), RATE_LIMIT_UNPAID_PER_IP)
        return await build_proof_pack_quote(target_url, question, pack, source)
    except RateLimitExceeded as exc:
        raise rate_limit_429(exc) from exc
    except PaymentValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.detail) from exc


@app.get("/quote", response_class=HTMLResponse, tags=["discovery"], summary="AxonGate quote page")
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


def build_discovery_index_payload() -> dict[str, Any]:
    """Return lightweight discovery data for crawlers and agent clients."""
    return {
        "status": "alive",
        "agent": "AxonGate",
        "version": app.version,
        "service": "AI Source Trust Layer",
        "positioning": "Checks whether public web evidence is safe for AI agents to cite or act on; clean Markdown extraction is a supporting capability.",
        "primary_value": "supported, weak, unsupported, and citation-ready trust decisions for public sources",
        "basename": "axongate.base.eth",
        "manifest": f"{PUBLIC_BASE_URL}/manifest.json",
        "agent_card": f"{PUBLIC_BASE_URL}/.well-known/agent.json",
        "agent_card_alias": f"{PUBLIC_BASE_URL}/.well-known/agent-card.json",
        "x402": f"{PUBLIC_BASE_URL}/.well-known/x402",
        "x402_json": f"{PUBLIC_BASE_URL}/.well-known/x402.json",
        "discovery": f"{PUBLIC_BASE_URL}/discovery/resources",
        "docs": f"{PUBLIC_BASE_URL}/docs",
        "about": f"{PUBLIC_BASE_URL}/about",
        "faq": f"{PUBLIC_BASE_URL}/faq",
        "contact": f"{PUBLIC_BASE_URL}/contact",
        "contact_api": f"{PUBLIC_BASE_URL}/v1/contact",
        "contact_email": PUBLIC_CONTACT_EMAIL,
        "operator_dashboard": f"{PUBLIC_BASE_URL}/operator",
        "operator_leads": f"{PUBLIC_BASE_URL}/operator/leads",
        "operator_leads_api": f"{PUBLIC_BASE_URL}/v1/operator/leads",
        "quickstart": f"{PUBLIC_BASE_URL}/quickstart",
        "paid_test_guide": f"{PUBLIC_BASE_URL}/paid-test",
        "quote": f"{PUBLIC_BASE_URL}/quote",
        "quote_api": f"{PUBLIC_BASE_URL}/v1/x402/quote",
        "proof_pack": f"{PUBLIC_BASE_URL}/proof-pack",
        "proof_pack_sample": f"{PUBLIC_BASE_URL}/proof-pack/sample",
        "proof_pack_sample_api": f"{PUBLIC_BASE_URL}/v1/proof-pack/sample",
        "proof_pack_preview": f"{PUBLIC_BASE_URL}/proof-pack/preview",
        "proof_pack_preview_api": f"{PUBLIC_BASE_URL}/v1/proof-pack/preview",
        "proof_pack_quote": f"{PUBLIC_BASE_URL}/proof-pack/quote",
        "proof_pack_quote_api": f"{PUBLIC_BASE_URL}/v1/proof-pack/quote",
        "proof_pack_request": f"{PUBLIC_BASE_URL}/proof-pack/request",
        "proof_pack_leads_api": f"{PUBLIC_BASE_URL}/v1/proof-pack/leads",
        "proof_pack_x402_endpoint": f"{PUBLIC_BASE_URL}/v1/x402/proof-pack",
        "proof_bundle": f"{PUBLIC_BASE_URL}/proof-pack/bundle",
        "proof_bundle_quote": f"{PUBLIC_BASE_URL}/proof-pack/bundle/quote",
        "proof_bundle_checkout_review": f"{PUBLIC_BASE_URL}/proof-pack/bundle/checkout",
        "proof_bundle_checkout": f"{PUBLIC_BASE_URL}/proof-pack/bundle/pay",
        "proof_bundle_delivery": f"{PUBLIC_BASE_URL}/proof-pack/bundle/delivery",
        "proof_bundle_delivery_api": f"{PUBLIC_BASE_URL}/v1/proof-pack/bundle/delivery",
        "proof_bundle_delivery_json": f"{PUBLIC_BASE_URL}/proof-pack/bundle/delivery.json",
        "proof_bundle_delivery_pdf": f"{PUBLIC_BASE_URL}/proof-pack/bundle/delivery.pdf",
        "proof_bundle_delivery_print": f"{PUBLIC_BASE_URL}/proof-pack/bundle/delivery/print",
        "proof_bundle_recovery": f"{PUBLIC_BASE_URL}/proof-pack/bundle/recover",
        "proof_bundle_recovery_api": f"{PUBLIC_BASE_URL}/v1/proof-pack/bundle/recover",
        "proof_bundle_quote_api": f"{PUBLIC_BASE_URL}/v1/proof-pack/bundle/quote",
        "proof_bundle_leads_api": f"{PUBLIC_BASE_URL}/v1/proof-pack/bundle/leads",
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


def request_prefers_json(request: Request) -> bool:
    """Detect machine clients that explicitly ask the root route for JSON."""
    requested_format = (request.query_params.get("format") or "").strip().lower()
    if requested_format in {"json", "discovery"}:
        return True
    accept = (request.headers.get("accept") or "").lower()
    return "application/json" in accept and "text/html" not in accept


@app.get("/", tags=["discovery"], summary="Customer homepage")
async def root(request: Request):
    """Serve the customer homepage by default and discovery JSON on request."""
    inc_discovery_hit("discovery_root_hits_total", attribution_source_from_request(request))
    if request_prefers_json(request):
        return build_discovery_index_payload()
    return HTMLResponse(build_home_html())


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
        items = [build_x402_resource(), build_proof_pack_resource(), build_proof_bundle_resource()]

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
    proof_pack_lead_storage = await proof_pack_leads_public_snapshot()
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
        "operator": {
            "private_leads_enabled": bool(OPERATOR_TOKEN),
            "lead_webhook_enabled": bool(PROOF_PACK_LEAD_WEBHOOK_URL),
            "operator_token_header": "X-AxonGate-Operator-Token",
        },
        "stripe": {
            "webhook_enabled": bool(STRIPE_WEBHOOK_SECRET),
            "webhook_endpoint": public_url("/v1/stripe/webhook"),
            "events_redis_key": STRIPE_EVENTS_REDIS_KEY if redis_client and METRICS_PERSISTENCE_ENABLED else None,
            "event_retention_seconds": STRIPE_EVENTS_RETENTION_SECONDS,
            "signature_tolerance_seconds": STRIPE_WEBHOOK_TOLERANCE_SECONDS,
        },
        "email_delivery": {
            "enabled": EMAIL_DELIVERY_ENABLED,
            "provider": EMAIL_PROVIDER,
            "from_configured": bool(EMAIL_FROM),
            "reply_to_configured": bool(EMAIL_REPLY_TO),
            "resend_api_key_configured": bool(RESEND_API_KEY),
            "last_error": email_delivery_last_error,
            "last_error_at": email_delivery_last_error_at,
            "last_status_code": email_delivery_last_status_code,
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
        "proof_pack_leads": proof_pack_lead_storage,
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
        "proof_bundle_pricing": {
            bundle: {
                "price_usdc": float(price),
                "amount_units": str(usdc_units(price)),
                "source_limit": proof_bundle_source_limit(bundle),
            }
            for bundle, price in PROOF_BUNDLE_PRICING_USDC.items()
        },
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
                    "description": "Operator guide for running a real paid smoke test.",
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
                    "description": "Public Proof Pack product page.",
                    "schema": {"type": "string", "format": "uri"},
                },
                "X-AxonGate-Proof-Pack-Quote": {
                    "description": "Supplier-free Proof Pack quote endpoint.",
                    "schema": {"type": "string", "format": "uri"},
                },
                "X-AxonGate-Proof-Pack-Request": {
                    "description": "No-spend Proof Pack request capture page.",
                    "schema": {"type": "string", "format": "uri"},
                },
            }
        )

    security_schemes = schema.setdefault("components", {}).setdefault("securitySchemes", {})
    security_schemes["OperatorToken"] = {
        "type": "apiKey",
        "in": "header",
        "name": "X-AxonGate-Operator-Token",
        "description": "Private operator token for raw Proof Pack lead contact data.",
    }
    for operator_path in ("/operator/leads", "/v1/operator/leads"):
        operator_get = schema.get("paths", {}).get(operator_path, {}).get("get")
        if isinstance(operator_get, dict):
            operator_get["security"] = [{"OperatorToken": []}]
            operator_get.setdefault("responses", {}).setdefault("401", {"description": "Operator token required"})
            operator_get.setdefault("responses", {}).setdefault(
                "503",
                {"description": "AXONGATE_OPERATOR_TOKEN is not configured"},
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

    proof_lead_schema = schema.get("components", {}).get("schemas", {}).get("ProofPackLeadRequest")
    if isinstance(proof_lead_schema, dict):
        pack_property = proof_lead_schema.get("properties", {}).get("pack")
        if isinstance(pack_property, dict):
            pack_property["enum"] = list(PROOF_PACK_PRICING_USDC.keys())
            pack_property["default"] = PROOF_PACK_SAMPLE_PACK

    proof_bundle_lead_schema = schema.get("components", {}).get("schemas", {}).get("ProofBundleLeadRequest")
    if isinstance(proof_bundle_lead_schema, dict):
        bundle_property = proof_bundle_lead_schema.get("properties", {}).get("bundle")
        if isinstance(bundle_property, dict):
            bundle_property["enum"] = list(PROOF_BUNDLE_PRICING_USDC.keys())
            bundle_property["default"] = DEFAULT_PROOF_BUNDLE

    schema["x-payment-info"] = payment_info
    app.openapi_schema = schema
    return app.openapi_schema


app.openapi = custom_openapi


if __name__ == "__main__":
    print("Booting AxonGate Revenue Server...")
    print("Listening for Agent-to-Agent (A2A) traffic on port 8000")
    uvicorn.run(app, host="0.0.0.0", port=8000)
