import hashlib
import io
import json
import math
import os
import re
import time
from datetime import datetime, timezone

import mutagen

TERMINAL = {"COMPLETED", "FAILED", "BLOCKED_BUDGET"}
ID_PATTERN = re.compile(r"^[a-f0-9]{64}$")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def month():
    return datetime.now(timezone.utc).strftime("%Y-%m")


def identity(text):
    return hashlib.sha256(text.encode()).hexdigest()


def validate_id(value):
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise ValueError("Invalid episode ID")
    return value


def job_name(bucket, episode_id):
    return "pl-" + identity(bucket)[:8] + "-" + validate_id(episode_id)


def inspect_audio(data, max_seconds=300):
    if not data:
        raise ValueError("Empty media")
    try:
        media = mutagen.File(io.BytesIO(data))
        seconds = float(media.info.length)
        kind = media.__class__.__name__.lower()
    except Exception as exc:
        raise ValueError("Cannot parse audio; use WAV, MP3, M4A, FLAC, or OGG") from exc
    formats = {"wave": "wav", "mp3": "mp3", "mp4": "mp4", "flac": "flac",
               "oggvorbis": "ogg", "oggopus": "ogg"}
    if kind not in formats or not math.isfinite(seconds) or not 0 < seconds <= max_seconds:
        raise ValueError("Unsupported format or recording exceeds MaxEpisodeSeconds")
    # A conservative one-second rounding margin and Transcribe's 15-second minimum.
    return formats[kind], max(15, math.ceil(seconds) + 1)


def log(event, **fields):
    print(json.dumps({"event": event, **fields}, default=str))


def metric(name, value=1):
    print(json.dumps({"_aws": {"Timestamp": int(time.time() * 1000), "CloudWatchMetrics": [
        {"Namespace": "PodcastLens", "Dimensions": [["Stack"]], "Metrics": [
            {"Name": name, "Unit": "Count"}]}]},
        "Stack": os.environ.get("STACK_NAME", "local"), name: value}))


def search_transcript(document, query, limit=20):
    query = query.strip().casefold()
    if not 1 <= len(query) <= 200:
        raise ValueError("Search must contain 1-200 characters")
    results = document.get("results", {})
    text = " ".join(x.get("transcript", "") for x in results.get("transcripts", []))
    # Casefold can change Unicode string length: search a separate normalized text
    # and return normalized context so offsets remain correct.
    normalized = text.casefold()
    matches, start = [], 0
    while len(matches) < limit:
        pos = normalized.find(query, start)
        if pos < 0:
            break
        matches.append({"offset": pos, "context": normalized[max(0, pos-70):pos+len(query)+70]})
        start = pos + max(1, len(query))
    return matches
