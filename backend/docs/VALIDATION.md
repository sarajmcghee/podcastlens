# Validation record

Recorded November 8, 2026. Backend stack: `podcast-lense-dev`.
Region: `us-east-1`. This is a personal development deployment.

The AWS observations below came from commands run by the operator in CloudShell,
pasted output, and the operator's reports from the console/email. The repository
preparation process did not independently connect to the AWS account. Identifiers,
account numbers, recipient addresses and resource suffixes are omitted from
the public examples.

## Source and local checks

The original pipeline Python modules, CLI, CloudFormation template, deployment
script, dependency pins and tests match the deployed source package and are now
under `backend/`. A dashboard API module, companion web stack, frontend integration
and tests were added. The existing visual design is retained; the old unused
backend is preserved under `legacy/` and is not deployed by the new scripts.

Original package: `PodcastLens_AWS_Project.zip`.

SHA-256:
`f9c788c7a34eca7086110260b9f237fe76895864fdb742d8912713fcf7e2b7ca`.

Local checks on the integrated repository:
- Backend/API pytest suite: **55 passed** (36 original tests plus 19 API checks).
- Existing backend tests under `legacy/`: **17 passed**.
- Deployment contract checks: **3 passed**.
- Both pipeline and web CloudFormation templates: lint passed.
- Python deployment-script compilation, shell/JavaScript syntax, workflow YAML,
  CLI help and diff whitespace checks passed.
- Browser suite added for signed-out access, existing recording/status/budget
  display, safe transcript rendering/search, feed/scan requests, session expiry,
  PKCE and OAuth state handling. Local Chromium startup was blocked by the
  execution environment's socket restrictions. All **6 browser checks passed in
  GitHub Actions**, along with the backend and deployment checks, in
  [run 42493338452](https://github.com/sarajmcghee/podcastlens/actions/runs/42493338452)
  for implementation commit `4b37d9be5e2b5c93cfbd6ceddabc60e0751d8531`.

The backend suite uses Moto for storage/queue integration and a deterministic
Transcribe fake. It covers identity, duplicate delivery, media validation,
atomic quota rollback, reservation reuse across months, recovery after service
acceptance, terminal-state protection, partial batches, missed-event recovery,
outbox repair and feed/network controls. These tests do not substitute for
actual service integration, distributed concurrency or billing verification.

## AWS observations

| Check | Evidence | Result and scope |
|---|---|---|
| Deployment | CloudShell reported successful creation/update of the named stack and its outputs. | Observed. |
| First recording | Episode status returned `COMPLETED`; transcript search returned the spoken word "hummingbird" and its surrounding sentence. | Observed upload-to-transcript/search path. |
| Repeated upload | Same episode ID and `COMPLETED` record returned; `enqueuePending` was false. | Observed reuse of the existing episode. |
| Reservation after duplicate | At this stage the budget was 15 seconds used of a 600-second cap. | Observed single reservation; this is not a final monthly usage snapshot or a cap-exhaustion test. |
| SNS delivery | Operator confirmed the subscription and received a direct test publication. | Operator-reported receipt. |
| CloudWatch notification action | A controlled `set-alarm-state` on JobsDLQAlarm produced an email. | Notification wiring observed; manually setting state does not test real DLQ redrive. |
| Partial-batch handler | Direct invocation with one malformed message and one completed episode returned only `bad` in `batchItemFailures`. | Observed handler behavior. The good message reused a completed episode. |
| Actual SQS failure | A malformed message was sent to JobsQueue; a worker log with the same message ID reported `KeyError` / `PROCESSING_ERROR`. | One processing attempt observed. Later attempts and DLQ arrival remain unconfirmed. |
| Worker-failure alert | Operator reported receiving a WorkerFailuresAlarm email during the controlled tests. | Metric-to-alarm notification observed; either controlled failure can contribute to the metric. |
| Follow-up recording status | Operator reported completion in the console and via CLI after the second-recording exercise was requested. | Reported completion; no second episode ID, raw result or timing evidence was supplied. Concurrent progress during retries is not independently verified. |
| Queue-age alert | Operator reported a QueueAge alarm email during the failure exercise. | Notification reported. Its cause and queue state have not yet been inspected. |

### Small excerpts from the supplied output

Transcript search:

```json
[
  {
    "offset": 22,
    "context": "i would like to see a hummingbird today."
  }
]
```

Reservation after the repeated upload:

```json
{
  "month": "2026-11",
  "capSeconds": "600",
  "reservation": {
    "pk": "BUDGET#2026-11",
    "usedSeconds": "15"
  }
}
```

Partial-batch handler response:

```json
{"batchItemFailures": [{"itemIdentifier": "bad"}]}
```

Actual queued-message failure, with the message ID redacted:

```json
{
  "event": "worker_failure",
  "messageId": "<SQS_TEST_MESSAGE_ID>",
  "errorType": "KeyError",
  "errorCode": "PROCESSING_ERROR"
}
```

## Pending acceptance checks

- **Actual DLQ arrival:** the receive-message command was supplied, but its output
  has not been provided. Match the original test message ID and body, save
  evidence, then remove only that test message. Do not mark this passed based
  on the manually triggered DLQ alarm.
- **Retry progression:** capture additional attempts for the same message if
  needed; only one matching failure log was supplied.
- **Queue-age investigation:** inspect the outstanding messages and correlate
  timing before attributing the alert solely to the controlled failure.
- **Distinct recording during retries:** preserve both episode IDs and timestamps
  to demonstrate that valid new work completes during the failure exercise.
- **Monthly cap enforcement and concurrent submissions on AWS:** run the isolated
  test-stack procedure in [ACCEPTANCE_TESTS.md](ACCEPTANCE_TESTS.md). The 15-second
  usage result tests reservation reuse, not rejection at the limit.
- **Actual RSS ingestion:** configure a permitted feed and observe discovery,
  ingest and repeat-scan behavior. The feed schedule starts disabled.
- **Missed completion recovery on AWS:** exercise reconciliation with the
  collector event intentionally withheld, then restore event delivery.
- **Unsigned S3 access, repeat deployment, load/soak and disaster recovery:**
  not demonstrated in this session.
- **Dashboard AWS deployment:** the frontend now uses the new API and existing
  pipeline schema in code. Deploy the companion stack and perform the root
  README's sign-in/status/transcript/feed smoke tests before marking live
  integration passed. The assistant did not deploy it to the AWS account.

Use [ACCEPTANCE_TESTS.md](ACCEPTANCE_TESTS.md) for procedures and
[RUNBOOK.md](RUNBOOK.md) for investigation/cleanup. This record documents a
development exercise; it does not claim production operating history.
