"""The normalised in-memory catalogue (+ persisted copy for instant restarts).

Only items in here can ever be played or have their artwork fetched: the browser
sends opaque IDs, never URLs.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Iterator

from . import providers, refresh_guard
from .epg import Guide, parse_xmltv
from .util import log, redact, write_json

SERIES_TTL = 6 * 3600
INFO_TTL = 24 * 3600
RETRY_FIRST = 1800                 # after a failed/rejected scheduled refresh: 30 min, then 1 h, 2 h... up to the normal interval


def img_key(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:20] if url else ""


class Catalogue:
    def __init__(self, settings, http):
        self.s = settings
        self.http = http
        self.provider = providers.make_provider(settings)
        self.path = settings.state_dir / "catalogue.json"
        self.extra_path = settings.state_dir / "details.json"
        self.data = providers.empty()
        self.by_id: dict[str, dict] = {}
        self.images: dict[str, str] = {}
        self.version = "0"
        self.updated = 0.0
        self.error = ""
        self.refreshing = False
        self.guide = Guide(settings.state_dir)
        self.guide_refreshing = False
        # lazily fetched details survive refreshes: {"movie": {id: (ts, info)}, "series": {id: (ts, meta, [eps])}}
        self.details = {"movie": {}, "series": {}}
        self._lock = asyncio.Lock()
        self._details_save = asyncio.Lock()   # one details save at a time (they share the .tmp file)
        self._series_locks: dict[str, asyncio.Lock] = {}
        self.history = refresh_guard.History(settings.state_dir / "refresh-history.json")
        self.accept_next = False       # `iptv refresh --accept-changes`: take the next refresh even if it looks wrong
        self.next_try = 0.0            # scheduled refreshes wait until then after a failure (backoff)
        self.on_event = None           # callback(kind, history entry): "accepted" | "rejected" | "failed"

    # ---------------------------------------------------------------- state
    def configure(self, settings) -> None:
        self.s = settings
        self.provider = providers.make_provider(settings)
        redact.set_secrets([settings.xtream_user, settings.xtream_pass, settings.m3u_url,
                            settings.epg_url, settings.xtream_base])

    def _index(self) -> None:
        self.by_id, self.images = self._build_index(self.data, list(self.details["series"].values()),
                                                    list(self.details["movie"].values()))
        self._set_version()

    def _set_version(self) -> None:
        self.version = hashlib.sha1(f"{self.updated}:{len(self.by_id)}".encode()).hexdigest()[:12]

    @staticmethod
    def _build_index(data: dict, series_details: list, movie_details: list) -> tuple[dict, dict]:
        """id -> item and artwork key -> URL. Pure, so a refresh can run it in a worker thread
        (the details are passed as snapshots: requests may add to them meanwhile)."""
        by_id, images = {}, {}
        for kind in ("live", "movie", "episode"):
            for it in data[kind]:
                by_id[it["id"]] = it
                if it.get("img"):
                    images[img_key(it["img"])] = it["img"]
        for s in data["series"]:
            by_id[s["id"]] = s
            for k in ("img", "backdrop"):
                if s.get(k):
                    images[img_key(s[k])] = s[k]
        Catalogue._add_details(by_id, images, series_details, movie_details)
        return by_id, images

    @staticmethod
    def _add_details(by_id: dict, images: dict, series_details, movie_details) -> None:
        """Episodes and extra artwork from titles opened so far (a few hundred at most)."""
        for _ts, meta, eps in series_details:
            if meta.get("backdrop"):
                images[img_key(meta["backdrop"])] = meta["backdrop"]
            for e in eps:
                by_id.setdefault(e["id"], e)
                if e.get("img"):
                    images[img_key(e["img"])] = e["img"]
        for _ts, info in movie_details:
            for k in ("backdrop", "img2"):
                if info.get(k):
                    images[img_key(info[k])] = info[k]

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text())
            if raw.get("provider") == self._provider_fingerprint():
                self.data, self.updated = raw["data"], raw.get("updated", 0)
            else:
                log.info("saved catalogue belongs to a different provider config; ignoring it")
        except (FileNotFoundError, ValueError, KeyError):
            pass
        try:
            raw = json.loads(self.extra_path.read_text())
            if raw.get("provider") == self._provider_fingerprint():
                self.details = raw["details"]
        except (FileNotFoundError, ValueError, KeyError):
            pass
        self._index()
        self.guide.load()

    def _provider_fingerprint(self) -> str:
        s = self.s
        src = f"{s.provider_type}|{s.m3u_url}|{s.xtream_base}|{s.xtream_user}"
        return hashlib.sha256(src.encode()).hexdigest()[:16]

    def _write(self, path: Path, obj) -> None:
        tmp = path.with_suffix(".tmp")
        with tmp.open("w") as fh:
            write_json(fh, obj)                     # in slices: never holds the interpreter lock for long
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    async def save_details(self) -> None:
        """Write the lazily fetched details. The snapshot is taken here, on the event loop, so other
        requests can keep adding details while the file is written in a thread."""
        async with self._details_save:
            snap = {"movie": dict(self.details["movie"]), "series": dict(self.details["series"])}
            await asyncio.to_thread(self._write, self.extra_path, {"provider": self._provider_fingerprint(), "details": snap})

    # -------------------------------------------------------------- refresh
    async def _record(self, status: str, reason: str, report=None, error: str = "", seconds: float = 0.0) -> dict:
        entry = await asyncio.to_thread(self.history.record, status, reason, report, error, seconds)
        if status == "accepted":
            self.next_try = 0.0
        else:
            streak = self.history.failing_streak()
            self.next_try = time.time() + min(RETRY_FIRST * 2 ** (streak - 1), self.s.refresh_hours * 3600)
        if self.on_event:
            try:
                self.on_event(status, entry)
            except Exception as e:  # noqa: BLE001 - reporting must never break a refresh
                log.warning("refresh event handler failed: %s", redact(e))
        return entry

    async def refresh(self, reason: str = "") -> bool:
        if self._lock.locked():
            return False
        async with self._lock:
            self.refreshing = True
            t0 = time.time()
            try:
                if not self.s.configured:
                    raise providers.ProviderError("no provider configured (see `iptv config`)")
                data = await self.provider.fetch(self.http)
                report = await asyncio.to_thread(refresh_guard.compare, self.data, data,
                                                 getattr(self.provider, "stats", {}))
                if report["status"] == "rejected":
                    if not self.accept_next:
                        why = "; ".join(report["anomalies"])
                        self.error = f"refresh rejected, previous catalogue kept: {why}"
                        log.warning("catalogue refresh (%s) REJECTED - %s. Still serving the previous catalogue; "
                                    "the next scheduled refresh will try again (iptv refresh --accept-changes to "
                                    "take it anyway).", reason or "manual", why)
                        await self._record("rejected", reason, report, seconds=time.time() - t0)
                        return False
                    log.warning("catalogue refresh accepted despite: %s (override)", "; ".join(report["anomalies"]))
                    report["warnings"] = report["anomalies"] + report["warnings"]
                    report["anomalies"] = []
                self.accept_next = False
                by_id, images = await asyncio.to_thread(self._build_index, data, list(self.details["series"].values()),
                                                        list(self.details["movie"].values()))
                # titles opened while that ran: add their episodes too (cheap, and on the loop)
                self._add_details(by_id, images, self.details["series"].values(), self.details["movie"].values())
                self.data, self.by_id, self.images = data, by_id, images
                self.updated, self.error = time.time(), ""
                self._set_version()
                await asyncio.to_thread(self._write, self.path,
                                        {"provider": self._provider_fingerprint(), "updated": self.updated, "data": data})
                entry = await self._record("accepted", reason, report, seconds=time.time() - t0)
                log.info("catalogue refreshed (%s): %d live, %d movies, %d series, %d episodes in %.1fs; changes: %s%s",
                         reason or "manual", len(data["live"]), len(data["movie"]), len(data["series"]),
                         len(data["episode"]), time.time() - t0, refresh_guard.change_line(entry),
                         "; warnings: " + "; ".join(report["warnings"]) if report["warnings"] else "")
                return True
            except providers.ProviderError as e:
                self.error = str(e)
                log.warning("catalogue refresh failed: %s", self.error)
                await self._record("failed", reason, error=self.error, seconds=time.time() - t0)
            except Exception as e:  # noqa: BLE001 - keep serving the old catalogue
                self.error = f"refresh failed ({type(e).__name__})"
                log.exception("catalogue refresh crashed: %s", redact(e))
                await self._record("failed", reason, error=self.error, seconds=time.time() - t0)
            finally:
                self.refreshing = False
            return False

    async def refresh_guide(self) -> bool:
        url = self.provider.epg_url()
        if not url or self.guide_refreshing:
            if not url:
                self.guide.error = "no EPG URL configured"
            return False
        self.guide_refreshing = True
        path = None
        t0 = time.time()
        try:
            path = await providers._download(self.http, url, {"User-Agent": self.s.user_agent}, "epg")
            data = await asyncio.to_thread(parse_xmltv, path, self.data["live"])
            self.guide.data, self.guide.updated, self.guide.error = data, time.time(), ""
            self.guide.channels = len(data)
            await asyncio.to_thread(self.guide.save)
            log.info("epg refreshed: %d channels matched in %.1fs", len(data), time.time() - t0)
            return True
        except providers.ProviderError as e:
            self.guide.error = str(e)
            log.warning("epg refresh failed: %s", e)
        except Exception as e:  # noqa: BLE001
            self.guide.error = f"epg failed ({type(e).__name__})"
            log.warning("epg refresh failed: %s", redact(e))
        finally:
            self.guide_refreshing = False
            if path:
                path.unlink(missing_ok=True)
        return False

    # -------------------------------------------------------------- details
    async def movie_details(self, item: dict) -> dict:
        cached = self.details["movie"].get(item["id"])
        if cached and time.time() - cached[0] < INFO_TTL:
            return cached[1]
        try:
            info = await self.provider.movie_info(self.http, item)
        except providers.ProviderError as e:
            log.info("movie info unavailable: %s", e)
            info = cached[1] if cached else {}
        self.details["movie"][item["id"]] = (time.time(), info)
        for k in ("backdrop", "img2"):
            if info.get(k):
                self.images[img_key(info[k])] = info[k]
        await self.save_details()
        return info

    async def series_details(self, series: dict) -> tuple[dict, list]:
        if series.get("loaded"):
            eps = [e for e in self.data["episode"] if e["series"] == series["id"]]
            return {}, eps
        lock = self._series_locks.setdefault(series["id"], asyncio.Lock())
        async with lock:
            cached = self.details["series"].get(series["id"])
            if cached and time.time() - cached[0] < SERIES_TTL:
                return cached[1], cached[2]
            try:
                meta, eps = await self.provider.series_info(self.http, series)
            except providers.ProviderError as e:
                if cached:
                    log.info("series info refresh failed, using cached copy: %s", e)
                    return cached[1], cached[2]
                raise
            self.details["series"][series["id"]] = (time.time(), meta, eps)
            for e in eps:
                self.by_id[e["id"]] = e
                if e.get("img"):
                    self.images[img_key(e["img"])] = e["img"]
            if meta.get("backdrop"):
                self.images[img_key(meta["backdrop"])] = meta["backdrop"]
            await self.save_details()
            return meta, eps

    def forget_locks(self) -> None:
        """Per-series locks are only needed while a fetch runs; drop the idle ones."""
        for key in [k for k, lock in self._series_locks.items() if not lock.locked()]:
            del self._series_locks[key]

    async def resolve(self, iid: str, series_hint: str = "") -> dict | None:
        """Find a playable item; for an episode of a not-yet-loaded series, load that series first."""
        it = self.by_id.get(iid)
        if it or not iid.startswith("e_") or not series_hint:
            return it
        ser = self.by_id.get(series_hint)
        if ser and ser.get("name") is not None and "loaded" in ser:
            try:
                await self.series_details(ser)
            except providers.ProviderError:
                return None
        return self.by_id.get(iid)

    # ------------------------------------------------------ public payloads
    def public_list(self, kind: str) -> tuple[dict, Iterator[list]]:
        """The browser's copy of one list ("live", "movies", "series"): a head {"v", "cats"} and
        compact item rows. Built once per version and cached gzipped (tvapp/lists.py)."""
        data = self.data                            # one consistent snapshot, even if a refresh lands meanwhile
        if kind == "live":
            guide = self.guide.data
            rows = ([i["id"], i["name"], i["cat"], img_key(i.get("img", "")), i.get("num", 0),
                     1 if i["id"] in guide else 0] for i in data["live"])
            cats = data["cats"]["live"]
        elif kind == "movies":
            rows = ([i["id"], i["name"], i["cat"], img_key(i.get("img", "")), i.get("year", 0),
                     i.get("rating", 0), i.get("added", 0)] for i in data["movie"])
            cats = data["cats"]["movie"]
        elif kind == "series":
            rows = ([s["id"], s["name"], s["cat"], img_key(s.get("img", "")), s.get("year", 0),
                     s.get("rating", 0)] for s in data["series"])
            cats = data["cats"]["series"]
        else:
            raise ValueError(kind)
        return {"v": self.version, "cats": [[k, v] for k, v in cats.items()]}, rows

    @staticmethod
    def public_episode(e: dict) -> dict:
        return {"id": e["id"], "series": e["series"], "season": e.get("season", 1), "episode": e.get("episode", 0),
                "name": e.get("name", ""), "plot": e.get("plot", ""), "dur": e.get("dur", 0),
                "img": img_key(e.get("img", ""))}

    def status(self) -> dict:
        d = self.data
        return {
            "provider": self.provider.kind, "configured": self.s.configured,
            "updated": int(self.updated), "refreshing": self.refreshing, "error": self.error,
            "counts": {"live": len(d["live"]), "movies": len(d["movie"]), "series": len(d["series"]),
                       "episodes": len(d["episode"])},
            "epg": {"updated": int(self.guide.updated), "channels": self.guide.channels,
                    "error": self.guide.error, "refreshing": self.guide_refreshing},
            "version": self.version,
            "refresh": {**self.history.summary(), "next_try": int(self.next_try), "accept_next": self.accept_next},
        }
