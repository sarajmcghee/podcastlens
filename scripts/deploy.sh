#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 scripts/deploy_web.py "${1:-podcast-lense-dev}" "${2:-us-east-1}"
