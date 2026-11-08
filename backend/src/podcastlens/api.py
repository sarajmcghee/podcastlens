"""Authenticated operator dashboard for the existing transcription pipeline.

API Gateway verifies access tokens and scopes. Only invited Cognito operators
may read the shared personal workspace or change its feed configuration.
"""
import base64
import hashlib
import json
import os
import re
from decimal import Decimal
from urllib.parse import urlsplit

import boto3
from botocore.exceptions import ClientError

from podcastlens.core import month, search_transcript, validate_id
from podcastlens.store import Store, code, marshal, unmarshal

MAX_TRANSCRIPT = 1024 * 1024


def response(status, data):
    return {"statusCode": status, "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
            "body": json.dumps(data, default=lambda v: int(v) if isinstance(v, Decimal) else str(v))}


def authorized(event):
    claims = event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})
    groups = claims.get("cognito:groups", [])
    if isinstance(groups, str):
        groups = [x.strip(' []\"') for x in groups.split(',')]
    return (claims.get("sub") and claims.get("token_use") == "access" and
            "podcastlens/access" in claims.get("scope", "").split() and "operators" in groups)


def body(event):
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw, validate=True).decode()
    if len(raw) > 8192:
        raise ValueError("Request is too large")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def public_episode(row):
    return {key: row[key] for key in ("episodeId", "title", "status", "source", "updatedAt", "errorCode", "reservedSeconds") if key in row}


def list_episodes(store, params):
    args = {"TableName": store.name, "Limit": 100, "ConsistentRead": True,
            "FilterExpression": "begins_with(pk, :ep)", "ExpressionAttributeValues": marshal({":ep": "EP#"})}
    if params.get("cursor"):
        if len(params["cursor"]) > 1024:
            raise ValueError("Invalid cursor")
        try:
            key = json.loads(base64.urlsafe_b64decode(params["cursor"]))
            if set(key) != {"pk"} or not isinstance(key["pk"], str) or len(key["pk"]) > 256:
                raise ValueError()
            args["ExclusiveStartKey"] = marshal(key)
        except (ValueError, TypeError, UnicodeError):
            raise ValueError("Invalid cursor") from None
    result = store.db.scan(**args)
    rows = [public_episode(unmarshal(x)) for x in result.get("Items", [])]
    rows.sort(key=lambda x: x.get("updatedAt", ""), reverse=True)
    cursor = result.get("LastEvaluatedKey")
    return {"episodes": rows, "nextCursor": base64.urlsafe_b64encode(json.dumps(unmarshal(cursor)).encode()).decode() if cursor else None}


def feed_id(url):
    return hashlib.sha256(url.encode()).hexdigest()


def feeds_result(config):
    return {"feeds": [{"feedId": feed_id(url), "url": url, "title": url} for url in config.get("feeds", [])],
            "allowedHosts": config.get("allowedHosts", [])}


def change_feeds(store, event, remove=None):
    config = store.get_key("CONFIG#FEEDS") or {}
    feeds = list(config.get("feeds", []))
    hosts = set(config.get("allowedHosts", []))
    if remove:
        validate_id(remove)
        feeds = [url for url in feeds if feed_id(url) != remove]
    else:
        data = body(event)
        url, added_hosts = data.get("url"), data.get("allowedHosts", [])
        if not isinstance(url, str) or len(url) > 4096 or not isinstance(added_hosts, list):
            raise ValueError("Provide an HTTPS feed URL and allowedHosts array")
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment or parsed.port not in (None, 443):
            raise ValueError("Feed URL must use HTTPS without credentials or a fragment")
        added_hosts = [parsed.hostname, *added_hosts]
        for host in added_hosts:
            if not isinstance(host, str) or len(host) > 253 or not re.fullmatch(r"[a-zA-Z0-9.-]+", host):
                raise ValueError("Use exact hostnames without schemes, paths or wildcards")
            hosts.add(host.lower())
        if url not in feeds:
            feeds.append(url)
        if len(feeds) > 5 or len(hosts) > 30:
            raise ValueError("Limit: five feeds and thirty allowed hosts")
    # Compare the actual arrays as well as the revision: the CLI can edit this
    # same row without a web revision, and its changes must not be overwritten.
    values = {":feeds": feeds, ":hosts": sorted(hosts), ":version": int(config.get("webVersion", 0)) + 1}
    condition = "attribute_not_exists(pk)"
    if config:
        condition = "feeds = :oldFeeds AND allowedHosts = :oldHosts"
        values.update({":oldFeeds": config.get("feeds", []), ":oldHosts": config.get("allowedHosts", [])})
    store.db.update_item(TableName=store.name, Key=marshal({"pk": "CONFIG#FEEDS"}),
        UpdateExpression="SET feeds = :feeds, allowedHosts = :hosts, webVersion = :version",
        ConditionExpression=condition, ExpressionAttributeValues=marshal(values))
    return feeds_result({"feeds": feeds, "allowedHosts": sorted(hosts)})


def transcript(store, episode_id, query=None):
    row = store.get(validate_id(episode_id))
    if not row:
        return response(404, {"error": "Recording not found"})
    if row["status"] != "COMPLETED":
        return response(409, {"error": "Transcript is not complete", "status": row["status"]})
    expected = f"transcripts/{episode_id}.json"
    if row.get("transcriptKey") != expected:
        raise ValueError("Invalid transcript location")
    obj = boto3.client("s3").get_object(Bucket=os.environ["OUTPUT_BUCKET"], Key=expected)
    stream = obj["Body"]
    try:
        if obj.get("ContentLength", 0) > MAX_TRANSCRIPT:
            raise ValueError("Transcript exceeds dashboard size limit")
        raw = stream.read(MAX_TRANSCRIPT + 1)
        if len(raw) > MAX_TRANSCRIPT:
            raise ValueError("Transcript exceeds dashboard size limit")
        document = json.loads(raw)
    finally:
        stream.close()
    if query is not None:
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 200:
            raise ValueError("Search for between 1 and 200 characters")
        return response(200, {"matches": search_transcript(document, query)})
    text = " ".join(x.get("transcript", "") for x in document.get("results", {}).get("transcripts", []))
    return response(200, {"episode": public_episode(row), "text": text})


def handler(event, context):
    if not authorized(event):
        return response(403, {"error": "An invited operator account is required"})
    try:
        store = Store()
        route = event.get("routeKey", "")
        params = event.get("queryStringParameters") or {}
        path = event.get("pathParameters") or {}
        if route == "GET /episodes":
            return response(200, list_episodes(store, params))
        if route == "GET /episodes/{episodeId}/transcript":
            return transcript(store, path.get("episodeId", ""), params.get("q"))
        if route == "GET /budget":
            stack = boto3.client("cloudformation").describe_stacks(StackName=os.environ["PIPELINE_STACK"])["Stacks"][0]
            outputs = {x.get("Description"): x["OutputValue"] for x in stack["Outputs"]}
            cap = int(outputs["MONTHLY_CAP_SECONDS"])
            used = int((store.get_key("BUDGET#" + month()) or {}).get("usedSeconds", 0))
            return response(200, {"month": month(), "usedSeconds": used, "capSeconds": cap})
        if route == "GET /feeds":
            return response(200, feeds_result(store.get_key("CONFIG#FEEDS") or {}))
        if route == "POST /feeds":
            return response(200, change_feeds(store, event))
        if route == "DELETE /feeds/{feedId}":
            return response(200, change_feeds(store, event, path.get("feedId", "")))
        if route == "POST /scan":
            result = boto3.client("lambda").invoke(FunctionName=os.environ["SCANNER_FUNCTION"], InvocationType="Event", Payload=b"{}")
            if result.get("StatusCode") != 202:
                raise RuntimeError("Scanner did not accept request")
            return response(202, {"message": "Feed check requested. Refresh recordings shortly."})
        return response(404, {"error": "Route not found"})
    except ClientError as exc:
        if code(exc) == "ConditionalCheckFailedException":
            return response(409, {"error": "Feed settings changed; refresh and try again"})
        if code(exc) in {"NoSuchKey", "NotFound"}:
            return response(404, {"error": "Transcript is unavailable or has expired"})
        print(json.dumps({"event": "dashboard_error", "errorType": code(exc)}))
        return response(503, {"error": "Service unavailable; please try again"})
    except (ValueError, TypeError, UnicodeError):
        return response(400, {"error": "Invalid request or unsupported data"})
    except Exception as exc:
        print(json.dumps({"event": "dashboard_error", "errorType": type(exc).__name__}))
        return response(500, {"error": "Unable to complete request"})
