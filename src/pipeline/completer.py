"""Runs when Amazon Transcribe finishes a job.

Nothing polls for completion. Transcribe emits an event, EventBridge matches it
against a rule, and the rule invokes this function. That is event-driven
architecture in one sentence, and it is why there is no idle compute anywhere
in this project.
"""

import json
import os

import boto3

from shared.common import (
    MEDIA_BUCKET, find_episode, index_keywords, notify, update_episode,
)

s3 = boto3.client("s3")
comprehend = boto3.client("comprehend")

CHUNK_BYTES = 4500          # Comprehend caps a single document at 5000 UTF-8 bytes
MAX_PHRASE_CHUNKS = 10      # ~45 KB of text: enough for a long episode
MAX_ENTITY_CHUNKS = 5       # entities are the pricier call, so sample less


def _chunk(text: str):
    """Split on word boundaries without exceeding Comprehend's byte limit."""
    chunks, current, size = [], [], 0
    for word in text.split():
        wlen = len(word.encode("utf-8")) + 1
        if size + wlen > CHUNK_BYTES and current:
            chunks.append(" ".join(current))
            current, size = [], 0
        current.append(word)
        size += wlen
    if current:
        chunks.append(" ".join(current))
    return chunks


def _key_phrases(chunks):
    terms = {}
    for i in range(0, len(chunks), 25):  # BatchDetect takes at most 25 documents
        batch = chunks[i:i + 25]
        resp = comprehend.batch_detect_key_phrases(TextList=batch, LanguageCode="en")
        for result in resp.get("ResultList", []):
            for phrase in result.get("KeyPhrases", []):
                if phrase["Score"] < 0.9:
                    continue
                text = phrase["Text"].strip()
                terms[text.lower()] = terms.get(text.lower(), 0) + 1
    return terms


def _entities(chunks):
    found = {}
    for i in range(0, len(chunks), 25):
        resp = comprehend.batch_detect_entities(TextList=chunks[i:i + 25], LanguageCode="en")
        for result in resp.get("ResultList", []):
            for ent in result.get("Entities", []):
                if ent["Score"] < 0.9 or ent["Type"] in ("QUANTITY", "DATE", "OTHER"):
                    continue
                found.setdefault(ent["Type"], set()).add(ent["Text"].strip())
    return {k: sorted(v)[:15] for k, v in found.items()}


def lambda_handler(event, context):
    detail = event.get("detail", {})
    job_name = detail.get("TranscriptionJobName", "")
    status = detail.get("TranscriptionJobStatus", "")
    print(f"job={job_name} status={status}")

    parts = job_name.split("-")
    if len(parts) != 3 or parts[0] != "pl":
        print(f"ignoring job not owned by PodcastLens: {job_name}")
        return {"ignored": job_name}
    _, feed_id, episode_id = parts

    episode = find_episode(feed_id, episode_id)
    if not episode:
        print(f"WARN no episode row for {feed_id}/{episode_id}")
        return {"ignored": job_name}
    sk = episode["SK"]

    if status != "COMPLETED":
        update_episode(feed_id, sk, "FAILED", {"note": f"transcribe status {status}"})
        return {"status": status}

    raw = s3.get_object(
        Bucket=MEDIA_BUCKET, Key=f"transcripts/{feed_id}/{episode_id}.json"
    )["Body"].read()
    text = json.loads(raw)["results"]["transcripts"][0]["transcript"]

    # A plain-text copy makes search snippets a single cheap GET instead of
    # parsing several megabytes of Transcribe JSON on every query.
    s3.put_object(
        Bucket=MEDIA_BUCKET,
        Key=f"transcripts/{feed_id}/{episode_id}.txt",
        Body=text.encode("utf-8"),
        ContentType="text/plain; charset=utf-8",
    )

    chunks = _chunk(text)
    phrases = _key_phrases(chunks[:MAX_PHRASE_CHUNKS])
    entities = _entities(chunks[:MAX_ENTITY_CHUNKS])

    top = sorted(phrases.items(), key=lambda kv: kv[1], reverse=True)[:40]
    topics = [t for t, _ in top]

    searchable = set(topics)
    for values in entities.values():
        searchable.update(v.lower() for v in values)
    index_keywords(feed_id, episode_id, episode.get("title", ""), searchable)

    update_episode(feed_id, sk, "READY", {
        "topics": topics,
        "entities": entities,
        "wordCount": len(text.split()),
        "transcriptKey": f"transcripts/{feed_id}/{episode_id}.txt",
    })

    # The audio has served its purpose. Deleting it now is the single biggest
    # storage saving in the project; the S3 lifecycle rule is only a safety net
    # for objects this path misses.
    audio_key = episode.get("audioKey") or f"audio/{feed_id}/{episode_id}.mp3"
    s3.delete_object(Bucket=MEDIA_BUCKET, Key=audio_key)

    notify(
        f"PodcastLens: {episode.get('title', 'episode')} is ready",
        f"{episode.get('title')}\n\n"
        f"{len(text.split())} words transcribed.\n"
        f"Topics: {', '.join(topics[:12])}\n",
    )
    return {"status": "READY", "topics": len(topics)}
