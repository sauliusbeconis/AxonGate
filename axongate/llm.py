"""Minimal OpenAI-compatible chat client returning JSON objects."""

from __future__ import annotations

import json
from typing import Any

import httpx

from . import config


class LLMError(Exception):
    pass


async def complete_json(client: httpx.AsyncClient, system: str, user: str, schema: dict[str, Any], max_tokens: int = 1200) -> tuple[dict[str, Any], dict[str, int]]:
    """Run one structured-output completion. Returns (parsed_json, usage)."""
    if not config.LLM_ENABLED:
        raise LLMError("LLM is not configured")
    payload = {
        "model": config.LLM_MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {"type": "json_schema", "json_schema": {"name": "result", "strict": True, "schema": schema}},
        "max_completion_tokens": max_tokens,
    }
    if config.LLM_MODEL.startswith("gpt-5"):
        payload["reasoning_effort"] = "minimal"
    try:
        response = await client.post(
            f"{config.LLM_BASE_URL}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {config.LLM_API_KEY}"},
            timeout=config.LLM_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise LLMError(f"LLM unreachable: {type(exc).__name__}") from exc
    if response.status_code >= 400:
        raise LLMError(f"LLM returned HTTP {response.status_code}: {response.text[:200]}")
    data = response.json()
    try:
        content = data["choices"][0]["message"]["content"]
        parsed = json.loads(content)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise LLMError("LLM returned an unparseable response") from exc
    usage = data.get("usage") or {}
    return parsed, {"input_tokens": int(usage.get("prompt_tokens") or 0), "output_tokens": int(usage.get("completion_tokens") or 0)}
