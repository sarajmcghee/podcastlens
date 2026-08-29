# PodcastLens

A serverless podcast transcription and search engine, built as a study project
for the **AWS Certified Solutions Architect – Associate (SAA-C03)** exam.

You subscribe to a podcast feed. On a schedule, PodcastLens notices new
episodes, transcribes them with Amazon Transcribe, pulls out the topics and
named entities with Amazon Comprehend, and gives you a searchable archive plus a
weekly email digest.

It runs at effectively **$0/month** inside the AWS free tier, and it deliberately
touches most of the services the exam actually asks about.

---

## The architecture

```
                    EventBridge Scheduler  ──(every 6h)──┐
                                                         ▼
  ┌──────────┐   HTTPS    ┌────────────┐          ┌─────────────┐
  │ Browser  │───────────▶│ CloudFront │          │  Poller λ   │
  └────┬─────┘            └─────┬──────┘          └──────┬──────┘
       │                        │ OAC                    │ new episodes
       │ OAuth2 + PKCE          ▼                        ▼
       │                  ┌──────────┐            ┌─────────────┐
       ▼                  │ S3 site  │            │  SQS queue  │──▶ DLQ
  ┌──────────┐            │ (private)│            └──────┬──────┘
  │ Cognito  │            └──────────┘                   │
  └────┬─────┘                                           ▼
       │ JWT                                      ┌─────────────┐
       ▼                                          │ Processor λ │
  ┌─────────────┐        ┌──────────┐             └──────┬──────┘
  │ API Gateway │───────▶│  API λ   │                    │ clip → S3
  │  (HTTP API) │        └────┬─────┘                    ▼
  └─────────────┘             │              ┌───────────────────────┐
                              │              │   Amazon Transcribe   │
                              ▼              └───────────┬───────────┘
                       ┌────────────┐                    │ job state change
                       │  DynamoDB  │◀───────┐           ▼
                       │  (1 table) │        │    ┌──────────────┐
                       └────────────┘        └────│ Completer λ  │
                                                  └──────┬───────┘
                       ┌────────────┐                    │
                       │ S3 media   │◀───────────────────┤ topics
                       │ transcripts│                    ▼
                       └────────────┘             ┌────────────┐
                                                  │ Comprehend │
                       ┌────────────┐             └────────────┘
                       │  SNS ──▶ 📧│◀── CloudWatch Alarms, Budgets
                       └────────────┘
```

Nothing in this diagram is ever idle-billed. There are no servers, no load
balancers, and no NAT gateways — which is also why it costs nothing when you
are not using it.

---

## Getting it running

**Prerequisites:** the AWS CLI, configured with credentials. That's it — there
is no build step, no Docker, and no SAM CLI. Every Lambda uses only the Python
standard library plus the `boto3` that AWS already ships in the runtime.

```bash
NOTIFY_EMAIL=you@example.com ./scripts/deploy.sh
```

That takes about 10 minutes the first time, almost all of it CloudFront. Then:

```bash
./scripts/create-user.sh you@example.com   # Cognito emails you a temp password
```

Confirm the SNS subscription email AWS sends you, open the site URL the deploy
printed, sign in, and add a feed:

```
https://lexfridman.com/feed/podcast/
```

Episodes appear on the next scheduled poll, or immediately with:

```bash
./scripts/poll-now.sh
./scripts/status.sh      # watch NEW → DOWNLOADING → TRANSCRIBING → READY
```

---

## About long episodes

Transcribe's free tier is **60 audio-minutes per month**. A single four-hour Lex
Fridman episode is 240 minutes — four times your entire monthly allowance, and
about $5.75 at the standard rate.

So by default PodcastLens transcribes only the **first 10 minutes** of each
episode. It does this with an HTTP `Range` request, downloading just the first
few megabytes of the MP3: an MP3 is a flat sequence of independent frames, so a
prefix of the file is still perfectly playable audio that simply stops early.

Six clips per month therefore fit inside the free tier with room to spare. Two
independent guardrails keep it that way:

| Guard | Where | What it does |
|---|---|---|
| `CLIP_SECONDS` | `scripts/config.sh` | How much of each episode to transcribe |
| `MONTHLY_TRANSCRIBE_SECONDS` | DynamoDB atomic counter | Hard refusal once the month's budget is spent |
| `MAX_EPISODES_PER_POLL` | `src/shared/common.py` | Stops a 400-episode back catalogue flooding the queue |
| AWS Budgets | `02-core.yaml` | Emails you at 50% of $5, and on any forecast overrun |

To transcribe whole episodes, set `CLIP_SECONDS=0` — and read
[docs/COSTS.md](docs/COSTS.md) first.

---

## Layout

```
infrastructure/
  01-auth.yaml     Cognito user pool + hosted sign-in UI
  02-core.yaml     DynamoDB, S3, SQS, SNS, 5 Lambdas, HTTP API, alarms, budget
  03-web.yaml      Private S3 bucket + CloudFront with Origin Access Control
src/
  api/handler.py         All HTTP routes behind the JWT authorizer
  pipeline/poller.py     Scheduled: find new episodes, fan out to SQS
  pipeline/processor.py  SQS consumer: clip → S3 → start Transcribe
  pipeline/completer.py  EventBridge: Transcribe done → Comprehend → index
  digest/weekly.py       Weekly SNS email
  shared/                DynamoDB access patterns, RSS parser
web/                     Vanilla JS single-page app, no build step
scripts/                 deploy, create-user, poll-now, status, logs, teardown
docs/                    Architecture notes, exam mapping, cost breakdown
```

## Scripts

| Command | What it does |
|---|---|
| `./scripts/deploy.sh` | Deploy or update everything. Safe to re-run. |
| `./scripts/create-user.sh EMAIL` | Create a Cognito login |
| `./scripts/poll-now.sh` | Trigger the poller immediately |
| `./scripts/status.sh` | Pipeline health: episode states, budget, DLQ depth |
| `./scripts/logs.sh processor` | Tail one Lambda's logs |
| `./scripts/teardown.sh` | Delete all of it |

## Tests

```bash
python3 tests/test_processor.py
```

Ten tests covering the clip-sizing and byte-limiting logic — the parts that
decide how much audio, and therefore how much money, each episode costs. They
stub out boto3, so they run offline with no credentials and no charges.

## Study material

- [docs/EXAM-MAP.md](docs/EXAM-MAP.md) — every service here mapped to the SAA-C03
  exam domains, with the specific question patterns each one shows up in
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — why each design decision went
  the way it did, and what you would do differently at scale
- [docs/COSTS.md](docs/COSTS.md) — line-by-line free tier accounting

The templates themselves are written to be read. Every resource carries a
comment explaining not just what it is but why it is shaped that way.
