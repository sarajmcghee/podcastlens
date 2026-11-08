#!/usr/bin/env python3
"""Use your own AWS session. No public HTTP endpoint or embedded credentials."""
import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
import boto3
from podcastlens.core import identity, search_transcript, validate_id
from podcastlens.feeds import register_audio
from podcastlens.store import Store, enqueue, marshal


def configure(stack, region):
    os.environ["AWS_DEFAULT_REGION"] = region
    response = boto3.client("cloudformation", region_name=region).describe_stacks(StackName=stack)
    outputs = {x["Description"]: x["OutputValue"] for x in response["Stacks"][0]["Outputs"]}
    for key, value in outputs.items():
        os.environ[key] = value
    return outputs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stack", default="podcastlens-dev")
    parser.add_argument("--region", default="us-east-1")
    commands = parser.add_subparsers(dest="command", required=True)
    upload = commands.add_parser("upload"); upload.add_argument("file"); upload.add_argument("--title", default="My test recording")
    status = commands.add_parser("status"); status.add_argument("episode_id")
    query = commands.add_parser("search"); query.add_argument("episode_id"); query.add_argument("query")
    commands.add_parser("outputs"); commands.add_parser("budget"); commands.add_parser("scan"); commands.add_parser("reconcile")
    feed = commands.add_parser("feeds"); feed.add_argument("config", help="JSON with feeds and allowedHosts")
    retry = commands.add_parser("retry-budget"); retry.add_argument("episode_id")
    args = parser.parse_args()
    outputs = configure(args.stack, args.region)
    store = Store()
    if args.command == "outputs":
        result = outputs
    elif args.command == "upload":
        path = Path(args.file)
        if path.stat().st_size > int(os.environ["MAX_MEDIA_BYTES"]):
            raise ValueError("File exceeds size limit")
        data = path.read_bytes()
        episode_id = hashlib.sha256(data).hexdigest()
        result = register_audio(episode_id, args.title, data)
    elif args.command in {"status", "search"}:
        record = store.get(validate_id(args.episode_id))
        if not record:
            raise ValueError("Episode not found")
        result = record
        if args.command == "search":
            if record["status"] != "COMPLETED":
                raise ValueError("Transcript is not complete")
            obj = boto3.client("s3").get_object(Bucket=os.environ["OUTPUT_BUCKET"], Key=record["transcriptKey"])
            body = obj["Body"]
            try:
                if obj.get("ContentLength", 0) > 10 * 1024 * 1024:
                    raise ValueError("Transcript exceeds local search limit")
                raw = body.read(10 * 1024 * 1024 + 1)
                if len(raw) > 10 * 1024 * 1024:
                    raise ValueError("Transcript exceeds local search limit")
                result = search_transcript(json.loads(raw), args.query)
            finally:
                body.close()
    elif args.command == "budget":
        from podcastlens.core import month
        result = {"month": month(), "capSeconds": os.environ["MONTHLY_CAP_SECONDS"],
                  "reservation": store.get_key("BUDGET#" + month()) or {"usedSeconds": 0}}
    elif args.command in {"scan", "reconcile"}:
        fn = outputs["SCANNER_FUNCTION" if args.command == "scan" else "RECONCILER_FUNCTION"]
        response = boto3.client("lambda").invoke(FunctionName=fn, Payload=b"{}")
        result = json.loads(response["Payload"].read())
        if response.get("FunctionError"):
            raise RuntimeError("Lambda failed; inspect its CloudWatch log")
    elif args.command == "feeds":
        config = json.loads(Path(args.config).read_text())
        if set(config) != {"feeds", "allowedHosts"} or not isinstance(config["feeds"], list) or not isinstance(config["allowedHosts"], list):
            raise ValueError("Expected feeds and allowedHosts arrays")
        if len(config["feeds"]) > 5 or len(config["allowedHosts"]) > 30:
            raise ValueError("Limit: five feeds and thirty allowed hosts")
        from urllib.parse import urlsplit
        for host in config["allowedHosts"]:
            if not isinstance(host, str) or "/" in host or ":" in host or "*" in host:
                raise ValueError("Use exact hostnames without URL prefixes, paths, or wildcards")
        for url in config["feeds"]:
            parsed = urlsplit(url)
            if parsed.scheme != "https" or parsed.hostname not in config["allowedHosts"]:
                raise ValueError("Each feed must be HTTPS and in allowedHosts")
        store.db.put_item(TableName=store.name, Item=marshal({"pk": "CONFIG#FEEDS", **config}))
        result = {"configuredFeeds": len(config["feeds"]), "schedule": "unchanged; run scan manually first"}
    else:
        episode_id = validate_id(args.episode_id)
        store.db.update_item(TableName=store.name, Key=marshal({"pk": "EP#" + episode_id}),
            UpdateExpression="SET #s = :q, enqueuePending = :p", ConditionExpression="#s = :b AND attribute_not_exists(reservationMonth)",
            ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues=marshal({":q": "QUEUED", ":p": True, ":b": "BLOCKED_BUDGET"}))
        enqueue(store, store.get(episode_id))
        result = {"requeued": episode_id}
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
