"""Live version choice in the server: probe only what can matter, never beside a running stream."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tvapp import livemeta, server
from tvapp.catalog import Catalogue
from tests.test_state_api import settings

FORMATS = {   # real formats of these provider entries (probed 11 Oct 2026)
    "l_uhd": {"v": "hevc", "bits": 10, "w": 3840, "h": 2160, "fps": 50.0, "hdr": True},
    "l_raw": {"v": "h264", "bits": 8, "w": 1920, "h": 1080, "fps": 50.0},
    "l_hd50": {"v": "h264", "bits": 8, "w": 1280, "h": 720, "fps": 50.0},
    "l_hd25": {"v": "h264", "bits": 8, "w": 1280, "h": 720, "fps": 25.0},
    "l_sd": {"v": "h264", "bits": 8, "w": 1024, "h": 576, "fps": 25.0},
    "l_hevchd": {"v": "hevc", "bits": 8, "w": 1280, "h": 720, "fps": 50.0},
}
NAMES = {"l_uhd": "4K: SKY SPORTS MAIN EVENTS ᵁᴴᴰ ³⁸⁴⁰ᴾ", "l_raw": "UK: SKY SPORT MAIN EVENT ᴿᴬᵂ",
         "l_hd50": "VIP: SKY SPORTS MAIN EVENT ᴴᴰ", "l_hd25": "UK: SKY SPORTS MAIN EVENTS ᴴᴰ ◉",
         "l_sd": "UK: SKY SPORTS MAIN EVENT SD", "l_hevchd": "UK: SKY SPORTS MAIN EVENTS HEVC HD"}
CATS = {"l_uhd": "4k"}


class FakeEngine:
    def __init__(self):
        self.jobs = {}
        self.probed = []

    async def probe_live(self, item):
        self.probed.append(item["id"])
        return dict(FORMATS[item["id"]])


class Choice(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        s = settings(d)
        server.S.settings = s
        server.S.cat = Catalogue(s, None)
        server.S.cat.data["live"] = [{"id": i, "name": n, "cat": CATS.get(i, "uk"), "url": "u"} for i, n in NAMES.items()]
        server.S.cat.data["cats"]["live"] = {"uk": "UK| SPORT", "4k": "4K| UHD"}
        server.S.cat._index()
        server.S.live_meta = livemeta.MetaCache(d / "live-meta.json")
        server.S.vindex = None
        server.S.engine = FakeEngine()

    def tearDown(self):
        self.tmp.cleanup()

    def choose(self, caps, iid="l_uhd", quality="auto"):
        item = server.S.cat.by_id[iid]
        d, cands, sport = asyncio.run(server.choose_live(item, livemeta.parse_caps(caps), quality))
        return d

    def test_capable_device_probes_only_the_chosen_channel(self):
        d = self.choose("avc.2160.60.s,hevc10.2160.60.s")
        self.assertEqual(d["play"], "l_uhd")
        self.assertEqual(server.S.engine.probed, ["l_uhd"])

    def test_fallback_probes_once_then_uses_the_cache(self):
        d = self.choose("avc.2160.60.s")
        self.assertEqual((d["play"], d["mode"]), ("l_raw", "copy"))
        first = list(server.S.engine.probed)
        self.assertNotIn("l_hevchd", first)                     # "HEVC" in the name: no use to this device
        self.assertNotIn("l_sd", first)                         # can't beat 1080p50 once that is known
        server.S.engine.probed.clear()
        d = self.choose("avc.2160.60.s")
        self.assertEqual(d["play"], "l_raw")
        self.assertEqual(server.S.engine.probed, [])            # second time: nothing to probe

    def test_no_probe_beside_a_running_stream(self):
        server.S.engine.jobs = {"x": SimpleNamespace()}         # another stream holds the one connection
        d = self.choose("avc.2160.60.s")
        self.assertEqual(server.S.engine.probed, [])
        self.assertEqual(d["mode"], "none")                     # format unknown: play_live copies it as before


if __name__ == "__main__":
    unittest.main()


class InterlacedTune(unittest.TestCase):
    """1.8.0: a channel cached before 1.8.0 as just "interlaced" turns out to be field-coded when its
    first segment is looked at; this device can't decode that, so the app chooses again before the
    player sees it."""

    def setUp(self):
        from tvapp.auth import Keys
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        s = settings(d)
        server.S.settings = s
        server.S.cat = Catalogue(s, None)
        names = {"l_i": "PT: RTP 1 ᴴᴰ", "l_p": "PT: RTP 1 SD"}
        server.S.cat.data["live"] = [{"id": i, "name": n, "cat": "pt", "url": "u"} for i, n in names.items()]
        server.S.cat.data["cats"]["live"] = {"pt": "PT| ENTERTAINMENT"}
        server.S.cat._index()
        server.S.live_meta = livemeta.MetaCache(d / "live-meta.json")
        server.S.live_meta.put("l_i", {"v": "h264", "w": 1920, "h": 1080, "fps": 25.0, "interlaced": True}, "job")
        server.S.live_meta.put("l_p", {"v": "h264", "w": 1280, "h": 720, "fps": 50.0, "field_order": "progressive"}, "probe")
        server.S.vindex = None
        server.S.keys = Keys(d)
        self.started, self.released = [], []
        tmp = d

        class Engine:
            jobs = {"busy": SimpleNamespace()}                       # the provider connection is in use: no probe

            async def probe_live(self, item):
                raise AssertionError("must not probe beside a running stream")

            async def start_live(eng, target, c, transcode, container, decision):
                job_dir = tmp / f"job{len(self.started)}"
                job_dir.mkdir()
                (job_dir / "seg_00001.ts").write_bytes(b"x")
                self.started.append(target["id"])
                return SimpleNamespace(id=f"{len(self.started):012x}", dir=job_dir, state="ready", transcode=transcode,
                                       error="")

            async def wait_ready(eng, job, n):
                return job

            async def analyze_file(eng, path):
                return {"v": "h264", "interlaced": True, "field_order": "tt", "fields": 50.0, "coding": "field"}

            async def release(eng, job_id, c):
                self.released.append(job_id)

        server.S.engine = Engine()
        self._gap = server.LIVE_RECONNECT_GAP
        server.LIVE_RECONNECT_GAP = 0
        self._saved_outcome = server.stream_outcome
        server.stream_outcome = lambda *a: None

    def tearDown(self):
        server.LIVE_RECONNECT_GAP = self._gap
        server.stream_outcome = self._saved_outcome
        self.tmp.cleanup()

    def test_field_coded_original_is_swapped_before_the_player_sees_it(self):
        req = SimpleNamespace(query_params={"caps": "avc.2160.60.s,hevc10.2160.60.s"}, headers={})
        resp = asyncio.run(server.play_live(req, server.S.cat.by_id["l_i"], "aabbccddeeff"))
        body = json.loads(resp.body)
        self.assertEqual(self.started, ["l_i", "l_p"])                  # the original, then the progressive version
        self.assertEqual(len(self.released), 1)
        self.assertEqual(body["variant"]["id"], "l_p")
        self.assertIn("can't decode the 1080i 25fps", body["notice"])
        self.assertEqual(server.S.live_meta.get("l_i")["coding"], "field")   # learned once, cached
