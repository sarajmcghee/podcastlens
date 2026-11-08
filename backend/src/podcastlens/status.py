import os
from datetime import datetime, timezone

import boto3

from podcastlens.core import identity, job_name, log, metric
from podcastlens.store import Store, enqueue
from podcastlens.worker import find_job, sync_job


def collector(event, context):
    name = event.get("detail", {}).get("TranscriptionJobName", "")
    prefix = "pl-" + identity(os.environ["INPUT_BUCKET"])[:8] + "-"
    if not name.startswith(prefix):
        return {"ignored": True}
    episode_id = name[len(prefix):]
    store = Store()
    record = store.get(episode_id)
    if not record:
        raise ValueError("Missing episode metadata")
    job = find_job(boto3.client("transcribe"), name)
    if not job:
        raise ValueError("Missing service job")
    sync_job(store, record, job)
    log("completion_handled", episodeId=episode_id)
    return {"episodeId": episode_id}


def reconcile(event, context):
    store, client = Store(), boto3.client("transcribe")
    checked = stale = 0
    # Persist a cursor per status to avoid permanently starving later pages.
    for state in ["QUEUED", "SUBMITTING", "PROCESSING"]:
        cursor_key = "CURSOR#" + state
        saved = store.get_key(cursor_key) or {}
        # Cursor is serialized as regular JSON to preserve DynamoDB wire types.
        import json
        from podcastlens.store import marshal
        cursor = json.loads(saved["cursor"]) if saved.get("cursor") else None
        records, next_cursor = store.by_status(state, limit=50, cursor=cursor)
        for record in records:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(record["updatedAt"])).total_seconds()
            if record.get("enqueuePending") and state == "QUEUED":
                enqueue(store, record)
            elif state in {"SUBMITTING", "PROCESSING"}:
                job = find_job(client, job_name(os.environ["INPUT_BUCKET"], record["episodeId"]))
                if job:
                    # Don't refresh updatedAt for unchanged PROCESSING: age must
                    # represent progress, not the last polling attempt.
                    new_state = job["TranscriptionJobStatus"]
                    if new_state in {"COMPLETED", "FAILED"} or state == "SUBMITTING":
                        sync_job(store, record, job)
                # Never automatically requeue a submitted/poison message here.
                # SQS owns its retry/DLQ lifecycle; operators repair DLQ failures.
            checked += 1
            if age > 1800 and (store.get(record["episodeId"]) or {}).get("status") not in {"COMPLETED", "FAILED", "BLOCKED_BUDGET"}:
                stale += 1
        store.db.put_item(TableName=store.name, Item=marshal({"pk": cursor_key, "cursor": json.dumps(next_cursor) if next_cursor else ""}))
    metric("StaleJobs", stale)
    log("reconciliation_finished", checked=checked, stale=stale)
    return {"checked": checked, "stale": stale}
