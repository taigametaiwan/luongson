import unittest
from unittest.mock import AsyncMock, patch

import main
from sources import chuoichien, luongson


class DomainFailoverTests(unittest.TestCase):
    def test_new_domains_are_routed(self) -> None:
        routed = main.route_urls([
            "https://live04.chuoichientv.me/live/123/test-vs-test",
            "https://catbee.io/truc-tiep/a-vs-b",
        ])
        self.assertEqual(len(routed["chuoichien"]), 1)
        self.assertEqual(len(routed["luongson"]), 1)

    def test_default_domain_priority(self) -> None:
        self.assertEqual(chuoichien.HOME_URLS[0], "https://live04.chuoichientv.me/")
        self.assertIn("https://live03.chuoichientv.me/", chuoichien.HOME_URLS)
        self.assertEqual(luongson.HOME_URLS[0], "https://catbee.io/")
        self.assertIn("https://hygenie.io/", luongson.HOME_URLS)


class AdaptiveScanTests(unittest.TestCase):
    def test_chuoichien_wait_policy(self) -> None:
        self.assertEqual(
            chuoichien.effective_stream_wait_seconds({"minutes_to_kickoff": 100}),
            min(chuoichien.STREAM_WAIT_SECONDS, chuoichien.UPCOMING_FAR_WAIT_SECONDS),
        )
        self.assertEqual(
            chuoichien.effective_stream_wait_seconds({"minutes_to_kickoff": 20}),
            min(chuoichien.STREAM_WAIT_SECONDS, chuoichien.UPCOMING_NEAR_WAIT_SECONDS),
        )
        self.assertEqual(
            chuoichien.effective_stream_wait_seconds({"minutes_to_kickoff": -10}),
            chuoichien.STREAM_WAIT_SECONDS,
        )

    def test_luongson_quality_click_policy(self) -> None:
        self.assertFalse(
            luongson.should_probe_quality_buttons(
                {"minutes_to_kickoff": luongson.UPCOMING_FAR_THRESHOLD_MINUTES + 1},
                has_candidate=False,
            )
        )
        self.assertTrue(
            luongson.should_probe_quality_buttons(
                {"minutes_to_kickoff": luongson.UPCOMING_FAR_THRESHOLD_MINUTES + 1},
                has_candidate=True,
            )
        )
        self.assertTrue(
            luongson.should_probe_quality_buttons({"minutes_to_kickoff": 0}, False)
        )


class AsyncFailoverTests(unittest.IsolatedAsyncioTestCase):
    async def test_chuoichien_uses_second_domain_when_first_is_empty(self) -> None:
        with patch.object(
            chuoichien,
            "collect_home_links",
            new=AsyncMock(side_effect=[[], [{"url": "https://live03.chuoichientv.me/live/1/a-vs-b"}]]),
        ) as mocked:
            links = await chuoichien.collect_home_links_with_failover(object())
        self.assertEqual(len(links), 1)
        self.assertEqual(mocked.await_count, 2)

    async def test_luongson_uses_second_domain_when_first_is_not_football(self) -> None:
        with patch.object(
            luongson,
            "collect_home_links",
            new=AsyncMock(side_effect=[[], [{"url": "https://catbee.io/truc-tiep/a-vs-b"}]]),
        ) as mocked:
            links = await luongson.collect_home_links_with_failover(object())
        self.assertEqual(len(links), 1)
        self.assertEqual(mocked.await_count, 2)


if __name__ == "__main__":
    unittest.main()
