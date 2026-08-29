# Design decisions, and what you would do differently

Every choice here has a "correct at this scale" answer and a "correct at real
scale" answer. Knowing both is roughly what the exam is testing.

---

## Why a queue between the poller and the processor

The poller is fast, predictable and finishes in seconds. The processor
downloads tens of megabytes from a podcast CDN and can take minutes. Wiring them
directly would mean one slow download blocks the whole poll, and any failure
loses the episode.

The queue buys four things at once: **retries** (SQS redelivers automatically),
**a dead letter queue** (failures are parked, not lost), **back-pressure** (a
thousand new episodes queue up instead of hammering Transcribe), and
**independent scaling** (Lambda adds consumers as the queue grows).

When the exam says *decouple*, this is the shape it means.

**At real scale:** add `MaximumConcurrency` on the event source mapping so a
backlog cannot exhaust your account's Lambda concurrency and starve the API.

---

## Why the completer is event-driven, not polled

Transcribe jobs take minutes. The obvious implementation — a loop calling
`GetTranscriptionJob` until it returns COMPLETED — burns Lambda duration doing
nothing, and hits API rate limits under load.

Instead, Transcribe publishes a **Transcribe Job State Change** event to the
default EventBridge bus, and a rule pattern-matches it to invoke the completer.
Zero cost while waiting, and no code to write.

Nearly every AWS service emits events like this. If a design polls for a state
change that a service already publishes, it is the wrong design.

---

## Why one DynamoDB table

Four entity types share one table:

```
Feed      PK=USER#<sub>            SK=FEED#<feedId>
Episode   PK=FEED#<feedId>         SK=EP#<publishedAt>#<episodeId>
          GSI1PK=STATUS#<status>   GSI1SK=<updatedAt>
Keyword   PK=KW#<term>             SK=REF#<feedId>#<episodeId>
Budget    PK=SYSTEM                SK=BUDGET#<YYYY-MM>
```

Sorting episodes by `publishedAt` inside the sort key means "the 20 most recent
episodes of this feed" is one query against one partition, already in order. No
sort, no filter, no scan. That is what people mean by *modelling your access
patterns*: the key schema **is** the query plan.

The GSI exists for exactly one question — "which episodes are in state X?" —
which the base table cannot answer without a scan.

**At real scale:** `KW#<term>` partitions become hot for common words. You would
either shard them (`KW#<term>#<0-9>`) or move search to OpenSearch. And
`scan_all_feeds` in the poller is a genuine scan; past a few thousand feeds you
would add a sparse GSI on `active` and query that instead. It is a scan here
because at ten feeds a scan is cheaper and simpler than an index you have to
maintain — the right tradeoff to make consciously, not by accident.

---

## Why transcripts live in S3 and only the index lives in DynamoDB

A DynamoDB item caps at 400 KB, and you pay per byte written. A one-hour
transcript is comfortably over that.

So: the **transcript text** goes to S3, the **keywords and metadata** go to
DynamoDB, and the DynamoDB item holds the S3 key. Search queries the index in
DynamoDB, then fetches only the matching transcripts from S3 to cut snippets.

Large blobs in object storage, small searchable metadata in the database, a
pointer between them. This exact pattern is a recurring exam answer.

---

## Why the front end is a private bucket behind CloudFront

S3 static website hosting requires a public bucket and only speaks HTTP.
Instead the bucket blocks all public access, and CloudFront authenticates to it
with **Origin Access Control** — SigV4-signed origin requests, pinned by bucket
policy condition to this one distribution.

You get HTTPS, edge caching, HTTP/3, compression and managed security headers,
and the bucket is unreachable except through the CDN.

OAC replaced Origin Access Identity. If you see OAI in a practice question, it
is either an old question or a distractor.

---

## Why a single API Lambda instead of one per route

This is a "Lambdalith": six routes, one function, one IAM role. It means fewer
cold starts (one warm function serves everything), less template, and one place
to look in the logs.

The cost is that the role is the **union** of what every route needs — the
search route can technically write to DynamoDB because the add-feed route must.

**When to split:** when one route needs materially broader permissions than the
others, when routes have very different memory or timeout profiles, or when one
route's traffic would starve the rest. None of those are true at this size.

Note that the *pipeline* functions are already separate, and for exactly that
reason: the processor needs Transcribe and S3 write; the poller needs neither.

---

## Why three stacks instead of one

Different lifecycles:

- **01-auth** holds your users. It changes almost never, and deleting it is a
  destructive event, so it gets its own blast radius and deletion protection.
- **02-core** is where all the iteration happens. Updates in ~60 seconds.
- **03-web** contains CloudFront, which takes 5–10 minutes to create. Putting it
  in the core stack would make every code change slow.

They communicate through CloudFormation **Exports** and `Fn::ImportValue`. The
cost of that coupling is real and worth understanding: you cannot delete a stack
whose exports are still imported, which is why `teardown.sh` deletes in reverse
order.

There is a genuine chicken-and-egg problem in the middle: Cognito must whitelist
the CloudFront URL as a sign-in redirect, but that URL does not exist until
stack 3 is created. `deploy.sh` resolves it by deploying stack 1 with a
placeholder and coming back to update it in step 4. Circular dependencies
between stacks are common in real infrastructure and this is the standard fix.

---

## Why clip-based transcription

Covered in [COSTS.md](COSTS.md), but the technique is worth noting on its own:
the processor sends an HTTP `Range: bytes=0-N` header to the podcast CDN and
receives only the first N bytes of the MP3.

This works because MP3 is a flat sequence of independent frames with no global
index — a prefix of the file is valid audio that simply stops early. The
required byte count is derived from the RSS enclosure, which usually declares
both the file size and the duration, giving us the real bitrate.

The `LimitedReader` wrapper enforces the ceiling locally as well, because
`Range` is a request the origin is free to ignore. Never let an external service
decide how much of your budget to spend.

---

## What is deliberately missing

- **A VPC.** Nothing here needs one. Lambda outside a VPC has no cold-start ENI
  penalty and no NAT gateway bill. You would add one only to reach a private
  resource such as RDS or ElastiCache.
- **WAF.** ~$5/month base plus per-rule and per-request charges. Correct for a
  production public endpoint, overkill for a single-user study project.
- **A custom domain.** Route 53 hosted zones are $0.50/month and CloudFront
  requires the ACM certificate to be issued in `us-east-1` — worth doing once,
  purely to learn that second detail.
- **CI/CD.** `deploy.sh` is idempotent and would drop into a CodePipeline or a
  GitHub Actions workflow with an OIDC role almost unchanged.
