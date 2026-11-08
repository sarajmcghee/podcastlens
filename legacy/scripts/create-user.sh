#!/usr/bin/env bash
# Create a sign-in for yourself. Cognito emails a temporary password and the
# hosted UI walks you through setting a real one, so no password is ever typed
# into a terminal or stored in a script.
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/config.sh

EMAIL="${1:-$NOTIFY_EMAIL}"
if [[ -z "$EMAIL" ]]; then
  echo "usage: ./scripts/create-user.sh you@example.com" >&2
  exit 1
fi

POOL_ID="$(stack_output "$AUTH_STACK" UserPoolId)"
aws cognito-idp admin-create-user \
  --user-pool-id "$POOL_ID" --region "$REGION" \
  --username "$EMAIL" \
  --user-attributes "Name=email,Value=$EMAIL" "Name=email_verified,Value=true" \
  --desired-delivery-mediums EMAIL \
  --query 'User.UserStatus' --output text

echo "Cognito has emailed $EMAIL a temporary password."
echo "Sign in at $(stack_output "$WEB_STACK" SiteUrl) and you will be asked to set a new one."
