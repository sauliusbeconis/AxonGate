import asyncio
import os
from cdp import CdpClient
from cdp.evm_transaction_types import TransactionRequestEIP1559
from dotenv import load_dotenv
from eth_utils import keccak
import eth_abi

load_dotenv()

def get_namehash(name: str) -> bytes:
    """Calculates the ENS namehash for axongate.base.eth"""
    node = b'\x00' * 32
    if name:
        for label in reversed(name.split(".")):
            node = keccak(node + keccak(text=label))
    return node

async def execute_resolution_update():
    api_key_id = os.getenv("CDP_API_KEY_ID")
    api_key_secret = os.getenv("CDP_API_KEY_SECRET")
    wallet_secret = os.getenv("CDP_WALLET_SECRET")

    print("🔄 Connecting to AxonGate Chassis to update resolution...")

    async with CdpClient(
        api_key_id=api_key_id,
        api_key_secret=api_key_secret,
        wallet_secret=wallet_secret
    ) as client:
        try:
            wallet = await client.evm.get_or_create_account(name="axongate-primary")
            VAULT_ADDRESS = wallet.address
            
            # Standard Base Mainnet L2 Public Resolver Contract
            RESOLVER_ADDRESS = "0xC6d566A56A1aFf6508b41f6c90ff131615583BCD" 
            
            # 1. Compute the namehash and encode the setAddr function
            node = get_namehash("axongate.base.eth")
            method_id = bytes.fromhex("d5fa2b00") # setAddr(bytes32,address)
            encoded_args = eth_abi.encode(['bytes32', 'address'], [node, VAULT_ADDRESS])
            tx_data = "0x" + (method_id + encoded_args).hex()

            print(f"📡 Broadcasting setAddr transaction for axongate.base.eth -> {VAULT_ADDRESS}")
            
            # 2. Send transaction using the strictly verified CDP SDK signature
            tx_hash = await client.evm.send_transaction(
                address=wallet.address,
                transaction=TransactionRequestEIP1559(
                    to=RESOLVER_ADDRESS,
                    data=tx_data
                ),
                network="base"
            )
            
            print(f"✅ Resolution Update Dispatched!")
            
            # The SDK might return a string hash or an object with a transaction_hash attribute
            hash_str = getattr(tx_hash, 'transaction_hash', tx_hash)
            print(f"🔗 Transaction Hash: {hash_str}")

        except Exception as e:
            print(f"❌ Error updating resolution: {e}")

if __name__ == "__main__":
    asyncio.run(execute_resolution_update())