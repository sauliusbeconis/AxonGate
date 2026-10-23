"""Request accounting and per-IP rate limiting as one pure ASGI middleware."""

from __future__ import annotations

import re
import time

from . import config
from .config import PRODUCTS
from .store import RateLimiter, store

_limiter = RateLimiter(config.RATE_LIMIT_PER_MINUTE)

_PATH_PRODUCT: dict[str, str] = {p.path: p.key for p in PRODUCTS.values()}
for _legacy, _key in config.LEGACY_ALIASES.items():
    _PATH_PRODUCT[_legacy] = _key
    for _source in config.SOURCE_ALIAS_PATHS:
        _PATH_PRODUCT[f"/from/{_source}{_legacy}"] = _key

_BOT_TOKEN = re.compile(r"(?:compatible;\s*)?([A-Za-z][\w.\-]{1,40})/[\d.]+")


def client_family(user_agent: str) -> str:
    if not user_agent:
        return "none"
    match = re.search(r"compatible;\s*([^;/)]+)", user_agent)
    if match:
        return match.group(1).strip()[:40]
    if user_agent.startswith("Mozilla/"):
        return "browser"
    match = _BOT_TOKEN.match(user_agent)
    return match.group(1)[:40] if match else user_agent[:40]


def client_ip(scope) -> str:
    for name, value in scope.get("headers") or []:
        if name == b"x-forwarded-for":
            return value.decode("latin-1").split(",")[0].strip()
        if name == b"x-real-ip":
            return value.decode("latin-1").strip()
    client = scope.get("client")
    return client[0] if client else "unknown"


class TelemetryMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if path == "/health":
            return await self.app(scope, receive, send)

        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers") or []}
        ip = client_ip(scope)
        if not _limiter.allow(ip):
            await store.incr("http", "rate_limited")
            await send({"type": "http.response.start", "status": 429, "headers": [(b"content-type", b"application/json"), (b"retry-after", b"60")]})
            await send({"type": "http.response.body", "body": b'{"error":"rate_limited","retry_after_seconds":60}'})
            return

        product = _PATH_PRODUCT.get(path)
        has_payment = "payment-signature" in headers or "x-payment" in headers
        started = time.monotonic()
        status_holder = {"status": 500, "settled": False}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                status_holder["settled"] = any(k.lower() in (b"payment-response", b"x-payment-response") for k, _ in message.get("headers") or [])
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            status = status_holder["status"]
            try:
                await store.incr("http", f"status_{status // 100}xx")
                if product:
                    family = client_family(headers.get("user-agent", ""))
                    if status == 402 and not has_payment:
                        await store.incr("challenges", product)
                        await store.incr("challenge_paths", path)
                    elif has_payment:
                        await store.incr("paid_attempts", product)
                        if status < 400 and status_holder["settled"]:
                            price = PRODUCTS[product].price
                            await store.incr("settled", product)
                            await store.incr("revenue_micro_usdc", product, int(price * 1_000_000))
                            await store.push_event("paid_events", {
                                "ts": int(time.time()),
                                "product": product,
                                "path": path,
                                "price_usdc": str(price),
                                "client": family,
                                "elapsed_ms": int((time.monotonic() - started) * 1000),
                            })
                        else:
                            await store.incr("paid_failures", f"{product}_{status}")
                    await store.incr("clients", family)
            except Exception:
                pass
