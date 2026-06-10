#!/usr/bin/env python3
"""Check that a releasable MIC tree does not contain local secrets/artifacts.

This is intentionally separate from ``pytest`` because developers normally keep
an untracked .env and local DBs while working. Run this before making a zip for
another Agent or teammate.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"tvly-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"SERPAPI_API_KEY\s*=\s*[^\s#]+"),
    re.compile(r"OPENCLAW_GATEWAY_TOKEN\s*=\s*[^\s#]+"),
]
FORBIDDEN_NAMES = {".env", "mic.db"}
FORBIDDEN_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache"}


def iter_files(root: Path):
    for path in root.rglob("*"):
        rel_parts = set(path.relative_to(root).parts)
        if rel_parts & SKIP_DIRS:
            continue
        if path.is_file():
            yield path


def main() -> int:
    parser = argparse.ArgumentParser(description="Check MIC release tree cleanliness")
    parser.add_argument("path", nargs="?", default=".")
    args = parser.parse_args()
    root = Path(args.path).resolve()
    problems: list[str] = []

    for path in iter_files(root):
        rel = path.relative_to(root)
        if path.name in FORBIDDEN_NAMES:
            problems.append(f"forbidden local artifact: {rel}")
            continue
        if path.suffix.lower() in FORBIDDEN_SUFFIXES and rel.parts[0] == "logs":
            problems.append(f"runtime DB under logs/: {rel}")
            continue
        if rel.parts[0] == "logs" and path.name != ".gitkeep":
            problems.append(f"runtime log/artifact under logs/: {rel}")
            continue
        if path.stat().st_size > 2_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if path.name == ".env.example":
            # Example file is expected to mention env var names but must keep
            # values blank. The generic key-name regexes are therefore skipped.
            for pat in SECRET_PATTERNS[:2]:
                if pat.search(text):
                    problems.append(f"secret-looking value in {rel}")
            continue
        if any(pat.search(text) for pat in SECRET_PATTERNS):
            problems.append(f"secret-looking value in {rel}")

    if problems:
        print("FAIL release cleanliness check")
        for item in problems:
            print(f"  - {item}")
        return 1
    print("PASS release cleanliness check")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
