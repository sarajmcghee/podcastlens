import json
import os
from pathlib import Path
from types import SimpleNamespace

import boto3
import pytest

from podcastlens import api
from podcastlens.core import month
from podcastlens.store import marshal

EPISODE = "a" * 64


def event(route, **kwargs):
    return {"routeKey": route, "requestContext": {"authorizer": {"jwt": {"claims": {
        "sub": "operator-1", "token_use": "access", "scope": "openid podcastlens/access", "cognito:groups": "[operators]"
    }}}}, **kwargs}


def call(route, **kwargs):
    result = api.handler(event(route, **kwargs), None)
    return result["statusCode"], json.loads(result["body"])


@pytest.mark.parametrize("claims", [{}, {"sub":"x","token_use":"id","scope":"podcastlens/access","cognito:groups":"operators"},
    {"sub":"x","token_use":"access","scope":"openid","cognito:groups":"operators"},
    {"sub":"x","token_use":"access","scope":"podcastlens/access","cognito:groups":"not-operators"}])
def test_access_denied_before_data_access(monkeypatch, claims):
    monkeypatch.setattr(api, "Store", lambda: pytest.fail("Unauthorized request accessed storage"))
    request = event("GET /episodes")
    request["requestContext"]["authorizer"]["jwt"]["claims"] = claims
    assert api.handler(request, None)["statusCode"] == 403


def test_list_reads_existing_episode_schema_and_pages(aws):
    store = aws["store"]
    for i in range(105):
        store.create({"episodeId": f"{i:064x}", "title": f"Recording {i}", "contentHash": "private", "objectKey": "private"})
    store.db.put_item(TableName=store.name, Item=marshal({"pk":"BUDGET#2026-11","usedSeconds":15}))
    status, first = call("GET /episodes")
    assert status == 200 and first["nextCursor"]
    _, second = call("GET /episodes", queryStringParameters={"cursor": first["nextCursor"]})
    rows = first["episodes"] + second["episodes"]
    assert len({row["episodeId"] for row in rows}) == 105
    assert all("pk" not in row and "contentHash" not in row and "objectKey" not in row for row in rows)


def test_transcript_and_search_use_existing_s3_json(aws):
    store = aws["store"]
    store.create({"episodeId": EPISODE, "title": "First recording"})
    key = f"transcripts/{EPISODE}.json"
    store.transition(EPISODE, "COMPLETED", transcriptKey=key)
    aws["s3"].put_object(Bucket=os.environ["OUTPUT_BUCKET"], Key=key,
        Body=json.dumps({"results":{"transcripts":[{"transcript":"I saw a hummingbird."}]}}))
    path = {"episodeId": EPISODE}
    status, result = call("GET /episodes/{episodeId}/transcript", pathParameters=path)
    assert status == 200 and result["text"] == "I saw a hummingbird."
    status, result = call("GET /episodes/{episodeId}/transcript", pathParameters=path, queryStringParameters={"q":"hummingbird"})
    assert status == 200 and len(result["matches"]) == 1
    assert "hummingbird" in result["matches"][0]["context"]


def test_transcript_not_ready_missing_and_expired(aws):
    path = {"episodeId": EPISODE}
    assert call("GET /episodes/{episodeId}/transcript", pathParameters=path)[0] == 404
    aws["store"].create({"episodeId":EPISODE})
    assert call("GET /episodes/{episodeId}/transcript", pathParameters=path)[0] == 409
    aws["store"].transition(EPISODE, "COMPLETED", transcriptKey=f"transcripts/{EPISODE}.json")
    assert call("GET /episodes/{episodeId}/transcript", pathParameters=path)[0] == 404


def test_rejects_arbitrary_transcript_key(aws):
    aws["store"].create({"episodeId":EPISODE})
    aws["store"].transition(EPISODE, "COMPLETED", transcriptKey="private/other.json")
    assert call("GET /episodes/{episodeId}/transcript", pathParameters={"episodeId":EPISODE})[0] == 400


def test_feed_changes_share_scanner_config_and_preserve_episodes(aws):
    status, data = call("POST /feeds", body=json.dumps({"url":"https://feed.example/rss","allowedHosts":["audio.example"]}))
    assert status == 200
    config = aws["store"].get_key("CONFIG#FEEDS")
    assert config["feeds"] == ["https://feed.example/rss"]
    assert config["allowedHosts"] == ["audio.example","feed.example"]
    aws["store"].create({"episodeId":EPISODE})
    assert call("DELETE /feeds/{feedId}", pathParameters={"feedId":data["feeds"][0]["feedId"]})[0] == 200
    assert aws["store"].get_key("CONFIG#FEEDS")["feeds"] == []
    assert aws["store"].get(EPISODE)


@pytest.mark.parametrize("data", [{"url":"http://example.com/rss"}, {"url":"https://user:pass@example.com/rss"},
    {"url":"https://example.com:444/rss"}, {"url":"https://example.com/rss#fragment"},
    {"url":"https://example.com/rss","allowedHosts":["*.example.com"]},
    {"url":"https://example.com/rss","allowedHosts":"example.com"}])
def test_bad_feed_config_does_not_write(aws, data):
    assert call("POST /feeds", body=json.dumps(data))[0] == 400
    assert aws["store"].get_key("CONFIG#FEEDS") is None


def test_concurrent_config_edit_is_not_lost(aws, monkeypatch):
    store = aws["store"]
    call("POST /feeds", body=json.dumps({"url":"https://first.example/rss"}))
    monkeypatch.setattr(api, "Store", lambda: store)
    update = store.db.update_item
    def raced_update(**kwargs):
        store.db.put_item(TableName=store.name, Item=marshal({"pk":"CONFIG#FEEDS", "feeds":["https://cli.example/rss"], "allowedHosts":["cli.example"]}))
        return update(**kwargs)
    monkeypatch.setattr(store.db, "update_item", raced_update)
    assert call("POST /feeds", body=json.dumps({"url":"https://second.example/rss"}))[0] == 409
    assert store.get_key("CONFIG#FEEDS")["feeds"] == ["https://cli.example/rss"]


def test_budget_uses_current_stack_cap_and_existing_counter(aws, monkeypatch):
    aws["store"].db.put_item(TableName=aws["store"].name,Item=marshal({"pk":"BUDGET#"+month(),"usedSeconds":15}))
    monkeypatch.setenv("PIPELINE_STACK","test-pipeline")
    cap = [600]
    fake = SimpleNamespace(describe_stacks=lambda **kw:{"Stacks":[{"Outputs":[{"Description":"MONTHLY_CAP_SECONDS","OutputValue":str(cap[0])}]}]})
    original = boto3.client
    monkeypatch.setattr(api.boto3,"client",lambda name,*a,**kw:fake if name=="cloudformation" else original(name,*a,**kw))
    assert call("GET /budget")[1] == {"month":month(),"usedSeconds":15,"capSeconds":600}
    cap[0] = 120
    assert call("GET /budget")[1]["capSeconds"] == 120


def test_scan_invokes_existing_scanner_asynchronously(aws, monkeypatch):
    calls = []
    monkeypatch.setenv("SCANNER_FUNCTION","existing-scanner")
    fake = SimpleNamespace(invoke=lambda **kw:(calls.append(kw) or {"StatusCode":202}))
    original = boto3.client
    monkeypatch.setattr(api.boto3,"client",lambda name,*a,**kw:fake if name=="lambda" else original(name,*a,**kw))
    assert call("POST /scan")[0] == 202
    assert calls == [{"FunctionName":"existing-scanner","InvocationType":"Event","Payload":b"{}"}]


def test_web_template_protects_every_route_and_reuses_data():
    template = json.loads((Path(__file__).resolve().parents[2]/"infrastructure/web.json").read_text())
    resources = template["Resources"]
    routes = [r["Properties"] for r in resources.values() if r["Type"] == "AWS::ApiGatewayV2::Route"]
    assert len(routes) == 7
    assert all(r["AuthorizationType"] == "JWT" and r["AuthorizationScopes"] == ["podcastlens/access"] for r in routes)
    assert not any(r["Type"] in {"AWS::DynamoDB::Table","AWS::SQS::Queue"} for r in resources.values())
    assert resources["UserPool"]["Properties"]["AdminCreateUserConfig"]["AllowAdminCreateUserOnly"] is True
    statements = resources["ApiRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
    write = next(s for s in statements if "dynamodb:UpdateItem" in s["Action"])
    assert write["Condition"]["ForAllValues:StringEquals"]["dynamodb:LeadingKeys"] == ["CONFIG#FEEDS"]
