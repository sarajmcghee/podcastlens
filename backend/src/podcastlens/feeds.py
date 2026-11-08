import hashlib
import http.client
import ipaddress
import json
import os
import socket
import ssl
from urllib.parse import urljoin, urlsplit

import boto3
from botocore.exceptions import ClientError
from defusedxml import ElementTree

from podcastlens.core import identity, inspect_audio, log, metric
from podcastlens.store import Store, code, enqueue


def checked_target(url, allowed_hosts):
    target = urlsplit(url)
    host = (target.hostname or "").encode("idna").decode("ascii").lower()
    if (target.scheme != "https" or target.username or target.password or target.fragment or
            target.port not in (None, 443) or host not in allowed_hosts or len(url) > 4096):
        raise ValueError("URL must be HTTPS on an explicitly allowed host")
    addresses = {x[4][0] for x in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
    if not addresses or any(not ipaddress.ip_address(x).is_global for x in addresses):
        raise ValueError("Non-public network destination rejected")
    return host, sorted(addresses)[0], (target.path or "/") + ("?" + target.query if target.query else "")


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address):
        super().__init__(host, 443, timeout=15, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        # Connect to the exact address that passed validation, while validating
        # the certificate and sending SNI for the original hostname.
        raw = socket.create_connection((self.address, 443), timeout=self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def fetch(url, allowed_hosts, max_bytes):
    for _ in range(4):
        host, address, path = checked_target(url, allowed_hosts)
        connection = PinnedHTTPS(host, address)
        try:
            connection.request("GET", path, headers={"User-Agent": "PodcastLens/1.0", "Accept-Encoding": "identity"})
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location:
                    raise ValueError("Redirect without location")
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise ValueError("Remote endpoint returned a non-success status")
            if response.getheader("Content-Encoding", "identity") not in {"identity", ""}:
                raise ValueError("Compressed HTTP responses are not supported")
            length = response.getheader("Content-Length")
            if length and int(length) > max_bytes:
                raise ValueError("Download exceeds configured size limit")
            data = response.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise ValueError("Download exceeds configured size limit")
            return data
        finally:
            connection.close()
    raise ValueError("Too many redirects")


def episodes(xml, feed_url):
    if len(xml) > 1048576:
        raise ValueError("Feed exceeds 1 MiB")
    root = ElementTree.fromstring(xml)
    # Intentionally RSS 2.0 only. Atom and authenticated feeds are not supported.
    for item in root.findall("./channel/item"):
        enclosure = item.find("enclosure")
        if enclosure is None or not enclosure.get("url"):
            continue
        url = urljoin(feed_url, enclosure.get("url"))
        guid = item.findtext("guid") or url
        yield {"episodeId": identity(feed_url + "\n" + guid), "title": (item.findtext("title") or "Untitled")[:200], "url": url}


def register_audio(episode_id, title, data, source="upload", store=None, s3=None, sqs=None):
    store, s3 = store or Store(), s3 or boto3.client("s3")
    existing = store.get(episode_id)
    digest = hashlib.sha256(data).hexdigest()
    if existing:
        if existing["contentHash"] != digest:
            raise ValueError("An episode identity cannot be replaced with different media")
        if existing.get("enqueuePending"):
            enqueue(store, existing, sqs)
        return existing
    limit = int(os.environ.get("MAX_MEDIA_BYTES", "26214400"))
    if len(data) > limit:
        raise ValueError("Media exceeds size limit")
    media_format, seconds = inspect_audio(data, int(os.environ.get("MAX_EPISODE_SECONDS", "300")))
    key = f"audio/{episode_id}/{digest}.{media_format}"
    try:
        s3.put_object(Bucket=os.environ["INPUT_BUCKET"], Key=key, Body=data,
                      ServerSideEncryption="AES256", IfNoneMatch="*")
    except ClientError as exc:
        if code(exc) != "PreconditionFailed":
            raise
    record = store.create({"episodeId": episode_id, "title": title[:200], "objectKey": key,
                           "contentHash": digest, "source": source, "estimatedSeconds": seconds})
    enqueue(store, record, sqs)
    return record


def scanner(event, context):
    store = Store()
    config = store.get_key("CONFIG#FEEDS") or {}
    feeds = config.get("feeds", [])
    hosts = {x.lower() for x in config.get("allowedHosts", [])}
    ingested = failures = attempted = 0
    # Limit work per invocation. Read only recent feed entries (first 20 each).
    for feed_url in feeds[:5]:
        if attempted >= 3:
            break
        try:
            parsed = list(episodes(fetch(feed_url, hosts, 1048576), feed_url))[:20]
            for episode in parsed:
                if context and context.get_remaining_time_in_millis() < 45000:
                    break
                if attempted >= 3:
                    break
                existing = store.get(episode["episodeId"])
                if existing:
                    if existing.get("enqueuePending"):
                        enqueue(store, existing)
                    continue
                attempted += 1
                try:
                    data = fetch(episode["url"], hosts, int(os.environ.get("MAX_MEDIA_BYTES", "26214400")))
                except Exception as exc:
                    failures += 1
                    log("feed_download_failure", episodeId=episode["episodeId"], errorType=type(exc).__name__)
                    continue
                try:
                    register_audio(episode["episodeId"], episode["title"], data, source="rss", store=store)
                    ingested += 1
                except ValueError:
                    # Permanently rejected media should not be downloaded on every scan.
                    record = store.create({"episodeId": episode["episodeId"], "title": episode["title"], "source": "rss"})
                    store.transition(record["episodeId"], "FAILED", errorCode="MEDIA_REJECTED")
                    store.enqueued(record["episodeId"])
                    failures += 1
                except Exception as exc:
                    failures += 1
                    log("feed_episode_failure", episodeId=episode["episodeId"], errorType=type(exc).__name__)
        except Exception as exc:
            failures += 1
            log("feed_failure", errorType=type(exc).__name__)
    metric("FeedFailures", failures)
    log("feed_scan_finished", ingested=ingested, failures=failures)
    return {"ingested": ingested, "failures": failures}
