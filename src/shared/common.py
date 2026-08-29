"""Shared helpers for every PodcastLens Lambda.

Everything here uses only the Python standard library plus boto3, which the
Lambda Python runtime already provides. That is deliberate: with zero external
dependencies we never have to build a deployment package inside Docker, and
`aws cloudformation package` can zip the source directly.
"""

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

TABLE_NAME = os.environ.get("TABLE_NAME", "")
MEDIA_BUCKET = os.environ.get("MEDIA_BUCKET", "")
TOPIC_ARN = os.environ.get("TOPIC_ARN", "")

# Cost guardrails. Amazon Transcribe's free tier is 60 audio-minutes per month
# for the first 12 months, so we refuse to start jobs once we have spent the
# monthly allowance. See docs/COSTS.md.
MONTHLY_TRANSCRIBE_SECONDS = int(os.environ.get("MONTHLY_TRANSCRIBE_SECONDS", "3000"))
MAX_EPISODES_PER_POLL = int(os.environ.get("MAX_EPISODES_PER_POLL", "1"))
MAX_AUDIO_BYTES = int(os.environ.get("MAX_AUDIO_BYTES", str(120 * 1024 * 1024)))

# Transcribe only the first CLIP_SECONDS of each episode. Set to 0 to do the
# whole thing - but a single four hour Lex Fridman episode is 240 minutes of
# audio against a 60 minute monthly free tier, so the default keeps the clip
# short and the bill at zero.
CLIP_SECONDS = int(os.environ.get("CLIP_SECONDS", "600"))

_dynamodb = boto3.resource("dynamodb")
_table = _dynamodb.Table(TABLE_NAME) if TABLE_NAME else None


def table():
    return _table


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def month_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def short_id(*parts: str) -> str:
    """Deterministic 16-char id. Deterministic matters: re-polling a feed must
    produce the same episode id so we never transcribe the same audio twice."""
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return digest[:16]


class DecimalEncoder(json.JSONEncoder):
    """DynamoDB returns numbers as Decimal, which json.dumps cannot serialise."""

    def default(self, o):
        if isinstance(o, Decimal):
            return int(o) if o % 1 == 0 else float(o)
        return super().default(o)


def json_response(status: int, body, extra_headers=None):
    headers = {
        "Content-Type": "application/json",
        # CloudFront serves the site from a different origin than the API,
        # so the API must send CORS headers itself.
        "Access-Control-Allow-Origin": os.environ.get("CORS_ORIGIN", "*"),
        "Access-Control-Allow-Headers": "content-type,authorization",
        "Access-Control-Allow-Methods": "GET,POST,DELETE,OPTIONS",
    }
    if extra_headers:
        headers.update(extra_headers)
    return {
        "statusCode": status,
        "headers": headers,
        "body": json.dumps(body, cls=DecimalEncoder),
    }


# ---------------------------------------------------------------------------
# Single-table access patterns
#
#   Feed      PK=USER#<sub>            SK=FEED#<feedId>
#   Episode   PK=FEED#<feedId>         SK=EP#<publishedAt>#<episodeId>
#             GSI1PK=STATUS#<status>   GSI1SK=<updatedAt>
#   Keyword   PK=KW#<term>             SK=REF#<feedId>#<episodeId>
#   Budget    PK=SYSTEM                SK=BUDGET#<YYYY-MM>
#
# One table, one GSI, four access patterns. This is the shape the exam calls
# "single-table design": you model the queries, not the entities.
# ---------------------------------------------------------------------------


def put_feed(user_sub: str, feed_id: str, attrs: dict):
    item = {"PK": f"USER#{user_sub}", "SK": f"FEED#{feed_id}", "type": "FEED",
            "feedId": feed_id, "createdAt": now_iso()}
    item.update(attrs)
    _table.put_item(Item=item)
    return item


def list_feeds(user_sub: str):
    resp = _table.query(
        KeyConditionExpression=Key("PK").eq(f"USER#{user_sub}") & Key("SK").begins_with("FEED#")
    )
    return resp.get("Items", [])


def scan_all_feeds():
    """Used by the scheduled poller, which runs with no user context.

    A Scan is the wrong tool at scale - it reads every item and burns capacity.
    At the size of a personal project it is cheaper and simpler than maintaining
    a second index, but see docs/ARCHITECTURE.md for what you would do instead.
    """
    feeds, kwargs = [], {
        "FilterExpression": "#t = :feed AND #a = :true",
        "ExpressionAttributeNames": {"#t": "type", "#a": "active"},
        "ExpressionAttributeValues": {":feed": "FEED", ":true": True},
    }
    while True:
        resp = _table.scan(**kwargs)
        feeds.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            return feeds
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def episode_sk(published_at: str, episode_id: str) -> str:
    return f"EP#{published_at}#{episode_id}"


def put_episode_if_new(feed_id: str, episode_id: str, published_at: str, attrs: dict) -> bool:
    """Returns True if this episode was newly created.

    attribute_not_exists(PK) makes the write conditional, so two concurrent
    pollers cannot both claim the same episode. This is the DynamoDB answer to
    "how do I get idempotency without a lock".
    """
    item = {
        "PK": f"FEED#{feed_id}",
        "SK": episode_sk(published_at, episode_id),
        "type": "EPISODE",
        "feedId": feed_id,
        "episodeId": episode_id,
        "publishedAt": published_at,
        "status": "NEW",
        "GSI1PK": "STATUS#NEW",
        "GSI1SK": now_iso(),
        "createdAt": now_iso(),
        "updatedAt": now_iso(),
    }
    item.update(attrs)
    try:
        _table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(PK) AND attribute_not_exists(SK)",
        )
        return True
    except _dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
        return False


def find_episode(feed_id: str, episode_id: str):
    resp = _table.query(
        KeyConditionExpression=Key("PK").eq(f"FEED#{feed_id}") & Key("SK").begins_with("EP#"),
        FilterExpression="episodeId = :e",
        ExpressionAttributeValues={":e": episode_id},
    )
    items = resp.get("Items", [])
    return items[0] if items else None


def update_episode(feed_id: str, sk: str, status: str, attrs: dict = None):
    names = {"#s": "status", "#u": "updatedAt", "#g1": "GSI1PK", "#g2": "GSI1SK"}
    values = {":s": status, ":u": now_iso(), ":g1": f"STATUS#{status}", ":g2": now_iso()}
    sets = ["#s = :s", "#u = :u", "#g1 = :g1", "#g2 = :g2"]
    for i, (k, v) in enumerate((attrs or {}).items()):
        names[f"#n{i}"] = k
        values[f":v{i}"] = v
        sets.append(f"#n{i} = :v{i}")
    _table.update_item(
        Key={"PK": f"FEED#{feed_id}", "SK": sk},
        UpdateExpression="SET " + ", ".join(sets),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def episodes_by_status(status: str, limit: int = 50):
    resp = _table.query(
        IndexName="GSI1",
        KeyConditionExpression=Key("GSI1PK").eq(f"STATUS#{status}"),
        Limit=limit,
        ScanIndexForward=False,
    )
    return resp.get("Items", [])


def list_episodes(feed_id: str, limit: int = 50):
    resp = _table.query(
        KeyConditionExpression=Key("PK").eq(f"FEED#{feed_id}") & Key("SK").begins_with("EP#"),
        Limit=limit,
        ScanIndexForward=False,
    )
    return resp.get("Items", [])


NORMALISE = re.compile(r"[^a-z0-9 ]+")


def normalise_term(term: str) -> str:
    return NORMALISE.sub("", term.lower()).strip()


def index_keywords(feed_id: str, episode_id: str, title: str, terms):
    """Write one item per keyword so search is a Query, not a Scan.

    This is an inverted index built by hand. Real full-text search would use
    OpenSearch or Kendra; both cost real money, and this teaches the underlying
    idea for free.
    """
    with _table.batch_writer(overwrite_by_pkeys=["PK", "SK"]) as batch:
        for term in terms:
            t = normalise_term(term)
            if len(t) < 3 or len(t) > 60:
                continue
            batch.put_item(Item={
                "PK": f"KW#{t}",
                "SK": f"REF#{feed_id}#{episode_id}",
                "type": "KEYWORD",
                "feedId": feed_id,
                "episodeId": episode_id,
                "episodeTitle": title,
                "term": t,
            })


def search_keyword(term: str, limit: int = 25):
    resp = _table.query(
        KeyConditionExpression=Key("PK").eq(f"KW#{normalise_term(term)}"),
        Limit=limit,
    )
    return resp.get("Items", [])


def claim_transcribe_budget(seconds: int) -> bool:
    """Atomically reserve audio-seconds from this month's Transcribe budget.

    ADD plus a condition on the resulting value is DynamoDB's atomic counter.
    Without the condition, two concurrent Lambdas could each read 2900, each
    decide there is room, and together blow past the free tier.
    """
    try:
        _table.update_item(
            Key={"PK": "SYSTEM", "SK": f"BUDGET#{month_key()}"},
            UpdateExpression="ADD usedSeconds :s SET #t = :type",
            ConditionExpression="attribute_not_exists(usedSeconds) OR usedSeconds <= :cap",
            ExpressionAttributeNames={"#t": "type"},
            ExpressionAttributeValues={
                ":s": seconds,
                ":cap": MONTHLY_TRANSCRIBE_SECONDS - seconds,
                ":type": "BUDGET",
            },
        )
        return True
    except _dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
        return False


def get_budget():
    resp = _table.get_item(Key={"PK": "SYSTEM", "SK": f"BUDGET#{month_key()}"})
    item = resp.get("Item") or {}
    return {
        "month": month_key(),
        "usedSeconds": int(item.get("usedSeconds", 0)),
        "capSeconds": MONTHLY_TRANSCRIBE_SECONDS,
    }


def notify(subject: str, message: str):
    if not TOPIC_ARN:
        return
    boto3.client("sns").publish(TopicArn=TOPIC_ARN, Subject=subject[:100], Message=message)
