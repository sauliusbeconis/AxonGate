"""Deterministic prompt-injection screening for untrusted text.

The scanner is intentionally rule-based: it is fast, free to run on every page
AxonGate reads, and its findings are explainable (category, excerpt, offset).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_F = re.IGNORECASE | re.MULTILINE


@dataclass(frozen=True)
class Rule:
    category: str
    weight: int
    pattern: re.Pattern[str]
    description: str


RULES: tuple[Rule, ...] = (
    Rule("instruction_override", 40, re.compile(
        r"\b(ignore|disregard|forget|override|bypass)\b[^.\n]{0,40}\b(all|any|every|the|your|previous|prior|above|earlier|preceding)\b[^.\n]{0,30}\b(instructions?|prompts?|rules|directives|guidelines|context|messages?)\b", _F),
        "Tells the reader to discard earlier instructions."),
    Rule("instruction_override", 30, re.compile(
        r"\b(new|updated|real|actual|true)\s+(system\s+)?(instructions?|directives?|task)\s*[:\-]", _F),
        "Introduces replacement instructions."),
    Rule("role_hijack", 30, re.compile(
        r"\b(you are now|from now on,? you|act as (an? )?(unrestricted|jailbroken|dan\b)|enter (developer|god|debug) mode|pretend (that )?you (are|have no))", _F),
        "Attempts to change the assistant's role or restrictions."),
    Rule("role_hijack", 35, re.compile(
        r"(<\|im_start\|>|<\|im_end\|>|<\|system\|>|\[/?INST\]|<</?SYS>>)", _F),
        "Contains chat-template control tokens."),
    Rule("role_markers", 12, re.compile(
        r"^\s*(system|assistant)\s*:\s", _F),
        "Contains transcript-style role markers."),
    Rule("agent_targeting", 20, re.compile(
        r"\b(if you are an?|attention|note to( all)?|message for( any)?|dear)\s+(ai|llm|language model|assistant|agent|chatbot|gpt|claude|bot)s?\b", _F),
        "Addresses AI agents directly."),
    Rule("prompt_exfiltration", 35, re.compile(
        r"\b(reveal|print|output|repeat|show|leak|display)\b[^.\n]{0,30}\b(system prompt|hidden instructions|initial instructions|your instructions|developer message)\b", _F),
        "Asks for the system prompt or hidden instructions."),
    Rule("secret_exfiltration", 45, re.compile(
        r"\b(send|share|post|email|paste|give|reveal|include|forward|upload)\b[^.\n]{0,40}\b(api[ _-]?keys?|access tokens?|passwords?|credentials|private keys?|seed phrases?|mnemonic|secret keys?|session cookies?)\b", _F),
        "Requests credentials or secrets."),
    Rule("data_exfiltration", 35, re.compile(
        r"!\[[^\]]*\]\(\s*https?://[^)\s]+\?[^)\s]*(\{|%7B|data=|q=|prompt=|secret|token|key=)[^)\s]*\)", _F),
        "Markdown image that could leak data through its URL."),
    Rule("unsafe_action", 40, re.compile(
        r"\b(transfer|send|withdraw|approve|bridge|swap)\b[^.\n]{0,30}\b(all|your|the|entire|full)\b[^.\n]{0,20}\b(funds|balance|usdc|usdt|eth|tokens?|wallet|crypto)\b", _F),
        "Instructs the reader to move funds."),
    Rule("unsafe_action", 35, re.compile(
        r"(\b(curl|wget)\b[^\n|]{0,200}\|\s*(ba|z)?sh\b|\brm\s+-rf\s+/|\b(run|execute)\s+(this|the following)\s+(command|code|script)\b)", _F),
        "Instructs the reader to execute commands."),
    Rule("tool_coercion", 25, re.compile(
        r"\b(call|invoke|use|trigger)\s+(the\s+)?[\w.-]+\s+(tool|function|plugin|action)\b[^.\n]{0,40}\b(immediately|now|without (asking|confirmation|telling))", _F),
        "Pressures the reader to call a tool without confirmation."),
    Rule("concealment", 30, re.compile(
        r"\b(do not|don't|never)\s+(tell|inform|mention|reveal|show)\b[^.\n]{0,30}\b(the )?(user|human|operator|owner)\b", _F),
        "Asks the reader to hide actions from the user."),
)

INVISIBLE = re.compile("[​‌‍⁠⁡⁢⁣⁤﻿­᠎]")
BIDI = re.compile("[‪-‮⁦-⁩]")
TAG_CHARS = re.compile("[\U000e0000-\U000e007f]")
BASE64_BLOB = re.compile(r"\b[A-Za-z0-9+/]{120,}={0,2}")

MAX_FINDINGS = 25


def _excerpt(text: str, start: int, end: int) -> str:
    left = max(0, start - 40)
    right = min(len(text), end + 40)
    snippet = text[left:right].replace("\n", " ")
    return ("…" if left else "") + snippet + ("…" if right < len(text) else "")


def scan_text(text: str) -> dict:
    """Return a risk assessment and a sanitized copy of `text`."""
    text = text or ""
    findings: list[dict] = []
    spans: list[tuple[int, int]] = []
    score = 0
    seen_categories: dict[str, int] = {}

    for name, pattern, weight, label in (
        ("hidden_unicode", INVISIBLE, 15, "Zero-width or invisible characters"),
        ("hidden_unicode", BIDI, 20, "Bidirectional override characters"),
        ("hidden_unicode", TAG_CHARS, 45, "Unicode tag characters (invisible ASCII smuggling)"),
    ):
        hits = list(pattern.finditer(text))
        if hits:
            score += weight if len(hits) < 20 else weight + 10
            findings.append({
                "category": name,
                "severity": "high" if weight >= 40 else "medium",
                "description": f"{label}: {len(hits)} found",
                "offset": hits[0].start(),
                "excerpt": _excerpt(text, hits[0].start(), hits[0].end()),
            })

    for rule in RULES:
        for match in rule.pattern.finditer(text):
            repeats = seen_categories.get(rule.category, 0)
            # Repeated hits in one category add less than the first one.
            score += rule.weight if repeats == 0 else max(5, rule.weight // 4)
            seen_categories[rule.category] = repeats + 1
            spans.append((match.start(), match.end()))
            if len(findings) < MAX_FINDINGS:
                findings.append({
                    "category": rule.category,
                    "severity": "high" if rule.weight >= 35 else "medium",
                    "description": rule.description,
                    "offset": match.start(),
                    "excerpt": _excerpt(text, match.start(), match.end()),
                })

    blobs = BASE64_BLOB.findall(text)
    if blobs:
        score += 10
        findings.append({
            "category": "encoded_payload",
            "severity": "low",
            "description": f"Long base64-like blob(s): {len(blobs)}",
            "offset": text.find(blobs[0]),
            "excerpt": blobs[0][:60] + "…",
        })

    score = min(score, 100)
    level = "high" if score >= 50 else "medium" if score >= 20 else "low"

    sanitized = TAG_CHARS.sub("", BIDI.sub("", INVISIBLE.sub("", text)))
    if spans:
        # Re-run on the cleaned text so offsets match what we redact.
        clean_spans = sorted({(m.start(), m.end()) for rule in RULES for m in rule.pattern.finditer(sanitized)})
        out, cursor = [], 0
        for start, end in clean_spans:
            if start < cursor:
                continue
            out.append(sanitized[cursor:start])
            out.append("[removed: possible prompt injection]")
            cursor = end
        out.append(sanitized[cursor:])
        sanitized = "".join(out)

    return {
        "risk": level,
        "score": score,
        "finding_count": len(findings),
        "categories": sorted({f["category"] for f in findings}),
        "findings": findings,
        "sanitized": sanitized,
        "chars_scanned": len(text),
        "method": "axongate-rules-v1",
    }


def summarize(scan: dict) -> dict:
    """Compact version embedded in read/search/verify responses."""
    return {
        "risk": scan["risk"],
        "score": scan["score"],
        "categories": scan["categories"],
        "findings": [{k: f[k] for k in ("category", "severity", "excerpt")} for f in scan["findings"][:5]],
    }
