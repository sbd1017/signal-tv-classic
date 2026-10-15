"""Pre-built catalogue lists: chunked encoding, build-once caching, the HTTP layer and the payload format."""
import asyncio
import gzip
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from tvapp import lists, server
from tvapp.catalog import Catalogue, img_key
from tvapp.lists import ListCache, encode_gz

from test_state_api import settings


def plain(head, rows):
    return json.dumps({**head, "items": rows}, separators=(",", ":"), ensure_ascii=False)


class Encoding(unittest.TestCase):
    def test_same_json_as_one_dumps_for_every_chunk_boundary(self):
        n = lists.ROWS_PER_CHUNK
        for count in (0, 1, n - 1, n, n + 1, 2 * n, 2 * n + 7):
            rows = [[f"m_{i:016x}", f"Café {i} \"quoted\" ☃", "12", "", i, 7.5] for i in range(count)]
            head = {"v": "abc", "cats": [["12", "Films é"]]}
            self.assertEqual(gzip.decompress(encode_gz(head, iter(rows))).decode(), plain(head, rows), count)

    def test_empty_head_is_refused(self):
        with self.assertRaises(ValueError):
            encode_gz({}, [])


class Cache(unittest.TestCase):
    def setUp(self):
        self.version = "1"
        self.calls = 0

        def build():
            self.calls += 1
            return gzip.compress(f"body{self.version}".encode())
        self.cache = ListCache({"x": (lambda: self.version, build)})

    def test_concurrent_requests_build_once(self):
        async def go():
            return await asyncio.gather(*(self.cache.get("x") for _ in range(8)))
        results = asyncio.run(go())
        self.assertEqual(self.calls, 1)
        self.assertEqual({r[0] for r in results}, {"1"})

    def test_rebuilds_only_when_the_version_changes(self):
        asyncio.run(self.cache.get("x"))
        asyncio.run(self.cache.get("x"))
        self.assertEqual(self.calls, 1)
        self.version = "2"
        tag, body = asyncio.run(self.cache.get("x"))
        self.assertEqual((tag, gzip.decompress(body), self.calls), ("2", b"body2", 2))

    def test_warm_needed(self):
        self.assertTrue(self.cache.warm_needed(["x"]))
        asyncio.run(self.cache.get("x"))
        self.assertFalse(self.cache.warm_needed(["x"]))
        self.version = "2"
        self.assertTrue(self.cache.warm_needed(["x"]))

    def test_warm_survives_a_failing_build(self):
        bad = ListCache({"x": (lambda: "1", lambda: 1 / 0), "y": (lambda: "1", lambda: b"ok")})
        asyncio.run(bad.warm(["x", "y"]))
        self.assertEqual(bad.sizes(), {"y": 2})


class Payloads(unittest.TestCase):
    """public_list() rows are exactly what the browser's shape() expects (same as before caching)."""

    def test_rows(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cat = Catalogue(settings(Path(tmp.name)), http=None)
        cat.version = "v9"
        cat.data = {"live": [{"id": "l_1", "name": "BBC", "cat": "c1", "img": "http://x/a.png", "num": 101}],
                    "movie": [{"id": "m_1", "name": "Film", "cat": "c2", "year": 1999, "rating": 7.1, "added": 5}],
                    "series": [{"id": "s_1", "name": "Show", "cat": "c3", "img": "", "year": 2020}], "episode": [],
                    "cats": {"live": {"c1": "News"}, "movie": {"c2": "Films"}, "series": {"c3": "Shows"}}}
        cat.guide.data = {"l_1": [[0, 1, "x", ""]]}
        expect = {
            "live": {"v": "v9", "cats": [["c1", "News"]], "items": [["l_1", "BBC", "c1", img_key("http://x/a.png"), 101, 1]]},
            "movies": {"v": "v9", "cats": [["c2", "Films"]], "items": [["m_1", "Film", "c2", "", 1999, 7.1, 5]]},
            "series": {"v": "v9", "cats": [["c3", "Shows"]], "items": [["s_1", "Show", "c3", "", 2020, 0]]},
        }
        for kind, want in expect.items():
            head, rows = cat.public_list(kind)
            self.assertEqual({**head, "items": list(rows)}, want, kind)
        with self.assertRaises(ValueError):
            cat.public_list("radio")


class Http(unittest.TestCase):
    def setUp(self):
        self.version = "7"
        self.builds = 0
        server.S.cat = SimpleNamespace(version="7", guide=SimpleNamespace(updated=0, data={}))

        def build():
            self.builds += 1
            return encode_gz({"v": self.version, "cats": []}, [["m_1", "Film", "", "", 0, 0, 0]])
        server.S.lists = ListCache({"movies": (lambda: f"m{self.version}", build)})
        self.c = TestClient(server.app)

    def test_gzip_body_etag_and_304_without_building(self):
        r = self.c.get("/tv/api/movies")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-encoding"], "gzip")
        self.assertEqual(r.headers["etag"], '"m7"')
        self.assertEqual(r.json()["items"][0][1], "Film")
        r = self.c.get("/tv/api/movies", headers={"If-None-Match": '"m7"'})
        self.assertEqual((r.status_code, self.builds), (304, 1))
        self.version = "8"                                             # catalogue refreshed
        r = self.c.get("/tv/api/movies", headers={"If-None-Match": '"m7"'})
        self.assertEqual((r.status_code, r.headers["etag"], self.builds), (200, '"m8"', 2))

    def test_client_without_gzip_gets_plain_json(self):
        r = self.c.get("/tv/api/movies", headers={"Accept-Encoding": "identity"})
        self.assertNotIn("content-encoding", r.headers)
        self.assertEqual(json.loads(r.content)["v"], "7")


if __name__ == "__main__":
    unittest.main()
