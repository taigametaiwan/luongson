import argparse
import os
import threading
import unittest
from unittest.mock import patch

import main as orchestrator


class ParallelOrchestrationTests(unittest.TestCase):
    def test_two_sources_start_in_parallel(self):
        started = {key: threading.Event() for key in ("chuoichien", "luongson")}
        release = threading.Event()
        result = {}

        def fake_run(config, _urls):
            started[config.key].set()
            release.wait(timeout=2)
            return (0, True, 0.1)

        args = argparse.Namespace(urls=[], source="all", merge_only=False)
        env = {**os.environ, "MULTI_RUN_SOURCES_PARALLEL": "1"}

        def target():
            with patch.dict(os.environ, env, clear=True), \
                 patch.object(orchestrator, "parse_args", return_value=args), \
                 patch.object(orchestrator, "run_source", side_effect=fake_run), \
                 patch.object(orchestrator, "debug_row_count", return_value=1), \
                 patch.object(orchestrator, "merge_sources", return_value={"selected_count": 1, "input_candidates": 2, "dropped_count": 1}), \
                 patch.object(orchestrator, "cleanup_intermediate_playlists", return_value=[]):
                result["code"] = orchestrator.main()

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        self.assertTrue(started["chuoichien"].wait(timeout=1), "Chuối Chiên chưa khởi động")
        self.assertTrue(started["luongson"].wait(timeout=1), "Lương Sơn chưa khởi động song song")
        release.set()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result.get("code"), 0)


if __name__ == "__main__":
    unittest.main()
