#!/usr/bin/env bash
# Delete everything. S3 buckets must be emptied before CloudFormation can
# remove them, and the user pool's deletion protection has to be turned off.
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/config.sh

echo "This deletes the $PROJECT stacks, buckets and ALL user accounts in $REGION."
read -r -p "Type the project name to confirm: " CONFIRM
[[ "$CONFIRM" == "$PROJECT" ]] || { echo "aborted"; exit 1; }

for BUCKET in \
  "$(stack_output "$WEB_STACK" SiteBucketName 2>/dev/null || true)" \
  "$(stack_output "$CORE_STACK" MediaBucketName 2>/dev/null || true)" \
  "$ARTIFACT_BUCKET"
do
  [[ -n "$BUCKET" && "$BUCKET" != "None" ]] || continue
  echo "==> emptying $BUCKET"
  aws s3 rm "s3://$BUCKET" --recursive --region "$REGION" >/dev/null 2>&1 || true
done

POOL_ID="$(stack_output "$AUTH_STACK" UserPoolId 2>/dev/null || true)"
if [[ -n "$POOL_ID" && "$POOL_ID" != "None" ]]; then
  echo "==> disabling user pool deletion protection"
  aws cognito-idp update-user-pool --user-pool-id "$POOL_ID" --region "$REGION" \
    --deletion-protection INACTIVE >/dev/null 2>&1 || true
fi

# Reverse of the deploy order: dependents first, exporters last. CloudFormation
# refuses to delete a stack whose exports are still imported by another.
for STACK in "$WEB_STACK" "$CORE_STACK" "$AUTH_STACK"; do
  echo "==> deleting $STACK"
  aws cloudformation delete-stack --stack-name "$STACK" --region "$REGION" 2>/dev/null || true
  aws cloudformation wait stack-delete-complete --stack-name "$STACK" --region "$REGION" 2>/dev/null || true
done

aws s3 rb "s3://$ARTIFACT_BUCKET" --region "$REGION" 2>/dev/null || true
echo "done."
