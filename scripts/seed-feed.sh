#!/usr/bin/env bash
# Add a feed without going through the browser.
#
#   ./scripts/seed-feed.sh https://lexfridman.com/feed/podcast/ you@example.com
#
# This invokes the API Lambda directly with a synthetic API Gateway event rather
# than faking a JWT. In production API Gateway validates the token and injects
# the `sub` claim; here we inject it ourselves and the handler cannot tell the
# difference. Useful for testing the handler in isolation - and a good reminder
# that anything able to invoke your Lambda directly bypasses the authorizer
# entirely, which is why invoke permissions matter as much as the authorizer.
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/config.sh

FEED_URL="${1:-https://lexfridman.com/feed/podcast/}"
EMAIL="${2:-$NOTIFY_EMAIL}"
[[ -n "$EMAIL" ]] || { echo "usage: ./scripts/seed-feed.sh FEED_URL EMAIL" >&2; exit 1; }

POOL_ID="$(stack_output "$AUTH_STACK" UserPoolId)"
SUB="$(aws cognito-idp admin-get-user --user-pool-id "$POOL_ID" --username "$EMAIL" \
  --region "$REGION" --query "UserAttributes[?Name=='sub'].Value" --output text)"
[[ -n "$SUB" && "$SUB" != "None" ]] || { echo "no Cognito user for $EMAIL" >&2; exit 1; }

PAYLOAD="$(FEED_URL="$FEED_URL" SUB="$SUB" python3 -c '
import json, os
print(json.dumps({
    "routeKey": "POST /feeds",
    "body": json.dumps({"url": os.environ["FEED_URL"]}),
    "requestContext": {"authorizer": {"jwt": {"claims": {"sub": os.environ["SUB"]}}}},
}))')"

OUT="$(mktemp)"
aws lambda invoke --function-name "${PROJECT}-api" --region "$REGION" \
  --cli-binary-format raw-in-base64-out --payload "$PAYLOAD" "$OUT" \
  --query 'StatusCode' --output text >/dev/null

python3 -c '
import json, sys
resp = json.load(open(sys.argv[1]))
body = json.loads(resp.get("body", "{}"))
if resp.get("statusCode") not in (200, 201):
    print("failed:", body.get("error") or body)
    sys.exit(1)
feed = body["feed"]
print("added: " + feed["title"])
print("  feedId            " + feed["feedId"])
print("  episodes in feed  " + str(feed["episodeCount"]))
' "$OUT"
rm -f "$OUT"
