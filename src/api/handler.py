"""HTTP API handler. One Lambda, several routes.

This is deliberately a "Lambdalith": every route shares one function and one
IAM role. The alternative - a Lambda per route - gives you tighter least
privilege and independent scaling, at the cost of more moving parts. Both are
valid; docs/ARCHITECTURE.md explains when you would split.
"""

import json
import os
import re

import boto3

from shared import rss
from shared.common import (
    MEDIA_BUCKET, get_budget, json_response, list_episodes, list_feeds,
    now_iso, put_feed, search_keyword, short_id, table,
)

s3 = boto3.client("s3")


def _sub(event) -> str:
    """The Cognito subject, injected by API Gateway's JWT authorizer.

    The authorizer has already validated the token's signature, issuer, audience
    and expiry before we ever run. That is the point of using a managed
    authorizer instead of verifying JWTs in application code.
    """
    try:
        return event["requestContext"]["authorizer"]["jwt"]["claims"]["sub"]
    except (KeyError, TypeError):
        return "public"


def _body(event) -> dict:
    try:
        return json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return {}


URL_RE = re.compile(r"^https?://[^\s]+$", re.I)


def add_feed(event):
    body = _body(event)
    url = (body.get("url") or "").strip()
    if not URL_RE.match(url):
        return json_response(400, {"error": "provide a valid http(s) feed url"})

    try:
        parsed = rss.parse(rss.fetch(url))
    except Exception as exc:
        return json_response(400, {"error": f"could not read that feed: {exc}"})

    feed_id = short_id(url)
    item = put_feed(_sub(event), feed_id, {
        "url": url,
        "title": parsed["title"],
        "description": parsed["description"],
        "image": parsed["image"],
        "active": True,
        "episodeCount": len(parsed["episodes"]),
        "lastPolledAt": None,
    })
    return json_response(201, {"feed": item})


def get_feeds(event):
    return json_response(200, {"feeds": list_feeds(_sub(event))})


def delete_feed(event):
    feed_id = (event.get("pathParameters") or {}).get("feedId", "")
    if not feed_id:
        return json_response(400, {"error": "feedId required"})
    table().delete_item(Key={"PK": f"USER#{_sub(event)}", "SK": f"FEED#{feed_id}"})
    return json_response(200, {"deleted": feed_id})


def get_episodes(event):
    feed_id = (event.get("pathParameters") or {}).get("feedId", "")
    if not feed_id:
        return json_response(400, {"error": "feedId required"})
    return json_response(200, {"episodes": list_episodes(feed_id)})


SNIPPET_RADIUS = 160


def _snippet(feed_id: str, episode_id: str, term: str) -> str:
    """Pull the plain-text transcript from S3 and cut out the matching sentence.

    The transcript never goes into DynamoDB. Items are capped at 400 KB and you
    pay per byte written; S3 is three orders of magnitude cheaper for blobs.
    Keep the index in DynamoDB, keep the payload in S3.
    """
    key = f"transcripts/{feed_id}/{episode_id}.txt"
    try:
        text = s3.get_object(Bucket=MEDIA_BUCKET, Key=key)["Body"].read().decode("utf-8", "replace")
    except Exception:
        return ""
    pos = text.lower().find(term.lower())
    if pos < 0:
        return text[:SNIPPET_RADIUS].strip()
    start = max(0, pos - SNIPPET_RADIUS // 2)
    end = min(len(text), pos + len(term) + SNIPPET_RADIUS // 2)
    return ("..." if start else "") + text[start:end].strip() + ("..." if end < len(text) else "")


def search(event):
    params = event.get("queryStringParameters") or {}
    term = (params.get("q") or "").strip()
    if len(term) < 3:
        return json_response(400, {"error": "query must be at least 3 characters"})

    hits = search_keyword(term)
    results = []
    for hit in hits[:8]:
        results.append({
            "feedId": hit["feedId"],
            "episodeId": hit["episodeId"],
            "episodeTitle": hit.get("episodeTitle", ""),
            "snippet": _snippet(hit["feedId"], hit["episodeId"], term),
        })
    return json_response(200, {"query": term, "count": len(hits), "results": results})


def budget(event):
    return json_response(200, get_budget())


ROUTES = {
    "GET /feeds": get_feeds,
    "POST /feeds": add_feed,
    "DELETE /feeds/{feedId}": delete_feed,
    "GET /feeds/{feedId}/episodes": get_episodes,
    "GET /search": search,
    "GET /budget": budget,
}


def lambda_handler(event, context):
    route = event.get("routeKey", "")
    handler = ROUTES.get(route)
    if not handler:
        return json_response(404, {"error": f"no route for {route}"})
    try:
        return handler(event)
    except Exception as exc:  # noqa: BLE001 - surface the error, log the trace
        print(f"ERROR route={route} err={exc!r}")
        return json_response(500, {"error": "internal error", "detail": str(exc)})
