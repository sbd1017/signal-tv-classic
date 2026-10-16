// One <video> element for everything. Live channels play docked in the Live view;
// movies/episodes play in a full-window overlay. Channel switches only swap the
// source - the page never reloads.
import { api, BASE, clientId, ApiError, fmtTime, setIcon, toast } from './api.js?v=20261016190000';
import * as store from './store.js?v=20261016190000';
import * as tracks from './tracks.js?v=20261016190000';
import * as caps from './caps.js?v=20261016190000';

const $ = (id) => document.getElementById(id);
const UA = navigator.userAgent;
const IS_APPLE_TOUCH = /iP(hone|ad|od)/.test(UA) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
const IS_SAFARI = /Safari\//.test(UA) && !/Chrome|Chromium|CriOS|Edg|OPR|Firefox|FxiOS/.test(UA);
let HlsLib = null;
const SEEK_SETTLE_MS = 700;      // remuxed titles: wait this long after the last skip press before restarting

function loadHls() {
  if (HlsLib) return Promise.resolve(HlsLib);
  return new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = '/tv/static/vendor/hls.min.js?v=20261016190000';
    s.onload = () => (window.Hls ? resolve((HlsLib = window.Hls)) : reject(new Error('hls.js missing')));
    s.onerror = () => reject(new Error('Could not load the HLS player.'));
    document.head.append(s);
  });
}

function trackPrefs() {
  const out = {};
  for (const [k, d] of Object.entries(tracks.DEFAULTS)) out[k] = store.prefs.get(k, d);
  return out;
}

export class Player {
  constructor(hooks) {
    this.hooks = hooks;            // {onChannel(delta), onLastChannel(), onClosed(item), nextEpisode(item), onStarted(item)}
    this.v = $('video');
    this.section = $('player');
    this.stage = $('stage');
    this.seq = 0;
    this.item = null;
    this.res = null;               // play response
    this.hls = null;
    this.ctl = null;
    this.retried = false;
    this.lastSave = 0;
    this.wake = null;
    this.beat = 0;
    this.uiTimer = 0;
    this.upnextTimer = 0;
    this.seeking = false;
    this.target = null;            // remuxed title: position a pending restart / the loading player will go to
    this.seekTimer = 0;
    this.loadingAt = null;         // VOD position being loaded (no play response yet)
    this.lastDur = 0;              // duration of the current title, kept across remux restarts
    this.pick = null;              // {a, s}: tracks picked in the menus for the title playing (kept across seeks)
    this.menu = null;              // open track menu: 'subs' | 'audio'
    const probe = document.createElement('video');
    this.nativeHls = !!probe.canPlayType('application/vnd.apple.mpegurl');
    this.preferNative = this.nativeHls && (IS_APPLE_TOUCH || IS_SAFARI);
    this.hevc = !!(probe.canPlayType('video/mp4; codecs="hvc1.1.6.L93.B0"') ||
      (window.MediaSource && MediaSource.isTypeSupported && MediaSource.isTypeSupported('video/mp4; codecs="hvc1.1.6.L93.B0"')));
    this.bind();
  }

  get mode() { return this.section.dataset.mode || 'off'; }
  get isLive() { return this.item && this.item.kind === 'live'; }
  get playing() { return !this.v.paused && !this.v.ended; }

  // ---------------------------------------------------------------- opening
  async open(item, { resume = 0, user = true, pick = null } = {}) {
    const seq = ++this.seq;
    if (user) this.v.play().catch(() => {});      // unlock media playback inside the user gesture (iOS)
    const prevJob = this.res && this.res.job;
    const prevItem = this.item;
    clearTimeout(this.seekTimer);
    if (!prevItem || prevItem.id !== item.id) { this.target = null; this.lastDur = 0; this.pick = null; }
    if (pick) this.pick = pick;
    this.closeMenu();
    this.loadingAt = item.kind === 'live' ? null : resume;
    this.teardown();
    if (this.ctl) this.ctl.abort();
    this.ctl = new AbortController();
    this.item = item;
    this.res = null;
    this.retried = user ? false : this.retried;
    this.showMode(item.kind === 'live' ? 'live' : 'vod');
    this.setTitles(item.name, item.sub || '');
    this.overlay('spinner', item.kind === 'live' ? 'Tuning…' : 'Loading…');
    this.hideUpNext();
    this.updateMediaSession();
    const qs = new URLSearchParams({ c: clientId, hevc: this.hevc ? '1' : '0' });
    if (prevJob) qs.set('prev', prevJob);
    if (resume > 0) qs.set('t', String(Math.floor(resume)));
    if (item.series) qs.set('series', item.series);
    if (item.kind !== 'live') {
      const params = tracks.playParams(trackPrefs(), store.prefs.get(tracks.titleKey(item), ''), this.pick);
      for (const [k, val] of Object.entries(params)) qs.set(k, val);
    } else {
      // what this device decodes + the quality chosen for this channel: the server picks the version
      const c = await caps.get(this.preferNative).catch(() => '');
      if (seq !== this.seq) return;
      if (c) qs.set('caps', c);
      const q = store.prefs.get(`lq.${item.id}`, 'auto');
      if (q !== 'auto') qs.set('q', q);
    }
    $('p-retry').textContent = 'Retry';
    this.retryAuto = false;
    let res;
    try {
      res = await api(`/play/${encodeURIComponent(item.id)}?${qs}`, { signal: this.ctl.signal, timeout: 60000 });
    } catch (e) {
      if (seq !== this.seq || (e && e.name === 'AbortError')) return;
      if (e instanceof ApiError && e.status === 409 && e.body && e.body.unplayable) {
        // the version chosen by hand can't be played smoothly here: say so, offer the best one
        this.res = { kind: 'live', variants: e.body.variants || [], quality: store.prefs.get(`lq.${item.id}`, 'auto') };
        this.paintTrackButtons();
        this.fail(e.message);
        $('p-retry').textContent = 'Play best version';
        this.retryAuto = true;
        return;
      }
      this.fail(e instanceof ApiError ? e.message : 'Could not reach the server.');
      return;
    }
    if (seq !== this.seq) {                        // user switched again while we waited: free that job
      if (res && res.job) this.release(res.job);
      return;
    }
    this.res = res;
    this.loadingAt = null;
    if (res.duration) this.lastDur = res.duration;
    if (prevItem && prevItem.id !== item.id && prevItem.kind === 'live' && item.kind === 'live') this.prevLive = prevItem;
    try {
      await this.attach(res);
    } catch (e) {
      if (seq === this.seq) this.fail(e.message || 'The player could not start.');
      return;
    }
    if (this.target != null) {                     // skipped again while it was loading
      const want = this.target;
      const loaded = res.mode === 'hls' ? res.offset || 0 : res.start || 0;
      this.target = null;
      if (Math.abs(want - loaded) > 2) this.seekTo(want);
    }
    if (res.job) this.beat = setInterval(() => this.heartbeat(), 15000);
    if (res.notice) toast(res.notice);
    if (item.kind === 'live') this.watchSmoothness(res);
    if (item.kind === 'live') store.lastChannel.set(item.id, { ...item, kind: 'live' });
    store.recent.add(item.id, item);
    if (this.hooks.onStarted) this.hooks.onStarted(item, res);
  }

  async attach(res) {
    const v = this.v;
    const isHls = res.mode === 'hls';
    this.$seekWrap.hidden = this.isLive;
    this.paintTrackButtons();
    if (isHls && !this.preferNative) {
      const Hls = await loadHls().catch((e) => { if (this.nativeHls) return null; throw e; });
      if (Hls && Hls.isSupported()) {
        const live = this.isLive;
        const hls = new Hls({
          enableWorker: true,
          startPosition: live ? -1 : 0,
          // live: the provider delivers in bursts with gaps of 4-12 s (measured), so the player keeps
          // ~12 s (6 segments) behind the edge - 3 segments stalled 1-2x a minute on 4K, 6 had none
          // in 2 x 150 s - and may drift to ~40 s behind after a hiccup rather than jump back to the
          // edge, which would only empty the cushion again
          liveSyncDurationCount: 6,
          liveMaxLatencyDurationCount: 20,
          maxBufferLength: live ? 30 : 45,
          backBufferLength: live ? 30 : 90,
          manifestLoadingMaxRetry: 4,
          levelLoadingMaxRetry: 6,
          fragLoadingMaxRetry: 6,
        });
        this.hls = hls;
        this.hlsRecovered = 0;
        hls.on(Hls.Events.ERROR, (_e, data) => {
          if (!data.fatal || this.hls !== hls) return;
          if (data.type === Hls.ErrorTypes.MEDIA_ERROR && this.hlsRecovered++ < 2) { hls.recoverMediaError(); return; }
          if (data.type === Hls.ErrorTypes.MEDIA_ERROR) this.decodeFailed();
          this.mediaFailed(data.type === Hls.ErrorTypes.NETWORK_ERROR ? 'The stream stopped responding.' : 'The stream could not be decoded.');
        });
        if (this.wantSubs()) {
          hls.on(Hls.Events.SUBTITLE_TRACKS_UPDATED, () => { hls.subtitleTrack = 0; hls.subtitleDisplay = true; });
        }
        hls.loadSource(res.url);
        hls.attachMedia(v);
        this.tryPlay();
        return;
      }
    }
    v.src = res.url;
    if (res.mode === 'file' && res.start > 1) {
      v.addEventListener('loadedmetadata', () => { try { v.currentTime = res.start; } catch { /* ignore */ } }, { once: true });
    }
    this.tryPlay();
  }

  tryPlay() {
    const p = this.v.play();
    if (p && p.catch) {
      p.catch((e) => {
        if (e && e.name === 'NotAllowedError') this.overlay('tap');
      });
    }
  }

  // ------------------------------------------------------------- tracks
  wantSubs() {
    return !!(this.res && this.res.tracks && this.res.tracks.s != null);
  }

  // Show the remux's subtitle rendition (or hide stale tracks). Native HLS (Safari/iOS) creates
  // the TextTrack itself; hls.js creates one per rendition and is told to show it in attach().
  syncTextTracks() {
    const want = this.wantSubs();
    for (const t of this.v.textTracks) {
      if (t.kind !== 'subtitles' && t.kind !== 'captions') continue;
      const mode = want ? 'showing' : 'disabled';
      if (t.mode !== mode) t.mode = mode;
    }
  }

  paintTrackButtons() {
    const info = this.res && this.res.tracks;
    const cc = $('p-cc');
    cc.hidden = !info;
    cc.classList.toggle('on', !!(info && info.s != null));
    cc.setAttribute('aria-pressed', info && info.s != null ? 'true' : 'false');
    $('p-audio').hidden = !(info && info.audio && info.audio.length > 1);
    const r = this.res || {};
    $('p-quality').hidden = !(r.variants && (r.variants.length > 1 || (r.quality && r.quality !== 'auto')));
  }

  toggleMenu(which) {
    if (this.menu === which) { this.closeMenu(); return; }
    if (!this.res || (which === 'quality' ? !this.res.variants : !this.res.tracks)) return;
    this.menu = which;
    this.renderMenu();
    this.stage.classList.add('show-ui');
  }

  closeMenu() {
    this.menu = null;
    const m = $('p-menu');
    if (m) { m.hidden = true; m.classList.remove('wide'); m.replaceChildren(); }
  }

  // Playback report for a bug report: the server's part (redacted there) + this browser's state.
  async showDiag() {
    if (!this.item) return;
    if (this.menu === 'diag') { this.closeMenu(); return; }
    const m = $('p-menu');
    const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text) e.textContent = text; return e; };
    this.menu = 'diag';
    m.classList.add('wide');
    m.replaceChildren(el('div', 'p-menu-head', 'Diagnostics'), el('p', 'p-menu-empty', 'Collecting…'));
    m.hidden = false;
    this.stage.classList.add('show-ui');
    const q = new URLSearchParams({ mode: this.res ? this.res.mode : '' });
    if (this.res && this.res.job) q.set('job', this.res.job);
    const c = await caps.get(this.preferNative).catch(() => '');
    if (c) q.set('caps', c);
    let text;
    try {
      text = (await api(`/diag/${encodeURIComponent(this.item.id)}?${q}`, { timeout: 15000 })).text;
    } catch (e) { text = `Server report unavailable: ${e.message || e}`; }
    if (this.menu !== 'diag') return;
    const v = this.v;
    const tt = [...v.textTracks].map((t) => `${t.label || t.language || t.kind}:${t.mode}`).join(', ') || 'none';
    const engine = this.hls ? `hls.js ${window.Hls && window.Hls.version ? window.Hls.version : ''}`.trim()
      : this.res && this.res.mode === 'file' ? 'direct (native)' : 'native HLS';
    text += `\nbrowser:   ${navigator.userAgent}\nplayer:    ${engine}; position ${fmtTime(this.absTime())} of ` +
      `${fmtTime(this.duration())}; readyState ${v.readyState}; ${v.paused ? 'paused' : 'playing'}` +
      `${v.error ? `; media error ${v.error.code}` : ''}; ${v.videoWidth}x${v.videoHeight}\ntext tracks: ${tt}`;
    const pre = el('pre', 'p-diag', text);
    const copy = el('button', 'btn small', 'Copy report');
    copy.type = 'button';
    copy.addEventListener('click', async (e) => {
      e.stopPropagation();
      try { await navigator.clipboard.writeText(text); toast('Diagnostics copied'); } catch {
        const r = document.createRange(); r.selectNodeContents(pre);
        const sel = getSelection(); sel.removeAllRanges(); sel.addRange(r);
        toast('Selected - copy it with your device’s Copy');
      }
    });
    m.replaceChildren(el('div', 'p-menu-head', 'Diagnostics'), pre, copy);
  }

  renderMenu() {
    const m = $('p-menu');
    const info = this.res.tracks || {};
    const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text) e.textContent = text; return e; };
    const item = (it, onPick) => {
      const b = el('button', 'p-menu-item');
      b.type = 'button';
      b.setAttribute('role', 'menuitemradio');
      b.setAttribute('aria-checked', it.on ? 'true' : 'false');
      b.disabled = !!it.disabled;
      b.append(el('span', 'p-menu-check', it.on ? '✓' : ''), el('span', 'p-menu-label', it.label));
      if (it.note) b.append(el('span', 'p-menu-note', it.note));
      b.addEventListener('click', (e) => { e.stopPropagation(); onPick(it.key); });
      return b;
    };
    const head = el('div', 'p-menu-head', this.menu === 'subs' ? 'Subtitles' : this.menu === 'quality' ? 'Quality' : 'Audio');
    const body = [head];
    if (this.menu === 'subs') {
      const memory = store.prefs.get(tracks.titleKey(this.item), '');
      const menu = tracks.subtitleMenu(info, trackPrefs(), memory);
      if (menu.empty) body.push(el('p', 'p-menu-empty', menu.empty));
      for (const it of menu.items) body.push(item(it, (k) => this.pickSubtitle(k)));
      if (menu.more.length) {
        const more = el('button', 'p-menu-more', `More languages (${menu.more.length})`);
        more.type = 'button';
        const list = el('div', 'p-menu-list');
        list.hidden = !menu.more.some((it) => it.on);
        for (const it of menu.more) list.append(item(it, (k) => this.pickSubtitle(k)));
        more.addEventListener('click', (e) => { e.stopPropagation(); list.hidden = !list.hidden; });
        body.push(more, list);
      }
    } else if (this.menu === 'quality') {
      for (const it of this.qualityMenu()) body.push(item(it, (k) => this.pickQuality(k)));
    } else {
      for (const it of tracks.audioMenu(info)) body.push(item(it, (k) => this.pickAudio(k)));
    }
    m.replaceChildren(...body);
    m.hidden = false;
    const first = m.querySelector('[aria-checked="true"]') || m.querySelector('button');
    if (first) first.focus({ preventScroll: true });
  }

  // A different subtitle/audio means a different remux: restart it here (a 2-4 s pause).
  pickSubtitle(key) {
    const info = this.res.tracks;
    const subs = info.subs || [];
    const key2 = tracks.titleKey(this.item);
    const memory = store.prefs.get(key2, '');
    if (key === 'off') store.prefs.set(key2, tracks.memoryFor('off', null, memory));
    else if (key === 'auto') store.prefs.set(key2, tracks.memoryFor('auto', null, memory));
    else store.prefs.set(key2, tracks.memoryFor(subs[key], null, memory));
    this.closeMenu();
    const s = key === 'off' ? null : key === 'auto' ? undefined : key;
    if (s !== undefined && s === info.s) return;
    this.osd(key === 'off' ? 'Subtitles off' : key === 'auto' ? 'Subtitles: forced only' : `Subtitles: ${subs[key].label}`);
    const pick = { a: info.a };
    if (s !== undefined) pick.s = s;
    this.open(this.item, { resume: this.absTime(), user: false, pick });
  }

  pickAudio(key) {
    const info = this.res.tracks;
    const t = info.audio[key];
    const key2 = tracks.titleKey(this.item);
    store.prefs.set(key2, tracks.memoryFor(null, t, store.prefs.get(key2, '')));
    this.closeMenu();
    if (key === info.a) return;
    this.osd(`Audio: ${t.label}`);
    this.open(this.item, { resume: this.absTime(), user: false, pick: { a: key, s: info.s } });
  }

  // ------------------------------------------------------------- live quality
  // Real playback check: MediaCapabilities can promise more than the device delivers (Firefox on
  // the Pi: "smooth" 4K HEVC, 98% of frames dropped). Over the first ~30 s, if more than 30% of
  // frames are dropped, record the format as failing for this device and (in Auto) switch version.
  watchSmoothness(res) {
    clearInterval(this.smoothTimer);
    const meta = res && res.meta;
    if (meta && meta.interlaced && !res.transcode) this.watchInterlaced(res);
    if (!meta || !meta.h || !this.v.getVideoPlaybackQuality) return;
    let base = null;
    let checks = 0;
    const seq = this.seq;
    this.smoothTimer = setInterval(() => {
      if (seq !== this.seq || checks++ > 16) { clearInterval(this.smoothTimer); return; }
      if (this.v.paused) return;
      const q = this.v.getVideoPlaybackQuality();
      if (!q.totalVideoFrames) return;
      if (!base) { base = q; return; }
      const total = q.totalVideoFrames - base.totalVideoFrames;
      const dropped = q.droppedVideoFrames - base.droppedVideoFrames;
      if (total < 150 || dropped / total <= 0.3) return;
      clearInterval(this.smoothTimer);
      caps.markFailing(caps.codecKey(meta), meta.h, meta.fps);
      if (res.quality && res.quality !== 'auto') { toast('This device is dropping frames on this version - Auto would pick a smoother one'); return; }
      toast(`This device can't decode ${res.variant && res.variant.label ? res.variant.label : 'this version'} smoothly - switching`);
      this.open(this.item, { user: false });
    }, 2000);
  }

  // Interlaced H.264 copied unchanged: the only way to know whether this device decodes it is to
  // watch. No picture while the clock (audio) runs on -> record it as failing here and let Auto
  // choose again (a progressive version, or the Pi's deinterlaced conversion); pictures for 30 s
  // -> record it as working, so Auto may pick this format here next time.
  watchInterlaced(res) {
    const meta = res.meta;
    const v = this.v;
    const seq = this.seq;
    let painted = 0;
    let start = null;
    const count = () => { if (seq !== this.seq) return; painted++; if (v.requestVideoFrameCallback) v.requestVideoFrameCallback(count); };
    if (v.requestVideoFrameCallback) v.requestVideoFrameCallback(count);
    const frames = () => (v.requestVideoFrameCallback ? painted
      : (v.getVideoPlaybackQuality ? (q => q.totalVideoFrames - q.droppedVideoFrames)(v.getVideoPlaybackQuality()) : -1));
    const key = caps.codecKey(meta);
    const timer = setInterval(() => {
      if (seq !== this.seq) { clearInterval(timer); return; }
      if (v.paused || v.readyState < 2) return;
      if (!start) { start = { t: v.currentTime, f: frames(), at: Date.now() }; return; }
      const ran = v.currentTime - start.t;
      const shown = frames() - start.f;
      if (ran >= 5 && shown === 0) {
        clearInterval(timer);
        caps.markFailing(key, meta.h, meta.fps);
        this.reswitch(res, `This browser can't decode ${res.variant && res.variant.label ? res.variant.label : 'this interlaced version'}`);
      } else if (ran >= 30) {
        clearInterval(timer);
        if (shown >= ran * (meta.fps || 25) * 0.8) caps.markWorking(key, meta.h, meta.fps);
      }
    }, 1000);
  }

  // Auto chooses again for this device (after recording what failed); a version picked by hand stays.
  reswitch(res, why) {
    if (res.quality && res.quality !== 'auto') { toast(`${why} - Auto would pick one that plays`); return; }
    toast(`${why} - switching`);
    this.open(this.item, { user: false });
  }

  qualityMenu() {
    const r = this.res || {};
    const cur = r.quality || 'auto';
    const items = [{ key: 'auto', label: 'Auto - best smooth quality', on: cur === 'auto',
      note: cur === 'auto' && r.variant ? `Now: ${r.variant.label}` : '' }];
    for (const v of r.variants || []) {
      const key = v.id === (this.item && this.item.id) ? 'orig' : v.id;
      items.push({ key, label: v.label, on: cur === key || (cur !== 'auto' && cur === v.id),
        note: v.name + (v.playable === 0 ? ' - not supported on this device' : v.playable === 1 ? ' - may not be smooth here' : '') });
    }
    return items;
  }

  pickQuality(key) {
    if (!this.item) return;
    store.prefs.set(`lq.${this.item.id}`, key);
    this.closeMenu();
    this.open(this.item, { user: false });
  }

  teardown() {
    clearInterval(this.beat);
    this.beat = 0;
    if (this.hls) { try { this.hls.destroy(); } catch { /* ignore */ } this.hls = null; }
    const v = this.v;
    if (v.getAttribute('src')) {
      v.removeAttribute('src');
      try { v.load(); } catch { /* ignore */ }
    }
    this.releaseWake();
  }

  release(job) {
    fetch(`${BASE}/stop?c=${clientId}&job=${encodeURIComponent(job)}`, { keepalive: true, credentials: 'same-origin' }).catch(() => {});
  }

  stop() {
    this.saveProgress(true);
    clearTimeout(this.seekTimer);
    this.seq++;
    if (this.ctl) this.ctl.abort();
    if (this.res && this.res.job) this.release(this.res.job);
    this.teardown();
    const item = this.item;
    this.item = null;
    this.res = null;
    this.target = null;
    this.loadingAt = null;
    this.pick = null;
    clearInterval(this.smoothTimer);
    this.closeMenu();
    this.hideUpNext();
    this.overlay(null);
    if (document.pictureInPictureElement) document.exitPictureInPicture().catch(() => {});
    return item;
  }

  close() {
    const item = this.stop();
    this.showMode('off');
    if (this.hooks.onClosed) this.hooks.onClosed(item);
  }

  retry() {
    if (!this.item) return;
    if (this.retryAuto) { store.prefs.set(`lq.${this.item.id}`, 'auto'); }
    const resume = this.isLive ? 0 : this.absTime();
    this.open(this.item, { resume });
  }

  heartbeat() {
    if (!this.res || !this.res.job) return;
    api(`/heartbeat?c=${clientId}&job=${this.res.job}`, { timeout: 10000 }).catch(() => {});
  }

  // ------------------------------------------------------------------- time
  absTime() {
    if (this.target != null) return this.target;
    if (!this.res) return this.loadingAt || 0;
    return (this.res.mode === 'hls' ? this.res.offset || 0 : 0) + (this.v.currentTime || 0);
  }

  duration() {
    if (!this.res) return this.lastDur;
    if (this.res.duration) return this.res.duration;
    return isFinite(this.v.duration) ? this.v.duration : 0;
  }

  seekTo(abs) {
    if (!this.item || this.isLive || (!this.res && this.loadingAt == null)) return;
    const dur = this.duration();
    abs = Math.max(0, dur ? Math.min(abs, dur - 1) : abs);
    if (!this.res) { this.pendingSeek(abs, false); return; }      // still loading: applied once it plays
    if (this.res.mode === 'file') {
      if (this.v.readyState < 1) this.res.start = abs;              // before metadata: attach() applies it
      this.v.currentTime = abs;
      return;
    }
    const rel = abs - (this.res.offset || 0);
    const s = this.v.seekable;
    const end = s && s.length ? s.end(s.length - 1) : 0;
    if (rel >= 0 && rel <= end - 1) {                             // already remuxed: instant
      clearTimeout(this.seekTimer);
      this.target = null;
      this.v.currentTime = rel;
      return;
    }
    this.pendingSeek(abs, true);
  }

  // Outside what has been remuxed: restart the remux at the new position - once, after the
  // presses stop, so ten taps on +30 s are one restart at +5 min rather than one at +30 s.
  pendingSeek(abs, restart) {
    this.target = abs;
    this.renderTime();
    this.osd(fmtTime(abs));
    clearTimeout(this.seekTimer);
    if (!restart) return;
    this.seekTimer = setTimeout(() => {
      const t = this.target;
      this.target = null;
      if (this.item && t != null) this.open(this.item, { resume: t, user: false });
    }, SEEK_SETTLE_MS);
  }

  saveProgress(force = false) {
    if (!this.item || this.isLive || !this.res) return;
    const now = Date.now();
    if (!force && now - this.lastSave < 5000) return;
    const t = this.absTime();
    const d = this.duration();
    if (t < 5 || !d) return;
    this.lastSave = now;
    store.progress.set(this.item.id, t, d, this.item);
  }

  // --------------------------------------------------------------- overlays
  showMode(mode) {
    $('p-live').hidden = mode !== 'live';
    this.section.dataset.mode = mode;
    this.section.hidden = mode === 'off';
    document.body.classList.toggle('vod-open', mode === 'vod');
    this.stage.classList.toggle('is-live', mode === 'live');
  }

  overlay(which, text) {
    $('p-spinner').hidden = which !== 'spinner';
    $('p-error').hidden = which !== 'error';
    $('p-idle').hidden = which !== 'idle';
    this.stage.classList.toggle('needs-tap', which === 'tap');
    if (which === 'spinner') $('p-spinner-text').textContent = text || 'Loading…';
    if (which === 'error') {
      $('p-error-text').textContent = text || 'Something went wrong.';
      $('p-skip').hidden = !this.isLive;
      this.stage.classList.add('show-ui');
    }
  }

  showIdle(ch) {
    this.showMode('live');
    this.setTitles(ch.name, 'Tap Resume to continue watching');
    $('p-idle-name').textContent = ch.name;
    const logo = $('p-idle-logo');
    if (ch.img) { logo.src = `${BASE}/img/${ch.img}`; logo.hidden = false; } else logo.hidden = true;
    this.idleChannel = ch;
    $('p-live').hidden = true;
    this.overlay('idle');
  }

  fail(message) {
    this.teardown();
    this.overlay('error', message);
  }

  // A live copy of interlaced H.264 that the decoder rejected: remember it for this device, so the
  // silent re-tune below lets Auto choose a version that decodes.
  decodeFailed() {
    const r = this.res;
    if (!this.isLive || !r || !r.meta || !r.meta.interlaced || r.transcode) return;
    caps.markFailing(caps.codecKey(r.meta), r.meta.h, r.meta.fps);
  }

  mediaFailed(message) {
    // a reaped/restarted live job: one silent re-tune before showing the error
    if (this.item && !this.retried) {
      this.retried = true;
      const resume = this.isLive ? 0 : this.absTime();
      this.open(this.item, { resume, user: false });
      return;
    }
    this.fail(message);
  }

  setTitles(title, sub) {
    $('p-title').textContent = title || '';
    $('p-sub').textContent = sub || '';
  }

  osd(text) {
    const o = $('p-osd');
    o.textContent = text;
    o.classList.remove('show');
    void o.offsetWidth;
    o.classList.add('show');
  }

  async showUpNext() {
    if (!this.hooks.nextEpisode || !this.item || this.item.kind !== 'episode') return;
    const next = await this.hooks.nextEpisode(this.item).catch(() => null);
    if (!next || !this.item) return;
    $('p-upnext-name').textContent = next.sub ? `${next.sub} · ${next.name}` : next.name;
    $('p-upnext').hidden = false;
    let n = 8;
    const btn = $('p-upnext-play');
    btn.textContent = `Play now (${n})`;
    this.upnextTimer = setInterval(() => {
      n -= 1;
      btn.textContent = `Play now (${n})`;
      if (n <= 0) { this.hideUpNext(); this.open(next, { user: false }); }
    }, 1000);
    this.upnext = next;
  }

  hideUpNext() {
    clearInterval(this.upnextTimer);
    this.upnextTimer = 0;
    $('p-upnext').hidden = true;
  }

  // ---------------------------------------------------------- media session
  updateMediaSession() {
    if (!('mediaSession' in navigator) || !this.item) return;
    const art = this.item.img ? [{ src: `${BASE}/img/${this.item.img}`, sizes: '512x512' }] : [];
    try {
      navigator.mediaSession.metadata = new MediaMetadata({ title: this.item.name, artist: this.item.sub || 'TV', artwork: art });
    } catch { /* old Safari */ }
    const set = (a, fn) => { try { navigator.mediaSession.setActionHandler(a, fn); } catch { /* unsupported */ } };
    const live = this.isLive;
    set('play', () => this.v.play());
    set('pause', () => this.v.pause());
    set('previoustrack', live ? () => this.hooks.onChannel(-1) : null);
    set('nexttrack', live ? () => this.hooks.onChannel(1) : null);
    set('seekbackward', live ? null : () => this.seekTo(this.absTime() - 10));
    set('seekforward', live ? null : () => this.seekTo(this.absTime() + 30));
  }

  async requestWake() {
    if (!('wakeLock' in navigator) || this.wake || document.visibilityState !== 'visible') return;
    try {
      this.wake = await navigator.wakeLock.request('screen');
      this.wake.addEventListener('release', () => { this.wake = null; });
    } catch { this.wake = null; }
  }

  releaseWake() {
    if (this.wake) { this.wake.release().catch(() => {}); this.wake = null; }
  }

  // --------------------------------------------------------------- controls
  togglePlay() {
    if (!this.item) { if (this.idleChannel) this.hooks.resumeIdle(); return; }
    if (this.v.paused) { this.tryPlay(); } else this.v.pause();
  }

  toggleMute() {
    this.v.muted = !this.v.muted;
    store.prefs.set('muted', this.v.muted);
    this.osd(this.v.muted ? 'Muted' : 'Sound on');
  }

  toggleFullscreen() {
    const d = document;
    if (d.fullscreenElement || d.webkitFullscreenElement) {
      (d.exitFullscreen || d.webkitExitFullscreen).call(d);
      return;
    }
    const el = this.stage;
    if (el.requestFullscreen && !IS_APPLE_TOUCH) el.requestFullscreen().catch(() => {});
    else if (el.webkitRequestFullscreen && !/iPhone|iPod/.test(UA)) el.webkitRequestFullscreen();
    else if (this.v.webkitEnterFullscreen) this.v.webkitEnterFullscreen();
  }

  async togglePip() {
    const v = this.v;
    try {
      if (document.pictureInPictureElement) await document.exitPictureInPicture();
      else if (v.requestPictureInPicture && document.pictureInPictureEnabled) await v.requestPictureInPicture();
      else if (v.webkitSetPresentationMode) {
        v.webkitSetPresentationMode(v.webkitPresentationMode === 'picture-in-picture' ? 'inline' : 'picture-in-picture');
      }
    } catch { toast('Picture in Picture is not available right now.'); }
  }

  wakeUi() {
    this.stage.classList.add('show-ui');
    clearTimeout(this.uiTimer);
    this.uiTimer = setTimeout(() => {
      if (this.playing && !this.menu && !this.stage.matches(':focus-within')) this.stage.classList.remove('show-ui');
    }, 3200);
  }

  renderTime() {
    if (!this.item || this.isLive) return;
    const t = this.absTime();
    const d = this.duration();
    $('p-pos').textContent = fmtTime(t);
    $('p-dur').textContent = d ? fmtTime(d) : '--:--';
    if (!this.seeking) {
      const val = d ? Math.round((t / d) * 1000) : 0;
      this.$seek.value = String(val);
      this.$seek.style.setProperty('--fill', `${val / 10}%`);
    }
  }

  bind() {
    const v = this.v;
    this.$seek = $('p-seek');
    this.$seekWrap = $('p-seek-wrap');
    v.muted = !!store.prefs.get('muted', false);
    const vol = store.prefs.get('volume', 1);
    v.volume = vol;
    $('p-vol').value = String(vol);
    const syncPlay = () => {
      setIcon($('p-play'), v.paused ? 'play' : 'pause');
      $('p-play').setAttribute('aria-label', v.paused ? 'Play' : 'Pause');
      this.stage.classList.toggle('paused', v.paused);
    };
    v.addEventListener('play', syncPlay);
    v.addEventListener('pause', () => { syncPlay(); this.saveProgress(true); this.releaseWake(); this.stage.classList.add('show-ui'); });
    v.addEventListener('playing', () => {
      this.overlay(null);
      $('p-live').hidden = !this.isLive;
      this.retried = false;
      this.requestWake();
      this.wakeUi();
    });
    v.addEventListener('waiting', () => { if (this.item) this.overlay('spinner', 'Buffering…'); });
    v.addEventListener('canplay', () => { if (!$('p-spinner').hidden && !v.paused) this.overlay(null); });
    v.addEventListener('timeupdate', () => { this.renderTime(); this.saveProgress(); });
    v.addEventListener('durationchange', () => this.renderTime());
    v.addEventListener('volumechange', () => {
      setIcon($('p-mute'), v.muted || v.volume === 0 ? 'muted' : 'volume');
      $('p-vol').value = String(v.muted ? 0 : v.volume);
    });
    v.addEventListener('ended', () => {
      if (!this.item) return;
      if (this.isLive) { this.mediaFailed('The channel stopped.'); return; }
      store.progress.markDone(this.item.id, this.item);
      this.showUpNext();
    });
    v.addEventListener('error', () => {
      if (!this.item || this.hls || !v.getAttribute('src')) return;
      const code = v.error ? v.error.code : 0;
      if (code === 3 || code === 4) this.decodeFailed();
      this.mediaFailed(code === 4 ? 'This format is not supported by this browser.' : 'The stream could not be played.');
    });

    $('p-play').addEventListener('click', () => this.togglePlay());
    $('p-mute').addEventListener('click', () => this.toggleMute());
    $('p-vol').addEventListener('input', (e) => {
      v.volume = Number(e.target.value);
      v.muted = v.volume === 0;
      store.prefs.set('volume', v.volume);
    });
    $('p-fs').addEventListener('click', () => this.toggleFullscreen());
    $('p-cc').addEventListener('click', (e) => { e.stopPropagation(); this.toggleMenu('subs'); });
    $('p-audio').addEventListener('click', (e) => { e.stopPropagation(); this.toggleMenu('audio'); });
    $('p-diag').addEventListener('click', (e) => { e.stopPropagation(); this.showDiag(); });
    $('p-quality').addEventListener('click', (e) => { e.stopPropagation(); this.toggleMenu('quality'); });
    $('p-menu').addEventListener('keydown', (e) => { if (e.key === 'Escape') { this.closeMenu(); e.stopPropagation(); e.preventDefault(); } });
    v.textTracks.addEventListener('addtrack', () => this.syncTextTracks());
    v.addEventListener('loadedmetadata', () => this.syncTextTracks());
    // iOS native fullscreen starts with its own subtitle menu on "Off" unless the track is
    // switched on again once it is up (tested in the iOS Simulator).
    v.addEventListener('webkitbeginfullscreen', () => {
      if (!this.wantSubs()) return;
      for (const t of v.textTracks) if (t.kind === 'subtitles' || t.kind === 'captions') t.mode = 'disabled';
      setTimeout(() => this.syncTextTracks(), 300);
    });
    $('p-pip').addEventListener('click', () => this.togglePip());
    $('p-back10').addEventListener('click', () => this.seekTo(this.absTime() - 10));
    $('p-fwd10').addEventListener('click', () => this.seekTo(this.absTime() + 30));
    $('p-chup').addEventListener('click', () => this.hooks.onChannel(1));
    $('p-chdown').addEventListener('click', () => this.hooks.onChannel(-1));
    $('p-last').addEventListener('click', () => this.hooks.onLastChannel());
    $('p-retry').addEventListener('click', () => this.retry());
    $('p-skip').addEventListener('click', () => this.hooks.onChannel(1));
    $('p-close').addEventListener('click', () => this.close());
    $('p-idle-play').addEventListener('click', () => this.hooks.resumeIdle());
    $('p-upnext-play').addEventListener('click', () => { const n = this.upnext; this.hideUpNext(); if (n) this.open(n); });
    $('p-upnext-cancel').addEventListener('click', () => this.hideUpNext());

    this.$seek.addEventListener('input', () => {
      this.seeking = true;
      const d = this.duration();
      const val = Number(this.$seek.value);
      this.$seek.style.setProperty('--fill', `${val / 10}%`);
      $('p-pos').textContent = fmtTime((val / 1000) * d);
    });
    this.$seek.addEventListener('change', () => {
      const d = this.duration();
      this.seeking = false;
      if (d) this.seekTo((Number(this.$seek.value) / 1000) * d);
    });

    const pipOk = (document.pictureInPictureEnabled && v.requestPictureInPicture) || v.webkitSupportsPresentationMode;
    $('p-pip').hidden = !pipOk;
    if (window.WebKitPlaybackTargetAvailabilityEvent) {
      v.addEventListener('webkitplaybacktargetavailabilitychanged', (e) => { $('p-airplay').hidden = e.availability !== 'available'; });
      $('p-airplay').addEventListener('click', () => v.webkitShowPlaybackTargetPicker());
    }

    // controls: show on any interaction, auto-hide while playing
    this.stage.addEventListener('pointermove', (e) => { if (e.pointerType === 'mouse') this.wakeUi(); });
    this.stage.addEventListener('click', (e) => {
      if (e.target.closest('button, input, .p-error, .p-idle, .p-upnext, .p-menu')) { this.wakeUi(); return; }
      if (this.menu) { this.closeMenu(); return; }
      if (this.stage.classList.contains('needs-tap')) { this.overlay(null); this.tryPlay(); return; }
      if (matchMedia('(hover: none)').matches) {
        if (this.stage.classList.contains('show-ui')) this.stage.classList.remove('show-ui'); else this.wakeUi();
      } else this.togglePlay();
    });
    this.stage.addEventListener('dblclick', (e) => { if (!e.target.closest('button, input')) this.toggleFullscreen(); });
    this.stage.addEventListener('focusin', () => this.wakeUi());

    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible' && this.playing) this.requestWake();
      if (document.visibilityState === 'hidden') this.saveProgress(true);
    });
    window.addEventListener('pagehide', () => {
      this.saveProgress(true);
      if (this.res && this.res.job) this.release(this.res.job);
    });
  }
}
