from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from merger import SourceFiles, cleanup_intermediate_playlists, merge_sources

TZ = ZoneInfo("Asia/Ho_Chi_Minh")


def write_playlist(path: Path, rows: list[dict[str, str]], pipe: bool = False) -> None:
    lines = ["#EXTM3U"]
    for row in rows:
        lines.extend([
            f'#EXTINF:-1 tvg-id="{row["id"]}" tvg-name="{row["name"]}" group-title="{row.get("group", "Bóng đá")}",{row["name"]}',
            "#EXTVLCOPT:http-referrer=https://example.test/",
            "#EXTVLCOPT:http-user-agent=UA",
            '#EXTHTTP:{"User-Agent":"UA","Referer":"https://example.test/"}',
            row["url"] + ("|User-Agent=UA&Referer=https://example.test/" if pipe else ""),
        ])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class MergerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = datetime(2026, 7, 20, 7, 0, tzinfo=TZ)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def make_source(self, key: str, label: str, rows: list[dict[str, str]], debug_rows: list[dict]) -> SourceFiles:
        universal = self.root / f"{key}.m3u"
        pipe = self.root / f"{key}_pipe.m3u"
        vlc = self.root / f"{key}_vlc.m3u"
        debug = self.root / f"{key}.json"
        write_playlist(universal, rows)
        write_playlist(pipe, rows, pipe=True)
        write_playlist(vlc, rows)
        debug.write_text(json.dumps(debug_rows, ensure_ascii=False), encoding="utf-8")
        return SourceFiles(key, label, universal, pipe, vlc, debug)

    def test_dedupe_and_quality_cap(self) -> None:
        match_name = "USA vs Poland - Nations League"
        rows_a = [
            {"id": "cc-1", "name": f"[08:00 20/07] {match_name} [BLV A] [FHD M3U8]", "url": "https://cdn/xhd/playlist.m3u8"},
            {"id": "cc-2", "name": f"[08:00 20/07] {match_name} [BLV A] [FHD FLV]", "url": "https://cdn/xhd.flv"},
            {"id": "cc-3", "name": f"[08:00 20/07] {match_name} [BLV A] [HD M3U8]", "url": "https://cdn/x/playlist.m3u8"},
        ]
        debug_a = [{
            "match_name": match_name, "date": "20/07/2026", "time": "08:00", "blv": "A",
            "streams": [
                {"url": "https://cdn/xhd/playlist.m3u8", "quality": "FHD", "playability": "verified", "http_status": 200},
                {"url": "https://cdn/xhd.flv", "quality": "FHD", "playability": "verified", "http_status": 200},
                {"url": "https://cdn/x/playlist.m3u8", "quality": "HD", "playability": "verified", "http_status": 200},
            ],
        }]
        rows_b = [{"id": "ls-1", "name": f"[20/07/2026 08:00] {match_name} [BLV A] [FHD M3U8]", "url": "https://cdn/xhd/playlist.m3u8"}]
        debug_b = [{"match_name": match_name, "date": "20/07/2026", "time": "08:00", "blv": "A", "streams": [{"url": "https://cdn/xhd/playlist.m3u8", "quality": "FHD", "playability": "verified"}]}]
        report = merge_sources(self.root, [self.make_source("cc", "CC", rows_a, debug_a), self.make_source("ls", "LS", rows_b, debug_b)], now=self.now, max_per_match=2, preserve_on_empty=False)
        self.assertEqual(report["selected_count"], 2)
        content = (self.root / "all_live.m3u").read_text(encoding="utf-8")
        self.assertEqual(content.count("https://cdn/xhd/playlist.m3u8"), 1)
        self.assertIn("https://cdn/x/playlist.m3u8", content)
        self.assertNotIn("https://cdn/xhd.flv", content)
        self.assertNotIn("|User-Agent=", content)
        self.assertFalse((self.root / "all_live_pipe.m3u").exists())
        self.assertFalse((self.root / "all_live_vlc.m3u").exists())

    def test_upcoming_four_hours_only(self) -> None:
        rows = [
            {"id": "a", "name": "Soon vs Team [FHD M3U8]", "url": "https://cdn/soon/playlist.m3u8"},
            {"id": "b", "name": "Far vs Team [FHD M3U8]", "url": "https://cdn/far/playlist.m3u8"},
        ]
        soon = self.now + timedelta(hours=4)
        far = self.now + timedelta(hours=4, minutes=1)
        debug = [
            {"match_name": "Soon vs Team", "kickoff_iso": soon.isoformat(), "streams": [{"url": "https://cdn/soon/playlist.m3u8", "quality": "FHD", "playability": "upcoming-pending"}]},
            {"match_name": "Far vs Team", "kickoff_iso": far.isoformat(), "streams": [{"url": "https://cdn/far/playlist.m3u8", "quality": "FHD", "playability": "upcoming-pending"}]},
        ]
        report = merge_sources(self.root, [self.make_source("cc", "CC", rows, debug)], now=self.now, max_per_match=2, upcoming_hours=4, preserve_on_empty=False)
        self.assertEqual(report["selected_count"], 1)
        content = (self.root / "all_live.m3u").read_text(encoding="utf-8")
        self.assertIn("/soon/", content)
        self.assertNotIn("/far/", content)

    def test_previous_fallback_is_rejected(self) -> None:
        rows = [{"id": "a", "name": "Dead vs Link [FLV]", "url": "https://cdn/dead.flv"}]
        debug = [{"match_name": "Dead vs Link", "streams": [{"url": "https://cdn/dead.flv", "playability": "previous-fallback"}]}]
        report = merge_sources(self.root, [self.make_source("cc", "CC", rows, debug)], now=self.now, preserve_on_empty=False)
        self.assertEqual(report["selected_count"], 0)
        self.assertEqual((self.root / "all_live.m3u").read_text(encoding="utf-8"), "#EXTM3U\n")

    def test_different_commentators_are_kept(self) -> None:
        rows = [
            {"id": "a", "name": "A vs B [BLV Một] [FHD M3U8]", "url": "https://cdn/one/playlist.m3u8"},
            {"id": "b", "name": "A vs B [BLV Hai] [FHD M3U8]", "url": "https://cdn/two/playlist.m3u8"},
        ]
        debug = [
            {"match_name": "A vs B", "blv": "Một", "streams": [{"url": "https://cdn/one/playlist.m3u8", "quality": "FHD", "playability": "verified"}]},
            {"match_name": "A vs B", "blv": "Hai", "streams": [{"url": "https://cdn/two/playlist.m3u8", "quality": "FHD", "playability": "verified"}]},
        ]
        report = merge_sources(self.root, [self.make_source("cc", "CC", rows, debug)], now=self.now, max_per_match=1, preserve_on_empty=False)
        self.assertEqual(report["selected_count"], 2)

    def test_cleanup_leaves_only_all_live_m3u(self) -> None:
        (self.root / "all_live.m3u").write_text("#EXTM3U\n", encoding="utf-8")
        for name in ("chuoichien_live.m3u", "hygenie_live.m3u", "all_live_pipe.m3u", "all_live_vlc.m3u"):
            (self.root / name).write_text("#EXTM3U\n", encoding="utf-8")
        removed = cleanup_intermediate_playlists(self.root)
        self.assertEqual(sorted(removed), sorted(["chuoichien_live.m3u", "hygenie_live.m3u", "all_live_pipe.m3u", "all_live_vlc.m3u"]))
        self.assertEqual([path.name for path in self.root.glob("*.m3u")], ["all_live.m3u"])


if __name__ == "__main__":
    unittest.main()
