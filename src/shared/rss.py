"""Minimal podcast RSS parser built on xml.etree, so we ship no dependencies."""

import re
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
USER_AGENT = "PodcastLens/1.0 (+https://github.com/) AWS-SAA-study-project"


def fetch(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def open_stream(url: str, timeout: int = 60, byte_limit: int = 0):
    """Return a file-like object for the audio, plus its declared length.

    We hand this straight to S3's upload_fileobj so the episode is streamed
    through the Lambda in 8 MB chunks rather than buffered whole in memory.

    byte_limit sends an HTTP Range header asking the podcast CDN for only the
    first N bytes. An MP3 is a flat sequence of independent frames, so a prefix
    of the file is still perfectly decodable audio - it just stops early. This
    is how we transcribe a ten minute clip of a four hour episode without
    downloading, storing, or paying to process the other three and a half hours.
    """
    headers = {"User-Agent": USER_AGENT}
    if byte_limit > 0:
        headers["Range"] = f"bytes=0-{byte_limit - 1}"
    req = urllib.request.Request(url, headers=headers)
    resp = urllib.request.urlopen(req, timeout=timeout)
    length = int(resp.headers.get("Content-Length") or 0)
    # 206 means the CDN honoured the range. 200 means it ignored it and is
    # sending the whole file, which the caller needs to know about.
    ranged = resp.status == 206
    return resp, length, ranged


def _text(node, *paths):
    for path in paths:
        found = node.find(path)
        if found is not None and (found.text or "").strip():
            return found.text.strip()
    return ""


def parse_duration(raw: str) -> int:
    """iTunes duration is either seconds, MM:SS, or HH:MM:SS."""
    raw = (raw or "").strip()
    if not raw:
        return 0
    if raw.isdigit():
        return int(raw)
    parts = raw.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return 0
    seconds = 0
    for n in nums:
        seconds = seconds * 60 + n
    return seconds


def parse_date(raw: str) -> str:
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    except Exception:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")


TAGS = re.compile(r"<[^>]+>")


def strip_html(raw: str, limit: int = 1200) -> str:
    return TAGS.sub(" ", raw or "").replace("&nbsp;", " ").strip()[:limit]


def parse(xml_bytes: bytes) -> dict:
    root = ElementTree.fromstring(xml_bytes)
    channel = root.find("channel")
    if channel is None:
        raise ValueError("not an RSS feed: no <channel> element")

    image = ""
    itunes_image = channel.find(f"{ITUNES}image")
    if itunes_image is not None:
        image = itunes_image.get("href", "")
    if not image:
        image = _text(channel, "image/url")

    episodes = []
    for item in channel.findall("item"):
        enclosure = item.find("enclosure")
        audio_url = enclosure.get("url", "") if enclosure is not None else ""
        if not audio_url:
            continue
        episodes.append({
            "title": _text(item, "title") or "(untitled)",
            "audioUrl": audio_url,
            "audioType": enclosure.get("type", "audio/mpeg"),
            "audioBytes": int(enclosure.get("length") or 0),
            "publishedAt": parse_date(_text(item, "pubDate")),
            "durationSeconds": parse_duration(_text(item, f"{ITUNES}duration")),
            "summary": strip_html(_text(item, "description", f"{ITUNES}summary")),
            "guid": _text(item, "guid") or audio_url,
        })

    return {
        "title": _text(channel, "title") or "(untitled feed)",
        "description": strip_html(_text(channel, "description"), 500),
        "image": image,
        "episodes": episodes,
    }
