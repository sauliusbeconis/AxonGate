"""AxonGate v2: safe web access for AI agents, paid per call over x402."""

from __future__ import annotations

import asyncio
import hmac
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response

from . import config, discovery, mcp_server, pages, payments, research, safety
from .config import PRODUCTS
from .fetcher import FetchError, fetch_page, new_client
from .llm import LLMError
from .search import SearchError, web_search
from .store import store
from .telemetry import TelemetryMiddleware
from .tokens import TokenError, check_token

_paid_slots = asyncio.Semaphore(config.PAID_CONCURRENCY)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http = new_client()
    yield
    await app.state.http.aclose()


app = FastAPI(
    title="AxonGate",
    version=config.VERSION,
    summary="Safe web access for AI agents: read, search, answer and verify, paid per call in USDC over x402.",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)


def error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": code, "message": message}, status_code=status)


async def read_params(request: Request) -> dict[str, Any]:
    params: dict[str, Any] = dict(request.query_params)
    if request.method == "POST":
        try:
            body = await request.json()
        except Exception:
            body = None
        if isinstance(body, dict):
            params.update(body)
    fields, default = getattr(request.state, "legacy_default", ((), None))
    if default and not any(params.get(f) for f in fields):
        params.update(default)
    return params


def _int(value: Any, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def _bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _urls(value: Any) -> list[str] | None:
    if not value:
        return None
    if isinstance(value, str):
        value = [u for u in value.split(",") if u.strip()]
    if not isinstance(value, list):
        return None
    return [str(u).strip() for u in value if str(u).strip()][:5] or None


async def _guarded(product: str, work):
    """Run paid work under the concurrency cap and map failures to non-billable errors."""
    try:
        async with _paid_slots:
            result = await work()
    except FetchError as exc:
        return error(exc.status, exc.code, str(exc))
    except SearchError as exc:
        return error(exc.status, "search_unavailable", str(exc))
    except TokenError as exc:
        return error(exc.status, exc.code, str(exc))
    except LLMError:
        return error(503, "reasoning_unavailable", "The reasoning model is temporarily unavailable. You were not charged.")
    except asyncio.TimeoutError:
        return error(504, "timeout", "The request took too long. You were not charged.")
    cost = result.pop("_cost_usd", 0.0) if isinstance(result, dict) else 0.0
    if cost:
        await store.incr("supplier_cost_micro_usd", product, int(cost * 1_000_000))
    return JSONResponse(result)


# ---------------------------------------------------------------- paid products


async def do_read(request: Request):
    p = await read_params(request)
    url = p.get("url") or p.get("target_url") or ""
    max_chars = _int(p.get("max_chars"), 60000, 500, 200000)
    render = _bool(p.get("render"), True)

    async def work():
        page = await asyncio.wait_for(
            research.cached_page(request.app.state.http, url) if render else fetch_page(request.app.state.http, url, allow_render=False),
            25,
        )
        scan = safety.scan_text(page.markdown)
        markdown = page.markdown
        truncated = len(markdown) > max_chars
        return {
            "url": page.url,
            "final_url": page.final_url,
            "title": page.title,
            "markdown": markdown[:max_chars],
            "truncated": truncated,
            "word_count": page.word_count,
            "content_sha256": page.content_sha256,
            "metadata": page.meta,
            "injection_risk": safety.summarize(scan),
            "extractor": page.extractor,
            "fetched_at": page.fetched_at,
            "_cost_usd": 0.0003 if page.extractor == "rendered" else 0.0,
        }

    return await _guarded("read", work)


async def do_search(request: Request):
    p = await read_params(request)
    query = str(p.get("q") or p.get("query") or "").strip()[:400]
    limit = _int(p.get("limit"), 8, 1, 10)
    read_top = _int(p.get("read_top"), 0, 0, 3)

    async def work():
        client = request.app.state.http
        results = await web_search(client, query, limit)
        if read_top:
            async def enrich(result):
                try:
                    page = await asyncio.wait_for(research.cached_page(client, result["url"]), 15)
                    result["markdown"] = page.markdown[:20000]
                    result["injection_risk"] = safety.summarize(safety.scan_text(page.markdown))
                except Exception as exc:  # one bad page must not fail the search
                    result["read_error"] = str(exc) or type(exc).__name__
            await asyncio.gather(*(enrich(r) for r in results[:read_top]))
        return {"query": query, "results": results, "_cost_usd": research.SEARCH_USD_PER_CALL}

    return await _guarded("search", work)


async def do_answer(request: Request):
    p = await read_params(request)
    question = str(p.get("question") or p.get("q") or "").strip()[:1000]
    if not question:
        return error(400, "invalid_request", "question is required")
    urls = _urls(p.get("urls") or p.get("target_url"))
    max_sources = _int(p.get("max_sources"), 4, 1, 5)
    return await _guarded("answer", lambda: asyncio.wait_for(research.answer_question(request.app.state.http, question, urls, max_sources), 55))


async def do_verify(request: Request):
    p = await read_params(request)
    claim = str(p.get("claim") or p.get("question") or "").strip()[:1000]
    if not claim:
        return error(400, "invalid_request", "claim is required")
    urls = _urls(p.get("urls") or p.get("target_url"))
    max_sources = _int(p.get("max_sources"), 4, 1, 5)
    return await _guarded("verify", lambda: asyncio.wait_for(research.verify_claim(request.app.state.http, claim, urls, max_sources), 55))


async def do_scan(request: Request):
    p = await read_params(request)
    text = p.get("text")
    if not isinstance(text, str) or not text:
        return error(400, "invalid_request", "text is required")
    if len(text) > 200000:
        return error(413, "too_large", "text is limited to 200000 characters")
    return JSONResponse(safety.scan_text(text))


async def do_token(request: Request):
    p = await read_params(request)
    address = str(p.get("address") or p.get("token") or p.get("contract") or "").strip()
    if not address:
        return error(400, "invalid_request", "address is required")
    chain = str(p.get("chain") or p.get("network") or "base")
    return await _guarded("token", lambda: asyncio.wait_for(check_token(request.app.state.http, address, chain), 30))


BATCH_MAX_URLS = 20


async def do_batch(request: Request):
    p = await read_params(request)
    raw = p.get("urls")
    if isinstance(raw, str):
        raw = [u for u in raw.split(",") if u.strip()]
    if not isinstance(raw, list) or not raw:
        return error(400, "invalid_request", "urls must be a list of 1-20 URLs")
    urls = list(dict.fromkeys(str(u).strip() for u in raw if str(u).strip()))
    if len(urls) > BATCH_MAX_URLS:
        return error(400, "invalid_request", f"urls is limited to {BATCH_MAX_URLS} per call")
    max_chars = _int(p.get("max_chars"), 20000, 500, 60000)
    render = _bool(p.get("render"), True)
    client = request.app.state.http
    slots = asyncio.Semaphore(8)

    async def one(url: str) -> dict[str, Any]:
        try:
            async with slots:
                page = await asyncio.wait_for(research.cached_page(client, url) if render else fetch_page(client, url, allow_render=False), 25)
        except FetchError as exc:
            return {"url": url, "ok": False, "error": exc.code, "message": str(exc)}
        except asyncio.TimeoutError:
            return {"url": url, "ok": False, "error": "timeout", "message": "The page took too long to load."}
        return {
            "url": page.url,
            "ok": True,
            "final_url": page.final_url,
            "title": page.title,
            "markdown": page.markdown[:max_chars],
            "truncated": len(page.markdown) > max_chars,
            "word_count": page.word_count,
            "content_sha256": page.content_sha256,
            "injection_risk": safety.summarize(safety.scan_text(page.markdown)),
            "extractor": page.extractor,
        }

    async def work():
        results = await asyncio.gather(*(one(u) for u in urls))
        good = sum(1 for r in results if r["ok"])
        if not good:
            # Nothing usable: fail the whole call so the buyer is not charged.
            raise FetchError("None of the URLs could be read. You were not charged.", 502, "all_failed")
        rendered = sum(1 for r in results if r.get("extractor") == "rendered")
        return {"count": len(results), "ok": good, "failed": len(results) - good, "results": results, "_cost_usd": 0.0003 * rendered}

    return await _guarded("batch", work)


HANDLERS = {"read": do_read, "search": do_search, "answer": do_answer, "verify": do_verify, "scan": do_scan, "token": do_token, "batch": do_batch}

for _product in PRODUCTS.values():
    for _method in payments.product_methods(_product):
        app.add_api_route(
            _product.path,
            HANDLERS[_product.key],
            methods=[_method],
            operation_id=f"{_product.key}_{_method.lower()}",
            tags=["paid"],
            summary=f"{_product.title} (${_product.price} USDC)",
            description=_product.summary,
        )

# Directory probes pay legacy URLs with no input at all. Rather than return a non-billable 400,
# those calls run the documented example input (listed as the default in /docs).
LEGACY_DEFAULTS = {
    "read": (("url", "target_url"), payments.INPUTS["read"][2]),
    "verify": (("claim", "question"), payments.INPUTS["verify"][2]),
}


def _legacy_handler(key: str):
    handler = HANDLERS[key]

    async def run(request: Request):
        request.state.legacy_default = LEGACY_DEFAULTS.get(key, ((), None))
        return await handler(request)

    return run


for _legacy, _key in config.LEGACY_ALIASES.items():
    for _path in [_legacy] + [f"/from/{s}{_legacy}" for s in config.SOURCE_ALIAS_PATHS]:
        app.add_api_route(_path, _legacy_handler(_key), methods=["GET", "POST"], include_in_schema=False)


# ---------------------------------------------------------------- discovery


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def home():
    return HTMLResponse(pages.home(), headers={"Cache-Control": "public, max-age=300"})


@app.get("/docs", response_class=HTMLResponse, include_in_schema=False)
async def docs():
    return HTMLResponse(pages.docs(), headers={"Cache-Control": "public, max-age=300"})


@app.get("/llms.txt", response_class=PlainTextResponse, include_in_schema=False)
async def llms_txt():
    return PlainTextResponse(discovery.llms_txt(), headers={"Cache-Control": "public, max-age=300"})


MCP_CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
    "Access-Control-Allow-Headers": "content-type, accept, authorization, mcp-protocol-version, mcp-session-id",
    "Access-Control-Expose-Headers": "mcp-session-id",
}


@app.api_route("/mcp", methods=["GET", "POST", "DELETE", "OPTIONS"], include_in_schema=False)
async def mcp_endpoint(request: Request):
    if request.method == "OPTIONS":
        return Response(status_code=204, headers=MCP_CORS)
    response = await mcp_server.handle(request, HANDLERS)
    response.headers.update(MCP_CORS)
    return response


@app.get("/.well-known/mcp/server-card.json", include_in_schema=False)
@app.get("/.well-known/mcp.json", include_in_schema=False)
async def mcp_server_card():
    return JSONResponse(mcp_server.server_card(), headers={"Cache-Control": "public, max-age=300", "Access-Control-Allow-Origin": "*"})


@app.get("/.well-known/x402", include_in_schema=False)
@app.get("/.well-known/x402.json", include_in_schema=False)
async def well_known_x402():
    return JSONResponse(discovery.x402_document(), headers={"Cache-Control": "public, max-age=300"})


@app.get("/discovery/resources", include_in_schema=False)
@app.get("/x402/discovery/resources", include_in_schema=False)
@app.get("/v2/x402/discovery/resources", include_in_schema=False)
async def discovery_resources():
    return JSONResponse(discovery.resources_document(), headers={"Cache-Control": "public, max-age=300"})


@app.get("/manifest.json", include_in_schema=False)
@app.get("/agent_manifest.json", include_in_schema=False)
async def manifest():
    return JSONResponse(discovery.manifest(), headers={"Cache-Control": "public, max-age=300"})


@app.get("/.well-known/agent.json", include_in_schema=False)
@app.get("/.well-known/agent-card.json", include_in_schema=False)
async def agent_card():
    return JSONResponse(discovery.agent_card(), headers={"Cache-Control": "public, max-age=300"})


@app.get("/pricing", include_in_schema=False)
@app.get("/v1/pricing", tags=["free"], summary="Machine-readable price list")
async def pricing():
    return JSONResponse(discovery.pricing(), headers={"Cache-Control": "public, max-age=300"})


@app.get("/.well-known/x402list.txt", response_class=PlainTextResponse, include_in_schema=False)
async def x402list_proof():
    if not config.X402LIST_TOKEN:
        return PlainTextResponse("", status_code=404)
    return PlainTextResponse(config.X402LIST_TOKEN + "\n", headers={"Cache-Control": "no-store"})


@app.get("/robots.txt", response_class=PlainTextResponse, include_in_schema=False)
async def robots():
    return PlainTextResponse(f"User-agent: *\nAllow: /\nSitemap: {config.PUBLIC_BASE_URL}/sitemap.xml\n")


@app.get("/sitemap.xml", include_in_schema=False)
async def sitemap():
    urls = ["/", "/docs", "/llms.txt", "/.well-known/x402", "/openapi.json"]
    body = "".join(f"<url><loc>{config.PUBLIC_BASE_URL}{u}</loc></url>" for u in urls)
    return Response(f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</urlset>', media_type="application/xml")


@app.get("/bot", response_class=HTMLResponse, include_in_schema=False)
async def bot_page():
    return HTMLResponse(pages.bot())


_ICON_PNG = (Path(__file__).parent / "icon.png").read_bytes()


@app.get("/favicon.ico", include_in_schema=False)
@app.get("/icon.png", include_in_schema=False)
async def icon_png():
    return Response(_ICON_PNG, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/icon.svg", include_in_schema=False)
async def icon_svg():
    return Response(pages.ICON_SVG, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/health", include_in_schema=False)
async def health():
    return {"status": "alive", "service": "axongate", "version": config.VERSION}


# ---------------------------------------------------------------- operator


def _operator_ok(request: Request) -> bool:
    if not config.OPERATOR_TOKEN:
        return False
    supplied = request.headers.get("x-axongate-operator-token") or request.query_params.get("operator_token") or ""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        supplied = supplied or auth[7:]
    return hmac.compare_digest(supplied.encode(), config.OPERATOR_TOKEN.encode())


@app.get("/metrics", include_in_schema=False)
async def metrics(request: Request):
    if not _operator_ok(request):
        return error(401, "unauthorized", "operator token required")
    buckets = ["http", "challenges", "challenge_paths", "paid_attempts", "settled", "settled_networks", "revenue_micro_usdc", "supplier_cost_micro_usd", "paid_failures", "mcp", "clients"]
    data = {b: await store.read_counters(b) for b in buckets}
    total_rev = sum(data["revenue_micro_usdc"].get("total", {}).values()) / 1e6
    total_cost = sum(data["supplier_cost_micro_usd"].get("total", {}).values()) / 1e6
    top_clients = sorted(data["clients"].get("total", {}).items(), key=lambda kv: -kv[1])[:25]
    return {
        "version": config.VERSION,
        "store": store.backend,
        "redis_ok": await store.ping(),
        "facilitator": payments.facilitator_label(),
        "networks": [r.network for r in payments.rails()],
        "summary": {
            "revenue_usdc": round(total_rev, 6),
            "supplier_cost_usd": round(total_cost, 6),
            "gross_margin_usd": round(total_rev - total_cost, 6),
            "settled_calls": data["settled"].get("total", {}),
            "settled_by_network": data["settled_networks"].get("total", {}),
            "challenges": data["challenges"].get("total", {}),
        },
        "top_clients_on_paid_routes": dict(top_clients),
        "counters": {k: v for k, v in data.items() if k != "clients"},
        "recent_paid_events": await store.read_events("paid_events", 50),
        "generated_at": int(time.time()),
    }


# ---------------------------------------------------------------- retired v1 surface

RETIRED_PREFIXES = (
    "/v1/proof-pack", "/proof-pack", "/v1/quotes", "/checkout", "/v1/checkout", "/quote", "/v1/x402/quote",
    "/v1/access", "/v1/x402/retry", "/v1/broker", "/v1/operator", "/operator", "/v1/agent", "/v1/contact",
    "/contact", "/quickstart", "/paid-test", "/demo", "/about", "/faq", "/from/",
)


@app.api_route("/{path:path}", methods=["GET", "POST", "HEAD", "PUT", "DELETE", "PATCH"], include_in_schema=False)
async def fallback(path: str, request: Request):
    full = "/" + path
    if request.method in {"GET", "HEAD"} and full.startswith(("/about", "/faq", "/quickstart", "/demo", "/paid-test", "/proof-pack", "/contact")):
        return RedirectResponse("/", status_code=301)
    if full.startswith(RETIRED_PREFIXES):
        return JSONResponse(
            {"error": "gone", "message": "This AxonGate v1 endpoint was retired. See the v2 catalog.", "catalog": f"{config.PUBLIC_BASE_URL}/.well-known/x402", "docs": f"{config.PUBLIC_BASE_URL}/docs"},
            status_code=410,
        )
    return error(404, "not_found", "Unknown path. See /docs or /.well-known/x402.")


# Middleware order: the last added runs first. Telemetry must see the final status (after x402).
X402_ACTIVE = payments.install(app)
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(TelemetryMiddleware)
