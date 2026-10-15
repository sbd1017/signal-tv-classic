"""Live TV search over the guide: what matches, how events are grouped and ranked, and the endpoint."""
import asyncio
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from tvapp import epgsearch, livemeta, server, sports
from tvapp.catalog import Catalogue
from tests.test_state_api import settings

NOW = 1_800_000_000
H = 3600


def row(start_h, hours, title, desc=""):
    return [int(NOW + start_h * H), int(NOW + (start_h + hours) * H), title, desc]


GUIDE = {
    "l_tnt4": [row(-1, 1.5, "Premier League", "Reigning champions Arsenal host Leeds United."),
               row(4, 2, "Women's Champions League: Barça v Lyon")],
    "l_bein1": [row(-0.5, 2, "Arsenal vs Leeds United - English Premier League")],
    "l_bein1hd": [row(-0.5, 2, "Arsenal vs Leeds United - English Premier League")],
    "l_bein1ar": [row(-0.4, 2, "ARSENAL VS LEEDS UNITED – English Premier League")],
    "l_navy": [row(-0.5, 1, "Navy CIS", "Gibbs investigates.")],
    "l_nbatv": [row(0.5, 2, "NBA Finals: Game 1"), row(30, 1, "NBA Tomorrow")],
    "l_f1": [row(-2, 1, "F1 Grand Prix Highlights"), row(2, 2, "Formula 1: Singapore Grand Prix", "Fórmula 1 en directo"),
             row(6, 1, "F150 Trucks Weekly")],
    "l_doc": [row(1, 1, "Real Lives", "Real-life stories from Madrid."), row(8, 1, "Real Madrid v Barcelona")],
    "l_late": [row(3, 1, "Arsenal Classics")],
    "l_utd": [row(5, 2, "Manchester United v Chelsea")],
}
NAMES = {"l_tnt4": "UK: TNT SPORTS 4 ᴴᴰ", "l_bein1": "UK: BEIN SPORTS 1 SD", "l_bein1hd": "UK: BEIN SPORTS 1 ᴴᴰ",
         "l_bein1ar": "AR: ALKASS ONE ᴴᴰ", "l_navy": "US: CBS", "l_nbatv": "US: NBA TV HD", "l_f1": "UK: SKY SPORTS F1 ᴴᴰ",
         "l_doc": "UK: BBC FOUR HD", "l_late": "UK: ARSENAL TV", "l_utd": "UK: SKY SPORTS PREMIER LEAGUE HD",
         "l_bein1uhd": "4K: BEIN SPORTS 1 ᵁᴴᴰ",
         "l_8k1raw": "8K: beIN SP⚽RTS 1 ᴿᴬᵂ", "l_8k1sd": "8K: beIN SP⚽RTS 1 SD", "l_fr8k": "FR: beIN SP⚽RTS 1 ᴴᴰ",
         "l_ar8k": "AR: beIN SP⚽RTS 1 ᴴᴰ"}


# what the server derives from channel names (sports.channel_sport)
CHANNEL_SPORT = {i: sports.channel_sport(n, "") for i, n in NAMES.items() if sports.channel_sport(n, "")}


class Matching(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.idx = epgsearch.Index(GUIDE, NOW, channel_sport=CHANNEL_SPORT)

    def titles(self, q):
        return [e["title"] for e in self.idx.search(q, NOW)["events"]]

    def test_normalising(self):
        self.assertEqual(epgsearch.norm("Women's Champions-League:  Barça!"), "womens champions league barca")
        self.assertIn("Barcelona (W) vs Lyon (W)", self.titles("barca"))
        self.assertIn("Barcelona (W) vs Lyon (W)", self.titles("WOMENS champions-league"))
        self.assertIn("Formula 1: Singapore Grand Prix", self.titles("fórmula"))       # accents both ways

    def test_word_start_only(self):
        self.assertEqual(self.titles("nba"), ["NBA Finals: Game 1"])                  # not "Navy CIS", not tomorrow +30h
        self.assertIn("Arsenal Classics", self.titles("arsen"))                       # typing: the last word may be unfinished
        self.assertEqual(self.titles("senal"), [])

    def test_short_words_match_whole_words(self):
        f1 = self.titles("f1")
        self.assertIn("Formula 1: Singapore Grand Prix", self.titles("formula 1"))
        self.assertNotIn("F150 Trucks Weekly", f1)
        self.assertEqual(self.titles("a"), [])                                          # under MIN_QUERY

    def test_phrase_or_all_words_in_title(self):
        self.assertEqual(self.titles("real madrid"), ["Real Madrid vs Barcelona"])    # not "Real-life stories from Madrid"
        self.assertIn("Arsenal vs Leeds United", self.titles("leeds arsenal"))
        self.assertEqual(self.titles("man utd"), ["Manchester United vs Chelsea"])      # a known alias (sports.py)
        self.assertEqual(self.titles("manchester unted"), [])                            # but nothing fuzzy

    def test_description_matches(self):
        ev = self.idx.search("reigning champions", NOW)["events"]
        self.assertEqual([(e["title"], e["when"], e["strength"]) for e in ev], [("Arsenal vs Leeds United", "live", 1)])

    def test_duplicates_are_one_event_and_ranking(self):
        ev = self.idx.search("arsenal", NOW)["events"]
        self.assertEqual([e["when"] for e in ev], ["live", "later"])
        top = ev[0]
        # beIN's "Arsenal vs Leeds United - English Premier League" (three feeds, starts 6 min apart) and
        # TNT's "Premier League" whose description says Arsenal host Leeds United: one match
        self.assertEqual((top["title"], top["sub"]), ("Arsenal vs Leeds United", "Premier League"))
        self.assertEqual(sorted(top["ids"]), ["l_bein1", "l_bein1ar", "l_bein1hd", "l_tnt4"])
        self.assertEqual(top["strength"], 3)
        self.assertEqual(ev[1]["title"], "Arsenal Classics")

    def test_window(self):
        self.assertNotIn("F1 Grand Prix Highlights", self.titles("f1"))               # ended
        self.assertEqual(self.idx.search("formula", NOW)["events"][0]["when"], "later")
        self.assertEqual(self.idx.search("nba finals", NOW)["events"][0]["when"], "soon")

    def test_merged_guides_one_live_event_per_title(self):
        # One channel's guide merged from several sources (seen for "8K: beIN SPORTS 1"): the match
        # listed at 11:45, 11:50 and 12:15, repeats later that overlap it, and a 24-hour row.
        t = "Liverpool vs Manchester City - English Premier League"
        guide = {"l_a": [row(-26, 25, t), row(-2.25, 3.25, t), row(-2.17, 3.25, t), row(-1.75, 3.5, t), row(0.75, 3.25, t),
                         row(10, 2, t)],
                 "l_b": [row(-1.25, 2.5, t)], "l_c": [row(-1.5, 2.5, t)], "l_d": [row(-1.25, 2.5, t)]}
        ev = epgsearch.Index(guide, NOW).search("liverpool", NOW)["events"]
        self.assertEqual([(e["when"], sorted(e["ids"])) for e in ev], [("live", ["l_a", "l_b", "l_c", "l_d"]), ("later", ["l_a"])])
        self.assertEqual((ev[0]["start"], ev[0]["stop"]), (NOW - 1.25 * H, NOW + 1.25 * H))     # the most common listing
        self.assertEqual(ev[1]["start"], NOW + 10 * H)                       # the 00:45 repeat overlapped the live one

    def test_broad_queries_list_only_now_and_soon(self):
        guide = {f"l_{i}": [row(-0.5, 1, f"The Show {i}"), row(5, 1, f"The Late Show {i}")] for i in range(9000)}
        r = epgsearch.Index(guide, NOW).search("the", NOW)
        self.assertTrue(r["broad"])
        self.assertTrue(all(e["when"] != "later" for e in r["events"]))


class Groups(unittest.TestCase):
    def setUp(self):
        live = [{"id": i, "name": n, "cat": "c"} for i, n in NAMES.items()]
        self.vindex = livemeta.VariantIndex(live, {"c": "SPORT"})
        self.metas = {"l_bein1uhd": {"v": "hevc", "bits": 10, "h": 2160, "fps": 50.0},
                      "l_bein1hd": {"v": "h264", "h": 720, "fps": 50.0}, "l_bein1": {"v": "h264", "h": 576, "fps": 25.0}}

    def groups(self, caps):
        return epgsearch.channel_groups(["l_bein1", "l_bein1ar", "l_bein1hd"], self.vindex, self.metas.get,
                                        livemeta.parse_caps(caps))

    def bein(self, caps):
        return next(g for g in self.groups(caps) if "l_bein1" in g["ids"])

    def test_versions_collapse_and_best_for_device(self):
        g = self.groups("avc.1080.60.s")
        self.assertEqual(len(g), 2)                                    # beIN 1 (SD + HD + 4K versions), and Alkass
        self.assertEqual(g[0]["ids"], ["l_bein1", "l_bein1hd"])
        self.assertEqual((g[0]["id"], g[0]["ok"], g[0]["versions"]), ("l_bein1hd", 2, 3))
        self.assertTrue(g[0]["label"].startswith("720p 50fps"))      # no HEVC here: the 4K version doesn't count
        self.assertEqual((g[1]["id"], g[1]["ok"], g[1]["est"], g[1]["label"]), ("l_bein1ar", -1, True, "HD"))

    def test_names_estimate_when_nothing_is_known(self):
        self.metas = {}
        g = self.groups("avc.1080.60.s")
        self.assertEqual([(e["id"], e["label"], e["est"], e["ok"]) for e in g],
                         [("l_bein1hd", "HD", True, -1), ("l_bein1ar", "HD", True, -1)])   # "4K" name: not for this device
        self.assertEqual(self.groups("avc.2160.60.s,hevc10.2160.60.s")[0]["label"], "4K")

    def test_same_name_and_prefix_in_one_event_is_one_channel(self):
        self.assertEqual(self.vindex.variants("l_8k1sd"), ["l_8k1sd"])           # 8K: + two countries: not paired in general
        g = epgsearch.channel_groups(["l_8k1sd", "l_8k1raw", "l_fr8k"], self.vindex, {}.get, livemeta.parse_caps(""))
        self.assertEqual([(e["id"], e["ids"], e["label"]) for e in g],
                         [("l_8k1raw", ["l_8k1sd", "l_8k1raw"], "1080p"), ("l_fr8k", ["l_fr8k"], "HD")])

    def test_interlaced_only_channel_shows_the_conversion(self):
        paff = {"v": "h264", "h": 1080, "w": 1920, "fps": 25.0, "interlaced": True, "coding": "field", "field_order": "tt"}
        self.metas = {"l_bein1": paff, "l_bein1hd": paff, "l_bein1uhd": paff}           # like SPORT TV: every version
        g = self.bein("avc.2160.60.s")
        self.assertEqual((g["ok"], g["label"], g["conv"]), (0, "720p 50fps · H.264", "1080i 25fps · H.264"))
        self.metas["l_bein1hd"] = {"v": "h264", "h": 1080, "w": 1920, "fps": 50.0}     # a progressive version: no conversion
        g = self.bein("avc.2160.60.s")
        self.assertEqual((g["id"], g["ok"], g["conv"]), ("l_bein1hd", 2, ""))
        self.assertEqual(self.bein("avc.2160.60.s,avcf.1080.30.u")["conv"], "")      # iPhone decodes the fields

    def test_capable_device_sees_4k(self):
        g = self.groups("avc.2160.60.s,hevc10.2160.60.s")
        self.assertTrue(g[0]["label"].startswith("4K 50fps"))
        self.assertEqual(g[0]["id"], "l_bein1hd")                      # an EPG channel; the live path switches to the 4K version


class Endpoint(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        s = settings(Path(self.tmp.name))
        server.S.settings = s
        server.S.cat = Catalogue(s, None)
        server.S.cat.data["live"] = [{"id": i, "name": n, "cat": "c", "url": "u"} for i, n in NAMES.items()]
        server.S.cat.data["cats"]["live"] = {"c": "UK| SPORT"}
        server.S.cat._index()
        server.S.cat.guide.data = GUIDE
        server.S.cat.guide.updated = NOW
        server.S.live_meta = livemeta.MetaCache(Path(self.tmp.name) / "lm.json")
        server.S.vindex = None
        server.S.epg_index = None
        server.S.epg_index_task = None
        server.S.tasks = set()
        server.S.sports_cache = {}
        self.c = TestClient(server.app)
        self._time = epgsearch.time.time
        epgsearch.time.time = lambda: NOW

    def tearDown(self):
        epgsearch.time.time = self._time
        self.tmp.cleanup()

    def test_search(self):
        r = self.c.get("/tv/api/live/search", params={"q": "Arsenal", "caps": "avc.1080.60.s"})
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual((d["events"][0]["title"], d["events"][0]["sub"]), ("Arsenal vs Leeds United", "Premier League"))
        self.assertEqual({g["id"] for g in d["events"][0]["channels"]}, {"l_bein1hd", "l_tnt4", "l_bein1ar"})   # beIN 1's versions, TNT 4, Alkass
        self.assertEqual(self.c.get("/tv/api/live/search", params={"q": "a"}).json()["events"], [])

    def test_rebuilt_when_the_guide_changes(self):
        self.c.get("/tv/api/live/search", params={"q": "arsenal"})
        first = server.S.epg_index
        server.S.cat.guide.data = {"l_navy": [row(-0.5, 1, "Arsenal Weekly")]}
        server.S.cat.guide.updated = NOW + 60

        # The rebuild runs as a background task. TestClient gives each request its own short-lived
        # event loop, so the task can't be left running between requests (on a slow machine like the
        # Pi it was cut off and the next request still saw the old index): start it and wait for it
        # in one loop here.
        async def change():
            self.assertIs(await server.epg_index(), first)                 # answered by the old index...
            await server.S.epg_index_task                                  # ...while the new one is built
        asyncio.run(change())
        d = self.c.get("/tv/api/live/search", params={"q": "arsenal"}).json()
        self.assertIsNot(server.S.epg_index, first)
        self.assertEqual([e["title"] for e in d["events"]], ["Arsenal Weekly"])


if __name__ == "__main__":
    unittest.main()


class XmltvCategories(unittest.TestCase):
    def test_categories_are_kept_and_searchable(self):
        from datetime import datetime, timezone
        from tvapp.epg import parse_xmltv
        t = lambda h: datetime.fromtimestamp(NOW + h * H, timezone.utc).strftime("%Y%m%d%H%M%S +0000")
        xml = (f'<tv><channel id="tnt4.uk"><display-name>TNT Sports 4</display-name></channel>'
               f'<programme channel="tnt4.uk" start="{t(-1)}" stop="{t(1)}"><title>Arsenal v Leeds</title>'
               f'<category>Football</category><category>UEFA Europa League</category><category>Football</category></programme>'
               f'<programme channel="tnt4.uk" start="{t(1)}" stop="{t(2)}"><title>Highlights</title></programme></tv>')
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "g.xml"
            path.write_text(xml)
            from tvapp import epg
            epg_time = epg.time.time
            epg.time.time = lambda: NOW
            try:
                guide = parse_xmltv(path, [{"id": "l_tnt4", "epg": "tnt4.uk", "name": "UK: TNT SPORTS 4"}])
            finally:
                epg.time.time = epg_time
            self.assertEqual(guide["l_tnt4"][0][4], "Football, UEFA Europa League")
            self.assertEqual(len(guide["l_tnt4"][1]), 4)                        # no categories: the row is unchanged
            idx = epgsearch.Index(guide, NOW)
            self.assertEqual([e["title"] for e in idx.search("europa league", NOW)["events"]], ["Arsenal vs Leeds United"])
