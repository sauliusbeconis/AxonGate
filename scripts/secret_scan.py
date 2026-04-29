"""Small repository secret scanner used by CI.

This intentionally avoids scanning .env and generated Python bytecode while
catching common private key/API token shapes in tracked source files.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


PATTERNS = [
    re.compile(r"jina_[A-Za-z0-9_-]{20,}"),
    re.compile(r"0x[a-fA-F0-9]{64}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
]
ALLOWLIST_SUBSTRINGS = {
    "your_cdp_api_key_secret",
    "your_cdp_wallet_secret",
    "your_jina_api_key",
    "<x402 payment proof",
    "<base64_signed",
    "0xLISTING_PAYMENT_TX",
    "0xYourTransactionHash",
}


def tracked_files() -> list[Path]:
    result = subprocess.run(["git", "ls-files"], check=True, capture_output=True, text=True)
    return [Path(line) for line in result.stdout.splitlines() if line.strip()]


def should_scan(path: Path) -> bool:
    if path.name == ".env" or path.suffix == ".pyc":
        return False
    if "__pycache__" in path.parts:
        return False
    return path.is_file()


def is_allowed(line: str) -> bool:
    return any(item in line for item in ALLOWLIST_SUBSTRINGS)


def main() -> int:
    findings: list[str] = []
    for path in tracked_files():
        if not should_scan(path):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            if is_allowed(line):
                continue
            if any(pattern.search(line) for pattern in PATTERNS):
                findings.append(f"{path}:{line_number}: possible secret")

    if findings:
        print("\n".join(findings))
        return 1

    print("No obvious secrets found in tracked files.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
