"""Favourite teams / competitions: For You (epgsearch.Index.for_you) and match alerts
(tvapp/sportsalerts.py) - once per match, whatever happens to the guide or the service."""
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from tvapp import epgsearch, livemeta, server, sports, sportsalerts
from tvapp.catalog import Catalogue
from tvapp.userstate import StateStore
from tests.test_state_api import settings

NOW = 1_800_000_000
H = 3600
SPORT = sports.OTHER


def row(start_h, hours, title, desc=""):
    return [int(NOW + start_h * H), int(NOW + (start_h + hours) * H), title, desc]


def guide(shift_h=0.0):
    k = 0.25 + shift_h                                                     # Arsenal v Chelsea kicks off in 15 min
    return {
        "l_tnt": [row(k, 2, "UEFA Champions League: Arsenal v Chelsea"), row(26, 2, "Premier League: Arsenal v Leeds United")],
        "l_bein": [row(k - 0.08, 2, "Arsenal vs Chelsea - UEFA Champions League 2026/2027 - MD 3")],   # 5 min earlier
        "l_ss": [row(k, 2, "UCL 26/27: Arsenal v Chelsea"), row(5, 2, "UCL 26/27: Arsenal v Chelsea")],  # + a replay
        "l_es": [row(0.1, 2, "LaLiga: Barcelona v Real Madrid"), row(0.2, 2, "Arsenal Women v Chelsea Women - WSL"),
                 row(0.2, 1.5, "UEFA Youth League: Arsenal v Inter")],
        "l_mu": [row(0.15, 2, "LIVE Champions League: Man Utd v Benfica")],
        "l_hl": [row(0.1, 0.5, "UCL Highlights: Arsenal v Chelsea")],
    }


def index(shift_h=0.0):
    g = guide(shift_h)
    return epgsearch.Index(g, NOW, horizon=40 * H, channel_sport={k: SPORT for k in g})


class ForYou(unittest.TestCase):
    def setUp(self):
        self.idx = index()

    def titles(self, favs, lo=NOW, hi=NOW + 48 * H):
        return sorted((e["title"], e["replay"]) for e in self.idx.for_you(favs, NOW, lo, hi))

    def test_favourite_team(self):
        got = self.titles(["team:arsenal"])
        self.assertIn(("Arsenal vs Chelsea", False), got)
        self.assertIn(("Arsenal vs Chelsea", True), got)                      # the 5-hours-later airing: a replay
        self.assertIn(("Arsenal vs Leeds United", False), got)                # tomorrow
        self.assertIn(("Arsenal (W) vs Chelsea (W)", False), got)            # shown (tagged) but never alerted
        self.assertNotIn("UCL Highlights: Arsenal v Chelsea", [t for t, _ in got])
        now_ev = next(e for e in self.idx.for_you(["team:arsenal"], NOW, NOW, NOW + H) if e["title"] == "Arsenal vs Chelsea")
        self.assertEqual(sorted(now_ev["ids"]), ["l_bein", "l_ss", "l_tnt"])  # three broadcasters, one event
        self.assertEqual((now_ev["comp_id"], now_ev["teams"]), ("comp:uefa-champions-league", ["team:arsenal", "team:chelsea"]))

    def test_team_and_competition_once(self):
        both = self.titles(["team:arsenal", "comp:uefa-champions-league"])
        self.assertEqual(both.count(("Arsenal vs Chelsea", False)), 1)
        self.assertIn(("Manchester United vs Benfica", False), both)        # "Man Utd" in the guide: the canonical team
        self.assertNotIn("Barcelona vs Real Madrid", [t for t, _ in both])

    def test_aliases_and_unknown_ids(self):
        self.assertEqual([t for t, _ in self.titles(["team:manchester-united"])], ["Manchester United vs Benfica"])
        self.assertEqual(self.titles(["team:no-such-club", "nonsense"]), [])

    def test_renamed_entries_keep_old_ids(self):
        ents = sports._load("@club football\nteam:arsenal-fc = Arsenal ; was: team:arsenal team:the-arsenal\n")
        self.assertEqual((ents[0].key, ents[0].was), ("team:arsenal-fc", ["team:arsenal", "team:the-arsenal"]))
        self.assertTrue(all(e.key in sports.BY_ID_KEY for e in sports.ENTITIES))


class FakeAlerts:
    def __init__(self, ok=True, configured=True):
        self.sent, self.ok, self.configured = [], ok, configured

    def notify(self, text):
        self.sent.append(text)
        return self.ok


class Alerts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.tmp.name) / "state.db")
        self.store.apply([{"ns": "sfav", "key": "team:arsenal", "value": {"name": "Arsenal", "kind": "team"}, "updated": 1}])
        self.tg = FakeAlerts()
        self.clock = [NOW]

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def check(self, idx=None, alerts=None):
        sa = sportsalerts.SportsAlerts(self.store, alerts or self.tg, clock=lambda: self.clock[0])
        return sa.check(idx or index(), lambda ev: len(ev["ids"]))

    def lead(self, minutes):
        self.store.apply([{"ns": "pref", "key": "sportsAlerts", "value": minutes, "updated": int(time.time() * 1000)}])

    def test_off_until_enabled(self):
        self.assertEqual(self.check()["note"], "alerts are off")
        self.assertEqual(self.tg.sent, [])

    def test_once_per_match_across_checks_restarts_and_rebuilds(self):
        self.lead("15")
        self.check()
        self.assertEqual(len(self.tg.sent), 1)
        text = self.tg.sent[0]
        self.assertIn("Arsenal vs Chelsea starts in 15 minutes", text)        # one event for three broadcasters
        self.assertIn("UEFA Champions League · " + time.strftime("%H:%M", time.localtime(NOW + 0.25 * H)), text)
        self.assertIn("3 broadcasters available", text)
        self.assertNotIn("Women", text)                                       # WSL game also starts soon: not alerted
        self.assertNotIn("Inter", text)                                       # youth: not alerted
        self.clock[0] += 60
        self.check()                                                          # next minute
        self.check(index())                                                   # a new SportsAlerts (restart) + a rebuilt index
        self.store.close()
        self.store = StateStore(Path(self.tmp.name) / "state.db")             # the database reopened (service restart)
        self.check(index())
        self.assertEqual(len(self.tg.sent), 1)

    def test_kick_off_changes(self):
        self.lead("15")
        self.check(index(shift_h=1))                                          # moved an hour later: not due yet
        self.assertEqual(self.tg.sent, [])
        self.check(index(shift_h=-0.1))                                       # moved earlier: due now, at the new time
        self.assertIn("starts in 9 minutes", self.tg.sent[0])
        self.clock[0] += 120
        self.check(index(shift_h=0.05))                                       # then later again: already alerted today
        self.assertEqual(len(self.tg.sent), 1)

    def test_replay_and_disappearing_events(self):
        self.lead("15")
        self.clock[0] = NOW + 4.8 * H                                         # 12 min before the 5-hours-later airing
        self.check()
        self.assertEqual(self.tg.sent, [])                                    # a replay: no "starts in" for it
        g = guide()
        del g["l_tnt"], g["l_bein"], g["l_ss"]
        self.clock[0] = NOW
        self.check(epgsearch.Index(g, NOW, channel_sport={k: SPORT for k in g}))
        self.assertEqual(self.tg.sent, [])                                    # gone from the guide: nothing to send

    def test_telegram_refusal_retries_and_not_set_up_records_nothing(self):
        self.lead("15")
        self.check(alerts=FakeAlerts(ok=False))
        self.assertEqual(self.check(alerts=FakeAlerts(configured=False))["note"], "Telegram is not set up")
        self.check()                                                          # works again: sent now
        self.assertEqual(len(self.tg.sent), 1)

    def test_single_untagged_broadcast_is_not_trusted(self):
        self.lead("15")
        g = {"l_one": [row(0.2, 2, "EFL Cup: Arsenal v Southampton")]}             # one channel, no LIVE tag
        idx = epgsearch.Index(g, NOW, channel_sport={"l_one": SPORT})
        self.check(idx)
        self.assertEqual(self.tg.sent, [])
        g = {"l_one": [row(0.2, 2, "LIVE EFL Cup: Arsenal v Southampton")]}        # the broadcaster says LIVE
        self.check(epgsearch.Index(g, NOW, channel_sport={"l_one": SPORT}))
        self.assertEqual(len(self.tg.sent), 1)

    def test_several_due_make_one_message(self):
        self.lead("30")
        self.store.apply([{"ns": "sfav", "key": "comp:uefa-champions-league", "value": {"name": "UCL", "kind": "comp"},
                           "updated": 2}])
        self.check()
        self.assertEqual(len(self.tg.sent), 1)
        self.assertIn("Arsenal vs Chelsea", self.tg.sent[0])
        self.assertIn("Manchester United vs Benfica", self.tg.sent[0])
        self.assertEqual(self.tg.sent[0].count("starts in"), 2)


class Endpoints(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        s = settings(Path(self.tmp.name))
        server.S.settings = s
        server.S.cat = Catalogue(s, None)
        g = guide()
        server.S.cat.data["live"] = [{"id": i, "name": f"UK: {i[2:].upper()} SPORTS HD", "cat": "c", "url": "u"} for i in g]
        server.S.cat.data["cats"]["live"] = {"c": "UK| SPORT"}
        server.S.cat._index()
        server.S.cat.guide.data = g
        server.S.cat.guide.updated = NOW
        server.S.live_meta = livemeta.MetaCache(Path(self.tmp.name) / "lm.json")
        server.S.vindex = None
        server.S.epg_index = None
        server.S.epg_index_task = None
        server.S.tasks = set()
        server.S.sports_cache = {}
        self.c = TestClient(server.app)
        self._t = epgsearch.time.time, server.time.time
        epgsearch.time.time = server.time.time = lambda: NOW

    def tearDown(self):
        epgsearch.time.time, server.time.time = self._t
        self.tmp.cleanup()

    def test_for_you_and_catalogue(self):
        d = self.c.get("/tv/api/sports", params={"view": "foryou", "fav": "team:arsenal,comp:uefa-champions-league,bogus",
                                                  "tzo": "0"}).json()
        titles = [(e["title"], e["day"], e["replay"]) for e in d["events"]]
        self.assertEqual(titles.count(("Arsenal vs Chelsea", "today", False)), 1)
        self.assertIn(("Arsenal vs Leeds United", "tomorrow", False), titles)
        self.assertTrue(all(e["channels"] for e in d["events"]))
        cat = self.c.get("/tv/api/sports/catalogue").json()["items"]
        ids = {i["id"]: i for i in cat}
        self.assertEqual((ids["team:arsenal"]["kind"], ids["comp:formula-1"]["kind"]), ("team", "comp"))
        self.assertTrue(ids["team:finland"]["national"])
        s = self.c.get("/tv/api/live/search", params={"q": "Arsenal"}).json()
        marked = {e["title"]: e["fav_ids"] for e in s["events"]}
        self.assertIn("team:arsenal", marked["Arsenal vs Chelsea"])           # Live search can mark favourites
        self.assertIn("comp:uefa-champions-league", marked["Arsenal vs Chelsea"])
        self.assertIn("team:arsenal", marked["UCL Highlights: Arsenal v Chelsea"])


if __name__ == "__main__":
    unittest.main()
