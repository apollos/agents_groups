#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs
export MIC_LOG_DIR="${MIC_LOG_DIR:-$PWD/logs}"
stamp="$(date -u +%Y%m%d_%H%M%S)"
pytest_log="logs/tests_${stamp}.log"
e2e_log="logs/e2e_tool_${stamp}.log"
echo "[tests] pytest started ${stamp} UTC" > "$pytest_log"
python -m pytest -q >> "$pytest_log" 2>&1
echo "[e2e] validation started ${stamp} UTC" > "$e2e_log"
python tools/e2e_validate.py --max-queries 12 --max-links 6 --max-model-calls 6 --quiet >> "$e2e_log" 2>&1
cat "$pytest_log"
cat "$e2e_log"
echo "logs: $pytest_log $e2e_log"
