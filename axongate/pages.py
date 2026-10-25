"""Human-facing HTML pages, rendered from the product catalog so prices never drift."""

from __future__ import annotations

import html
import json
from functools import lru_cache

from . import config, discovery, payments
from .config import PRODUCTS

ICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" fill="#0f172a"/>'
    '<path d="M18 46 32 16l14 30" fill="none" stroke="#5eead4" stroke-width="6" stroke-linecap="round" stroke-linejoin="round"/>'
    '<path d="M23 36h18" stroke="#5eead4" stroke-width="6" stroke-linecap="round"/></svg>'
)

STYLE = """
:root{--bg:#f8fafc;--panel:#ffffff;--ink:#0f172a;--muted:#475569;--line:#e2e8f0;--accent:#0f766e;--accent-soft:#ccfbf1;--code:#0f172a;--code-ink:#e2e8f0;--warn:#b45309}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0b1120;--panel:#111827;--ink:#e5e7eb;--muted:#94a3b8;--line:#1f2937;--accent:#5eead4;--accent-soft:#134e4a;--code:#020617;--code-ink:#e2e8f0;--warn:#fbbf24}}
:root[data-theme="dark"]{--bg:#0b1120;--panel:#111827;--ink:#e5e7eb;--muted:#94a3b8;--line:#1f2937;--accent:#5eead4;--accent-soft:#134e4a;--code:#020617;--code-ink:#e2e8f0;--warn:#fbbf24}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
a{color:var(--accent)}main{max-width:1040px;margin:0 auto;padding:0 16px 64px}
header.top{display:flex;align-items:center;justify-content:space-between;max-width:1040px;margin:0 auto;padding:18px 16px}
.brand{display:flex;gap:10px;align-items:center;font-weight:700;text-decoration:none;color:var(--ink)}.brand svg{width:28px;height:28px}
nav a{margin-left:18px;text-decoration:none;color:var(--muted);font-size:15px}nav a:hover{color:var(--ink)}
.hero{padding:48px 0 28px}.eyebrow{display:inline-block;background:var(--accent-soft);color:var(--accent);border-radius:999px;padding:4px 12px;font-size:13px;font-weight:600}
h1{font-size:clamp(32px,6vw,52px);line-height:1.1;margin:16px 0 14px;letter-spacing:-.02em}
.lede{font-size:clamp(17px,2.4vw,20px);color:var(--muted);max-width:720px}
.cta{display:flex;flex-wrap:wrap;gap:12px;margin-top:24px}.btn{display:inline-block;padding:10px 18px;border-radius:10px;text-decoration:none;font-weight:600;border:1px solid var(--line);color:var(--ink);background:var(--panel)}
.btn.primary{background:var(--accent);border-color:var(--accent);color:var(--bg)}
section{margin-top:44px}h2{font-size:24px;margin:0 0 14px;letter-spacing:-.01em}h3{margin:0 0 6px;font-size:18px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px}
.card p{margin:6px 0 0;color:var(--muted);font-size:15px}.price{font-weight:700;color:var(--accent);font-variant-numeric:tabular-nums}
.path{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px;color:var(--muted)}
pre{background:var(--code);color:var(--code-ink);border-radius:12px;padding:16px;overflow-x:auto;font-size:13.5px;line-height:1.5;margin:10px 0}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden;font-size:15px}
th,td{text-align:left;padding:10px 12px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-weight:600;font-size:13px;text-transform:uppercase;letter-spacing:.04em}
.table-wrap{overflow-x:auto}.muted{color:var(--muted)}
.points{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px}
footer{max-width:1040px;margin:0 auto;padding:24px 16px 40px;color:var(--muted);font-size:14px;border-top:1px solid var(--line)}
.endpoint{border-top:1px solid var(--line);padding-top:28px;margin-top:28px}
@media (max-width:560px){nav a{margin-left:12px;font-size:14px}.hero{padding-top:28px}}
"""


def _layout(title: str, description: str, body: str) -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><meta name="description" content="{html.escape(description)}">
<link rel="icon" href="/icon.svg" type="image/svg+xml"><link rel="alternate" type="text/plain" href="/llms.txt" title="llms.txt">
<style>{STYLE}</style></head><body>
<header class="top"><a class="brand" href="/">{ICON_SVG}<span>AxonGate</span></a>
<nav><a href="/docs">Docs</a><a href="/v1/pricing">Pricing</a><a href="/.well-known/x402">Catalog</a><a href="{html.escape(config.GITHUB_REPO_URL)}">GitHub</a></nav></header>
<main>{body}</main>
<footer>AxonGate v{config.VERSION} · Payments in USDC on {config.NETWORK_LABELS} via x402 · payTo <code>{config.PAY_TO}</code> · <a href="mailto:{config.CONTACT_EMAIL}">{config.CONTACT_EMAIL}</a></footer>
</body></html>"""


def _price(p) -> str:
    return f"${p.price.normalize():f}"


@lru_cache(maxsize=1)
def home() -> str:
    cards = "".join(
        f'<div class="card"><h3>{html.escape(p.title)} <span class="price">{_price(p)}</span></h3>'
        f'<div class="path">{"/".join(payments.product_methods(p))} {p.path}</div><p>{html.escape(p.summary)}</p></div>'
        for p in PRODUCTS.values()
    )
    base = config.PUBLIC_BASE_URL
    body = f"""
<div class="hero"><span class="eyebrow">x402 · USDC on {config.NETWORK_LABELS} · no API keys</span>
<h1>Safe web access for AI agents.</h1>
<p class="lede">Read any page as clean Markdown, search the live web, get grounded answers and fact-check claims — with
prompt-injection screening on every page and every quote checked verbatim against its source. Pay per call; you're only charged when it works.</p>
<div class="cta"><a class="btn primary" href="/docs">Read the docs</a><a class="btn" href="/.well-known/x402">x402 catalog</a><a class="btn" href="/llms.txt">llms.txt</a></div></div>

<section><h2>Endpoints</h2><div class="grid">{cards}</div></section>

<section><h2>Try it in one call</h2>
<pre><code># Any x402 client works. With Node:
npm i @x402/fetch @x402/evm viem

import {{ wrapFetchWithPayment, x402Client }} from "@x402/fetch";
import {{ registerExactEvmScheme }} from "@x402/evm/exact/client";
import {{ privateKeyToAccount }} from "viem/accounts";

const client = new x402Client();
registerExactEvmScheme(client, {{ signer: privateKeyToAccount(process.env.PRIVATE_KEY) }});
const pay = wrapFetchWithPayment(fetch, client);

const res = await pay("{base}/v1/verify", {{
  method: "POST", headers: {{ "content-type": "application/json" }},
  body: JSON.stringify({{ claim: "HTTP status code 402 is reserved for future use." }}),
}});
console.log(await res.json()); // verdict, confidence, verbatim-checked evidence</code></pre>
<p class="muted">Without payment, every endpoint answers <code>402 Payment Required</code> with machine-readable requirements in the <code>PAYMENT-REQUIRED</code> header.</p></section>

<section><h2>Why agents use AxonGate</h2><div class="points">
<div class="card"><h3>Injection-screened reads</h3><p>Every page is scanned for instruction overrides, role hijacks, secret-exfiltration lures, hidden Unicode and data-leaking image links before your model sees it.</p></div>
<div class="card"><h3>Quotes that actually exist</h3><p>Answers and verdicts cite quotes that are matched verbatim against the fetched page. Anything the model can't back up is dropped, and verdicts without proof are downgraded.</p></div>
<div class="card"><h3>Pay only for success</h3><p>Settlement happens after the response. Timeouts, blocked targets and supplier errors return 4xx/5xx and are never charged.</p></div>
<div class="card"><h3>No account, no key</h3><p>Discover, pay and call in one request cycle. Works with any x402 v2 client and appears in x402 directories.</p></div>
</div></section>
"""
    return _layout("AxonGate — safe web access for AI agents", discovery.DESCRIPTION, body)


def _example_curl(p) -> str:
    _, _, example = payments.INPUTS[p.key]
    url = f"{config.PUBLIC_BASE_URL}{p.path}"
    if payments.discovery_method(p) == "GET":
        query = "&".join(f"{k}={html.escape(str(v)).replace(' ', '+')}" for k, v in example.items())
        return f"curl -i \"{url}?{query}\""
    return f"curl -i -X POST {url} \\\n  -H 'content-type: application/json' \\\n  -d '{html.escape(json.dumps(example))}'"


@lru_cache(maxsize=1)
def docs() -> str:
    rows = "".join(
        f"<tr><td><b>{html.escape(p.title)}</b></td><td><code>{'/'.join(payments.product_methods(p))} {p.path}</code></td><td class=\"price\">{_price(p)}</td></tr>"
        for p in PRODUCTS.values()
    )
    sections = []
    for p in PRODUCTS.values():
        properties, required, _ = payments.INPUTS[p.key]
        params = "".join(
            f"<tr><td><code>{name}</code>{' <span class=\"muted\">(required)</span>' if name in required else ''}</td><td>{html.escape(spec.get('description', ''))}</td></tr>"
            for name, spec in properties.items()
        )
        example_out = html.escape(json.dumps(payments.OUTPUT_EXAMPLES[p.key], indent=2))
        sections.append(f"""<div class="endpoint" id="{p.key}"><h2>{html.escape(p.title)} <span class="price">{_price(p)}</span></h2>
<p class="path">{'/'.join(payments.product_methods(p))} {config.PUBLIC_BASE_URL}{p.path}</p><p>{html.escape(p.summary)}</p>
<div class="table-wrap"><table><tr><th>Parameter</th><th>Description</th></tr>{params}</table></div>
<pre><code>{_example_curl(p)}</code></pre><p class="muted">Response (abridged):</p><pre><code>{example_out}</code></pre></div>""")
    body = f"""
<div class="hero" style="padding-bottom:8px"><span class="eyebrow">Docs</span><h1>AxonGate API</h1>
<p class="lede">Five endpoints, one payment flow. GET endpoints take query parameters; POST endpoints take a JSON body.</p></div>
<section><div class="table-wrap"><table><tr><th>Endpoint</th><th>Route</th><th>Price</th></tr>{rows}</table></div></section>
<section><h2>Payment flow</h2><ol>
<li>Call the endpoint. Without payment you receive <code>402</code> with base64 JSON requirements in <code>PAYMENT-REQUIRED</code> (scheme <code>exact</code>, one entry per network: {', '.join(f'{r.label} <code>{r.network}</code>' for r in payments.rails())}; on EVM networks payTo is <code>{config.PAY_TO}</code>).</li>
<li>Sign an EIP-3009 USDC authorization with an x402 client and retry with <code>PAYMENT-SIGNATURE</code>.</li>
<li>On success you get the result plus a <code>PAYMENT-RESPONSE</code> header with the settlement transaction. Errors are not settled.</li></ol>
<p>Errors use <code>{{"error": code, "message": text}}</code>: <code>400 invalid_url / invalid_request</code>, <code>413 too_large</code>, <code>415 unsupported_content</code>, <code>424 target_error / no_sources</code>, <code>429 rate_limited</code>, <code>503 search_unavailable / reasoning_unavailable</code>, <code>504 timeout</code>.</p></section>
{''.join(sections)}
<section class="endpoint"><h2>Legacy URLs</h2><p><code>/v1/x402/access</code> and <code>/v1/x402/starter</code> now serve Safe Read (they accept <code>target_url</code>). <code>/v1/x402/proof-pack</code> now serves Claim Verify (<code>question</code> + <code>target_url</code>). Called with no input, a legacy URL runs the documented example (Safe Read of the example URL; Claim Verify of the example claim) instead of failing. The v1 Proof Pack, bundle and quote endpoints are retired.</p></section>
<section><h2>Machine-readable</h2><p><a href="/openapi.json">OpenAPI</a> · <a href="/.well-known/x402">/.well-known/x402</a> · <a href="/llms.txt">llms.txt</a> · <a href="/.well-known/agent.json">agent card</a> · <a href="/v1/pricing">pricing JSON</a></p></section>
"""
    return _layout("AxonGate API docs", "Endpoints, parameters, prices and the x402 payment flow for AxonGate.", body)


def bot() -> str:
    body = f"""<div class="hero"><h1>AxonGateBot</h1><p class="lede">AxonGateBot fetches public pages only when an AxonGate customer asks to read,
answer from or verify a specific URL. It sends <code>{html.escape(config.USER_AGENT)}</code>, does not crawl, and never accesses private networks.
To ask us to stop fetching your site, email <a href="mailto:{config.CONTACT_EMAIL}">{config.CONTACT_EMAIL}</a>.</p></div>"""
    return _layout("AxonGateBot", "About the AxonGate page fetcher.", body)
