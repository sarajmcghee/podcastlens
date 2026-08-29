"""Scheduled feed poller. EventBridge Scheduler invokes this on a fixed rate.

Its only job is to notice new episodes and hand them off. It does no heavy
lifting - that is what the queue is for. A short, fast, predictable function
that fans work out to a queue is the shape you want for anything on a timer.
"""

import json
import os

import boto3

from shared import rss
from shared.common import (
    MAX_EPISODES_PER_POLL, now_iso, put_episode_if_new, scan_all_feeds,
    short_id, table,
)

sqs = boto3.client("sqs")
QUEUE_URL = os.environ["QUEUE_URL"]


def lambda_handler(event, context):
    feeds = scan_all_feeds()
    print(f"polling {len(feeds)} feed(s)")
    queued = 0
    errors = []

    for feed in feeds:
        feed_id, url = feed["feedId"], feed["url"]
        try:
            parsed = rss.parse(rss.fetch(url))
        except Exception as exc:  # a dead feed must not stop the others
            print(f"WARN feed={feed_id} fetch failed: {exc!r}")
            errors.append(feed_id)
            continue

        # Newest first, and only ever look at the newest few. Without this cap,
        # adding a podcast with an 800-episode back catalogue would try to
        # transcribe all 800 of them.
        episodes = sorted(parsed["episodes"], key=lambda e: e["publishedAt"], reverse=True)
        for ep in episodes[:MAX_EPISODES_PER_POLL]:
            episode_id = short_id(feed_id, ep["guid"])
            created = put_episode_if_new(feed_id, episode_id, ep["publishedAt"], {
                "title": ep["title"],
                "audioUrl": ep["audioUrl"],
                "audioType": ep["audioType"],
                "audioBytes": ep["audioBytes"],
                "durationSeconds": ep["durationSeconds"],
                "summary": ep["summary"],
                "feedTitle": feed.get("title", ""),
            })
            if not created:
                continue  # already seen on an earlier poll
            sqs.send_message(
                QueueUrl=QUEUE_URL,
                MessageBody=json.dumps({
                    "feedId": feed_id,
                    "episodeId": episode_id,
                    "publishedAt": ep["publishedAt"],
                    "audioUrl": ep["audioUrl"],
                    "durationSeconds": ep["durationSeconds"],
                    "title": ep["title"],
                }),
            )
            queued += 1

        table().update_item(
            Key={"PK": feed["PK"], "SK": feed["SK"]},
            UpdateExpression="SET lastPolledAt = :t, episodeCount = :c",
            ExpressionAttributeValues={":t": now_iso(), ":c": len(parsed["episodes"])},
        )

    print(f"queued {queued} new episode(s); {len(errors)} feed error(s)")
    return {"feeds": len(feeds), "queued": queued, "errors": errors}
