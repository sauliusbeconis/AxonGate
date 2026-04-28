import asyncio
import base64
import inspect
import json
import os
import re
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from web3 import Web3
from web3.exceptions import TransactionNotFound

try:
    from cdp import CdpClient
    from cdp.evm_client import EvmClient
except ImportError:  # pragma: no cover - Railway installs cdp-sdk from requirements.txt.
    CdpClient = None
    EvmClient = None

load_dotenv()

app = FastAPI(title="AxonGate Sovereign Gateway")

PUBLIC_BASE_URL = os.getenv("AXONGATE_PUBLIC_BASE_URL", "https://web-production-8136ee.up.railway.app").rstrip("/")
BASE_MAINNET_RPC_URL = os.getenv("BASE_RPC_URL", "https://mainnet.base.org")
BASE_RPC_TIMEOUT_SECONDS = float(os.getenv("BASE_RPC_TIMEOUT_SECONDS", "5"))
BASE_USDC_ADDRESS = Web3.to_checksum_address(
    os.getenv("BASE_USDC_ADDRESS", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")
)
PAYAI_FACILITATOR_URL = os.getenv("PAYAI_FACILITATOR_URL", "https://facilitator.payai.network")

JINA_API_KEY = os.getenv("JINA_API_KEY")
JINA_READER_BASE_URL = os.getenv("JINA_READER_BASE_URL", "https://r.jina.ai")
JINA_TIMEOUT_SECONDS = float(os.getenv("JINA_TIMEOUT_SECONDS", "20"))

USDC_DECIMALS = 6
REQUIRED_USDC_FEE = Decimal(os.getenv("AXONGATE_BASE_FEE_USDC", "0.02"))
REQUIRED_USDC_AMOUNT = int(REQUIRED_USDC_FEE * (Decimal(10) ** USDC_DECIMALS))

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
CDP_TRANSACTION_METHOD_NAMES = ("get_transaction", "get_evm_transaction", "get_transaction_receipt")


class AccessRequest(BaseModel):
    target_url: str = Field(..., description="HTTP or HTTPS URL to convert into clean markdown")


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


@dataclass(frozen=True)
class PaymentVerification:
    tx_hash: str
    vault_address: str
    token_address: str
    amount_usdc: Decimal


class PaymentValidationError(Exception):
    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


class NetworkUnavailableError(Exception):
    pass


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


def build_x402_accepts() -> list[dict[str, Any]]:
    """Return PayAI/x402-compatible payment requirements for AxonGate."""
    return [
        {
            "scheme": "exact",
            "network": "eip155:8453",
            "amount": str(REQUIRED_USDC_AMOUNT),
            "asset": BASE_USDC_ADDRESS,
            "payTo": load_vault_address(),
            "maxTimeoutSeconds": 300,
            "extra": {
                "name": "USDC",
                "version": "2",
                "decimals": USDC_DECIMALS,
                "price": f"${REQUIRED_USDC_FEE}",
                "mimeType": "application/json",
                "resource": f"{PUBLIC_BASE_URL}/v1/access",
                "description": "Clean Web-to-Markdown context extraction for autonomous agents.",
            },
        }
    ]


def build_x402_resource() -> dict[str, Any]:
    """Build the resource object used by PayAI-style discovery endpoints."""
    return {
        "resource": f"{PUBLIC_BASE_URL}/v1/access",
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
            "url": f"{PUBLIC_BASE_URL}/v1/access",
            "description": "AxonGate Clean Context Broker: paid Web-to-Markdown extraction.",
            "mimeType": "application/json",
        },
        "accepts": build_x402_accepts(),
        "extensions": {
            "agentManifest": f"{PUBLIC_BASE_URL}/manifest.json",
            "discovery": f"{PUBLIC_BASE_URL}/discovery/resources",
            "paymentHashHeader": "X-AxonGate-Payment-Hash",
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
        raise PaymentValidationError("target_url must be an absolute http or https URL")
    return target_url


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
    base_fee_wei = await fetch_current_base_fee_wei()
    gas_cost_eth = Decimal(base_fee_wei * UEG_GAS_UNITS) / Decimal(10**18)
    dynamic_gas_cost_usdc = gas_cost_eth * ETH_USDC_PRICE
    projected_profit = REQUIRED_USDC_FEE - (dynamic_gas_cost_usdc + JINA_API_COST_USDC)

    return UEGReceipt(
        revenue_usdc=REQUIRED_USDC_FEE,
        dynamic_gas_cost_usdc=dynamic_gas_cost_usdc,
        jina_api_cost_usdc=JINA_API_COST_USDC,
        projected_profit_usdc=projected_profit,
        base_fee_wei=base_fee_wei,
        gas_units=UEG_GAS_UNITS,
    )


async def check_profitability() -> bool:
    """Return True only when the Clean Context Broker margin is > 0.01 USDC."""
    receipt = await calculate_profitability()
    return receipt.projected_profit_usdc > MIN_PROFIT_MARGIN_USDC


async def verify_x402_payment(tx_hash: str) -> PaymentVerification:
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

    async with processed_txs_lock:
        if normalized_hash in processed_txs:
            raise PaymentValidationError("Payment hash has already been processed")

    vault_address = load_vault_address()
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

    if total_transferred_to_vault != REQUIRED_USDC_AMOUNT:
        raise PaymentValidationError(f"Payment must transfer exactly {REQUIRED_USDC_FEE} USDC to AxonGate")

    async with processed_txs_lock:
        if normalized_hash in processed_txs:
            raise PaymentValidationError("Payment hash has already been processed")
        processed_txs.add(normalized_hash)

    return PaymentVerification(
        tx_hash=normalized_hash,
        vault_address=vault_address,
        token_address=BASE_USDC_ADDRESS,
        amount_usdc=REQUIRED_USDC_FEE,
    )


async def release_processed_tx(tx_hash: str) -> None:
    """Release a reserved payment hash when no paid response was delivered."""
    async with processed_txs_lock:
        processed_txs.discard(tx_hash)


async def fetch_clean_markdown(target_url: str) -> str:
    """Call Jina Reader and return the upstream markdown body."""
    if not JINA_API_KEY:
        raise NetworkUnavailableError("Jina API key is not configured")

    reader_url = f"{JINA_READER_BASE_URL.rstrip('/')}/{target_url}"
    headers = {"Authorization": f"Bearer {JINA_API_KEY}"}

    try:
        async with httpx.AsyncClient(timeout=JINA_TIMEOUT_SECONDS, follow_redirects=True) as client:
            response = await client.get(reader_url, headers=headers)
            response.raise_for_status()
            return response.text
    except httpx.TimeoutException as exc:
        raise NetworkUnavailableError("Jina Reader timed out") from exc
    except httpx.HTTPError as exc:
        raise NetworkUnavailableError("Jina Reader request failed") from exc


def retry_later_503(exc: Exception) -> HTTPException:
    print(f"[UPSTREAM] Temporary failure: {exc}")
    return HTTPException(
        status_code=503,
        detail="Upstream service temporarily unavailable. Client agent should retry in 5 seconds.",
    )


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
    if not x_axongate_payment_hash:
        detail = "Payment Required. Provide X-AxonGate-Payment-Hash with a Base USDC transaction hash."
        raise HTTPException(
            status_code=402,
            detail=detail,
            headers=payment_required_headers(detail),
        )

    payment: PaymentVerification | None = None
    try:
        target_url = validate_target_url(request.target_url)
        payment = await verify_x402_payment(x_axongate_payment_hash)
        profitability = await calculate_profitability()
        if profitability.projected_profit_usdc <= MIN_PROFIT_MARGIN_USDC:
            raise PaymentValidationError("Dynamic UEG rejected request; projected margin is too low")
        markdown = await fetch_clean_markdown(target_url)
    except NetworkUnavailableError as exc:
        if payment is not None:
            await release_processed_tx(payment.tx_hash)
        raise retry_later_503(exc) from exc
    except RuntimeError as exc:
        if payment is not None:
            await release_processed_tx(payment.tx_hash)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except PaymentValidationError as exc:
        if payment is not None:
            await release_processed_tx(payment.tx_hash)
        raise HTTPException(status_code=400, detail=exc.detail) from exc

    return {
        "status": "success",
        "target_url": target_url,
        "markdown": markdown,
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
