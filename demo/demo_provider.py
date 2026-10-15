"""signal-tv-classic-demo: a tiny local IPTV provider used until a real one is configured.

127.0.0.1:8791 only. Serves legal content: locally generated test patterns (see
make-media.sh) plus a few public test streams (Mux, Apple, Blender Foundation).

  /playlist.m3u               extended M3U (live + movies + series)
  /epg.xml.gz                 XMLTV guide for the local channels
  /live/<n>.ts                endless MPEG-TS feed (ffmpeg -re -stream_loop -1 -c copy)
  /vod/<file>                 test files with Range support
  /player_api.php, /xmltv.php, /live|movie|series/<u>/<p>/<id>.<ext>
                              Xtream Codes emulation (username demo / password demo)
"""
from __future__ import annotations

import asyncio
import gzip
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

MEDIA = Path(os.environ.get("DEMO_MEDIA", "/opt/signal-tv-classic/demo/media"))
BASE = os.environ.get("DEMO_BASE", "http://127.0.0.1:8791")
USER, PASS = "demo", "demo"

LIVE = [  # id, name, file, group, epg id
    (1, "Demo One", "live1.ts", "Demo Channels", "demo1.local"),
    (2, "Demo Two", "live2.ts", "Demo Channels", "demo2.local"),
    (3, "Demo Three (MPEG-2)", "live3.ts", "Demo Channels", "demo3.local"),
    (4, "Demo Four (AC-3)", "live4.ts", "Demo Channels", "demo4.local"),
]
PUBLIC_LIVE = [  # HLS sources, played through the same restream path
    (101, "Mux Test Stream (HLS)", "https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8", "Public Test Streams", ""),
    (102, "Apple BipBop (HLS)", "https://devstreaming-cdn.apple.com/videos/streaming/examples/img_bipbop_adv_example_ts/v5/prog_index.m3u8",
     "Public Test Streams", ""),
]
MOVIES = [  # id, name, file-or-url, ext, group, year, plot
    (201, "Test Pattern Feature (2026)", "movie1.mp4", "mp4", "Demo Movies", 2026,
     "Three minutes of moving test pattern. MP4 H.264/AAC - plays by direct byte-range proxy."),
    (202, "Colour Bars (2026)", "movie2.mkv", "mkv", "Demo Movies", 2026,
     "SMPTE bars in Matroska with AC-3 5.1 audio - exercises the remux-to-HLS path."),
    (203, "Late Moov (2026)", "movie3.mp4", "mp4", "Demo Movies", 2026,
     "An MP4 whose index sits at the end of the file, a classic Safari range-request test."),
    (204, "Tears of Steel (2012)", "https://download.blender.org/demo/movies/ToS/tears_of_steel_720p.mov", "mov",
     "Open Movies", 2012, "Blender Foundation open movie (CC BY 3.0). Public test file, proxied with Range support."),
]
SERIES = [  # series id, name, group, [(episode id, season, ep, title, file, ext)]
    (301, "Demo Series", "Demo Shows", [
        (3011, 1, 1, "Pilot", "series_s01e01.mkv", "mkv"),
        (3012, 1, 2, "The Second One", "series_s01e02.mkv", "mkv"),
        (3013, 1, 3, "Finale", "series_s01e03.mkv", "mkv"),
        (3021, 2, 1, "Return", "series_s02e01.mp4", "mp4"),
        (3022, 2, 2, "Bars Again", "series_s02e02.mp4", "mp4"),
    ]),
]
LOGO = BASE + "/logo/{}.png"

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def _src(f: str) -> str:
    return f if f.startswith("http") else f"{BASE}/vod/{f}"


@app.get("/playlist.m3u")
async def playlist():
    out = ['#EXTM3U url-tvg="' + BASE + '/epg.xml.gz"']
    for i, name, f, group, epg in LIVE:
        out += [f'#EXTINF:-1 tvg-id="{epg}" tvg-name="{name}" tvg-logo="{LOGO.format(i)}" tvg-chno="{i}" group-title="{group}",{name}',
                f"{BASE}/live/{i}.ts"]
    for i, name, url, group, _ in PUBLIC_LIVE:
        out += [f'#EXTINF:-1 tvg-id="" tvg-logo="{LOGO.format(i)}" group-title="{group}",{name}', url]
    for i, name, f, ext, group, year, _ in MOVIES:
        out += [f'#EXTINF:-1 tvg-logo="{LOGO.format(i)}" group-title="Movies: {group}",{name}', _src(f)]
    for sid, sname, group, eps in SERIES:
        for eid, s, e, title, f, ext in eps:
            out += [f'#EXTINF:-1 tvg-logo="{LOGO.format(sid)}" group-title="Series: {group}",{sname} S{s:02d}E{e:02d} {title}',
                    _src(f)]
    return Response("\n".join(out) + "\n", media_type="audio/x-mpegurl")


def _xmltv() -> bytes:
    now = int(time.time()) // 1800 * 1800
    shows = ["Morning Patterns", "Bars & Tones", "The Signal Hour", "Test Card Theatre", "Night Loop", "Calibration Live"]
    rows = ['<?xml version="1.0" encoding="UTF-8"?>', '<tv generator-info-name="signal-tv-classic-demo">']
    for i, name, _, _, epg in LIVE:
        rows.append(f'<channel id="{epg}"><display-name>{escape(name)}</display-name></channel>')
    for i, name, _, _, epg in LIVE:
        for k in range(-4, 72):
            st = now + k * 1800
            title = shows[(k + i) % len(shows)]
            f = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y%m%d%H%M%S +0000")  # noqa: E731
            rows.append(f'<programme start="{f(st)}" stop="{f(st + 1800)}" channel="{epg}">'
                        f'<title lang="en">{escape(title)}</title>'
                        f'<desc lang="en">{escape(name)} - episode {k + 10}. Generated demo guide data.</desc></programme>')
    rows.append("</tv>")
    return gzip.compress("\n".join(rows).encode())


@app.get("/epg.xml.gz")
async def epg():
    return Response(_xmltv(), media_type="application/gzip")


@app.get("/logo/{n}.png")
async def logo(n: int):
    p = MEDIA / f"logo{n}.png"
    return FileResponse(p) if p.is_file() else Response(status_code=404)


async def _live_feed(n: int):
    item = next((x for x in LIVE if x[0] == n), None)
    if not item:
        return Response(status_code=404)
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-re", "-stream_loop", "-1",
        "-i", str(MEDIA / item[2]), "-c", "copy", "-f", "mpegts", "pipe:1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, stdin=asyncio.subprocess.DEVNULL)

    async def body():
        try:
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()

    return StreamingResponse(body(), media_type="video/mp2t")


@app.get("/live/{n}.ts")
async def live(n: int):
    return await _live_feed(n)


@app.api_route("/vod/{name}", methods=["GET", "HEAD"])
async def vod(name: str):
    p = (MEDIA / name).resolve()
    if p.parent != MEDIA.resolve() or not p.is_file():
        return Response(status_code=404)
    return FileResponse(p, media_type="video/mp4" if p.suffix == ".mp4" else "video/x-matroska")


# ------------------------------------------------------------- Xtream emulation
def _auth(u: str, p: str) -> bool:
    return u == USER and p == PASS


@app.get("/player_api.php")
async def player_api(request: Request):
    q = request.query_params
    if not _auth(q.get("username", ""), q.get("password", "")):
        return JSONResponse({"user_info": {"auth": 0}})
    a = q.get("action", "")
    if a == "":
        return {"user_info": {"auth": 1, "status": "Active", "username": USER, "max_connections": "2"},
                "server_info": {"url": "127.0.0.1", "port": "8791"}}
    if a == "get_live_categories":
        return [{"category_id": "1", "category_name": "Demo Channels"}, {"category_id": "2", "category_name": "Public Test Streams"}]
    if a == "get_vod_categories":
        return [{"category_id": "10", "category_name": "Demo Movies"}, {"category_id": "11", "category_name": "Open Movies"}]
    if a == "get_series_categories":
        return [{"category_id": "20", "category_name": "Demo Shows"}]
    if a == "get_live_streams":
        return [{"num": i, "name": n, "stream_id": i, "stream_icon": LOGO.format(i), "epg_channel_id": e,
                 "category_id": "1"} for i, n, _, _, e in LIVE]
    if a == "get_vod_streams":
        return [{"num": k + 1, "name": n, "stream_id": i, "stream_icon": LOGO.format(i), "rating_5based": 4,
                 "category_id": "11" if f.startswith("http") else "10", "container_extension": ext, "added": str(1700000000 + i)}
                for k, (i, n, f, ext, _, _, _) in enumerate(MOVIES)]
    if a == "get_vod_info":
        m = next((x for x in MOVIES if str(x[0]) == q.get("vod_id")), None)
        if not m:
            return {"info": [], "movie_data": {}}
        return {"info": {"plot": m[6], "genre": "Test", "releasedate": f"{m[5]}-01-01", "rating": "8",
                         "movie_image": LOGO.format(m[0])},
                "movie_data": {"stream_id": m[0], "name": m[1], "container_extension": m[3]}}
    if a == "get_series":
        return [{"num": 1, "series_id": sid, "name": n, "cover": LOGO.format(sid), "plot": "A demo series of test patterns.",
                 "genre": "Test", "releaseDate": "2026-01-01", "rating": "7", "category_id": "20"}
                for sid, n, _, _ in SERIES]
    if a == "get_series_info":
        s = next((x for x in SERIES if str(x[0]) == q.get("series_id")), None)
        if not s:
            return {"episodes": {}, "info": {}}
        eps: dict = {}
        for eid, se, e, title, f, ext in s[3]:
            eps.setdefault(str(se), []).append({"id": str(eid), "episode_num": e, "title": f"{s[1]} S{se:02d}E{e:02d} - {title}",
                                                "container_extension": ext, "season": se,
                                                "info": {"duration_secs": 60, "plot": f"Episode {e} of season {se}."}})
        return {"seasons": [], "info": {"name": s[1], "plot": "A demo series of test patterns.", "genre": "Test"},
                "episodes": eps}
    return JSONResponse([])


@app.get("/xmltv.php")
async def xmltv(request: Request):
    if not _auth(request.query_params.get("username", ""), request.query_params.get("password", "")):
        return Response(status_code=401)
    return Response(gzip.decompress(_xmltv()), media_type="application/xml")


@app.get("/live/{u}/{p}/{sid}.ts")
async def xt_live(u: str, p: str, sid: int):
    if not _auth(u, p):
        return Response(status_code=401)
    return await _live_feed(sid)


@app.api_route("/movie/{u}/{p}/{name}", methods=["GET", "HEAD"])
async def xt_movie(u: str, p: str, name: str):
    if not _auth(u, p):
        return Response(status_code=401)
    sid = name.split(".", 1)[0]
    m = next((x for x in MOVIES if str(x[0]) == sid), None)
    if not m:
        return Response(status_code=404)
    if m[2].startswith("http"):
        return Response(status_code=302, headers={"Location": m[2]})
    return await vod(m[2])


@app.api_route("/series/{u}/{p}/{name}", methods=["GET", "HEAD"])
async def xt_series(u: str, p: str, name: str):
    if not _auth(u, p):
        return Response(status_code=401)
    eid = name.split(".", 1)[0]
    for _, _, _, eps in SERIES:
        for e in eps:
            if str(e[0]) == eid:
                return await vod(e[4])
    return Response(status_code=404)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8791, access_log=False, server_header=False, log_level="warning")
