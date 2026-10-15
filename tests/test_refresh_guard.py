"""Catalogue refresh safety: suspicious refreshes are rejected (old catalogue kept), changes recorded."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tvapp import providers, refresh_guard
from tvapp.catalog import Catalogue


def catalogue(live=3000, movies=20000, series=5000, cats=10, prefix="", uncat=False):
    d = providers.empty()
    for kind, n, letter in (("live", live, "l"), ("movie", movies, "m"), ("series", series, "s")):
        for c in range(cats):
            d["cats"][kind][f"{kind}{c}"] = f"{kind.title()} cat {c}"
        for i in range(n):
            d[kind].append({"id": f"{letter}_{prefix}{i:08x}", "name": f"{kind} {i}", "cat": "x" if uncat else f"{kind}{i % cats}",
                            "added": i})
    if uncat:
        for kind in ("live", "movie", "series"):
            d["cats"][kind] = {"x": "Uncategorised"}
    return d


class Compare(unittest.TestCase):
    def setUp(self):
        self.old = catalogue()

    def test_normal_churn_is_accepted_and_counted(self):
        new = catalogue()
        new["movie"] = new["movie"][150:] + [{"id": f"m_new{i}", "name": f"New film {i}", "cat": "movie1", "added": 10**9 + i}
                                             for i in range(300)]
        r = refresh_guard.compare(self.old, new)
        self.assertEqual(r["status"], "accepted")
        self.assertEqual((r["added"]["movie"], r["removed"]["movie"]), (300, 150))
        self.assertEqual(r["new_titles"]["movie"][0], "New film 299")         # newest first
        self.assertEqual(len(r["new_ids"]["movie"]), 300)
        self.assertEqual(refresh_guard.change_line(r), "+300/-150 movies")
        self.assertFalse(r["warnings"])

    def test_empty_section_is_rejected(self):
        new = catalogue()
        new["movie"] = []
        r = refresh_guard.compare(self.old, new)
        self.assertEqual(r["status"], "rejected")
        self.assertIn("movies: came back empty (had 20000)", r["anomalies"])

    def test_large_drop_is_rejected_but_a_small_one_only_warns(self):
        new = catalogue()
        new["movie"] = new["movie"][:12000]                                    # -40%
        self.assertEqual(refresh_guard.compare(self.old, new)["status"], "rejected")
        new = catalogue()
        new["movie"] = new["movie"][:18500]                                    # -7.5%
        r = refresh_guard.compare(self.old, new)
        self.assertEqual(r["status"], "accepted")
        self.assertTrue(any("movies: 20000 -> 18500" in w for w in r["warnings"]))

    def test_live_has_a_looser_limit(self):
        new = catalogue()
        new["live"] = new["live"][:2400]                                       # -20% channels, 600: fine
        self.assertEqual(refresh_guard.compare(self.old, new)["status"], "accepted")

    def test_renumbered_ids_are_rejected(self):
        new = catalogue(prefix="z")                                            # same counts, all IDs new
        r = refresh_guard.compare(self.old, new)
        self.assertEqual(r["status"], "rejected")
        self.assertTrue(any("IDs disappeared" in a for a in r["anomalies"]))

    def test_categories_disappearing(self):
        new = catalogue()
        new["movie"] = [m for m in new["movie"] if m["cat"] != "movie3"]          # one category (10%): warn
        del new["cats"]["movie"]["movie3"]
        r = refresh_guard.compare(self.old, new)
        self.assertEqual(r["status"], "accepted")
        self.assertIn("Movie cat 3", r["cats_removed"]["movie"])
        self.assertTrue(any("categories gone: Movie cat 3 (2000)" in w for w in r["warnings"]))
        new = catalogue()
        gone = {"movie0", "movie1", "movie2"}
        for c in gone:
            del new["cats"]["movie"][c]
        new["movie"] = [m for m in new["movie"] if m["cat"] not in gone] + [
            {"id": f"m_x{i}", "name": "x", "cat": "movie5"} for i in range(6000)]  # count kept up, 30% of cats' items gone
        r = refresh_guard.compare(self.old, new)
        self.assertEqual(r["status"], "rejected")
        self.assertTrue(any("3 categories with 6000 items disappeared" in a for a in r["anomalies"]))

    def test_malformed_replies(self):
        r = refresh_guard.compare(self.old, catalogue(), {"movie": (20500, 500)})
        self.assertTrue(any("500 of 20500 rows were unusable" in a for a in r["anomalies"]))
        r = refresh_guard.compare(self.old, catalogue(uncat=True))
        self.assertTrue(any("category list missing" in a for a in r["anomalies"]))

    def test_first_refresh_accepts_anything_non_hostile(self):
        r = refresh_guard.compare(providers.empty(), catalogue(movies=10))
        self.assertEqual(r["status"], "accepted")
        self.assertEqual(r["new_titles"], {})                                   # not "everything is new"


class History(unittest.TestCase):
    def test_bounded_and_ids_only_kept_recently(self):
        with tempfile.TemporaryDirectory() as d:
            h = refresh_guard.History(Path(d) / "h.json", clock=lambda: 1000)
            for i in range(refresh_guard.HISTORY_KEEP + 5):
                h.record("accepted", "scheduled", {"added": {"movie": i}, "removed": {}, "new_ids": {"movie": ["m_1"]}})
            self.assertEqual(len(h.entries), refresh_guard.HISTORY_KEEP)
            self.assertEqual(sum(1 for e in h.entries if "new_ids" in e), refresh_guard.IDS_KEEP_ENTRIES)
            h.record("failed", "scheduled", error="xtream movies: HTTP 502")
            h.record("rejected", "scheduled", {"anomalies": ["movies: came back empty"]})
            self.assertEqual(h.failing_streak(), 2)
            again = refresh_guard.History(Path(d) / "h.json")
            self.assertEqual(again.summary()["last"]["status"], "rejected")
            self.assertEqual(again.summary()["last_ok"]["changes"], f"+{refresh_guard.HISTORY_KEEP + 4}/-0 movies")


class FakeProvider:
    kind = "xtream"

    def __init__(self, results):
        self.results = list(results)
        self.stats = {}

    async def fetch(self, http):
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class CatalogueRefresh(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        s = SimpleNamespace(state_dir=Path(self.tmp.name), configured=True, refresh_hours=12, provider_type="xtream",
                            m3u_url="", xtream_base="http://x", xtream_user="u", xtream_pass="p", epg_url="")
        self.cat = Catalogue(s, None)
        self.events = []
        self.cat.on_event = lambda kind, entry: self.events.append((kind, entry))

    def tearDown(self):
        self.tmp.cleanup()

    def run_refresh(self, *results):
        self.cat.provider = FakeProvider(results)
        return [asyncio.run(self.cat.refresh("test")) for _ in results]

    def test_rejected_refresh_keeps_the_old_catalogue_until_accepted(self):
        good = catalogue(movies=1000)
        self.assertEqual(self.run_refresh(good), [True])
        version = self.cat.version
        bad = catalogue(movies=1000)
        bad["movie"] = []
        self.assertEqual(self.run_refresh(bad), [False])
        self.assertEqual(len(self.cat.data["movie"]), 1000)                     # still serving the good one
        self.assertEqual(self.cat.version, version)                             # lists/caches not rebuilt
        self.assertIn("refresh rejected", self.cat.error)
        self.assertEqual(self.events[-1][0], "rejected")
        self.assertGreater(self.cat.next_try, 0)                                # scheduled retry backs off
        st = self.cat.status()["refresh"]
        self.assertEqual(st["last"]["status"], "rejected")
        self.cat.accept_next = True                                             # iptv refresh --accept-changes
        self.assertEqual(self.run_refresh(bad), [True])
        self.assertEqual(len(self.cat.data["movie"]), 0)
        self.assertFalse(self.cat.accept_next)
        self.assertEqual(self.cat.next_try, 0)

    def test_provider_failure_is_recorded_with_backoff(self):
        self.run_refresh(catalogue(movies=1000))
        r = self.run_refresh(providers.ProviderError("xtream movies: provider answered HTTP 502 - live streams had loaded fine"),
                             providers.ProviderError("xtream login: ConnectTimeout"))
        self.assertEqual(r, [False, False])
        self.assertEqual(self.cat.history.failing_streak(), 2)
        self.assertEqual([k for k, _ in self.events], ["accepted", "failed", "failed"])
        self.assertEqual(len(self.cat.data["movie"]), 1000)
        self.assertIn("live streams had loaded fine", self.cat.history.entries[-2]["error"])


class DetailsSave(unittest.TestCase):
    """Opening a series while another one's details are being written used to fail with
    "dictionary changed size during iteration" (the writer thread walked the live dict)."""

    def test_details_can_be_added_while_a_save_is_writing(self):
        import json
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            s = SimpleNamespace(state_dir=Path(tmp), configured=True, refresh_hours=12, provider_type="xtream",
                                m3u_url="", xtream_base="http://x", xtream_user="u", xtream_pass="p", epg_url="")
            cat = Catalogue(s, None)
            cat.details["series"] = {f"s{i}": (0, {}, []) for i in range(50)}
            started, added = threading.Event(), threading.Event()
            write = cat._write

            def slow_write(path, obj):                    # stops half-way through, until a detail is added
                items = iter(obj["details"]["series"].items())
                next(items)
                started.set()
                added.wait(5)
                list(items)
                write(path, obj)
            cat._write = slow_write

            async def go():
                saving = asyncio.create_task(cat.save_details())
                await asyncio.to_thread(started.wait, 5)
                cat.details["series"]["new"] = (1, {}, [])
                added.set()
                await saving
                cat._write = write
                await cat.save_details()                  # the next save has it
            asyncio.run(go())
            saved = json.loads((Path(tmp) / "details.json").read_text())["details"]["series"]
            self.assertIn("new", saved)
            self.assertEqual(len(saved), 51)


if __name__ == "__main__":
    unittest.main()
