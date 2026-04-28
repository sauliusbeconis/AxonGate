from fastapi import FastAPI, HTTPException, Header, Query
from pydantic import BaseModel
import uvicorn
import time
import json
import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from web3 import Web3

load_dotenv()

app = FastAPI(title="AxonGate Sovereign Gateway")

BASE_MAINNET_RPC_URL = os.getenv("BASE_RPC_URL", "https://mainnet.base.org")
BASE_USDC_ADDRESS = Web3.to_checksum_address(
    os.getenv("BASE_USDC_ADDRESS", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")
)
REQUIRED_USDC_AMOUNT = 20_000
TRANSFER_TOPIC = Web3.keccak(text="Transfer(address,address,uint256)").hex()

# Request Model expected from other Autonomous Agents
class ComputeRequest(BaseModel):
    agent_id: str
    task_payload: dict
    offered_fee: float # The ETH amount the requesting agent is willing to pay

# Unit Economic Guardian (UEG) Parameters
BASE_INFERENCE_COST = 0.0005
BASE_GAS_ESTIMATE = 0.0001
PROFIT_MARGIN_REQUIREMENT = 0.002

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

def as_hex(value) -> str:
    if hasattr(value, "hex"):
        return value.hex()
    return str(value)

def verify_usdc_transfer(tx_hash: str) -> bool:
    if not tx_hash:
        return False

    web3 = Web3(Web3.HTTPProvider(BASE_MAINNET_RPC_URL))
    if not web3.is_connected():
        raise RuntimeError("Unable to connect to Base RPC")

    try:
        vault_address = load_vault_address()
        receipt = web3.eth.get_transaction_receipt(tx_hash)
    except Exception as exc:
        print(f"[X402 VERIFY] Could not load transaction receipt: {exc}")
        return False

    if receipt is None or receipt.get("status") != 1:
        return False

    vault_topic = "0x" + vault_address.lower().replace("0x", "").rjust(64, "0")

    for log in receipt.get("logs", []):
        log_address = Web3.to_checksum_address(log.get("address"))
        topics = [as_hex(topic) for topic in log.get("topics", [])]

        if log_address != BASE_USDC_ADDRESS:
            continue
        if len(topics) < 3 or topics[0].lower() != TRANSFER_TOPIC.lower():
            continue
        if topics[2].lower() != vault_topic:
            continue

        amount = int(as_hex(log.get("data", "0x0")), 16)
        if amount == REQUIRED_USDC_AMOUNT:
            return True

    return False

@app.post("/v1/access")
async def verify_access(
    tx_hash: Optional[str] = Query(None),
    x_tx_hash: Optional[str] = Header(None, alias="x-tx-hash"),
    x402_tx_hash: Optional[str] = Header(None, alias="x402-tx-hash"),
):
    submitted_tx_hash = tx_hash or x_tx_hash or x402_tx_hash

    if not submitted_tx_hash:
        raise HTTPException(
            status_code=402,
            detail="Payment Required. Provide a Base USDC transaction hash via tx_hash, x-tx-hash, or x402-tx-hash.",
        )

    try:
        payment_valid = verify_usdc_transfer(submitted_tx_hash)
    except RuntimeError as exc:
        print(f"[X402 VERIFY] Infrastructure error: {exc}")
        raise HTTPException(status_code=503, detail="Payment verification temporarily unavailable") from exc

    if not payment_valid:
        raise HTTPException(
            status_code=402,
            detail="Payment Required. Transaction must be a successful 0.02 USDC transfer to the AxonGate vault on Base.",
        )

    return {
        "status": "success",
        "message": "x402 payment verified. AxonGate access granted.",
        "tx_hash": submitted_tx_hash,
        "network": "base-mainnet",
        "amount_usdc": 0.02,
    }

@app.post("/v1/broker/compute")
async def process_task(request: ComputeRequest, x402_token: str = Header(None)):
    print(f"\n📡 [INBOUND REQUEST] Agent: {request.agent_id}")
    
    # 1. Verify Payment Protocol (x402)
    if not x402_token:
        print("❌ [REJECTED] Missing x402 Payment Header")
        raise HTTPException(status_code=401, detail="Missing x402 Payment Token")

    # 2. Unit Economic Guardian (UEG) Check
    projected_cost = BASE_INFERENCE_COST + BASE_GAS_ESTIMATE
    projected_profit = request.offered_fee - projected_cost

    print(f"🧮 [UEG CHECK] Offered: {request.offered_fee} ETH | Cost: {projected_cost} ETH | Profit: {projected_profit} ETH")

    if projected_profit <= PROFIT_MARGIN_REQUIREMENT:
        minimum_fee = projected_cost + PROFIT_MARGIN_REQUIREMENT
        print(f"❌ [UEG REJECTED] Insufficient margin. Required: {minimum_fee} ETH")
        raise HTTPException(
            status_code=402, 
            detail=f"Payment Required. UEG rejected transaction. Minimum viable fee is {minimum_fee} ETH"
        )

    print("✅ [UEG PASSED] Executing API Brokerage...")

    # 3. Proceed with API Brokerage
    # In a production environment, this is where you route request.task_payload to an LLM provider
    time.sleep(1.5) # Simulating compute time
    
    response_payload = {
        "status": "success",
        "message": "Task processed successfully via AxonGate Brokerage",
        "result": "simulated_llm_output_data",
        "ueg_receipt": {
            "fee_collected": request.offered_fee,
            "net_profit": projected_profit,
            "timestamp": time.time()
        }
    }
    
    print("📤 [DISPATCHING RESPONSE] Task complete.")
    return response_payload

if __name__ == "__main__":
    print("🚀 Booting AxonGate Revenue Server...")
    print("🌐 Listening for Agent-to-Agent (A2A) traffic on port 8000")
    uvicorn.run(app, host="0.0.0.0", port=8000)
