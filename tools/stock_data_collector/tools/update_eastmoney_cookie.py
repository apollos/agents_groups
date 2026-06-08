from __future__ import annotations

import argparse
import re
import shlex
import sys
from pathlib import Path


def _extract_cookie(text: str) -> str:
    value = text.strip()
    if not value:
        raise SystemExit("No cookie text provided.")

    # Accept full "Copy as cURL" text from browser DevTools.
    try:
        parts = shlex.split(value)
    except ValueError:
        parts = []
    for idx, part in enumerate(parts):
        if part in {"-b", "--cookie", "--cookie-jar"} and idx + 1 < len(parts):
            return parts[idx + 1].strip()
        if part.lower().startswith("cookie:"):
            return part.split(":", 1)[1].strip()

    match = re.search(r"(?:^|\s)-b\s+(['\"])(.*?)\1", value, flags=re.S)
    if match:
        return match.group(2).strip()
    match = re.search(r"Cookie:\s*([^\r\n]+)", value, flags=re.I)
    if match:
        return match.group(1).strip()

    # Otherwise treat the argument/stdin as the raw cookie itself.
    return value


def _quote_env_value(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def update_env(env_path: Path, cookie: str) -> None:
    key = "EASTMONEY_COOKIE"
    new_line = f"{key}={_quote_env_value(cookie)}"
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
    else:
        lines = []

    replaced = False
    for idx, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[idx] = new_line
            replaced = True
            break
    if not replaced:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("# Browser-verified Eastmoney cookie for money-flow APIs.")
        lines.append(new_line)

    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Update EASTMONEY_COOKIE in a local .env file.")
    parser.add_argument("--env-file", default=".env", help="Path to .env file. Defaults to ./.env")
    parser.add_argument("--cookie", default=None, help="Raw Cookie string, Cookie header, or full Copy-as-cURL command.")
    args = parser.parse_args()

    raw = args.cookie if args.cookie is not None else sys.stdin.read()
    cookie = _extract_cookie(raw)
    update_env(Path(args.env_file), cookie)
    print(f"Updated {args.env_file} with EASTMONEY_COOKIE ({len(cookie)} characters).")


if __name__ == "__main__":
    main()
