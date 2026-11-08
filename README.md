# PodcastLens

A podcast transcription pipeline with an authenticated operator dashboard.
The frontend keeps the existing PodcastLens styling and now reads the same
DynamoDB episode records and S3 transcripts used by the deployed Python pipeline.

The pipeline is deployed as **`podcast-lense-dev` in `us-east-1`**.
The dashboard has been deployed and its owner reported successful sign-in.
Authenticator MFA requires applying the updated web template and completing enrollment.
See the [validation record](backend/docs/VALIDATION.md) for observed results.

## Dashboard

- Shows recordings from CLI uploads and feed discovery with their actual
  processing, completion, failure or budget-blocked status.
- Refreshes the first page every 15 seconds while visible and supports pagination.
- Reads completed transcripts and searches within a selected transcript.
- Shows reserved seconds and the current cap from the existing pipeline.
- Manages the scanner's existing feed configuration, including allowed audio
  hosts, and requests manual scans.
- Requires authenticator-app MFA in the Cognito deployment template.
- Uses Cognito code-flow sign-in with PKCE and state verification. API Gateway
  requires an access token and scope; the API also requires the operators group.

Accounts are invitation-only. All operators share one personal workspace; this
is not a tenant-isolated service. Audio uploads continue through the CLI.
Browser uploads, Comprehend topic extraction and cross-transcript indexing are
not implemented.

## Layout

| Location | Purpose |
|---|---|
| `web/` | Preserved visual design, updated for the deployed pipeline |
| `backend/` | Pipeline, CLI, original tests and the new dashboard API/tests |
| `infrastructure/web.json` | Companion CloudFront/private S3 site, Cognito, HTTP API and API Lambda |
| `scripts/deploy.sh` | Deploy the dashboard against an existing pipeline |
| `scripts/create_user.py` | Invite a trusted operator |
| `tests/test_dashboard.py` | Browser checks with mocked HTTP services |
| `legacy/` | Preserved original unused backend; excluded from deployment |

The API reads the existing episode table and transcript bucket and invokes the
existing scanner. It does not create a second episode table, queue or worker.
The original pipeline template and processing modules remain unchanged.

## Connect the website

In CloudShell, use a non-root administrative login in the pipeline's region.
Clone the current main branch:

```bash
git clone --branch main https://github.com/sarajmcghee/podcastlens.git podcastlens-dashboard
cd podcastlens-dashboard
bash scripts/deploy.sh podcast-lense-dev us-east-1
```

This creates or updates **`podcast-lense-dev-web`**, reads the pipeline outputs,
packages the API, generates frontend configuration and publishes the site.
CloudFront provisioning can take several minutes. The command prints the site URL.

The deployment uses the existing pipeline and data. It does not redeploy the
pipeline, enable feed scheduling or deploy the old backend. It creates billable
website/authentication/API resources and uses the existing artifact bucket and
alarm topic.

Invite your operator login:

```bash
python3 scripts/create_user.py your-email@example.com podcast-lense-dev us-east-1
```

New users receive a Cognito invitation email. Open the printed website URL,
sign in, set the requested password, enroll an authenticator app when prompted,
and inspect an existing recording.
Only invite people who should read all recordings and manage feeds.

A GitHub push or merge does not deploy to AWS. CI has no deployment job or AWS
credentials. Run the deployment command to apply the integration.

## Require MFA on the existing dashboard

From a fresh clone of main in CloudShell, apply the updated template to the
existing companion stack. The CLI retains the stack's existing parameter values,
including the deployed API package. This update enables required TOTP MFA on the
existing user pool; it does not create replacement accounts.

```bash
aws cloudformation deploy \
  --region us-east-1 \
  --stack-name podcast-lense-dev-web \
  --template-file infrastructure/web.json \
  --capabilities CAPABILITY_IAM \
  --no-fail-on-empty-changeset \
  --no-cli-pager
```

After the stack update succeeds, sign out of the dashboard and sign in again.
Complete the authenticator-app setup on the Cognito sign-in page: scan its QR
code and enter the current code from the authenticator. Keep the QR code, setup
secret and one-time codes private. Sign out and sign in once more to confirm the
authenticator code is required after the password. Existing issued access tokens
can remain valid until expiry (one hour in this template).

Verify the pool setting in CloudShell:

```bash
PODCASTLENS_POOL_ID=$(aws cloudformation describe-stacks \
  --region us-east-1 --stack-name podcast-lense-dev-web \
  --query 'Stacks[0].Outputs[?OutputKey==`UserPoolId`].OutputValue | [0]' \
  --output text --no-cli-pager)
aws cognito-idp get-user-pool-mfa-config \
  --region us-east-1 --user-pool-id "$PODCASTLENS_POOL_ID" \
  --no-cli-pager
```

Expected: `MfaConfiguration` is `ON` and `SoftwareTokenMfaConfiguration.Enabled`
is `true`. This protects dashboard sign-in. AWS root/IAM console MFA is configured
separately. GitHub tests cannot confirm live deployment or personal enrollment.

## First live checks

1. Open the website signed out; recordings must not be visible.
2. Sign in with the invited operator, complete authenticator MFA and find an existing completed recording.
3. Read its transcript and search for a recorded word.
4. Compare status and reserved seconds with the CLI.
5. Confirm an API request without a token is rejected.
6. Save a permitted feed, request a scan and observe the existing pipeline.
7. Sign out and confirm recordings and transcript text disappear.

Track each live check separately. Successful deployment and owner-reported sign-in
do not establish that every acceptance check or MFA enrollment has been completed.

## Tests

```bash
cd backend
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/cfn-lint template.json ../infrastructure/web.json
cd ..
```

Browser checks intercept HTTP requests and use no AWS credentials:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-web-dev.txt
.venv/bin/python -m playwright install chromium
.venv/bin/python tests/test_dashboard.py
```

Set `CHROMIUM_EXECUTABLE` only for an existing compatible Chromium binary.
CI also runs the preserved original backend tests separately.

## Operations and cleanup

See the [backend walkthrough](backend/docs/START_HERE.md),
[acceptance checks](backend/docs/ACCEPTANCE_TESTS.md) and
[runbook](backend/docs/RUNBOOK.md). The default admission cap is 600 reserved
transcription seconds per UTC month. This is not a cap on the total AWS bill;
storage, requests, logs, alarms and the website can incur charges.

The API has a separate Lambda error alarm on the existing SNS topic.
Feed configuration is shared with the CLI. Conditional writes prevent silently
overwriting concurrent configuration edits. Removing a feed preserves recordings.

To remove only the dashboard, save needed information, empty its site bucket
and delete the companion web stack. Its Cognito pool is protected and retained,
so deleting login accounts is a separate deliberate operation. The pipeline
and its data remain. Remove only the `PIPELINE_STACK/web-api/` artifact prefix
when it is no longer needed. Use the backend runbook for pipeline cleanup.
