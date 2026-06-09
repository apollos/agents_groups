#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs
export MIC_ALLOW_MOCK=true
export MIC_LOG_DIR="${MIC_LOG_DIR:-$PWD/logs}"
python tools/e2e_validate.py "$@"
