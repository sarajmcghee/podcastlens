# Acceptance checks in your AWS account

These are not marked complete by the local tests. Record the date, region, stack,
commit/archive version, observation, and sanitized evidence for each check.

| Check | Procedure | Pass condition |
|---|---|---|
| Deploy | Run deploy.sh with your non-root identity | Stack complete; runtime roles and outputs exist |
| End to end | Upload your 20-30 second speech recording | One Transcribe job; S3 JSON; COMPLETED metadata |
| Privacy | Try an unsigned URL for the audio/transcript | Access denied |
| Duplicate | Upload the same bytes twice and replay its SQS ID | One job name; one quota reservation |
| Worker failure | Send a malformed test message to JobsQueue | WorkerFailures metric; only bad record retried |
| DLQ | Leave that controlled bad message to exhaust receives | It reaches JobsDLQ; valid episodes still finish |
| Mixed batch | Invoke a sandbox worker test event with one valid episode and one invalid message | batchItemFailures contains only the bad message ID |
| Missing completion | Disable TranscribeEvents rule for one new clip; run reconciliation | Service completion is recovered; re-enable rule |
| Concurrent quota | In an isolated fresh test stack, cap at 15 seconds; submit two distinct 1-second WAVs together | One reserved job; other BLOCKED_BUDGET; counter stays 15 |
| Notifications | Confirm SNS subscription and cause a controlled alarm | Alarm enters ALARM and your email arrives |
| Repeatability | Deploy a second named lab stack using the same code | Independent buckets/table/queue; same behavior |

Do not intentionally inject failures into anything used for real workloads.
Reconciliation runs every five minutes; if testing it manually, allow any active
run to finish first so the evidence is easy to interpret.

## A controlled malformed message

From `cli.py outputs`, copy the `QUEUE_URL`. This costs queue/function requests
but starts no transcription because no episode ID can be parsed:

```bash
aws sqs send-message --queue-url 'YOUR_QUEUE_URL' --message-body '{"test":"bad-schema"}' --region us-east-1
```

With a 120-second Lambda timeout and 720-second visibility timeout, the message
can take roughly an hour or longer to reach the DLQ. Do not shorten visibility
below the worker's retry margin just to make the demonstration faster.

For a deterministic partial-batch test use a Lambda console test event:

```json
{
  "Records": [
    {"messageId": "good", "body": "{\"episodeId\":\"YOUR_EXISTING_COMPLETED_EPISODE_ID\"}"},
    {"messageId": "bad", "body": "not-json"}
  ]
}
```

Expected: only `bad` appears in `batchItemFailures`. A direct console invocation
tests handler behavior; it does not prove actual SQS redrive timing.

## Cost tests

The concurrency example uses two different WAV files to produce distinct IDs.
The quota minimum is 15 seconds, so a 15-second cap admits exactly one short job.
Use an isolated stack and do not erase real reservations to force a test.
Keep the evidence after each test and clean up the isolated stack afterward.
