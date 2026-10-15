"""Live TV: what each channel really sends, which channels are the same channel at another
quality, and which one to play on this device.

Principle: play the best version the device can play continuously. Buffering is a failure, so
"best" is not simply "most pixels".

Source formats (MetaCache, live-meta.json): codec, profile, bit depth, HDR transfer, size, fps
and bitrate per channel. They come from a short ffprobe when needed and from every live job's
own ffmpeg output; kept 14 days. Provider names can't be trusted for this: here, "HEVC 4K" was
really 1080p50 and the "RAW" feeds were the best 1080p50 H.264 ones.

Variants (variants()): channels whose names normalise to the same base, e.g. "4K: SKY SPORTS MAIN
EVENTS UHD 3840P", "VIP: SKY SPORTS MAIN EVENT HD" and "UK: SKY SPORT MAIN EVENT RAW":
superscript markers folded (NFKC), quality/format words removed, singular/plural ignored. The
region comes from the name prefix (see below). Quality-only prefixes (4K:, 8K:, UHD:) carry
no region and join a group only when it has exactly one region. Bases shorter than two words never
group. The country comes from the name's own prefix (UK:, NZ:, CZ: ...); VIP: and NOW: are
packages whose country is their category's ("UK| SPORT VIP"); a different country prefix is a
different channel (Canal 12 Uruguay is not Canal 12 El Salvador; a Czech feed has Czech audio).
Anything less certain is not a variant.

Ranking (score()): resolution, then motion - 50/60 fps is worth a lot for sport (720p50 beats
1080p25 for football; for a film channel 1080p25 wins) - then a small bonus for HDR, then bitrate.

Client (parse_caps()): the browser reports what it can decode (MediaCapabilities, by codec and
resolution ladder: "s" smooth, "u" supported but not smooth, "x" seen failing in real playback).

Interlaced H.264 comes in two codings, and browsers treat them differently (measured, 1.8.0):
- frame-coded (MBAFF, or frame pictures): one picture per frame; Chrome, Firefox and Safari decode
  it like progressive video. Capability key "avci", which falls back to the device's "avc" entries.
- field-coded (PAFF): every field is its own picture, so a 1080i25 stream carries 50 pictures a
  second. Chrome (hardware and software decoders), Firefox (VideoToolbox) and macOS Safari (native
  HLS) all fail on it - no frame is shown, audio may play on. The iOS Simulator decoded it. Key
  "avcf": not decodable unless the device has proved otherwise in real playback; iPhone/iPad
  start as "possibly" ("u").
Which one a stream is: a probe (or a segment the app already wrote) gives field_order and the
duration of each video packet - a field-coded stream's packets last one field (1/50 s at 25 fps).
MediaCapabilities can't be asked about interlacing, so the device learns it from real playback.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
import unicodedata
from pathlib import Path

META_TTL = 14 * 86400
META_MAX = 5000
UK_NAMES = {"UK", "GB", "ENG"}
PACKAGES = {"VIP", "NOW"}             # feed packages: their region is the category's ("UK| SPORT VIP")
QUALITY_PREFIXES = {"4K", "8K", "UHD", "FHD", "HD", "SD", "RAW", "HDR"}
_DROP = {"UHD", "FHD", "HD", "SD", "4K", "8K", "RAW", "HEVC", "H265", "H264", "HDR", "HDR10", "HLG", "DOLBY",
         "AUDIO", "ATMOS", "50FPS", "60FPS", "FPS", "2160P", "3840P", "1080P", "1080I", "720P", "576P", "480P", "BK",
         "BACKUP", "LIVE", "LQ"}
_SPORT = re.compile(r"SPORT|FOOTBALL|SOCCER|\bF1\b|FORMULA|RACING|GOLF|CRICKET|RUGBY|\bNBA\b|\bNFL\b|\bNHL\b|\bMLB\b|"
                    r"BOXING|\bUFC\b|DAZN|EUROSPORT|BEIN|TENNIS|PREMIER LEAGUE|LALIGA|SERIE A|BUNDESLIGA|MOTOGP|DARTS")
_PREFIX = re.compile(r"^\s*([A-Z0-9]{2,4})\s*[:|]\s*")
_HDR = {"smpte2084", "arib-std-b67"}

# ---------------------------------------------------------------- transcode cost (measured)
# Pi 5, software, measured 11 Oct 2026: real 4K50 HEVC broadcast decodes at 0.45x real time
# (= 186 Mpx/s); scaling 4K -> 720p costs ~0.5 s per second of 4K50; libx264 veryfast 720p50
# encodes at ~2.5x (= 115 Mpx/s of output). Together: 4K50 HEVC -> 720p50 = 0.31x predicted,
# 0.32x measured. H.264/MPEG-2 decode is cheaper than HEVC (estimates, conservative).
DECODE_MPX = {"hevc": 186.0, "h264": 450.0, "mpeg2video": 900.0}
SCALE_MPX = 830.0
ENCODE_MPX = 115.0
TRANSCODE_MARGIN = 1.5      # predicted speed needed: live input arrives in ~4 s bursts, and a
                            # catalogue refresh or a VOD remux can take a core meanwhile


# Interlaced sources are converted by deinterlacing every field (bwdif) to 720p50 with x264
# ultrafast: measured on this Pi from a 1080i25 field-coded H.264 sample at 1.69x real time (2.6
# cores flat out, ~1.5 at real-time pace). Slower options measured on the same sample: yadif ->
# 720p25 veryfast 1.35x (the 1.4.0 path, below the margin), bwdif -> 720p50 veryfast 1.23x /
# superfast 1.38x, separatefields -> 720p50 veryfast 1.43x, bwdif -> 1080p50 ultrafast 1.53x and
# veryfast 0.76x. The generic model below predicted 2.6x for this source, so interlaced input uses
# the measured figure, scaled by its pixel rate.
DEINT_SPEED = 1.69
DEINT_REF_MPX = 1920 * 1080 * 25 / 1e6


def predicted_transcode_speed(meta: dict) -> float:
    w, h, fps = meta.get("w") or 0, meta.get("h") or 0, meta.get("fps") or 25
    if not (w and h):
        return 0.0
    if meta.get("interlaced"):
        return round(DEINT_SPEED * DEINT_REF_MPX / (w * h * fps / 1e6), 2)
    src = w * h * fps / 1e6
    out_w = min(1280, w)
    out = out_w * (h * out_w / w) * fps / 1e6
    cost = src / DECODE_MPX.get(meta.get("v", ""), 150.0) + (src / SCALE_MPX if w > 1280 else 0) + out / ENCODE_MPX
    return round(1 / cost, 2) if cost else 0.0


# ------------------------------------------------------------------------- names
def fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").upper()


def name_info(name: str, category: str = "") -> dict:
    """Normalised base, region and quality hints of a channel name."""
    n = fold(name)
    region = ""
    m = _PREFIX.match(n)
    if m:
        region = m.group(1)
        n = n[m.end():]
    cm = _PREFIX.match(fold(category))
    cat_region = cm.group(1) if cm else ""
    # The name's own prefix decides: "UY: CANAL 12" and "LSV: CANAL 12" are different channels even
    # in one category, and "CZ: VIASAT NATURE" has Czech audio. VIP/NOW are packages: their country is
    # the category's. 4K:/8K:/UHD: say nothing about the country.
    reg = region
    if reg in PACKAGES or not reg:
        reg = cat_region if cat_region not in QUALITY_PREFIXES else ""
    if reg in UK_NAMES:
        reg = "UK"
    elif reg in QUALITY_PREFIXES or reg in PACKAGES or not reg:
        reg = "*"
    words = []
    hints = set()
    if re.match(r"^\s*[#=*~-]{2,}", fold(name)):
        return {"base": "", "region": "*", "prefix": "", "hints": [], "sport": False}   # "##### SKY SPORTS #####": a heading row
    n = n.replace("+", " PLUS ")                                             # SKY SPORTS+ is its own channel
    for raw in re.findall(r"[A-Z0-9]+", n):
        if raw in _DROP or re.fullmatch(r"BK\d*|\d{3,4}P|\d+K", raw):
            hints.add(raw)
            continue
        words.append(raw[:-1] if len(raw) > 3 and raw.endswith("S") else raw)
    base = " ".join(words) if len(words) >= 2 else ""
    return {"base": base, "region": reg, "prefix": region, "hints": sorted(hints),
            "sport": bool(_SPORT.search(n) or _SPORT.search(fold(category)))}


def hint_height(hints: list[str]) -> int:
    h = set(hints)
    if h & {"UHD", "4K", "8K", "2160P", "3840P"}:
        return 2160
    if h & {"FHD", "1080P", "1080I", "RAW"}:
        return 1080
    if h & {"HD", "720P"}:
        return 720
    if h & {"SD", "LQ", "576P", "480P"}:
        return 576
    return 0


class VariantIndex:
    """base name -> channels, built once per catalogue version."""

    def __init__(self, live: list[dict], cats: dict):
        self.info: dict[str, dict] = {}
        self.groups: dict[str, list[str]] = {}
        for it in live:
            inf = name_info(it.get("name", ""), cats.get(it.get("cat", ""), ""))
            self.info[it["id"]] = inf
            if inf["base"]:
                self.groups.setdefault(inf["base"], []).append(it["id"])

    def variants(self, iid: str) -> list[str]:
        """Same channel at other qualities (the channel itself first), or [iid] when unsure."""
        inf = self.info.get(iid)
        if not inf or not inf["base"]:
            return [iid]
        group = self.groups.get(inf["base"], [])
        regions = {self.info[g]["region"] for g in group if self.info[g]["region"] != "*"}
        if inf["region"] == "*":
            if len(regions) != 1:
                return [iid]                        # "4K: X" where X exists in several countries
            region = next(iter(regions))
        else:
            region = inf["region"]
        out = [iid] + [g for g in group if g != iid and self.info[g]["region"] in (region, "*")
                       and not (self.info[g]["region"] == "*" and len(regions) != 1)]
        return out[:40]


# ---------------------------------------------------------------- formats cache
class MetaCache:
    def __init__(self, path: Path, clock=time.time):
        self.path = path
        self.clock = clock
        try:
            self.data = json.loads(path.read_text())
            if not isinstance(self.data, dict):
                self.data = {}
        except (FileNotFoundError, ValueError):
            self.data = {}
        self.dirty = False

    def get(self, iid: str) -> dict | None:
        m = self.data.get(iid)
        if m and self.clock() - m.get("t", 0) < META_TTL:
            return m
        return None

    def put(self, iid: str, meta: dict, source: str) -> dict:
        old = self.data.get(iid) or {}
        m = {**old, **{k: v for k, v in meta.items() if v not in (None, "", 0)}, "t": int(self.clock()), "src": source}
        self.data[iid] = m
        if len(self.data) > META_MAX:
            for k in sorted(self.data, key=lambda k: self.data[k].get("t", 0))[: len(self.data) - META_MAX]:
                del self.data[k]
        self.dirty = True
        return m

    def note(self, iid: str, **fields) -> None:
        """Facts learned while playing (measured transcode speed, bitrate) - no TTL refresh."""
        if iid in self.data:
            self.data[iid].update(fields)
            self.dirty = True

    def save(self) -> None:
        if not self.dirty:
            return
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, separators=(",", ":"), ensure_ascii=False))
        os.replace(tmp, self.path)
        self.dirty = False


FIELD_ORDERS = ("tt", "bb", "tb", "bt")


def scan_meta(field_order: str, fps: float, packet_durations: list[float]) -> dict:
    """Interlace facts from ffprobe: field order, and (from the video packets) field or frame coding."""
    if field_order not in FIELD_ORDERS:
        return {"interlaced": False, "field_order": "progressive"} if field_order == "progressive" else {}
    out = {"interlaced": True, "field_order": field_order, "fields": round(2 * fps, 2) if fps else 0}
    durs = sorted(d for d in packet_durations if d > 0)
    if durs and fps:
        out["coding"] = "field" if durs[len(durs) // 2] * fps < 0.75 else "frame"
    return out


def meta_from_probe(streams: list[dict], packets: list[dict] | None = None) -> dict:
    v = next((s for s in streams if s.get("codec_type") == "video"), {})
    a = next((s for s in streams if s.get("codec_type") == "audio"), {})
    fps = 0.0
    for key in ("avg_frame_rate", "r_frame_rate"):
        try:
            n, d = str(v.get(key) or "0/1").split("/")
            if int(d) and int(n):
                fps = round(int(n) / int(d), 2)
                break
        except ValueError:
            pass
    pix = v.get("pix_fmt") or ""
    vidx = streams.index(v) if v in streams else -1
    durs = [float(p.get("duration_time") or 0) for p in (packets or []) if p.get("stream_index") == vidx]
    return {"v": v.get("codec_name", ""), "profile": v.get("profile", ""), "bits": 10 if "10" in pix else 8,
            "w": int(v.get("width") or 0), "h": int(v.get("height") or 0), "fps": fps,
            "interlaced": v.get("field_order") in FIELD_ORDERS,
            "hdr": (v.get("color_transfer") or "") in _HDR, "transfer": v.get("color_transfer") or "",
            "a": a.get("codec_name", ""), "ach": int(a.get("channels") or 0),
            **scan_meta(v.get("field_order") or "", fps, durs)}


_V_LINE = re.compile(r"Video: (\w+)(?: \(([^)]*)\))?.*?, (\w+)(?:\(([^)]*)\))?, (\d{2,5})x(\d{2,5}).*?(?:, ([\d.]+) fps)?(?:,|$)")


def meta_from_ffmpeg_line(line: str) -> dict | None:
    """'Stream #0:0[0x100]: Video: hevc (Main 10) ([36][0][0][0] / 0x0024), yuv420p10le(tv, bt2020nc/bt2020/smpte2084),
    3840x2160 [SAR 1:1 DAR 16:9], 50 fps, 50 tbr, 90k tbn' -> meta."""
    m = _V_LINE.search(line)
    if not m:
        return None
    codec, profile, pix, colour, w, h, fps = m.groups()
    colour = colour or ""
    out = {"v": codec, "profile": profile or "", "bits": 10 if "10" in pix else 8, "w": int(w), "h": int(h),
           "fps": float(fps) if fps else 0.0, "interlaced": "top first" in colour or "bottom first" in colour,
           "hdr": any(t in colour for t in _HDR), "transfer": next((t for t in _HDR if t in colour), "")}
    if out["interlaced"]:                         # field order only: field / frame coding needs the packets
        out["field_order"] = "tt" if "top first" in colour else "bb"
        out["fields"] = round(2 * out["fps"], 2) if out["fps"] else 0
    return out


# -------------------------------------------------------------------- ranking
def score(meta: dict, sport: bool) -> float:
    h = min(meta.get("h") or 0, 2160)
    if not h:
        return 0.0
    res = 1.0 + math.log2(max(h, 360) / 360) * 1.16          # 360p 1.0, 720p ~2.2, 1080p ~2.8, 2160p ~4.0
    fps = meta.get("fps") or 25
    motion = (1.2 if sport else 0.35) if fps >= 47 else 0.0
    hdr = 0.15 if meta.get("hdr") else 0.0
    kbps = meta.get("kbps") or 0
    # 1080i25 is 25 pictures a second on screen (players don't make 50p of it), with combing on
    # motion: below 1080p25, and well below 720p50 for sport
    interlaced = 0.2 if meta.get("interlaced") else 0.0
    return round(res + motion + hdr + min(kbps, 20000) / 200000 - interlaced, 3)


def label(meta: dict | None) -> str:
    if not meta or not meta.get("h"):
        return "format not known yet"
    h = meta["h"]
    tier = "4K" if h >= 2000 else f"{h}{'i' if meta.get('interlaced') else 'p'}"
    fps = meta.get("fps") or 0
    codec = {"h264": "H.264", "hevc": "HEVC", "mpeg2video": "MPEG-2"}.get(meta.get("v", ""), meta.get("v", "").upper())
    return (f"{tier} {round(fps)}fps" if fps else tier) + (" HDR" if meta.get("hdr") else "") + f" · {codec}"


def describe(meta: dict | None) -> str:
    """Full format line for diagnostics: '1920×1080 · H.264 · interlaced, field-coded, top field
    first · 25 fps / 50 fields'."""
    if not meta or not meta.get("h"):
        return "format not known yet"
    codec = {"h264": "H.264", "hevc": "HEVC", "mpeg2video": "MPEG-2"}.get(meta.get("v", ""), meta.get("v", "").upper())
    parts = [f"{meta.get('w') or '?'}\u00d7{meta['h']}", codec + (f" {meta['profile']}" if meta.get("profile") else "")]
    fps = meta.get("fps") or 0
    if meta.get("interlaced"):
        order = {"tt": "top field first", "bb": "bottom field first", "tb": "top field first", "bt": "bottom field first"}
        bits = ["interlaced", {"field": "field-coded", "frame": "frame-coded"}.get(meta.get("coding", ""), "coding not known yet"),
                order.get(meta.get("field_order", ""), "")]
        parts.append(", ".join(b for b in bits if b))
        parts.append(f"{fps:g} fps / {meta.get('fields') or 2 * fps:g} fields" if fps else "")
    else:
        parts.append("progressive" if meta.get("field_order") == "progressive" or meta.get("interlaced") is False else "")
        parts.append(f"{fps:g} fps" if fps else "")
    if meta.get("hdr"):
        parts.append("HDR")
    return " \u00b7 ".join(p for p in parts if p)


def needs_probe(meta: dict | None) -> bool:
    """Probe again: no format yet, or interlaced H.264 whose coding (field / frame) isn't known -
    cache entries from before 1.8.0 know only that it is interlaced."""
    return not meta or (meta.get("v") == "h264" and bool(meta.get("interlaced")) and "coding" not in meta)


def scan_unknown(meta: dict | None) -> bool:
    """H.264 whose scan type isn't fully known (no field order yet, or interlaced with no coding)."""
    return bool(meta) and meta.get("v") == "h264" and ("field_order" not in meta or needs_probe(meta))


# ----------------------------------------------------------------- client caps
def codec_key(meta: dict) -> str:
    v = meta.get("v", "")
    if v == "h264":
        if meta.get("interlaced"):
            return "avcf" if meta.get("coding") == "field" else "avci"
        return "avc"
    if v == "hevc":
        return "hevc10" if meta.get("bits") == 10 else "hevc"
    return v


def parse_caps(text: str) -> dict:
    """'avc.2160.50.s,hevc10.2160.50.u,hevc10.2160.50.x' -> {codec: [(h, fps, flag), ...]}.
    Without any (an old page), a browser is assumed to play H.264 up to 1080p60 smoothly, which is
    what every browser this app supports does."""
    caps: dict = {}
    for tok in (text or "").split(","):
        parts = tok.strip().split(".")
        if len(parts) != 4 or parts[3] not in ("s", "u", "x"):
            continue
        try:
            caps.setdefault(parts[0], []).append((int(parts[1]), int(parts[2]), parts[3]))
        except ValueError:
            continue
    if not caps:
        caps = {"avc": [(1080, 60, "s"), (2160, 60, "u")]}
    return caps


def playable(meta: dict, caps: dict) -> int:
    """2 = smooth, 1 = supported but maybe not smooth, 0 = can't play this format without converting.
    Frame-coded interlaced H.264 ("avci") plays where H.264 does, unless the device has seen it fail;
    field-coded ("avcf") only where the device has reported it (iPhone/iPad "u", or learned)."""
    key = codec_key(meta)
    entries = caps.get(key, [])
    if key == "avci":
        entries = caps.get("avc", []) + entries
    h, fps = meta.get("h") or 0, meta.get("fps") or 25
    if any(f == "x" and eh <= h and efps <= max(fps, 25) + 1 for eh, efps, f in entries):
        return 0                                   # failed for real at this size (or smaller) on this device
    best = 0
    for eh, efps, f in entries:
        if eh >= h and efps + 1 >= fps:
            best = max(best, 2 if f == "s" else 1 if f == "u" else 0)
    return best


# ---------------------------------------------------------------------- decision
def decide(selected: str, candidates: list[str], metas: dict, caps: dict, sport: bool, quality: str = "auto") -> dict:
    """Pick what to play for `selected` (the channel the viewer chose).

    candidates: selected + confidently matched variants (VariantIndex.variants); metas: id -> meta
    for the ones whose format is known. quality: "auto" (best smooth version), "orig" (only the
    chosen channel), or a variant id (only that one). Returns {play, mode, reason, notice} where
    mode is "copy", "transcode" or "none" (nothing playable: reason says why)."""
    if quality not in ("auto", "orig"):
        pool = [quality] if quality in candidates else [selected]
    elif quality == "orig":
        pool = [selected]
    else:
        pool = candidates
    rows = []
    for iid in pool:
        m = metas.get(iid)
        if not m:
            continue
        rows.append((iid, m, playable(m, caps), score(m, sport)))
    sel_meta = metas.get(selected)
    sel_label = label(sel_meta)
    smooth = [r for r in rows if r[2] == 2]
    if smooth:
        best = max(smooth, key=lambda r: (r[3], r[0] == selected))
        chosen = best
        if selected in [r[0] for r in smooth]:
            own = next(r for r in smooth if r[0] == selected)
            if own[3] >= best[3]:
                chosen = own
        notice = ""
        if chosen[0] != selected and sel_meta and codec_key(sel_meta) == "avcf" and playable(sel_meta, caps) == 0:
            notice = f"Playing {label(chosen[1])}: this browser can't decode the {label(sel_meta)} original"
        elif chosen[0] != selected:
            better = sel_meta and score(sel_meta, sport) < chosen[3] and playable(sel_meta, caps) == 2
            notice = (f"Playing {label(chosen[1])} - a better version of this channel" if better
                      else f"Playing {label(chosen[1])} for smooth playback on this device")
        return {"play": chosen[0], "mode": "copy", "notice": notice,
                "reason": ("original quality, video copied" if chosen[0] == selected else
                           f"best smooth equivalent ({label(chosen[1])}); the chosen version is {sel_label}"
                           + ("" if not sel_meta else f", which this device {'cannot decode' if playable(sel_meta, caps) == 0 else 'may not decode smoothly'}"
                              if playable(sel_meta, caps) < 2 else ""))}
    maybe = [r for r in rows if r[2] == 1]
    if maybe:
        best = max(maybe, key=lambda r: (r[3], r[0] == selected))
        return {"play": best[0], "mode": "copy", "notice": "" if best[0] == selected else f"Playing {label(best[1])}",
                "reason": "supported by this device but not reported smooth; no smooth version known"}
    sustainable = [(r, predicted_transcode_speed(r[1])) for r in rows]
    sustainable = [(r, sp) for r, sp in sustainable
                   if sp >= TRANSCODE_MARGIN and (r[1].get("measured_speed") or 99) >= 1.0]
    if sustainable:
        (iid, m, _, _), sp = max(sustainable, key=lambda x: (x[0][3], x[1]))
        if m.get("interlaced"):
            # deinterlaced field by field: 720p50 keeps the motion of the 50 fields
            return {"play": iid, "mode": "transcode",
                    "notice": f"Converting {label(m)} to 720p 50fps on the Pi: this device can't decode the original",
                    "reason": f"no version this device can decode ({describe(m)}); deinterlacing to 720p50 H.264 "
                              f"(predicted {sp}x real time)"}
        return {"play": iid, "mode": "transcode", "notice": "" if iid == selected else f"Playing {label(m)}",
                "reason": f"no version this device can decode; converting to H.264 (predicted {sp}x real time)"}
    worst = min((predicted_transcode_speed(r[1]) for r in rows), default=0)
    return {"play": None, "mode": "none", "notice": "",
            "reason": (f"{sel_label}: this device can't decode it and converting it on the Pi runs at only "
                       f"~{worst}x real time" if rows else "the format of this channel isn't known yet")}
