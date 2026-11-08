import io
import wave

import boto3
import pytest
from moto import mock_aws

from podcastlens.store import Store


@pytest.fixture
def wav():
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000)
    return out.getvalue()


@pytest.fixture
def aws(monkeypatch):
    for key, value in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing",
                       "AWS_DEFAULT_REGION": "us-east-1", "AWS_EC2_METADATA_DISABLED": "true", "TABLE_NAME": "episodes",
                       "INPUT_BUCKET": "podcastlens-test-input", "OUTPUT_BUCKET": "podcastlens-test-output",
                       "TRANSCRIBE_ROLE_ARN": "arn:aws:iam::123456789012:role/transcribe", "MONTHLY_CAP_SECONDS": "60",
                       "MAX_EPISODE_SECONDS": "300", "MAX_MEDIA_BYTES": "26214400"}.items():
        monkeypatch.setenv(key, value)
    with mock_aws():
        db = boto3.client("dynamodb")
        db.create_table(TableName="episodes", BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": k, "AttributeType": "S"} for k in ["pk", "status", "updatedAt"]],
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}], GlobalSecondaryIndexes=[{
                "IndexName": "status-updated", "KeySchema": [{"AttributeName": "status", "KeyType": "HASH"},
                {"AttributeName": "updatedAt", "KeyType": "RANGE"}], "Projection": {"ProjectionType": "ALL"}}])
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket="podcastlens-test-input"); s3.create_bucket(Bucket="podcastlens-test-output")
        sqs = boto3.client("sqs"); queue = sqs.create_queue(QueueName="jobs")["QueueUrl"]
        monkeypatch.setenv("QUEUE_URL", queue)
        yield {"store": Store(), "s3": s3, "sqs": sqs, "queue": queue}
