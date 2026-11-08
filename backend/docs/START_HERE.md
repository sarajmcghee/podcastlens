# Deploy and inspect the transcription backend

Run this walkthrough from `backend/` in a GitHub checkout. If you are using the
original ZIP in CloudShell, the equivalent directory is `~/podcastlens`.

This backend was deployed on November 8, 2026 as `podcast-lense-dev` in
`us-east-1`. See [VALIDATION.md](VALIDATION.md) for the observed results.
The frontend is connected through a companion stack using the
[root walkthrough](../../README.md). This document covers the pipeline itself.

## 1. Get the reviewed source

While the pull request is open, its branch contains this backend:

```bash
git clone --branch codex/deployed-backend-2026-11-08 https://github.com/sarajmcghee/podcastlens.git podcastlens-review
cd podcastlens-review/backend
```

After the pull request is merged, it can also be checked out from `main`.
Use a fresh checkout directory to keep an existing CloudShell installation intact.

Sign into the target AWS account with a non-root user or administrative role.
In the console, select **US East (N. Virginia), us-east-1** and open CloudShell.
Inspect the current identity:

```bash
aws sts get-caller-identity
```

The deployment script refuses root credentials. Deployment needs permissions for
CloudFormation, IAM, Lambda, S3, SQS, DynamoDB, EventBridge, CloudWatch and SNS.

## 2. Prepare the operator CLI

For a fresh checkout used to inspect an already deployed stack:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python cli.py --stack podcast-lense-dev --region us-east-1 outputs
```

All examples explicitly name the deployed stack. The CLI and script defaults
remain `podcastlens-dev`, which is a different name from `podcast-lense-dev`.

## 3. Deploy only when creating or updating the backend

Review `template.json` and the cost defaults below first. If the stack is already
deployed, you can continue to the recording/status steps without redeploying it.

```bash
bash scripts/deploy.sh podcast-lense-dev us-east-1 'your-email@example.com'
```

This creates or updates private audio/transcript buckets, a metadata/quota table,
an episode queue, two DLQs, four Lambdas, event rules, seven CloudWatch alarms
and an SNS topic. Packaging also creates a private artifact bucket outside the
stack. This is the **backend** deployment script; the root repository's
`scripts/deploy.sh` deploys the dashboard against this existing pipeline.

Wait for `CREATE_COMPLETE` or `UPDATE_COMPLETE`. Confirm the SNS email
subscription to receive notifications. For failures, inspect CloudFormation
Events and the first resource error before retrying.

## 4. Upload and inspect a recording

Record a short clip containing a distinctive word, such as "hummingbird".
Export WAV, MP3 or M4A and upload it through CloudShell's **Actions > Upload file**.
The file normally lands in your CloudShell home directory.

```bash
.venv/bin/python cli.py --stack podcast-lense-dev --region us-east-1 upload "$HOME/test.m4a" --title "First transcription"
```

Copy the returned `episodeId` into the following commands:

```bash
.venv/bin/python cli.py --stack podcast-lense-dev --region us-east-1 status EPISODE_ID
.venv/bin/python cli.py --stack podcast-lense-dev --region us-east-1 search EPISODE_ID hummingbird
.venv/bin/python cli.py --stack podcast-lense-dev --region us-east-1 budget
```

Transcription is asynchronous. Poll status until it reaches `COMPLETED` or a
failure/blocking state. Search after completion. A search offset is a character
position in the transcript, not an audio timestamp.

For a visual check in the same region:
- **Amazon Transcribe > Transcription jobs** shows the service job status.
- **DynamoDB > Tables > the stack's Episodes table > Explore table items** shows
  application records. Rows whose `pk` starts with `EP#` contain `title` and
  `status`.
- **S3 > the output bucket > transcripts/** contains transcript JSON.
- **CloudWatch > Alarms** shows alarm states; the worker's log group shows
  per-message failure categories.

The CLI uses the application status in DynamoDB. Transcribe service completion
can appear before the collector or reconciler updates that record.

## 5. Verify the operational controls

Uploading identical bytes again should reuse the episode and budget reservation.
Follow [ACCEPTANCE_TESTS.md](ACCEPTANCE_TESTS.md) for controlled failures, mixed
batches, quota boundaries and recovery. Record observed results in
[VALIDATION.md](VALIDATION.md); a configured control is not automatically a passed
cloud test.

For a malformed SQS test message, allow roughly an hour or longer for five
receives with the 720-second visibility timeout before checking the DLQ. A
worker-failure or queue-age email alone does not prove DLQ delivery.

## Cost and retention defaults

- Feed polling starts **disabled**. Configure and manually test a feed before
  enabling its hourly schedule.
- The application admits at most **600 reserved seconds per UTC month** by
  default, with a 15-second minimum reservation per job. Reservations are
  conservative and are not automatically refunded.
- Each recording is limited to five minutes and 25 MiB by default.
- The cap applies to this application's admission logic, not the total AWS bill.
  Storage, logs, alarms, requests and other workloads can incur charges.
- Reconciliation runs every five minutes.
- Input audio expires after 7 days, transcripts after 30 days and logs after
  7 days. Metadata persists until the table is deleted. Export evidence you
  need to retain.
- The deployment uses no EC2 instance, RDS instance, load balancer, NAT gateway
  or EKS cluster.

## 6. Add a feed after the recording tests

Copy `feeds.example.json` to `feeds.local.json`. Supply a trusted RSS 2.0 URL
and every exact HTTPS feed/audio/redirect host in `allowedHosts`. Use media you
have permission to process and an episode within the duration/size limits.

```bash
.venv/bin/python cli.py --stack podcast-lense-dev --region us-east-1 feeds feeds.local.json
.venv/bin/python cli.py --stack podcast-lense-dev --region us-east-1 scan
```

The scanner checks the newest 20 items per feed and attempts up to three new
downloads per run. Inspect its logs and episode records. Once manual scanning
works, deliberately enable `FeedScheduleEnabled` through CloudFormation,
preserving other parameter values. This starts hourly scanning.

For troubleshooting and eventual lab cleanup, use [RUNBOOK.md](RUNBOOK.md).
