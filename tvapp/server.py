"""HTTP layer (FastAPI). Everything is mounted under /tv; nginx forwards the path unchanged.

/tv/                      app shell (nginx: Basic Auth or session cookie)
/tv/static/...            JS/CSS/vendor (same protection)
/tv/pwa/...               manifest + icons (public: iOS fetches them without credentials)
/tv/api/...               JSON API (same protection as the shell)
/tv/m/<token>/...         media; authorised by the signed, expiring token in the path

/tv/api/state is the only route that accepts POST (nginx allows it there and nowhere
else under /tv); writes also need a JSON body, the X-TV-Sync header and a same-site Origin.
"""
from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import gzip
import json
import logging
import os
import re
import secrets
import signal
import time
import zlib
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from . import __version__
from . import config as cfg
from .auth import CLIENT_RE, COOKIE, COOKIE_DAYS, Keys
from .catalog import Catalogue, img_key
from .lists import ListCache, encode_gz
from . import alerts, epgsearch, hls, livemeta, memguard, refresh_guard, sports, sportsalerts, tracks
from .health import ProviderHealth
from .providers import ProviderError
from .streams import Engine
from .userstate import StateError, StateStore
from .util import log, redact, valid_id

IMG_KEY = re.compile(r"^[0-9a-f]{20}$")
JOB_RE = re.compile(r"^[0-9a-f]{12}$")
# index.m3u8 + seg_N: normal remux / live. With a subtitle (one HLS output with a WebVTT
# rendition): master.m3u8, index_0.m3u8 + seg_0_N (video/audio), index_0_vtt.m3u8 + index_0N.vtt.
HLS_NAME = re.compile(r"^(index\.m3u8|master\.m3u8|index_0\.m3u8|index_0_vtt\.m3u8|index_0\d{1,7}\.vtt|init(_\d{1,3})?\.mp4|"
                      r"seg_(0_)?\d{5,7}\.(ts|m4s))$")
RANGE_RE = re.compile(r"^bytes=\d*-\d*$")
DEVICE_RE = re.compile(r"^[0-9a-f]{12,32}$")
STATE_MAX_BODY = 1024 * 1024
LIST_KINDS = ("live", "movies", "series")
EPG_NOW_SECONDS = 30                 # now/next for all channels is rebuilt at most this often
IMG_MAX = 4 * 1024 * 1024
IMG_CACHE_MAX = 400 * 1024 * 1024
STATIC_TYPES = {".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".png": "image/png",
                ".webmanifest": "application/manifest+json", ".json": "application/json", ".ico": "image/x-icon"}
MEDIA_TYPES = {".m3u8": "application/vnd.apple.mpegurl", ".ts": "video/mp2t", ".m4s": "video/iso.segment",
               ".mp4": "video/mp4", ".vtt": "text/vtt; charset=utf-8"}


class State:
    settings: cfg.Settings
    http: httpx.AsyncClient
    cat: Catalogue
    engine: Engine
    keys: Keys
    state: StateStore
    lists: ListCache
    guard: memguard.MemoryGuard
    alerts: alerts.Alerts
    health: ProviderHealth
    live_meta: livemeta.MetaCache
    vindex: livemeta.VariantIndex | None = None
    vindex_version: str = ""
    epg_index: epgsearch.Index | None = None
    epg_index_key: tuple = ()
    epg_index_task: asyncio.Task | None = None
    sports_cache: dict
    sports_alerts: sportsalerts.SportsAlerts
    img_fail: dict
    img_sem: asyncio.Semaphore
    range_ok: dict
    last_manual: float = 0.0
    started: float = 0.0
    tasks: set


S = State()
try:
    _libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6")
    _libc.malloc_trim                               # glibc only (not macOS / musl)
except (OSError, AttributeError):
    _libc = None


# ------------------------------------------------------------------ helpers
def jresp(request: Request, obj, status: int = 200, etag: str = "", cache: str = "no-store") -> Response:
    if etag and request.headers.get("if-none-match") == f'"{etag}"':
        return Response(status_code=304, headers={"ETag": f'"{etag}"', "Cache-Control": cache})
    body = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode()
    headers = {"Cache-Control": cache, "Vary": "Accept-Encoding"}
    if etag:
        headers["ETag"] = f'"{etag}"'
    if len(body) > 1400 and "gzip" in request.headers.get("accept-encoding", ""):
        body = gzip.compress(body, 5)
        headers["Content-Encoding"] = "gzip"
    return Response(body, status_code=status, media_type="application/json", headers=headers)


def err(request: Request, status: int, message: str) -> Response:
    return jresp(request, {"error": message}, status)


def client_id(request: Request) -> str | None:
    c = request.query_params.get("c", "")
    return c if CLIENT_RE.match(c) else None


def background(coro) -> asyncio.Task:
    """create_task that keeps a reference until the task finishes (the loop itself only holds a weak one)."""
    task = asyncio.get_running_loop().create_task(coro)
    S.tasks.add(task)
    task.add_done_callback(S.tasks.discard)
    return task


def release_memory() -> None:
    """Hand memory freed by a refresh back to the system. A refresh briefly holds the old and the
    new catalogue; glibc keeps most of that once freed, so the process stayed ~250 MB larger
    after every refresh (measured 858 -> 1099 MB) without this. (No gc.collect(): a full
    collection over millions of objects holds the interpreter lock for about a second.)"""
    if _libc is not None:
        _libc.malloc_trim(0)


async def refresh_all(reason: str) -> None:
    ok = await S.cat.refresh(reason)
    if ok or not S.cat.guide.updated or time.time() - S.cat.guide.updated > S.settings.epg_refresh_hours * 3600:
        await S.cat.refresh_guide()
    await S.lists.warm(LIST_KINDS)
    await asyncio.to_thread(release_memory)
    await epg_index()


def epg_now_gz() -> bytes:
    """Now/next for every channel with a guide (runs in a worker thread via ListCache)."""
    now = time.time()
    guide = S.cat.guide
    out = {}
    for iid in list(guide.data):
        nn = guide.now_next(iid, now)
        if nn:
            out[iid] = nn
    return gzip.compress(json.dumps({"t": int(now), "guide": out}, separators=(",", ":"),
                                    ensure_ascii=False).encode(), 6)


def make_lists() -> ListCache:
    return ListCache({
        "live": (lambda: f"l{S.cat.version}{int(S.cat.guide.updated)}", lambda: encode_gz(*S.cat.public_list("live"))),
        "movies": (lambda: f"m{S.cat.version}", lambda: encode_gz(*S.cat.public_list("movies"))),
        "series": (lambda: f"s{S.cat.version}", lambda: encode_gz(*S.cat.public_list("series"))),
        "epg-now": (lambda: f"e{int(S.cat.guide.updated)}.{int(time.time() // EPG_NOW_SECONDS)}", epg_now_gz),
    }, quiet=("epg-now",))


async def scheduler() -> None:
    await refresh_all("startup")
    while True:
        await asyncio.sleep(600)
        now = time.time()
        try:
            if now - S.cat.updated > S.settings.refresh_hours * 3600 and now >= S.cat.next_try:
                await S.cat.refresh("scheduled")
            if now - S.cat.guide.updated > S.settings.epg_refresh_hours * 3600:
                await S.cat.refresh_guide()
            if S.lists.warm_needed(LIST_KINDS):         # a scheduled refresh changed something
                await S.lists.warm(LIST_KINDS)
                await asyncio.to_thread(release_memory)
            await asyncio.to_thread(prune_images)
            await epg_index()                           # rebuilt in the background when stale
            forget_old(now)
            await asyncio.to_thread(S.state.maintain)
        except Exception as e:  # noqa: BLE001
            log.warning("scheduler: %s", redact(e))


def forget_old(now: float) -> None:
    """Drop expired entries from the small in-memory lookups so they cannot grow without bound."""
    for key in [k for k, t in S.img_fail.items() if now - t > 3600]:
        del S.img_fail[key]
    if len(S.range_ok) > 5000:
        S.range_ok.clear()                          # re-probed (one 2-byte request) when next played
    S.engine.forget_old(now)
    S.cat.forget_locks()


def memory_usage() -> dict:
    """Resident memory of this process and of each running ffmpeg job (MB)."""
    jobs = {j.id: round(memguard.rss_mb(j.proc.pid))
            for j in list(S.engine.jobs.values()) if j.proc and j.proc.returncode is None}
    app = memguard.rss_mb()
    return {"app_mb": round(app), "ffmpeg_mb": sum(jobs.values()), "total_mb": round(app + sum(jobs.values())),
            "jobs": jobs}


def memory_details(usage: dict) -> dict:
    """What was going on, for diagnosing a guard event later."""
    c = S.cat
    return {**usage, "refreshing": c.refreshing or c.guide_refreshing,
            "catalogue": {k: len(c.data[k]) for k in ("live", "movie", "series", "episode")},
            "by_id": len(c.by_id), "images": len(c.images), "details": {k: len(v) for k, v in c.details.items()},
            "guide_channels": len(c.guide.data), "list_cache_kb": {k: v >> 10 for k, v in S.lists.sizes().items()},
            "epg_search": S.epg_index.stats() if S.epg_index else None,
            "img_fail": len(S.img_fail), "range_ok": len(S.range_ok), "probes": len(S.engine.probes),
            "tasks": len(S.tasks), "uptime_s": int(time.time() - S.started)}


async def sports_alert_loop() -> None:
    """Favourite match alerts (tvapp/sportsalerts.py): one cheap check a minute against the
    current guide index; nothing to do while alerts are off or there are no favourites."""
    while True:
        await asyncio.sleep(sportsalerts.CHECK_EVERY)
        try:
            await asyncio.to_thread(S.sports_alerts.check, S.epg_index, event_broadcasters)
        except Exception as e:  # noqa: BLE001 - the loop itself must keep running
            log.warning("sports alert check failed: %s", redact(e))


def event_broadcasters(ev: dict) -> int:
    """Channels showing an event, versions of one channel counted once (as the Sports cards)."""
    return len(epgsearch.channel_groups([i for i in ev["ids"] if i in S.cat.by_id], variant_index(),
                                        S.live_meta.get, livemeta.parse_caps("")))


async def memory_watch() -> None:
    """TV-only memory safety net (rules in tvapp/memguard.py)."""
    while True:
        await asyncio.sleep(memguard.CHECK_SECONDS)
        try:
            usage = memory_usage()
            action, reason = S.guard.observe(usage["total_mb"])
            if action == "suppressed":
                event = S.guard.record(action, reason, memory_details(usage))
                log.critical("MEMORY GUARD: %s - NOT restarting: %d restarts in the last %dh already. Details: %s",
                             reason, S.guard.recent_restarts(), memguard.WINDOW_SECONDS // 3600, json.dumps(event))
                await alert("memory-guard", "Memory guard: too many restarts, NOT restarting",
                            f"{reason}. {S.guard.recent_restarts()} restarts in the last "
                            f"{memguard.WINDOW_SECONDS // 3600} h already. Check: iptv status / iptv logs 100")
            elif action == "restart":
                event = S.guard.record(action, reason, memory_details(usage))
                log.error("MEMORY GUARD: %s - restarting the TV service (systemd brings it back in ~5 s). "
                          "Details: %s", reason, json.dumps(event))
                await alert("memory-guard", "Memory guard restarted the TV service",
                            f"{reason}. Streams were stopped; the service is back in ~5 s. "
                            f"(journalctl -u signal-tv-classic | grep 'MEMORY GUARD')")
                await restart_for_memory()
        except Exception as e:  # noqa: BLE001 - the watcher itself must keep running
            log.warning("memory guard check failed: %s", redact(e))


async def restart_for_memory() -> None:
    """Stop ffmpeg jobs cleanly, then exit non-zero so systemd (Restart=on-failure) restarts us."""
    try:
        await asyncio.wait_for(S.engine.shutdown(), 10)
        S.state.close()
    finally:
        logging.shutdown()
        os._exit(memguard.EXIT_CODE)


async def alert(key: str, title: str, detail: str = "", resolve: bool = False) -> None:
    """Telegram alert with dedup/backoff (tvapp/alerts.py); never raises, never waits > 12 s."""
    try:
        fn = S.alerts.resolve if resolve else S.alerts.raise_
        args = (key, detail) if resolve else (key, title, detail)
        await asyncio.wait_for(asyncio.to_thread(fn, *args), 12)
    except Exception as e:  # noqa: BLE001
        log.warning("alert %s: %s", key, redact(e))


def refresh_event(kind: str, entry: dict) -> None:
    """Called by the catalogue after each refresh (accepted / rejected / failed)."""
    if kind == "rejected":
        wait = max(0, int((S.cat.next_try - time.time()) // 60))
        background(alert("refresh-rejected", "Catalogue refresh rejected - previous catalogue kept",
                         "; ".join(entry.get("anomalies", [])) +
                         f". Next try in ~{wait} min. If the change is real: iptv refresh --accept-changes"))
    elif kind == "failed":
        streak = S.cat.history.failing_streak()
        if streak >= 3:
            background(alert("refresh-failing", f"Catalogue refresh failed {streak} times in a row",
                             f"{entry.get('error', '')}. Still serving the catalogue from "
                             f"{time.strftime('%d %b %H:%M', time.localtime(S.cat.updated)) if S.cat.updated else 'never'}."))
    elif kind == "accepted":
        background(alert("refresh-rejected", "", f"Refresh OK: {refresh_guard.change_line(entry)}", resolve=True))
        background(alert("refresh-failing", "", f"Refresh OK: {refresh_guard.change_line(entry)}", resolve=True))


def stream_outcome(ok: bool, item_id: str, message: str = "") -> None:
    change = S.health.record(ok, item_id, message)
    if change == "down":
        background(alert("provider-streams", "Provider keeps refusing streams",
                         f"Last error: {message}. {S.health.summary()['failures_1h']} failed starts in the last hour, "
                         f"none working. Possible causes: provider outage, account/connection limit."))
    elif change == "up":
        background(alert("provider-streams", "", "Streams are starting again.", resolve=True))


def health_report() -> dict:
    """Overall health for `iptv status` and the Library status card."""
    now = time.time()
    problems = []
    st = S.cat.status()
    r = st["refresh"]
    if r["last"] and r["last"]["status"] != "accepted":
        problems.append(f"catalogue refresh {r['last']['status']}: " +
                        ("; ".join(r["last"].get("anomalies") or []) or r["last"].get("error", "")))
    if S.cat.updated and now - S.cat.updated > 2 * S.settings.refresh_hours * 3600 + 3600:
        problems.append(f"catalogue is {int((now - S.cat.updated) // 3600)} h old")
    if S.health.down:
        problems.append("provider keeps refusing streams")
    g = S.guard.summary()
    ev = g.get("last_event")
    if ev and now - ev["at"] < 86400:
        problems.append(f"memory guard {ev['action']} {int((now - ev['at']) // 3600)} h ago")
    for p in S.alerts.open_problems():
        if p["key"].startswith("unit:"):
            problems.append(p["title"].lower())
    return {"ok": not problems, "problems": problems, "alerts": S.alerts.summary(), "streams": S.health.summary(),
            "sports_alerts": dict(getattr(S, "sports_alerts", None).last) if getattr(S, "sports_alerts", None) else None}


def reload_config() -> None:
    log.info("SIGHUP: re-reading %s and refreshing", cfg.CONFIG_PATH)
    S.settings = cfg.load()
    S.cat.configure(S.settings)
    S.engine.s = S.settings
    S.keys.reload()
    S.range_ok.clear()
    background(refresh_all("reload"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    S.settings = cfg.load()
    for d in (S.settings.state_dir, S.settings.cache_dir, S.settings.cache_dir / "img"):
        d.mkdir(parents=True, exist_ok=True)
    S.http = httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(15, read=60),
                               limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
                               headers={"Accept-Encoding": "identity"})
    S.cat = Catalogue(S.settings, S.http)
    S.cat.configure(S.settings)
    S.cat.load()
    S.engine = Engine(S.settings)
    S.engine.prepare()
    S.keys = Keys(S.settings.state_dir)
    S.state = StateStore(S.settings.state_dir / "state.db")
    S.img_fail, S.img_sem, S.range_ok = {}, asyncio.Semaphore(6), {}
    S.tasks = set()
    S.sports_cache = {}
    S.started = time.time()
    S.guard = memguard.MemoryGuard(S.settings.memory_limit_mb, S.settings.state_dir / "memory-guard.json")
    S.alerts = alerts.Alerts(S.settings.state_dir / "alerts.json")
    S.sports_alerts = sportsalerts.SportsAlerts(S.state, S.alerts)
    S.health = ProviderHealth()
    S.cat.on_event = refresh_event
    S.live_meta = livemeta.MetaCache(S.settings.state_dir / "live-meta.json")
    S.engine.on_meta = learned_meta
    background(memory_watch())
    S.lists = make_lists()
    background(S.lists.warm(LIST_KINDS))           # the saved catalogue, while the startup refresh runs
    background(scheduler())
    background(sports_alert_loop())
    asyncio.get_running_loop().add_signal_handler(signal.SIGHUP, reload_config)
    log.info("signal-tv-classic %s listening on %s:%d (provider=%s, configured=%s)", __version__,
             S.settings.host, S.settings.port, S.settings.provider_type, S.settings.configured)
    try:
        yield
    finally:
        for t in list(S.tasks):
            t.cancel()
        await S.engine.shutdown()
        await S.http.aclose()
        S.state.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def session_cookie(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/tv/m/") or path.startswith("/tv/pwa/"):
        return response
    # nginx sets this header (from $remote_user) only when Basic Auth succeeded.
    if request.headers.get("x-tv-auth-user"):
        ok, exp = S.keys.cookie_ok(request.cookies.get(COOKIE))
        if not ok or exp - time.time() < 7 * 86400:
            response.set_cookie(COOKIE, S.keys.make_cookie(), max_age=COOKIE_DAYS * 86400, path="/tv/",
                                secure=True, httponly=True, samesite="lax")
    return response


# ------------------------------------------------------------------- shell
# Same policy nginx sends (snippets/signal-tv-classic-headers.conf); repeated here so a
# direct localhost/SSH-tunnel session behaves exactly like the public one.
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
       "media-src 'self' blob:; worker-src 'self' blob:; connect-src 'self'; manifest-src 'self'; font-src 'self'; "
       "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


@app.get("/tv/")
async def shell():
    return FileResponse(S.settings.web_dir / "index.html", media_type="text/html; charset=utf-8",
                        headers={"Cache-Control": "no-store", "Content-Security-Policy": CSP,
                                 "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"})


# ------------------------------------------------------------------- auth
@app.get("/tv/api/auth-check")
async def auth_check(request: Request):
    ok, _ = S.keys.cookie_ok(request.cookies.get(COOKIE))
    return Response(status_code=204 if ok else 401)


# -------------------------------------------------------------- catalogue
@app.get("/tv/api/status")
async def status(request: Request):
    st = S.cat.status()
    st["jobs"] = S.engine.stats()
    st["app"] = __version__
    st["limits"] = {"live": S.settings.max_live, "vod": S.settings.max_vod}
    st["state"] = await asyncio.to_thread(S.state.summary)
    st["list_cache_kb"] = {k: v >> 10 for k, v in S.lists.sizes().items()}
    st["memory"] = {**{k: v for k, v in memory_usage().items() if k != "jobs"}, **S.guard.summary()}
    st["health"] = await asyncio.to_thread(health_report)
    return jresp(request, st)


async def cached_list(request: Request, kind: str, cache: str = "private, no-cache", etag: bool = True) -> Response:
    """A pre-built list from S.lists (gzip as stored; decompressed only for a client without gzip)."""
    headers = {"Cache-Control": cache, "Vary": "Accept-Encoding"}
    if etag:
        tag = S.lists.tag(kind)
        if request.headers.get("if-none-match") == f'"{tag}"':        # nothing to build or send
            return Response(status_code=304, headers={"ETag": f'"{tag}"', "Cache-Control": cache})
    tag, body = await S.lists.get(kind)
    if etag:
        headers["ETag"] = f'"{tag}"'
    if "gzip" in request.headers.get("accept-encoding", ""):
        headers["Content-Encoding"] = "gzip"
    else:
        body = await asyncio.to_thread(gzip.decompress, body)
    return Response(body, media_type="application/json", headers=headers)


@app.get("/tv/api/live")
async def live(request: Request):
    return await cached_list(request, "live")


@app.get("/tv/api/movies")
async def movies(request: Request):
    return await cached_list(request, "movies")


@app.get("/tv/api/series")
async def series_list(request: Request):
    return await cached_list(request, "series")


@app.get("/tv/api/epg/now")
async def epg_now(request: Request):
    return await cached_list(request, "epg-now", cache="no-store", etag=False)


@app.get("/tv/api/epg/{iid}")
async def epg_one(iid: str, request: Request):
    if not valid_id(iid, "l"):
        return err(request, 404, "Unknown channel")
    return jresp(request, {"id": iid, "rows": S.cat.guide.schedule(iid)})


# ------------------------------------------------------------------ guide search (Live TV search box)
async def _build_epg_index(key: tuple) -> None:
    t0 = time.time()
    vindex = await asyncio.to_thread(variant_index)     # results group channel versions with it: ready too
    chs = await asyncio.to_thread(channel_sports, vindex)
    idx = await asyncio.to_thread(epgsearch.Index, S.cat.guide.data, None, epgsearch.AHEAD + epgsearch.REBUILD_AFTER, chs)
    S.epg_index, S.epg_index_key = idx, key
    S.sports_cache = {}
    await asyncio.to_thread(release_memory)            # the previous index and the build's scratch
    st = idx.stats()
    log.info("guide search index: %d programmes (%d sport), %d rows, %d channels, %d words in %.1fs",
             st["records"], st["sport_programmes"], st["rows"], st["channels"], st["words"], time.time() - t0)


def channel_sports(vindex: livemeta.VariantIndex) -> dict[str, int]:
    """Channels with a guide -> what their name says about sport (sports.channel_sport)."""
    cats = S.cat.data["cats"]["live"]
    out = {}
    for iid in S.cat.guide.data:
        it = S.cat.by_id.get(iid)
        if it:
            s = sports.channel_sport(it.get("name", ""), cats.get(it.get("cat", ""), ""))
            if not s and (vindex.info.get(iid) or {}).get("sport"):
                s = sports.OTHER
            if s:
                out[iid] = s
    return out


async def epg_index() -> epgsearch.Index | None:
    """The current index. Rebuilt in a worker thread when the guide or channel list changed or it
    is a few hours old; until then the previous one answers. Only the very first search waits."""
    key = (S.cat.guide.updated, S.cat.version)
    idx = S.epg_index
    stale = idx is None or S.epg_index_key != key or time.time() - idx.built > epgsearch.REBUILD_AFTER
    if stale and S.cat.guide.data and (S.epg_index_task is None or S.epg_index_task.done()):
        S.epg_index_task = background(_build_epg_index(key))
    if idx is None and S.epg_index_task and not S.epg_index_task.done():
        await asyncio.shield(S.epg_index_task)
    return S.epg_index


def search_guide(idx: epgsearch.Index, q: str, caps: dict) -> dict:
    t0 = time.perf_counter()
    res = idx.search(q)
    vindex = variant_index()
    events = []
    for ev in res["events"][:epgsearch.MAX_EVENTS]:
        ev = {k: ev[k] for k in ("when", "title", "sub", "fav_ids", "desc", "start", "stop", "strength")} | {"n": len(ev["ids"]), "ids": ev["ids"]}
        groups = epgsearch.channel_groups([i for i in ev.pop("ids") if i in S.cat.by_id], vindex, S.live_meta.get, caps)
        if groups:
            ev["channels"], ev["more"] = groups[:epgsearch.MAX_GROUPS], max(0, len(groups) - epgsearch.MAX_GROUPS)
            events.append(ev)
    return {"events": events, "total": len(res["events"]), "broad": res["broad"],
            "ms": round((time.perf_counter() - t0) * 1000, 1), "built": int(idx.built)}


SPORTS_MAX = 400                     # events per day in the Sports view


def day_bounds(now: float, tzo: int) -> float:
    """The viewer's next midnight (tzo: their offset from UTC in minutes)."""
    local = now + tzo * 60
    return (local // 86400 + 1) * 86400 - tzo * 60


def sports_events(idx: epgsearch.Index, day: str, tzo: int, now: float) -> list[dict]:
    """The day's sports events (no channel versions yet: those depend on the device). Cached
    for a minute per day and time zone; the index answers in ~20-60 ms on the Pi."""
    key = (day, tzo, int(now // 60), int(idx.built))
    hit = S.sports_cache.get(key)
    if hit is not None:
        return hit
    midnight = day_bounds(now, tzo)
    if day == "tomorrow":
        lo, hi = midnight, midnight + 86400
    else:
        lo, hi = now, max(midnight, now + epgsearch.SPORTS_SOON)
    events = idx.sports(now, lo, hi)[:SPORTS_MAX]
    if len(S.sports_cache) > 8:
        S.sports_cache.clear()
    S.sports_cache[key] = events
    return events


def sports_page(idx: epgsearch.Index, day: str, tzo: int, caps: dict, favourites: list[str] | None = None) -> dict:
    t0 = time.perf_counter()
    now = time.time()
    if favourites is not None:
        # For You: the favourites' events today and tomorrow, each once (Index.for_you)
        midnight = day_bounds(now, tzo)
        events = idx.for_you(favourites, now, now, midnight + 86400)
        for ev in events:
            ev["day"] = "today" if ev["start"] < midnight else "tomorrow"
        events.sort(key=lambda e: e["start"])
    else:
        events = sports_events(idx, day, tzo, now)
    t1 = time.perf_counter()
    vindex = variant_index()
    out, counts = [], {}
    for ev in events:
        groups = epgsearch.channel_groups([i for i in ev["ids"] if i in S.cat.by_id], vindex, S.live_meta.get, caps)
        if not groups:
            continue
        counts[ev["sport"]] = counts.get(ev["sport"], 0) + 1
        out.append({k: v for k, v in ev.items() if k not in ("ids", "key", "score")} |
                   {"channels": groups[:epgsearch.MAX_GROUPS], "more": max(0, len(groups) - epgsearch.MAX_GROUPS)})
    return {"events": out, "counts": counts, "day": day, "now": int(now), "built": int(idx.built),
            "ms": {"events": round((t1 - t0) * 1000, 1), "total": round((time.perf_counter() - t0) * 1000, 1)}}


@app.get("/tv/api/sports")
async def sports_view(request: Request):
    """Sports on now / soon / later today (or tomorrow), one card per event (epgsearch.Index.sports)."""
    q = request.query_params
    day = "tomorrow" if q.get("day") == "tomorrow" else "today"
    favourites = None
    if q.get("view") == "foryou":
        favourites = [f for f in (q.get("fav") or "").split(",")[:200] if f in sports.BY_ID_KEY]
    try:
        tzo = max(-840, min(int(q.get("tzo") or 0), 840))
    except ValueError:
        tzo = 0
    idx = await epg_index()
    if idx is None:
        return jresp(request, {"events": [], "counts": {}, "day": day, "pending": True})
    caps = livemeta.parse_caps(q.get("caps", "")[:400])
    return jresp(request, await asyncio.to_thread(sports_page, idx, day, tzo, caps, favourites))


@app.get("/tv/api/sports/catalogue")
async def sports_catalogue(request: Request):
    """Every team and competition that can be a favourite: stable id, name, kind, sport, other names."""
    rows = [{"id": e.key, "name": e.name, "kind": "comp" if e.kind == "competition" else "team",
             "sport": sports.SPORT_KEYS.get(e.sport, ""), "national": e.kind == "national",
             "aka": [n for n in e.names[1:] if len(n) > 2][:6]} for e in sports.ENTITIES]
    return jresp(request, {"items": rows}, etag=f"sc{len(rows)}-{zlib.crc32(sports.DATA.encode()):x}", cache="private, no-cache")


@app.get("/tv/api/live/search")
async def live_search(request: Request):
    """Programmes on now and in the next 24 hours matching q, each with the channels showing it."""
    q = (request.query_params.get("q") or "").strip()[:80]
    if len(q) < epgsearch.MIN_QUERY:
        return jresp(request, {"events": [], "total": 0, "broad": False})
    idx = await epg_index()
    if idx is None:
        return jresp(request, {"events": [], "total": 0, "broad": False, "pending": True})
    caps = livemeta.parse_caps(request.query_params.get("caps", "")[:400])
    return jresp(request, await asyncio.to_thread(search_guide, idx, q, caps))


@app.get("/tv/api/movie/{iid}")
async def movie(iid: str, request: Request):
    it = S.cat.by_id.get(iid) if valid_id(iid, "m") else None
    if not it:
        return err(request, 404, "This movie is no longer in the catalogue.")
    info = await S.cat.movie_details(it)
    return jresp(request, {
        "id": it["id"], "name": it["name"], "year": info.get("year") or it.get("year", 0),
        "rating": info.get("rating") or it.get("rating", 0), "plot": info.get("plot", it.get("plot", "")),
        "genre": info.get("genre", it.get("genre", "")), "cast": info.get("cast", ""),
        "director": info.get("director", ""), "dur": info.get("dur") or it.get("dur", 0),
        "img": img_key(it.get("img") or info.get("img2", "")), "backdrop": img_key(info.get("backdrop", "")),
        "cat": S.cat.data["cats"]["movie"].get(it.get("cat", ""), ""),
    })


@app.get("/tv/api/series/{iid}")
async def series_one(iid: str, request: Request):
    ser = S.cat.by_id.get(iid) if valid_id(iid, "s") else None
    if not ser:
        return err(request, 404, "This series is no longer in the catalogue.")
    try:
        meta, eps = await S.cat.series_details(ser)
    except ProviderError as e:
        return err(request, 502, f"Could not load episodes: {e}")
    seasons: dict[int, list] = {}
    for e in sorted(eps, key=lambda e: (e.get("season", 1), e.get("episode", 0))):
        seasons.setdefault(int(e.get("season", 1)), []).append(Catalogue.public_episode(e))
    return jresp(request, {
        "id": ser["id"], "name": ser["name"], "year": ser.get("year", 0), "rating": ser.get("rating", 0),
        "plot": meta.get("plot") or ser.get("plot", ""), "genre": meta.get("genre") or ser.get("genre", ""),
        "cast": meta.get("cast", ""), "img": img_key(ser.get("img", "")),
        "backdrop": img_key(meta.get("backdrop") or ser.get("backdrop", "")),
        "cat": S.cat.data["cats"]["series"].get(ser.get("cat", ""), ""),
        "seasons": [{"n": n, "episodes": v} for n, v in sorted(seasons.items())],
    })


def direct_local(request: Request) -> bool:
    """A request made on this machine straight to the app (`iptv` on the Pi), not through nginx."""
    return bool(request.client) and request.client.host in ("127.0.0.1", "::1") and "x-forwarded-for" not in request.headers


@app.get("/tv/api/refresh")
async def manual_refresh(request: Request):
    # accept=1 overrides the refresh guard, so only `iptv refresh --accept-changes` may send it: a plain
    # GET, it could otherwise be triggered from another site through a signed-in browser
    if request.query_params.get("accept") == "1" and direct_local(request):
        S.cat.accept_next = True            # `iptv refresh --accept-changes`
        log.info("refresh: the next refresh will be accepted even if it looks wrong")
    if time.time() - S.last_manual < 60 or S.cat.refreshing:
        return jresp(request, {"started": False, "message": "A refresh ran less than a minute ago."})
    S.last_manual = time.time()
    background(refresh_all("manual"))
    return jresp(request, {"started": True})


# ------------------------------------------------------------- synced state
# Favourites, progress, recent, last channel and preferences, shared by every device
# (tvapp/userstate.py has the merge rules).
def write_allowed(request: Request) -> bool:
    """Writes come from our own page: JSON + custom header (a cross-site page cannot send
    either without a CORS preflight, which is never answered) and, if sent, our Origin."""
    if request.headers.get("x-tv-sync") != "1":
        return False
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        return False
    origin = request.headers.get("origin")
    if origin is None:
        return True
    return origin == S.settings.public_origin or urlsplit(origin).netloc == request.headers.get("host", "")


async def read_json(request: Request):
    body = await request.body()
    if len(body) > STATE_MAX_BODY:
        raise StateError("request too large")
    try:
        return json.loads(body)
    except ValueError:
        raise StateError("request body is not JSON") from None


def since_param(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


@app.get("/tv/api/state")
async def state_get(request: Request):
    since = since_param(request.query_params.get("since"))
    return jresp(request, await asyncio.to_thread(S.state.changes_since, since))


@app.post("/tv/api/state")
async def state_post(request: Request):
    if not write_allowed(request):
        return err(request, 403, "Forbidden")
    try:
        body = await read_json(request)
        if not isinstance(body, dict):
            raise StateError("bad request")
        device = body.get("device") if isinstance(body.get("device"), str) and DEVICE_RE.match(body["device"]) else ""
        result = await asyncio.to_thread(S.state.apply, body.get("changes") or [], device)
    except StateError as e:
        return err(request, 400, str(e))
    if result["rejected"]:
        log.info("state: %d invalid change(s) from device %s ignored", result["rejected"], device or "?")
    out = await asyncio.to_thread(S.state.changes_since, since_param(body.get("since")))
    return jresp(request, {**out, **result})


@app.get("/tv/api/state/export")
async def state_export(request: Request):
    doc = await asyncio.to_thread(S.state.export, app_version=__version__, provider_type=S.settings.provider_type)
    name = time.strftime("signal-tv-classic-state-%Y%m%d-%H%M.json")
    return Response(json.dumps(doc, indent=2, ensure_ascii=False), media_type="application/json",
                    headers={"Cache-Control": "no-store", "Content-Disposition": f'attachment; filename="{name}"'})


@app.post("/tv/api/state/import")
async def state_import(request: Request):
    if not write_allowed(request):
        return err(request, 403, "Forbidden")
    mode = request.query_params.get("mode", "merge")
    try:
        doc = await read_json(request)
        result = await asyncio.to_thread(S.state.import_doc, doc, mode)
    except StateError as e:
        return err(request, 400, str(e))
    log.info("state: import (%s): %s", mode, result)
    return jresp(request, result)


# ------------------------------------------------------------------ images
def _img_type(head: bytes) -> str:
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[4:12] in (b"ftypavif", b"ftypavis"):
        return "image/avif"
    if head.startswith(b"BM"):
        return "image/bmp"
    if head.startswith(b"\x00\x00\x01\x00"):
        return "image/x-icon"
    return ""                       # SVG/HTML/anything else is refused


def prune_images() -> None:
    d = S.settings.cache_dir / "img"
    files = []
    for p in d.iterdir():
        try:
            st = p.stat()
        except FileNotFoundError:                   # replaced/removed by a request meanwhile
            continue
        files.append((st.st_atime, st.st_size, p))
    total = sum(f[1] for f in files)
    if total <= IMG_CACHE_MAX:
        return
    for _, size, p in sorted(files):
        p.unlink(missing_ok=True)
        total -= size
        if total < IMG_CACHE_MAX * 0.8:
            break


@app.get("/tv/api/img/{key}")
async def image(key: str, request: Request):
    if not IMG_KEY.match(key):
        return Response(status_code=404)
    path = S.settings.cache_dir / "img" / key
    if path.is_file():
        data = await asyncio.to_thread(path.read_bytes)
        return Response(data, media_type=_img_type(data[:16]) or "application/octet-stream",
                        headers={"Cache-Control": "private, max-age=604800"})
    url = S.cat.images.get(key)
    if not url or time.time() - S.img_fail.get(key, 0) < 3600:
        return Response(status_code=404, headers={"Cache-Control": "private, max-age=600"})
    async with S.img_sem:
        try:
            async with S.http.stream("GET", url, headers={"User-Agent": S.settings.user_agent},
                                     timeout=httpx.Timeout(8, read=12)) as r:
                if r.status_code != 200:
                    raise ValueError(f"HTTP {r.status_code}")
                buf = bytearray()
                async for chunk in r.aiter_bytes(65536):
                    buf += chunk
                    if len(buf) > IMG_MAX:
                        raise ValueError("too large")
        except (httpx.HTTPError, ValueError) as e:
            S.img_fail[key] = time.time()
            log.debug("image %s failed: %s", key, redact(e))
            return Response(status_code=404, headers={"Cache-Control": "private, max-age=600"})
    data = bytes(buf)
    ctype = _img_type(data[:16])
    if not ctype:
        S.img_fail[key] = time.time()
        return Response(status_code=404)
    tmp = path.with_name(f"{key}.{secrets.token_hex(4)}.tmp")   # own name: two requests for one image can overlap
    await asyncio.to_thread(tmp.write_bytes, data)
    tmp.replace(path)
    return Response(data, media_type=ctype, headers={"Cache-Control": "private, max-age=604800"})


# -------------------------------------------------------------- playback
async def _range_supported(item: dict) -> bool:
    if item["id"] in S.range_ok:
        return S.range_ok[item["id"]]
    ok = False
    try:
        headers = {"User-Agent": item.get("ua") or S.settings.user_agent, "Range": "bytes=0-1"}
        if item.get("ref"):
            headers["Referer"] = item["ref"]
        async with S.http.stream("GET", item["url"], headers=headers, timeout=httpx.Timeout(10, read=15)) as r:
            ok = r.status_code == 206
    except httpx.HTTPError:
        ok = False
    S.range_ok[item["id"]] = ok
    return ok


def _int_param(value) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except ValueError:
        return None


def pick_tracks(q, audio: list[dict], subs: list[dict]) -> tuple[int | None, int | None]:
    """Audio/subtitle stream to play, from the viewer's preferences sent with the play request:
    a / s = an explicit pick from the menu (s=-1: subtitles off); otherwise sub = off|auto|on,
    sl = subtitle languages, sdh = prefer|avoid|either, al = audio languages, ad=1 = prefer
    Audio Description. A client that sends none of these gets what it got before: the file's
    default audio, no subtitles."""
    a = tracks.choose_audio(audio, tracks.parse_langs(q.get("al", "")), q.get("ad") == "1", _int_param(q.get("a")))
    mode = q.get("sub") if q.get("sub") in tracks.SUB_MODES else "off"
    sdh = q.get("sdh") if q.get("sdh") in tracks.SDH_MODES else "either"
    audio_lang = audio[a]["lang"] if a is not None else ""
    s = tracks.choose_subtitle(subs, mode, tracks.parse_langs(q.get("sl", "")), sdh, audio_lang, _int_param(q.get("s")))
    return a, s


def public_track(t: dict) -> dict:
    return {k: t[k] for k in ("i", "label", "lang", "name", "region", "sdh", "forced", "ad", "commentary", "ok", "def", "ch")
            if k in t}


# -------------------------------------------------------------- live quality
# Which version of a live channel to play on this device: tvapp/livemeta.py has the rules.
MAX_VARIANT_PROBES = 6          # formats probed (one short provider connection each, one at a time)
PROBE_BUDGET_S = 12             # ...and no more than this long in total before starting playback
LIVE_READY_SEGMENTS = 5         # segments before hand-over: the provider bursts ~20 s at connect, so 5 cost
                                # nothing (measured: hand-over 2.1 s) and give the player a cushion for its
                                # later 4-12 s delivery gaps (3 segments: 2 stalls in 90 s; 5: none in 120 s)
LIVE_RECONNECT_GAP = 2.0        # s between closing a provider connection and opening the next (a 4xx seen at 1 s)


def learned_meta(iid: str, meta: dict | None, **notes) -> None:
    """Formats/speeds learned from live jobs (tvapp/streams.py)."""
    if meta:
        S.live_meta.put(iid, meta, "job")
    if notes:
        S.live_meta.note(iid, **notes)
    background(asyncio.to_thread(S.live_meta.save))


def variant_index() -> livemeta.VariantIndex:
    if S.vindex is None or S.vindex_version != S.cat.version:
        S.vindex = livemeta.VariantIndex(S.cat.data["live"], S.cat.data["cats"]["live"])
        S.vindex_version = S.cat.version
    return S.vindex


def variant_rows(cands: list[str], caps: dict, current: str, sport: bool) -> list[dict]:
    """The quality menu: known versions of this channel, best first."""
    rows = []
    for iid in cands:
        it = S.cat.by_id.get(iid)
        m = S.live_meta.get(iid)
        if not it or not m:
            continue
        rows.append({"id": iid, "name": it["name"], "label": livemeta.label(m), "playable": livemeta.playable(m, caps),
                     "score": livemeta.score(m, sport), "current": iid == current})
    rows.sort(key=lambda r: (-r["playable"] if r["playable"] < 2 else -2, -r["score"]))
    return rows[:12]


async def choose_live(item: dict, caps: dict, quality: str) -> tuple[dict, list[str], bool]:
    idx = await asyncio.to_thread(variant_index)
    cands = idx.variants(item["id"])
    inf = idx.info.get(item["id"]) or {}
    sport = bool(inf.get("sport"))
    can_probe = not S.engine.jobs                    # never a second provider connection beside a running stream
    t0 = time.time()
    probed = 0

    async def ensure(iid: str) -> None:
        nonlocal probed
        if not livemeta.needs_probe(S.live_meta.get(iid)) or not can_probe or probed >= MAX_VARIANT_PROBES \
                or time.time() - t0 > PROBE_BUDGET_S:
            return
        it = S.cat.by_id.get(iid)
        if not it:
            return
        probed += 1
        meta = await S.engine.probe_live(it)
        if meta:
            S.live_meta.put(iid, meta, "probe")

    first = quality if quality not in ("auto", "orig") and quality in cands else item["id"]
    await ensure(first)
    metas = {i: m for i in cands if (m := S.live_meta.get(i))}
    d = livemeta.decide(item["id"], cands, metas, caps, sport, quality)
    sel = metas.get(first)
    if quality == "auto" and (not sel or livemeta.playable(sel, caps) < 2):
        # the chosen version is unknown or not smooth here: look at the likeliest other versions
        has_hevc = any(k.startswith("hevc") for k in caps)

        def ceiling(i: str) -> float:
            """Best score this unknown version could have, judged by its name."""
            h = livemeta.hint_height((idx.info.get(i) or {}).get("hints", [])) or 2160
            return livemeta.score({"h": h, "fps": 50, "hdr": h >= 2160}, sport)

        smooth = [livemeta.score(m, sport) for m in metas.values() if livemeta.playable(m, caps) == 2]
        best = max(smooth, default=0.0)
        unknown = [i for i in cands if i not in metas
                   and not ("HEVC" in (idx.info.get(i) or {}).get("hints", []) and not has_hevc)
                   and ceiling(i) > best]
        for iid in sorted(unknown, key=ceiling, reverse=True):
            if ceiling(iid) <= best:
                continue                        # a version already found is at least as good
            await ensure(iid)
            m = S.live_meta.get(iid)
            if m and livemeta.playable(m, caps) == 2:
                best = max(best, livemeta.score(m, sport))
        metas = {i: m for i in cands if (m := S.live_meta.get(i))}
        d = livemeta.decide(item["id"], cands, metas, caps, sport, quality)
    if probed:
        await asyncio.to_thread(S.live_meta.save)
    d["probed"] = probed
    d["metas"] = metas
    return d, cands, sport


async def learn_scan(job, target_id: str) -> dict | None:
    """Field order and field/frame coding from the first segment a copy job wrote (a local ffprobe,
    ~0.2 s, no provider connection); cached, so each channel is looked at once."""
    for _ in range(10):
        segs = sorted(job.dir.glob("seg_*.ts")) + sorted(job.dir.glob("seg_*.m4s"))
        if segs:
            break
        await asyncio.sleep(0.2)
    else:
        return None
    found = await S.engine.analyze_file(segs[0])
    if not found:
        return None
    scan = {k: found[k] for k in ("interlaced", "field_order", "fields", "coding") if k in found}
    if not scan:
        return None
    meta = S.live_meta.put(target_id, scan, "segment")
    log.info("live %s: %s", target_id, livemeta.describe(meta))
    return meta


async def play_live(request: Request, item: dict, c: str, rechecked: bool = False) -> Response:
    q = request.query_params
    caps_text = q.get("caps", "")[:400]
    caps = livemeta.parse_caps(caps_text)
    quality = q.get("q") or "auto"
    if quality not in ("auto", "orig") and not valid_id(quality, "l"):
        quality = "auto"
    d, cands, sport = await choose_live(item, caps, quality)
    metas = d.pop("metas")
    menu = variant_rows(cands, caps, d.get("play") or item["id"], sport)
    if d["mode"] == "none":
        if not metas.get(item["id"]):
            # format unknown (e.g. the provider connection is busy): play it as before, the job learns it
            d = {"play": item["id"], "mode": "copy", "notice": "", "reason": "format not known yet; copying",
                 "probed": d.get("probed", 0)}
        else:
            return jresp(request, {"error": f"This version can't be played smoothly on this device: {d['reason']}.",
                                   "variants": menu, "unplayable": True}, 409)
    target = S.cat.by_id.get(d["play"]) or item
    meta = metas.get(target["id"]) or {}
    transcode = d["mode"] == "transcode"
    container = "fmp4" if (meta.get("v") == "hevc" and not transcode) else "ts"
    decision = {**{k: d[k] for k in ("mode", "reason", "notice")}, "requested": item["id"], "requested_name": item["name"],
                "requested_meta": metas.get(item["id"]), "meta": meta, "caps": caps_text, "quality": quality,
                "probed": d.get("probed", 0), "sport": sport}
    job = await S.engine.start_live(target, c, transcode, container, decision)
    ready = LIVE_READY_SEGMENTS
    if (q.get("ready") or "").isdigit():
        ready = max(2, min(int(q["ready"]), 6))       # testing knob: segments before hand-over
    job = await S.engine.wait_ready(job, ready)
    if job.state == "failed":
        stream_outcome(False, target["id"], job.error)
        return err(request, 502, job.error or "The channel could not be started.")
    stream_outcome(True, target["id"])
    if not transcode and livemeta.scan_unknown(S.live_meta.get(target["id"]) or meta):
        learned = await learn_scan(job, target["id"])
        if learned and not rechecked and livemeta.playable(learned, caps) == 0:
            # field-coded interlaced H.264 this device can't decode (1.8.0): decide again with what was
            # just learned, before the player sees a stream it would only show black for
            log.info("live %s: %s can't be decoded by this device; choosing again", target["id"], livemeta.label(learned))
            await S.engine.release(job.id, c)
            await asyncio.sleep(LIVE_RECONNECT_GAP)      # the provider counts a connection for a moment after it closes
            return await play_live(request, item, c, rechecked=True)
        meta = learned or meta
    tok = S.keys.make_token(f"h:{job.id}", c)
    return jresp(request, {"kind": "live", "mode": "hls", "job": job.id, "url": f"/tv/m/{tok}/h/{job.id}/index.m3u8",
                           "transcode": job.transcode, "container": container, "quality": quality,
                           "variant": {"id": target["id"], "name": target["name"], "label": livemeta.label(meta) if meta else ""},
                           "notice": d.get("notice", ""), "variants": menu, "meta": meta})


@app.get("/tv/api/play/{iid}")
async def play(iid: str, request: Request):
    c = client_id(request)
    if not c or not valid_id(iid, "lme"):
        return err(request, 400, "Bad request")
    q = request.query_params
    hint = q.get("series", "")
    item = await S.cat.resolve(iid, hint if valid_id(hint, "s") else "")
    if not item or item.get("kind") not in ("live", "movie", "episode"):
        return err(request, 404, "This item is no longer in the catalogue. Try refreshing.")
    prev = q.get("prev", "")
    if JOB_RE.match(prev):
        await S.engine.release(prev, c)          # channel switch: free the provider connection first
    hevc_ok = q.get("hevc") == "1"
    try:
        offset = max(0.0, min(float(q.get("t") or 0), 86400.0))
        if item["kind"] == "live":
            return await play_live(request, item, c)
        probe = await S.engine.probe(item)
        dur = probe.get("dur") or item.get("dur") or 0
        if dur and offset > dur - 3:
            offset = 0.0
        audio, subs = probe.get("audio") or [], probe.get("subs") or []
        a, s = pick_tracks(q, audio, subs)
        mode = S.engine.vod_mode(item, probe, hevc_ok)
        if mode == "direct" and (s is not None or (a or 0) > 0):
            mode = "hls"                 # a browser plays an MP4's first audio only, and no embedded text
        if mode == "direct" and not await _range_supported(item):
            mode = "hls"
        info = {"audio": [public_track(t) for t in audio], "subs": [public_track(t) for t in subs],
                "a": a, "s": s}
        if mode == "direct":
            tok = S.keys.make_token(f"f:{item['id']}", c)
            return jresp(request, {"kind": item["kind"], "mode": "file", "job": None, "offset": 0,
                                   "start": offset, "duration": dur, "tracks": info,
                                   "url": f"/tv/m/{tok}/f/{item['id']}/stream"})
        job = await S.engine.start_vod(item, c, offset, hevc_ok, a if len(audio) > 1 else None, s)
        if s is not None:
            job.sub_track = subs[s]
        job = await S.engine.wait_ready(job, 1)
        if job.state == "failed":
            stream_outcome(False, item["id"], job.error)
            return err(request, 502, job.error or "This title could not be started.")
        stream_outcome(True, item["id"])
        origin = await S.engine.origin(job)
        tok = S.keys.make_token(f"h:{job.id}", c)
        return jresp(request, {"kind": item["kind"], "mode": "hls", "job": job.id, "offset": origin,
                               "start": 0, "duration": dur, "transcode": job.transcode, "tracks": info,
                               "url": f"/tv/m/{tok}/h/{job.id}/{'master.m3u8' if s is not None else 'index.m3u8'}"})
    except RuntimeError as e:
        return err(request, 429, str(e))
    except (OSError, ValueError) as e:
        log.warning("play %s failed: %s", iid, redact(e))
        return err(request, 500, "The player could not start this item.")


def diagnostics(item: dict, job, mode: str, client_caps: str = "") -> dict:
    """One title/channel's playback report for copying into a bug report. Built only from what
    the app already knows (no provider request); every line is redacted."""
    now = time.time()
    probe = S.engine.probes.get(item["id"]) or {}
    recent = S.engine.recent.get(item["id"])
    st = S.cat.status()
    jobs = S.engine.stats()
    live_n = sum(1 for j in jobs if j["kind"] == "live")
    vod_n = sum(1 for j in jobs if j["kind"] == "vod")
    lines = [f"Signal TV Classic {__version__} diagnostics - {time.strftime('%Y-%m-%d %H:%M:%S %Z', time.localtime(now))}",
             f"title:     {item.get('name', '')} [{item['id']}] ({item.get('kind', '')})",
             f"container: {item.get('ext') or '?'}" + (f" / {probe.get('fmt')}" if probe.get("fmt") else "")]
    if probe:
        lines.append(f"video:     {probe.get('v') or '?'} {probe.get('w') or '?'}x{probe.get('h') or '?'}"
                     f"{' interlaced' if probe.get('interlaced') else ''}; duration {int(probe.get('dur') or 0)} s")
        lines.append("audio:     " + (", ".join(f"#{t['i']} {t['label']} ({t['codec']})" for t in probe.get("audio") or [])
                                       or "none"))
        lines.append("subtitles: " + (", ".join(f"#{t['i']} {t['label']} ({t['codec']}{'' if t['ok'] else ', picture'})"
                                                for t in probe.get("subs") or []) or "none"))
    elif item.get("kind") != "live":                    # live formats are shown under "source" below
        lines.append("probe:     not probed yet")
    if item.get("kind") == "live":
        d = job.decision if job and job.kind == "live" else {}
        req_id = d.get("requested") or item["id"]
        req = S.cat.by_id.get(req_id) or item
        src = d.get("requested_meta") or S.live_meta.get(req_id)
        lines.append(f"selected:  {req['name']} [{req_id}]")
        lines.append(f"source:    {livemeta.describe(src) if src else 'format not known yet'}"
                     + (f" · ~{src['kbps'] / 1000:.1f} Mbit/s" if src and src.get("kbps") else ""))
        caps_text = d.get("caps") or client_caps
        lines.append(f"client:    {caps_text or 'no capability report (old page): assumed H.264 only'}")
        if src:
            ok = livemeta.playable(src, livemeta.parse_caps(caps_text))
            lines.append(f"           this device {['cannot decode', 'may not decode smoothly', 'decodes smoothly'][ok]} "
                         f"the selected version; converting it on the Pi: ~{livemeta.predicted_transcode_speed(src)}x real time"
                         + (f" (measured {src['measured_speed']}x)" if src.get("measured_speed") else ""))
        if d:
            m = d.get("meta") or {}
            play_it = S.cat.by_id.get(job.item_id) or item
            lines.append(f"playing:   {play_it['name']} [{play_it['id']}]" + ("" if play_it["id"] == req_id else " (equivalent version)"))
            how = ("deinterlaced to 720p50 H.264 on the Pi" if d.get("mode") == "transcode" and m.get("interlaced")
                   else "video converted to H.264" if d.get("mode") == "transcode" else "video copied unchanged")
            lines.append(f"           {livemeta.describe(m)}; {how} ({job.container.upper()} segments), audio to AAC stereo")
            lines.append(f"reason:    {d.get('reason')}" + (f" [quality: {d.get('quality')}]" if d.get("quality") != "auto" else ""))
    if job:
        lines.append(f"playback:  {'live HLS' if job.kind == 'live' else 'remux to HLS'}"
                     f"{', video transcoded to H.264' if job.transcode else ', video copied'}; job {job.id} {job.state}, "
                     f"age {int(now - job.started)} s, restarts {job.restarts}")
        if job.kind == "vod":
            lines.append(f"remux:     from {job.offset:.1f} s (shown from {job.origin if job.origin is not None else '?'} s); "
                         f"audio #{job.audio if job.audio is not None else 0}; "
                         f"subtitle {'#' + str(job.sub) if job.sub is not None else 'off'}")
        if job.error:
            lines.append(f"error:     {job.error}")
        tail = job.tail[-8:]
    else:
        lines.append(f"playback:  {'direct play (byte-range proxy, no ffmpeg)' if mode == 'file' else mode or 'not playing'}")
        tail = (recent or {}).get("tail", [])
    if recent and not job:
        lines.append(f"last job:  ended {int(now - recent['t'])} s ago ({recent['why']}), state {recent['state']}"
                     f"{', error: ' + recent['error'] if recent.get('error') else ''}")
    if tail:
        lines.append("ffmpeg:    " + "\n           ".join(tail))
    r = st["refresh"]
    lines.append(f"provider:  {st['provider']}; catalogue updated {int((now - st['updated']) // 60) if st['updated'] else '?'} min ago"
                 f"{'; error: ' + st['error'] if st['error'] else ''}"
                 f"{'; last refresh ' + r['last']['status'] if r['last'] else ''}")
    hs = S.health.summary()
    lines.append(f"streams:   {live_n}/{S.settings.max_live} live and {vod_n}/{S.settings.max_vod} remux slots in use; "
                 f"{hs['failures_1h']} of {hs['starts_1h']} starts failed in the last hour")
    slot_words = ("458", "509", "connection limit", "403")
    slot_hits = [e for e in S.health.last_errors(10) if any(w in e["message"].lower() for w in slot_words)]
    if slot_hits or live_n >= S.settings.max_live or vod_n >= S.settings.max_vod:
        lines.append("slots:     POSSIBLE connection-slot exhaustion: "
                     + (f"{len(slot_hits)} recent refusals look like a connection limit" if slot_hits
                        else "all of this app's stream slots are busy")
                     + " (another device or app using the same account counts too)")
    for e in S.health.last_errors(3):
        lines.append(f"recent:    {time.strftime('%H:%M:%S', time.localtime(e['t']))} {e['item']}: {e['message']}")
    text = "\n".join(alerts.clean(x, 2000) for x in lines)
    return {"text": text, "version": __version__, "t": int(now)}


@app.get("/tv/api/diag/{iid}")
async def diag(iid: str, request: Request):
    item = S.cat.by_id.get(iid) if valid_id(iid, "lmes") else None
    if not item:
        return err(request, 404, "Unknown item")
    job_id = request.query_params.get("job", "")
    job = S.engine.find(job_id) if JOB_RE.match(job_id) else None
    return jresp(request, await asyncio.to_thread(diagnostics, item, job, request.query_params.get("mode", ""),
                                                  request.query_params.get("caps", "")[:400]))


@app.get("/tv/api/heartbeat")
async def heartbeat(request: Request):
    c, job_id = client_id(request), request.query_params.get("job", "")
    job = S.engine.find(job_id) if c and JOB_RE.match(job_id) else None
    if job:
        job.touch(c, media=False)
    return jresp(request, {"alive": bool(job), "state": job.state if job else "gone",
                           "produced": S.engine.produced(job) if job and job.kind == "vod" else None})


@app.get("/tv/api/stop")
async def stop(request: Request):
    c, job_id = client_id(request), request.query_params.get("job", "")
    if c and JOB_RE.match(job_id):
        await S.engine.release(job_id, c)
    return Response(status_code=204)


# ------------------------------------------------------------------ media
@app.api_route("/tv/m/{tok}/h/{job_id}/{name}", methods=["GET", "HEAD"])
async def hls_file(tok: str, job_id: str, name: str):
    if not JOB_RE.match(job_id) or not HLS_NAME.match(name):
        return Response(status_code=404)
    client = S.keys.token_client(tok, f"h:{job_id}")
    job = S.engine.find(job_id) if client else None
    if not job:
        return Response(status_code=404 if client else 403, headers={"Cache-Control": "no-store"})
    path = job.dir / name
    if not path.is_file():
        return Response(status_code=404, headers={"Cache-Control": "no-store"})
    job.touch(client)
    ext = Path(name).suffix
    if job.kind == "live" and name == "index.m3u8":
        text = await asyncio.to_thread(path.read_text, errors="replace")
        return Response(hls.live_playlist(text), media_type=MEDIA_TYPES[ext], headers={"Cache-Control": "no-store"})
    if job.kind == "vod" and ext in (".m3u8", ".vtt"):          # tvapp/hls.py says why each is changed
        text = await asyncio.to_thread(path.read_text, errors="replace")
        if name == job.playlist:
            text = hls.media_playlist(text)
        elif name == "master.m3u8":
            text = hls.master_playlist(text, job.sub_track)
        elif name == "index_0_vtt.m3u8":
            text = hls.subtitle_playlist(text)
        elif ext == ".vtt":
            text = hls.webvtt_segment(text)
        return Response(text, media_type=MEDIA_TYPES[ext], headers={"Cache-Control": "no-store"})
    return FileResponse(path, media_type=MEDIA_TYPES[ext], headers={"Cache-Control": "no-store"})


@app.api_route("/tv/m/{tok}/f/{iid}/stream", methods=["GET", "HEAD"])
async def file_proxy(tok: str, iid: str, request: Request):
    if not valid_id(iid, "me") or not S.keys.token_client(tok, f"f:{iid}"):
        return Response(status_code=403)
    item = S.cat.by_id.get(iid)
    if not item:
        return Response(status_code=404)
    headers = {"User-Agent": item.get("ua") or S.settings.user_agent, "Accept-Encoding": "identity"}
    if item.get("ref"):
        headers["Referer"] = item["ref"]
    rng = request.headers.get("range", "")
    if rng and RANGE_RE.match(rng.replace(" ", "")):
        headers["Range"] = rng.replace(" ", "")
    req = S.http.build_request(request.method, item["url"], headers=headers,
                               timeout=httpx.Timeout(15, read=60))
    try:
        r = await S.http.send(req, stream=True)
    except httpx.HTTPError as e:
        log.info("file proxy %s: %s", iid, redact(e))
        return Response(status_code=502)
    if r.status_code not in (200, 206, 416):
        await r.aclose()
        log.info("file proxy %s: upstream HTTP %d", iid, r.status_code)
        return Response(status_code=502)
    out = {k: r.headers[k] for k in ("content-length", "content-range", "last-modified", "etag") if k in r.headers}
    out["Accept-Ranges"] = "bytes"
    out["Cache-Control"] = "no-store"
    out["Content-Disposition"] = "inline"
    return StreamingResponse(r.aiter_raw(65536), status_code=r.status_code, headers=out,
                             media_type="video/mp4", background=BackgroundTask(r.aclose))


# must be registered last: it would otherwise shadow the API routes
@app.get("/tv/{area}/{sub:path}")
async def static_file(area: str, sub: str, request: Request):
    """/tv/static/<file> and /tv/pwa/<file> only; anything else 404s."""
    if area not in ("static", "pwa") or not re.match(r"^[A-Za-z0-9._/-]{1,120}$", sub) or ".." in sub:
        return Response("Not found", status_code=404, media_type="text/plain")
    root = (S.settings.web_dir / area).resolve()
    path = (root / sub).resolve()
    if root not in path.parents or not path.is_file() or path.suffix not in STATIC_TYPES:
        return Response("Not found", status_code=404, media_type="text/plain")
    if area == "pwa":
        cache = "public, max-age=86400"
    else:
        cache = "private, max-age=604800, immutable" if request.query_params.get("v") else "no-cache"
    return FileResponse(path, media_type=STATIC_TYPES[path.suffix], headers={"Cache-Control": cache})


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    s = cfg.load()
    uvicorn.run(app, host=s.host, port=s.port, access_log=False, server_header=False, proxy_headers=False,
                log_level="info", timeout_graceful_shutdown=5)
