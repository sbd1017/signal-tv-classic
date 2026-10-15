"""Provider adapters. Each turns a provider into the same normalised shape:

    {"live": [item], "movie": [item], "series": [series], "episode": [item],
     "cats": {"live": {cat_id: name}, "movie": {...}, "series": {...}}}

item   = {id, kind, name, cat, img, url, epg, num, year, rating, plot, genre, dur, ext,
          series, season, episode, added, ua, ref}
series = {id, name, cat, img, plot, genre, year, rating, backdrop, native, loaded}

`url`, `ua`, `ref` and `native` never leave the server (see catalog.public_*).
"""
from __future__ import annotations

import asyncio
import itertools
import json
import re
import tempfile
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

from .config import Settings
from .util import clean, clean_long, http_url, iter_json_array, make_id, redact, to_float, to_int

MAX_PLAYLIST_BYTES = 400 * 1024 * 1024
VIDEO_EXT = {"mp4", "mkv", "avi", "mov", "m4v", "wmv", "flv", "webm", "mpg", "mpeg", "divx", "3gp", "ogv"}
_ATTR = re.compile(r'([A-Za-z0-9_-]+)="([^"]*)"')
_EP = re.compile(r"(?i)^(?P<show>.*?)[\s._\-|:]*\b(?:S(?P<s>\d{1,3})[\s._-]*E(?P<e>\d{1,4})|(?P<s2>\d{1,2})x(?P<e2>\d{2,3}))\b[\s._\-:|]*(?P<rest>.*)$")
_XT_PATH = re.compile(r"/(live|movie|series)/[^/]+/[^/]+/(\d+)(?:\.([A-Za-z0-9]{1,5}))?$")
_YEAR = re.compile(r"\((19\d{2}|20\d{2})\)|\b(19\d{2}|20\d{2})\b\s*$")
MOVIE_WORDS = re.compile(r"(?i)\b(vod|movie|movies|film|films|cinema|kino|pel[ií]culas?)\b")
SERIES_WORDS = re.compile(r"(?i)\b(series|tv shows?|shows|serien|s[eé]ries|box ?sets?)\b")


def _ep_title(show: str, title: str) -> str:
    """'Show S01E02 - Title' -> 'Title' (panels often repeat the show and numbering)."""
    m = _EP.match(title)
    if m and (not show or m.group("show").strip(" -|:._").lower() in ("", show.lower())):
        return clean(m.group("rest"), 160).strip(" -|:._")
    return title


class ProviderError(Exception):
    """A provider problem with a message that is safe to show (no URLs or credentials)."""


def _ext(url: str) -> str:
    path = urlsplit(url).path
    if "." in path.rsplit("/", 1)[-1]:
        return path.rsplit(".", 1)[-1].lower()[:5]
    return ""


def _year(name: str) -> int:
    m = _YEAR.search(name)
    return int(m.group(1) or m.group(2)) if m else 0


async def _download(http: httpx.AsyncClient, url: str, headers: dict, what: str) -> Path:
    """Stream a (possibly huge) provider file to a temp file; never logs the URL."""
    tmp = tempfile.NamedTemporaryFile(prefix="tv-", suffix=".dl", delete=False)
    size = 0
    try:
        async with http.stream("GET", url, headers=headers, timeout=httpx.Timeout(30, read=120)) as r:
            if r.status_code != 200:
                raise ProviderError(f"{what}: provider answered HTTP {r.status_code}")
            async for chunk in r.aiter_bytes(1 << 16):
                size += len(chunk)
                if size > MAX_PLAYLIST_BYTES:
                    raise ProviderError(f"{what}: file is larger than {MAX_PLAYLIST_BYTES >> 20} MB")
                tmp.write(chunk)
        tmp.close()
        return Path(tmp.name)
    except httpx.HTTPError as e:
        tmp.close()
        Path(tmp.name).unlink(missing_ok=True)
        raise ProviderError(f"{what}: {type(e).__name__} ({redact(e)})") from None
    except BaseException:
        tmp.close()
        Path(tmp.name).unlink(missing_ok=True)
        raise


def empty() -> dict:
    return {"live": [], "movie": [], "series": [], "episode": [], "cats": {"live": {}, "movie": {}, "series": {}}}


# --------------------------------------------------------------------------- M3U
def iter_m3u(path: Path):
    """Yields (duration, attrs, title, url, opts) for each entry of an extended M3U."""
    attrs: dict = {}
    title = ""
    dur = -1.0
    opts: dict = {}
    group = ""
    have_inf = False
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            if line.startswith("#EXTINF:"):
                body = line[8:]
                attrs = {k.lower(): v for k, v in _ATTR.findall(body)}
                # title = text after the last comma that is outside quotes
                inq, cut = False, -1
                for i, c in enumerate(body):
                    if c == '"':
                        inq = not inq
                    elif c == "," and not inq:
                        cut = i
                title = body[cut + 1:] if cut >= 0 else ""
                head = body.split(" ", 1)[0].split(",", 1)[0]
                dur = to_float(head, -1)
                have_inf = True
                opts = {}
                group = ""
            elif line.startswith("#EXTGRP:"):
                group = line[8:]
            elif line.startswith("#EXTVLCOPT:"):
                k, _, v = line[11:].partition("=")
                opts[k.strip().lower()] = v.strip()
            elif line.startswith("#"):
                continue
            elif have_inf:
                if group and "group-title" not in attrs:
                    attrs["group-title"] = group
                yield dur, attrs, title, line, opts
                have_inf = False


def classify(dur: float, attrs: dict, title: str, url: str) -> str:
    """live / movie / episode, best signal first (documented in README)."""
    m = _XT_PATH.search(urlsplit(url).path)
    if m:
        return {"live": "live", "movie": "movie", "series": "episode"}[m.group(1)]
    t = (attrs.get("tvg-type") or attrs.get("type") or "").lower()
    if t in ("live", "movie", "series", "episode", "vod"):
        return {"vod": "movie", "series": "episode"}.get(t, t)
    group = attrs.get("group-title", "")
    ext = _ext(url)
    is_file = ext in VIDEO_EXT
    if _EP.match(title) and (is_file or SERIES_WORDS.search(group) or dur > 0):
        return "episode"
    if SERIES_WORDS.search(group) and (is_file or dur > 0):
        return "episode"
    if MOVIE_WORDS.search(group) and (is_file or dur > 0 or ext not in ("m3u8", "ts")):
        return "movie"
    if is_file:
        return "movie"
    return "live"


class M3UProvider:
    kind = "m3u"

    def __init__(self, s: Settings):
        self.s = s

    def epg_url(self) -> str:
        return http_url(self.s.epg_url)

    async def fetch(self, http: httpx.AsyncClient) -> dict:
        url = http_url(self.s.m3u_url)
        if not url:
            raise ProviderError("IPTV_M3U_URL is not set (or is not an http/https URL)")
        path = await _download(http, url, {"User-Agent": self.s.user_agent}, "playlist")
        try:
            return await asyncio.to_thread(parse_playlist, path, self)     # big pure-Python loop: not on the event loop
        finally:
            path.unlink(missing_ok=True)

    async def movie_info(self, http, item) -> dict:
        return {}

    async def series_info(self, http, series) -> tuple[dict, list]:
        return {}, []          # M3U series are complete at refresh time


def parse_playlist(path: Path, provider) -> dict:
    out = empty()
    seen: set = set()
    series_by_key: dict = {}
    first_line = path.open("rb").read(64).lstrip(b"\xef\xbb\xbf").strip()
    if not first_line.startswith(b"#EXTM3U") and not first_line.startswith(b"#EXTINF"):
        raise ProviderError("playlist: this does not look like an M3U playlist")
    num = 0
    for dur, attrs, title, url, opts in iter_m3u(path):
        url = http_url(url)
        if not url:
            continue
        kind = classify(dur, attrs, title, url)
        name = clean(title or attrs.get("tvg-name"), 160) or "Untitled"
        group = clean(attrs.get("group-title"), 80) or "Uncategorised"
        logo = http_url(attrs.get("tvg-logo") or attrs.get("logo") or "")
        m = _XT_PATH.search(urlsplit(url).path)
        native = m.group(2) if m else urlsplit(url).path + "?" + (attrs.get("tvg-id") or "")
        ua = clean(opts.get("http-user-agent") or attrs.get("user-agent") or "", 200)
        ref = http_url(opts.get("http-referrer") or opts.get("http-referer") or "")
        cat_kind = {"episode": "series"}.get(kind, kind)
        cat_id = make_id("c", cat_kind, group)[2:10]
        out["cats"][cat_kind].setdefault(cat_id, group)
        base = {"name": name, "cat": cat_id, "img": logo, "url": url, "ua": ua, "ref": ref, "ext": _ext(url)}
        if kind == "live":
            num += 1
            key = (native if m else f"{group}|{name}|{attrs.get('tvg-id', '')}")
            iid = make_id("l", "m3u", key)
            n = 2
            while iid in seen:
                iid = make_id("l", "m3u", key, n)
                n += 1
            seen.add(iid)
            chno = to_int(attrs.get("tvg-chno") or attrs.get("channel-number"), 0)
            out["live"].append({**base, "id": iid, "kind": "live", "epg": clean(attrs.get("tvg-id"), 120),
                                "epg_name": clean(attrs.get("tvg-name") or name, 120), "num": chno or num})
        elif kind == "movie":
            iid = make_id("m", "m3u", native)
            if iid in seen:
                continue
            seen.add(iid)
            out["movie"].append({**base, "id": iid, "kind": "movie", "year": _year(name),
                                 "dur": int(dur) if dur > 0 else 0, "added": len(out["movie"])})
        else:
            mm = _EP.match(title or name)
            if mm:
                show = clean(mm.group("show"), 120).strip(" -|:._") or group
                season = to_int(mm.group("s") or mm.group("s2"), 1)
                ep = to_int(mm.group("e") or mm.group("e2"), 0)
                rest = clean(mm.group("rest"), 160)
            else:
                show, season, ep, rest = group, 1, 0, name
            skey = (group, show.lower())
            ser = series_by_key.get(skey)
            if not ser:
                ser = {"id": make_id("s", "m3u", group, show.lower()), "name": show, "cat": cat_id, "img": logo,
                       "plot": "", "genre": "", "year": _year(show), "rating": 0, "backdrop": "",
                       "native": "", "loaded": True, "count": 0}
                series_by_key[skey] = ser
                out["series"].append(ser)
            ser["count"] += 1
            if not ser["img"] and logo:
                ser["img"] = logo
            iid = make_id("e", "m3u", native)
            if iid in seen:
                continue
            seen.add(iid)
            if not ep:
                ep = ser["count"]
            out["episode"].append({**base, "id": iid, "kind": "episode", "series": ser["id"], "season": season,
                                   "episode": ep, "name": rest or f"Episode {ep}", "img": logo,
                                   "dur": int(dur) if dur > 0 else 0})
    for s in out["series"]:
        s.pop("count", None)
    return out


# ------------------------------------------------------------------------ Xtream
class XtreamProvider:
    kind = "xtream"

    def __init__(self, s: Settings):
        self.s = s
        self.base = http_url(s.xtream_base)
        self.stats: dict = {}          # last fetch: {section: (rows seen, rows unusable)} for the refresh guard

    def _api(self, **params) -> tuple[str, dict]:
        p = {"username": self.s.xtream_user, "password": self.s.xtream_pass, **params}
        return f"{self.base}/player_api.php", p

    def epg_url(self) -> str:
        if http_url(self.s.epg_url):
            return self.s.epg_url
        return (f"{self.base}/xmltv.php?username={quote(self.s.xtream_user, safe='')}"
                f"&password={quote(self.s.xtream_pass, safe='')}")

    def stream_url(self, kind: str, sid: object, ext: str) -> str:
        u, p = quote(self.s.xtream_user, safe=""), quote(self.s.xtream_pass, safe="")
        return f"{self.base}/{kind}/{u}/{p}/{quote(str(sid), safe='')}.{ext}"

    async def _get_raw(self, http: httpx.AsyncClient, what: str, **params) -> bytes:
        url, p = self._api(**params)
        try:
            r = await http.get(url, params=p, headers={"User-Agent": self.s.user_agent},
                               timeout=httpx.Timeout(20, read=180))
        except httpx.HTTPError as e:
            raise ProviderError(f"xtream {what}: {type(e).__name__} ({redact(e)})") from None
        if r.status_code != 200:
            raise ProviderError(f"xtream {what}: provider answered HTTP {r.status_code}")
        return r.content

    async def _get(self, http: httpx.AsyncClient, what: str, **params):
        """Small replies (login, categories, one title's info): parsed in place."""
        raw = await self._get_raw(http, what, **params)
        try:
            return json.loads(raw)
        except ValueError:
            raise ProviderError(f"xtream {what}: provider did not return JSON") from None

    async def _each(self, http: httpx.AsyncClient, what: str, action: str, handle) -> None:
        """A big list (live/vod/series: up to ~100 MB): downloaded, then parsed and handed to
        handle(row) one element at a time in a worker thread, so the event loop - and live
        video - keeps running, and the raw reply is never held as one huge list of dicts."""
        box = [await self._get_raw(http, what, action=action)]

        def work():
            try:
                for row in iter_json_array(box.pop()):
                    handle(row)
            except ValueError:
                raise ProviderError(f"xtream {what}: provider did not return JSON") from None
        await asyncio.to_thread(work)

    async def fetch(self, http: httpx.AsyncClient) -> dict:
        if not (self.base and self.s.xtream_user and self.s.xtream_pass):
            raise ProviderError("Xtream is selected but XTREAM_BASE_URL / USERNAME / PASSWORD are not all set")
        info = await self._get(http, "login")
        ui = (info or {}).get("user_info") or {} if isinstance(info, dict) else {}
        if str(ui.get("auth", "1")) == "0":
            raise ProviderError("xtream: the provider rejected the username/password")
        if ui.get("status") and str(ui.get("status")).lower() not in ("active", "trial"):
            raise ProviderError(f"xtream: account status is '{clean(ui.get('status'), 30)}'")
        out = empty()
        for kind, action in (("live", "get_live_categories"), ("movie", "get_vod_categories"),
                             ("series", "get_series_categories")):
            for c in await self._get(http, action, action=action) or []:
                if isinstance(c, dict):
                    out["cats"][kind][clean(c.get("category_id"), 20)] = clean(c.get("category_name"), 80) or "Uncategorised"

        def cat(kind, value):
            cid = clean(value, 20)
            if cid not in out["cats"][kind]:
                out["cats"][kind][cid] = "Uncategorised"
            return cid

        position = itertools.count(1)          # default channel number: place in the provider's list
        stats = {k: [0, 0] for k in ("live", "movie", "series")}

        def bad(kind, ok):
            stats[kind][0] += 1
            if not ok:
                stats[kind][1] += 1
            return not ok

        def live(c):
            n = next(position)
            if bad("live", isinstance(c, dict) and c.get("stream_id") not in (None, "")):
                return
            sid = c["stream_id"]
            out["live"].append({
                "id": make_id("l", "xt", sid), "kind": "live", "name": clean(c.get("name"), 160) or "Channel",
                "cat": cat("live", c.get("category_id")), "img": http_url(c.get("stream_icon") or ""),
                "url": self.stream_url("live", sid, "ts"), "epg": clean(c.get("epg_channel_id"), 120),
                "epg_name": clean(c.get("name"), 120), "num": to_int(c.get("num"), n), "ext": "ts",
                "ua": "", "ref": "", "native": str(sid)})

        def movie(c):
            if bad("movie", isinstance(c, dict) and c.get("stream_id") not in (None, "")):
                return
            sid, ext = c["stream_id"], clean(c.get("container_extension"), 5).lower() or "mp4"
            name = clean(c.get("name"), 160) or "Movie"
            out["movie"].append({
                "id": make_id("m", "xt", sid), "kind": "movie", "name": name,
                "cat": cat("movie", c.get("category_id")), "img": http_url(c.get("stream_icon") or ""),
                "url": self.stream_url("movie", sid, ext), "ext": ext, "year": to_int(c.get("year"), 0) or _year(name),
                "rating": round(to_float(c.get("rating_5based"), 0) * 2, 1) or to_float(c.get("rating"), 0),
                "added": to_int(c.get("added"), 0), "dur": 0, "ua": "", "ref": "", "native": str(sid)})

        def series(c):
            if bad("series", isinstance(c, dict) and c.get("series_id") not in (None, "")):
                return
            sid = c["series_id"]
            bd = c.get("backdrop_path")
            out["series"].append({
                "id": make_id("s", "xt", sid), "name": clean(c.get("name"), 160) or "Series",
                "cat": cat("series", c.get("category_id")), "img": http_url(c.get("cover") or ""),
                "plot": clean_long(c.get("plot")), "genre": clean(c.get("genre"), 120),
                "year": to_int(str(c.get("releaseDate") or c.get("release_date") or "")[:4], 0),
                "rating": to_float(c.get("rating"), 0),
                "backdrop": http_url((bd[0] if isinstance(bd, list) and bd else bd) or "") if not isinstance(bd, dict) else "",
                "native": str(sid), "loaded": False})

        # one list at a time, so only one raw reply is in memory at once. If one fails, nothing is
        # replaced (the caller keeps the old catalogue); the error says what had loaded fine.
        done = []
        for what, action, handle in (("live streams", "get_live_streams", live), ("movies", "get_vod_streams", movie),
                                     ("series", "get_series", series)):
            try:
                await self._each(http, what, action, handle)
            except ProviderError as e:
                if done:
                    raise ProviderError(f"{e} - {' and '.join(done)} had loaded fine; nothing was replaced") from None
                raise
            done.append(what)
        self.stats = {k: tuple(v) for k, v in stats.items()}
        return out

    async def movie_info(self, http, item) -> dict:
        data = await self._get(http, "movie info", action="get_vod_info", vod_id=item.get("native", ""))
        info = (data or {}).get("info") or {} if isinstance(data, dict) else {}
        if not isinstance(info, dict):
            return {}
        bd = info.get("backdrop_path")
        return {
            "plot": clean_long(info.get("plot") or info.get("description")),
            "genre": clean(info.get("genre"), 120), "cast": clean(info.get("cast") or info.get("actors"), 300),
            "director": clean(info.get("director"), 120),
            "dur": to_int(info.get("duration_secs"), 0),
            "year": to_int(str(info.get("releasedate") or info.get("release_date") or "")[:4], 0),
            "rating": to_float(info.get("rating"), 0),
            "backdrop": http_url((bd[0] if isinstance(bd, list) and bd else bd) or "") if not isinstance(bd, dict) else "",
            "img2": http_url(info.get("movie_image") or info.get("cover_big") or ""),
        }

    async def series_info(self, http, series) -> tuple[dict, list]:
        data = await self._get(http, "series info", action="get_series_info", series_id=series.get("native", ""))
        if not isinstance(data, dict):
            return {}, []
        info = data.get("info") if isinstance(data.get("info"), dict) else {}
        eps = []
        episodes = data.get("episodes") or {}
        if isinstance(episodes, list):                      # some panels send a list of lists
            episodes = {str(i + 1): v for i, v in enumerate(episodes)}
        for season_key, lst in episodes.items():
            for e in lst or []:
                if not isinstance(e, dict) or e.get("id") in (None, ""):
                    continue
                einfo = e.get("info") if isinstance(e.get("info"), dict) else {}
                ext = clean(e.get("container_extension"), 5).lower() or "mp4"
                season = to_int(e.get("season"), to_int(season_key, 1))
                epn = to_int(e.get("episode_num"), 0)
                eps.append({
                    "id": make_id("e", "xt", e["id"]), "kind": "episode", "series": series["id"],
                    "season": season, "episode": epn,
                    "name": _ep_title(series.get("name", ""), clean(e.get("title"), 160)) or f"Episode {epn}",
                    "plot": clean_long(einfo.get("plot")), "dur": to_int(einfo.get("duration_secs"), 0),
                    "img": http_url(einfo.get("movie_image") or ""), "url": self.stream_url("series", e["id"], ext),
                    "ext": ext, "ua": "", "ref": "", "native": str(e["id"]), "cat": series.get("cat", "")})
        bd = info.get("backdrop_path")
        meta = {
            "plot": clean_long(info.get("plot")), "genre": clean(info.get("genre"), 120),
            "cast": clean(info.get("cast"), 300),
            "backdrop": http_url((bd[0] if isinstance(bd, list) and bd else bd) or "") if not isinstance(bd, dict) else "",
        }
        return meta, eps


def make_provider(s: Settings):
    return XtreamProvider(s) if s.provider_type == "xtream" else M3UProvider(s)
