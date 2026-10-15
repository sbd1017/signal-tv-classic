// App state: favourites, watch progress, recently played, last channel, preferences,
// hidden channels and categories.
//
// The Pi is the source of truth (tvapp/userstate.py, SQLite). This module keeps a
// local copy in localStorage so reads are instant and work offline, queues local
// changes, and syncs both ways:
//   push - changed rows ~2 s after a change, and straight away when the page is hidden/closed
//   pull - on start, when the page becomes visible again, and every 30 s while visible
// Every row carries the time it last changed (on the Pi's clock); per row the newer
// change wins, deletions included, so stale data on one device never overwrites newer
// data from another. Volume, mute and the last search stay on this device only.

const BASE = '/tv/api';
const LS = 'tv.v2.';
const V1 = 'tv.v1.';                      // pre-sync, browser-only storage (migrated once, then left alone)
const NS = ['fav', 'progress', 'recent', 'last', 'pref', 'hide', 'sfav'];
const LOCAL_PREFS = new Set(['muted', 'volume', 'lastSearch']);
const MAX_RECENT = 40;                    // shown
const KEEP = { progress: 2000, recent: 100 };   // kept (same caps as the Pi)
const TOMBSTONE_MS = 180 * 86400000;
const PUSH_DELAY = 2000;
const PULL_EVERY = 30000;
const BATCH = 200;                        // changes per request
const KEEPALIVE_BATCH = 100;              // keepalive bodies are limited to 64 KB
export const DONE_AT = 0.95;

const storage = (() => { try { return globalThis.localStorage || null; } catch { return null; } })();
const ls = {
  get(key, fallback) {
    try { const raw = storage.getItem(key); return raw == null ? fallback : JSON.parse(raw); } catch { return fallback; }
  },
  set(key, value) {
    try { storage.setItem(key, JSON.stringify(value)); } catch { /* private mode / quota: the in-memory copy still works */ }
  },
};

// ------------------------------------------------------------------ local copy
const rows = {};          // ns -> {key: {v: value, u: updated ms, d: 0|1 deleted}}
const views = {};         // ns -> cached {key: value (+ at)} without deletions
let pending = {};         // 'ns|key' -> 1 for rows changed here and not yet accepted by the Pi
let meta = {};            // {db, rev, device}
let skew = 0;             // Pi clock minus this device's clock (ms)
let migrated = false;     // old browser-only data confirmed on the Pi
export const syncStatus = { ok: null, at: 0, error: '', pending: 0 };
const dirty = new Set();
let saveTimer = 0;
const listeners = new Set();

const pk = (ns, key) => `${ns}|${key}`;
const splitPk = (k) => { const i = k.indexOf('|'); return [k.slice(0, i), k.slice(i + 1)]; };
const clock = () => Date.now() + skew;

function randomHex(bytes) {
  const a = new Uint8Array(bytes);
  globalThis.crypto.getRandomValues(a);
  return Array.from(a, (b) => b.toString(16).padStart(2, '0')).join('');
}

function persist() {
  clearTimeout(saveTimer);
  saveTimer = 0;
  for (const ns of dirty) ls.set(LS + 'rows.' + ns, rows[ns]);
  dirty.clear();
  ls.set(LS + 'pending', pending);
  ls.set(LS + 'meta', meta);
}

function touched(ns) {
  delete views[ns];
  dirty.add(ns);
  if (!saveTimer) saveTimer = setTimeout(persist, 400);
}

function view(ns) {
  if (!views[ns]) {
    const out = {};
    for (const [k, r] of Object.entries(rows[ns])) if (!r.d) out[k] = { ...r.v, at: r.u };
    views[ns] = out;
  }
  return views[ns];
}

function put(ns, key, value, deleted = false) {
  const cur = rows[ns][key];
  let at = clock();
  if (cur && cur.u >= at) at = cur.u + 1;     // always newer than what this device has seen for the row
  rows[ns][key] = deleted ? { v: null, u: at, d: 1 } : { v: value, u: at, d: 0 };
  pending[pk(ns, key)] = 1;
  touched(ns);
  if (!batching) persist();                 // saved locally at once: a change is never lost before the Pi has it
  schedulePush();
}

// Many puts (bulk hide, clear recent): save once at the end instead of after each one.
let batching = false;
function batch(fn) {
  batching = true;
  try { return fn(); } finally { batching = false; persist(); }
}

// Drop the oldest rows beyond the cap on this device only (the Pi applies the same cap).
function trim(ns) {
  const live = Object.entries(rows[ns]).filter(([, r]) => !r.d);
  if (live.length <= KEEP[ns]) return;
  live.sort((a, b) => a[1].u - b[1].u).slice(0, live.length - KEEP[ns]).forEach(([k]) => {
    delete rows[ns][k];
    delete pending[pk(ns, k)];
  });
  touched(ns);
}

function seed(ns, key, value, at) {        // a legacy row: queued for the Pi as it was
  if (!key || (ns !== 'pref' && (!value || typeof value !== 'object'))) return;
  if (rows[ns][key]) return;                // already here (migrated earlier, or changed since): keep that
  rows[ns][key] = { v: value, u: Math.max(1, Math.floor(at || 1)), d: 0 };
  pending[pk(ns, key)] = 1;
  dirty.add(ns);
}

// Browser-only data from before sync: queued with its original times, so it merges
// with what the Pi already has (newer wins). Rows without a time (prefs, previous
// channel) get time 1 and so never override anything already synced. The old tv.v1.*
// keys are never changed or removed, and the migration only counts as done once the Pi
// has accepted everything (see sync()); until then it runs again on each load, adding
// only rows this browser does not have yet.
function migrateV1() {
  const strip = (v) => { const { at, ...rest } = v || {}; return [rest, at]; };
  for (const [id, v] of Object.entries(ls.get(V1 + 'favs', {}) || {})) seed('fav', id, ...strip(v));
  for (const [id, v] of Object.entries(ls.get(V1 + 'progress', {}) || {})) seed('progress', id, ...strip(v));
  const rec = ls.get(V1 + 'recent', []) || [];
  for (const e of rec) { const { id, ...rest } = e || {}; seed('recent', id, ...strip(rest)); }
  const last = ls.get(V1 + 'lastChannel', null);
  if (last && last.id) {
    const seen = rec.filter((e) => e && e.id === last.id).map((e) => e.at || 1);
    seed('last', 'channel', last, Math.max(1, ...seen));
  }
  const prev = ls.get(V1 + 'prevChannel', null);
  if (prev && prev.id) seed('last', 'previous', prev, 1);
  try {
    for (let i = 0; i < storage.length; i++) {
      const k = storage.key(i);
      if (k && k.startsWith(V1 + 'pref.') && !LOCAL_PREFS.has(k.slice(V1.length + 5))) seed('pref', k.slice(V1.length + 5), ls.get(k, null), 1);
    }
  } catch { /* no storage */ }
}

function load() {
  let missing = false;
  for (const ns of NS) {
    const saved = ls.get(LS + 'rows.' + ns, null);
    if (!saved || typeof saved !== 'object') missing = true;
    rows[ns] = saved && typeof saved === 'object' ? saved : {};
    dirty.add(ns);                          // write every namespace back, so "missing" means lost
  }
  pending = ls.get(LS + 'pending', {}) || {};
  meta = { db: '', rev: 0, device: '', ...(ls.get(LS + 'meta', {}) || {}) };
  if (!meta.device) meta.device = randomHex(8);
  // Part of the local copy is gone (cleared, corrupted, or a kind of data added by an
  // update): fetch everything again instead of only what changed since the last sync.
  if (missing) meta.rev = 0;
  migrated = !!ls.get(LS + 'migrated', false);
  if (!migrated) migrateV1();
  syncStatus.pending = Object.keys(pending).length;
  const old = Date.now() - TOMBSTONE_MS;
  for (const ns of NS) {
    for (const [k, r] of Object.entries(rows[ns])) if (r.d && r.u < old && !pending[pk(ns, k)]) { delete rows[ns][k]; dirty.add(ns); }
  }
  persist();
}
load();

// ------------------------------------------------------------------ public API (same shape as before sync)
// meta: {name, img, kind, series?, season?, episode?, seriesName?, num?}
function slim(m = {}) {
  const out = {};
  for (const k of ['name', 'img', 'kind', 'series', 'season', 'episode', 'seriesName', 'num']) {
    if (m[k] !== undefined && m[k] !== '') out[k] = m[k];
  }
  return out;
}

export const favs = {
  all() { return view('fav'); },
  has(id) { return !!view('fav')[id]; },
  toggle(id, m) {
    const on = !this.has(id);
    put('fav', id, on ? slim(m) : null, !on);
    return on;
  },
  list(kind) {
    return Object.entries(this.all())
      .filter(([, v]) => !kind || v.kind === kind)
      .sort((a, b) => b[1].at - a[1].at)
      .map(([id, v]) => ({ id, ...v }));
  },
};

// Favourite sports teams and competitions (Sports > For You, match alerts on the Pi). Keyed by
// the stable id from tvapp/sports.py ("team:arsenal", "comp:premier-league"), never by a name
// as some guide wrote it; the value only keeps the name and kind for the list.
export const sportsFavs = {
  all() { return view('sfav'); },
  ids() { return Object.keys(view('sfav')); },
  has(id) { return !!view('sfav')[id]; },
  toggle(id, m = {}) {
    const on = !this.has(id);
    const value = {};
    for (const k of ['name', 'kind', 'sport']) if (m[k]) value[k] = String(m[k]).slice(0, 80);
    put('sfav', id, on ? value : null, !on);
    return on;
  },
  list() {
    return Object.entries(this.all()).map(([id, v]) => ({ id, ...v })).sort((a, b) => (a.name || a.id).localeCompare(b.name || b.id));
  },
};

export const progress = {
  all() { return view('progress'); },
  get(id) { return this.all()[id] || null; },
  set(id, t, d, m) {
    if (!d || !isFinite(d) || !isFinite(t)) return;
    const r = rows.progress[id];
    const done = t / d >= DONE_AT;
    put('progress', id, { ...(r && !r.d ? r.v : {}), ...slim(m), t: done ? d : Math.max(0, t), d, done });
    trim('progress');
  },
  markDone(id, m) {
    const p = this.get(id);
    const d = (p && p.d) || 1;
    this.set(id, d, d, m);
  },
  clear(id) { if (this.get(id)) put('progress', id, null, true); },
  fraction(id) { const p = this.get(id); return p && p.d ? Math.min(1, p.t / p.d) : 0; },
  inProgress() {
    return Object.entries(this.all())
      .filter(([, v]) => !v.done && v.t > 15)
      .sort((a, b) => b[1].at - a[1].at)
      .map(([id, v]) => ({ id, ...v }));
  },
  // series id -> most recently watched episode entry (for "next up")
  lastBySeries() {
    const out = {};
    for (const [id, v] of Object.entries(this.all())) {
      if (v.series && (!out[v.series] || out[v.series].at < v.at)) out[v.series] = { id, ...v };
    }
    return out;
  },
};

export const recent = {
  list() {
    return Object.entries(view('recent')).map(([id, v]) => ({ id, ...v }))
      .sort((a, b) => b.at - a.at).slice(0, MAX_RECENT);
  },
  add(id, m) { put('recent', id, slim(m)); trim('recent'); },
  clear() { batch(() => { for (const id of Object.keys(view('recent'))) put('recent', id, null, true); }); },
};

export const prefs = {
  get(key, fallback) {
    if (LOCAL_PREFS.has(key)) return ls.get(LS + 'local.' + key, ls.get(V1 + 'pref.' + key, fallback));
    const r = rows.pref[key];
    return r && !r.d ? r.v : fallback;
  },
  set(key, value) {
    if (LOCAL_PREFS.has(key)) { ls.set(LS + 'local.' + key, value); return; }
    const r = rows.pref[key];
    if (r && !r.d && r.v === value) return;        // unchanged: nothing to sync
    put('pref', key, value);
  },
};

// Hidden categories (per kind: 'live' | 'movie' | 'series') and hidden live channels.
// Row keys: 'c:<kind>:<category id>' (characters outside the key alphabet escaped) and
// the channel's own id; the value keeps the raw id and name for the "hidden" lists.
const catKey = (kind, id) => `c:${kind}:${String(id).replace(/[^A-Za-z0-9_.-]/g, (c) => `:${c.codePointAt(0).toString(16)}`)}`.slice(0, 80);
let hiddenSets = { from: null, cats: {}, channels: new Set() };
function hiddenNow() {
  const v = view('hide');
  if (hiddenSets.from !== v) {
    const cats = {};
    const channels = new Set();
    for (const [k, val] of Object.entries(v)) {
      if (k.startsWith('c:')) (cats[val.kind] ||= new Set()).add(val.cat);
      else channels.add(k);
    }
    hiddenSets = { from: v, cats, channels };
  }
  return hiddenSets;
}

export const hidden = {
  cats(kind) { return hiddenNow().cats[kind] || new Set(); },
  channels() { return hiddenNow().channels; },
  // cats: [[id, name], ...]; returns how many changed
  setCats(kind, cats, on) {
    return batch(() => {
      let n = 0;
      for (const [id, name] of cats) {
        if (this.cats(kind).has(id) === on) continue;
        put('hide', catKey(kind, id), on ? { kind, cat: id, name } : null, !on);
        n++;
      }
      return n;
    });
  },
  setChannel(id, m, on) {
    if (this.channels().has(id) === on) return;
    put('hide', id, on ? { ...slim(m), kind: 'live' } : null, !on);
  },
  channelList() {
    return Object.entries(view('hide')).filter(([k]) => !k.startsWith('c:'))
      .map(([id, v]) => ({ id, ...v })).sort((a, b) => String(a.name).localeCompare(String(b.name)));
  },
};

export const lastChannel = {
  get() { const r = rows.last.channel; return r && !r.d ? r.v : null; },
  set(id, m) {
    const cur = this.get();
    if (cur && cur.id !== id) put('last', 'previous', cur);
    put('last', 'channel', { id, ...slim(m) });
  },
  previous() { const r = rows.last.previous; return r && !r.d ? r.v : null; },
};

// ------------------------------------------------------------------ sync with the Pi
let inflight = null;
let again = false;
let pushTimer = 0;
let started = false;

// Called with a Set of namespaces whenever rows changed because of another device.
export function onChange(fn) { listeners.add(fn); return () => listeners.delete(fn); }

async function call(method, path, body, keepalive = false) {
  const t0 = Date.now();
  const headers = { Accept: 'application/json' };
  if (body !== undefined) Object.assign(headers, { 'Content-Type': 'application/json', 'X-TV-Sync': '1' });
  const res = await fetch(BASE + path, { method, headers, keepalive, credentials: 'same-origin',
    body: body === undefined ? undefined : (typeof body === 'string' ? body : JSON.stringify(body)) });
  let data = null;
  try { data = await res.json(); } catch { /* empty */ }
  if (!res.ok) throw new Error((data && data.error) || (res.status === 401 ? 'Login expired - reload the page.' : `HTTP ${res.status}`));
  const rtt = Date.now() - t0;
  if (data && data.now && rtt < 4000) skew = data.now - (t0 + rtt / 2);
  return data;
}

function changesFor(keys) {
  const out = [];
  for (const k of keys) {
    const [ns, key] = splitPk(k);
    const r = rows[ns] && rows[ns][key];
    if (!r) { delete pending[k]; continue; }
    out.push({ ns, key, value: r.d ? null : r.v, updated: r.u, deleted: !!r.d });
  }
  return out;
}

function merge(changes) {
  const hit = new Set();
  for (const c of changes) {
    if (!rows[c.ns]) continue;
    const k = pk(c.ns, c.key);
    const cur = rows[c.ns][c.key];
    if (pending[k] && cur && cur.u > c.updated) continue;   // our newer change is still on its way to the Pi
    const next = c.deleted ? { v: null, u: c.updated, d: 1 } : { v: c.value, u: c.updated, d: 0 };
    delete pending[k];
    if (cur && cur.u === next.u && cur.d === next.d && JSON.stringify(cur.v) === JSON.stringify(next.v)) continue;
    rows[c.ns][c.key] = next;
    touched(c.ns);
    hit.add(c.ns);
  }
  return hit;
}

// The Pi's database is not the one this device last synced with: recreated (new id) or
// restored from an older copy (same id, but its revision went backwards). Send it
// everything held here before taking anything from it; newer-wins merges it back in,
// so a restore can never overwrite newer data on a device.
function resend(db) {
  meta.db = db;
  meta.rev = 0;
  for (const ns of NS) for (const key of Object.keys(rows[ns])) pending[pk(ns, key)] = 1;
}

export function sync() {
  if (inflight) { again = true; return inflight; }
  inflight = (async () => {
    const hit = new Set();
    try {
      for (let round = 0; round < 25; round++) {
        again = false;
        const sent = changesFor(Object.keys(pending).slice(0, BATCH));
        const res = sent.length
          ? await call('POST', '/state', { since: meta.rev, device: meta.device, changes: sent })
          : await call('GET', `/state?since=${meta.rev}`);
        if (meta.db && (res.db !== meta.db || res.rev < meta.rev)) { resend(res.db); continue; }
        meta.db = res.db;
        for (const c of sent) {             // accepted (or superseded, and the newer row is in res.changes)
          const r = rows[c.ns][c.key];
          if (r && r.u === c.updated) delete pending[pk(c.ns, c.key)];
        }
        for (const ns of merge(res.changes)) hit.add(ns);
        meta.rev = res.rev;
        if (!again && !Object.keys(pending).length) break;
      }
      if (!migrated && !Object.keys(pending).length) {   // everything (incl. old data) accepted by the Pi
        migrated = true;
        ls.set(LS + 'migrated', true);
      }
      Object.assign(syncStatus, { ok: true, at: Date.now(), error: '' });
    } catch (e) {
      Object.assign(syncStatus, { ok: false, error: e.message || 'offline' });
    } finally {
      syncStatus.pending = Object.keys(pending).length;
      persist();
      inflight = null;
    }
    if (hit.size) for (const fn of listeners) { try { fn(hit); } catch { /* a view's problem, not sync's */ } }
    return syncStatus;
  })();
  return inflight;
}

function schedulePush() {
  syncStatus.pending = Object.keys(pending).length;
  if (!started) return;
  clearTimeout(pushTimer);
  pushTimer = setTimeout(sync, PUSH_DELAY);
}

// Page going away: save locally and send what is queued without waiting for an answer.
// Rows stay queued until a normal sync confirms them, so nothing is lost if this fails.
function flushNow() {
  clearTimeout(pushTimer);
  persist();
  const sent = changesFor(Object.keys(pending).slice(0, KEEPALIVE_BATCH));
  if (sent.length) call('POST', '/state', { since: meta.rev, device: meta.device, changes: sent }, true).catch(() => {});
}

export function startSync() {
  if (started) return syncStatus;
  started = true;
  sync();
  setInterval(() => { if (document.visibilityState === 'visible') sync(); }, PULL_EVERY);
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') sync(); else flushNow(); });
  window.addEventListener('pagehide', flushNow);
  window.addEventListener('pageshow', (e) => { if (e.persisted) sync(); });
  window.addEventListener('online', () => sync());
  return syncStatus;
}

// ------------------------------------------------------------------ backup file
export const EXPORT_URL = `${BASE}/state/export`;

// mode: 'merge' (newer wins per item) or 'replace' (the file becomes everything saved)
export async function importState(text, mode = 'merge') {
  try { JSON.parse(text); } catch { throw new Error('That file is not a Signal TV Classic backup (not JSON).'); }
  await sync();                                   // so local changes are on the Pi before it merges/replaces
  const result = await call('POST', `/state/import?mode=${mode === 'replace' ? 'replace' : 'merge'}`, text);
  await sync();
  return result;
}
