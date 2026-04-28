import asyncio
import os
from cdp import CdpClient
from dotenv import load_dotenv

# Load your provided keys from .env
load_dotenv()

async def verify_axongate_sovereignty():
    # 1. Pull keys from .env
    api_key_id = os.getenv("CDP_API_KEY_ID")
    api_key_secret = os.getenv("CDP_API_KEY_SECRET")
    wallet_secret = os.getenv("CDP_WALLET_SECRET")

    print(f"🔄 Connecting to AxonGate Chassis...")
    
    # Initialize the April 2026 CDP Client
    async with CdpClient(
        api_key_id=api_key_id,
        api_key_secret=api_key_secret,
        wallet_secret=wallet_secret
    ) as client:
        
        try:
            # 2. Re-summon the Vault
            wallet = await client.evm.get_or_create_account(name="axongate-primary")
            
            print(f"\n🦾 AxonGate Status: [ACTIVE]")
            print(f"📍 Vault Address: {wallet.address}")
            print(f"🆔 Sovereign Name: axongate.base.eth (Resolution Pending Update)")
            
            # 3. Check for ETH Fuel
            # The 2026 SDK uses list_token_balances for high-speed indexing
            response = await client.evm.list_token_balances(
                address=wallet.address,
                network="base"
            )
            
            eth_amount = 0.0
            for b in response.balances:
                if b.token.symbol == "ETH":
                    eth_amount = float(b.amount.amount) / (10 ** b.amount.decimals)
                    break
            
            print(f"💰 Fuel Level: {eth_amount} ETH")
            
            if eth_amount > 0:
                print("\n✅ MISSION READY: The vault is fueled and own its identity.")
                print("🚀 You can now proceed to run the 'Revenue Server' script.")
            else:
                print("\n🚨 FUEL REQUIRED: Please send the $5 ETH to the address above.")

        except Exception as e:
            print(f"❌ Error reading vault state: {e}")

if __name__ == "__main__":
    asyncio.run(verify_axongate_sovereignty())