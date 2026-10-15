"""Diagnostics report and health status over HTTP: useful content, and never a secret."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from tvapp import alerts, memguard, server
from tvapp.catalog import Catalogue
from tvapp.health import ProviderHealth
from tvapp.streams import Engine, Job
from tvapp.userstate import StateStore
from tests.test_state_api import SECRETS, settings


class Diag(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        s = settings(d)
        server.S.settings = s
        server.S.state = StateStore(d / "state.db")
        server.S.cat = Catalogue(s, None)
        server.S.cat.configure(s)                           # registers the secrets with the redactor
        url = f"{SECRETS[0]}/movie/{SECRETS[1]}/{SECRETS[2]}/77.mkv"
        movie = {"id": "m_0123456789abcdef", "kind": "movie", "name": "Test Film (2020)", "cat": "1", "ext": "mkv", "url": url}
        server.S.cat.data["movie"] = [movie]
        server.S.cat._index()
        server.S.engine = Engine(s)
        server.S.engine.probes[movie["id"]] = {
            "t": 0, "ok": True, "fmt": "matroska,webm", "dur": 5400, "v": "h264", "a": "eac3", "h": 1080, "w": 1920,
            "interlaced": False,
            "audio": [{"i": 0, "label": "English · 5.1", "codec": "eac3"}, {"i": 1, "label": "French", "codec": "aac"}],
            "subs": [{"i": 0, "label": "English (SDH)", "codec": "subrip", "ok": True},
                     {"i": 1, "label": "German", "codec": "hdmv_pgs_subtitle", "ok": False}]}
        job = Job(id="abcdef012345", key="k", kind="vod", item_id=movie["id"], dir=d, url=url, ua="", ref="",
                  offset=600, vcodec="h264", audio=1, sub=0, origin=597.9)
        job.state = "ready"
        job.tail = [f"[http @ 0x1] HTTP error 403 Forbidden for {url}", "Error opening input: Server returned 403"]
        server.S.engine.jobs[job.id] = job
        server.S.alerts = alerts.Alerts(d / "alerts.json", d / "none.env")
        server.S.health = ProviderHealth()
        server.S.health.record(False, "l_x", f"403 from {url}")
        server.S.guard = memguard.MemoryGuard(2048, d / "mg.json")
        server.S.lists = SimpleNamespace(sizes=lambda: {})
        self.c = TestClient(server.app)

    def tearDown(self):
        server.S.state.close()
        self.tmp.cleanup()

    def assert_clean(self, text):
        for secret in SECRETS[:3] + ("provider.example",):
            self.assertNotIn(secret, text)

    def test_report_content_and_redaction(self):
        r = self.c.get("/tv/api/diag/m_0123456789abcdef?job=abcdef012345&mode=hls")
        self.assertEqual(r.status_code, 200)
        text = r.json()["text"]
        self.assert_clean(r.text)
        for want in ("Test Film (2020) [m_0123456789abcdef]", "container: mkv / matroska,webm", "h264 1920x1080",
                     "#1 French (aac)", "#1 German (hdmv_pgs_subtitle, picture)", "remux to HLS, video copied",
                     "from 600.0 s (shown from 597.9 s); audio #1; subtitle #0", "HTTP error 403 Forbidden",
                     "POSSIBLE connection-slot exhaustion", server.__version__):
            self.assertIn(want, text)

    def test_unknown_item(self):
        self.assertEqual(self.c.get("/tv/api/diag/m_ffffffffffffffff").status_code, 404)
        self.assertEqual(self.c.get("/tv/api/diag/..%2Fetc").status_code, 404)

    def test_status_health(self):
        server.S.cat.history.record("rejected", "scheduled", {"anomalies": ["movies: came back empty (had 181010)"]})
        r = self.c.get("/tv/api/status")
        self.assertEqual(r.status_code, 200)
        self.assert_clean(r.text)
        h = r.json()["health"]
        self.assertFalse(h["ok"])
        self.assertIn("catalogue refresh rejected: movies: came back empty (had 181010)", h["problems"])
        self.assertFalse(h["alerts"]["configured"])
        self.assertEqual(r.json()["refresh"]["last"]["status"], "rejected")


class RefreshAccept(unittest.TestCase):
    """accept=1 (override the refresh guard) only from `iptv` on the Pi, never through nginx."""
    setUp, tearDown = Diag.setUp, Diag.tearDown

    def ask(self, client, headers=None):
        import time
        server.S.cat.accept_next = False
        server.S.last_manual = time.time()                  # within the minute: no refresh actually starts
        c = TestClient(server.app, client=client)
        self.assertEqual(c.get("/tv/api/refresh?accept=1", headers=headers or {}).status_code, 200)
        return server.S.cat.accept_next

    def test_only_a_direct_local_request_can_accept(self):
        self.assertTrue(self.ask(("127.0.0.1", 50000)))
        self.assertFalse(self.ask(("127.0.0.1", 50000), {"X-Forwarded-For": "203.0.113.9"}), "through nginx")
        self.assertFalse(self.ask(("203.0.113.9", 50000)))


if __name__ == "__main__":
    unittest.main()
