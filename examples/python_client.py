"""Call AxonGate from Python, paying per call with x402.

    pip install "x402[httpx]" eth-account
    AXONGATE_PRIVATE_KEY=0x... python examples/python_client.py "Is HTTP 402 reserved for future use?"

The wallet needs a little USDC on Base. No ETH is needed: the facilitator pays gas.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

from eth_account import Account
from x402 import x402Client
from x402.http.clients.httpx import x402HttpxClient
from x402.mechanisms.evm.exact.register import register_exact_evm_client
from x402.mechanisms.evm.signers import EthAccountSigner

BASE_URL = os.getenv("AXONGATE_BASE_URL", "https://api.axongate.one")


async def main(claim: str) -> None:
    account = Account.from_key(os.environ["AXONGATE_PRIVATE_KEY"])
    client = x402Client()
    register_exact_evm_client(client, EthAccountSigner(account))

    async with x402HttpxClient(client, base_url=BASE_URL, timeout=90) as http:
        # $0.02: verdict + verbatim-checked evidence
        verdict = await http.post("/v1/verify", json={"claim": claim})
        print(json.dumps(verdict.json(), indent=2)[:3000])

        # $0.002: read one of the sources as clean, injection-screened markdown
        sources = verdict.json().get("sources") or []
        if sources:
            page = await http.get("/v1/read", params={"url": sources[0]["url"], "max_chars": 2000})
            data = page.json()
            print(data["title"], data["injection_risk"]["risk"])
            print(data["markdown"][:500])


if __name__ == "__main__":
    asyncio.run(main(" ".join(sys.argv[1:]) or "HTTP status code 402 is reserved for future use."))
