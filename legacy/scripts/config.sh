#!/usr/bin/env bash
# Shared settings. Every script sources this.
# Override any of them from your shell: PROJECT=other ./scripts/deploy.sh

export PROJECT="${PROJECT:-podcastlens}"
export REGION="${REGION:-$(aws configure get region)}"
export REGION="${REGION:-eu-west-1}"

# Where SNS sends "episode ready", CloudWatch alarms, and AWS Budgets warnings.
# You must click the confirmation link AWS emails you before anything arrives.
export NOTIFY_EMAIL="${NOTIFY_EMAIL:-}"

# Seconds of each episode to transcribe. Transcribe's free tier is 60 minutes
# per month; 600s means six episodes fit inside it. Set 0 for full episodes.
export CLIP_SECONDS="${CLIP_SECONDS:-600}"

# Hard monthly ceiling enforced in code, below the 3600s free tier.
export MONTHLY_TRANSCRIBE_SECONDS="${MONTHLY_TRANSCRIBE_SECONDS:-3000}"

# How often the poller checks feeds, in minutes.
export POLL_RATE_MINUTES="${POLL_RATE_MINUTES:-360}"

# AWS Budgets alerts you before you cross this, in USD.
export MONTHLY_BUDGET_USD="${MONTHLY_BUDGET_USD:-5}"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
export ACCOUNT_ID
export ARTIFACT_BUCKET="${PROJECT}-artifacts-${ACCOUNT_ID}-${REGION}"

export AUTH_STACK="${PROJECT}-auth"
export CORE_STACK="${PROJECT}-core"
export WEB_STACK="${PROJECT}-web"

# Resolve a single output from a deployed stack.
stack_output() {
  aws cloudformation describe-stacks \
    --stack-name "$1" --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" --output text
}
