"""Playback engine.

Live:  one ffmpeg per channel, shared by every viewer of that channel.
       provider stream -> ffmpeg (video copied, audio -> AAC) -> short sliding HLS
       window in RAM (/dev/shm). Non-H.264 video (e.g. MPEG-2) is transcoded.
VOD:   ffprobe once; MP4/MOV with H.264 + AAC/MP3 is byte-range proxied (native
       seeking). Anything else is remuxed (video copied) to HLS from an offset.

Jobs are reaped when nobody has fetched a segment / heartbeat for a while, on
shutdown, and the work directories are wiped at start, so no orphans remain.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import shutil
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from . import livemeta, tracks
from .util import log, redact

PROTOCOLS = "http,https,tls,tcp,crypto"
LIVE_IDLE = 35           # s without any segment/playlist fetch or heartbeat
LIVE_PAUSED_MAX = 300    # s with heartbeats but no media fetches (paused live)
VOD_IDLE = 150
READY_TIMEOUT = 30
PROBE_TTL = 6 * 3600
LIVE_SEGMENTS = 15       # live playlist window (x ~2 s): room for a ~10 s cushion behind the edge + recovery
TRANSCODE_GUARD_AFTER = 20   # s: a live transcode is judged on its speed after this warm-up...
TRANSCODE_GUARD_SPEED = 0.95  # ...and stopped if it stays below this (it would only ever buffer)
COPY_VIDEO_VOD = {"h264", "hevc"}
DIRECT_AUDIO = {"aac", "mp3"}
_VIDEO_LINE = re.compile(r"Stream #0:\d+.*?: Video: (\w+)")
_AUDIO_LINE = re.compile(r"Stream #0:\d+.*?: Audio: (\w+)")


@dataclass
class Job:
    id: str
    key: str
    kind: str                      # live | vod
    item_id: str
    dir: Path
    url: str
    ua: str
    ref: str
    offset: float = 0.0
    hevc_ok: bool = True
    transcode: bool = False
    proc: asyncio.subprocess.Process | None = None
    started: float = field(default_factory=time.time)
    last_media: float = field(default_factory=time.time)
    last_beat: float = field(default_factory=time.time)
    viewers: dict = field(default_factory=dict)
    state: str = "starting"        # starting | ready | failed | ended
    error: str = ""
    vcodec: str = ""
    acodec: str = ""
    interlaced: bool = False
    restarts: int = 0
    seq: int = 0
    tail: list = field(default_factory=list)
    stopping: bool = False
    audio: int | None = None       # VOD: audio stream (0:a:n) to play; None = first
    sub: int | None = None         # VOD: subtitle stream (0:s:n) converted to WebVTT; None = none
    origin: float | None = None    # VOD: source time at media time 0 (see Engine.origin)
    has_audio: bool = True
    sub_track: dict = field(default_factory=dict)   # the chosen subtitle's labels (for the master playlist)
    container: str = "ts"          # live: "ts" (H.264, as always) or "fmp4" (HEVC copy: Safari needs fMP4 for HEVC)
    decision: dict = field(default_factory=dict)    # live: what was chosen and why (diagnostics)
    speeds: list = field(default_factory=list)      # live transcode: recent ffmpeg speed readings

    @property
    def playlist(self) -> str:
        """Media playlist name: with a subtitle the remux writes a master playlist + index_0.m3u8."""
        return "index_0.m3u8" if self.sub is not None else "index.m3u8"

    def touch(self, client: str | None = None, media: bool = True) -> None:
        now = time.time()
        self.last_beat = now
        if media:
            self.last_media = now
        if client:
            self.viewers[client] = now


class Engine:
    def __init__(self, settings):
        self.s = settings
        self.jobs: dict[str, Job] = {}
        self.by_key: dict[str, str] = {}
        self.probes: dict[str, dict] = {}
        self.recent: dict[str, dict] = {}  # item id -> how its last job ended (for diagnostics; last 50)
        self.on_meta = None                # callback(item_id, meta | None, **notes): formats learned from live jobs
        self._start_lock = asyncio.Lock()
        self._reaper: asyncio.Task | None = None
        self._watchers: set[asyncio.Task] = set()

    # ------------------------------------------------------------ lifecycle
    def prepare(self) -> None:
        for d in (self.s.live_dir, self.s.vod_dir):
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
            d.mkdir(parents=True, exist_ok=True)
            os.chmod(d, 0o700)
        self._reaper = asyncio.create_task(self._reap_loop())

    async def shutdown(self) -> None:
        if self._reaper:
            self._reaper.cancel()
        await asyncio.gather(*(self.stop(j.id, "shutdown") for j in list(self.jobs.values())),
                             return_exceptions=True)
        for d in (self.s.live_dir, self.s.vod_dir):
            shutil.rmtree(d, ignore_errors=True)

    def stats(self) -> list[dict]:
        now = time.time()
        return [{"job": j.id, "kind": j.kind, "item": j.item_id, "state": j.state, "transcode": j.transcode,
                 "viewers": sum(1 for t in j.viewers.values() if now - t < LIVE_IDLE),
                 "age": int(now - j.started), "idle": int(now - j.last_media), "restarts": j.restarts}
                for j in self.jobs.values()]

    def forget_old(self, now: float) -> None:
        for key in [k for k, p in self.probes.items() if now - p["t"] > PROBE_TTL]:
            del self.probes[key]

    # ------------------------------------------------------------- probing
    async def probe(self, item: dict) -> dict:
        cached = self.probes.get(item["id"])
        if cached and time.time() - cached["t"] < PROBE_TTL:
            return cached
        cmd = ["ffprobe", "-v", "error", "-hide_banner", "-protocol_whitelist", PROTOCOLS,
               "-rw_timeout", "15000000", "-user_agent", item.get("ua") or self.s.user_agent]
        if item.get("ref"):
            cmd += ["-headers", f"Referer: {item['ref']}\r\n"]
        cmd += ["-show_entries", "format=format_name,duration:stream=index,codec_type,codec_name,channels,height,"
                "width,field_order:stream_tags=language,title,handler_name:"
                "stream_disposition=default,forced,hearing_impaired,visual_impaired,comment",
                "-of", "json", item["url"]]
        info = {"t": time.time(), "ok": False}
        try:
            p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                     stderr=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.DEVNULL)
            try:
                out, err = await asyncio.wait_for(p.communicate(), 30)
            except asyncio.TimeoutError:
                p.kill()
                await p.wait()
                raise RuntimeError("probe timed out")
            data = json.loads(out or b"{}")
            fmt = data.get("format") or {}
            streams = data.get("streams") or []
            v = next((s for s in streams if s.get("codec_type") == "video" and s.get("codec_name") not in ("mjpeg", "png")), {})
            a = next((s for s in streams if s.get("codec_type") == "audio"), {})
            audio, subs = tracks.describe(streams)
            info.update(ok=bool(streams), fmt=fmt.get("format_name", ""), dur=float(fmt.get("duration") or 0),
                        v=v.get("codec_name", ""), a=a.get("codec_name", ""), h=int(v.get("height") or 0),
                        w=int(v.get("width") or 0), interlaced=v.get("field_order") in ("tt", "bb", "tb", "bt"),
                        audio=audio, subs=subs)
            if not streams:
                info["err"] = redact(err.decode(errors="replace").strip().splitlines()[-1:] or ["no streams"])
        except Exception as e:  # noqa: BLE001
            info["err"] = redact(e)
            log.info("probe failed for %s: %s", item["id"], info["err"])
        if info["ok"]:
            self.probes[item["id"]] = info
        return info

    @staticmethod
    def vod_mode(item: dict, probe: dict, hevc_ok: bool) -> str:
        """'direct' (byte-range proxy) or 'hls' (remux)."""
        if not probe.get("ok"):
            return "direct" if item.get("ext") in ("mp4", "m4v", "mov") else "hls"
        isomp4 = "mp4" in probe["fmt"] or "mov" in probe["fmt"]
        audio_ok = probe["a"] in DIRECT_AUDIO or not probe["a"]
        if isomp4 and audio_ok and (probe["v"] == "h264" or (probe["v"] == "hevc" and hevc_ok)):
            return "direct"
        return "hls"

    # ---------------------------------------------------------------- jobs
    def find(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    async def start_live(self, item: dict, client: str, transcode: bool = False, container: str = "ts",
                         decision: dict | None = None) -> Job:
        return await self._start("live", item, client, 0.0, True, live_mode=(transcode, container, decision or {}))

    async def probe_live(self, item: dict) -> dict | None:
        """Format of a live channel: one short provider connection (~1-2 s), closed before returning."""
        cmd = ["ffprobe", "-v", "error", "-hide_banner", "-protocol_whitelist", PROTOCOLS, "-rw_timeout", "10000000",
               "-user_agent", item.get("ua") or self.s.user_agent, "-analyzeduration", "4000000", "-probesize", "6000000"]
        if item.get("ref"):
            cmd += ["-headers", f"Referer: {item['ref']}\r\n"]
        # the first 2 s of packets too: their durations tell field-coded interlacing (one packet per
        # field) from frame-coded (livemeta.scan_meta)
        cmd += ["-read_intervals", "%+2", "-show_entries", "stream=codec_type,codec_name,profile,pix_fmt,width,height,"
                "r_frame_rate,avg_frame_rate,field_order,color_transfer,channels:packet=stream_index,duration_time",
                "-of", "json", item["url"]]
        try:
            p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                     stdin=asyncio.subprocess.DEVNULL)
            try:
                out, err = await asyncio.wait_for(p.communicate(), 20)
            except asyncio.TimeoutError:
                p.kill()
                await p.wait()
                raise RuntimeError("probe timed out")
            data = json.loads(out or b"{}")
        except (OSError, ValueError, RuntimeError) as e:
            log.info("live probe %s failed: %s", item["id"], redact(e))
            return None
        meta = livemeta.meta_from_probe(data.get("streams") or [], data.get("packets") or [])
        return meta if meta.get("v") else None

    async def analyze_file(self, path: Path) -> dict | None:
        """The same facts from a segment this app already wrote (local file, no provider connection)."""
        cmd = ["ffprobe", "-v", "error", "-hide_banner", "-show_entries",
               "stream=codec_type,codec_name,profile,pix_fmt,width,height,r_frame_rate,avg_frame_rate,field_order,"
               "color_transfer,channels:packet=stream_index,duration_time", "-of", "json", str(path)]
        try:
            p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                                                     stdin=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(p.communicate(), 10)
            data = json.loads(out or b"{}")
        except (OSError, ValueError, asyncio.TimeoutError):
            return None
        meta = livemeta.meta_from_probe(data.get("streams") or [], data.get("packets") or [])
        return meta if meta.get("v") else None

    async def start_vod(self, item: dict, client: str, offset: float, hevc_ok: bool,
                        audio: int | None = None, sub: int | None = None) -> Job:
        return await self._start("vod", item, client, max(0.0, offset), hevc_ok, audio, sub)

    async def _start(self, kind: str, item: dict, client: str, offset: float, hevc_ok: bool,
                     audio: int | None = None, sub: int | None = None, live_mode=None) -> Job:
        transcode, container, decision = live_mode or (False, "ts", {})
        key = f"{kind}:{item['id']}" + (f":{int(offset)}:{int(hevc_ok)}:{audio}:{sub}" if kind == "vod"
                                        else f":{container}:{int(transcode)}")
        async with self._start_lock:
            jid = self.by_key.get(key)
            job = self.jobs.get(jid) if jid else None
            if job and job.state in ("starting", "ready"):
                job.touch(client)
                return job
            if job:
                await self.stop(job.id, "replaced")
            limit = self.s.max_live if kind == "live" else self.s.max_vod
            same = [j for j in self.jobs.values() if j.kind == kind]
            if len(same) >= limit:
                # evict the job that has been quiet the longest (a viewer that went away)
                victim = min(same, key=lambda j: j.last_media)
                busy = time.time() - victim.last_media < 8 and any(time.time() - t < 8 for t in victim.viewers.values())
                if busy:
                    raise RuntimeError(f"all {limit} {kind} stream slots are in use - stop another player first")
                await self.stop(victim.id, "evicted")
            jid = secrets.token_hex(6)
            base = self.s.live_dir if kind == "live" else self.s.vod_dir
            job = Job(id=jid, key=key, kind=kind, item_id=item["id"], dir=base / jid, url=item["url"],
                      ua=item.get("ua") or self.s.user_agent, ref=item.get("ref") or "", offset=offset,
                      hevc_ok=hevc_ok, audio=audio, sub=sub)
            if kind == "vod":
                pr = self.probes.get(item["id"]) or {}
                job.vcodec, job.acodec, job.interlaced = pr.get("v", ""), pr.get("a", ""), pr.get("interlaced", False)
                job.has_audio = bool(pr.get("audio", True))
                job.transcode = bool(job.vcodec) and (job.vcodec not in COPY_VIDEO_VOD or (job.vcodec == "hevc" and not hevc_ok))
            else:
                job.transcode, job.container, job.decision = transcode, container, decision
                job.vcodec = (decision.get("meta") or {}).get("v", "")
                job.interlaced = bool((decision.get("meta") or {}).get("interlaced"))
            job.dir.mkdir(mode=0o700)
            job.touch(client)
            self.jobs[jid] = job
            self.by_key[key] = jid
            await self._spawn(job)
        return job

    def _cmd(self, job: Job) -> list[str]:
        cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-loglevel", "info",
               "-protocol_whitelist", PROTOCOLS, "-user_agent", job.ua]
        if job.kind == "live" and job.transcode:
            cmd += ["-progress", "pipe:2", "-stats_period", "2"]      # speed= lines for the realtime guard
        if job.ref:
            cmd += ["-headers", f"Referer: {job.ref}\r\n"]
        if job.kind == "live":
            cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_on_network_error", "1",
                    "-reconnect_delay_max", "4", "-rw_timeout", "15000000",
                    "-fflags", "+genpts+discardcorrupt", "-analyzeduration", "4000000", "-probesize", "4000000"]
            if urlsplit(job.url).path.lower().endswith(".m3u8"):
                cmd += ["-re"]          # HLS sources: pace at real time (a VOD-style playlist would otherwise race to its end)
        else:
            cmd += ["-reconnect", "1", "-reconnect_on_network_error", "1", "-reconnect_delay_max", "6",
                    "-rw_timeout", "20000000", "-readrate", "3"]
            if job.offset > 0:
                cmd += ["-ss", f"{job.offset:.3f}"]
        audio = f"0:a:{job.audio}" if job.audio else "0:a:0?"
        cmd += ["-i", job.url, "-map", "0:v:0?", "-map", audio]
        if job.sub is not None:
            cmd += ["-map", f"0:s:{job.sub}", "-c:s", "webvtt"]
        else:
            cmd += ["-sn"]
        cmd += ["-dn", "-max_muxing_queue_size", "2048"]
        if job.transcode and job.kind == "live" and job.interlaced:
            # every field deinterlaced into a frame (bwdif): 50p keeps the motion sport needs. x264
            # ultrafast is what this Pi sustains for 1080i25 -> 720p50 (1.69x; veryfast 1.23x,
            # yadif -> 720p25 veryfast 1.35x): measurements in livemeta.DEINT_SPEED
            cmd += ["-vf", "bwdif=mode=send_field:deint=interlaced,scale=w='min(1280,iw)':h=-2",
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "22", "-maxrate", "4500k", "-bufsize", "9M",
                    "-pix_fmt", "yuv420p", "-g", "100", "-threads", "3"]
        elif job.transcode:
            vf = ["yadif=deint=interlaced"] if (job.interlaced or job.vcodec == "mpeg2video") else []
            vf.append("scale=w='min(1280,iw)':h=-2")
            cmd += ["-vf", ",".join(vf), "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
                    "-maxrate", "4500k", "-bufsize", "9M", "-pix_fmt", "yuv420p",
                    "-g", "50" if job.kind == "live" else "100", "-threads", "3"]
        else:
            cmd += ["-c:v", "copy"]
            if job.vcodec == "hevc":
                cmd += ["-tag:v", "hvc1"]                           # what Safari / iOS require for HEVC
        cmd += ["-c:a", "aac", "-b:a", "160k", "-ac", "2", "-ar", "48000"]
        if job.kind == "live":
            flags = "delete_segments+independent_segments+temp_file+omit_endlist"
            if job.restarts:
                flags += "+discont_start"
            cmd += ["-f", "hls", "-hls_time", "2", "-hls_list_size", str(LIVE_SEGMENTS), "-hls_delete_threshold", "4",
                    "-hls_flags", flags, "-start_number", str(job.seq)]
            if job.container == "fmp4":
                # HEVC copied unchanged: fMP4 segments (Safari plays HEVC only from fMP4); a new init
                # file name after each restart so players reload it at the discontinuity
                cmd += ["-hls_segment_type", "fmp4", "-hls_fmp4_init_filename", f"init_{job.restarts}.mp4",
                        "-hls_segment_filename", "seg_%05d.m4s", "index.m3u8"]
            else:
                cmd += ["-hls_segment_filename", "seg_%05d.ts", "index.m3u8"]
        else:
            cmd += ["-f", "hls", "-hls_time", "4", "-hls_playlist_type", "event", "-hls_segment_type", "fmp4",
                    "-hls_fmp4_init_filename", "init.mp4", "-hls_flags", "independent_segments+temp_file"]
            if job.sub is not None:
                # One HLS output carrying video, audio and the subtitle (as a WebVTT rendition): the
                # muxer then shifts all three by the same amount, so cues stay in step with the
                # picture after a seek. (A separate WebVTT output is shifted differently - by up to a
                # GOP - which is the "subtitles ~2 s out" effect.) ffmpeg 5.1 allows one WebVTT
                # rendition per output, so changing subtitle restarts the remux.
                cmd += ["-master_pl_name", "master.m3u8", "-var_stream_map",
                        "v:0,a:0,s:0,sgroup:subs" if job.has_audio else "v:0,s:0,sgroup:subs",
                        "-hls_segment_filename", "seg_%v_%05d.m4s", "index_%v.m3u8"]
            else:
                cmd += ["-hls_segment_filename", "seg_%05d.m4s", "index.m3u8"]
        return cmd

    async def _spawn(self, job: Job) -> None:
        if not (job.restarts and job.state == "ready"):
            job.state = "starting"
        job.error = ""
        job.proc = await asyncio.create_subprocess_exec(
            *self._cmd(job), cwd=job.dir, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            start_new_session=True)
        log.info("job %s: start %s %s%s%s", job.id, job.kind, job.item_id,
                 " transcode" if job.transcode else "", f" @{int(job.offset)}s" if job.offset else "")
        task = asyncio.create_task(self._watch(job, job.proc))
        self._watchers.add(task)
        task.add_done_callback(self._watchers.discard)

    async def _watch(self, job: Job, proc) -> None:
        seen_output = False
        assert proc.stderr
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            text = line.decode(errors="replace").rstrip()
            if text.startswith("Output #0"):
                seen_output = True
            if text.startswith("speed="):
                if await self._speed_guard(job, proc, text):
                    return
                continue
            if "=" in text and " " not in text and job.kind == "live" and job.transcode:
                continue                                    # other -progress key=value lines
            if not seen_output:
                m = _VIDEO_LINE.search(text)
                if m:
                    seen = m.group(1)
                    if job.kind == "live":
                        meta = livemeta.meta_from_ffmpeg_line(text)
                        if meta and self.on_meta:
                            self.on_meta(job.item_id, meta)
                        if not job.transcode and job.container == "ts" and seen != "h264":
                            # the channel no longer sends what it did when it was chosen: stop, so the
                            # player retries and the choice is made again with the new format
                            log.warning("job %s: expected H.264, the channel now sends %s", job.id, seen)
                            job.state, job.error = "failed", "This channel's format has changed - please try again."
                            await self._kill(proc)
                            return
                    if not job.vcodec:
                        job.vcodec = seen
                    job.interlaced = job.interlaced or "top first" in text or "bottom first" in text or "top coded first" in text
                m = _AUDIO_LINE.search(text)
                if m and not job.acodec:
                    job.acodec = m.group(1)
            if "Opening '" in text or (text.startswith("  ") and seen_output):
                continue
            job.tail = (job.tail + [redact(text)])[-15:]
        rc = await proc.wait()
        if job.stopping or job.proc is not proc:
            return
        if job.kind == "vod" and rc == 0:
            job.state = "ready" if self._segments(job) else "failed"
            log.info("job %s: remux finished", job.id)
            return
        msg = self._friendly(job.tail)
        log.warning("job %s: ffmpeg exited %s: %s", job.id, rc, " | ".join(job.tail[-3:]))
        if job.kind == "live" and job.state == "ready" and job.restarts < 4 and job.viewers:
            job.restarts += 1
            job.seq = self._last_seq(job) + 1
            await asyncio.sleep(min(2 * job.restarts, 6))
            if not job.stopping and job.id in self.jobs:
                log.info("job %s: restarting (attempt %d)", job.id, job.restarts)
                await self._spawn(job)
            return
        job.state, job.error = "failed", msg

    async def _speed_guard(self, job: Job, proc, text: str) -> bool:
        """Live transcode realtime guard. Live input arrives at real time, so a transcode that keeps
        up reads ~1.0x; one that stays below TRANSCODE_GUARD_SPEED after the warm-up can only ever
        buffer: stop it (the player shows why) and remember the measured speed so this channel is
        not converted again. Returns True when the job was stopped."""
        try:
            speed = float(text.split("=", 1)[1].rstrip("x").strip())
        except ValueError:
            return False
        job.speeds = (job.speeds + [speed])[-5:]
        if time.time() - job.started < TRANSCODE_GUARD_AFTER or len(job.speeds) < 3:
            return False
        if all(sp < TRANSCODE_GUARD_SPEED for sp in job.speeds[-3:]):
            avg = round(sum(job.speeds[-3:]) / 3, 2)
            log.warning("job %s: transcode only %.2fx real time - stopping it (it can only buffer)", job.id, avg)
            if self.on_meta:
                self.on_meta(job.item_id, None, measured_speed=avg)
            job.state = "failed"
            job.error = (f"The Pi can only convert this channel at {avg}x real time, so it would keep buffering. "
                         "Choose another version from the quality menu.")
            await self._kill(proc)
            return True
        return False

    @staticmethod
    def _friendly(tail: list[str]) -> str:
        t = " ".join(tail).lower()
        for needle, msg in (("401", "The provider refused the login (HTTP 401)."),
                            ("403", "The provider refused this stream (HTTP 403) - check the subscription or connection limit."),
                            ("404", "The provider says this stream does not exist (HTTP 404)."),
                            ("458", "The provider's connection limit was reached."),
                            ("509", "The provider's connection limit was reached."),
                            ("connection refused", "Could not connect to the provider."),
                            ("timed out", "The provider did not respond in time."),
                            ("i/o error", "The provider closed the stream."),
                            ("invalid data", "The provider sent data that is not a playable stream.")):
            if needle in t:
                return msg
        return "The stream stopped unexpectedly."

    def _playlist(self, job: Job) -> str:
        try:
            return (job.dir / job.playlist).read_text()
        except FileNotFoundError:
            return ""

    async def origin(self, job: Job) -> float:
        """Source time shown at media time 0 of a remux (player position = origin + currentTime).

        The video is copied, so the remux really starts at the keyframe before the requested
        offset, and the muxer then shifts timestamps; media time 0 is therefore up to a GOP
        (often 2-5 s) before the offset - taking the offset itself put the shown/saved position
        that far out. The audio is re-encoded and starts exactly at the offset, so
        origin = offset - (media time of the first audio sample). Read locally from the first
        segment: no provider request."""
        if job.origin is not None:
            return job.origin
        job.origin = job.offset
        if job.offset <= 0:
            job.origin = 0.0
            return job.origin
        segs = sorted(p.name for p in job.dir.glob("seg_*.m4s"))
        if not segs or not (job.dir / "init.mp4").is_file():
            return job.origin
        cmd = ["ffprobe", "-v", "error", "-protocol_whitelist", "file,concat", "-show_entries",
               "stream=codec_type,start_time", "-of", "json", f"concat:init.mp4|{segs[0]}"]
        try:
            p = await asyncio.create_subprocess_exec(*cmd, cwd=job.dir, stdout=asyncio.subprocess.PIPE,
                                                     stderr=asyncio.subprocess.DEVNULL, stdin=asyncio.subprocess.DEVNULL)
            try:
                out, _ = await asyncio.wait_for(p.communicate(), 10)
            except asyncio.TimeoutError:
                p.kill()
                await p.wait()
                raise
            streams = json.loads(out or b"{}").get("streams") or []
            start = next((float(s["start_time"]) for s in streams
                          if s.get("codec_type") == "audio" and s.get("start_time") not in (None, "N/A")), None)
        except (OSError, ValueError, asyncio.TimeoutError) as e:
            log.info("job %s: could not read the remux start (%s); using the offset", job.id, redact(e))
            return job.origin
        if start is not None and 0 <= start <= 30:
            job.origin = round(max(0.0, job.offset - start), 3)
        return job.origin

    def _segments(self, job: Job) -> int:
        return self._playlist(job).count("#EXTINF")

    def _last_seq(self, job: Job) -> int:
        nums = [int(n) for n in re.findall(r"seg_(\d+)\.", self._playlist(job))]
        return max(nums) if nums else job.seq

    def produced(self, job: Job) -> float:
        return sum(float(x) for x in re.findall(r"#EXTINF:([\d.]+)", self._playlist(job)))

    async def wait_ready(self, job: Job, need: int) -> Job:
        deadline = time.time() + READY_TIMEOUT
        while time.time() < deadline:
            if job.state == "failed":
                return job
            have = self._segments(job) and (job.sub is None or (job.dir / "master.m3u8").is_file())
            if have and (self._segments(job) >= need or job.state == "ready"):
                if job.state == "starting":
                    job.state = "ready"
                return job
            await asyncio.sleep(0.25)
        if job.state == "starting":
            job.state, job.error = "failed", "The stream took too long to start."
            await self.stop(job.id, "start timeout")
        return job

    async def _kill(self, proc) -> None:
        if proc.returncode is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), 4)
        except asyncio.TimeoutError:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()

    async def stop(self, job_id: str, why: str = "") -> None:
        job = self.jobs.pop(job_id, None)
        if not job:
            return
        job.stopping = True
        self._remember(job, why)
        if job.kind == "live" and not job.transcode and self.on_meta:
            kbps = self._live_kbps(job)
            if kbps:
                self.on_meta(job.item_id, None, kbps=kbps)
        if self.by_key.get(job.key) == job_id:
            self.by_key.pop(job.key, None)
        if job.proc:
            await self._kill(job.proc)
        shutil.rmtree(job.dir, ignore_errors=True)
        log.info("job %s: stopped (%s)", job.id, why or "-")

    def _live_kbps(self, job: Job) -> int:
        """Average source bitrate from the segments on disk (video copied, so this is the provider's)."""
        text = self._playlist(job)
        names = re.findall(r"^(seg_\d+\.(?:ts|m4s))$", text, re.M)
        secs = sum(float(x) for x in re.findall(r"#EXTINF:([\d.]+)", text))
        try:
            size = sum((job.dir / n).stat().st_size for n in names)
        except OSError:
            return 0
        return int(size * 8 / secs / 1000) if secs > 4 else 0

    def _remember(self, job: Job, why: str) -> None:
        self.recent.pop(job.item_id, None)
        self.recent[job.item_id] = {"t": int(time.time()), "kind": job.kind, "state": job.state, "why": why,
                                    "error": job.error, "transcode": job.transcode, "audio": job.audio,
                                    "sub": job.sub, "restarts": job.restarts, "tail": job.tail[-8:]}
        while len(self.recent) > 50:
            self.recent.pop(next(iter(self.recent)))

    async def release(self, job_id: str, client: str) -> None:
        """A viewer left (closed the player / switched channel)."""
        job = self.jobs.get(job_id)
        if not job:
            return
        job.viewers.pop(client, None)
        now = time.time()
        if not any(now - t < 20 for t in job.viewers.values()):
            await self.stop(job_id, "viewer left")

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(5)
            await self._reap_once(time.time())

    async def _reap_once(self, now: float) -> None:
        """Stop jobs nobody is watching. One job failing to stop must not end the reaper
        (it would then never stop idle jobs again, and they would hold provider slots)."""
        for job in list(self.jobs.values()):
            try:
                for c, t in list(job.viewers.items()):
                    if now - t > 60:
                        job.viewers.pop(c, None)
                if job.kind == "live":
                    idle = now - max(job.last_media, job.last_beat) > LIVE_IDLE
                    paused = now - job.last_media > LIVE_PAUSED_MAX
                    dead = job.state == "failed" and now - job.last_beat > 20
                    if idle or paused or dead:
                        await self.stop(job.id, "idle")
                else:
                    if now - max(job.last_media, job.last_beat) > VOD_IDLE or (job.state == "failed" and now - job.last_beat > 20):
                        await self.stop(job.id, "idle")
            except Exception as e:  # noqa: BLE001
                log.warning("job %s: could not be stopped by the reaper: %s", job.id, redact(e))
