import hashlib
import json
import os

import boto3
from botocore.exceptions import ClientError

from podcastlens.core import TERMINAL, inspect_audio, job_name, log, metric, month, validate_id
from podcastlens.store import Store, code


def find_job(client, name):
    try:
        return client.get_transcription_job(TranscriptionJobName=name)["TranscriptionJob"]
    except ClientError as exc:
        # GetTranscriptionJob returns BadRequestException for a nonexistent job.
        message = exc.response.get("Error", {}).get("Message", "").lower()
        if code(exc) == "BadRequestException" and ("not found" in message or "couldn't be found" in message or "does not exist" in message):
            return None
        if code(exc) == "NotFoundException":
            return None
        raise


def sync_job(store, record, job):
    expected = f"s3://{os.environ['INPUT_BUCKET']}/{record['objectKey']}"
    if job.get("Media", {}).get("MediaFileUri") != expected:
        raise ValueError("Job identity/media mismatch")
    state = job["TranscriptionJobStatus"]
    if state == "COMPLETED":
        store.transition(record["episodeId"], "COMPLETED", transcriptKey=f"transcripts/{record['episodeId']}.json")
    elif state == "FAILED":
        # Raw failure text can contain file URLs; retain a category, inspect AWS for details.
        store.transition(record["episodeId"], "FAILED", errorCode="TRANSCRIBE_FAILED")
        metric("TranscriptionFailures")
    else:
        store.transition(record["episodeId"], "PROCESSING")


def process(episode_id, store=None, s3=None, transcribe=None):
    validate_id(episode_id)
    store, s3, transcribe = store or Store(), s3 or boto3.client("s3"), transcribe or boto3.client("transcribe")
    record = store.get(episode_id)
    if not record:
        raise ValueError("Unknown episode")
    if record["status"] in TERMINAL:
        return record["status"]
    name = job_name(os.environ["INPUT_BUCKET"], episode_id)
    existing = find_job(transcribe, name)
    if existing:
        if "reservationMonth" not in record:
            raise ValueError("Existing service job has no local quota reservation")
        sync_job(store, record, existing)
        return "RECOVERED"
    limit = int(os.environ.get("MAX_MEDIA_BYTES", "26214400"))
    response = s3.get_object(Bucket=os.environ["INPUT_BUCKET"], Key=record["objectKey"])
    body = response["Body"]
    try:
        if response.get("ContentLength", 0) > limit:
            raise ValueError("Media too large")
        data = body.read(limit + 1)
    finally:
        body.close()
    if len(data) > limit or hashlib.sha256(data).hexdigest() != record["contentHash"]:
        raise ValueError("Media too large or hash mismatch")
    media_format, seconds = inspect_audio(data, int(os.environ.get("MAX_EPISODE_SECONDS", "300")))
    if not store.reserve(episode_id, seconds, month(), int(os.environ.get("MONTHLY_CAP_SECONDS", "600"))):
        metric("BudgetRejections")
        return "BLOCKED_BUDGET"
    params = dict(TranscriptionJobName=name, LanguageCode=os.environ.get("LANGUAGE_CODE", "en-US"),
                  Media={"MediaFileUri": f"s3://{os.environ['INPUT_BUCKET']}/{record['objectKey']}"},
                  MediaFormat=media_format, OutputBucketName=os.environ["OUTPUT_BUCKET"],
                  OutputKey=f"transcripts/{episode_id}.json", JobExecutionSettings={
                      "AllowDeferredExecution": True, "DataAccessRoleArn": os.environ["TRANSCRIBE_ROLE_ARN"]})
    try:
        job = transcribe.start_transcription_job(**params)["TranscriptionJob"]
    except ClientError as exc:
        if code(exc) != "ConflictException":
            raise
        job = find_job(transcribe, name)
        if not job:
            raise
    sync_job(store, store.get(episode_id), job)
    log("job_submitted", episodeId=episode_id, jobName=name)
    return "SUBMITTED"


def handler(event, context):
    failures = []
    for message in event.get("Records", []):
        try:
            payload = json.loads(message["body"])
            process(payload["episodeId"])
        except Exception as exc:
            failures.append({"itemIdentifier": message["messageId"]})
            metric("WorkerFailures")
            log("worker_failure", messageId=message["messageId"], errorType=type(exc).__name__,
                errorCode=code(exc) if isinstance(exc, ClientError) else "PROCESSING_ERROR")
    return {"batchItemFailures": failures}
