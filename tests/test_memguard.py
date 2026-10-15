"""TV-only memory guard: when it restarts, when it must not, and that it cannot loop."""
import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tvapp import memguard, server
from tvapp.memguard import HARD_FACTOR, MAX_RESTARTS, SUSTAIN_SECONDS, WINDOW_SECONDS, MemoryGuard


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


class Guard(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.clock = Clock()
        self.g = MemoryGuard(2048, self.dir / "memory-guard.json", clock=self.clock)

    def run_for(self, seconds, mb, step=10):
        """Feed one reading every `step` seconds; returns the actions seen."""
        actions = []
        for _ in range(int(seconds // step)):
            self.clock.t += step
            actions.append(self.g.observe(mb)[0])
        return actions

    def test_normal_use_and_refresh_peaks_never_trigger(self):
        self.assertEqual(set(self.run_for(3600, 700)), {"ok"})
        self.assertEqual(set(self.run_for(30, 1200)), {"ok"})        # a refresh peak

    def test_brief_spike_over_the_limit_is_tolerated(self):
        self.assertEqual(self.run_for(50, 2500), ["over"] * 5)      # 50 s over: still waiting
        self.assertEqual(self.run_for(60, 900), ["ok"] * 6)         # came back down: counter resets
        self.assertEqual(self.run_for(50, 2500), ["over"] * 5)

    def test_sustained_excess_restarts(self):
        actions = self.run_for(SUSTAIN_SECONDS + 10, 2100)
        self.assertEqual(actions[-1], "restart")
        self.assertNotIn("restart", actions[:-1])

    def test_runaway_restarts_immediately(self):
        self.clock.t += 10
        action, reason = self.g.observe(2048 * HARD_FACTOR + 1)
        self.assertEqual(action, "restart")
        self.assertIn("1.5x", reason)

    def test_budget_prevents_a_restart_loop_and_survives_restarts(self):
        for _ in range(MAX_RESTARTS):
            self.g.record("restart", "test", {"total_mb": 4000})
            self.g = MemoryGuard(2048, self.dir / "memory-guard.json", clock=self.clock)   # the process restarted
            self.clock.t += 600
        self.clock.t += 10
        self.assertEqual(self.g.observe(5000)[0], "suppressed")       # logged, not restarted
        self.clock.t += 10
        self.assertEqual(self.g.observe(5000)[0], "over")             # and not nagging every check
        self.clock.t += WINDOW_SECONDS                                 # budget window passed
        self.assertEqual(self.g.observe(5000)[0], "restart")

    def test_summary_and_corrupt_file(self):
        self.assertEqual(self.g.summary(), {"limit_mb": 2048, "restarts_last_6h": 0, "last_event": None})
        self.g.record("restart", "why", {"total_mb": 3000})
        self.assertEqual(self.g.summary()["last_event"]["reason"], "why")
        (self.dir / "memory-guard.json").write_text("{not json")
        self.assertEqual(MemoryGuard(2048, self.dir / "memory-guard.json").events, [])

    def test_rss_of_this_process(self):
        mb = memguard.rss_mb()
        if Path("/proc/self/status").exists():
            self.assertGreater(mb, 5)
        self.assertEqual(memguard.rss_mb(999_999_999), 0.0)


class ServerHook(unittest.TestCase):
    """memory_watch() logs a full breakdown, records the event and calls the restart path."""

    def test_trigger_logs_records_and_restarts(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d)
        server.S.guard = MemoryGuard(100, d / "memory-guard.json")
        server.S.engine = SimpleNamespace(jobs={}, probes={})
        server.S.cat = SimpleNamespace(refreshing=False, guide_refreshing=False, by_id={}, images={},
                                       details={"movie": {}, "series": {}}, guide=SimpleNamespace(data={}),
                                       data={"live": [], "movie": [], "series": [], "episode": []})
        server.S.lists = SimpleNamespace(sizes=lambda: {})
        server.S.img_fail, server.S.range_ok, server.S.tasks, server.S.started = {}, {}, set(), 0.0

        async def fake_restart():
            raise SystemExit(memguard.EXIT_CODE)
        with mock.patch.object(memguard, "CHECK_SECONDS", 0), \
                mock.patch.object(server, "memory_usage", lambda: {"app_mb": 900, "ffmpeg_mb": 0, "total_mb": 900, "jobs": {}}), \
                mock.patch.object(server, "restart_for_memory", fake_restart), \
                self.assertLogs("tv", "ERROR") as logs, self.assertRaises(SystemExit):
            asyncio.run(server.memory_watch())                     # 900 MB > 1.5 x 100 MB: immediate
        self.assertIn("MEMORY GUARD", logs.output[0])
        event = json.loads((d / "memory-guard.json").read_text())[-1]
        self.assertEqual((event["action"], event["app_mb"], event["catalogue"]["movie"]), ("restart", 900, 0))


if __name__ == "__main__":
    unittest.main()
