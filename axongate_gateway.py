import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field
from web3 import Web3
from web3.exceptions import TransactionNotFound

load_dotenv()

app = FastAPI(title="AxonGate Sovereign Gateway")

BASE_MAINNET_RPC_URL = os.getenv("BASE_RPC_URL", "https://mainnet.base.org")
BASE_RPC_TIMEOUT_SECONDS = float(os.getenv("BASE_RPC_TIMEOUT_SECONDS", "5"))
BASE_USDC_ADDRESS = Web3.to_checksum_address(
    os.getenv("BASE_USDC_ADDRESS", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")
)

USDC_DECIMALS = 6
REQUIRED_USDC_FEE = Decimal(os.getenv("AXONGATE_BASE_FEE_USDC", "0.02"))
REQUIRED_USDC_AMOUNT = int(REQUIRED_USDC_FEE * (Decimal(10) ** USDC_DECIMALS))

FIXED_API_OVERHEAD_USDC = Decimal(os.getenv("AXONGATE_FIXED_API_OVERHEAD_USDC", "0.0005"))
PROFIT_MARGIN_REQUIREMENT_USDC = Decimal(os.getenv("AXONGATE_PROFIT_MARGIN_USDC", "0.002"))
UEG_GAS_UNITS = int(os.getenv("AXONGATE_UEG_GAS_UNITS", "21000"))
ETH_USDC_PRICE = Decimal(os.getenv("AXONGATE_ETH_USDC_PRICE", "3500"))

TRANSFER_TOPIC = Web3.keccak(text="Transfer(address,address,uint256)").hex()

web3 = Web3(
    Web3.HTTPProvider(
        BASE_MAINNET_RPC_URL,
        request_kwargs={"timeout": BASE_RPC_TIMEOUT_SECONDS},
    )
)

processed_payment_hashes: set[str] = set()
processed_payment_hash_lock = asyncio.Lock()


class ComputeRequest(BaseModel):
    agent_id: str
    task_payload: dict[str, Any]
    offered_fee: float = Field(..., description="Fee offered by the client agent in USDC")


@dataclass(frozen=True)
class UEGReceipt:
    fee_received_usdc: Decimal
    dynamic_gas_cost_usdc: Decimal
    fixed_api_overhead_usdc: Decimal
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
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


class NetworkUnavailableError(Exception):
    pass


def load_vault_address() -> str:
    env_address = os.getenv("AXONGATE_VAULT_ADDRESS") or os.getenv("VAULT_ADDRESS")
    if env_address:
        return Web3.to_checksum_address(env_address)

    manifest_path = Path(__file__).with_name("agent_manifest.json")
    if manifest_path.exists():
        manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_address = manifest_data.get("vault_address")
        if manifest_address:
            return Web3.to_checksum_address(manifest_address)

    raise RuntimeError("Vault address is not configured")


def as_0x_hex(value: Any) -> str:
    if isinstance(value, str):
        return value if value.startswith("0x") else f"0x{value}"
    if hasattr(value, "hex"):
        hex_value = value.hex()
        return hex_value if hex_value.startswith("0x") else f"0x{hex_value}"
    return str(value)


def usdc_amount_from_units(units: int) -> Decimal:
    return Decimal(units) / (Decimal(10) ** USDC_DECIMALS)


def normalize_tx_hash(tx_hash: str) -> str:
    normalized = tx_hash.strip().lower()
    if not re.fullmatch(r"0x[a-fA-F0-9]{64}", normalized):
        raise PaymentValidationError(400, "Invalid payment hash format")
    return normalized


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


async def ensure_base_rpc_ready() -> None:
    connected = await call_base_rpc("connectivity check", web3.is_connected)
    if not connected:
        raise NetworkUnavailableError("Base RPC is not reachable")


async def fetch_current_base_fee_wei() -> int:
    """Fetch the latest EIP-1559 base fee from Base for dynamic UEG pricing."""
    await ensure_base_rpc_ready()
    latest_block = await call_base_rpc("latest block lookup", lambda: web3.eth.get_block("latest"))
    base_fee = latest_block.get("baseFeePerGas")
    if base_fee is None:
        gas_price = await call_base_rpc("gas price lookup", lambda: web3.eth.gas_price)
        return int(gas_price)
    return int(base_fee)


async def calculate_dynamic_gas_cost_usdc() -> tuple[Decimal, int]:
    base_fee_wei = await fetch_current_base_fee_wei()
    gas_cost_eth = Decimal(base_fee_wei * UEG_GAS_UNITS) / Decimal(10**18)
    return gas_cost_eth * ETH_USDC_PRICE, base_fee_wei


async def is_profitable(fee_received_usdc: Decimal) -> UEGReceipt:
    """Run the dynamic Unit Economic Guardian check with live Base fee data."""
    dynamic_gas_cost_usdc, base_fee_wei = await calculate_dynamic_gas_cost_usdc()
    projected_profit = fee_received_usdc - (dynamic_gas_cost_usdc + FIXED_API_OVERHEAD_USDC)

    return UEGReceipt(
        fee_received_usdc=fee_received_usdc,
        dynamic_gas_cost_usdc=dynamic_gas_cost_usdc,
        fixed_api_overhead_usdc=FIXED_API_OVERHEAD_USDC,
        projected_profit_usdc=projected_profit,
        base_fee_wei=base_fee_wei,
        gas_units=UEG_GAS_UNITS,
    )


def assert_ueg_margin(receipt: UEGReceipt) -> None:
    if receipt.projected_profit_usdc <= PROFIT_MARGIN_REQUIREMENT_USDC:
        minimum_fee = (
            receipt.dynamic_gas_cost_usdc
            + receipt.fixed_api_overhead_usdc
            + PROFIT_MARGIN_REQUIREMENT_USDC
        )
        raise PaymentValidationError(
            402,
            f"Payment Required. Dynamic UEG rejected transaction. "
            f"Minimum viable fee is {minimum_fee:.6f} USDC",
        )


async def validate_x402_payment(tx_hash: str) -> PaymentVerification:
    """
    Validate the AxonGate x402 payment loop against Base mainnet.

    The client supplies X-AxonGate-Payment-Hash after sending the 0.02 USDC fee.
    AxonGate never trusts that hash by itself. The gateway queries Base, verifies
    the transaction receipt succeeded, requires the transaction call to target the
    Base USDC contract, parses the canonical ERC-20 Transfer logs, and accepts only
    an exact 0.02 USDC transfer whose recipient is the AxonGate vault address.

    A successful hash is inserted into an in-memory replay cache after validation.
    Railway may run more than one process or restart the process, so this cache is
    intentionally a lightweight first line of defense; a shared cache or database is
    the next production step if the deployment scales horizontally.
    """
    normalized_hash = normalize_tx_hash(tx_hash)

    async with processed_payment_hash_lock:
        if normalized_hash in processed_payment_hashes:
            raise PaymentValidationError(400, "Payment hash has already been processed")

    vault_address = load_vault_address()
    await ensure_base_rpc_ready()

    try:
        transaction, receipt = await asyncio.gather(
            call_base_rpc("transaction lookup", lambda: web3.eth.get_transaction(normalized_hash)),
            call_base_rpc("transaction receipt lookup", lambda: web3.eth.get_transaction_receipt(normalized_hash)),
        )
    except TransactionNotFound as exc:
        raise PaymentValidationError(400, "Payment transaction was not found on Base") from exc

    if receipt.get("status") != 1:
        raise PaymentValidationError(402, "Payment transaction was not successful")

    transaction_to = transaction.get("to")
    if not transaction_to or Web3.to_checksum_address(transaction_to) != BASE_USDC_ADDRESS:
        raise PaymentValidationError(402, "Payment transaction must call the Base USDC contract")

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
        raise PaymentValidationError(
            402,
            f"Payment must transfer exactly {REQUIRED_USDC_FEE} USDC to the AxonGate vault",
        )

    async with processed_payment_hash_lock:
        if normalized_hash in processed_payment_hashes:
            raise PaymentValidationError(400, "Payment hash has already been processed")
        processed_payment_hashes.add(normalized_hash)

    return PaymentVerification(
        tx_hash=normalized_hash,
        vault_address=vault_address,
        token_address=BASE_USDC_ADDRESS,
        amount_usdc=REQUIRED_USDC_FEE,
    )


def retry_later_503(exc: Exception) -> HTTPException:
    print(f"[BASE RPC] Temporary verification failure: {exc}")
    return HTTPException(
        status_code=503,
        detail="Base RPC temporarily unavailable. Client agent should retry in 5 seconds.",
    )


@app.get("/health")
async def health():
    return {"status": "alive", "vault_address": load_vault_address()}


@app.post("/v1/access")
async def verify_access(
    x_axongate_payment_hash: Optional[str] = Header(None, alias="X-AxonGate-Payment-Hash"),
    tx_hash: Optional[str] = Query(None),
    x_tx_hash: Optional[str] = Header(None, alias="x-tx-hash"),
    x402_tx_hash: Optional[str] = Header(None, alias="x402-tx-hash"),
):
    submitted_tx_hash = x_axongate_payment_hash or tx_hash or x_tx_hash or x402_tx_hash

    if not submitted_tx_hash:
        raise HTTPException(
            status_code=402,
            detail="Payment Required. Provide X-AxonGate-Payment-Hash with a Base USDC transaction hash.",
        )

    try:
        payment = await validate_x402_payment(submitted_tx_hash)
        ueg_receipt = await is_profitable(payment.amount_usdc)
        assert_ueg_margin(ueg_receipt)
    except NetworkUnavailableError as exc:
        raise retry_later_503(exc) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except PaymentValidationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    return {
        "status": "success",
        "message": "x402 payment verified on Base. AxonGate access granted.",
        "tx_hash": payment.tx_hash,
        "network": "base-mainnet",
        "vault_address": payment.vault_address,
        "token_address": payment.token_address,
        "amount_usdc": float(payment.amount_usdc),
        "ueg_receipt": {
            "fee_received_usdc": float(ueg_receipt.fee_received_usdc),
            "dynamic_gas_cost_usdc": float(ueg_receipt.dynamic_gas_cost_usdc),
            "fixed_api_overhead_usdc": float(ueg_receipt.fixed_api_overhead_usdc),
            "projected_profit_usdc": float(ueg_receipt.projected_profit_usdc),
            "base_fee_wei": ueg_receipt.base_fee_wei,
            "gas_units": ueg_receipt.gas_units,
        },
    }


@app.post("/v1/broker/compute")
async def process_task(request: ComputeRequest, x402_token: str = Header(None)):
    print(f"\n[INBOUND REQUEST] Agent: {request.agent_id}")

    if not x402_token:
        print("[REJECTED] Missing x402 Payment Header")
        raise HTTPException(status_code=401, detail="Missing x402 Payment Token")

    try:
        offered_fee_usdc = Decimal(str(request.offered_fee))
        ueg_receipt = await is_profitable(offered_fee_usdc)
        assert_ueg_margin(ueg_receipt)
    except NetworkUnavailableError as exc:
        raise retry_later_503(exc) from exc
    except PaymentValidationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    print(
        "[UEG CHECK] "
        f"Offered: {ueg_receipt.fee_received_usdc:.6f} USDC | "
        f"Dynamic Gas: {ueg_receipt.dynamic_gas_cost_usdc:.6f} USDC | "
        f"Overhead: {ueg_receipt.fixed_api_overhead_usdc:.6f} USDC | "
        f"Profit: {ueg_receipt.projected_profit_usdc:.6f} USDC"
    )
    print("[UEG PASSED] Executing API Brokerage...")

    await asyncio.sleep(1.5)

    response_payload = {
        "status": "success",
        "message": "Task processed successfully via AxonGate Brokerage",
        "result": "simulated_llm_output_data",
        "ueg_receipt": {
            "fee_collected_usdc": float(ueg_receipt.fee_received_usdc),
            "dynamic_gas_cost_usdc": float(ueg_receipt.dynamic_gas_cost_usdc),
            "net_profit_usdc": float(ueg_receipt.projected_profit_usdc),
            "timestamp": time.time(),
        },
    }

    print("[DISPATCHING RESPONSE] Task complete.")
    return response_payload


if __name__ == "__main__":
    print("Booting AxonGate Revenue Server...")
    print("Listening for Agent-to-Agent (A2A) traffic on port 8000")
    uvicorn.run(app, host="0.0.0.0", port=8000)
