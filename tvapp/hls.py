"""Small fixes to the playlists and WebVTT segments ffmpeg writes for a VOD remux, applied as
they are served. Each was found by testing in Safari (macOS + iOS), Firefox and Chrome/hls.js:

- media playlist: EXT-X-START at 0. An EVENT playlist (still growing) otherwise starts at the
  live edge in Safari and hls.js.
- master playlist: ffmpeg names the subtitle rendition "subtitle_0" with no LANGUAGE or
  AUTOSELECT, so iOS lists it as an unnamed track and its native (AVKit) fullscreen player shows it
  as off; the real name, language and FORCED / accessibility flags are filled in.
- subtitle playlist: ffmpeg leaves out PLAYLIST-TYPE, which makes Safari treat it as a live
  (sliding) playlist and re-poll it while the video playlist is an EVENT one.
- WebVTT segments: without an X-TIMESTAMP-MAP header Safari places cues relative to each
  segment and repeats/misplaces them (seen: one cue shown 50 s late, cue count tripled).
  "MPEGTS:0 <-> 00:00:00.000" says the cue times are already on the media timeline, which is
  exactly what a single-output remux produces; hls.js reads it the same way.
"""
from __future__ import annotations

import re

START = "#EXT-X-START:TIME-OFFSET=0,PRECISE=YES"
LIVE_HOLD_BACK = 12.0       # s behind the live edge for native players (Safari/iOS); hls.js is set the same
TIMESTAMP_MAP = "X-TIMESTAMP-MAP=MPEGTS:0,LOCAL:00:00:00.000"
SDH_CHARACTERISTICS = "public.accessibility.transcribes-spoken-dialog,public.accessibility.describes-music-and-sound"
_MEDIA_LINE = re.compile(r"^#EXT-X-MEDIA:TYPE=SUBTITLES,.*$", re.M)
_URI = re.compile(r'URI="([^"]*)"')


def media_playlist(text: str) -> str:
    if text.startswith("#EXTM3U") and "#EXT-X-START" not in text:
        text = text.replace("#EXTM3U", "#EXTM3U\n" + START, 1)
    return text


def _quoted(value: str) -> str:
    return re.sub(r'["\r\n,]', " ", value).strip()[:60] or "Subtitles"


def master_playlist(text: str, sub: dict) -> str:
    """sub: the chosen subtitle track (tracks.describe): label, lang, forced, sdh."""
    def line(m: re.Match) -> str:
        uri = _URI.search(m.group(0))
        attrs = ['TYPE=SUBTITLES', 'GROUP-ID="subs"', f'NAME="{_quoted(sub.get("label", ""))}"']
        if sub.get("lang"):
            attrs.append(f'LANGUAGE="{_quoted(sub["lang"])}"')
        attrs += ["DEFAULT=YES", "AUTOSELECT=YES"]
        if sub.get("forced"):
            attrs.append("FORCED=YES")
        if sub.get("sdh"):
            attrs.append(f'CHARACTERISTICS="{SDH_CHARACTERISTICS}"')
        if uri:
            attrs.append(f'URI="{uri.group(1)}"')
        return "#EXT-X-MEDIA:" + ",".join(attrs)
    return _MEDIA_LINE.sub(line, text, count=1)


def subtitle_playlist(text: str) -> str:
    if text.startswith("#EXTM3U") and "#EXT-X-PLAYLIST-TYPE" not in text:
        text = re.sub(r"^(#EXT-X-TARGETDURATION:\d+)$", r"\1\n#EXT-X-PLAYLIST-TYPE:EVENT", text, count=1, flags=re.M)
    return text


def webvtt_segment(text: str) -> str:
    if text.startswith("﻿"):
        text = text[1:]
    if "X-TIMESTAMP-MAP" in text.split("\n\n", 1)[0]:
        return text
    first, nl, rest = text.partition("\n")
    if not first.startswith("WEBVTT"):
        return text
    return f"{first}\n{TIMESTAMP_MAP}\n{rest}" if nl else f"{first}\n{TIMESTAMP_MAP}\n"


def live_playlist(text: str) -> str:
    """Live: ask native players (Safari, iOS) to start ~12 s behind the edge, like hls.js does here
    (the provider delivers in bursts with 4-12 s gaps). Players that don't know the tag ignore it."""
    if text.startswith("#EXTM3U") and "#EXT-X-SERVER-CONTROL" not in text:
        text = text.replace("#EXTM3U", f"#EXTM3U\n#EXT-X-SERVER-CONTROL:HOLD-BACK={LIVE_HOLD_BACK}", 1)
    return text
