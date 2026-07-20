import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from sources import chuoichien, luongson


VN = ZoneInfo("Asia/Ho_Chi_Minh")
NOW = datetime(2026, 7, 20, 10, 0, tzinfo=VN)


class ChuoiChienScanWindowTests(unittest.TestCase):
    def test_boundaries_and_unknown_live(self):
        rows = [
            {"url": "https://live03.chuoichientv.me/live/1/a-vs-b", "raw_title": "A vs B", "raw_time": "07:30 20/07", "card_text": "07:30 20/07"},
            {"url": "https://live03.chuoichientv.me/live/2/c-vs-d", "raw_title": "C vs D", "raw_time": "07:29 20/07", "card_text": "07:29 20/07"},
            {"url": "https://live03.chuoichientv.me/live/3/e-vs-f", "raw_title": "E vs F", "raw_time": "14:00 20/07", "card_text": "14:00 20/07"},
            {"url": "https://live03.chuoichientv.me/live/4/g-vs-h", "raw_title": "G vs H", "raw_time": "14:01 20/07", "card_text": "14:01 20/07"},
            {"url": "https://live03.chuoichientv.me/live/5/i-vs-j", "raw_title": "I vs J", "raw_time": "", "card_text": "Đang diễn ra"},
            {"url": "https://live03.chuoichientv.me/live/6/k-vs-l", "raw_title": "K vs L", "raw_time": "", "card_text": "Sắp diễn ra"},
        ]
        kept, stats = chuoichien.filter_links_by_scan_window(rows, NOW)
        self.assertEqual({row["url"] for row in kept}, {rows[0]["url"], rows[2]["url"], rows[4]["url"]})
        self.assertEqual(stats["window"], 2)
        self.assertEqual(stats["unknown_live"], 1)
        self.assertEqual(stats["past"], 1)
        self.assertEqual(stats["future"], 1)
        self.assertEqual(stats["unknown"], 1)
        self.assertEqual(rows[0]["minutes_to_kickoff"], -150)
        self.assertEqual(rows[2]["minutes_to_kickoff"], 240)


class LuongSonScanWindowTests(unittest.TestCase):
    def test_url_datetime_window(self):
        rows = [
            {"url": "https://hygenie.io/truc-tiep/a-vs-b-vao-luc-0730-20-07-2026", "raw_title": "A vs B"},
            {"url": "https://hygenie.io/truc-tiep/c-vs-d-vao-luc-0729-20-07-2026", "raw_title": "C vs D"},
            {"url": "https://hygenie.io/truc-tiep/e-vs-f-vao-luc-1400-20-07-2026", "raw_title": "E vs F"},
            {"url": "https://hygenie.io/truc-tiep/g-vs-h-vao-luc-1401-20-07-2026", "raw_title": "G vs H"},
            {"url": "https://hygenie.io/truc-tiep/i-vs-j", "raw_title": "I vs J", "card_text": "Đang đá"},
            {"url": "https://hygenie.io/truc-tiep/k-vs-l", "raw_title": "K vs L", "card_text": "Sắp diễn ra"},
        ]
        kept, stats = luongson.filter_links_by_scan_window(rows, NOW)
        self.assertEqual({row["url"] for row in kept}, {rows[0]["url"], rows[2]["url"], rows[4]["url"]})
        self.assertEqual(stats["window"], 2)
        self.assertEqual(stats["unknown_live"], 1)
        self.assertEqual(stats["past"], 1)
        self.assertEqual(stats["future"], 1)
        self.assertEqual(stats["unknown"], 1)
        self.assertEqual(rows[0]["minutes_to_kickoff"], -150)
        self.assertEqual(rows[2]["minutes_to_kickoff"], 240)


if __name__ == "__main__":
    unittest.main()
