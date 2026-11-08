import json
import os

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

from podcastlens.core import TERMINAL, now, validate_id

SER, DES = TypeSerializer(), TypeDeserializer()


def marshal(item):
    return {k: SER.serialize(v) for k, v in item.items()}


def unmarshal(item):
    return {k: DES.deserialize(v) for k, v in item.items()}


def code(exc):
    return exc.response.get("Error", {}).get("Code", "Unknown")


class Store:
    def __init__(self, table=None, client=None):
        self.name = table or os.environ["TABLE_NAME"]
        self.db = client or boto3.client("dynamodb")

    def get_key(self, pk):
        row = self.db.get_item(TableName=self.name, Key=marshal({"pk": pk}), ConsistentRead=True).get("Item")
        return unmarshal(row) if row else None

    def get(self, episode_id):
        return self.get_key("EP#" + validate_id(episode_id))

    def create(self, record):
        item = {**record, "pk": "EP#" + validate_id(record["episodeId"]),
                "status": "QUEUED", "updatedAt": now(), "enqueuePending": True}
        try:
            self.db.put_item(TableName=self.name, Item=marshal(item), ConditionExpression="attribute_not_exists(pk)")
            return item
        except ClientError as exc:
            if code(exc) != "ConditionalCheckFailedException":
                raise
            return self.get(record["episodeId"])

    def enqueued(self, episode_id):
        self.db.update_item(TableName=self.name, Key=marshal({"pk": "EP#" + validate_id(episode_id)}),
                            UpdateExpression="SET enqueuePending = :f",
                            ConditionExpression="attribute_exists(pk)", ExpressionAttributeValues=marshal({":f": False}))

    def transition(self, episode_id, status, **fields):
        names = {"#s": "status"}
        values = {":s": status, ":t": now(), ":done": "COMPLETED", ":failed": "FAILED", ":blocked": "BLOCKED_BUDGET"}
        assignments = ["#s = :s", "updatedAt = :t"]
        for i, (key, value) in enumerate(fields.items()):
            names[f"#f{i}"] = key
            values[f":f{i}"] = value
            assignments.append(f"#f{i} = :f{i}")
        try:
            self.db.update_item(TableName=self.name, Key=marshal({"pk": "EP#" + validate_id(episode_id)}),
                                UpdateExpression="SET " + ", ".join(assignments),
                                ConditionExpression="attribute_exists(pk) AND NOT (#s IN (:done, :failed, :blocked))",
                                ExpressionAttributeNames=names, ExpressionAttributeValues=marshal(values))
            return True
        except ClientError as exc:
            if code(exc) != "ConditionalCheckFailedException":
                raise
            return False

    def reserve(self, episode_id, seconds, period, cap):
        existing = self.get(episode_id)
        if not existing:
            raise ValueError("Unknown episode")
        if "reservationMonth" in existing:
            return True
        if existing["status"] in TERMINAL:
            return False
        if seconds > cap:
            return self._block_if_unreserved(episode_id)
        try:
            self.db.transact_write_items(TransactItems=[
                {"Update": {"TableName": self.name, "Key": marshal({"pk": "EP#" + episode_id}),
                    "UpdateExpression": "SET reservationMonth = :m, reservedSeconds = :n, #s = :s, updatedAt = :t",
                    "ConditionExpression": "attribute_exists(pk) AND attribute_not_exists(reservationMonth) AND #s = :q",
                    "ExpressionAttributeNames": {"#s": "status"},
                    "ExpressionAttributeValues": marshal({":m": period, ":n": seconds, ":s": "SUBMITTING", ":q": "QUEUED", ":t": now()})}},
                {"Update": {"TableName": self.name, "Key": marshal({"pk": "BUDGET#" + period}),
                    "UpdateExpression": "ADD usedSeconds :n",
                    "ConditionExpression": "attribute_not_exists(usedSeconds) OR usedSeconds <= :remaining",
                    "ExpressionAttributeValues": marshal({":n": seconds, ":remaining": cap - seconds})}}
            ])
            return True
        except ClientError as exc:
            if code(exc) != "TransactionCanceledException":
                raise
            # A concurrent retry might already have reserved this exact episode.
            if "reservationMonth" in (self.get(episode_id) or {}):
                return True
            reasons = [x.get("Code") for x in exc.response.get("CancellationReasons", [])]
            if "TransactionConflict" in reasons:
                raise
            if not reasons:
                budget = self.get_key("BUDGET#" + period) or {}
                if int(budget.get("usedSeconds", 0)) + seconds > cap:
                    return self._block_if_unreserved(episode_id)
                raise
            if len(reasons) > 1 and reasons[1] == "ConditionalCheckFailed":
                return self._block_if_unreserved(episode_id)
            # Terminal-state race or other condition: retry/re-read; do not hide failures.
            current = self.get(episode_id)
            if current and current["status"] in TERMINAL:
                return False
            raise

    def _block_if_unreserved(self, episode_id):
        try:
            self.db.update_item(TableName=self.name, Key=marshal({"pk": "EP#" + episode_id}),
                UpdateExpression="SET #s = :b, updatedAt = :t",
                ConditionExpression="attribute_not_exists(reservationMonth) AND #s = :q",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues=marshal({":b": "BLOCKED_BUDGET", ":q": "QUEUED", ":t": now()}))
            return False
        except ClientError as exc:
            if code(exc) != "ConditionalCheckFailedException":
                raise
            return "reservationMonth" in (self.get(episode_id) or {})

    def by_status(self, status, limit=100, cursor=None):
        args = dict(TableName=self.name, IndexName="status-updated", KeyConditionExpression="#s = :s",
                    ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues=marshal({":s": status}), Limit=limit)
        if cursor:
            args["ExclusiveStartKey"] = cursor
        response = self.db.query(**args)
        return [unmarshal(x) for x in response.get("Items", [])], response.get("LastEvaluatedKey")


def enqueue(store, record, sqs=None):
    if record["status"] in TERMINAL:
        return
    (sqs or boto3.client("sqs")).send_message(QueueUrl=os.environ["QUEUE_URL"],
        MessageBody=json.dumps({"episodeId": record["episodeId"]}))
    # A crash here may duplicate a message, but cannot lose the pending marker.
    store.enqueued(record["episodeId"])
