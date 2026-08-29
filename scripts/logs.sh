#!/usr/bin/env bash
# Tail one function's logs.  ./scripts/logs.sh processor
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/config.sh

FN="${1:-processor}"
case "$FN" in
  api|poller|processor|completer|digest) ;;
  *) echo "usage: ./scripts/logs.sh [api|poller|processor|completer|digest]" >&2; exit 1 ;;
esac

echo "tailing /aws/lambda/${PROJECT}-${FN} (ctrl-c to stop)"
aws logs tail "/aws/lambda/${PROJECT}-${FN}" --follow --since 15m --region "$REGION"
