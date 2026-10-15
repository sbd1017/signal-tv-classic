# Signal TV Classic — private IPTV + VOD at https://example.com/tv

A browser player for live TV, movies and series. It is built for iPhone Safari (and works as a home-screen app), iPad, and macOS Safari/Chrome.

## At a glance

| | |
|---|---|
| Source (git) | `~/signal-tv-classic` — edit here, deploy with `deploy/install.sh` |
| App code | `/opt/signal-tv-classic/` (`tvapp/` Python package, `web/` frontend, `venv/`) — deployed copy, root-owned |
| Service | `signal-tv-classic.service`: user `signal-tv-classic`, **127.0.0.1:8790** only |
| Demo provider | `signal-tv-classic-demo.service`: **127.0.0.1:8791** only (legal test media) |
| Config (secrets) | `/etc/signal-tv-classic/tv.env`: `root:signal-tv-classic`, mode `0640` |
| Config template | `/opt/signal-tv-classic/tv.env.example` |
| Alerts | `/etc/signal-tv-classic/telegram.env` (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`; root:signal-tv-classic 0640) and `signal-tv-classic-alert@.service` — see [Alerts and diagnostics](#alerts-and-diagnostics) |
| State | `/var/lib/signal-tv-classic/` (0700): **`state.db`** (synced favourites/progress/history/prefs — the only user data), `backups/` (daily copies of it); caches `catalogue.json`, `epg.json`, `details.json`; `secret.key`; `refresh-history.json`, `alerts.json`, `memory-guard.json` |
| Backups | `/var/backups/signal-tv-classic/signal-tv-classic-*.tar.gz` (daily, keeps 30; `iptv backup`) — see [Sync, backup and restore](#sync-backup-and-restore) |
| Cache | `/var/cache/signal-tv-classic/img` (artwork, pruned at 400 MB), `/var/cache/signal-tv-classic/vod` (remux segments) |
| Live segments | `/dev/shm/signal-tv-classic/` (RAM; wiped on start/stop and per job) |
| nginx | `conf.d/30-signal-tv-classic.conf`, `snippets/signal-tv-classic{,-proxy,-headers}.conf`, one `include` line in `sites-available/example.com.conf` |
| Logs | `journalctl -u signal-tv-classic` (`iptv logs`); nginx `/var/log/nginx/signal-tv-classic.access.log` (tokens redacted) |
| CLI | `/usr/local/bin/iptv` |

Stack: Python 3.11, FastAPI/uvicorn, httpx, ffmpeg/ffprobe 5.1, and vanilla JS with hls.js 1.7.3 (vendored, npm integrity verified). There are no external fonts, CDNs or analytics.

## Routing and security

```
Cloudflare Tunnel -> nginx 127.0.0.1:8081 -> /tv/... -> 127.0.0.1:8790 (signal-tv-classic)
```

- **`/tv/`, `/tv/api/`, `/tv/static/`**
  - Uses the site's existing Basic Auth (`snippets/site-basic-auth.conf`, realm "Private apps").
  - After a successful Basic Auth request, the app sets an HttpOnly, Secure, SameSite=Lax cookie `tv_s`. It is HMAC-signed and lasts 30 days.
  - nginx accepts that cookie in place of Basic Auth through `auth_request` (`satisfy any`). This means the iPhone home-screen app does not ask for the password on every launch.
  - Only nginx can grant the cookie: it overwrites `X-TV-Auth-User` with `$remote_user`.
  - `iptv revoke-sessions` signs out every remembered device.
- **`/tv/m/<token>/…`** (video)
  - Has no login. It is authorised by a signed token in the path that expires after 12 hours.
  - Each token is scoped to one stream job or one catalogue item, because AVPlayer, PiP and AirPlay don't reliably send credentials.
  - nginx logs these paths as `/tv/m/<token>/…`.
- **`/tv/pwa/`** (manifest and icons) is public, because iOS fetches it without credentials.
- **Methods**: only GET and HEAD reach `/tv` (the existing server-level method map), except POST to `/tv/api/state` and `/tv/api/state/import` (synced state; one line in `conf.d/10-site.conf`). Those writes also need a JSON body, an `X-TV-Sync: 1` header and a same-site `Origin`, so another site cannot make your browser send one. Request bodies are capped at 1 KB everywhere under `/tv` except 1 MB there. There are rate limits per visitor IP.
- **Provider credentials, playlist URLs and stream URLs never reach the browser.**
  - The browser only sees opaque IDs (`l_…` live, `m_…` movie, `s_…` series, `e_…` episode).
  - The server plays only catalogued items, and only `http(s)` URLs. ffmpeg is limited to `http,https,tls,tcp,crypto`.
  - There is no arbitrary-URL proxy. The artwork proxy (`/tv/api/img/<hash>`) only fetches URLs found in the catalogue, accepts real image bytes only (no SVG/HTML), and caps them at 4 MB.
  - Provider text is cleaned server-side and always inserted with `textContent`.
  - App logs replace URLs with `<url>` and mask credentials.
- **Response headers**: strict CSP (no inline script or style), `nosniff`, `X-Frame-Options DENY`, and `Cache-Control: no-store` on API and media. `Cloudflare-CDN-Cache-Control: no-store` stops the CDN caching video.
- **Settings page**: there is no `/tv/settings`. Configuration is SSH-only by design.

## Providers

`IPTV_PROVIDER_TYPE=m3u` or `xtream`. Both adapters produce the same normalised catalogue: live channels, movies, series and episodes, with categories, artwork, guide IDs and so on.

### M3U / M3U8

The playlist is downloaded (up to 400 MB) and parsed for `#EXTINF` attributes:
- `tvg-id`, `tvg-name`, `tvg-logo`, `tvg-chno`, `group-title`;
- `#EXTGRP`;
- `#EXTVLCOPT:http-user-agent` and `http-referrer`.

Each entry is classified using the strongest signal available:

1. **Xtream-style URL path**: `/live/…`, `/movie/…` or `/series/…`.
2. **Explicit attribute**: `tvg-type` / `type` (`live`, `movie`/`vod`, `series`/`episode`).
3. **Episode pattern** in the title, such as `S01E02` or `1x02`, when the entry is a file or the group looks like series → **episode**.
   - It is grouped into a series by show name and group.
4. **Group keywords**:
   - Series: "Series", "TV Shows", "Box sets" … → episode.
   - Movies: "VOD", "Movies", "Films", "Cinema" … → **movie**.
5. **Video file extension** (`.mp4 .mkv .avi .mov …`) → **movie**.
6. **Anything else** (`.ts`, `.m3u8`, no extension) → **live**.

### Xtream Codes

Uses `player_api.php` for login and status, then:
- `get_live_categories` / `get_live_streams`;
- `get_vod_categories` / `get_vod_streams`;
- `get_series_categories` / `get_series`.

Movie details (`get_vod_info`) and series episodes (`get_series_info`) are fetched when first opened, then cached (6–24 h, persisted across restarts). Stream URLs are built server-side as `/{live|movie|series}/user/pass/id.ext`.

### Guide (XMLTV)

- Set by `IPTV_EPG_URL` (`.xml` or `.xml.gz`). For Xtream, it defaults to the panel's `xmltv.php`.
- Downloaded on start, then every `IPTV_EPG_REFRESH_HOURS`.
- Parsed in a streaming way, and kept only for catalogue channels, from 3 h ago to 36 h ahead.
- Matching uses `tvg-id`, falling back to a normalised display name.
- The Live search index (`tvapp/epgsearch.py`) is rebuilt in the background after each guide or
  catalogue refresh and every 3 h. It costs ~40 MB and 3–5 s of one core to build; searches take
  2–10 ms on the Pi.

### Catalogue refresh

On service start, the saved copy is served immediately and then refreshed. After that it refreshes every `IPTV_REFRESH_HOURS` (default 12), on `iptv refresh` / `iptv reload`, and from **Library → Status → Refresh catalogue** (rate-limited to one a minute). Search is local in the browser: typing never sends a request.

The lists sent to the browser (`/tv/api/live`, `/movies`, `/series`; the movie list alone is ~18 MB of
JSON, 7 MB gzipped) are built once per catalogue/guide version in a worker thread, in chunks, and kept
gzipped in memory (`tvapp/lists.py`); they are rebuilt straight after each refresh. Before this every
request rebuilt them on the event loop: ~2 s for movies, during which nothing else (live video
included) was served. Now-and-next for all channels (`/tv/api/epg/now`) is cached for 30 s the same way.

**Refresh safety** (`tvapp/refresh_guard.py`, thresholds documented there). A provider having a bad
moment usually still answers "200 OK", with an empty, partial or junk list. So a fetched
catalogue is compared with the one being served before it replaces it. It is **rejected** when:
- a section that had ≥100 items comes back empty;
- a section shrinks beyond its limit: live > 30 % and > 2000, films/series > 15 % and > 5000/2000;
- more than half of a section's IDs change (that would orphan favourites and resume points);
- categories that disappeared held > 25 % of a section;
- the reply is malformed: > 2 % unusable rows, or the category list is missing.

When a refresh is rejected:
- the previous catalogue and the cached lists stay in use;
- the anomaly is logged, recorded and shown in `iptv status`;
- a Telegram alert goes out;
- scheduled retries back off: 30 min, 1 h, 2 h… up to the normal interval.

If the change is genuine, run `iptv refresh --accept-changes`. A section that fails outright (HTTP
error or non-JSON) keeps the whole old catalogue, and the error names what had loaded fine. Smaller
shrinks (> 5 %) and single large categories disappearing are accepted, but recorded as warnings.

**Change history:** every refresh is recorded in `/var/lib/signal-tv-classic/refresh-history.json` (last 60).
Each entry has the counts, titles added/removed per section, categories added/removed and the newest
titles; the added IDs are kept for the latest 6 entries, ready for a future "Recently added".
`iptv status` shows the last change, e.g. `last refresh: +13/-0 movies`, plus any rejection.

## Playback

- **Live**: the channel you pick, played in the best version this device can play *smoothly*
  (`tvapp/livemeta.py`). Buffering counts as failure, so "best" is not just "most pixels".
  - **Formats:** each channel's real format is learned from a 1–2 s ffprobe the first time it's
    needed (one provider connection, closed before the stream starts) and from every stream's own
    ffmpeg output; kept 14 days in `/var/lib/signal-tv-classic/live-meta.json`. It covers codec, profile,
    bit depth, HDR, size, fps and bitrate. Names lie: "HEVC 4K" turned out to be 1080p, and "RAW"
    feeds were the best 1080p50 H.264.
  - **Video is copied, never re-encoded when avoidable:**
    - H.264 goes out as MPEG-TS HLS, exactly as before;
    - HEVC goes out unchanged as fMP4 HLS (`hvc1`; Safari needs fMP4 for HEVC). On a Mac, Chrome,
      Firefox and Safari all play 4K50 HEVC Main10 this way, and so does iPhone Safari. The Pi
      only repackages it, at a few % CPU.
  - **Device capabilities:** the browser reports what it can decode (MediaCapabilities: H.264 /
    HEVC / HEVC 10-bit at 2160/1080/720p, smooth or not), cached per device for a week. Real
    playback is checked too: if more than 30 % of frames are dropped in the first ~30 s, that format
    is recorded as failing on this device and Auto switches version. (Firefox on the Pi claims
    smooth 4K HEVC, then drops 98 % of the frames.)
  - **Versions of a channel:** "4K: SKY SPORTS MAIN EVENTS UHD", "VIP: SKY SPORTS MAIN EVENT HD",
    "UK: SKY SPORT MAIN EVENT RAW" and so on are recognised as one channel.
    - The name's country prefix decides; VIP:/NOW: take their category's country; 4K:/8K: join only
      an unambiguous group.
    - Separator rows and "+" channels stay apart.
    - Anything uncertain is not treated as a version.
  - **Ranking:** resolution first, but 50/60 fps counts heavily for sport. 720p50 beats 1080p25 for
    football; for a film channel 1080p25 wins. HDR and bitrate break ties.
  - **Auto (default):** play the chosen channel if this device decodes it smoothly. Otherwise use
    the best smooth version, probing the likeliest few versions once (≤ 6 probes, ≤ 12 s, then
    cached). A short notice says so, e.g. "Playing 1080p 50fps for smooth playback on this device".
    A known better version is used too ("a better version of this channel").
  - **Quality menu** (the "HD" button in the player): Auto or any known version. A version you pick
    by hand that this device can't play is refused with the reason, plus a "Play best version"
    button, instead of starting a hopeless conversion.
  - **Conversion to H.264:** only when no copyable version exists *and* it is predicted to run at
    ≥ 1.5× real time. The model is calibrated on this Pi: 4K50 HEVC → 720p is predicted 0.32× and
    measured 0.32×; SD MPEG-2 is ~10×. A running conversion that stays below 0.95× is stopped, and
    its speed remembered so it isn't tried again.
  - **Interlaced H.264 (1.8.0):** formats record progressive / interlaced, field order and how
    the fields are coded. Field-coded 1080i ("PAFF", e.g. the Portuguese SPORT TV feeds) is copied
    fine by the Pi but Chrome, Firefox and macOS Safari can't show it (iPhone Safari can).
    - Auto plays a progressive version of the same channel when there is one (e.g. DAZN 1 1080i →
      its 1080p50 version), with a notice.
    - Otherwise the Pi deinterlaces field by field to 720p50 H.264 (`bwdif`, x264 ultrafast:
      1.69× real time, ~2.4 cores). Motion stays at 50 per second.
    - Frame-coded interlaced H.264 ("MBAFF") plays in Chrome and Firefox and is copied as before.
    - The player checks real playback: an interlaced copy that paints no frames is recorded as
      failing on this device and Auto picks again; one that plays is recorded as working.
  - **Buffering:**
    - 2 s segments with a 15-segment (~30 s) window;
    - the stream is handed to the player when 3 segments exist;
    - hls.js starts ~3 segments behind the edge and may fall up to 14 segments behind after a
      hiccup instead of jumping back to the edge (which would empty the cushion again).

    This provider delivers live data in ~4 s bursts (two segments at a time, measured).
  - **Diagnostics (ⓘ):** the selected channel, its real format, this device's capabilities, the
    version playing, copy or convert, and the reason.
  - **Unchanged:** one ffmpeg per channel shared by viewers; reconnects with an HLS discontinuity;
    a channel switch frees the provider connection first; idle jobs stop after ~35 s.
- **Movies and episodes** are probed once with ffprobe.
  - **MP4/MOV with H.264 + AAC/MP3** (or HEVC on clients that support it): byte-range proxied, which gives native seeking, correct Safari `Range`/206 behaviour, and is checked with a `bytes=0-1` probe.
  - **Anything else** (MKV, AVI, AC-3/DTS audio, providers without Range): remuxed with video copied to fMP4 HLS, starting at the resume point.
    - The custom seek bar shows the real timeline.
    - Seeking inside what has already been remuxed is instant. Seeking beyond it restarts the remux at the new position.
      Repeated skips (arrow keys, ±10/30 s buttons) are added up and restart it once, 0.7 s after the last press.
    - Read speed is capped at 3× real time. Segments are deleted once the job has been idle for about 2.5 min.
- **Limits**: `IPTV_MAX_LIVE_STREAMS` (default 2) and `IPTV_MAX_VOD_REMUX` (default 2). When the limit is hit, the quietest job is evicted. If every slot is actively watched, the new request gets a clear error instead.
- **Players**: Safari, iPhone and iPad use native HLS (AirPlay and PiP work). Chrome and others use hls.js.
- **Position accuracy**: a remux really starts at the keyframe before the requested time. The app
  reads where the audio begins in the first segment (locally, no provider request) and computes the
  true start, so the shown/saved position and seeks are exact. Before 1.3.0 they were up to one
  keyframe interval (often ~2 s) out.

### Subtitles and audio tracks (films and episodes)

- **Discovery:** ffprobe (the same probe as before, no extra provider connection) lists every
  embedded audio and subtitle stream. `tvapp/tracks.py` labels them:
  - language names, from 3-letter, 2-letter or BCP-47 tags and from the title text;
  - regions such as Portuguese (Brazil), Spanish (Latin America) and Chinese (Simplified);
  - SDH, Forced, Audio Description and commentary;
  - repeated labels are numbered.
- **Text subtitles** (SubRip, ASS/SSA, MP4 timed text, WebVTT) are converted to WebVTT inside the
  remux, with the video still copied. Nothing is burned in.
  - Picture-based subtitles (Blu-ray PGS, DVD/DVB) are listed but can't be shown without burning
    them in, so they aren't offered.
  - Video, audio and the chosen subtitle go out as one HLS output (a WebVTT rendition), so cues stay
    on the right picture after any seek.
  - `tvapp/hls.py` adds what Safari needs: a named rendition with LANGUAGE/AUTOSELECT, an EVENT
    subtitle playlist, and an X-TIMESTAMP-MAP in each segment. Without them iOS misplaced and
    duplicated cues, and its native fullscreen player showed subtitles as off.
- **Choosing:** the player's **CC** button (or `C`) offers Off / Auto (forced only) / your languages /
  More languages, or says "No subtitles available". The **audio** button appears when a title has
  more than one audio track.
  - Picking either restarts the remux at the current position (a 2–4 s pause).
  - The choice is remembered for that film, or for the whole series.
  - A direct-play MP4 switches to a remux only when a subtitle or a non-default audio track is chosen.
- **Preferences** (Library → Subtitles & audio; synced to every device, and a new device uses the
  defaults until its first sync):
  - subtitles Off / Forced only / On;
  - subtitle languages in order (default `en`);
  - SDH prefer / avoid / either;
  - audio languages (empty = each title's original/default audio);
  - prefer audio description.
- **Automatic choice**, with no preference set:
  - audio is the file's default track, but never Audio Description unless asked for;
  - "Forced only" (the default) shows forced subtitles only in the audio's language, and only when
    that is one of your subtitle languages;
  - a language you didn't list is never switched on automatically.

### Alerts and diagnostics

- **Telegram alerts** (`tvapp/alerts.py`) use the Pi's existing bot. The credentials are in
  `/etc/signal-tv-classic/telegram.env` (root:signal-tv-classic 0640), never in the repo or in messages; without
  that file, alerts are only logged. Alerts are sent for:
  - a rejected refresh;
  - 3 or more failed refreshes in a row;
  - persistent provider stream failures (5+ failed starts of 2+ titles over 10+ min, none working);
  - the memory guard restarting the service;
  - failed systemd units: nightly backup, Google Drive upload, or the service itself, via
    `signal-tv-classic-alert@.service` hooked into those units with `OnFailure=`/`OnSuccess=`.
- **No spam:**
  - repeats of the same alert wait 1 h, then 4 h, 12 h, then daily, with a count;
  - one "resolved" note is sent when the problem clears;
  - at most 6 messages an hour.
  - URLs, tokens and provider credentials are stripped from every message.
  - `iptv alert-test` sends a test message.
- **Diagnostics:** the ⓘ button in the player (or `I`) shows a one-screen report for the title or
  channel, with **Copy report**. It covers:
  - title, ID and type; container;
  - video codec and resolution; every audio and subtitle track;
  - the playback mode (direct play / remux / transcode, which tracks, the true start);
  - recent ffmpeg lines;
  - provider and catalogue status; slot use, with a "possible connection-slot exhaustion" hint;
  - app version and time;
  - this browser's player state.

  It is built from what the app already knows (no provider request) and redacted.
- **Health** in `iptv status`: OK, or the open problems (refresh rejected/failing, stale catalogue,
  provider refusing streams, a recent memory-guard event, failed backup/upload), plus whether alerts
  are on and how many stream starts failed in the last hour.

## Using the app

- **Sections**: Live TV, Movies, Series, Search, and Library (Continue Watching, Favourites, Recent, Status). A sidebar is shown on desktop and a bottom tab bar on phones.
- **Categories** (Live, Movies, Series): the category button opens a searchable list with item counts.
  The eye on a category hides it from "All categories" and from Search on every device (filter by
  All / Shown / Hidden; when searching, "Hide all N" / "Show all N" act on every match). A hidden
  category can still be picked to look inside it.
- **Live**
  - Categories, favourites, recent, and now/next with progress bars. There is a full guide per channel.
  - **Search** finds channels by name and by what they show: programmes on now and in the next 24 h.
    Results are grouped as Live now / Starting soon / Later / Channels. One programme on many
    channels is one result, its channels listed best version for this device first ("~HD" = quality
    guessed from the name until the channel has been played). Tapping one plays it as usual (Auto
    quality). `tvapp/epgsearch.py` describes the index and the matching rules.
    Team and competition names match their aliases ("Man Utd" ↔ "Manchester United", "UCL" ↔
    "Champions League"; the list is `DATA` in `tvapp/sports.py`, easy to extend).
  - **Hide channels** (the eye button; icon-only on phones) switches the list to edit mode: tapping a channel hides or shows it. Hidden
    channels are listed (and can be shown again) at the bottom of the Live category list. Favourites
    and Recent always show everything.
  - Channel up/down and previous channel. The last channel is offered as a Resume card.
  - Switching never reloads the page.
- **Sports**:
  - Football first, then other sports and a Today / Tomorrow choice.
  - Sections: Live now, Starting soon (3 h), Later today.
  - One card per match, whatever each broadcaster calls it. The best feed for this device comes
    first; tap the card to play it. "N more broadcasters" offers the others.
  - Built from the guide by `tvapp/sports.py` (team / competition aliases, sport words) and
    `epgsearch.Index.sports()`. Replays are tagged.
  - **For you**: every event of your favourite teams and competitions today and tomorrow (Live now
    / Starting soon / Later today / Tomorrow), each once. **Favourites** (star button) lists them,
    finds any of the ~280 known teams and competitions by any name, and sets **match alerts**:
    off (default), or a Telegram message 15 / 30 / 60 min before kick-off ("Arsenal vs Lille starts
    in 15 minutes · UEFA Champions League · 19:55 · 9 broadcasters available"). The ☆ on a card
    favourites its teams or competition; a small ★ marks favourite events here and in Live search.
  - Alerts (`tvapp/sportsalerts.py`): checked every minute against the built guide index. A match
    is announced once (event + kick-off day, recorded before sending), and only if the guide shows
    it is live: a broadcaster's LIVE tag or two broadcasters at once. Replays, youth and women's
    games and F1 practice are never announced.
- **Movies**: poster grid with sorting, a title filter, details sheet, Resume / Start over, progress bars, and mark as watched. Completed at 95%.
- **Series**: poster grid with sorting and a title filter; seasons → episodes (with an episode filter
  across all seasons for series with more than 12 episodes: name, plot, `5` or `s2e5`), per-episode progress, a Next-up button, and an auto "Up next" countdown at the end of an episode.
- **Storage**: favourites, progress, recently played, last channel, preferences and hidden channels/categories are saved on the Pi and shared by every device (see below). Volume, mute and the last search stay per device.
- **Player**:
  - Fullscreen (native fullscreen on iPhone), mute, volume (desktop), PiP and AirPlay.
  - Screen Wake Lock while playing, and Media Session (lock-screen controls; next/previous = channel).
  - Retry and Next-channel buttons on errors, with one silent automatic re-tune first.
- **Keyboard**:

  | Key | Action |
  |---|---|
  | ↑ / ↓ (also PgUp / PgDn) | channel up / down |
  | B or Backspace | previous channel |
  | Space or K | play / pause |
  | F | fullscreen |
  | M | mute |
  | P | picture-in-picture |
  | ← / → | −10 s / +30 s |
  | / | search |
  | Esc | close |

- **iPhone**: open https://example.com/tv in Safari → Share → **Add to Home Screen**. It runs standalone, respects safe areas and the notch, and is touch-first.

## The `iptv` command

```
iptv status              service, provider, catalogue/guide counts, active streams
iptv show-config         settings with URLs/credentials masked
iptv config              edit /etc/signal-tv-classic/tv.env (sudoedit), then reload
iptv set-playlist        hidden prompt for the M3U URL -> provider m3u -> reload
iptv set-epg             hidden prompt for the XMLTV URL (empty = none) -> reload
iptv set-xtream          prompts: server URL, username, password (hidden) -> provider xtream -> reload
iptv set-provider m3u|xtream
iptv use-demo            back to the built-in demo provider
iptv demo on|off         start/enable or stop/disable the demo provider service
iptv refresh | reload    re-read tv.env + refresh catalogue and guide (no restart, streams keep running)
iptv refresh --accept-changes   accept the next refresh even if it looks wrong (after a rejection)
iptv alert-test          send a test Telegram alert
iptv restart | start | stop
iptv logs [-f] [N]       journal for both services
iptv jobs                active ffmpeg jobs
iptv revoke-sessions     sign out all remembered browsers / home-screen apps
iptv backup              back up synced state + config + code now (also runs daily)
iptv backups             list backup archives
```

Secrets are typed at a hidden prompt and passed to the root helper on stdin, never as arguments, so they stay out of `ps` and shell history. Passing a URL as an argument also works, but prints a warning.

### Common tasks

- **Change the M3U URL**: `iptv set-playlist`
- **Change the EPG URL**: `iptv set-epg`
- **Use Xtream**: `iptv set-xtream` (and optionally `iptv set-epg`). Then turn the demo off with `iptv demo off`.
- **Restart / logs / refresh**: `iptv restart`, `iptv logs -f`, `iptv refresh`
- **Change limits or the user agent**: `iptv config` (keys are documented in `tv.env.example`)

## Sync, backup and restore

### Synced state

Favourites, playback progress (Continue Watching), recently played, last/previous channel,
preferences (filters, sort orders, last season opened, the match-alert setting), hidden
channels/categories and favourite sports teams/competitions live on the Pi in
`/var/lib/signal-tv-classic/state.db` (SQLite, `tvapp/userstate.py`). The browser keeps a copy in
`localStorage` (`tv.v2.*`), so the app reads it instantly and keeps working offline.

- **Sync**: a device sends its changes about 2 s after they happen (and straight away when the page
  is hidden or closed). It fetches other devices' changes on load, when the page becomes visible
  again, and every 30 s while it is open (`GET/POST /tv/api/state`, "changes since revision N").
- **Conflicts**: every item (one favourite, one title's progress, one preference…) carries the time
  it last changed. Per item, the newer change wins. Deletions are kept as tombstones for 180 days,
  so an un-favourite also wins over older copies. A device that was offline therefore cannot
  overwrite newer progress with its stale copy, while its own newer offline changes do win once
  sent. Timestamps use the Pi's clock (devices correct for their clock offset). An exact tie (same
  millisecond) is settled by content, never by which device's write arrived first.
- **Nothing is deleted by absence**: a deletion only ever comes from an explicit removal on some
  device (un-favourite, Clear recent, Mark as unwatched, show a hidden item). A new or empty
  browser only reads; a browser that lost part of its local copy downloads everything again.
- **Lost or replaced data**: if the Pi's database is recreated (new id), restored from an older
  snapshot (same id, lower revision), or found damaged at startup (moved aside as
  `state.db.damaged-*`, new one started), devices notice and send back everything they hold
  before taking anything from it, merged by the same newer-wins rule.
- **Migration**: on the first loads after the upgrade, each browser's old per-device data
  (`tv.v1.*`) is merged into the Pi with its original times (so it never beats newer data). It only
  counts as done once the Pi has accepted it; until then it is retried on every load, adding only
  items the browser does not hold yet. The old keys are never changed or removed.
- **Local copy**: every change is written to the browser's storage immediately, so it survives a
  reload or crash before it reaches the Pi.
- Volume, mute and last search are deliberately not synced.
- **Sports favourites** are stored by stable id (`team:arsenal`, `comp:uefa-champions-league`,
  from `tvapp/sports.py`), never by a name a guide used, so renaming or adding aliases keeps them
  working. The Pi's record of match alerts already sent (`sports_alerts` table) is in the same
  database: in every snapshot and backup, not synced to devices and not in the JSON export.

### Backup file (Library → Sync & backup)

- **Export backup** downloads `signal-tv-classic-state-<date>.json`: readable JSON with names, IDs,
  positions (`position_s` / `duration_s`) and times. It holds no provider URLs or credentials.
- **Import (merge)** adds what is newer in the file; nothing newer on the Pi is overwritten.
- **Restore (replace)** makes the file the entire saved state on every device (it asks first).
- Item IDs come from the provider's stream IDs, so a file restores fully onto any install using
  the same provider. With a different provider, the entries will not match its catalogue.

### System backups

`signal-tv-classic-backup.timer` runs daily at about 03:50 (`iptv backup` runs it now):

1. As `signal-tv-classic`: a consistent copy of `state.db` (+ JSON) into `/var/lib/signal-tv-classic/backups/`
   (keeps 14).
2. As root: `/var/backups/signal-tv-classic/signal-tv-classic-<UTC time>.tar.gz` (keeps 30, mode 0640
   root:pi). It holds that snapshot, `tv.env`, the systemd units, the nginx files, `iptv`, a
   git bundle of the source, the deployed code and `RESTORE.txt`. The snapshot is also kept as plain
   files in `/var/backups/signal-tv-classic/user-state/` (keeps 30).
3. As `pi` (`signal-tv-classic-backup-upload.service`, started when 2 succeeds): `user-state/` is copied
   to Google Drive, `gdrive:signal-tv-classic-backups/user-state/`, with the owner's rclone remote. Copies there
   older than 90 days are deleted. Only user data leaves the Pi: no `tv.env`, no credentials.
   `iptv status` / `iptv backups` show whether the last upload worked.

If Google's sign-in for rclone expires (uploads fail with `invalid_grant`), renew it. The Pi has no
browser: run `rclone config reconnect gdrive:` on the Pi and answer `n` to "Use web browser to
automatically authenticate". It prints an `rclone authorize "drive" …` command: run that on the Mac
(rclone is installed there), sign in to Google in the browser that opens, and paste the token it
prints back into the Pi. Then `iptv backup` to check.

Not included, on purpose: the catalogue/guide/details caches (re-downloaded on start), artwork and
remux caches, `secret.key` (a new one only means signing in once more), the venv
(`requirements.txt`), demo media (`demo/make-media.sh`) and the site-wide
`/etc/nginx/private.htpasswd`.

**The archive contains the provider credentials (`tv.env`)**, which is why only the user-state part
goes to Google Drive. The full archives stay on the SD card, so they protect against mistakes but not
against the card failing: copy one off the Pi now and then to somewhere private, e.g.
`scp 'pi:/var/backups/signal-tv-classic/signal-tv-classic-*.tar.gz' …`.

### Restore on a new Pi

Follow `RESTORE.txt` in the archive:

1. Install the packages and `git clone code/signal-tv-classic.bundle`.
2. Create the `signal-tv-classic` user, copy `etc/` and `usr/` into place, and run `deploy/install.sh`.
3. Put `user-state/state.sqlite3` at `/var/lib/signal-tv-classic/state.db` (owner `signal-tv-classic`, 0600).
4. Reload systemd and nginx, then enable `signal-tv-classic` and `signal-tv-classic-backup.timer`.

To restore only the user data, copy a snapshot over `state.db` while the service is stopped, or
use **Restore (replace)** with the JSON.

## Memory guard

`tvapp/memguard.py`, TV app only (no kernel cgroups, nothing else on the Pi affected):

- Every 10 s it adds up the memory of the app and its ffmpeg jobs.
- It restarts the service only if that total stays over `TV_MEMORY_LIMIT_MB` (2048, set in the
  unit) for a full minute, or passes 1.5× the limit (3 GB) at any check. Normal refresh peaks
  (~1.2 GB for seconds) are far below either.
- A restart means: log a `MEMORY GUARD:` error with a full breakdown (memory per ffmpeg job,
  catalogue sizes, caches, whether a refresh was running), record it in
  `/var/lib/signal-tv-classic/memory-guard.json`, stop the ffmpeg jobs cleanly and exit with code 75.
  systemd (`Restart=on-failure`, 5 s) starts it again.
- **No loops**: at most 3 such restarts in 6 hours. After that it only logs (CRITICAL, at most every
  30 minutes) until the window has passed.
- `iptv status` shows the current memory and the last guard event;
  `journalctl -u signal-tv-classic | grep 'MEMORY GUARD'` has the details.

## Development

- Source: `~/signal-tv-classic` (git). `deploy/install.sh` rsyncs it to
  `/opt/signal-tv-classic` (keeping `venv/` and `demo/media/`), installs `requirements.txt` and
  `/usr/local/bin/iptv`. Then run `sudo systemctl restart signal-tv-classic`. The nginx and systemd files
  in `deploy/` are copied by hand.
- After editing anything under `web/static`, change the `?v=` stamp everywhere it appears
  (`index.html`, `app.js`, `player.js`): static files are cached as immutable for a week.
- Tests, from the repo: `/opt/signal-tv-classic/venv/bin/python -m unittest discover -s tests` (store, API,
  list cache, backup helpers). The JavaScript tests need Node (not installed on the Pi; run them on
  the Mac): `TV_TEST_PYTHON=<python with requirements.txt> node --test tests/*.mjs`. `test_sync.mjs`
  runs the real `store.js` as several devices against a real server; `test_api.mjs` covers the list
  helpers.

## Demo provider

`signal-tv-classic-demo.service` serves the following on loopback only:
- an M3U and gzip XMLTV;
- four looping "broadcast" channels:
  - H.264/AAC;
  - H.264/MP2;
  - MPEG-2/MP2 (tests transcoding);
  - H.264/AC-3;
- two public test HLS streams (Mux, Apple);
- three generated test movies (MP4, MKV with AC-3 5.1, MP4 with the index at the end);
- the Blender open movie *Tears of Steel* (CC BY 3.0, proxied from download.blender.org);
- a five-episode demo series.

It also emulates the Xtream API with username `demo` / password `demo`. These are demo-only credentials that never touch your real provider.

Media is generated locally by `demo/make-media.sh` (ffmpeg test sources, no downloads).

## Limitations

- No catch-up, timeshift or recordings. Subtitles are the ones embedded in the file only (no
  OpenSubtitles/SubDL yet); picture-based subtitles (PGS/DVD/DVB) can't be shown.
- Changing subtitle or audio restarts the remux (2–4 s). One subtitle at a time (ffmpeg 5.1 allows
  one WebVTT rendition per output).
- iPhone native fullscreen uses Apple's player: its scrubber covers only what has been remuxed so far
  and may show "LIVE" while a remux is still growing (as before 1.3.0); the app's own bar shows the
  full length. Subtitles there follow the app's choice; Apple's own Subtitles menu can also turn
  them off/on. PiP and AirPlay could not be tested in the Simulator.
- Firefox draws subtitles at the very bottom even while the controls are showing (Safari and Chrome
  lift them above the bar).
- Seeking in remuxed titles (MKV and similar) lands on the nearest keyframe before the requested time.
- The first play of a remuxed title, or seeking past what has been remuxed, waits a few seconds for ffmpeg.
- Converting video uses software x264 (the Pi 5 has no H.264 encoder). It is used only when it
  can keep up: 4K HEVC can't (0.32× in software; ≤ 0.64× even with the Pi's hardware HEVC decoder),
  so such a channel plays as-is on HEVC-capable devices or as its best H.264 version elsewhere.
- Programme search knows ~240 teams and competitions (`tvapp/sports.py`). Other names are matched
  literally, and a match is grouped across broadcasters only when its teams can be read from
  the titles. Greek/Cyrillic team names, and clubs not in the table under other languages'
  names ("Bayern Monachium"), stay separate. A delayed broadcast with no replay marker, and no
  whole earlier airing in the last 3 h, shows as "live". Only
  channels with guide data can be found by programme. The guide has no category tags, so
  competitions are found only where titles or descriptions name them.
- The first time a channel is played, its format is probed (+1–2 s). The first time Auto has to
  find another version, it may probe a few versions (up to ~12 s, once; then cached). Probing is
  skipped while another stream holds the provider's single connection.
- A provider feed that itself arrives slower than real time, or keeps dropping, will still buffer;
  diagnostics and the log show it (ffmpeg "speed=0.95x", reconnects).
- The Range proxy streams through the Pi and Cloudflare, so upload bandwidth limits quality for remote viewing. Providers that cap connections at 1 may refuse the parallel range requests Safari makes for direct-play MP4s.
- The server only fetches URLs that appear in the playlist or provider you configure, including private-network addresses if the playlist lists them. Point it only at a provider you trust.
- Memory: about 550–650 MB after start and ~700–780 MB between refreshes (the Live search index is
  ~40 MB of that) (freed memory is handed back
  after each one); a refresh peaks at ~1.0–1.2 GB while the old and new catalogue briefly coexist.
  `MemoryMax=2G` in the unit is not enforced (Raspberry Pi OS boots with `cgroup_disable=memory`;
  enabling it Pi-wide is a separate infrastructure task: `docs/INFRA-memory-cgroups.md`). Instead the
  app guards itself, see **Memory guard** below.
