import json
import os
from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError

from podcastlens.core import identity, inspect_audio, job_name, search_transcript
from podcastlens.feeds import checked_target, episodes, register_audio
from podcastlens.store import Store, marshal
from podcastlens.worker import find_job, handler, process, sync_job


class TranscribeFake:
    def __init__(self):
        self.jobs = {}; self.starts = 0

    def get_transcription_job(self, TranscriptionJobName):
        if TranscriptionJobName not in self.jobs:
            raise ClientError({"Error": {"Code": "BadRequestException", "Message": "The requested job couldn't be found. Check the job name and try your request again."}}, "GetTranscriptionJob")
        return {"TranscriptionJob": self.jobs[TranscriptionJobName]}

    def start_transcription_job(self, **params):
        name = params["TranscriptionJobName"]
        if name in self.jobs:
            raise ClientError({"Error": {"Code": "ConflictException", "Message": "exists"}}, "StartTranscriptionJob")
        self.starts += 1
        self.jobs[name] = {"TranscriptionJobName": name, "TranscriptionJobStatus": "IN_PROGRESS", "Media": params["Media"]}
        return {"TranscriptionJob": self.jobs[name]}


def add(aws, wav, key="one"):
    return register_audio(identity(key), "Test", wav, store=aws["store"], s3=aws["s3"], sqs=aws["sqs"])


def test_audio_has_billing_floor(wav):
    assert inspect_audio(wav) == ("wav", 15)


def test_invalid_media_rejected():
    with pytest.raises(ValueError): inspect_audio(b"not audio")


def test_oversized_duration_rejected(wav):
    with pytest.raises(ValueError): inspect_audio(wav, max_seconds=0.5)


def test_upload_registers_once_and_acknowledges_outbox(aws, wav):
    record = add(aws, wav)
    add(aws, wav)
    assert not aws["store"].get(record["episodeId"])["enqueuePending"]
    messages = aws["sqs"].receive_message(QueueUrl=aws["queue"], MaxNumberOfMessages=10)["Messages"]
    assert len(messages) == 1


def test_changed_media_cannot_replace_episode(aws, wav):
    add(aws, wav)
    with pytest.raises(ValueError): register_audio(identity("one"), "Changed", wav + b"x", store=aws["store"], s3=aws["s3"])


def test_pending_message_survives_send_failure(aws, wav):
    class BrokenSQS:
        def send_message(self, **kw): raise RuntimeError("transient")
    with pytest.raises(RuntimeError): register_audio(identity("one"), "Test", wav, store=aws["store"], s3=aws["s3"], sqs=BrokenSQS())
    assert aws["store"].get(identity("one"))["enqueuePending"]
    add(aws, wav)
    assert not aws["store"].get(identity("one"))["enqueuePending"]


def test_reservation_is_once_even_across_months(aws, wav):
    record = add(aws, wav); store = aws["store"]; key = record["episodeId"]
    assert store.reserve(key, 15, "2026-11", 60)
    assert store.reserve(key, 15, "2026-12", 60)
    assert store.get_key("BUDGET#2026-11")["usedSeconds"] == 15
    assert store.get_key("BUDGET#2026-12") is None
    assert store.get(key)["reservationMonth"] == "2026-11"


def test_quota_denial_rolls_back_episode_reservation(aws, wav):
    a = add(aws, wav, "a"); b = add(aws, wav, "b"); store = aws["store"]
    assert store.reserve(a["episodeId"], 15, "2026-11", 15)
    assert not store.reserve(b["episodeId"], 15, "2026-11", 15)
    assert store.get_key("BUDGET#2026-11")["usedSeconds"] == 15
    row = store.get(b["episodeId"])
    assert row["status"] == "BLOCKED_BUDGET" and "reservationMonth" not in row


def test_single_job_larger_than_cap_is_rejected(aws, wav):
    record = add(aws, wav)
    assert not aws["store"].reserve(record["episodeId"], 61, "2026-11", 60)
    assert aws["store"].get_key("BUDGET#2026-11") is None


def test_duplicate_delivery_starts_one_external_job(aws, wav):
    record = add(aws, wav); tc = TranscribeFake(); key = record["episodeId"]
    assert process(key, aws["store"], aws["s3"], tc) == "SUBMITTED"
    assert process(key, aws["store"], aws["s3"], tc) == "RECOVERED"
    assert tc.starts == 1


def test_crash_after_service_acceptance_recovers_same_job(aws, wav):
    record = add(aws, wav); store = aws["store"]; tc = TranscribeFake(); key = record["episodeId"]
    with patch.object(store, "transition", side_effect=RuntimeError("simulated crash")):
        with pytest.raises(RuntimeError): process(key, store, aws["s3"], tc)
    assert store.get(key)["status"] == "SUBMITTING"
    process(key, store, aws["s3"], tc)
    assert tc.starts == 1 and store.get(key)["status"] == "PROCESSING"


def test_worker_rejects_changed_s3_content(aws, wav):
    record = add(aws, wav)
    aws["s3"].put_object(Bucket=os.environ["INPUT_BUCKET"], Key=record["objectKey"], Body=wav + b"bad")
    tc = TranscribeFake()
    with pytest.raises(ValueError): process(record["episodeId"], aws["store"], aws["s3"], tc)
    assert tc.starts == 0


def test_completed_state_cannot_regress(aws, wav):
    record = add(aws, wav); store = aws["store"]
    store.transition(record["episodeId"], "COMPLETED", transcriptKey="transcripts/result.json")
    assert not store.transition(record["episodeId"], "PROCESSING")
    assert store.get(record["episodeId"])["status"] == "COMPLETED"


def test_job_media_mismatch_rejected(aws, wav):
    record = add(aws, wav)
    with pytest.raises(ValueError): sync_job(aws["store"], record, {"Media": {"MediaFileUri": "s3://wrong/else"}, "TranscriptionJobStatus": "COMPLETED"})


def test_partial_batch_returns_only_failed_message_ids():
    event = {"Records": [{"messageId": "ok", "body": json.dumps({"episodeId": identity("ok")})},
                         {"messageId": "bad", "body": "not json"}]}
    with patch("podcastlens.worker.process") as process_mock:
        assert handler(event, None) == {"batchItemFailures": [{"itemIdentifier": "bad"}]}
        assert process_mock.call_count == 1


def test_permission_failure_is_not_mistaken_for_missing_job():
    class Denied:
        def get_transcription_job(self, **kw):
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "denied"}}, "GetTranscriptionJob")
    with pytest.raises(ClientError): find_job(Denied(), "name")


@pytest.mark.parametrize("address", ["127.0.0.1", "169.254.169.254", "10.0.0.1", "::1", "::ffff:127.0.0.1"])
def test_feed_download_rejects_private_addresses(address):
    with patch("socket.getaddrinfo", return_value=[(0,0,0,"",(address,443))]):
        with pytest.raises(ValueError): checked_target("https://example.com/feed", {"example.com"})


@pytest.mark.parametrize("url", ["http://example.com/feed", "https://example.com:444/feed", "https://user:pw@example.com/feed", "https://other.example/feed"])
def test_feed_download_requires_https_and_allowlist(url):
    with pytest.raises(ValueError): checked_target(url, {"example.com"})


def test_public_target_is_pinned():
    with patch("socket.getaddrinfo", return_value=[(0,0,0,"",("93.184.216.34",443))]):
        assert checked_target("https://example.com/feed?a=1", {"example.com"}) == ("example.com", "93.184.216.34", "/feed?a=1")


def test_rss_duplicate_identity_stays_stable():
    xml=b'<rss><channel><item><title>Test</title><guid>42</guid><enclosure url="https://audio.example/test.mp3"/></item></channel></rss>'
    one=list(episodes(xml, "https://feed.example/rss"))[0]
    two=list(episodes(xml.replace(b'Test',b'New title'), "https://feed.example/rss"))[0]
    assert one["episodeId"] == two["episodeId"]


def test_xml_entity_expansion_is_rejected():
    xml=b'<!DOCTYPE rss [<!ENTITY x "boom">]><rss><channel><item><title>&x;</title><enclosure url="https://example.com/test.mp3"/></item></channel></rss>'
    with pytest.raises(Exception): list(episodes(xml, "https://example.com/feed"))


def test_search_is_bounded_and_unicode_safe():
    doc={"results":{"transcripts":[{"transcript":"Straße cloud CLOUD cloud"}]}}
    assert len(search_transcript(doc,"cloud",limit=2)) == 2
    assert search_transcript(doc,"STRASSE")[0]["offset"] == 0


def test_empty_query_rejected():
    with pytest.raises(ValueError): search_transcript({}," ")


def test_reconciliation_finds_a_missed_completion(aws, wav):
    record=add(aws,wav); tc=TranscribeFake(); key=record["episodeId"]
    process(key,aws["store"],aws["s3"],tc)
    tc.jobs[job_name(os.environ["INPUT_BUCKET"],key)]["TranscriptionJobStatus"]="COMPLETED"
    import podcastlens.status as status
    original=status.boto3.client
    with patch.object(status.boto3,"client",side_effect=lambda service,*a,**kw: tc if service=="transcribe" else original(service,*a,**kw)):
        status.reconcile({},None)
    assert aws["store"].get(key)["status"]=="COMPLETED"


def test_infrastructure_defaults_are_bounded():
    from pathlib import Path
    template=json.loads((Path(__file__).parents[1]/"template.json").read_text())
    assert template["Parameters"]["FeedScheduleEnabled"]["Default"]=="false"
    assert template["Parameters"]["MonthlyCapSeconds"]["Default"]==600
    mapping=template["Resources"]["WorkerMapping"]["Properties"]
    assert mapping["FunctionResponseTypes"]==["ReportBatchItemFailures"]
    assert mapping["ScalingConfig"]["MaximumConcurrency"]==2
    assert template["Resources"]["JobsQueue"]["Properties"]["VisibilityTimeout"] >= 6*template["Resources"]["Worker"]["Properties"]["Timeout"]


def test_scanner_discovers_and_queues_a_feed_episode_once(aws, wav):
    import podcastlens.feeds as feeds
    config={"pk":"CONFIG#FEEDS","feeds":["https://example.com/rss"],"allowedHosts":["example.com"]}
    aws["store"].db.put_item(TableName=aws["store"].name,Item=marshal(config))
    xml=b'<rss><channel><item><guid>one</guid><title>Speech</title><enclosure url="https://example.com/audio.wav"/></item></channel></rss>'
    with patch.object(feeds,"fetch",side_effect=lambda url,*a:xml if url.endswith('/rss') else wav):
        first=feeds.scanner({},None); second=feeds.scanner({},None)
    assert first["ingested"]==1 and second["ingested"]==0
    assert len(aws["sqs"].receive_message(QueueUrl=aws["queue"],MaxNumberOfMessages=10)["Messages"])==1


def test_scanner_retries_download_errors_on_next_scan(aws, wav):
    import podcastlens.feeds as feeds
    aws["store"].db.put_item(TableName=aws["store"].name,Item=marshal({"pk":"CONFIG#FEEDS","feeds":["https://example.com/rss"],"allowedHosts":["example.com"]}))
    xml=b'<rss><channel><item><guid>one</guid><enclosure url="https://example.com/audio.wav"/></item></channel></rss>'
    with patch.object(feeds,"fetch",side_effect=[xml,ValueError('remote failure')]):
        assert feeds.scanner({},None)["failures"]==1
    with patch.object(feeds,"fetch",side_effect=[xml,wav]):
        assert feeds.scanner({},None)["ingested"]==1


def test_reconciliation_does_not_requeue_poison_submission(aws,wav):
    from podcastlens.status import reconcile
    record=add(aws,wav)
    aws["store"].reserve(record["episodeId"],15,"2026-11",60)
    # Empty the initial queue so a mistaken re-enqueue is visible.
    aws["sqs"].purge_queue(QueueUrl=aws["queue"])
    import podcastlens.status as status
    original=status.boto3.client
    with patch.object(status.boto3,"client",side_effect=lambda service,*a,**kw: TranscribeFake() if service=="transcribe" else original(service,*a,**kw)):
        reconcile({},None)
    assert "Messages" not in aws["sqs"].receive_message(QueueUrl=aws["queue"])


def test_collector_ignores_other_stacks(aws):
    from podcastlens.status import collector
    assert collector({"detail":{"TranscriptionJobName":"another-application"}},None)=={"ignored":True}
