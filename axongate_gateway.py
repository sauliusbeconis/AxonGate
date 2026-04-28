import asyncio
import base64
import hashlib
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
from urllib.parse import urljoin, urlparse

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
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

load_dotenv()

app = FastAPI(title="AxonGate Sovereign Gateway")

PUBLIC_BASE_URL = os.getenv("AXONGATE_PUBLIC_BASE_URL", "https://web-production-8136ee.up.railway.app").rstrip("/")
BASE_MAINNET_RPC_URL = os.getenv("BASE_RPC_URL", "https://mainnet.base.org")
BASE_RPC_TIMEOUT_SECONDS = float(os.getenv("BASE_RPC_TIMEOUT_SECONDS", "5"))
BASE_USDC_ADDRESS = Web3.to_checksum_address(
    os.getenv("BASE_USDC_ADDRESS", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")
)
PAYAI_FACILITATOR_URL = os.getenv("PAYAI_FACILITATOR_URL", "https://facilitator.payai.network")
REDIS_URL = os.getenv("REDIS_URL")
DEFAULT_CACHE_TTL_SECONDS = int(os.getenv("AXONGATE_CACHE_TTL_SECONDS", "3600"))
DELIVERY_CREDIT_TTL_SECONDS = int(os.getenv("AXONGATE_DELIVERY_CREDIT_TTL_SECONDS", "900"))
DELIVERY_CREDIT_MAX_ATTEMPTS = int(os.getenv("AXONGATE_DELIVERY_CREDIT_MAX_ATTEMPTS", "2"))

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
REQUIRED_USDC_AMOUNT = int(REQUIRED_USDC_FEE * (Decimal(10) ** USDC_DECIMALS))
TIER_PRICING_USDC = {
    "basic": Decimal(os.getenv("AXONGATE_BASIC_PRICE_USDC", "0.02")),
    "fresh": Decimal(os.getenv("AXONGATE_FRESH_PRICE_USDC", "0.03")),
    "deep": Decimal(os.getenv("AXONGATE_DEEP_PRICE_USDC", "0.05")),
}

JINA_API_COST_USDC = Decimal(
    os.getenv("AXONGATE_JINA_API_COST_USDC", os.getenv("AXONGATE_FIXED_API_OVERHEAD_USDC", "0.0005"))
)
MIN_PROFIT_MARGIN_USDC = max(
    Decimal(os.getenv("AXONGATE_PROFIT_MARGIN_USDC", "0.01")),
    Decimal("0.01"),
)
UEG_GAS_UNITS = int(os.getenv("AXONGATE_UEG_GAS_UNITS", "65000"))
ETH_USDC_PRICE = Decimal(os.getenv("AXONGATE_ETH_USDC_PRICE", "3500"))

TRANSFER_TOPIC = Web3.keccak(text="Transfer(address,address,uint256)").hex()

web3 = Web3(
    Web3.HTTPProvider(
        BASE_MAINNET_RPC_URL,
        request_kwargs={"timeout": BASE_RPC_TIMEOUT_SECONDS},
    )
)

processed_txs: set[str] = set()
processed_txs_lock = asyncio.Lock()
markdown_cache: dict[str, tuple[float, str]] = {}
markdown_cache_lock = asyncio.Lock()
delivery_credits: dict[str, dict[str, Any]] = {}
delivery_credits_lock = asyncio.Lock()
redis_client = redis.from_url(REDIS_URL, decode_responses=True) if redis and REDIS_URL else None
metrics: dict[str, int] = {
    "requests_total": 0,
    "legacy_access_requests_total": 0,
    "x402_access_requests_total": 0,
    "delivery_credit_retries_total": 0,
    "payment_required_total": 0,
    "payment_verified_total": 0,
    "jina_requests_total": 0,
    "cache_hits_total": 0,
    "cache_misses_total": 0,
    "target_preflight_total": 0,
    "target_preflight_rejections_total": 0,
    "ssrf_rejections_total": 0,
    "supplier_rejections_total": 0,
    "delivery_credits_issued_total": 0,
    "delivery_credit_success_total": 0,
    "delivery_credit_exhausted_total": 0,
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
    tier: str = Field("basic", description="Pricing tier: basic, fresh, or deep")
    force_refresh: bool = Field(False, description="Bypass cache when true")


class ComputeRequest(BaseModel):
    agent_id: str
    task_payload: dict[str, Any]
    offered_fee: float = Field(..., description="Fee offered by the client agent in USDC")


@dataclass(frozen=True)
class UEGReceipt:
    revenue_usdc: Decimal
    dynamic_gas_cost_usdc: Decimal
    jina_api_cost_usdc: Decimal
    projected_profit_usdc: Decimal
    base_fee_wei: int
    gas_units: int
    supplier_attempts: int = 1


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
    """Load the canonical AxonGate discovery card from manifest.json."""
    manifest_path = Path(__file__).with_name("manifest.json")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def inc_metric(name: str, amount: int = 1) -> None:
    metrics[name] = metrics.get(name, 0) + amount


def stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_tier(tier: Optional[str]) -> str:
    normalized = (tier or "basic").strip().lower()
    if normalized not in TIER_PRICING_USDC:
        raise PaymentValidationError("Unsupported tier. Use basic, fresh, or deep.")
    return normalized


def usdc_units(amount: Decimal) -> int:
    return int(amount * (Decimal(10) ** USDC_DECIMALS))


def price_for_tier(tier: Optional[str]) -> Decimal:
    return TIER_PRICING_USDC[normalize_tier(tier)]


def cache_ttl_for_tier(tier: str, force_refresh: bool = False) -> int:
    if force_refresh or tier == "fresh":
        return 0
    if tier == "deep":
        return max(DEFAULT_CACHE_TTL_SECONDS // 2, 300)
    return DEFAULT_CACHE_TTL_SECONDS


def build_x402_accepts(tier: str = "basic") -> list[dict[str, Any]]:
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
                "name": "USDC",
                "version": "2",
                "decimals": USDC_DECIMALS,
                "price": f"${price}",
                "tier": normalized_tier,
                "mimeType": "application/json",
                "resource": f"{PUBLIC_BASE_URL}/v1/x402/access",
                "description": "Clean Web-to-Markdown context extraction for autonomous agents.",
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
            "facilitator": PAYAI_FACILITATOR_URL,
            "legacyTxHashEndpoint": f"{PUBLIC_BASE_URL}/v1/access",
            "retryEndpoint": f"{PUBLIC_BASE_URL}/v1/x402/retry",
            "supplyGuards": {
                "dnsSsrfProtection": True,
                "targetPreflight": PREFLIGHT_ENABLED,
                "maxContentBytes": PREFLIGHT_MAX_CONTENT_BYTES,
                "allowedTargetPorts": sorted(ALLOWED_TARGET_PORTS),
            },
            "pricing": {
                tier: {
                    "amount": str(usdc_units(price)),
                    "price": f"${price}",
                    "currency": "USDC",
                }
                for tier, price in TIER_PRICING_USDC.items()
            },
        },
        "inputSchema": {
            "type": "http",
            "method": "POST",
            "contentType": "application/json",
            "bodyFields": {
                "target_url": {
                    "type": "string",
                    "description": "Absolute HTTP/HTTPS URL to convert into clean markdown.",
                    "required": True,
                },
                "tier": {
                    "type": "string",
                    "description": "basic, fresh, or deep. For standard x402 payment, also pass tier as ?tier= or X-AxonGate-Tier.",
                    "required": False,
                },
                "force_refresh": {
                    "type": "boolean",
                    "description": "Bypass cache for this request.",
                    "required": False,
                }
            },
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "target_url": {"type": "string"},
                "markdown": {"type": "string"},
                "payment": {"type": "object"},
                "ueg_receipt": {"type": "object"},
            },
        },
        "discoverable": True,
    }


def build_payment_required_payload(error: str) -> dict[str, Any]:
    """Build the x402 PAYMENT-REQUIRED header payload for agent clients."""
    return {
        "x402Version": 2,
        "error": error,
        "resource": {
            "url": f"{PUBLIC_BASE_URL}/v1/x402/access",
            "description": "AxonGate Clean Context Broker: paid Web-to-Markdown extraction.",
            "mimeType": "application/json",
        },
        "accepts": build_x402_accepts(),
        "extensions": {
            "agentManifest": f"{PUBLIC_BASE_URL}/manifest.json",
            "discovery": f"{PUBLIC_BASE_URL}/discovery/resources",
            "paymentHashHeader": "X-AxonGate-Payment-Hash",
            "standardPaymentHeader": "PAYMENT-SIGNATURE",
            "tierHeader": "X-AxonGate-Tier",
            "retryCreditHeader": "X-AxonGate-Retry-Credit",
            "retryEndpoint": f"{PUBLIC_BASE_URL}/v1/x402/retry",
            "facilitator": PAYAI_FACILITATOR_URL,
        },
    }


def payment_required_headers(error: str) -> dict[str, str]:
    payload = build_payment_required_payload(error)
    encoded = base64.b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii")
    return {
        "PAYMENT-REQUIRED": encoded,
        "X-Payment-Required": encoded,
        "X-AxonGate-Payment-Asset": BASE_USDC_ADDRESS,
        "X-AxonGate-Payment-Amount": str(REQUIRED_USDC_FEE),
    }


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

    routes = {
        "POST /v1/x402/access": RouteConfig(
            accepts=PaymentOption(
                scheme="exact",
                pay_to=load_vault_address(),
                price=x402_dynamic_price,
                network="eip155:8453",
                max_timeout_seconds=300,
                extra={
                    "name": "USDC",
                    "version": "2",
                    "decimals": USDC_DECIMALS,
                },
            ),
            resource=f"{PUBLIC_BASE_URL}/v1/x402/access",
            description="AxonGate Clean Context Broker: paid Web-to-Markdown extraction.",
            mime_type="application/json",
            extensions={
                "agentManifest": f"{PUBLIC_BASE_URL}/manifest.json",
                "discovery": f"{PUBLIC_BASE_URL}/discovery/resources",
                "tiers": {tier: f"${price}" for tier, price in TIER_PRICING_USDC.items()},
            },
        )
    }

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
        "User-Agent": "AxonGate-Preflight/1.0 (+https://web-production-8136ee.up.railway.app/manifest.json)",
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


async def call_base_rpc(label: str, rpc_call):
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(rpc_call),
            timeout=BASE_RPC_TIMEOUT_SECONDS + 1,
        )
    except TransactionNotFound:
        raise
    except asyncio.TimeoutError as exc:
        raise NetworkUnavailableError(f"Base RPC timed out during {label}") from exc
    except Exception as exc:
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


async def calculate_profitability() -> UEGReceipt:
    """Calculate 0.02 USDC revenue minus live Base gas estimate and Jina cost."""
    return await calculate_profitability_for_price(REQUIRED_USDC_FEE)


async def calculate_profitability_for_price(revenue_usdc: Decimal, supplier_attempts: int = 1) -> UEGReceipt:
    """Calculate tier revenue minus live Base gas estimate and bounded supplier cost."""
    base_fee_wei = await fetch_current_base_fee_wei()
    gas_cost_eth = Decimal(base_fee_wei * UEG_GAS_UNITS) / Decimal(10**18)
    dynamic_gas_cost_usdc = gas_cost_eth * ETH_USDC_PRICE
    bounded_supplier_attempts = max(1, int(supplier_attempts))
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
    )


async def check_profitability() -> bool:
    """Return True only when the Clean Context Broker margin is > 0.01 USDC."""
    receipt = await calculate_profitability()
    return receipt.projected_profit_usdc > MIN_PROFIT_MARGIN_USDC


def x402_dynamic_price(context: HTTPRequestContext):
    """Return tier-aware x402 price for PayAI middleware."""
    tier = None
    if context and context.adapter:
        tier = context.adapter.get_query_param("tier") or context.adapter.get_header("x-axongate-tier")

    normalized_tier = normalize_tier(tier)
    price = price_for_tier(normalized_tier)
    return AssetAmount(
        amount=str(usdc_units(price)),
        asset=BASE_USDC_ADDRESS,
        extra={
            "name": "USDC",
            "version": "2",
            "decimals": USDC_DECIMALS,
            "tier": normalized_tier,
        },
    )


configure_standard_x402_middleware()


async def verify_x402_payment(tx_hash: str, expected_fee_usdc: Decimal = REQUIRED_USDC_FEE) -> PaymentVerification:
    """
    Verify the AxonGate x402 payment hash against Base mainnet.

    The Clean Context Broker accepts one paid request per successful transaction.
    This validator normalizes and replay-checks the hash, probes CDP if the SDK
    provides a read-only transaction method, then uses Base RPC receipt data to
    enforce the economic facts: the transaction succeeded, it called the Base USDC
    contract, and its ERC-20 Transfer event sent exactly 0.02 USDC to the AxonGate
    vault. Only after those checks pass is the hash stored in processed_txs.
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
            return response.text
    except httpx.TimeoutException as exc:
        raise NetworkUnavailableError(
            "Jina Reader timed out",
            source="jina",
            supplier_attempt_charged=True,
        ) from exc
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        if status_code == 408 or status_code == 429 or status_code >= 500:
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
        raise NetworkUnavailableError(
            "Jina Reader request failed",
            source="jina",
            supplier_attempt_charged=False,
        ) from exc


async def get_clean_markdown(target_url: str, tier: str, force_refresh: bool = False) -> tuple[str, bool]:
    """Return cleaned markdown, using cache when the selected tier allows it."""
    ttl = cache_ttl_for_tier(tier, force_refresh)
    if ttl > 0:
        cached = await get_cached_markdown(target_url, tier)
        if cached is not None:
            inc_metric("cache_hits_total")
            return cached, True

    inc_metric("cache_misses_total")
    markdown = await fetch_clean_markdown(target_url)
    await set_cached_markdown(target_url, tier, markdown, ttl)
    return markdown, False


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


@app.get("/")
async def root():
    """Return a lightweight discovery index for crawlers and agent clients."""
    return {
        "status": "alive",
        "agent": "AxonGate",
        "service": "The Clean Context Broker",
        "basename": "axongate.base.eth",
        "manifest": f"{PUBLIC_BASE_URL}/manifest.json",
        "agent_card": f"{PUBLIC_BASE_URL}/.well-known/agent.json",
        "x402": f"{PUBLIC_BASE_URL}/.well-known/x402",
        "discovery": f"{PUBLIC_BASE_URL}/discovery/resources",
        "standard_x402_endpoint": f"{PUBLIC_BASE_URL}/v1/x402/access",
        "legacy_tx_hash_endpoint": f"{PUBLIC_BASE_URL}/v1/access",
        "retry_endpoint": f"{PUBLIC_BASE_URL}/v1/x402/retry",
    }


@app.get("/health")
async def health():
    return {"status": "alive", "vault_address": load_vault_address()}


@app.get("/manifest.json")
async def manifest():
    """Return the full AxonGate agent card used by other agents for discovery."""
    return load_agent_card()


@app.get("/.well-known/agent.json")
async def well_known_agent():
    """Expose the agent card at a common agent-discovery well-known path."""
    return load_agent_card()


@app.get("/.well-known/x402")
async def well_known_x402():
    """Expose AxonGate's x402 resource advertisement for crawler discovery."""
    return build_payment_required_payload("Payment required to access AxonGate Clean Context Broker")


@app.get("/discovery/resources")
async def discovery_resources(type: Optional[str] = None, limit: int = 20, offset: int = 0):
    """Return a PayAI-style Bazaar resource listing for AxonGate."""
    if type not in (None, "http"):
        items: list[dict[str, Any]] = []
    else:
        items = [build_x402_resource()]

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


@app.get("/metrics")
async def metrics_snapshot():
    """Expose lightweight operational counters for conversion and margin tuning."""
    return {
        "status": "ok",
        "metrics": metrics,
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
        "pricing": {tier: float(price) for tier, price in TIER_PRICING_USDC.items()},
    }


@app.head("/v1/x402/access")
@app.get("/v1/x402/access")
async def access_context_broker_x402_probe():
    """
    Return a machine-readable x402 challenge for directory probes.

    The paid Clean Context Broker operation is POST-only because it requires a
    target_url body. Some service directories probe submitted paths with GET, so
    this companion route advertises the same payment requirements without
    triggering upstream work or accepting payment for a body-less request.
    """
    inc_metric("requests_total")
    inc_metric("payment_required_total")
    detail = "Payment Required. Use POST with PAYMENT-SIGNATURE and a JSON target_url body."
    raise HTTPException(status_code=402, detail=detail, headers=payment_required_headers(detail))


@app.post("/v1/x402/access")
async def access_context_broker_x402(
    request: Request,
    access_request: AccessRequest,
    x_axongate_tier: Optional[str] = Header(None, alias="X-AxonGate-Tier"),
):
    """
    Standard PayAI/x402 endpoint.

    PaymentMiddlewareASGI verifies and settles PAYMENT-SIGNATURE before this
    handler spends the Jina request. This path is what PayAI-style automated
    buyers should prefer because it follows the standard x402 flow.
    """
    inc_metric("requests_total")
    inc_metric("x402_access_requests_total")

    if not hasattr(request.state, "payment_payload"):
        inc_metric("payment_required_total")
        detail = "Payment Required. Retry with PAYMENT-SIGNATURE for the selected x402 requirement."
        raise HTTPException(status_code=402, detail=detail, headers=payment_required_headers(detail))

    payment_reference = payment_reference_from_request(request)
    target_url: Optional[str] = None
    tier: Optional[str] = None
    try:
        target_url = await assert_public_target_url(access_request.target_url)
        tier = normalize_tier(x_axongate_tier or access_request.tier)
        profitability = await calculate_profitability_for_price(price_for_tier(tier))
        if profitability.projected_profit_usdc <= MIN_PROFIT_MARGIN_USDC:
            raise PaymentValidationError("Dynamic UEG rejected request; projected margin is too low")
        markdown, cache_hit = await get_clean_markdown(target_url, tier, access_request.force_refresh)
        inc_metric("payment_verified_total")
    except NetworkUnavailableError as exc:
        credit = None
        if target_url and tier:
            credit = await maybe_issue_delivery_credit(
                exc=exc,
                payment_reference=payment_reference,
                target_url=target_url,
                tier=tier,
                force_refresh=access_request.force_refresh,
                amount_usdc=price_for_tier(tier),
                mode="x402-facilitator",
            )
        raise retry_later_503(exc, credit) from exc
    except PaymentValidationError as exc:
        inc_metric("errors_total")
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
        },
    }


@app.post("/v1/x402/retry")
async def retry_context_broker_delivery(
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

    if not x_axongate_retry_credit:
        inc_metric("payment_required_total")
        detail = "Payment Required. Provide PAYMENT-SIGNATURE or a valid X-AxonGate-Retry-Credit."
        raise HTTPException(status_code=402, detail=detail, headers=payment_required_headers(detail))

    reservation: DeliveryCreditReservation | None = None
    try:
        target_url = await assert_public_target_url(access_request.target_url)
        tier = normalize_tier(access_request.tier)
        reservation = await reserve_delivery_credit(
            x_axongate_retry_credit,
            target_url=target_url,
            tier=tier,
            force_refresh=access_request.force_refresh,
        )

        amount_usdc = Decimal(str(reservation.record["amount_usdc"]))
        profitability = await calculate_profitability_for_price(
            amount_usdc,
            supplier_attempts=reservation.total_supplier_attempts,
        )
        if profitability.projected_profit_usdc <= MIN_PROFIT_MARGIN_USDC:
            await restore_delivery_credit_attempt(reservation)
            raise PaymentValidationError("Retry credit rejected; projected margin is below AxonGate guard")

        markdown, cache_hit = await get_clean_markdown(target_url, tier, access_request.force_refresh)
        await delete_delivery_credit(reservation.token)
        inc_metric("delivery_credit_success_total")
        inc_metric("payment_verified_total")
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
        },
    }


@app.post("/v1/access")
async def access_context_broker(
    request: AccessRequest,
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

    if not x_axongate_payment_hash:
        inc_metric("payment_required_total")
        detail = "Payment Required. Provide X-AxonGate-Payment-Hash with a Base USDC transaction hash."
        raise HTTPException(
            status_code=402,
            detail=detail,
            headers=payment_required_headers(detail),
        )

    payment: PaymentVerification | None = None
    target_url: Optional[str] = None
    tier: Optional[str] = None
    try:
        target_url = await assert_public_target_url(request.target_url)
        tier = normalize_tier(request.tier)
        price = price_for_tier(tier)
        payment = await verify_x402_payment(x_axongate_payment_hash, price)
        profitability = await calculate_profitability_for_price(payment.amount_usdc)
        if profitability.projected_profit_usdc <= MIN_PROFIT_MARGIN_USDC:
            raise PaymentValidationError("Dynamic UEG rejected request; projected margin is too low")
        markdown, cache_hit = await get_clean_markdown(target_url, tier, request.force_refresh)
        inc_metric("payment_verified_total")
    except NetworkUnavailableError as exc:
        credit = None
        if payment is not None and target_url and tier:
            credit = await maybe_issue_delivery_credit(
                exc=exc,
                payment_reference=f"legacy-tx:{payment.tx_hash}",
                target_url=target_url,
                tier=tier,
                force_refresh=request.force_refresh,
                amount_usdc=payment.amount_usdc,
                mode="legacy-tx-hash",
            )
        raise retry_later_503(exc, credit) from exc
    except RuntimeError as exc:
        inc_metric("errors_total")
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except PaymentValidationError as exc:
        inc_metric("errors_total")
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
        },
    }


@app.post("/v1/broker/compute")
async def process_task(request: ComputeRequest, x402_token: str = Header(None)):
    print(f"\n[INBOUND REQUEST] Agent: {request.agent_id}")

    if not x402_token:
        print("[REJECTED] Missing x402 Payment Header")
        raise HTTPException(status_code=401, detail="Missing x402 Payment Token")

    try:
        profitability = await calculate_profitability()
        offered_fee_usdc = Decimal(str(request.offered_fee))
        projected_profit = offered_fee_usdc - (
            profitability.dynamic_gas_cost_usdc + profitability.jina_api_cost_usdc
        )
        if projected_profit <= MIN_PROFIT_MARGIN_USDC:
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
            "timestamp": time.time(),
        },
    }

    print("[DISPATCHING RESPONSE] Task complete.")
    return response_payload


if __name__ == "__main__":
    print("Booting AxonGate Revenue Server...")
    print("Listening for Agent-to-Agent (A2A) traffic on port 8000")
    uvicorn.run(app, host="0.0.0.0", port=8000)
