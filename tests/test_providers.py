"""Xtream catalogue fetch (normalisation, errors, not blocking the event loop) and the big-JSON helpers."""
import asyncio
import io
import json
import time
import unittest
from pathlib import Path

import httpx

from tvapp.config import Settings
from tvapp.providers import ProviderError, XtreamProvider
from tvapp.util import iter_json_array, make_id, write_json

SETTINGS = Settings(provider_type="xtream", m3u_url="", epg_url="", xtream_base="http://prov.example:8080",
                    xtream_user="u", xtream_pass="p", user_agent="t", refresh_hours=12, epg_refresh_hours=12,
                    max_live=2, max_vod=2, state_dir=Path("."), cache_dir=Path("."), live_dir=Path("."),
                    vod_dir=Path("."), web_dir=Path("."), host="", port=0, public_origin="")


def provider_replies(over=None, **kw):
    data = {
        None: {"user_info": {"auth": 1, "status": "Active"}},
        "get_live_categories": [{"category_id": "1", "category_name": "News"}],
        "get_vod_categories": [{"category_id": "7", "category_name": "Films"}],
        "get_series_categories": [],
        "get_live_streams": ["junk", {"stream_id": 11, "name": "BBC One", "category_id": "1", "num": "",
                                      "stream_icon": "http://x/bbc.png", "epg_channel_id": "bbc1.uk"},
                             {"stream_id": None, "name": "no id"}, {"stream_id": 12, "name": "Odd", "category_id": "42"}],
        "get_vod_streams": [{"stream_id": 21, "name": "Film (1999)", "category_id": "7", "container_extension": "MKV",
                             "rating_5based": 4, "added": "100"}],
        "get_series": [{"series_id": 31, "name": "Show", "category_id": "5", "backdrop_path": ["http://x/b.jpg"],
                        "releaseDate": "2020-02-02", "plot": "<i>Plot</i>"}],
    }
    data.update(over or {}, **kw)
    return data


async def fetch(data, status=200):
    def handler(request):
        reply = data[request.url.params.get("action")]
        return httpx.Response(status, content=reply if isinstance(reply, bytes) else json.dumps(reply).encode())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        return await XtreamProvider(SETTINGS).fetch(http)


class Fetch(unittest.TestCase):
    def test_normalised_catalogue(self):
        out = asyncio.run(fetch(provider_replies()))
        live = out["live"]
        self.assertEqual([(c["id"], c["name"], c["num"], c["cat"]) for c in live],
                         [(make_id("l", "xt", 11), "BBC One", 2, "1"), (make_id("l", "xt", 12), "Odd", 4, "42")])
        self.assertEqual(live[0]["url"], "http://prov.example:8080/live/u/p/11.ts")
        self.assertEqual(out["cats"]["live"], {"1": "News", "42": "Uncategorised"})
        m = out["movie"][0]
        self.assertEqual((m["ext"], m["year"], m["rating"], m["added"]), ("mkv", 1999, 8.0, 100))
        s = out["series"][0]
        self.assertEqual((s["backdrop"], s["year"], s["plot"], s["cat"]), ("http://x/b.jpg", 2020, "Plot", "5"))

    def test_empty_or_odd_lists(self):
        out = asyncio.run(fetch(provider_replies(get_live_streams=None, get_vod_streams={"a": 1}, get_series=[])))
        self.assertEqual((out["live"], out["movie"], out["series"]), ([], [], []))

    def test_errors(self):
        with self.assertRaisesRegex(ProviderError, "movies: provider did not return JSON"):
            asyncio.run(fetch(provider_replies(get_vod_streams=b"<html>busy</html>")))
        with self.assertRaisesRegex(ProviderError, "series: provider did not return JSON"):
            asyncio.run(fetch(provider_replies(get_series=b'[{"series_id": 1}, {"series')))   # cut off
        with self.assertRaisesRegex(ProviderError, "rejected the username"):
            asyncio.run(fetch(provider_replies({None: {"user_info": {"auth": 0}}})))
        with self.assertRaisesRegex(ProviderError, "HTTP 500"):
            asyncio.run(fetch(provider_replies(), status=500))

    def test_event_loop_keeps_running_during_a_big_list(self):
        """The movie list is parsed in a worker thread in small steps: the loop is never stuck."""
        rows = [{"stream_id": i, "name": f"Film number {i} (2001)", "category_id": str(i % 50),
                 "stream_icon": f"http://img.example/{i}.jpg", "container_extension": "mp4", "rating_5based": 3.5,
                 "added": "1700000000", "plot": "x" * 200} for i in range(150_000)]
        data = provider_replies(get_vod_streams=json.dumps(rows).encode())

        async def main():
            gaps, done = [], False

            async def ticker():
                last = time.perf_counter()
                while not done:
                    await asyncio.sleep(0.005)
                    now = time.perf_counter()
                    gaps.append(now - last)
                    last = now
            tick = asyncio.create_task(ticker())
            out = await fetch(data)
            done = True
            await tick
            return out, max(gaps)
        out, worst = asyncio.run(main())
        self.assertEqual(len(out["movie"]), 150_000)
        self.assertLess(worst, 0.3, f"event loop blocked for {worst:.2f}s")


class BigJson(unittest.TestCase):
    def test_iter_json_array(self):
        rows = [{"a": i, "s": 'é "q" ] \\', "l": [1, {"b": []}]} for i in range(5000)]
        for text, want in ((json.dumps(rows), rows), (" \n[ 1 ,2,\n3 ] ", [1, 2, 3]), ("[]", []), ("[ ]", []),
                           ('{"a": [1]}', []), ("null", []), ("﻿[1]", [1])):
            self.assertEqual(list(iter_json_array(text.encode())), want, text[:20])
        for bad in ("[1,2", "[1 2]", "[1,]", "nope", ""):
            with self.assertRaises(ValueError, msg=bad):
                list(iter_json_array(bad.encode()))

    def test_write_json_matches_json_dumps(self):
        doc = {"provider": "x", "updated": 1.5, "data": {"movie": [{"id": i, "n": "é"} for i in range(9001)],
                                                          "episode": [], "cats": {"7": "Films"}}, "e": {}}
        for chunk in (1, 4000, 10_000):
            fh = io.StringIO()
            write_json(fh, doc, chunk)
            self.assertEqual(fh.getvalue(), json.dumps(doc, separators=(",", ":")), chunk)


if __name__ == "__main__":
    unittest.main()
