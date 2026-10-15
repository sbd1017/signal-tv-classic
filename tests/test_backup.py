"""Backup helpers that run without root: the owner-readable user-state copy and the rclone upload."""
import shutil
import tempfile
import unittest
from pathlib import Path

from tvapp import backup
from tvapp.userstate import StateStore, snapshot


class UserCopy(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.snaps = self.tmp / "snaps"
        self.snaps.mkdir()

    def snap(self, stamp):
        db, js = self.snaps / f"state-{stamp}.sqlite3", self.snaps / f"state-{stamp}.json"
        db.write_bytes(b"db" + stamp.encode())
        js.write_text('{"format": "signal-tv-classic-state"}')
        return db, js

    def test_copies_newest_pair_and_keeps_n(self):
        out = self.tmp / "out"
        out.mkdir()
        for i in range(4):
            backup._keep_user_copy(self.snap(f"2026010{i}T000000Z"), out, keep=2)
        names = sorted(p.name for p in (out / "user-state").iterdir())
        self.assertEqual(names, ["state-20260102T000000Z.json", "state-20260102T000000Z.sqlite3",
                                 "state-20260103T000000Z.json", "state-20260103T000000Z.sqlite3"])
        self.assertEqual(oct((out / "user-state" / names[0]).stat().st_mode & 0o777), "0o640")

    def test_real_snapshot_copy_carries_sports_state(self):
        """state.db -> nightly snapshot -> the user-state copy that goes to Google Drive: sports
        favourites, the alert setting and the sent-alert record are all in it."""
        store = StateStore(self.tmp / "state.db")
        store.apply([{"ns": "sfav", "key": "team:arsenal", "value": {"name": "Arsenal", "kind": "team"}, "updated": 1},
                     {"ns": "pref", "key": "sportsAlerts", "value": "30", "updated": 1}])
        store.record_alerts(["team:arsenal|team:chelsea@20261014"], "Arsenal vs Chelsea starts in 30 minutes")
        store.close()
        db, js = snapshot(self.tmp / "state.db", self.snaps, keep=3)
        out = self.tmp / "out"
        out.mkdir()
        backup._keep_user_copy((db, js), out, keep=5)
        copy = StateStore(out / "user-state" / db.name)
        self.assertEqual(list(copy.values("sfav")), ["team:arsenal"])
        self.assertEqual(copy.pref("sportsAlerts"), "30")
        self.assertEqual(copy.alerts_sent(["team:arsenal|team:chelsea@20261014"]), {"team:arsenal|team:chelsea@20261014"})
        copy.close()
        self.assertIn('"sports_favourites"', (out / "user-state" / js.name).read_text())

    @unittest.skipUnless(shutil.which("rclone"), "rclone not installed")
    def test_upload_to_an_rclone_remote(self):
        out = self.tmp / "out"
        out.mkdir()
        backup._keep_user_copy(self.snap("20260101T000000Z"), out, keep=5)
        (out / "user-state" / "notes.txt").write_text("not a backup")       # only state-* is uploaded
        remote = self.tmp / "remote"                                         # a local path works as an rclone remote
        backup.upload(str(remote), out)
        self.assertEqual(sorted(p.name for p in (remote / "user-state").iterdir()),
                         ["state-20260101T000000Z.json", "state-20260101T000000Z.sqlite3"])
        backup.upload(str(remote), out)                                      # again: nothing changes

    @unittest.skipUnless(shutil.which("rclone"), "rclone not installed")
    def test_upload_failure_is_reported(self):
        out = self.tmp / "out"
        out.mkdir()
        backup._keep_user_copy(self.snap("20260101T000000Z"), out, keep=5)
        with self.assertRaises(SystemExit) as cm:
            backup.upload("no-such-remote-xyz:", out)
        self.assertIn("upload to no-such-remote-xyz: failed", str(cm.exception))
        self.assertNotIn("sign-in has expired", str(cm.exception))      # only said when it is the cause

    def test_nothing_to_upload(self):
        with self.assertRaises(SystemExit):
            backup.upload(str(self.tmp / "remote"), self.tmp)


if __name__ == "__main__":
    unittest.main()
