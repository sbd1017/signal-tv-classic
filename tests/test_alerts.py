"""Telegram alerts: dedup/backoff, resolved notes, hourly cap, redaction; provider health rule."""
import tempfile
import unittest
from pathlib import Path

from tvapp import alerts
from tvapp.health import ProviderHealth
from tvapp.util import redact


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class Alerts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        (d / "telegram.env").write_text("TELEGRAM_BOT_TOKEN=123456789:" + "A" * 35 + "\nTELEGRAM_CHAT_ID=42\n")
        self.sent = []
        self.clock = Clock()
        self.a = alerts.Alerts(d / "alerts.json", d / "telegram.env", clock=self.clock,
                               sender=lambda cfg, text: self.sent.append(text))

    def tearDown(self):
        self.tmp.cleanup()

    def test_dedup_and_backoff(self):
        self.assertTrue(self.a.raise_("refresh-rejected", "Refresh rejected", "movies: came back empty"))
        for _ in range(5):                                  # keeps happening within the hour: silent
            self.clock.t += 600
            self.assertFalse(self.a.raise_("refresh-rejected", "Refresh rejected", "again"))
        self.clock.t += 600                                 # 1 h after the first: one reminder with a count
        self.assertTrue(self.a.raise_("refresh-rejected", "Refresh rejected", "again"))
        self.assertIn("happened 7 times", self.sent[-1])
        self.clock.t += 3600                                # next one waits 4 h
        self.assertFalse(self.a.raise_("refresh-rejected", "Refresh rejected", "again"))
        self.clock.t += 3 * 3600
        self.assertTrue(self.a.raise_("refresh-rejected", "Refresh rejected", "again"))
        self.assertEqual(len(self.sent), 3)

    def test_resolved_only_after_an_alert_went_out(self):
        self.assertFalse(self.a.resolve("refresh-failing"))           # never raised: nothing to say
        self.a.raise_("refresh-failing", "Refresh failed 3 times", "HTTP 502")
        self.assertTrue(self.a.resolve("refresh-failing", "Refresh OK: +12/-3 movies"))
        self.assertIn("resolved", self.sent[-1])
        self.assertFalse(self.a.resolve("refresh-failing"))           # once
        self.assertEqual(self.a.open_problems(), [])
        self.clock.t += 60                                             # a new episode alerts again at once
        self.assertTrue(self.a.raise_("refresh-failing", "Refresh failed 3 times", "HTTP 502"))

    def test_hourly_cap(self):
        for i in range(10):
            self.a.raise_(f"k{i}", f"problem {i}")
        self.assertEqual(len(self.sent), alerts.MAX_PER_HOUR)

    def test_secrets_never_leave(self):
        redact.set_secrets(["s3cretpass", "myuser"])
        try:
            self.a.raise_("provider-streams", "Provider refusing streams",
                          "ffmpeg: http://prov.example:8080/live/myuser/s3cretpass/123.ts 403 Forbidden; "
                          "bot 123456789:" + "B" * 35 + " /movie/u/p/9.mkv")
        finally:
            redact.set_secrets([])
        text = self.sent[-1]
        for leak in ("s3cretpass", "myuser", "prov.example", "B" * 35, "/movie/u/p/"):
            self.assertNotIn(leak, text)
        self.assertIn("403 Forbidden", text)

    def test_not_configured_means_logged_only(self):
        a = alerts.Alerts(Path(self.tmp.name) / "x.json", Path(self.tmp.name) / "missing.env",
                          sender=lambda *a: self.fail("must not send"))
        with self.assertLogs("tv", "INFO"):
            self.assertFalse(a.raise_("memory-guard", "Memory guard restarted the service"))
        self.assertFalse(a.configured)
        self.assertEqual(len(a.open_problems()), 1)                    # still shown in iptv status

    def test_send_failure_does_not_count_as_sent(self):
        def boom(cfg, text):
            raise RuntimeError("Telegram send failed (URLError)")
        a = alerts.Alerts(Path(self.tmp.name) / "y.json", Path(self.tmp.name) / "telegram.env", clock=self.clock, sender=boom)
        with self.assertLogs("tv", "WARNING"):
            self.assertFalse(a.raise_("unit:signal-tv-classic-backup.service", "Nightly backup failed"))
        self.clock.t += 60
        a.sender = lambda cfg, text: self.sent.append(text)
        self.assertTrue(a.raise_("unit:signal-tv-classic-backup.service", "Nightly backup failed"))   # retried, not backed off

    def test_unit_hook(self):
        orig_state, orig_cfg, orig_post = alerts.STATE, alerts.CONFIG, alerts.post
        alerts.STATE, alerts.CONFIG = Path(self.tmp.name) / "unit.json", Path(self.tmp.name) / "telegram.env"
        alerts.post = lambda cfg, text: self.sent.append(text)
        try:
            self.assertEqual(alerts.main(["failed:signal-tv-classic-backup-upload.service"]), 0)
            self.assertIn("Google Drive backup upload failed", self.sent[-1])
            self.assertIn("invalid_grant", self.sent[-1])
            self.assertEqual(alerts.main(["ok:signal-tv-classic-backup-upload.service"]), 0)
            self.assertIn("resolved", self.sent[-1])
        finally:
            alerts.STATE, alerts.CONFIG, alerts.post = orig_state, orig_cfg, orig_post


class Health(unittest.TestCase):
    def test_one_dead_channel_is_not_an_outage(self):
        clock = Clock()
        h = ProviderHealth(clock)
        for _ in range(8):
            clock.t += 120
            self.assertIsNone(h.record(False, "l_dead", "HTTP 404"))

    def test_persistent_failure_and_recovery(self):
        clock = Clock()
        h = ProviderHealth(clock)
        results = []
        for i in range(6):
            clock.t += 130
            results.append(h.record(False, f"l_{i % 3}", "The provider's connection limit was reached."))
        self.assertEqual(results.count("down"), 1)
        self.assertTrue(h.down)
        self.assertEqual(h.record(True, "l_9"), "up")
        self.assertFalse(h.down)

    def test_a_success_in_the_window_prevents_it(self):
        clock = Clock()
        h = ProviderHealth(clock)
        h.record(True, "m_ok")
        for i in range(6):
            clock.t += 120
            self.assertIsNone(h.record(False, f"l_{i}", "x"))


if __name__ == "__main__":
    unittest.main()
