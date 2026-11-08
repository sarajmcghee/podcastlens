# Where to look in the AWS Console

Two things catch everyone out first:

1. **The region selector must say Ireland (eu-west-1).** The console shows one
   region at a time. On the wrong region your account looks completely empty.
   The links below all pin the region for you.
2. **There is nothing in EC2.** No instances, no load balancers, no volumes,
   no security groups of your own. That is not a deployment failure — this
   architecture has no servers in it. If you are used to looking for an EC2
   instance to confirm something is running, that habit has to go.

## Start here: CloudFormation

**[Stacks →](https://eu-west-1.console.aws.amazon.com/cloudformation/home?region=eu-west-1#/stacks)**

This is the "did it deploy" screen. Three stacks, all `CREATE_COMPLETE`:
`podcastlens-auth`, `podcastlens-core`, `podcastlens-web`.

Click `podcastlens-core` and use these tabs:

| Tab | What it tells you |
|---|---|
| **Resources** | Every resource the stack created, with a link to each one. This is the fastest way to navigate the whole project. |
| **Events** | Chronological deploy log, newest first. When a deploy fails, the *oldest* `CREATE_FAILED` row is the real cause — everything after it is just cancellation noise. |
| **Outputs** | Your API URL, table name, bucket names. |
| **Template** | The YAML that produced all of it. |

Get comfortable with the Events tab. Reading it correctly is most of debugging
CloudFormation.

## The rest, service by service

| Service | What you will see | Link |
|---|---|---|
| **Lambda** | 5 functions: api, poller, processor, completer, digest. Open one → **Monitor** tab → **View CloudWatch logs**. | [Functions](https://eu-west-1.console.aws.amazon.com/lambda/home?region=eu-west-1#/functions) |
| **DynamoDB** | One table. **Explore table items** shows the raw single-table design — feeds, episodes, keywords and the budget counter all sharing one table. | [Tables](https://eu-west-1.console.aws.amazon.com/dynamodbv2/home?region=eu-west-1#tables) |
| **S3** | Three buckets: `-site` (the web app), `-media` (audio + transcripts), `-artifacts` (Lambda zips). All private. | [Buckets](https://s3.console.aws.amazon.com/s3/buckets?region=eu-west-1) |
| **API Gateway** | One HTTP API with 6 routes and a JWT authorizer attached. | [APIs](https://eu-west-1.console.aws.amazon.com/apigateway/main/apis?region=eu-west-1) |
| **SQS** | The episode queue and its dead letter queue. Watch **Messages available** while the pipeline runs. | [Queues](https://eu-west-1.console.aws.amazon.com/sqs/v3/home?region=eu-west-1#/queues) |
| **Cognito** | Your user pool, its app client, and the **Users** list. | [User pools](https://eu-west-1.console.aws.amazon.com/cognito/v2/idp/user-pools?region=eu-west-1) |
| **Amazon Transcribe** | **Transcription jobs** — watch one go `IN_PROGRESS` → `COMPLETED`. | [Jobs](https://eu-west-1.console.aws.amazon.com/transcribe/home?region=eu-west-1#jobs) |
| **EventBridge** | The schedule that fires the poller, and the rule matching Transcribe completion events. | [Rules](https://eu-west-1.console.aws.amazon.com/events/home?region=eu-west-1#/rules) |
| **CloudWatch** | Log groups per function, and 2 alarms. | [Logs](https://eu-west-1.console.aws.amazon.com/cloudwatch/home?region=eu-west-1#logsV2:log-groups) |
| **Step Functions** | Nothing. Worth knowing this project does not use it. | — |

## The two global services

CloudFront and Billing are **not** regional. Their consoles always show
`us-east-1` in the URL no matter where your resources live — that is correct and
not a mistake.

- **[CloudFront distributions →](https://us-east-1.console.aws.amazon.com/cloudfront/v4/home#/distributions)**
  One distribution. Its domain name is your site URL.
- **[Billing & Free Tier →](https://us-east-1.console.aws.amazon.com/billing/home#/freetier)**
  The single most useful page in the console for a study account. Shows free
  tier usage per service as a percentage. Check it weekly.
- **[Budgets →](https://us-east-1.console.aws.amazon.com/billing/home#/budgets)**
  The $5 budget this project created.

That CloudFront and Billing are global while everything else is regional is
itself an exam fact. So is the related one: **CloudFront can only use an ACM
certificate issued in us-east-1**, regardless of where the origin lives.

## Watching the pipeline actually run

```bash
./scripts/poll-now.sh    # trigger ingestion immediately
./scripts/status.sh      # episode states, budget used, DLQ depth
./scripts/logs.sh processor
```

Then follow it in the console: SQS **Messages available** ticks up, the
processor Lambda's **Invocations** metric spikes, a job appears in Transcribe,
and a few minutes later the episode item in DynamoDB flips to `READY`.

Seeing one request move across five services is worth more than any diagram.
