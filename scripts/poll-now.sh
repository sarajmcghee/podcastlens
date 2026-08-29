#!/usr/bin/env bash
# Run the poller immediately instead of waiting for its schedule.
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/config.sh

OUT="$(mktemp)"
aws lambda invoke --function-name "${PROJECT}-poller" --region "$REGION" \
  --cli-binary-format raw-in-base64-out --payload '{"source":"manual"}' \
  "$OUT" --query 'StatusCode' --output text
echo "poller returned: $(cat "$OUT")"
rm -f "$OUT"
echo
echo "Episodes now move NEW -> DOWNLOADING -> TRANSCRIBING -> READY."
echo "Transcribe usually takes a few minutes. Watch it with:  ./scripts/status.sh"
