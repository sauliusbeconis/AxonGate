"""Token safety verdicts for agents about to buy or accept a crypto token.

Security data comes from GoPlus Security (contract analysis, holders, liquidity) and,
on chains it covers, a honeypot.is buy/sell simulation as a second opinion. The verdict
is rule-based so the same facts always give the same answer.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from typing import Any

import httpx

from . import config
from .store import store

GOPLUS = "https://api.gopluslabs.io/api/v1"
HONEYPOT_IS = "https://api.honeypot.is/v2/IsHoneypot"
CACHE_TTL_SECONDS = 300

# chain name -> (GoPlus chain id, honeypot.is chain id or None)
EVM_CHAINS: dict[str, tuple[str, int | None]] = {
    "ethereum": ("1", 1),
    "base": ("8453", 8453),
    "bsc": ("56", 56),
    "arbitrum": ("42161", None),
    "polygon": ("137", None),
    "optimism": ("10", None),
    "avalanche": ("43114", None),
}
CHAIN_ALIASES = {"eth": "ethereum", "mainnet": "ethereum", "bnb": "bsc", "arb": "arbitrum", "matic": "polygon", "op": "optimism", "avax": "avalanche", "sol": "solana"}
CHAINS = tuple(EVM_CHAINS) + ("solana",)

_EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_SOLANA_ADDRESS = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

SEVERITY_POINTS = {"critical": 100, "high": 35, "medium": 15, "low": 5}


class TokenError(Exception):
    def __init__(self, message: str, status: int = 503, code: str = "token_data_unavailable") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def normalize_chain(chain: str | None) -> str:
    name = (chain or "base").strip().lower()
    name = CHAIN_ALIASES.get(name, name)
    if name not in CHAINS:
        raise TokenError(f"chain must be one of: {', '.join(CHAINS)}", 400, "invalid_request")
    return name


def validate_address(address: str, chain: str) -> str:
    address = (address or "").strip()
    pattern = _SOLANA_ADDRESS if chain == "solana" else _EVM_ADDRESS
    if not pattern.match(address):
        raise TokenError(f"address is not a valid {'Solana mint' if chain == 'solana' else 'EVM contract'} address", 400, "invalid_request")
    return address


def _f(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _on(value: Any) -> bool:
    return str(value) == "1"


class _Flags:
    def __init__(self) -> None:
        self.items: list[dict[str, str]] = []

    def add(self, code: str, severity: str, detail: str) -> None:
        self.items.append({"code": code, "severity": severity, "detail": detail})


def _tax_flags(flags: _Flags, kind: str, tax: float | None) -> None:
    if tax is None:
        return
    if tax >= 0.5:
        flags.add(f"{kind}_tax_extreme", "critical", f"{kind} tax is {tax:.0%}")
    elif tax > 0.1:
        flags.add(f"{kind}_tax_high", "high", f"{kind} tax is {tax:.0%}")
    elif tax > 0.05:
        flags.add(f"{kind}_tax_elevated", "medium", f"{kind} tax is {tax:.0%}")


def _concentration_flags(flags: _Flags, top10: float | None) -> None:
    if top10 is None:
        return
    if top10 > 0.7:
        flags.add("holders_concentrated", "high", f"top 10 wallets (excluding contracts and locks) hold {top10:.0%} of supply")
    elif top10 > 0.4:
        flags.add("holders_concentrated", "medium", f"top 10 wallets (excluding contracts and locks) hold {top10:.0%} of supply")


def _liquidity_flags(flags: _Flags, liquidity: float | None, listed_on_cex: bool) -> None:
    if listed_on_cex:
        return
    if liquidity is None or liquidity <= 0:
        flags.add("no_dex_liquidity", "high", "no DEX liquidity found")
    elif liquidity < 10_000:
        flags.add("liquidity_very_low", "high", f"total DEX liquidity is about ${liquidity:,.0f}")
    elif liquidity < 50_000:
        flags.add("liquidity_low", "medium", f"total DEX liquidity is about ${liquidity:,.0f}")


def _evm_facts(t: dict[str, Any], flags: _Flags) -> dict[str, Any]:
    trusted = _on(t.get("trust_list"))
    cex = t.get("is_in_cex") or {}
    listed_on_cex = _on(cex.get("listed")) if isinstance(cex, dict) else False
    liquidity = sum(_f(d.get("liquidity")) or 0 for d in t.get("dex") or []) if t.get("dex") else None
    wallets = [h for h in t.get("holders") or [] if not h.get("is_contract") and not h.get("is_locked")]
    top10 = sum(_f(h.get("percent")) or 0 for h in wallets[:10]) if t.get("holders") else None
    lp = t.get("lp_holders") or []
    lp_locked = sum(_f(h.get("percent")) or 0 for h in lp if h.get("is_locked")) if lp else None
    buy_tax, sell_tax = _f(t.get("buy_tax")), _f(t.get("sell_tax"))

    if _on(t.get("is_honeypot")):
        flags.add("honeypot", "critical", "contract analysis marks this token as a honeypot")
    if _on(t.get("cannot_sell_all")):
        flags.add("cannot_sell_all", "critical", "holders cannot sell their full balance")
    if _on(t.get("cannot_buy")) and not trusted:
        flags.add("cannot_buy", "high", "the token cannot be bought on DEXes")
    # Widely held, exchange-listed tokens (USDT, stablecoins, majors) are issuer-controlled by design:
    # report those powers, but as context rather than as reasons to avoid the token.
    established = trusted or (listed_on_cex and (_f(t.get("holder_count")) or 0) >= 100_000)
    if _on(t.get("owner_change_balance")):
        if established:
            flags.add("owner_can_change_balances", "low", "the issuer can change holder balances (common for issuer-controlled tokens)")
        else:
            flags.add("owner_can_change_balances", "critical", "the owner can change any holder's balance")
    _tax_flags(flags, "buy", buy_tax)
    _tax_flags(flags, "sell", sell_tax)
    if established and not trusted:
        for key, detail in (("is_blacklisted", "the issuer can blacklist wallets"), ("transfer_pausable", "the issuer can pause transfers"), ("is_mintable", "new supply can be minted"), ("is_proxy", "the contract is upgradeable (proxy)")):
            if _on(t.get(key)):
                flags.add(key, "low", detail)
    elif not trusted:
        if str(t.get("is_open_source")) == "0":
            flags.add("not_open_source", "high", "contract source code is not verified")
        for key, severity, detail in (
            ("hidden_owner", "high", "the contract has a hidden owner"),
            ("can_take_back_ownership", "high", "renounced ownership can be reclaimed"),
            ("selfdestruct", "high", "the contract can self-destruct"),
            ("slippage_modifiable", "high", "the owner can change trading taxes"),
            ("personal_slippage_modifiable", "high", "the owner can set taxes for specific wallets"),
            ("honeypot_with_same_creator", "high", "the creator has deployed honeypots before"),
            ("transfer_pausable", "medium", "the owner can pause transfers"),
            ("is_blacklisted", "medium", "the owner can blacklist wallets"),
            ("is_mintable", "medium", "new supply can be minted"),
            ("is_proxy", "medium", "the contract is upgradeable (proxy)"),
            ("external_call", "low", "the contract calls external contracts"),
            ("trading_cooldown", "low", "trading has a cooldown"),
            ("anti_whale_modifiable", "low", "anti-whale limits can be changed"),
        ):
            if _on(t.get(key)):
                flags.add(key, severity, detail)
        _concentration_flags(flags, top10)
        _liquidity_flags(flags, liquidity, listed_on_cex)

    return {
        "name": t.get("token_name"),
        "symbol": t.get("token_symbol"),
        "trusted_token": trusted,
        "established_token": established,
        "listed_on_cex": listed_on_cex,
        "buy_tax": buy_tax,
        "sell_tax": sell_tax,
        "holder_count": int(_f(t.get("holder_count")) or 0) or None,
        "top10_wallet_share": round(top10, 4) if top10 is not None else None,
        "creator_share": _f(t.get("creator_percent")),
        "owner_share": _f(t.get("owner_percent")),
        "owner_address": t.get("owner_address") or None,
        "lp_locked_share": round(lp_locked, 4) if lp_locked is not None else None,
        "dex_liquidity_usd": round(liquidity, 2) if liquidity is not None else None,
        "is_open_source": None if t.get("is_open_source") in (None, "") else _on(t.get("is_open_source")),
        "is_proxy": None if t.get("is_proxy") in (None, "") else _on(t.get("is_proxy")),
        "is_mintable": None if t.get("is_mintable") in (None, "") else _on(t.get("is_mintable")),
    }


def _authority_on(t: dict[str, Any], key: str) -> bool:
    value = t.get(key)
    return isinstance(value, dict) and _on(value.get("status"))


def _solana_facts(t: dict[str, Any], flags: _Flags) -> dict[str, Any]:
    trusted = _on(t.get("trusted_token"))
    meta = t.get("metadata") or {}
    liquidity = sum(_f(d.get("tvl")) or 0 for d in t.get("dex") or []) if t.get("dex") else None
    holders = [h for h in t.get("holders") or [] if not h.get("is_locked")]
    top10 = sum(_f(h.get("percent")) or 0 for h in holders[:10]) if t.get("holders") else None
    fee = t.get("transfer_fee") or {}
    fee_rate = (_f(fee.get("fee_rate")) or 0) / 100 if isinstance(fee, dict) and fee else None

    if not trusted:
        for key, severity, detail in (
            ("balance_mutable_authority", "critical", "an authority can change holder balances"),
            ("freezable", "high", "an authority can freeze token accounts"),
            ("closable", "high", "an authority can close the mint"),
            ("mintable", "medium", "new supply can be minted"),
            ("transfer_fee_upgradable", "medium", "transfer fees can be changed"),
            ("transfer_hook_upgradable", "medium", "a transfer hook can be added or changed"),
            ("default_account_state_upgradable", "medium", "the default account state can be changed"),
            ("metadata_mutable", "low", "token metadata can be changed"),
        ):
            if _authority_on(t, key):
                flags.add(key, severity, detail)
        if _on(t.get("non_transferable")):
            flags.add("non_transferable", "critical", "the token cannot be transferred")
        if t.get("transfer_hook"):
            flags.add("transfer_hook", "medium", "transfers run a custom hook program")
        _tax_flags(flags, "transfer", fee_rate)
        _concentration_flags(flags, top10)
        _liquidity_flags(flags, liquidity, False)

    return {
        "name": meta.get("name"),
        "symbol": meta.get("symbol"),
        "trusted_token": trusted,
        "transfer_fee": fee_rate,
        "holder_count": int(_f(t.get("holder_count")) or 0) or None,
        "top10_wallet_share": round(top10, 4) if top10 is not None else None,
        "dex_liquidity_usd": round(liquidity, 2) if liquidity is not None else None,
        "is_mintable": _authority_on(t, "mintable"),
        "is_freezable": _authority_on(t, "freezable"),
    }


_goplus_token: dict[str, Any] = {"value": "", "expires": 0.0}


async def _goplus_auth(client: httpx.AsyncClient) -> dict[str, str]:
    """Optional GoPlus app key: keyless access works but is rate-limited hard."""
    if not (config.GOPLUS_APP_KEY and config.GOPLUS_APP_SECRET):
        return {}
    if _goplus_token["value"] and time.time() < _goplus_token["expires"]:
        return {"Authorization": _goplus_token["value"]}
    now = int(time.time())
    sign = hashlib.sha1(f"{config.GOPLUS_APP_KEY}{now}{config.GOPLUS_APP_SECRET}".encode()).hexdigest()
    try:
        response = await client.post(f"{GOPLUS}/token", json={"app_key": config.GOPLUS_APP_KEY, "time": now, "sign": sign}, timeout=10)
        result = response.json().get("result") or {}
    except (httpx.HTTPError, ValueError):
        return {}
    if not result.get("access_token"):
        return {}
    _goplus_token.update(value=result["access_token"], expires=time.time() + max(60, int(result.get("expires_in") or 3600) - 60))
    return {"Authorization": _goplus_token["value"]}


async def _goplus(client: httpx.AsyncClient, address: str, chain: str) -> dict[str, Any] | None:
    url = f"{GOPLUS}/solana/token_security" if chain == "solana" else f"{GOPLUS}/token_security/{EVM_CHAINS[chain][0]}"
    for attempt in range(2):
        try:
            response = await client.get(url, params={"contract_addresses": address}, headers=await _goplus_auth(client), timeout=15)
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise TokenError(f"Token security supplier unreachable: {type(exc).__name__}") from exc
        limited = response.status_code == 429 or "too many" in str(data.get("message", "")).lower()
        if not limited:
            break
        if attempt == 0:
            await asyncio.sleep(1.5)
    if response.status_code >= 400 or data.get("code") != 1:
        raise TokenError(f"Token security supplier error: {data.get('message') or response.status_code}. You were not charged.")
    result = data.get("result") or {}
    return result.get(address) or result.get(address.lower()) or (next(iter(result.values())) if result else None)


async def _simulate(client: httpx.AsyncClient, address: str, chain: str) -> dict[str, Any] | None:
    chain_id = EVM_CHAINS.get(chain, (None, None))[1]
    if not chain_id:
        return None
    try:
        response = await client.get(HONEYPOT_IS, params={"address": address, "chainID": chain_id}, timeout=10)
        if response.status_code >= 400:
            return None
        data = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    sim = data.get("simulationResult") or {}
    return {
        "simulation_success": bool(data.get("simulationSuccess")),
        "is_honeypot": bool((data.get("honeypotResult") or {}).get("isHoneypot")),
        # honeypot.is reports taxes in percent
        "buy_tax": _f(sim.get("buyTax")) / 100 if _f(sim.get("buyTax")) is not None else None,
        "sell_tax": _f(sim.get("sellTax")) / 100 if _f(sim.get("sellTax")) is not None else None,
        "risk": (data.get("summary") or {}).get("risk"),
    }


def verdict_for(flags: list[dict[str, str]]) -> tuple[str, int, str]:
    score = min(100, sum(SEVERITY_POINTS[f["severity"]] for f in flags))
    if any(f["severity"] == "critical" for f in flags) or score >= 50:
        return "avoid", score, "do_not_transact"
    if score >= 20:
        return "caution", score, "small_amounts_only"
    return "ok", score, "proceed"


async def check_token(client: httpx.AsyncClient, address: str, chain: str | None = None) -> dict[str, Any]:
    chain = normalize_chain(chain)
    address = validate_address(address, chain)
    cache_key = f"axg2:token:{chain}:{address.lower() if chain != 'solana' else address}"
    cached = await store.get_json(cache_key)
    if cached is not None:
        return cached

    report, simulation = await asyncio.gather(_goplus(client, address, chain), _simulate(client, address, chain))
    if not report:
        raise TokenError("No token data for this address on this chain. Check the address and chain. You were not charged.", 404, "unknown_token")

    flags = _Flags()
    facts = _solana_facts(report, flags) if chain == "solana" else _evm_facts(report, flags)
    sources = ["GoPlus Security token analysis"]
    if simulation and simulation["simulation_success"]:
        sources.append("honeypot.is buy/sell simulation")
        if simulation["is_honeypot"] and not any(f["code"] == "honeypot" for f in flags.items):
            flags.add("honeypot", "critical", "a buy/sell simulation could not sell the token")
        for kind in ("buy", "sell"):
            tax = simulation[f"{kind}_tax"]
            if tax is not None and (facts.get(f"{kind}_tax") is None or tax > (facts.get(f"{kind}_tax") or 0) + 0.01):
                _tax_flags(flags, f"simulated_{kind}", tax)

    verdict, score, action = verdict_for(flags.items)
    result = {
        "address": address,
        "chain": chain,
        "name": facts.pop("name"),
        "symbol": facts.pop("symbol"),
        "verdict": verdict,
        "risk_score": score,
        "agent_action": action,
        "flags": sorted(flags.items, key=lambda f: -SEVERITY_POINTS[f["severity"]]),
        "facts": facts,
        "simulation": simulation,
        "sources": sources,
        "attribution": "Powered by GoPlus Security; simulation by honeypot.is where available.",
        "checked_at": int(time.time()),
        "disclaimer": "Automated risk signals, not financial advice.",
    }
    await store.set_json(cache_key, result, CACHE_TTL_SECONDS)
    return result
