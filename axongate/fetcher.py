"""SSRF-safe page fetching and HTML-to-Markdown extraction.

Pages are fetched directly and extracted locally (no supplier cost). Jina Reader
is used only as a fallback for pages that render client-side or block us.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import re
import socket
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from . import config

try:
    import trafilatura
except ImportError:  # pragma: no cover
    trafilatura = None

TEXT_TYPES = ("text/plain", "text/markdown", "application/json", "text/csv", "application/xml", "text/xml")
HTML_TYPES = ("text/html", "application/xhtml+xml")
MIN_USEFUL_CHARS = 80


class FetchError(Exception):
    """A target could not be fetched. `status` is the HTTP status AxonGate returns."""

    def __init__(self, message: str, status: int = 422, code: str = "fetch_failed") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass
class Page:
    url: str
    final_url: str
    title: str
    markdown: str
    content_type: str
    status_code: int
    extractor: str
    fetched_at: int
    elapsed_ms: int
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.markdown.encode("utf-8")).hexdigest()

    @property
    def word_count(self) -> int:
        return len(re.findall(r"\w+", self.markdown))


def _is_public_ip(value: str) -> bool:
    ip = ipaddress.ip_address(value)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


async def validate_target(url: str) -> str:
    """Normalize a URL and reject anything that resolves to a non-public address."""
    url = (url or "").strip()
    if not url:
        raise FetchError("url is required", 400, "invalid_url")
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise FetchError("Only absolute http(s) URLs are supported", 400, "invalid_url")
    if parts.username or parts.password:
        raise FetchError("URLs with credentials are not allowed", 400, "invalid_url")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if port not in config.ALLOWED_PORTS:
        raise FetchError(f"Port {port} is not allowed", 400, "invalid_url")
    host = parts.hostname
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise FetchError(f"Could not resolve host {host}", 422, "dns_failed") from exc
    addresses = {info[4][0] for info in infos}
    if not addresses or not all(_is_public_ip(a) for a in addresses):
        raise FetchError("Target resolves to a private or reserved address", 400, "blocked_target")
    return url


def _decode(body: bytes, response: httpx.Response) -> str:
    encoding = response.encoding or "utf-8"
    try:
        return body.decode(encoding, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def _title_from_html(html: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    if not match:
        return ""
    return re.sub(r"\s+", " ", match.group(1)).strip()[:300]


def html_to_markdown(html: str, url: str) -> tuple[str, str, dict[str, Any]]:
    """Return (title, markdown, metadata) using trafilatura, with a crude fallback."""
    title = _title_from_html(html)
    meta: dict[str, Any] = {}
    if trafilatura is not None:
        markdown = trafilatura.extract(
            html,
            url=url,
            output_format="markdown",
            include_links=True,
            include_tables=True,
            include_comments=False,
            favor_recall=True,
        ) or ""
        try:
            info = trafilatura.extract_metadata(html, default_url=url)
            if info is not None:
                title = info.title or title
                meta = {k: v for k, v in {"author": info.author, "date": info.date, "site_name": info.sitename, "description": info.description}.items() if v}
        except Exception:
            pass
        if markdown:
            return title, markdown.strip(), meta
    text = re.sub(r"(?is)<(script|style|noscript|svg|template)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return title, text.strip(), meta


async def _direct_fetch(client: httpx.AsyncClient, url: str) -> Page:
    started = time.monotonic()
    current = url
    for _ in range(config.FETCH_MAX_REDIRECTS + 1):
        current = await validate_target(current)
        async with client.stream("GET", current) as response:
            if response.is_redirect and response.headers.get("location"):
                current = urljoin(current, response.headers["location"])
                continue
            content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
            if content_type and not content_type.startswith(HTML_TYPES + TEXT_TYPES):
                raise FetchError(f"Unsupported content type {content_type}", 415, "unsupported_content")
            declared = int(response.headers.get("content-length") or 0)
            if declared > config.FETCH_MAX_BYTES:
                raise FetchError("Page is larger than the 5 MB limit", 413, "too_large")
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > config.FETCH_MAX_BYTES:
                    raise FetchError("Page is larger than the 5 MB limit", 413, "too_large")
                chunks.append(chunk)
            body = _decode(b"".join(chunks), response)
            status = response.status_code
        if status >= 400:
            raise FetchError(f"Target returned HTTP {status}", 424, "target_error")
        if content_type.startswith(TEXT_TYPES):
            title, markdown, meta = "", body.strip(), {}
        else:
            title, markdown, meta = await asyncio.to_thread(html_to_markdown, body, current)
        return Page(
            url=url,
            final_url=current,
            title=title,
            markdown=markdown,
            content_type=content_type or "text/html",
            status_code=status,
            extractor="direct",
            fetched_at=int(time.time()),
            elapsed_ms=int((time.monotonic() - started) * 1000),
            meta=meta,
        )
    raise FetchError("Too many redirects", 424, "too_many_redirects")


async def _jina_fetch(client: httpx.AsyncClient, url: str) -> Page:
    started = time.monotonic()
    headers = {"Accept": "application/json", "X-Return-Format": "markdown", "X-Retain-Images": "none"}
    if config.JINA_API_KEY:
        headers["Authorization"] = f"Bearer {config.JINA_API_KEY}"
    response = await client.get(f"{config.JINA_READER_BASE_URL}/{url}", headers=headers, timeout=config.JINA_TIMEOUT_SECONDS)
    if response.status_code >= 400:
        raise FetchError(f"Reader fallback returned HTTP {response.status_code}", 424, "target_error")
    data = (response.json() or {}).get("data") or {}
    return Page(
        url=url,
        final_url=data.get("url") or url,
        title=(data.get("title") or "")[:300],
        markdown=(data.get("content") or "").strip(),
        content_type="text/html",
        status_code=200,
        extractor="rendered",
        fetched_at=int(time.time()),
        elapsed_ms=int((time.monotonic() - started) * 1000),
        meta={k: v for k, v in {"description": data.get("description"), "date": data.get("publishedTime")}.items() if v},
    )


def new_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(config.FETCH_TIMEOUT_SECONDS, connect=5.0),
        follow_redirects=False,
        headers={
            "User-Agent": config.USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
            "Accept-Language": "en;q=0.9,*;q=0.5",
        },
        limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
    )


async def fetch_page(client: httpx.AsyncClient, url: str, allow_render: bool = True) -> Page:
    """Fetch and extract a page, falling back to rendered reading when the result is thin."""
    url = await validate_target(url)
    direct_error: FetchError | None = None
    page: Page | None = None
    try:
        page = await _direct_fetch(client, url)
    except FetchError as exc:
        if exc.code in {"invalid_url", "blocked_target", "too_large", "unsupported_content", "dns_failed"}:
            raise
        direct_error = exc
    except httpx.HTTPError as exc:
        direct_error = FetchError(f"Could not fetch target: {type(exc).__name__}", 424, "target_unreachable")

    if page is not None and len(page.markdown) >= MIN_USEFUL_CHARS:
        return page
    if allow_render:
        try:
            rendered = await _jina_fetch(client, url)
            if len(rendered.markdown) > len(page.markdown if page else ""):
                return rendered
        except (FetchError, httpx.HTTPError, ValueError):
            pass
    if page is not None and page.markdown:
        return page
    raise direct_error or FetchError("Page had no readable content", 422, "empty_content")
