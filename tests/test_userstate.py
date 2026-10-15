"""Merge rules, validation, export/import and snapshots of the synced state store.

Run: /opt/signal-tv-classic/venv/bin/python -m unittest discover -s tests -v   (from the repo root)
"""
import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from tvapp import userstate
from tvapp.userstate import StateError, StateStore, now_ms, snapshot

T0 = 1_700_000_000_000


def fav(key, at, name="Ch", deleted=False):
    return {"ns": "fav", "key": key, "value": None if deleted else {"name": name, "kind": "live"},
            "updated": at, "deleted": deleted}


def prog(key, at, t, d=3600):
    return {"ns": "progress", "key": key, "value": {"t": t, "d": d, "done": False, "kind": "movie", "name": "M"},
            "updated": at}


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.db"
        self.s = StateStore(self.path)

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def rows(self, since=0):
        return {(c["ns"], c["key"]): c for c in self.s.changes_since(since)["changes"]}


class MergeRules(StoreTest):
    def test_newer_write_wins_older_is_ignored(self):
        self.assertEqual(self.s.apply([prog("m_1", T0 + 2000, 1200)])["applied"], 1)
        r = self.s.apply([prog("m_1", T0 + 1000, 300)])          # stale device with older progress
        self.assertEqual(r, {"applied": 0, "rejected": 0})
        self.assertEqual(self.rows()[("progress", "m_1")]["value"]["t"], 1200)
        self.s.apply([prog("m_1", T0 + 3000, 1500)])
        self.assertEqual(self.rows()[("progress", "m_1")]["value"]["t"], 1500)

    def test_exact_ties_do_not_depend_on_arrival_order(self):
        a, b = prog("m_1", T0, 100), prog("m_1", T0, 999)
        gone = fav("l_x", T0, deleted=True)
        other = StateStore(Path(self.tmp.name) / "other.db")
        self.s.apply([a]); self.s.apply([b]); self.s.apply([fav("l_x", T0)]); self.s.apply([gone])
        other.apply([b]); other.apply([a]); other.apply([gone]); other.apply([fav("l_x", T0)])
        mine = {k: (c["value"], c["deleted"]) for k, c in self.rows().items()}
        theirs = {(c["ns"], c["key"]): (c["value"], c["deleted"]) for c in other.changes_since(0)["changes"]}
        other.close()
        self.assertEqual(mine, theirs)
        self.assertFalse(mine[("fav", "l_x")][1])                 # on a tie a value beats a deletion
        self.s.apply([prog("m_1", T0, 5)])                         # same time again: still no flip-flop
        self.assertEqual(self.rows()[("progress", "m_1")]["value"], mine[("progress", "m_1")][0])

    def test_deletion_beats_older_add_and_loses_to_newer_add(self):
        self.s.apply([fav("l_a", T0)])
        self.s.apply([fav("l_a", T0 + 10, deleted=True)])
        self.s.apply([fav("l_a", T0 + 5)])                       # an old copy from another device
        row = self.rows()[("fav", "l_a")]
        self.assertTrue(row["deleted"])
        self.assertIsNone(row["value"])
        self.s.apply([fav("l_a", T0 + 20)])                      # favourited again later
        self.assertFalse(self.rows()[("fav", "l_a")]["deleted"])

    def test_deleting_unknown_row_is_stored_as_tombstone(self):
        self.s.apply([fav("l_x", T0, deleted=True)])
        self.assertTrue(self.rows()[("fav", "l_x")]["deleted"])

    def test_future_timestamps_are_clamped_to_server_time(self):
        before = now_ms()
        self.s.apply([prog("m_1", before + 3_600_000, 50)])      # device clock an hour fast
        stored = self.rows()[("progress", "m_1")]["updated"]
        self.assertLessEqual(stored, now_ms())
        self.assertGreaterEqual(stored, before)
        time.sleep(0.005)
        self.s.apply([prog("m_1", now_ms(), 60)])                # a correct device a moment later still wins
        self.assertEqual(self.rows()[("progress", "m_1")]["value"]["t"], 60)


class Revisions(StoreTest):
    def test_since_returns_only_newer_changes(self):
        self.s.apply([fav("l_a", T0), fav("l_b", T0)])
        rev = self.s.changes_since(0)["rev"]
        self.s.apply([fav("l_c", T0)])
        out = self.s.changes_since(rev)
        self.assertEqual([c["key"] for c in out["changes"]], ["l_c"])
        self.assertEqual(out["rev"], rev + 1)
        self.assertEqual(self.s.changes_since(out["rev"])["changes"], [])

    def test_unknown_future_revision_returns_everything(self):
        self.s.apply([fav("l_a", T0)])
        self.assertEqual(len(self.s.changes_since(10_000)["changes"]), 1)

    def test_revision_never_goes_backwards_after_purge(self):
        self.s.apply([fav("l_a", 1, deleted=True)])              # ancient tombstone, highest rev
        rev = self.s.changes_since(0)["rev"]
        self.assertEqual(self.s.maintain(), 1)
        self.s.apply([fav("l_b", T0)])
        self.assertEqual(self.s.changes_since(0)["rev"], rev + 1)

    def test_db_id_is_stable_across_reopen(self):
        db_id = self.s.db_id
        self.s.close()
        self.s = StateStore(self.path)
        self.assertEqual(self.s.db_id, db_id)


class Validation(StoreTest):
    def test_bad_rows_are_skipped_not_fatal(self):
        r = self.s.apply([
            {"ns": "nope", "key": "a", "value": {}, "updated": T0},
            {"ns": "fav", "key": "bad key!", "value": {}, "updated": T0},
            {"ns": "fav", "key": "l_a", "value": "not an object", "updated": T0},
            {"ns": "pref", "key": "x", "value": {"nested": 1}, "updated": T0},
            {"ns": "fav", "key": "l_a", "value": {}, "updated": "yesterday"},
            {"ns": "fav", "key": "l_a", "value": {}, "updated": True},
            {"ns": "fav", "key": "l_a", "value": {"name": "x" * 3000}, "updated": T0},
            "garbage",
            fav("l_ok", T0),
        ])
        self.assertEqual(r, {"applied": 1, "rejected": 8})

    def test_batch_limit(self):
        with self.assertRaises(StateError):
            self.s.apply([fav(f"l_{i}", T0) for i in range(userstate.MAX_BATCH + 1)])
        self.assertEqual(self.s.changes_since(0)["rev"], 0)

    def test_prefs_accept_plain_values(self):
        self.s.apply([{"ns": "pref", "key": "movies.sort", "value": "rating", "updated": T0},
                      {"ns": "pref", "key": "liveCat", "value": None, "updated": T0}])
        self.assertEqual(self.rows()[("pref", "movies.sort")]["value"], "rating")

    def test_caps_drop_oldest(self):
        with mock.patch.dict(userstate.CAPS, {"recent": 3}):
            self.s.apply([{"ns": "recent", "key": f"l_{i}", "value": {"name": str(i)}, "updated": T0 + i}
                          for i in range(5)])
        keys = sorted(k for (ns, k), c in self.rows().items() if ns == "recent" and not c["deleted"])
        self.assertEqual(keys, ["l_2", "l_3", "l_4"])


class ExportImport(StoreTest):
    def seed(self):
        self.s.apply([fav("l_a", T0, "BBC One"), prog("m_1", T0 + 5, 1234.5),
                      {"ns": "last", "key": "channel", "value": {"id": "l_a", "name": "BBC One"}, "updated": T0},
                      {"ns": "pref", "key": "liveMode", "value": "fav", "updated": T0},
                      {"ns": "hide", "key": "c:live:12", "value": {"kind": "live", "cat": "12", "name": "Arabic"}, "updated": T0},
                      fav("l_gone", T0 + 9, deleted=True)])

    def test_export_is_readable_and_has_no_tombstones(self):
        self.seed()
        doc = self.s.export(app_version="x")
        self.assertEqual(doc["format"], "signal-tv-classic-state")
        self.assertEqual([e["key"] for e in doc["favourites"]], ["l_a"])
        p = doc["progress"][0]
        self.assertEqual((p["position_s"], p["duration_s"]), (1234.5, 3600))
        self.assertNotIn("t", p)
        self.assertEqual(doc["preferences"], [{"key": "liveMode", "updated": userstate.iso(T0), "value": "fav"}])
        self.assertEqual(doc["channels"][0]["id"], "l_a")
        self.assertEqual(doc["hidden"], [{"key": "c:live:12", "updated": userstate.iso(T0), "kind": "live", "cat": "12", "name": "Arabic"}])
        json.dumps(doc)

    def test_round_trip_into_empty_store(self):
        self.seed()
        doc = json.loads(json.dumps(self.s.export()))
        other = StateStore(Path(self.tmp.name) / "other.db")
        self.assertEqual(other.import_doc(doc)["applied"], 5)
        a = {k: (c["value"], c["updated"]) for k, c in self.rows().items() if not c["deleted"]}
        b = {(c["ns"], c["key"]): (c["value"], c["updated"]) for c in other.changes_since(0)["changes"]}
        self.assertEqual(a, b)
        self.assertEqual(other.import_doc(doc)["applied"], 0)      # importing again changes nothing
        other.close()

    def test_merge_import_does_not_overwrite_newer(self):
        self.seed()
        doc = self.s.export()
        self.s.apply([prog("m_1", T0 + 100, 2000)])
        self.s.import_doc(doc, "merge")
        self.assertEqual(self.rows()[("progress", "m_1")]["value"]["t"], 2000)

    def test_replace_import_makes_file_the_whole_state(self):
        self.seed()
        doc = self.s.export()
        doc["favourites"] = []
        self.s.apply([fav("l_new", now_ms())])
        r = self.s.import_doc(doc, "replace")
        self.assertEqual(r["removed"], 2)                           # l_a and l_new
        live = {k for k, c in self.rows().items() if not c["deleted"]}
        self.assertEqual(live, {("progress", "m_1"), ("last", "channel"), ("pref", "liveMode"), ("hide", "c:live:12")})

    def test_older_exports_without_hidden_still_import(self):
        self.seed()
        doc = self.s.export()
        del doc["hidden"]
        other = StateStore(Path(self.tmp.name) / "old.db")
        self.assertEqual(other.import_doc(doc)["applied"], 4)
        other.close()

    def test_bad_files_are_refused_without_changes(self):
        self.seed()
        before = self.s.changes_since(0)
        for bad in ({}, {"format": "other", "version": 1}, {"format": "signal-tv-classic-state", "version": 2},
                    {"format": "signal-tv-classic-state", "version": 1, "favourites": "x"}):
            with self.assertRaises(StateError):
                self.s.import_doc(bad, "replace")
        broken = self.s.export()
        broken["favourites"].append({"key": "l_b", "updated": "not a time"})
        with self.assertRaises(StateError):
            self.s.import_doc(broken, "replace")
        self.assertEqual(self.s.changes_since(0)["rev"], before["rev"])


class BackupCoverage(StoreTest):
    """Every kind of synced data is in every backup form: snapshot, its JSON, export, restore."""

    ONE_OF_EACH = [
        {"ns": "fav", "key": "l_a", "value": {"name": "BBC One", "kind": "live"}, "updated": T0},
        {"ns": "progress", "key": "m_1", "value": {"t": 61.5, "d": 5400, "done": False, "kind": "movie", "name": "F"}, "updated": T0},
        {"ns": "recent", "key": "e_1", "value": {"name": "Ep", "kind": "episode", "series": "s_1"}, "updated": T0},
        {"ns": "last", "key": "channel", "value": {"id": "l_a", "name": "BBC One", "kind": "live"}, "updated": T0},
        {"ns": "pref", "key": "movies.sort", "value": "rating", "updated": T0},
        {"ns": "hide", "key": "c:live:12", "value": {"kind": "live", "cat": "12", "name": "Arabic"}, "updated": T0},
        {"ns": "sfav", "key": "team:arsenal", "value": {"name": "Arsenal", "kind": "team", "sport": "football"}, "updated": T0},
    ]

    def test_every_namespace_is_covered(self):
        self.assertEqual({c["ns"] for c in self.ONE_OF_EACH}, set(userstate.NAMESPACES))   # list is complete
        self.s.apply(self.ONE_OF_EACH)
        want = {(c["ns"], c["key"]): c["value"] for c in self.ONE_OF_EACH}
        db, js = snapshot(self.path, Path(self.tmp.name) / "snaps", keep=3)
        restored = StateStore(db)                                   # the snapshot opens as a working store
        got = {(c["ns"], c["key"]): c["value"] for c in restored.changes_since(0)["changes"]}
        self.assertEqual(got, want)
        self.assertEqual(restored.db_id, self.s.db_id)
        doc = json.loads(js.read_text())                            # its readable JSON = the export format
        self.assertEqual({k: len(doc[k]) for k in userstate.SECTIONS.values()}, {k: 1 for k in userstate.SECTIONS.values()})
        fresh = StateStore(Path(self.tmp.name) / "fresh.db")        # restore the JSON into an empty Pi
        self.assertEqual(fresh.import_doc(doc, "replace")["applied"], len(self.ONE_OF_EACH))
        self.assertEqual({(c["ns"], c["key"]): c["value"] for c in fresh.changes_since(0)["changes"]}, want)
        restored.close()
        fresh.close()


class SportsState(StoreTest):
    def test_sports_favourites_and_alert_setting_in_every_backup_form(self):
        self.s.apply([{"ns": "sfav", "key": "comp:uefa-champions-league", "value": {"name": "UEFA Champions League",
                       "kind": "comp", "sport": "football"}, "updated": T0},
                      {"ns": "pref", "key": "sportsAlerts", "value": "15", "updated": T0}])
        self.s.record_alerts(["team:arsenal|team:chelsea@20261014"], "Arsenal vs Chelsea starts in 15 minutes")
        self.assertEqual(self.s.values("sfav"), {"comp:uefa-champions-league": {"name": "UEFA Champions League",
                                                                               "kind": "comp", "sport": "football"}})
        self.assertEqual(self.s.pref("sportsAlerts"), "15")
        db, js = snapshot(self.path, Path(self.tmp.name) / "snaps", keep=3)
        restored = StateStore(db)                                  # the nightly snapshot (local + Google Drive copy)
        self.assertEqual(restored.pref("sportsAlerts"), "15")
        self.assertIn("comp:uefa-champions-league", restored.values("sfav"))
        self.assertEqual(restored.alerts_sent(["team:arsenal|team:chelsea@20261014", "x"]),
                         {"team:arsenal|team:chelsea@20261014"})     # sent alerts survive a restore: no repeat
        doc = json.loads(js.read_text())
        self.assertEqual([e["key"] for e in doc["sports_favourites"]], ["comp:uefa-champions-league"])
        self.assertIn({"key": "sportsAlerts", "value": "15"}, [{k: e[k] for k in ("key", "value")} for e in doc["preferences"]])
        self.assertNotIn("sports_alerts", doc)                     # the Pi's own record, not user data
        restored.close()

    def test_old_database_gains_the_alerts_table(self):
        self.s.apply([fav("l_a", T0)])
        self.s.close()
        with closing(sqlite3.connect(self.path)) as con:          # a 1.6.0 database
            con.execute("DROP TABLE sports_alerts")
            con.execute("PRAGMA user_version = 1")
            con.commit()
        self.s = StateStore(self.path)
        self.assertEqual([c["key"] for c in self.s.changes_since(0)["changes"]], ["l_a"])
        self.s.record_alerts(["a@1"], "x")
        self.assertEqual(self.s.alerts_sent(["a@1"]), {"a@1"})

    def test_sent_alerts_are_forgotten_after_a_week(self):
        self.s.record_alerts(["old@1"], "x")
        self.s.db.execute("UPDATE sports_alerts SET sent = 0")
        self.s.record_alerts(["new@1"], "y")
        self.assertEqual(self.s.alerts_sent(["old@1", "new@1"]), {"new@1"})


class Damage(StoreTest):
    def test_damaged_database_is_set_aside_and_replaced(self):
        old_id = self.s.db_id
        self.s.close()
        self.path.write_bytes(b"this is not a database" * 200)
        with self.assertLogs("tv", "ERROR") as logs:
            self.s = StateStore(self.path)
        self.assertNotEqual(self.s.db_id, old_id)                 # devices will notice and resend
        self.assertEqual(self.s.changes_since(0)["changes"], [])
        kept = list(self.path.parent.glob("state.db.damaged-*"))
        self.assertEqual(len(kept), 1)
        self.assertIn("damaged", logs.output[0])
        self.s.apply([fav("l_a", T0)])                              # and it works normally
        self.assertEqual(len(self.s.changes_since(0)["changes"]), 1)


class Snapshots(StoreTest):
    def test_snapshot_is_consistent_single_file_with_json(self):
        self.s.apply([fav("l_a", T0)])
        out = Path(self.tmp.name) / "backups"
        db, js = snapshot(self.path, out, keep=2)
        self.assertFalse(Path(str(db) + "-wal").exists())
        with closing(sqlite3.connect(db)) as con:
            self.assertEqual(con.execute("SELECT key FROM state").fetchall(), [("l_a",)])
        self.assertEqual(json.loads(js.read_text())["favourites"][0]["key"], "l_a")
        self.assertEqual(oct(db.stat().st_mode & 0o777), "0o600")


if __name__ == "__main__":
    unittest.main()
