# What this actually costs

Figures are for `eu-west-1`, and assume the default settings: one feed, a
6-hourly poll, 10-minute clips, and roughly 6 episodes a month.

## Monthly bill, as configured

| Service | Free tier | This project uses | Cost |
|---|---|---|---|
| Lambda | 1M requests + 400,000 GB-s **always free** | ~200 invocations, ~600 GB-s | $0.00 |
| DynamoDB | 25 GB **always free**, on-demand billed per request | a few thousand requests, <1 MB | $0.00 |
| API Gateway (HTTP) | 1M requests/month, first 12 months | a few hundred | $0.00 |
| S3 | 5 GB + 20k GET + 2k PUT, first 12 months | <100 MB after lifecycle | $0.00 |
| CloudFront | 1 TB out + 10M requests **always free** | a few MB | $0.00 |
| Cognito | 10,000 MAU **always free** | 1 | $0.00 |
| SQS | 1M requests **always free** | ~50 | $0.00 |
| SNS | 1,000 email notifications **always free** | ~10 | $0.00 |
| CloudWatch | 10 alarms + 5 GB logs **always free** | 2 alarms, <100 MB | $0.00 |
| X-Ray | 100,000 traces/month **always free** | ~200 | $0.00 |
| **Transcribe** | **60 audio-min/month, first 12 months** | 60 min (6 × 10-min clips) | **$0.00** |
| Comprehend | 50,000 units/month, first 12 months | ~2,700 units | $0.00 |
| AWS Budgets | 2 budgets free | 1 | $0.00 |
| **Total** | | | **$0.00** |

"Always free" means forever. "First 12 months" means from the date you opened
the AWS account — **not** from when you deploy this. If your account is older
than a year, S3, API Gateway, Transcribe and Comprehend all start billing.

## The one line item that can actually bite you

**Amazon Transcribe: $0.024 per minute** after the free tier.

| Scenario | Audio minutes | Cost |
|---|---|---|
| Default: 6 clips × 10 min | 60 | $0.00 (free tier) |
| Same, on an account >12 months old | 60 | $1.44 |
| `CLIP_SECONDS=0`, one 4-hour Lex episode | 240 | $5.76 |
| `CLIP_SECONDS=0`, four episodes a month | 960 | $23.04 |
| A 400-episode back catalogue at 3h each | 72,000 | **$1,728** |

That last row is why `MAX_EPISODES_PER_POLL` defaults to 1 and why
`claim_transcribe_budget` reserves capacity *before* starting a job rather than
checking afterwards. Both guardrails exist specifically to make that row
impossible to reach by accident.

## If your account is older than 12 months

Realistic cost at default settings: **about $1.50–2.00/month**, essentially all
Transcribe. To get back to zero, either lower `CLIP_SECONDS` to 300 or set
`MONTHLY_TRANSCRIBE_SECONDS=0` to pause transcription entirely while keeping
the rest of the pipeline running.

## Things that would break the $0 promise

None of these are in the project. They are the usual ways a study account
starts costing money:

- **NAT Gateway** — ~$32/month plus data processing, and it bills whether or not
  traffic flows. The most common surprise on any AWS bill.
- **Idle EC2 or RDS** — a `t3.micro` is free tier for 12 months; anything larger,
  or a second one, is not.
- **Elastic IPs not attached to a running instance** — ~$3.60/month each.
- **KMS customer-managed keys** — $1/month per key, plus per-request charges.
- **Secrets Manager** — $0.40/secret/month. SSM Parameter Store Standard is free.
- **CloudWatch log groups with no retention** — they default to *never expire*.
- **Interface VPC endpoints** — ~$7/month each. Gateway endpoints (S3, DynamoDB)
  are free.

## Checking your actual spend

```bash
aws ce get-cost-and-usage \
  --time-period Start=$(date -u -v1d +%Y-%m-%d),End=$(date -u +%Y-%m-%d) \
  --granularity MONTHLY --metrics UnblendedCost \
  --group-by Type=DIMENSION,Key=SERVICE \
  --query 'ResultsByTime[0].Groups[?Metrics.UnblendedCost.Amount!=`0`].[Keys[0],Metrics.UnblendedCost.Amount]' \
  --output table --region us-east-1
```

Cost Explorer's API lives in `us-east-1` regardless of where your resources are,
and the first Cost Explorer API call each month is not free — it is $0.01 per
request. Use the console for casual checks.
