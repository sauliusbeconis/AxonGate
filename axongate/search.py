"""Live web search through Jina Search (s.jina.ai)."""

from __future__ import annotations

import hashlib
from urllib.parse import urlsplit

import httpx

from . import config
from .store import store


class SearchError(Exception):
    def __init__(self, message: str, status: int = 503) -> None:
        super().__init__(message)
        self.status = status


def _domain(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


async def web_search(client: httpx.AsyncClient, query: str, limit: int = 8) -> list[dict]:
    query = (query or "").strip()
    if not query:
        raise SearchError("query is required", 400)
    if not config.JINA_API_KEY:
        raise SearchError("Search supplier is not configured", 503)
    limit = max(1, min(limit, 10))
    cache_key = "axg2:search:" + hashlib.sha256(f"{query.lower()}|{limit}".encode()).hexdigest()[:32]
    cached = await store.get_json(cache_key)
    if cached is not None:
        return cached

    try:
        response = await client.get(
            f"{config.JINA_SEARCH_BASE_URL}/",
            params={"q": query},
            headers={
                "Authorization": f"Bearer {config.JINA_API_KEY}",
                "Accept": "application/json",
                "X-Respond-With": "no-content",
            },
            timeout=config.JINA_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise SearchError(f"Search supplier unreachable: {type(exc).__name__}") from exc
    if response.status_code >= 400:
        raise SearchError(f"Search supplier returned HTTP {response.status_code}")

    results: list[dict] = []
    seen: set[str] = set()
    for item in (response.json() or {}).get("data") or []:
        url = item.get("url") or ""
        if not url.startswith(("http://", "https://")) or url in seen:
            continue
        seen.add(url)
        results.append({
            "rank": len(results) + 1,
            "title": (item.get("title") or "").strip()[:300],
            "url": url,
            "domain": _domain(url),
            "snippet": (item.get("description") or "").strip()[:600],
            "published": item.get("date") or None,
        })
        if len(results) >= limit:
            break
    await store.set_json(cache_key, results, ttl=min(config.CACHE_TTL_SECONDS, 1800))
    return results
