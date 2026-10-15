// JSON API client + catalogue cache + small DOM helpers.

export const BASE = '/tv/api';
export const clientId = (() => {
  const a = new Uint8Array(6);
  crypto.getRandomValues(a);
  return Array.from(a, (b) => b.toString(16).padStart(2, '0')).join('');
})();

export class ApiError extends Error {
  constructor(message, status, body = null) { super(message); this.status = status; this.body = body; }
}

export async function api(path, { signal, timeout = 45000 } = {}) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(new DOMException('timeout', 'TimeoutError')), timeout);
  if (signal) signal.addEventListener('abort', () => ctl.abort(signal.reason), { once: true });
  let res;
  try {
    res = await fetch(BASE + path, { signal: ctl.signal, credentials: 'same-origin', headers: { Accept: 'application/json' } });
  } catch (e) {
    if (signal && signal.aborted) throw e;
    throw new ApiError(e && e.name === 'TimeoutError' ? 'The server took too long to answer.' : 'Network error - check your connection.', 0);
  } finally {
    clearTimeout(timer);
  }
  if (res.status === 401) throw new ApiError('Your login has expired - reload the page to sign in again.', 401);
  let body = null;
  try { body = await res.json(); } catch { /* empty */ }
  if (!res.ok) throw new ApiError((body && body.error) || `Server error (${res.status})`, res.status, body);
  return body;
}

export const img = (key) => (key ? `${BASE}/img/${key}` : '');

// ---------------------------------------------------------------- catalogue
// Lists are fetched once per session (ETag revalidation makes reloads cheap) and
// searched locally: typing never causes a request.
const lists = {};
const pending = {};

export function fold(s) {
  return s.toLowerCase().normalize('NFKD').replace(/[\u0300-\u036f]/g, '');   // strip accents
}

function shape(kind, raw) {
  const cats = new Map(raw.cats);
  const items = raw.items.map((r) => {
    const base = { id: r[0], name: r[1], cat: r[2], img: r[3], key: fold(r[1]) };
    if (kind === 'live') return { ...base, num: r[4], epg: !!r[5] };
    if (kind === 'movies') return { ...base, year: r[4], rating: r[5], added: r[6] };
    return { ...base, year: r[4], rating: r[5] };
  });
  const byId = new Map(items.map((i) => [i.id, i]));
  const counts = new Map();                 // category -> number of items (also: which categories are used)
  for (const i of items) counts.set(i.cat, (counts.get(i.cat) || 0) + 1);
  return { v: raw.v, cats, items, byId, counts, sorted: {} };
}

// Categories that have items, in the provider's order: [[id, name, count], ...]
export function usedCats(list) {
  return [...list.cats.entries()].filter(([k]) => list.counts.has(k)).map(([k, name]) => [k, name, list.counts.get(k)]);
}

const SORTS = {
  name: (a, b) => a.name.localeCompare(b.name),
  year: (a, b) => (b.year || 0) - (a.year || 0),
  rating: (a, b) => (b.rating || 0) - (a.rating || 0),
  added: (a, b) => (b.added || 0) - (a.added || 0),
};
// The whole list in one sort order, sorted once per session (lists never change in place).
// Filtering this keeps the order, so changing category never re-sorts 180k titles.
export function sortedItems(list, sort) {
  if (!SORTS[sort]) return list.items;
  if (!list.sorted[sort]) list.sorted[sort] = [...list.items].sort(SORTS[sort]);
  return list.sorted[sort];
}

export function catalogue(kind, { force = false } = {}) {
  if (lists[kind] && !force) return Promise.resolve(lists[kind]);
  if (pending[kind]) return pending[kind];
  pending[kind] = api('/' + kind, { timeout: 120000 })
    .then((raw) => (lists[kind] = shape(kind, raw)))
    .finally(() => { delete pending[kind]; });
  return pending[kind];
}
export const cached = (kind) => lists[kind] || null;
export function dropCatalogue() { for (const k of Object.keys(lists)) delete lists[k]; }

// keep(item) -> false leaves an item out (hidden channels / categories)
export function search(list, query, limit = 60, keep = null) {
  const terms = fold(query).split(/\s+/).filter(Boolean);
  if (!terms.length) return [];
  const out = [];
  for (const it of list.items) {
    if (terms.every((t) => it.key.includes(t)) && (!keep || keep(it))) {
      out.push(it);
      if (out.length >= limit * 4) break;
    }
  }
  const q = terms.join(' ');
  out.sort((a, b) => (b.key.startsWith(q) - a.key.startsWith(q)) || a.name.length - b.name.length);
  return out.slice(0, limit);
}

// ---------------------------------------------------------------- EPG cache
let guide = { t: 0, data: {} };
let guideAt = 0;
export async function epgNow(force = false) {
  if (!force && Date.now() - guideAt < 60000) return guide.data;
  try {
    const g = await api('/epg/now');
    guide = { t: g.t, data: g.guide };
    guideAt = Date.now();
  } catch { /* keep the old one */ }
  return guide.data;
}

// ---------------------------------------------------------------- guide search
// Programmes on now / in the next 24 h matching q (tvapp/epgsearch.py). The page asks after a
// pause in typing; answers are kept for a minute (what's "on now" moves on).
const guideHits = new Map();
export async function searchGuide(q, caps, signal) {
  const key = `${fold(q).trim()}|${caps}`;
  const hit = guideHits.get(key);
  if (hit && Date.now() - hit.at < 55000) return hit.d;
  const d = await api(`/live/search?q=${encodeURIComponent(q)}&caps=${encodeURIComponent(caps)}`, { signal, timeout: 20000 });
  if (!d.pending) {
    guideHits.delete(key);
    guideHits.set(key, { at: Date.now(), d });
    if (guideHits.size > 30) guideHits.delete(guideHits.keys().next().value);
  }
  return d;
}

// ---------------------------------------------------------------- DOM helpers
export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'text') el.textContent = v;
      else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
      else if (k === 'dataset') Object.assign(el.dataset, v);
      else el.setAttribute(k, v === true ? '' : v);
    }
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

const SVGNS = 'http://www.w3.org/2000/svg';
export function icon(name, cls = 'ico') {
  const s = document.createElementNS(SVGNS, 'svg');
  s.setAttribute('class', cls);
  s.setAttribute('aria-hidden', 'true');
  const u = document.createElementNS(SVGNS, 'use');
  u.setAttribute('href', `/tv/static/icons.svg#${name}`);
  s.append(u);
  return s;
}

// Swap the whole <svg>: Chrome does not repaint an external <use> whose href changes.
export function setIcon(button, name) {
  const old = button.querySelector('svg');
  if (old && old.dataset.icon === name) return;
  const fresh = icon(name);
  fresh.dataset.icon = name;
  if (old) old.replaceWith(fresh); else button.prepend(fresh);
}

export function fmtTime(sec) {
  if (!isFinite(sec) || sec < 0) sec = 0;
  sec = Math.floor(sec);
  const hh = Math.floor(sec / 3600), mm = Math.floor((sec % 3600) / 60), ss = sec % 60;
  return hh ? `${hh}:${String(mm).padStart(2, '0')}:${String(ss).padStart(2, '0')}` : `${mm}:${String(ss).padStart(2, '0')}`;
}

export function fmtClock(ts) {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

export function fmtDuration(sec) {
  if (!sec) return '';
  const m = Math.round(sec / 60);
  return m >= 60 ? `${Math.floor(m / 60)}h ${m % 60}m` : `${m}m`;
}

let toastTimer = 0;
export function toast(message, ms = 3200) {
  const t = document.getElementById('toast');
  t.textContent = message;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, ms);
}

// Poster/logo with graceful fallback to initials.
export function thumb(key, name, cls) {
  const wrap = h('span', { class: `thumb ${cls || ''}` });
  const initials = h('span', { class: 'thumb-fallback', text: (name || '?').replace(/[^\p{L}\p{N} ]/gu, '').trim().slice(0, 2).toUpperCase() || '·' });
  wrap.append(initials);
  if (key) {
    const im = h('img', { src: img(key), alt: '', loading: 'lazy', decoding: 'async' });
    im.addEventListener('load', () => wrap.classList.add('loaded'), { once: true });
    im.addEventListener('error', () => im.remove(), { once: true });
    wrap.append(im);
  }
  return wrap;
}

// Renders a long list in chunks as the user scrolls (thousands of channels/posters).
export function chunked(container, items, render, { chunk = 120, root = null } = {}) {
  container.replaceChildren();
  let i = 0;
  const sentinel = h('div', { class: 'sentinel', 'aria-hidden': 'true' });
  const more = () => {
    const frag = document.createDocumentFragment();
    for (const end = Math.min(items.length, i + chunk); i < end; i++) frag.append(render(items[i], i));
    container.append(frag);
    if (i < items.length) container.append(sentinel);
    else sentinel.remove();
  };
  more();
  const io = new IntersectionObserver((entries) => {
    if (entries.some((e) => e.isIntersecting)) { sentinel.remove(); more(); }
  }, { root, rootMargin: '800px' });
  io.observe(sentinel);
  return () => io.disconnect();
}
