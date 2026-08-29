#!/usr/bin/env bash
# One place to see whether the pipeline is healthy.
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/config.sh

TABLE="$(stack_output "$CORE_STACK" TableName)"
DLQ_URL="$(aws sqs get-queue-url --queue-name "${PROJECT}-episodes-dlq" \
  --region "$REGION" --query QueueUrl --output text 2>/dev/null || echo "")"

echo "== endpoints"
printf "  site  %s\n" "$(stack_output "$WEB_STACK" SiteUrl)"
printf "  api   %s\n" "$(stack_output "$CORE_STACK" ApiUrl)"

echo
echo "== episodes by status"
aws dynamodb scan --table-name "$TABLE" --region "$REGION" \
  --filter-expression "#t = :ep" \
  --expression-attribute-names '{"#t":"type","#s":"status"}' \
  --expression-attribute-values '{":ep":{"S":"EPISODE"}}' \
  --projection-expression "#s,title" \
  --query 'Items[].[status.S,title.S]' --output text 2>/dev/null \
  | sort | awk -F'\t' 'NF{printf "  %-16s %s\n", $1, substr($2,1,60)} END{if(NR==0)print "  (none yet)"}'

echo
echo "== transcribe budget this month"
aws dynamodb get-item --table-name "$TABLE" --region "$REGION" \
  --key "{\"PK\":{\"S\":\"SYSTEM\"},\"SK\":{\"S\":\"BUDGET#$(date -u +%Y-%m)\"}}" \
  --query 'Item.usedSeconds.N' --output text 2>/dev/null \
  | awk -v cap="$MONTHLY_TRANSCRIBE_SECONDS" \
      '{u=($1=="None"||$1=="")?0:$1; printf "  %d of %d seconds used (%d%%)\n", u, cap, (u*100)/cap}'

echo
echo "== dead letter queue"
if [[ -n "$DLQ_URL" ]]; then
  aws sqs get-queue-attributes --queue-url "$DLQ_URL" --region "$REGION" \
    --attribute-names ApproximateNumberOfMessages \
    --query 'Attributes.ApproximateNumberOfMessages' --output text \
    | awk '{printf "  %s failed message(s)\n", $1}'
fi
