#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
STACK_NAME="${1:-podcastlens-dev}"
DEPLOY_REGION="${2:-us-east-1}"
ALERT_EMAIL="${3:-}"
if [[ ! "$STACK_NAME" =~ ^[a-zA-Z][a-zA-Z0-9-]{0,39}$ ]]; then
  echo 'Use a stack name of at most 40 letters, digits, or hyphens, starting with a letter.' >&2; exit 1
fi
command -v aws >/dev/null || { echo 'AWS CLI is required. AWS CloudShell already includes it.' >&2; exit 1; }
CALLER_ARN="$(aws sts get-caller-identity --query Arn --output text --region "$DEPLOY_REGION")"
if [[ "$CALLER_ARN" == *:root ]]; then
  echo 'Sign in with your non-root administrator/role before deploying.' >&2; exit 1
fi
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text --region "$DEPLOY_REGION")"
echo "Deploying $STACK_NAME in $DEPLOY_REGION under $CALLER_ARN"
echo 'Creates billable AWS resources; default transcription admission cap is 10 minutes/month.'
python3 -m venv .venv
.venv/bin/pip install --quiet -r requirements.txt
mkdir -p .build/package
rm -rf .build/package
mkdir -p .build/package
.venv/bin/pip install --quiet -r requirements.txt --target .build/package
cp -R src/podcastlens .build/package/
.venv/bin/python - <<'PY'
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
base=Path('.build/package')
with ZipFile('.build/lambda.zip','w',ZIP_DEFLATED) as archive:
 for file in sorted(base.rglob('*')):
  if file.is_file() and '__pycache__' not in file.parts:
   archive.write(file,file.relative_to(base))
PY
ARTIFACT_BUCKET="podcastlens-artifacts-${ACCOUNT_ID}-${DEPLOY_REGION}"
if ! aws s3api head-bucket --bucket "$ARTIFACT_BUCKET" --region "$DEPLOY_REGION" 2>/dev/null; then
  if [[ "$DEPLOY_REGION" == us-east-1 ]]; then
    aws s3api create-bucket --bucket "$ARTIFACT_BUCKET" --region "$DEPLOY_REGION" >/dev/null
  else
    aws s3api create-bucket --bucket "$ARTIFACT_BUCKET" --region "$DEPLOY_REGION" --create-bucket-configuration "LocationConstraint=$DEPLOY_REGION" >/dev/null
  fi
fi
aws s3api put-public-access-block --bucket "$ARTIFACT_BUCKET" --region "$DEPLOY_REGION" --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-encryption --bucket "$ARTIFACT_BUCKET" --region "$DEPLOY_REGION" --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
PACKAGE_HASH="$(.venv/bin/python -c 'import hashlib; print(hashlib.sha256(open(".build/lambda.zip","rb").read()).hexdigest())')"
CODE_KEY="$STACK_NAME/$PACKAGE_HASH.zip"
aws s3 cp .build/lambda.zip "s3://$ARTIFACT_BUCKET/$CODE_KEY" --region "$DEPLOY_REGION" --only-show-errors
aws cloudformation validate-template --template-body file://template.json --region "$DEPLOY_REGION" >/dev/null
# Preserve operational parameters on updates. Override explicitly through env vars.
PARAMS=("CodeBucket=$ARTIFACT_BUCKET" "CodeKey=$CODE_KEY")
if ! aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$DEPLOY_REGION" >/dev/null 2>&1; then
  PARAMS+=("FeedScheduleEnabled=false" "MonthlyCapSeconds=${MONTHLY_CAP_SECONDS:-600}" "MaxEpisodeSeconds=${MAX_EPISODE_SECONDS:-300}")
fi
if [[ -n "$ALERT_EMAIL" ]]; then PARAMS+=("AlertEmail=$ALERT_EMAIL"); fi
if [[ -n "${MONTHLY_CAP_SECONDS:-}" ]]; then PARAMS+=("MonthlyCapSeconds=$MONTHLY_CAP_SECONDS"); fi
if [[ -n "${MAX_EPISODE_SECONDS:-}" ]]; then PARAMS+=("MaxEpisodeSeconds=$MAX_EPISODE_SECONDS"); fi
aws cloudformation deploy --template-file template.json --stack-name "$STACK_NAME" --region "$DEPLOY_REGION" --capabilities CAPABILITY_IAM --parameter-overrides "${PARAMS[@]}" --tags Project=PodcastLens --no-fail-on-empty-changeset
echo 'Deployment complete. Confirm any SNS email subscription before testing notifications.'
.venv/bin/python cli.py --stack "$STACK_NAME" --region "$DEPLOY_REGION" outputs
