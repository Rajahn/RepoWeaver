#!/usr/bin/env python3
"""Fail when public-source files contain credential patterns or private references."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".venv", ".repoweaver", ".pytest_cache", ".ruff_cache"}
TEXT_SUFFIXES = {
    ".md",
    ".py",
    ".toml",
    ".yaml",
    ".yml",
    ".json",
    ".sql",
    ".txt",
    ".ini",
    ".cfg",
}

# Strings are split so this scanner does not flag its own source.
BANNED = [
    re.compile("xiaohong" + "shu\\.com", re.IGNORECASE),
    re.compile("code" + "wiz", re.IGNORECASE),
    re.compile("governance" + "-traction", re.IGNORECASE),
    re.compile("ghp" + r"_[A-Za-z0-9]{20,}"),
    re.compile("MAAS" + r"[A-Za-z0-9]{16,}"),
    re.compile("QST" + r"[A-Za-z0-9]{16,}"),
]


def iter_text_files() -> list[Path]:
    return [
        path
        for path in ROOT.rglob("*")
        if path.is_file()
        and path.suffix.lower() in TEXT_SUFFIXES
        and not any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts)
    ]


def main() -> int:
    findings: list[str] = []
    for path in iter_text_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in BANNED:
            if pattern.search(text):
                findings.append(f"{path.relative_to(ROOT)}: matched protected pattern")

    if findings:
        print("Public-source scan FAILED:")
        print("\n".join(f"  {finding}" for finding in findings))
        return 1

    print("Public-source scan PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
