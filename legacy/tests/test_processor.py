"""Unit tests for the parts that decide how much audio - and therefore how much
money - each episode costs. Run with: python3 tests/test_processor.py

boto3 and the AWS environment are stubbed out, so these run offline with no
credentials and no charges.
"""

import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

# Stub boto3/botocore so importing the handlers does not require AWS.
def _stub(name, **attrs):
    mod = types.ModuleType(name)
    mod.__path__ = []          # makes it importable as a package
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod

_stub("boto3", client=lambda *a, **k: None, resource=lambda *a, **k: None)
_stub("boto3.dynamodb")
_stub("boto3.dynamodb.conditions", Key=object, Attr=object)
_stub("botocore")
_stub("botocore.exceptions", ClientError=type("ClientError", (Exception,), {}))

from pipeline import processor  # noqa: E402
from shared import rss  # noqa: E402


class ClipSizing(unittest.TestCase):
    """The clip must never contain more audio than we reserved budget for."""

    def seconds_of_audio(self, limit_bytes, real_bytes_per_sec):
        return limit_bytes / real_bytes_per_sec

    def test_good_metadata_yields_an_accurate_clip(self):
        # Lex #501: 250 MB over 321m57s -> ~104 kbps.
        limit = processor._clip_bytes(19317, 250_000_000, 600)
        self.assertAlmostEqual(self.seconds_of_audio(limit, 250_000_000 / 19317), 600, delta=5)

    def test_missing_duration_never_overshoots(self):
        # Lex #500 declares no duration and a placeholder 5 MiB length.
        limit = processor._clip_bytes(0, 5_242_880, 600)
        real_bps = 13_000  # what the file actually is
        self.assertLessEqual(self.seconds_of_audio(limit, real_bps), 600)

    def test_implausible_bitrate_is_rejected(self):
        # A 5 MiB placeholder length against a real 5-hour duration implies
        # 291 B/s, which is not audio. Fall back rather than believe it.
        limit = processor._clip_bytes(18000, 5_242_880, 600)
        fallback = processor._clip_bytes(0, 0, 600)
        self.assertEqual(limit, fallback)

    def test_absurdly_high_bitrate_is_rejected(self):
        limit = processor._clip_bytes(60, 500_000_000, 600)
        self.assertEqual(limit, processor._clip_bytes(0, 0, 600))

    def test_never_exceeds_the_hard_byte_ceiling(self):
        limit = processor._clip_bytes(3600, 100_000_000, 999_999)
        self.assertLessEqual(limit, processor.MAX_AUDIO_BYTES)


class Id3Parsing(unittest.TestCase):
    """The bug that made Transcribe reject our first real episode."""

    @staticmethod
    def header(size_bytes):
        """Build an ID3v2.3 header declaring a tag of `size_bytes` total."""
        n = size_bytes - 10
        syncsafe = bytes(((n >> 21) & 0x7F, (n >> 14) & 0x7F,
                          (n >> 7) & 0x7F, n & 0x7F))
        return b"ID3\x03\x00\x00" + syncsafe

    def test_reads_the_real_lex_header(self):
        # Verbatim first 10 bytes of Lex Fridman #501: an 18 MB tag of chapter
        # markers. A 4.8 MB "clip" of this file contains no audio at all.
        head = bytes.fromhex("4944 3303 0000 0853 5f2b".replace(" ", ""))
        # 18,149,291 bytes of tag body, plus the 10-byte header.
        self.assertEqual(rss.id3v2_size(head), 18_149_301)

    def test_roundtrip(self):
        self.assertEqual(rss.id3v2_size(self.header(4096)), 4096)

    def test_no_tag(self):
        self.assertEqual(rss.id3v2_size(b"\xff\xfb" + b"\x00" * 8), 0)
        self.assertEqual(rss.id3v2_size(b"short"), 0)

    def test_rejects_non_syncsafe_size(self):
        # High bit set is invalid in a syncsafe integer. Trusting it would skip
        # a wildly wrong number of bytes.
        self.assertEqual(rss.id3v2_size(b"ID3\x03\x00\x00\xff\x00\x00\x00"), 0)

    def test_finds_frame_sync(self):
        self.assertEqual(rss.find_frame_sync(b"\x00\x00\xff\xfb\x90\x00"), 2)
        self.assertEqual(rss.find_frame_sync(b"\xff\xfb\x90\x00"), 0)

    def test_frame_sync_absent_returns_zero(self):
        self.assertEqual(rss.find_frame_sync(b"\x00" * 100), 0)


class LimitedReaderTest(unittest.TestCase):
    """Range is a request the origin may ignore, so we enforce it locally too."""

    def test_stops_at_the_limit(self):
        import io
        reader = processor.LimitedReader(io.BytesIO(b"x" * 10_000), 1_000)
        chunks, total = [], 0
        while True:
            chunk = reader.read(256)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        self.assertEqual(total, 1_000)

    def test_unbounded_read_still_respects_the_limit(self):
        import io
        reader = processor.LimitedReader(io.BytesIO(b"x" * 10_000), 1_500)
        self.assertEqual(len(reader.read(-1)), 1_500)

    def test_prefix_is_served_before_the_stream(self):
        import io
        reader = processor.LimitedReader(io.BytesIO(b"B" * 100), 10, prefix=b"AAAA")
        out = b""
        while True:
            chunk = reader.read(3)
            if not chunk:
                break
            out += chunk
        self.assertEqual(out, b"AAAA" + b"B" * 6)


class DurationParsing(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(rss.parse_duration("3600"), 3600)
        self.assertEqual(rss.parse_duration("45:30"), 2730)
        self.assertEqual(rss.parse_duration("05:21:57"), 19317)
        self.assertEqual(rss.parse_duration(""), 0)
        self.assertEqual(rss.parse_duration("not a duration"), 0)


class FeedParsing(unittest.TestCase):
    SAMPLE = b"""<?xml version="1.0"?>
    <rss xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
      <channel>
        <title>Test Show</title>
        <description>&lt;p&gt;Hello &lt;b&gt;there&lt;/b&gt;&lt;/p&gt;</description>
        <itunes:image href="https://example.com/art.jpg"/>
        <item>
          <title>Episode One</title>
          <pubDate>Tue, 26 Aug 2025 21:47:04 +0000</pubDate>
          <itunes:duration>1:30:00</itunes:duration>
          <enclosure url="https://example.com/1.mp3" length="70000000" type="audio/mpeg"/>
          <guid>ep-1</guid>
        </item>
        <item>
          <title>No audio, should be skipped</title>
          <guid>ep-2</guid>
        </item>
      </channel>
    </rss>"""

    def test_parses_and_skips_items_without_audio(self):
        feed = rss.parse(self.SAMPLE)
        self.assertEqual(feed["title"], "Test Show")
        self.assertEqual(feed["image"], "https://example.com/art.jpg")
        self.assertNotIn("<b>", feed["description"])
        self.assertEqual(len(feed["episodes"]), 1)
        ep = feed["episodes"][0]
        self.assertEqual(ep["durationSeconds"], 5400)
        self.assertTrue(ep["publishedAt"].startswith("2025-08-26T21:47:04"))

    def test_rejects_non_rss(self):
        with self.assertRaises(ValueError):
            rss.parse(b"<html><body>not a feed</body></html>")


if __name__ == "__main__":
    unittest.main(verbosity=2)
