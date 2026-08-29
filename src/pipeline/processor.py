"""SQS consumer: copy a clip of the audio into S3 and start an Amazon
Transcribe job.

This is the expensive, slow, failure-prone step, which is exactly why it sits
behind a queue instead of running inline. The queue gives us retries, a dead
letter queue, and back-pressure for free - the textbook reason to decouple.
"""

import json
import os

import boto3
from botocore.exceptions import ClientError

from shared import rss
from shared.common import (
    CLIP_SECONDS, MAX_AUDIO_BYTES, MEDIA_BUCKET, claim_transcribe_budget,
    episode_sk, update_episode,
)

s3 = boto3.client("s3")
transcribe = boto3.client("transcribe")

DEFAULT_DURATION = 1800   # assume 30 min when the feed declares no duration

# Podcast RSS metadata is not trustworthy. In the Lex Fridman feed, 54 of 502
# items carry no itunes:duration at all and several declare a placeholder
# enclosure length of exactly 5 MiB. So we only believe a derived bitrate when
# it lands in a plausible speech-audio range, and fall back otherwise.
MIN_BYTES_PER_SEC = 4000    # 32 kbps
MAX_BYTES_PER_SEC = 40000   # 320 kbps

# The fallback deliberately UNDER-estimates (64 kbps). Get this backwards and
# the failure is expensive: over-estimating the bitrate means fetching more
# bytes than intended, and Transcribe bills for every second of audio we hand
# it - so we would silently blow through the budget we just reserved.
# Under-estimating only means a shorter clip than asked for. Always err toward
# the cheap mistake.
CONSERVATIVE_BYTES_PER_SEC = 8000

HEADER_PADDING = 64 * 1024   # room for ID3 tags before the first audio frame


class LimitedReader:
    """A file-like wrapper that refuses to yield more than `limit` bytes.

    The Range header is a request, not a guarantee - some CDNs ignore it and
    start streaming the full four hour episode anyway. This enforces the ceiling
    on our side so a badly behaved origin cannot run us past the free tier.
    """

    def __init__(self, stream, limit: int):
        self._stream = stream
        self._remaining = limit

    def read(self, size: int = -1) -> bytes:
        if self._remaining <= 0:
            return b""
        want = self._remaining if size is None or size < 0 else min(size, self._remaining)
        chunk = self._stream.read(want)
        self._remaining -= len(chunk)
        return chunk


def _clip_bytes(declared_duration: int, audio_bytes: int, clip_seconds: int) -> int:
    """How many bytes of the file is roughly `clip_seconds` of audio?

    Pass the duration the feed actually declared - zero if it declared none.
    Substituting a guessed duration here would produce a confidently wrong
    bitrate, which is worse than admitting we do not know.
    """
    bytes_per_sec = CONSERVATIVE_BYTES_PER_SEC
    if audio_bytes > 0 and declared_duration > 0:
        derived = audio_bytes / declared_duration
        if MIN_BYTES_PER_SEC <= derived <= MAX_BYTES_PER_SEC:
            bytes_per_sec = derived
        else:
            print(f"ignoring implausible bitrate {derived:.0f} B/s from feed metadata")
    return min(int(bytes_per_sec * clip_seconds) + HEADER_PADDING, MAX_AUDIO_BYTES)


def _handle(msg: dict):
    feed_id = msg["feedId"]
    episode_id = msg["episodeId"]
    sk = episode_sk(msg["publishedAt"], episode_id)

    declared_duration = int(msg.get("durationSeconds") or 0)
    audio_bytes = int(msg.get("audioBytes") or 0)
    # Used for the budget reservation and the clip cap; the bitrate calculation
    # below deliberately does NOT see this guess.
    duration = declared_duration or DEFAULT_DURATION
    clip_seconds = min(CLIP_SECONDS, duration) if CLIP_SECONDS > 0 else duration

    # Reserve the audio-minutes BEFORE doing any work. Checking afterwards would
    # mean we had already paid for the transcription.
    if not claim_transcribe_budget(clip_seconds):
        print(f"SKIP episode={episode_id}: monthly Transcribe budget exhausted")
        update_episode(feed_id, sk, "BUDGET_SKIPPED",
                       {"note": "monthly Transcribe allowance spent"})
        return

    update_episode(feed_id, sk, "DOWNLOADING")

    limit = _clip_bytes(declared_duration, audio_bytes, clip_seconds)
    key = f"audio/{feed_id}/{episode_id}.mp3"
    stream, length, ranged = rss.open_stream(msg["audioUrl"], byte_limit=limit)
    print(f"episode={episode_id} duration={duration}s clip={clip_seconds}s "
          f"limit={limit}B server_length={length} ranged={ranged}")

    try:
        # upload_fileobj streams in parts; the whole file never sits in memory.
        s3.upload_fileobj(
            LimitedReader(stream, limit), MEDIA_BUCKET, key,
            ExtraArgs={"ContentType": "audio/mpeg"},
        )
    finally:
        stream.close()
    print(f"stored s3://{MEDIA_BUCKET}/{key}")

    job_name = f"pl-{feed_id}-{episode_id}"
    try:
        transcribe.start_transcription_job(
            TranscriptionJobName=job_name,
            LanguageCode="en-US",
            MediaFormat="mp3",
            Media={"MediaFileUri": f"s3://{MEDIA_BUCKET}/{key}"},
            OutputBucketName=MEDIA_BUCKET,
            OutputKey=f"transcripts/{feed_id}/{episode_id}.json",
            Settings={"ShowSpeakerLabels": True, "MaxSpeakerLabels": 2},
        )
    except ClientError as exc:
        # A duplicate job name means a retry re-delivered this message after the
        # job already started. That is success, not failure - do not raise, or
        # the message goes back on the queue forever.
        if exc.response["Error"]["Code"] == "ConflictException":
            print(f"job {job_name} already exists - treating as in flight")
        else:
            raise

    update_episode(feed_id, sk, "TRANSCRIBING", {
        "jobName": job_name,
        "audioKey": key,
        "clipSeconds": clip_seconds,
    })


def lambda_handler(event, context):
    """Partial batch failure: we report only the messages that actually failed.

    Without ReportBatchItemFailures, one poison message forces the whole batch
    to be redelivered and the successful ones get processed twice.
    """
    failures = []
    for record in event.get("Records", []):
        try:
            _handle(json.loads(record["body"]))
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR messageId={record['messageId']}: {exc!r}")
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
