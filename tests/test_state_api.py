"""HTTP layer of the synced state: write protection, sync round trips, export/import.

Uses the real FastAPI app with only the state store wired up (no provider, no ffmpeg).
"""
import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from tvapp import config as cfg
from tvapp import server
from tvapp.userstate import StateStore, now_ms

SECRETS = ("http://provider.example:8080", "secret-user", "secret-pass", "http://epg.example/x.xml")
WRITE = {"X-TV-Sync": "1", "Content-Type": "application/json"}


def settings(tmp: Path) -> cfg.Settings:
    return cfg.Settings(provider_type="xtream", m3u_url="", epg_url=SECRETS[3], xtream_base=SECRETS[0],
                        xtream_user=SECRETS[1], xtream_pass=SECRETS[2], user_agent="t", refresh_hours=12,
                        epg_refresh_hours=12, max_live=2, max_vod=2, state_dir=tmp, cache_dir=tmp, live_dir=tmp,
                        vod_dir=tmp, web_dir=tmp, host="127.0.0.1", port=0, public_origin="https://example.com")


class StateApi(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        server.S.settings = settings(Path(self.tmp.name))
        server.S.state = StateStore(Path(self.tmp.name) / "state.db")
        self.c = TestClient(server.app)          # no lifespan: nothing else starts

    def tearDown(self):
        server.S.state.close()
        self.tmp.cleanup()

    def post(self, body, headers=WRITE, path="/tv/api/state"):
        return self.c.post(path, content=body if isinstance(body, (str, bytes)) else json.dumps(body), headers=headers)

    def change(self, key="l_a", at=None, **value):
        return {"ns": "fav", "key": key, "value": {"name": "BBC One", "kind": "live", **value},
                "updated": at or now_ms()}

    def test_get_empty_state(self):
        r = self.c.get("/tv/api/state")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual((body["rev"], body["changes"]), (0, []))
        self.assertTrue(body["db"])
        self.assertEqual(r.headers["cache-control"], "no-store")

    def test_writes_need_sync_header_json_and_same_site_origin(self):
        body = {"since": 0, "changes": [self.change()]}
        for headers in ({"Content-Type": "application/json"},                     # no custom header
                        {"X-TV-Sync": "1", "Content-Type": "text/plain"},         # form-able content type
                        {**WRITE, "Origin": "https://evil.example"},
                        {**WRITE, "Origin": "null"}):
            self.assertEqual(self.post(body, headers).status_code, 403, headers)
        self.assertEqual(server.S.state.changes_since(0)["rev"], 0)
        for headers in (WRITE, {**WRITE, "Origin": "https://example.com"},
                        {**WRITE, "Origin": "http://testserver"}):                # direct / SSH-tunnel access
            self.assertEqual(self.post(body, headers).status_code, 200, headers)

    def test_post_applies_and_returns_changes_since(self):
        first = self.post({"since": 0, "device": "abcdef012345", "changes": [self.change("l_a")]}).json()
        self.assertEqual((first["applied"], first["rev"]), (1, 1))
        self.assertEqual([c["key"] for c in first["changes"]], ["l_a"])
        second = self.post({"since": 1, "changes": [self.change("l_b")]}).json()
        self.assertEqual([c["key"] for c in second["changes"]], ["l_b"])
        self.assertEqual(len(self.c.get("/tv/api/state?since=0").json()["changes"]), 2)

    def test_stale_write_is_not_applied_and_newer_row_comes_back(self):
        self.post({"since": 0, "changes": [self.change("l_a", at=now_ms(), name="New")]})
        r = self.post({"since": 0, "changes": [self.change("l_a", at=1000, name="Old")]}).json()
        self.assertEqual(r["applied"], 0)
        self.assertEqual(r["changes"][0]["value"]["name"], "New")

    def test_bad_requests(self):
        self.assertEqual(self.post("{not json").status_code, 400)
        self.assertEqual(self.post([1, 2]).status_code, 400)
        self.assertEqual(self.post({"changes": "x"}).status_code, 400)
        self.assertEqual(self.post(b"x" * (server.STATE_MAX_BODY + 1)).status_code, 400)
        self.assertEqual(self.c.get("/tv/api/state?since=abc").status_code, 200)

    def test_export_is_an_attachment_without_provider_secrets(self):
        self.post({"since": 0, "changes": [self.change("l_a"), {"ns": "pref", "key": "liveMode", "value": "fav",
                                                                  "updated": now_ms()}]})
        r = self.c.get("/tv/api/state/export")
        self.assertEqual(r.status_code, 200)
        self.assertIn("attachment", r.headers["content-disposition"])
        for s in SECRETS:
            self.assertNotIn(s, r.text)
        doc = r.json()
        self.assertEqual(doc["provider_type"], "xtream")
        self.assertEqual(doc["favourites"][0]["name"], "BBC One")

    def test_import_merge_and_replace(self):
        self.post({"since": 0, "changes": [self.change("l_a")]})
        doc = self.c.get("/tv/api/state/export").json()
        doc["favourites"].append({"key": "l_b", "updated": doc["exported_at"], "name": "ITV", "kind": "live"})
        r = self.post(doc, path="/tv/api/state/import?mode=merge")
        self.assertEqual(r.json()["applied"], 1)
        doc["favourites"] = doc["favourites"][1:]                                # only l_b
        r = self.post(doc, path="/tv/api/state/import?mode=replace")
        self.assertEqual(r.json()["removed"], 1)
        live = [c["key"] for c in self.c.get("/tv/api/state").json()["changes"] if not c["deleted"]]
        self.assertEqual(live, ["l_b"])
        self.assertEqual(self.post(doc, path="/tv/api/state/import?mode=wipe").status_code, 400)
        self.assertEqual(self.post({"hello": 1}, path="/tv/api/state/import").status_code, 400)
        self.assertEqual(self.post(doc, headers={"Content-Type": "application/json"},
                                   path="/tv/api/state/import").status_code, 403)

    def test_other_api_routes_still_reject_post(self):
        self.assertEqual(self.c.post("/tv/api/refresh", headers=WRITE).status_code, 405)


if __name__ == "__main__":
    unittest.main()
