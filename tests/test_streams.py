"""Playback job reaper: idle jobs are stopped, and one job failing to stop never ends the reaper."""
import asyncio
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from tvapp.streams import LIVE_IDLE, Engine, Job


def job(jid, idle_for):
    t = time.time() - idle_for
    j = Job(id=jid, key=f"live:{jid}", kind="live", item_id="l_x", dir=Path("/nonexistent"), url="", ua="", ref="")
    j.last_media = j.last_beat = t
    return j


class Reaper(unittest.TestCase):
    def test_one_failing_stop_does_not_stop_the_others(self):
        eng = Engine(SimpleNamespace())
        eng.jobs = {"a": job("a", LIVE_IDLE + 10), "b": job("b", LIVE_IDLE + 10), "c": job("c", 1)}
        stopped = []

        async def stop(jid, why=""):
            if jid == "a":
                raise OSError("kill failed")
            stopped.append(jid)
            eng.jobs.pop(jid)
        eng.stop = stop
        with self.assertLogs("tv", "WARNING") as logs:
            asyncio.run(eng._reap_once(time.time()))
        self.assertEqual(stopped, ["b"])                       # idle b stopped, busy c kept
        self.assertIn("a", eng.jobs)
        self.assertIn("could not be stopped", logs.output[0])


if __name__ == "__main__":
    unittest.main()
