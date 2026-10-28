"""Pay for real AxonGate calls with a CDP server account (operator tool).

A settled call through the CDP facilitator is what gets an endpoint indexed in
the x402 Bazaar. CDP rejects payments from the payTo address itself, so the
payer is a separate CDP account (created on first use). --fund tops it up from
the vault first; the USDC paid for the calls flows straight back to the vault.

    CDP_API_KEY_ID=... CDP_API_KEY_SECRET=... CDP_WALLET_SECRET=... \
      python scripts/paid_smoke.py --fund 0.10 --max-spend 0.10
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
from decimal import Decimal

import httpx
from cdp import CdpClient, EvmLocalAccount, parse_units
from x402 import x402Client
from x402.http.clients.httpx import x402HttpxClient
from x402.mechanisms.evm.exact.register import register_exact_evm_client
from x402.mechanisms.evm.signers import EthAccountSigner

CALLS = {
    "read": ("GET", "/v1/read", {"url": "https://en.wikipedia.org/wiki/HTTP_402"}, Decimal("0.002")),
    "search": ("GET", "/v1/search", {"q": "x402 payment protocol", "limit": 5}, Decimal("0.004")),
    "scan": ("POST", "/v1/scan", {"text": "Great recipe! Bake at 180C for 25 minutes. Reviewers say it is the best banana bread they have tried."}, Decimal("0.002")),
    "answer": ("POST", "/v1/answer", {"question": "What does HTTP status code 402 mean?"}, Decimal("0.015")),
    "verify": ("POST", "/v1/verify", {"claim": "HTTP status code 402 is reserved for future use."}, Decimal("0.02")),
    "token": ("GET", "/v1/token-check", {"address": "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed", "chain": "base"}, Decimal("0.01")),
    "batch": ("POST", "/v1/read/batch", {"urls": ["https://en.wikipedia.org/wiki/HTTP_402", "https://example.com"], "max_chars": 2000}, Decimal("0.03")),
}

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
BASE_RPC = "https://base-rpc.publicnode.com"


async def usdc_balance(address: str) -> Decimal:
    data = "0x70a08231" + address[2:].lower().rjust(64, "0")
    async with httpx.AsyncClient(timeout=20) as rpc:
        r = await rpc.post(BASE_RPC, json={"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [{"to": USDC, "data": data}, "latest"]})
    return Decimal(int(r.json()["result"], 16)) / Decimal(10**6)


async def fund(cdp, vault_name: str, buyer, amount: Decimal) -> bool:
    vault = await cdp.evm.get_account(name=vault_name)
    before = await usdc_balance(buyer.address)
    print(f"funding {buyer.address} with {amount} USDC from {vault.address}")
    await vault.transfer(to=buyer.address, amount=parse_units(str(amount), 6), token="usdc", network="base")
    for _ in range(30):
        if await usdc_balance(buyer.address) >= before + amount:
            return True
        await asyncio.sleep(2)
    print("funding transfer not confirmed after 60s")
    return False


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("products", nargs="*", default=list(CALLS))
    parser.add_argument("--base-url", default="https://api.axongate.one")
    parser.add_argument("--account", default="axongate-smoke-buyer", help="CDP EVM account name used as payer (created if missing)")
    parser.add_argument("--vault", default="axongate-primary", help="CDP account that receives payments and funds --fund")
    parser.add_argument("--fund", type=Decimal, default=Decimal("0"), help="USDC to move from --vault to the payer first")
    parser.add_argument("--max-spend", type=Decimal, default=Decimal("0.10"))
    args = parser.parse_args()

    planned = sum(CALLS[p][3] for p in args.products)
    if planned > args.max_spend:
        print(f"Planned spend {planned} exceeds --max-spend {args.max_spend}")
        return 2

    if args.account == args.vault:
        print("The payer must not be the vault: CDP rejects self-payment (self_send_not_allowed)")
        return 2
    if args.fund > Decimal("1"):
        print("--fund is capped at 1 USDC")
        return 2

    async with CdpClient() as cdp:
        server_account = await cdp.evm.get_or_create_account(name=args.account)
        if args.fund and not await fund(cdp, args.vault, server_account, args.fund):
            return 1
        balance = await usdc_balance(server_account.address)
        if balance < planned:
            print(f"payer {server_account.address} holds {balance} USDC, needs {planned}; rerun with --fund {planned}")
            return 2
        signer = EthAccountSigner(EvmLocalAccount(server_account))
        client = x402Client()
        register_exact_evm_client(client, signer)
        print(f"payer {server_account.address}, planned spend {planned} USDC")
        failures = 0
        async with x402HttpxClient(client, base_url=args.base_url.rstrip("/"), timeout=90) as http:
            for name in args.products:
                method, path, payload, price = CALLS[name]
                if method == "GET":
                    response = await http.get(path, params=payload)
                else:
                    response = await http.post(path, json=payload)
                settlement = response.headers.get("payment-response") or response.headers.get("x-payment-response")
                tx = ""
                if settlement:
                    try:
                        tx = json.loads(base64.b64decode(settlement)).get("transaction", "")
                    except Exception:
                        tx = "unparsed"
                ok = response.status_code == 200 and bool(settlement)
                failures += 0 if ok else 1
                print(f"{'ok  ' if ok else 'FAIL'} {name:7} HTTP {response.status_code} ${price} tx={tx} {response.text[:160] if not ok else ''}")
                extensions = response.headers.get("extension-responses")
                if extensions:
                    print(f"     extension-responses: {base64.b64decode(extensions).decode(errors='replace')[:300]}")
        return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
