import json
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sources.hybrid_support import (
    extract_explicit_references,
    load_state,
    save_state,
    should_scan_now,
    update_state_from_results,
)
from sources import chuoichien, luongson

VN = ZoneInfo("Asia/Ho_Chi_Minh")
NOW = datetime(2026, 7, 20, 10, 0, tzinfo=VN)


class ExplicitReferenceTests(unittest.TestCase):
    def test_extract_embed_and_stream_url_without_global_noise(self):
        text = '''
        <script>const globalList = ["https://cdn.example/live/wrong.flv"];</script>
        <iframe src="https://player.example/embed/?streamUrl=https%3A%2F%2Fcdn.example%2Flive%2Fright%2Fplaylist.m3u8&autoplay=1"></iframe>
        '''
        refs = extract_explicit_references(text, "https://site.example/match")
        urls = {item["url"] for item in refs}
        self.assertIn(
            "https://player.example/embed/?streamUrl=https://cdn.example/live/right/playlist.m3u8&autoplay=1",
            urls,
        )
        self.assertIn("https://cdn.example/live/right/playlist.m3u8", urls)
        self.assertNotIn("https://cdn.example/live/wrong.flv", urls)

    def test_http_source_is_high_confidence(self):
        entry = {"sources": ["http/iframe"]}
        self.assertTrue(chuoichien._entry_is_high_confidence_observed(entry))
        self.assertTrue(luongson._entry_is_high_confidence_observed(entry))


class DeltaStateTests(unittest.TestCase):
    def test_far_match_is_deferred_until_next_scan(self):
        match = {"minutes_to_kickoff": 100}
        state = {"next_scan_at": (NOW + timedelta(minutes=15)).isoformat(), "has_verified": False}
        due, reason = should_scan_now(match, state, NOW, near_minutes=45)
        self.assertFalse(due)
        self.assertTrue(reason.startswith("delta-wait-"))

    def test_near_match_always_scans(self):
        match = {"minutes_to_kickoff": 30}
        state = {"next_scan_at": (NOW + timedelta(hours=1)).isoformat(), "has_verified": False}
        due, reason = should_scan_now(match, state, NOW, near_minutes=45)
        self.assertTrue(due)
        self.assertEqual(reason, "near-or-live")

    def test_verified_stream_is_rechecked(self):
        match = {"minutes_to_kickoff": 100}
        state = {"next_scan_at": (NOW + timedelta(hours=1)).isoformat(), "has_verified": True}
        due, reason = should_scan_now(match, state, NOW, near_minutes=45)
        self.assertTrue(due)
        self.assertEqual(reason, "cached-stream-needs-recheck")


    def test_pending_stream_is_rechecked_to_prevent_playlist_loss(self):
        match = {"minutes_to_kickoff": 100}
        state = {
            "next_scan_at": (NOW + timedelta(hours=1)).isoformat(),
            "has_stream": True,
            "has_verified": False,
        }
        due, reason = should_scan_now(match, state, NOW, near_minutes=45)
        self.assertTrue(due)
        self.assertEqual(reason, "cached-stream-needs-recheck")

    def test_state_roundtrip(self):
        rows = {}
        results = [{
            "url": "https://live04.chuoichientv.me/live/123/a-vs-b",
            "match_name": "A vs B",
            "kickoff_iso": (NOW + timedelta(hours=1)).isoformat(),
            "minutes_to_kickoff": 60,
            "streams": [{"url": "https://cdn/live/a.flv", "playability": "verified"}],
        }]
        update_state_from_results(rows, results, chuoichien.match_id_from_url, NOW)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            save_state(path, rows, "test")
            loaded = load_state(path)
        self.assertIn("123", loaded)
        self.assertTrue(loaded["123"]["has_verified"])


class _FakeResponse:
    def __init__(self, body: str, status: int = 200, content_type: str = "text/html"):
        self._body = body.encode("utf-8")
        self.status = status
        self.headers = {"content-type": content_type}

    async def body(self):
        return self._body


class _FakeRequestContext:
    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    async def get(self, url, **_kwargs):
        self.calls.append(url)
        response = self.mapping[url]
        response.url = url
        return response


class _FakeBrowserContext:
    def __init__(self, mapping):
        self.request = _FakeRequestContext(mapping)


class HttpFirstDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_chuoichien_follows_embed_without_opening_page(self):
        match_url = "https://live04.chuoichientv.me/live/123/a-vs-b?blv=angao"
        embed_url = "https://live.chuoichien.tv/embed/?id=123&streamUrl=https%3A%2F%2Fcdn.example%2Flive%2Fangao%2Fplaylist.m3u8"
        context = _FakeBrowserContext({
            match_url: _FakeResponse(f'<iframe src="{embed_url}"></iframe>'),
            "https://live.chuoichien.tv/embed/?id=123&streamUrl=https://cdn.example/live/angao/playlist.m3u8": _FakeResponse('<video src="https://cdn.example/live/angao/playlist.m3u8"></video>'),
        })
        captured = []

        def capture(url, source, **_kwargs):
            for stream in chuoichien.extract_stream_urls(url):
                captured.append((stream, source))

        count = await chuoichien.discover_http_candidates(context, {"url": match_url, "errors": []}, capture)
        self.assertGreaterEqual(count, 2)
        self.assertTrue(any(url == "https://cdn.example/live/angao/playlist.m3u8" for url, _ in captured))
        self.assertEqual(len(context.request.calls), 1)


class _NoPageContext:
    async def new_page(self):
        raise AssertionError("Chromium page must not be opened")


class BrowserFallbackPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_far_upcoming_without_http_stream_skips_chromium(self):
        match = {
            "url": "https://live04.chuoichientv.me/live/777/a-vs-b",
            "raw_title": "A vs B",
            "raw_time": "12:00 20/07",
            "minutes_to_kickoff": 100,
            "sport_group": "Bóng đá",
        }
        with patch.object(chuoichien, "discover_http_candidates", new=AsyncMock(return_value=0)):
            result = await chuoichien.fetch_stream(_NoPageContext(), match, __import__("asyncio").Semaphore(1))
        self.assertEqual(result.get("scan_decision"), "http-only-far-upcoming")
        self.assertEqual(result.get("streams"), [])

    async def test_two_verified_http_streams_skip_chromium(self):
        match = {
            "url": "https://live04.chuoichientv.me/live/778/a-vs-b?blv=angao",
            "raw_title": "A vs B",
            "raw_time": "10:10 20/07",
            "minutes_to_kickoff": 10,
            "sport_group": "Bóng đá",
        }

        async def fake_discover(_context, _match, capture):
            capture("https://cdn.example/live/angao/playlist.m3u8", "http/stream", frame_url=_match["url"])
            capture("https://cdn.example/live/angaohd/playlist.m3u8", "http/stream", frame_url=_match["url"])
            return 2

        verified = [
            {"url": "https://cdn.example/live/angaohd/playlist.m3u8", "playability": "verified", "quality": "FHD"},
            {"url": "https://cdn.example/live/angao/playlist.m3u8", "playability": "verified", "quality": "HD"},
        ]
        with patch.object(chuoichien, "discover_http_candidates", new=fake_discover), \
             patch.object(chuoichien, "finalize_stream_map", new=AsyncMock(return_value=(verified, []))):
            result = await chuoichien.fetch_stream(_NoPageContext(), match, __import__("asyncio").Semaphore(1))
        self.assertEqual(result.get("scan_decision"), "http-first-complete")
        self.assertEqual(len(result.get("streams") or []), 2)


class WindowDefaultsTests(unittest.TestCase):
    def test_past_window_is_150_minutes(self):
        self.assertEqual(chuoichien.SCAN_PAST_MINUTES, 150)
        self.assertEqual(luongson.SCAN_PAST_MINUTES, 150)


if __name__ == "__main__":
    unittest.main()
