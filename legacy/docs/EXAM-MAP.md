# What this project teaches you for SAA-C03

The exam is four domains. Here is where each one shows up in this codebase, and
the specific question patterns the service tends to appear in.

---

## Domain 1 — Design Secure Architectures (30%)

| In this project | File | The exam pattern behind it |
|---|---|---|
| Cognito user pool, hosted UI, PKCE | `01-auth.yaml`, `web/app.js` | "Users must authenticate to a web app without you managing passwords." User pools = authentication. Identity pools = temporary AWS credentials. Know the difference; it is asked constantly. |
| API Gateway JWT authorizer | `02-core.yaml` → `HttpApi.Auth` | "Reject unauthenticated requests before they reach compute." Validation happens at the API layer; the Lambda never runs and you are not billed. |
| One IAM role per Lambda, scoped | every `Policies:` block | "Least privilege." Notice the Transcribe policy is scoped to `transcription-job/pl-*`, not `*`. Scoping to resource ARNs is almost always the right answer. |
| S3 Block Public Access + OAC | `03-web.yaml` | "Serve a static site securely." The wrong answer is a public bucket with website hosting. The right one is a private bucket + CloudFront + Origin Access Control. |
| `aws:SecureTransport` deny | `02-core.yaml` → `MediaBucketPolicy` | "Enforce encryption in transit." A bucket policy condition, not a setting. |
| SSE-S3 with bucket keys | both buckets | Encryption at rest. Know the ladder: **SSE-S3** (free, AWS-managed) → **SSE-KMS** (your key policy, ~$1/mo + request charges) → **SSE-C** (you supply the key each request) → **client-side**. |
| `AdminCreateUserOnly` | `01-auth.yaml` | Self-registration disabled. Otherwise your sign-in page is an open registration form on the public internet. |

**Also know but not built here:** IAM Identity Center vs IAM users, SCPs and
Organizations, KMS key rotation, Secrets Manager vs SSM Parameter Store
(Secrets Manager rotates and costs $0.40/secret/month; Parameter Store Standard
is free and does not).

---

## Domain 2 — Design Resilient Architectures (26%)

| In this project | File | The exam pattern behind it |
|---|---|---|
| SQS between poller and processor | `02-core.yaml` → `EpisodeQueue` | **The single most-tested pattern on the exam.** A queue between a fast producer and a slow consumer gives you retries, buffering and back-pressure. If a question says "decouple", the answer is SQS or SNS. |
| Dead letter queue, `maxReceiveCount: 3` | `EpisodeDlq` | "What happens to messages that keep failing?" They must not retry forever and they must not vanish. |
| Visibility timeout ≥ consumer timeout | `VisibilityTimeout: 960` | Classic distractor: a queue whose visibility timeout is shorter than the Lambda's timeout will hand the same message to a second consumer while the first is still working. |
| `ReportBatchItemFailures` | `processor.py` | Partial batch failure. Without it, one poison message causes the whole batch to be redelivered and the successes to be reprocessed. |
| Conditional writes for idempotency | `put_episode_if_new` | "How do you prevent duplicate processing in a distributed system?" `attribute_not_exists` — no lock, no coordinator. |
| Event-driven completion | `completer.py` + EventBridge rule | Nothing polls Transcribe for its status. The service emits an event; a rule routes it. Polling is almost always the wrong answer. |
| DynamoDB point-in-time recovery | `Table` | 35 days of continuous backup, restorable to the second. |
| Multi-AZ by default | (implicit) | S3, DynamoDB, SQS, Lambda are all regional and already replicated across AZs. Knowing which services you *don't* have to make highly available is worth several questions. |

---

## Domain 3 — Design High-Performing Architectures (24%)

| In this project | File | The exam pattern behind it |
|---|---|---|
| CloudFront in front of S3 | `03-web.yaml` | "Reduce latency for global users." Caching at the edge. |
| Managed cache + response-headers policies | `CachePolicyId`, `ResponseHeadersPolicyId` | Current best practice over hand-written cache behaviours. |
| DynamoDB single-table design + GSI | `shared/common.py` | Model the *queries*, not the entities. The GSI turns "find every episode in state X" from a table scan into a query. |
| Inverted keyword index | `index_keywords` | Search without OpenSearch. Understand *why* the naive approach (scanning transcripts) does not scale. |
| Blobs in S3, index in DynamoDB | `_snippet` | Items cap at 400 KB and you pay per byte written. Large objects go in S3 with a pointer in the database — a recurring exam answer. |
| Graviton (`arm64`) Lambdas | `Globals` | ~20% better price-performance for the same code. |
| Lambda memory ⇒ CPU and network | `ProcessorFunction: 1024` | Memory is the only performance dial. More memory means more CPU *and* more network throughput, so a bigger function can finish faster and cost less. |
| HTTP API, not REST API | `AWS::Serverless::HttpApi` | ~70% cheaper and lower latency. REST API wins only when you need request validation, usage plans and API keys, WAF, or private endpoints. |
| X-Ray tracing | `Tracing: Active` | "How do you find the slow hop in a distributed call?" |

---

## Domain 4 — Design Cost-Optimized Architectures (20%)

| In this project | File | The exam pattern behind it |
|---|---|---|
| Serverless everywhere | all of it | No idle cost. Compare against an EC2 instance doing the same work at ~$8/month whether or not anything happens. |
| DynamoDB on-demand | `PAY_PER_REQUEST` | Bursty and unpredictable ⇒ on-demand. Steady and high ⇒ provisioned + auto scaling, ~7× cheaper at sustained load. |
| S3 lifecycle expiration | `expire-audio-clips` | Audio is deleted 3 days after ingest. Storage you forget about is the classic hobby-account bill. |
| Abort incomplete multipart uploads | `abort-incomplete-uploads` | Failed uploads leave paid-for parts that do **not** appear in the console. Everyone learns this once. |
| CloudWatch log retention | every `LogGroup` | Log groups default to *never expire*. Setting retention is free and it is on the exam. |
| `PriceClass_100` | `03-web.yaml` | Fewer CloudFront edge locations, lower price. |
| AWS Budgets, ACTUAL + FORECASTED | `Budget` | Forecast alerts catch a runaway before the money is gone. |
| Application-level cost guardrail | `claim_transcribe_budget` | An atomic counter that refuses work past a threshold. Not an exam topic — a real-world one. |

---

## Things the exam tests that this project deliberately does not use

Serverless architectures skip a lot of the exam. Study these separately:

- **VPC**: subnets, route tables, IGW vs NAT gateway, security groups (stateful,
  allow-only) vs NACLs (stateless, allow *and* deny), VPC endpoints — Gateway
  endpoints for S3/DynamoDB are free, Interface endpoints cost per hour.
- **EC2 & Auto Scaling**: launch templates, target tracking, warm pools.
  Purchasing options: On-Demand, Reserved, Savings Plans, Spot.
- **ELB**: ALB (layer 7, host/path routing) vs NLB (layer 4, static IP, extreme
  throughput) vs GWLB (appliances).
- **RDS & Aurora**: Multi-AZ (synchronous, failover, **not** for reads) vs read
  replicas (asynchronous, for reads). This distinction is asked constantly.
- **Storage**: EBS volume types, EFS vs FSx, Storage Gateway.
- **Migration**: Snowball, DMS, DataSync.
- **Route 53**: routing policies — simple, weighted, latency, failover,
  geolocation, geoproximity, multivalue.

A reasonable next step after this project: rebuild the same features on EC2
behind an ALB in a custom VPC, and watch the bill go from $0 to ~$35/month. The
comparison teaches Domain 4 better than any flashcard.
