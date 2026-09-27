#!/usr/bin/env python3
"""Fail if anything that looks like a real credential is tracked by git.

CLAUDE.md: never commit secrets; only .env.example is committed.
Usage: python infrastructure/scripts/check_secrets.py   (from the repo root)
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

PATTERNS = {
    "Anthropic API key": re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    "Alpaca key id": re.compile(r"\b[PA]K[A-Z0-9]{16,}\b"),
    "Private key block": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
}
FORBIDDEN_FILES = re.compile(r"(^|/)\.env(\.(?!example$)[^/]+)?$")
SKIP_SUFFIXES = {".png", ".jpg", ".ico", ".woff", ".woff2", ".lock"}


def tracked_files() -> list[str]:
    """Tracked files plus untracked, non-ignored ones (catch secrets before they're committed)."""
    out = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                         check=True, capture_output=True)
    return [p for p in out.stdout.decode().split("\0") if p]


def main() -> int:
    problems: list[str] = []
    for path in tracked_files():
        if FORBIDDEN_FILES.search(path):
            problems.append(f"{path}: environment files must not be committed")
            continue
        if Path(path).suffix in SKIP_SUFFIXES or path.endswith("package-lock.json"):
            continue
        try:
            text = Path(path).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        for label, pattern in PATTERNS.items():
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                problems.append(f"{path}:{line}: possible {label}")
    for p in problems:
        print(p, file=sys.stderr)
    if problems:
        print(f"\n{len(problems)} potential secret(s) found.", file=sys.stderr)
        return 1
    print("No secrets found in tracked files.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
