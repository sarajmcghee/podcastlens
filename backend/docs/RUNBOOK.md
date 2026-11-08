# PodcastLens runbook

## Find the job

Use `cli.py status EPISODE_ID`, the DynamoDB row, the worker's CloudWatch log,
and the deterministic Transcribe job name. Logs contain identifiers and error
categories, not transcript bodies or credentials. Inspect Transcribe directly
for its detailed service failure explanation.

## Queue age or worker failures

1. Check arrivals, worker failures, throttles, mapping health, and downstream
   Transcribe quotas. A low Lambda Errors count is insufficient because partial
   batch failures are emitted through WorkerFailures.
2. Inspect the specific error category and relevant IAM/resource configuration.
3. If needed, disable the worker event-source mapping and feed schedule to stop
   new processing while investigating. Disabling feeds alone does not drain or
   pause messages already queued. Existing Transcribe jobs can continue.
4. Repair the cause, re-enable processing, and watch queue age and job completion.

## Episode DLQ

The source queue moves failures to JobsDLQ after five receives. Read its messages
without purging the queue. For an application failure, fix code/configuration
and redrive the affected messages through the SQS console. An intentionally
malformed demo message should be deleted individually after evidence is saved;
redriving that unchanged malformed message will just fail again.

Do not replace an existing episode's content or generate a fresh Transcribe name
as a generic retry. Same-episode retries recover the original job/reservation.

## Operations DLQ

This queue contains EventBridge target delivery failures and Lambda asynchronous
failure records. Inspect the envelope to identify Scanner, Collector, or
Reconciler. A failed collector can usually be repaired with `cli.py reconcile`.
Do not bulk-redrive this mixed queue to JobsQueue: its payloads have different
schemas and purposes. Reinvoke the correct function with its original event
only after identifying and fixing the failure.

## Stuck SUBMITTING or PROCESSING

Run `cli.py reconcile`. If a job exists, it synchronizes service state. If a
SUBMITTING job does not exist, inspect the episode DLQ and worker logs; recovery
does not automatically restart a poison message forever. Check the exact
InputBucket and job name if service state is unexpectedly absent.

## Budget rejection

`cli.py budget` shows this UTC month's reserved seconds. BLOCKED_BUDGET creates
no new Transcribe job. Increase the cap deliberately through CloudFormation or
wait until a new month, then run `cli.py retry-budget EPISODE_ID`. That command
only allows previously unreserved BLOCKED_BUDGET rows. It may be rejected again
if capacity is still insufficient. Do not clear uncertain reservations: a prior
job may already be billable.

## Feed failures

Check RSS validity, allowed hosts (including redirects/CDNs), HTTPS, download
size, and episode duration. Atom, authenticated feeds and compressed HTTP feeds
are not supported. Unknown hosts/private addresses are rejected. Download errors
are retried during later scans; rejected media gets FAILED/MEDIA_REJECTED.
If you deliberately change the duration limit, a rejected row requires manual
inspection and deliberate re-ingestion; do not delete legitimate episode records.

## Rollback and cleanup

For rollback, deploy the previous tested source archive. Do not assume rolling
back code reverses metadata or quota changes. Keep stable job IDs across versions.

For complete lab cleanup:

1. Disable the feed schedule and worker mapping. Inspect active Transcribe work;
   wait for active jobs to finish before removing their output locations.
2. Export any transcripts/evidence you want to retain.
3. Empty only this stack's input/output buckets, then delete the CloudFormation
   stack. The template deletes its table and queued messages too; this is destructive.
4. Remove only this stack's prefix from the separate artifact bucket. Keep other
   projects' artifacts. Delete that bucket only if empty and no longer used.
5. Check CloudFormation Events for delete failures and confirm that functions,
   queues, alarms, schedules and data resources are gone. Service job metadata may
   remain in Transcribe and can be removed separately after completion.
6. Check billing later; billing reports can lag. Stored data and out-of-stack
   artifacts can keep incurring costs after processing is stopped.
