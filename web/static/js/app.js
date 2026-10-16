// Signal TV Classic - app shell, router and views. No framework; all provider text is
// inserted with textContent (never innerHTML).
import { api, catalogue, cached, dropCatalogue, epgNow, fold, search, searchGuide, sortedItems, usedCats, h, icon, setIcon, thumb,
  chunked, toast, fmtClock, fmtDuration, fmtTime, img } from './api.js?v=20261016190000';
import * as caps from './caps.js?v=20261016190000';
import * as store from './store.js?v=20261016190000';
import { Player } from './player.js?v=20261016190000';
import * as tracks from './tracks.js?v=20261016190000';
import * as lang from './lang.js?v=20261016190000';
import * as names from './names.js?v=20261016190000';

const $ = (id) => document.getElementById(id);
const view = $('view');
const main = $('main');
const state = { section: '', cleanup: [], liveList: [], liveFilter: null, gridQuery: {}, seriesCache: new Map(), status: null, redraw: null };
const STORE_KIND = { live: 'live', movies: 'movie', series: 'series' };     // view name -> kind used for hidden categories

// ------------------------------------------------------------------ player
const player = new Player({
  onChannel: (d) => stepChannel(d),
  onLastChannel: () => lastChannel(),
  resumeIdle: () => { const c = player.idleChannel; if (c) playChannel(c); },
  nextEpisode: (item) => nextEpisode(item),
  onStarted: (item) => {
    if (item.kind === 'live') { markActiveChannel(item.id); renderLiveInfo(item); }
  },
  onClosed: (item) => {
    if (item && item.kind === 'live') { main.classList.remove('live-playing'); maybeIdle(); }
    if (state.section === 'library' || state.section === 'continue') render(true);
    refreshProgressBadges();
  },
});

function playChannel(ch) {
  const item = { id: ch.id, kind: 'live', name: ch.name, img: ch.img, num: ch.num, sub: '' };
  const nn = guideData[ch.id];
  if (nn && nn.now) item.sub = nn.now[2];
  main.classList.add('live-playing');
  player.open(item);
  if (state.section !== 'live') location.hash = '#/live';
}

function playVod(item, resume) {
  player.open(item, { resume: resume || 0 });
}

function stepChannel(delta) {
  const cur = player.item && player.item.kind === 'live' ? player.item.id : (player.idleChannel && player.idleChannel.id);
  let list = state.liveList.length ? state.liveList : ((cached('live') || {}).items || []);
  if (!list.length) return;
  let i = list.findIndex((c) => c.id === cur);
  if (i < 0) { list = (cached('live') || {}).items || list; i = list.findIndex((c) => c.id === cur); }
  const next = list[(i + delta + list.length) % list.length];
  if (next) { playChannel(next); player.osd(`${next.num ? next.num + '  ' : ''}${next.name}`); }
}

function lastChannel() {
  const prev = player.prevLive || store.lastChannel.previous();
  if (!prev) { toast('No previous channel yet.'); return; }
  const ch = ((cached('live') || {}).byId || new Map()).get(prev.id) || prev;
  playChannel(ch);
  player.osd(ch.name);
}

// ------------------------------------------------------------------ router
const SECTIONS = ['live', 'sports', 'movies', 'series', 'search', 'library', 'continue', 'favourites', 'recent', 'settings'];

function parseHash() {
  const [sec, id] = location.hash.replace(/^#\/?/, '').split('/');
  return { sec: SECTIONS.includes(sec) ? sec : 'live', id: id || '' };
}

function route() {
  const { sec, id } = parseHash();
  if (sec !== state.section) {
    if (state.section === 'live' && sec !== 'live' && player.isLive && !document.pictureInPictureElement) {
      player.stop();
      player.showMode('off');
      main.classList.remove('live-playing');
    } else if (state.section === 'live' && sec !== 'live' && !player.item) {
      player.showMode('off');                       // the "Last watched / Resume" card belongs to Live TV only
      main.classList.remove('live-playing');
    }
    state.section = sec;
    render();
  }
  for (const a of document.querySelectorAll('[data-nav]')) {
    const on = a.dataset.nav === sec || (a.dataset.nav === 'library' && ['continue', 'favourites', 'recent', 'settings'].includes(sec));
    if (on) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  }
  if (id && (sec === 'movies' || sec === 'series')) openDetails(sec, id);
  else closeSheet(false);
}

function render(keepScroll = false) {
  for (const fn of state.cleanup.splice(0)) { try { fn(); } catch { /* ignore */ } }
  main.classList.toggle('live-mode', state.section === 'live');
  if (!keepScroll) window.scrollTo(0, 0);
  const fn = { live: viewLive, sports: viewSports, movies: () => viewGrid('movies'), series: () => viewGrid('series'), search: viewSearch,
    library: viewLibrary, continue: viewLibrary, favourites: viewLibrary, recent: viewLibrary, settings: viewSettings }[state.section];
  fn();
  document.title = { live: 'Live TV', sports: 'Sports', movies: 'Movies', series: 'Series', search: 'Search', settings: 'Settings' }[state.section] || 'Library';
  document.title += ' · TV';
}

// ------------------------------------------------------------------ shared bits
function skeletonRows(n, cls = 'sk-row') { return h('div', { class: 'skeleton', 'aria-hidden': 'true' }, Array.from({ length: n }, () => h('div', { class: cls }))); }
function skeletonGrid(n) { return h('div', { class: 'grid skeleton', 'aria-hidden': 'true' }, Array.from({ length: n }, () => h('div', { class: 'sk-card' }))); }

function errorBox(message, retry) {
  return h('div', { class: 'state-box error-box', role: 'alert' },
    h('p', { class: 'state-title', text: 'Something went wrong' }),
    h('p', { text: message }),
    retry ? h('button', { class: 'btn primary', type: 'button', onclick: retry, text: 'Retry' }) : null);
}

function emptyBox(title, text, extra) {
  return h('div', { class: 'state-box' }, h('p', { class: 'state-title', text: title }), text ? h('p', { text }) : null, extra || null);
}

function providerHint() {
  const st = state.status;
  if (!st) return null;
  if (!st.configured) return h('p', { class: 'muted small', text: 'No provider is configured yet. On the Pi run: iptv set-playlist (or iptv set-xtream).' });
  if (st.error) return h('p', { class: 'muted small', text: `Last refresh problem: ${st.error}` });
  return null;
}

function paintFav(b) {
  const on = store.favs.has(b.dataset.favId);
  b.classList.toggle('on', on);
  b.setAttribute('aria-pressed', on ? 'true' : 'false');
  b.setAttribute('aria-label', on ? `Remove ${b.dataset.favName} from favourites` : `Add ${b.dataset.favName} to favourites`);
}

function favButton(id, meta, onChange) {
  const b = h('button', { class: 'fav-btn', type: 'button', dataset: { favId: id, favName: meta.name || '' } }, icon('star'));
  b.addEventListener('click', (e) => {
    e.stopPropagation();
    const on = store.favs.toggle(id, meta);
    paintFav(b);
    toast(on ? 'Added to favourites' : 'Removed from favourites', 1600);
    if (onChange) onChange(on);
  });
  paintFav(b);
  return b;
}

function progressBar(frac, cls = '') {
  const bar = h('span', { class: `pbar ${cls}` }, h('span'));
  bar.firstChild.style.width = `${Math.round(Math.max(0, Math.min(1, frac)) * 100)}%`;
  return bar;
}

// One channel showing a programme: logo, name and the quality this device gets (search results,
// Sports cards). c: {id, label, est, ok, ch} (tvapp/epgsearch.py channel_groups).
function feedButton(c) {
  const ch = c.ch;
  const short = (c.label || '').split(' · ')[0];
  const tip = c.est ? 'Estimated from the channel name; the exact format is learned when it is played'
    : c.conv ? `Converted from ${c.conv} to ${c.label} on the Pi: this device can't decode the original`
    : c.ok === 2 ? `Best version for this device: ${c.label}` : c.ok === 1 ? `${c.label}: may not play smoothly here`
      : c.ok === 0 ? `${c.label}: not smooth on this device; another version will be offered` : '';
  const b = h('button', { class: 'prog-ch', type: 'button', title: tip, 'aria-label': `Play ${ch.name}${short ? `, ${short}` : ''}` },
    thumb(ch.img, ch.name, 'logo sm'), h('span', { class: 'prog-ch-name', text: ch.name }), qualityBadge(c));
  b.addEventListener('click', () => playChannel(ch));
  return b;
}

function qualityBadge(c) {
  const short = (c.label || '').split(' · ')[0];
  if (!short) return '';
  return h('span', { class: `q-badge ${c.est ? 'est' : ''} ${c.ok === 2 ? 'ok' : c.conv ? '' : c.ok >= 0 ? 'bad' : ''}`, text: c.est ? `~${short}` : short });
}

// Channels the viewer hasn't hidden (channels or their categories), as in Live's "All" list.
function visibleFeeds(channels, list) {
  const hch = store.hidden.channels();
  const hc = store.hidden.cats('live');
  const out = [];
  for (const c of channels) {
    const ids = c.ids.filter((i) => { const it = list.byId.get(i); return it && !hch.has(i) && !hc.has(it.cat); });
    const ch = list.byId.get(ids.includes(c.id) ? c.id : ids[0]);
    if (ch) out.push({ ...c, ch });
  }
  return out;
}

// "in 25 min", "19:30", "Tue 01:00"
function whenText(ts, now) {
  const mins = Math.round((ts - now) / 60);
  if (mins <= 60) return `in ${Math.max(1, mins)} min`;
  const d = new Date(ts * 1000);
  const sameDay = d.toDateString() === new Date(now * 1000).toDateString();
  return sameDay ? fmtClock(ts) : `${d.toLocaleDateString([], { weekday: 'short' })} ${fmtClock(ts)}`;
}

function selectEl(label, options, value, onchange) {
  const sel = h('select', { class: 'select', 'aria-label': label });
  for (const [v, t] of options) sel.append(h('option', { value: v, text: t }));
  sel.value = value;
  sel.addEventListener('change', () => onchange(sel.value));
  return sel;
}

function viewHead(title, ...extra) {
  return h('header', { class: 'view-head' }, h('h1', { text: title }), h('div', { class: 'view-head-extra' }, ...extra));
}

// ------------------------------------------------------------------ live or replay
// Many channels re-air a match "live" days later with nothing in the guide to say so. The Pi's
// sidecar (the new interface's tvnext/fixtures.py) knows the real fixture dates: asked for live
// football, basketball and NFL games with two known teams. Without the sidecar the guide's own
// reading stands.
const fixtureIdx = new Map();
const FX_SPORTS = new Set(['football', 'basketball', 'nfl']);
const fxKey = (ev) => (FX_SPORTS.has(ev.sport) && (ev.teams || []).length === 2 && ev.teams.every((t) => String(t).startsWith('team:'))
  ? `${ev.teams[0]}|${ev.teams[1]}|${ev.start}` : '');
async function checkFixtures(evs) {
  const want = [...new Set(evs.filter((e) => e.when === 'live' && !e.replay).map(fxKey))].filter((k) => k && !fixtureIdx.has(k));
  let found = false;
  let pending = 0;
  for (let i = 0; i < want.length; i += 80) {
    try {
      const res = await fetch(`/tv/next/api/fixtures?${new URLSearchParams({ q: want.slice(i, i + 80).join(',') })}`, { credentials: 'same-origin' });
      if (!res.ok) break;
      const d = await res.json();
      for (const [k, v] of Object.entries(d.items || {})) { fixtureIdx.set(k, v); found = found || !!v.replay; }
      pending += d.pending || 0;
    } catch { break; }
  }
  return { found, pending };
}
// ...and from the real kick-off on ESPN's scoreboards (the sidecar's tvnext/scores.py): an airing that
// starts long after it is a replay. Only that verdict is used here; Classic shows no scores.
const kickoffIdx = new Map();
const koKey = (ev) => {
  if (!FX_SPORTS.has(ev.sport) || (ev.kind && ev.kind !== 'match')) return '';
  const ids = (ev.teams || []).filter((t) => String(t).startsWith('team:'));
  const pair = ids.length === 2 ? ids : (names.teams(ev.title) || []).map((l) => `name:${String(l).replace(/[|,]/g, ' ').trim().slice(0, 60)}`);
  return pair.length === 2 && pair.every((p) => p.length > 5) ? `${ev.comp_id || 'comp:none'}|${pair[0]}|${pair[1]}|${ev.start}` : '';
};
async function checkKickoffs(evs) {
  const want = [...new Set(evs.filter((e) => e.when === 'live' && !e.replay && !withFixture(e).replay).map(koKey))]
    .filter((k) => k && !kickoffIdx.has(k)).slice(0, 40);
  if (!want.length) return false;
  let found = false;
  try {
    const res = await fetch(`/tv/next/api/scores?${new URLSearchParams({ q: want.join(',') })}`, { credentials: 'same-origin' });
    const d = res.ok ? await res.json() : { items: {} };
    for (const k of want) {
      const v = (d.items || {})[k];
      kickoffIdx.set(k, !!(v && v.replay));
      found = found || !!(v && v.replay);
    }
  } catch { /* no sidecar */ }
  return found;
}
const withFixture = (ev) => {
  const v = fixtureIdx.get(fxKey(ev));
  return (v && v.replay) || kickoffIdx.get(koKey(ev)) ? { ...ev, replay: true } : ev;
};

// ------------------------------------------------------------------ LIVE
let guideData = {};

async function viewLive() {
  const head = viewHead('Live TV');
  const body = h('div', { class: 'live-body' }, skeletonRows(10));
  view.replaceChildren(h('div', { class: 'live-view' }, head, body));
  maybeIdle();
  let list;
  try {
    [list, guideData] = await Promise.all([catalogue('live'), epgNow()]);
  } catch (e) {
    body.replaceChildren(errorBox(e.message, () => render()));
    return;
  }
  if (state.section !== 'live') return;
  if (!player.item) maybeIdle();          // redraw the "last watched" card now the guide has loaded
  if (!list.items.length) {
    body.replaceChildren(emptyBox('No channels', 'The playlist has no live channels.', providerHint()));
    return;
  }
  const filter = state.liveFilter || { mode: store.prefs.get('liveMode', 'all'), cat: store.prefs.get('liveCat', ''), q: '' };
  filter.edit = false;
  if (filter.cat && !list.counts.has(filter.cat)) filter.cat = '';
  state.liveFilter = filter;
  const chips = h('div', { class: 'chips', role: 'tablist', 'aria-label': 'Channel groups' });
  const modes = [['all', 'All'], ['fav', 'Favourites'], ['recent', 'Recent']];
  const catBtn = h('button', { class: 'select cat-btn', type: 'button', 'aria-haspopup': 'dialog' });
  catBtn.addEventListener('click', () => openCategoryPicker({
    title: 'Live TV categories', kind: 'live', list, current: filter.cat, extra: [['', 'All categories']],
    onPick: (id) => { filter.cat = id; store.prefs.set('liveCat', id); draw(); paintProgrammes(); }, onHide: () => { draw(); paintProgrammes(); },
  }));
  const editBtn = h('button', { class: 'chip edit-chip', type: 'button', 'aria-pressed': 'false', 'aria-label': 'Hide channels', title: 'Hide channels' },
    icon('eye-off'), h('span', { class: 'edit-label', text: 'Hide channels' }));
  const editNote = h('div', { class: 'edit-note', hidden: true },
    h('p', { class: 'small', text: 'Tap channels to hide or show them. Hidden channels are left out of the list and of search, on all your devices.' }),
    h('button', { class: 'btn small primary', type: 'button', text: 'Done', onclick: () => editBtn.click() }));
  editBtn.addEventListener('click', () => {
    filter.edit = !filter.edit;
    editBtn.setAttribute('aria-pressed', String(filter.edit));
    editNote.hidden = !filter.edit;
    draw();
    findProgrammes();
  });
  const q = h('input', { class: 'input', type: 'search', placeholder: 'Search channels and programmes',
    'aria-label': 'Search channels and programmes', enterkeyhint: 'search', autocomplete: 'off' });
  q.value = filter.q;
  q.addEventListener('input', () => { filter.q = q.value; draw(); findProgrammes(); });
  for (const [m, label] of modes) {
    const b = h('button', { class: 'chip', type: 'button', role: 'tab', text: label, 'aria-selected': filter.mode === m ? 'true' : 'false' });
    b.addEventListener('click', () => {
      filter.mode = m; store.prefs.set('liveMode', m);
      for (const c of chips.children) c.setAttribute('aria-selected', c === b ? 'true' : 'false');
      draw();
      paintProgrammes();
    });
    chips.append(b);
  }
  const count = h('span', { class: 'muted small' });
  head.lastChild.append(count);
  const ul = h('ul', { class: 'ch-list', role: 'list' });
  const progBox = h('div', { class: 'prog-results', hidden: true, 'aria-live': 'polite' });
  const chHead = h('h2', { class: 'eyebrow list-head', text: 'Channels', hidden: true });
  body.replaceChildren(h('div', { class: 'filters' }, h('div', { class: 'chip-row' }, chips, editBtn),
    h('div', { class: 'filter-row' }, catBtn, q), editNote), progBox, chHead, ul);

  // Everything the current mode / category / hidden settings show, before the search words.
  function shown() {
    let items = list.items;
    if (filter.mode === 'fav') { const f = store.favs.all(); items = items.filter((i) => f[i.id]); }
    if (filter.mode === 'recent') {
      const order = store.recent.list().filter((r) => r.kind === 'live').map((r) => r.id);
      items = order.map((id) => list.byId.get(id)).filter(Boolean);
    }
    if (filter.cat) items = items.filter((i) => i.cat === filter.cat);
    if (filter.mode === 'all') {
      // Hidden categories drop out of "All categories" (a picked one still shows); hidden
      // channels drop out unless editing. Favourites and Recent always show everything.
      const hc = store.hidden.cats('live');
      const hch = store.hidden.channels();
      if ((hc.size && !filter.cat) || (hch.size && !filter.edit)) {
        items = items.filter((i) => (filter.cat || !hc.has(i.cat)) && (filter.edit || !hch.has(i.id)));
      }
    }
    return items;
  }

  function current() {
    let items = shown();
    if (filter.q.trim()) {
      const terms = fold(filter.q).split(/\s+/).filter(Boolean);
      items = items.filter((i) => terms.every((t) => i.key.includes(t) || String(i.num) === t));
    }
    return items;
  }

  let stopChunks = null;
  function draw() {
    const hiddenCats = store.hidden.cats('live').size;
    catBtn.textContent = `${filter.cat ? list.cats.get(filter.cat) || 'Category' : 'All categories'}${hiddenCats ? ` · ${hiddenCats} hidden` : ''}`;
    const items = current();
    state.liveList = items;
    shownIds = null;
    setCount();
    if (stopChunks) stopChunks();
    if (!items.length) {
      emptyList();
      return;
    }
    stopChunks = chunked(ul, items, (ch) => channelRow(ch, filter.edit), { chunk: 80 });
    if (player.item && player.item.kind === 'live') markActiveChannel(player.item.id);
  }

  function setCount() {
    const n = state.liveList.length;
    const p = progs.shown;
    count.textContent = `${n.toLocaleString()} channel${n === 1 ? '' : 's'}${p ? ` · ${p} programme${p === 1 ? '' : 's'}` : ''}`;
  }

  function emptyList() {
    if (progs.shown) { ul.replaceChildren(h('li', { class: 'empty-li muted small', text: 'No channel names match.' })); return; }
    ul.replaceChildren(h('li', { class: 'empty-li' }, emptyBox(filter.mode === 'fav' ? 'No favourite channels yet' : 'Nothing here',
      filter.mode === 'fav' ? 'Tap the star next to a channel to add it.' : 'Try another category or filter.')));
  }

  // ---- programmes: the guide searched on the Pi (tvapp/epgsearch.py), after a pause in typing
  const progs = { timer: 0, ctl: null, data: null, shown: 0, open: new Set() };
  let shownIds = null;
  function allowed() {
    if (filter.mode === 'all' && !filter.cat && !store.hidden.cats('live').size && !store.hidden.channels().size) return null;
    if (!shownIds) shownIds = new Set(shown().map((i) => i.id));
    return shownIds;
  }
  function findProgrammes(now = false) {
    clearTimeout(progs.timer);
    const text = filter.q.trim();
    if (filter.edit || fold(text).replace(/[^\p{L}\p{N}]/gu, '').length < 2) {
      if (progs.ctl) progs.ctl.abort();
      progs.data = null;
      paintProgrammes();
      return;
    }
    progs.timer = setTimeout(async () => {
      if (progs.ctl) progs.ctl.abort();
      const ctl = progs.ctl = new AbortController();
      progBox.classList.add('busy');
      try {
        const c = await caps.get(player.preferNative).catch(() => '');
        const d = await searchGuide(text, c, ctl.signal);
        if (ctl.signal.aborted || text !== filter.q.trim()) return;
        progs.data = d;
      } catch (e) {
        if (ctl.signal.aborted) return;
        progs.data = { events: [], error: e.message };
      } finally {
        if (progs.ctl === ctl) progBox.classList.remove('busy');
      }
      paintProgrammes();
    }, now ? 0 : 280);
  }
  function paintProgrammes() {
    const d = progs.data;
    const sections = { live: [], soon: [], later: [] };
    if (d && !filter.edit) {
      const allow = allowed();
      for (const ev of d.events || []) {
        const chans = [];
        for (const c of ev.channels) {
          const ids = allow ? c.ids.filter((i) => allow.has(i)) : c.ids;
          const ch = list.byId.get(ids.includes(c.id) ? c.id : ids[0]);
          if (ch) chans.push({ ...c, ch });
        }
        if (chans.length) sections[ev.when].push({ ...ev, chans });
      }
    }
    progs.shown = sections.live.length + sections.soon.length + sections.later.length;
    setCount();
    chHead.hidden = !progs.shown;
    if (!state.liveList.length) emptyList();
    if (!progs.shown) {
      progBox.hidden = !(d && d.error);
      progBox.replaceChildren(d && d.error ? h('p', { class: 'muted small', text: `Programme search: ${d.error}` }) : '');
      return;
    }
    const out = [];
    for (const [when, title] of [['live', 'Live now'], ['soon', 'Starting soon'], ['later', 'Later']]) {
      const evs = sections[when];
      if (!evs.length) continue;
      const ol = h('ol', { class: 'prog-list', role: 'list' });
      const more = h('button', { class: 'btn ghost small prog-more', type: 'button' });
      const LIMIT = 6;
      const paint = (all) => {
        ol.replaceChildren(...(all ? evs : evs.slice(0, LIMIT)).map(programmeRow));
        more.hidden = all || evs.length <= LIMIT;
        more.textContent = `Show ${evs.length - LIMIT} more`;
      };
      more.addEventListener('click', () => { progs.open.add(when); paint(true); });
      paint(progs.open.has(when));
      out.push(h('section', { class: 'prog-sec' }, h('h2', { class: `eyebrow prog-sec-head ${when}`, text: title }), ol, more));
    }
    if (d.broad) out.push(h('p', { class: 'muted small', text: 'Lots of programmes match: showing only what is on now and starting soon. Add a word to see later ones.' }));
    progBox.replaceChildren(...out);
    progBox.hidden = false;
  }
  function programmeRow(ev) {
    const now = Date.now() / 1000;
    const head = h('div', { class: 'prog-head' });
    if (ev.when === 'live') head.append(h('span', { class: `badge ${ev.replay ? 'replay-badge' : 'live-badge'}`, text: ev.replay ? 'REPLAY' : 'LIVE' }));
    else head.append(h('span', { class: `prog-when ${ev.when}`, text: whenText(ev.start, now) }));
    head.append(h('strong', { class: `prog-title ${isFavEvent(ev.fav_ids) ? 'is-fav' : ''}`, text: ev.title }));
    const rows = [head, h('div', { class: 'prog-time muted small' }, `${fmtClock(ev.start)} – ${fmtClock(ev.stop)}`,
      ev.sub ? h('span', { class: 'prog-sub', text: ev.sub }) : '',
      ev.when === 'live' ? progressBar((now - ev.start) / (ev.stop - ev.start), 'thin') : '')];
    if (ev.strength < 2 && ev.desc) rows.push(h('p', { class: 'prog-desc muted small', text: ev.desc }));
    const chs = h('div', { class: 'prog-chs' });
    const SHOW = 3;
    const paint = (all) => {
      chs.replaceChildren(...(all ? ev.chans : ev.chans.slice(0, SHOW)).map(feedButton));
      const rest = ev.chans.length - SHOW + (ev.more || 0);
      if (!all && ev.chans.length > SHOW + 1) {
        chs.append(h('button', { class: 'chip prog-chs-more', type: 'button', text: `+${rest} more channel${rest === 1 ? '' : 's'}`,
          onclick: () => paint(true) }));
      }
    };
    paint(ev.chans.length <= SHOW + 1);
    rows.push(chs);
    return h('li', { class: `prog ${ev.strength < 2 ? 'weak' : ''}` }, ...rows);
  }

  draw();
  findProgrammes(true);
  state.redraw = (changed) => {
    if (changed.has('hide') || (filter.mode !== 'all' && (changed.has('fav') || changed.has('recent')))) { draw(); paintProgrammes(); }
  };
  state.cleanup.push(() => stopChunks && stopChunks(), () => { state.redraw = null; clearTimeout(progs.timer); if (progs.ctl) progs.ctl.abort(); });
  const timer = setInterval(async () => {
    guideData = await epgNow(); updateGuideRows(); if (player.isLive) renderLiveInfo(player.item);
    if (progs.data) findProgrammes(true);                // what's on now has moved on
  }, 60000);
  state.cleanup.push(() => clearInterval(timer));
  if (player.isLive) renderLiveInfo(player.item);
}

// edit: the row hides/shows the channel instead of playing it (Live > Hide channels)
function channelRow(ch, edit = false) {
  const nn = guideData[ch.id];
  const li = h('li', { class: 'ch-li', dataset: { id: ch.id } });
  const btn = h('button', { class: 'ch', type: 'button', 'aria-label': `${ch.num ? 'Channel ' + ch.num + ', ' : ''}${ch.name}` },
    h('span', { class: 'ch-num', text: ch.num || '' }),
    thumb(ch.img, ch.name, 'logo'),
    h('span', { class: 'ch-text' }, h('span', { class: 'ch-name', text: ch.name }), epgLine(nn)));
  const meta = { name: ch.name, img: ch.img, kind: 'live', num: ch.num };
  if (!edit) {
    btn.addEventListener('click', () => playChannel(ch));
    li.append(btn, favButton(ch.id, meta));
    return li;
  }
  const eye = h('button', { class: 'fav-btn eye-btn', type: 'button' });
  const paint = () => {
    const off = store.hidden.channels().has(ch.id);
    li.classList.toggle('is-hidden', off);
    eye.setAttribute('aria-pressed', String(off));
    eye.setAttribute('aria-label', off ? `Show ${ch.name}` : `Hide ${ch.name}`);
    setIcon(eye, off ? 'eye-off' : 'eye');
  };
  const toggle = () => { store.hidden.setChannel(ch.id, meta, !store.hidden.channels().has(ch.id)); paint(); };
  btn.addEventListener('click', toggle);
  eye.addEventListener('click', toggle);
  li.append(btn, eye);
  paint();
  return li;
}

function epgLine(nn) {
  const wrap = h('span', { class: 'ch-epg' });
  fillEpg(wrap, nn);
  return wrap;
}

function fillEpg(wrap, nn) {
  wrap.replaceChildren();
  if (!nn) return;
  const now = Date.now() / 1000;
  if (nn.now) {
    const [s, e, t] = nn.now;
    wrap.append(h('span', { class: 'epg-now', text: t }), progressBar((now - s) / (e - s), 'thin'));
  }
  if (nn.next) wrap.append(h('span', { class: 'epg-next', text: `${fmtClock(nn.next[0])}  ${nn.next[2]}` }));
}

function updateGuideRows() {
  for (const li of document.querySelectorAll('.ch-li')) {
    const w = li.querySelector('.ch-epg');
    if (w) fillEpg(w, guideData[li.dataset.id]);
  }
}

function markActiveChannel(id) {
  for (const el of document.querySelectorAll('.ch-li.active')) el.classList.remove('active');
  const li = document.querySelector(`.ch-li[data-id="${CSS.escape(id)}"]`);
  if (li) {
    li.classList.add('active');
    const r = li.getBoundingClientRect();
    if (r.top < 0 || r.bottom > innerHeight) li.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }
}

function maybeIdle() {
  if (state.section !== 'live' || player.item) return;
  const last = store.lastChannel.get();
  if (!last) { player.showMode('off'); main.classList.remove('live-playing'); return; }
  main.classList.add('live-playing');
  player.showIdle(last);
  renderLiveInfo(last);
}

async function renderLiveInfo(item) {
  const box = $('p-info');
  if (!item) { box.replaceChildren(); return; }
  const nn = guideData[item.id];
  const now = Date.now() / 1000;
  const top = h('div', { class: 'info-top' },
    thumb(item.img, item.name, 'logo sm'),
    h('div', { class: 'info-titles' }, h('strong', { text: `${item.num ? item.num + '  ' : ''}${item.name}` }),
      nn && nn.now ? h('span', { class: 'muted small', text: `${fmtClock(nn.now[0])} – ${fmtClock(nn.now[1])}` }) : null),
    favButton(item.id, { name: item.name, img: item.img, kind: 'live', num: item.num }));
  const rows = [top];
  if (nn && nn.now) {
    rows.push(h('div', { class: 'info-now' }, h('span', { class: 'eyebrow', text: 'Now' }), h('span', { text: nn.now[2] }),
      progressBar((now - nn.now[0]) / (nn.now[1] - nn.now[0]))));
    player.setTitles(item.name, nn.now[2]);
  }
  if (nn && nn.next) rows.push(h('div', { class: 'info-next' }, h('span', { class: 'eyebrow', text: 'Next' }), h('span', { text: `${fmtClock(nn.next[0])}  ${nn.next[2]}` })));
  if (!nn) rows.push(h('p', { class: 'muted small', text: 'No programme guide for this channel.' }));
  const guideBtn = nn ? h('button', { class: 'btn ghost small', type: 'button', 'aria-expanded': 'false' }, icon('guide'), 'Full guide') : null;
  const sched = h('ol', { class: 'schedule', hidden: true });
  if (guideBtn) {
    guideBtn.addEventListener('click', async () => {
      const open = sched.hidden;
      sched.hidden = !open;
      guideBtn.setAttribute('aria-expanded', String(open));
      if (open && !sched.childElementCount) {
        sched.append(h('li', { class: 'muted small', text: 'Loading…' }));
        try {
          const g = await api(`/epg/${item.id}`);
          const t = Date.now() / 1000;
          sched.replaceChildren(...g.rows.map(([s, e, title, desc]) => h('li', { class: s <= t && t < e ? 'on-now' : '' },
            h('span', { class: 'sched-time', text: fmtClock(s) }),
            h('span', { class: 'sched-text' }, h('strong', { text: title }), desc ? h('span', { class: 'muted small', text: desc }) : null))));
          if (!g.rows.length) sched.replaceChildren(h('li', { class: 'muted small', text: 'No upcoming programmes.' }));
        } catch (e) { sched.replaceChildren(h('li', { class: 'muted small', text: e.message })); }
      }
    });
    rows.push(guideBtn, sched);
  }
  box.replaceChildren(...rows);
}

// ------------------------------------------------------------------ SPORTS
// Matches and events from the guide (tvapp/epgsearch.py Index.sports): one card per event, its
// channels best-for-this-device first. Tapping a card plays the first through the normal live
// path (Auto quality); "N more" lists the other broadcasters.
const SPORTS = [['football', 'Football'], ['all', 'All sports'], ['f1', 'F1'], ['mma', 'UFC / MMA'], ['boxing', 'Boxing'],
  ['basketball', 'Basketball'], ['nfl', 'NFL'], ['tennis', 'Tennis'], ['cricket', 'Cricket'], ['rugby', 'Rugby']];
const SPORT_NAMES = Object.fromEntries([...SPORTS, ['hockey', 'Ice hockey'], ['handball', 'Handball'],
  ['volleyball', 'Volleyball'], ['other', 'Other']]);
const sportsCache = new Map();

async function sportsData(day, force = false) {
  const hit = sportsCache.get(day);
  if (hit && !force && Date.now() - hit.at < 55000) return hit.d;
  const c = await caps.get(player.preferNative).catch(() => '');
  const tzo = -new Date().getTimezoneOffset();
  const d = await api(`/sports?day=${day}&tzo=${tzo}&caps=${encodeURIComponent(c)}`, { timeout: 30000 });
  if (!d.pending) sportsCache.set(day, { at: Date.now(), d });
  return d;
}

// For You: the favourites' events today and tomorrow (Index.for_you on the Pi). The favourites
// are sent with the request, so a star tapped a moment ago counts before it has synced.
async function forYouData(force = false) {
  const favs = store.sportsFavs.ids().sort();
  const key = `foryou|${favs.join(',')}`;
  const hit = sportsCache.get(key);
  if (hit && !force && Date.now() - hit.at < 55000) return hit.d;
  if (!favs.length) return { events: [], counts: {}, none: true };
  const c = await caps.get(player.preferNative).catch(() => '');
  const tzo = -new Date().getTimezoneOffset();
  const d = await api(`/sports?view=foryou&fav=${encodeURIComponent(favs.join(','))}&tzo=${tzo}&caps=${encodeURIComponent(c)}`,
    { timeout: 30000 });
  if (!d.pending) sportsCache.set(key, { at: Date.now(), d });
  return d;
}

// Every team and competition that can be a favourite (tvapp/sports.py), fetched once.
let sportsCat = null;
async function sportsCatalogue() {
  if (!sportsCat) {
    const d = await api('/sports/catalogue');
    sportsCat = { items: d.items, byId: new Map(d.items.map((i) => [i.id, i])) };
    for (const i of d.items) i.key = fold(`${i.name} ${(i.aka || []).join(' ')}`);
  }
  return sportsCat;
}

function isFavEvent(ids) {
  return (ids || []).some((i) => store.sportsFavs.has(i));
}

const ALERT_CHOICES = [['off', 'Off'], ['15', '15 minutes before'], ['30', '30 minutes before'], ['60', '60 minutes before']];

// Favourite teams and competitions: the list, a search over every known one, and match alerts.
async function openSportsFavs() {
  const box = h('div', { class: 'sfav-sheet' }, h('h2', { text: 'Favourite teams & competitions' }), skeletonRows(4));
  openSheet(box, 'Favourite teams and competitions');
  let cat;
  try { cat = await sportsCatalogue(); } catch (e) { box.replaceChildren(errorBox(e.message, () => openSportsFavs())); return; }
  const alerts = selectEl('Match alerts', ALERT_CHOICES, String(store.prefs.get('sportsAlerts', 'off')),
    (v) => { store.prefs.set('sportsAlerts', v); toast(v === 'off' ? 'Match alerts off' : `Alerts ${v} minutes before kick-off`, 1800); });
  const mine = h('ul', { class: 'sfav-list', role: 'list' });
  const q = h('input', { class: 'input', type: 'search', placeholder: 'Find a team or competition', 'aria-label': 'Find a team or competition',
    autocomplete: 'off', enterkeyhint: 'search' });
  const found = h('ul', { class: 'sfav-list', role: 'list' });
  const row = (item) => {
    const on = store.sportsFavs.has(item.id);
    const star = h('button', { class: `fav-btn ${on ? 'on' : ''}`, type: 'button', 'aria-pressed': String(on),
      'aria-label': `${on ? 'Remove' : 'Add'} ${item.name}` }, icon('star'));
    star.addEventListener('click', () => { store.sportsFavs.toggle(item.id, item); draw(); });
    const kind = item.kind === 'comp' ? 'Competition' : item.national ? 'National team' : 'Team';
    return h('li', { class: 'sfav-row' }, h('span', { class: 'sfav-name' }, h('strong', { text: item.name }),
      h('span', { class: 'muted small', text: [kind, SPORT_NAMES[item.sport]].filter(Boolean).join(' · ') })), star);
  };
  function draw() {
    const list = store.sportsFavs.list().map((f) => cat.byId.get(f.id) || { id: f.id, name: f.name || f.id, kind: f.kind || 'team' });
    mine.replaceChildren(...(list.length ? list.map(row) : [h('li', { class: 'muted small', text: 'None yet: find a team or competition below, or tap the star on a Sports card.' })]));
    const terms = fold(q.value).split(/\s+/).filter(Boolean);
    const hits = terms.length ? cat.items.filter((i) => terms.every((t) => i.key.includes(t))).slice(0, 30) : [];
    found.replaceChildren(...hits.map(row));
    if (terms.length && !hits.length) found.append(h('li', { class: 'muted small', text: 'Nothing known by that name yet.' }));
  }
  q.addEventListener('input', draw);
  box.replaceChildren(h('h2', { text: 'Favourite teams & competitions' }),
    h('p', { class: 'muted small', text: 'They fill Sports › For you, on all your devices. A star marks their events elsewhere.' }),
    h('div', { class: 'sfav-alerts' }, h('label', { class: 'small', text: 'Telegram alert before kick-off' }), alerts),
    h('p', { class: 'muted small', text: 'One message per match, sent by the Pi (no scores). Not for replays, youth or women’s games.' }),
    h('h3', { class: 'eyebrow', text: 'Your favourites' }), mine, q, found);
  draw();
}

async function viewSports() {
  const count = h('span', { class: 'muted small' });
  const favBtn = h('button', { class: 'chip edit-chip', type: 'button', 'aria-label': 'Favourite teams and competitions',
    title: 'Favourite teams and competitions' }, icon('star'), h('span', { class: 'edit-label', text: 'Favourites' }));
  favBtn.addEventListener('click', () => openSportsFavs());
  const head = viewHead('Sports', count, favBtn);
  const body = h('div', { class: 'sports-body' }, skeletonRows(6));
  view.replaceChildren(h('div', { class: 'sports-view' }, head, body));
  const pick = { sport: store.prefs.get('sportsTab', 'football'), day: 'today' };
  if (!SPORTS.some(([k]) => k === pick.sport)) pick.sport = 'football';
  const chips = h('div', { class: 'chips', role: 'tablist', 'aria-label': 'Sport' });
  const days = h('div', { class: 'chips day-chips', role: 'tablist', 'aria-label': 'Day' });
  const list = h('div', { class: 'sp-sections', 'aria-live': 'polite' });
  let data = null;
  let mine = null;                                // For You data
  let live = null;
  const load = (force = false) => (pick.sport === 'foryou' ? forYouData(force).then((d) => { mine = d; })
    : sportsData(pick.day, force).then((d) => { data = d; }));
  try {
    [live] = await Promise.all([catalogue('live'), sportsData(pick.day).then((d) => { data = d; }),
      pick.sport === 'foryou' ? forYouData().then((d) => { mine = d; }) : null]);
    sportsCatalogue().then(() => paint()).catch(() => {});      // names for the card stars
  } catch (e) {
    body.replaceChildren(errorBox(e.message, () => render()));
    return;
  }
  if (state.section !== 'sports') return;
  body.replaceChildren(h('div', { class: 'filters' }, chips, days), list);
  for (const [d, label] of [['today', 'Today'], ['tomorrow', 'Tomorrow']]) {
    const b = h('button', { class: 'chip', type: 'button', role: 'tab', text: label, 'aria-selected': String(pick.day === d) });
    b.addEventListener('click', async () => {
      pick.day = d;
      for (const x of days.children) x.setAttribute('aria-selected', String(x === b));
      await reload();
    });
    days.append(b);
  }

  // replays the guide doesn't mark: the Pi's fixture dates (the new interface's sidecar) say so
  let fxTries = 0;
  async function fixtures() {
    const evs = (pick.sport === 'foryou' ? mine && mine.events : data && data.events) || [];
    const r = await checkFixtures(evs);
    const late = await checkKickoffs(evs);
    if (state.section !== 'sports') return;
    if (r.found || late) paint();
    if (r.pending && fxTries++ < 3) setTimeout(fixtures, 8000);
  }

  async function reload(force = false) {
    list.classList.add('busy');
    try { await load(force); } catch (e) { list.replaceChildren(errorBox(e.message, () => render())); return; }
    finally { list.classList.remove('busy'); }
    paint();
    fixtures();
  }

  function visible(events) {
    const out = [];
    for (const ev of events || []) {
      if (!['all', 'foryou'].includes(pick.sport) && ev.sport !== pick.sport) continue;
      const feeds = visibleFeeds(ev.channels, live);
      if (feeds.length) out.push({ ...ev, feeds });
    }
    return out;
  }

  function paintChips() {
    const n = {};
    for (const ev of data.events || []) n[ev.sport] = (n[ev.sport] || 0) + 1;
    chips.replaceChildren();
    for (const [k, label] of [['foryou', 'For you'], ...SPORTS]) {
      const c = k === 'all' ? (data.events || []).length : k === 'foryou' ? 0 : n[k] || 0;
      if (!c && k !== pick.sport && !['football', 'all', 'foryou'].includes(k)) continue;
      const b = h('button', { class: `chip ${k === 'foryou' ? 'foryou-chip' : ''}`, type: 'button', role: 'tab', 'aria-selected': String(pick.sport === k) },
        k === 'foryou' ? icon('star') : '', label, h('span', { class: 'chip-n', text: c ? String(c) : '' }));
      b.addEventListener('click', async () => {
        pick.sport = k;
        store.prefs.set('sportsTab', k);
        if (k === 'foryou' || !data) await reload(); else paint();
      });
      chips.append(b);
    }
    days.hidden = pick.sport === 'foryou';         // For You covers today and tomorrow
  }

  function section(when, title, items) {
    const ol = h('ol', { class: 'sp-list', role: 'list' });
    const LIMIT = when === 'live' ? 40 : 24;
    const more = h('button', { class: 'btn ghost small prog-more', type: 'button', text: `Show ${items.length - LIMIT} more` });
    ol.append(...items.slice(0, LIMIT).map(sportCard));
    more.hidden = items.length <= LIMIT;
    more.addEventListener('click', () => { ol.append(...items.slice(LIMIT).map(sportCard)); more.hidden = true; });
    return h('section', { class: 'prog-sec' }, h('h2', { class: `eyebrow prog-sec-head ${when}`, text: title }), ol, more);
  }

  function paint() {
    if (!data) return;
    paintChips();
    const foryou = pick.sport === 'foryou';
    if (foryou && !mine) { reload(); return; }
    const evs = visible(foryou ? mine.events : data.events).map(withFixture);
    count.textContent = `${evs.length} event${evs.length === 1 ? '' : 's'}`;
    // replays airing now get their own section after the live games, never mixed in with them
    const secs = foryou ? [['live', 'Live now'], ['replay', 'Replays on now'], ['soon', 'Starting soon'], ['later', 'Later today'], ['tomorrow', 'Tomorrow']]
      : pick.day === 'today' ? [['live', 'Live now'], ['replay', 'Replays on now'], ['soon', 'Starting soon'], ['later', 'Later today']] : [['all', 'Tomorrow']];
    const isWhen = (e, when) => (when === 'replay' ? e.when === 'live' && !!e.replay : when === 'live' ? e.when === 'live' && !e.replay : e.when === when);
    const out = [];
    for (const [when, title] of secs) {
      const items = evs.filter((e) => when === 'all' || (foryou ? (when === 'tomorrow' ? e.day === 'tomorrow' : e.day === 'today' && isWhen(e, when)) : isWhen(e, when)));
      if (!items.length) continue;
      if (when !== 'live') items.sort((a, b) => a.start - b.start || b.score - a.score);
      out.push(section(when, title, items));
    }
    if (!out.length && foryou) {
      const has = store.sportsFavs.ids().length;
      const add = h('button', { class: 'btn primary small', type: 'button', onclick: () => openSportsFavs() }, icon('star'), 'Choose favourites');
      out.push(emptyBox(has ? 'Nothing from your favourites in the guide today or tomorrow' : 'No favourite teams or competitions yet',
        has ? 'Matches show up here as soon as a channel’s guide lists them.'
          : 'Add teams like Arsenal or competitions like the Champions League, or tap ☆ on any Sports card. Their matches from every broadcaster then appear here.',
        add));
    } else if (!out.length) {
      const what = pick.sport === 'all' ? 'sport' : (SPORT_NAMES[pick.sport] || 'sport').toLowerCase();
      out.push(emptyBox(`No ${what} in the guide ${pick.day === 'today' ? 'for the rest of today' : 'for tomorrow'}`,
        'Only channels with a programme guide are listed. Live search finds everything else by name.'));
    }
    list.replaceChildren(...out);
  }

  // The card's own ☆: favourite its teams / competition in place (names from the catalogue)
  function favControl(ev, li) {
    const ids = [...(ev.teams || []), ev.comp_id].filter(Boolean);
    if (!ids.length) return null;
    const on = isFavEvent(ids);
    const btn = h('button', { class: `sp-star ${on ? 'on' : ''}`, type: 'button', 'aria-expanded': 'false',
      'aria-label': `Favourite ${ev.title} teams or competition`, title: 'Favourite a team or competition' }, icon('star'));
    const row = h('div', { class: 'sp-fav-row', hidden: true });
    const paintRow = () => {
      row.replaceChildren(...ids.map((id) => {
        const item = (sportsCat && sportsCat.byId.get(id)) || { id, name: id.split(':')[1].replace(/-/g, ' '), kind: id.split(':')[0] };
        const sel = store.sportsFavs.has(id);
        const chip = h('button', { class: `chip sp-fav-chip ${sel ? 'on' : ''}`, type: 'button', 'aria-pressed': String(sel) },
          icon('star'), item.name);
        chip.addEventListener('click', () => {
          const now = store.sportsFavs.toggle(id, item);
          toast(now ? `${item.name} added to favourites` : `${item.name} removed`, 1600);
          paintRow();
          btn.classList.toggle('on', isFavEvent(ids));
          li.classList.toggle('is-fav', isFavEvent(ids));
        });
        return chip;
      }));
    };
    btn.addEventListener('click', () => {
      row.hidden = !row.hidden;
      btn.setAttribute('aria-expanded', String(!row.hidden));
      if (!row.hidden) paintRow();
    });
    return [btn, row];
  }

  function sportCard(ev) {
    const now = Date.now() / 1000;
    const best = ev.feeds[0];
    const top = h('div', { class: 'sp-top' });
    // a replay on now is never shown as LIVE
    if (ev.when === 'live') top.append(h('span', { class: `badge ${ev.replay ? 'replay-badge' : 'live-badge'}`, text: ev.replay ? 'REPLAY' : 'LIVE' }));
    else top.append(h('span', { class: `prog-when ${ev.when}`, text: whenText(ev.start, now) }));
    const meta = [ev.comp, ['all', 'foryou'].includes(pick.sport) || !ev.comp ? SPORT_NAMES[ev.sport] : ''].filter(Boolean).join(' · ');
    if (meta) top.append(h('span', { class: 'sp-comp', text: meta }));
    if (ev.replay && ev.when !== 'live') top.append(h('span', { class: 'sp-tag', text: 'Replay' }));
    if (ev.youth) top.append(h('span', { class: 'sp-tag', text: 'Youth' }));
    const ids = [...(ev.teams || []), ev.comp_id].filter(Boolean);
    const main = h('button', { class: 'sp-main', type: 'button', 'aria-label': `Play ${ev.title} on ${best.ch.name}` },
      top, h('strong', { class: 'sp-title', text: ev.title }),
      h('span', { class: 'sp-time muted small' }, `${fmtClock(ev.start)} – ${fmtClock(ev.stop)}`,
        ev.when === 'live' ? progressBar((now - ev.start) / (ev.stop - ev.start), 'thin') : ''),
      h('span', { class: 'sp-best' }, icon('play', 'ico sm'), thumb(best.ch.img, best.ch.name, 'logo sm'),
        h('span', { class: 'prog-ch-name', text: best.ch.name }), qualityBadge(best)));
    main.addEventListener('click', () => playChannel(best.ch));
    const li = h('li', { class: `sp-card ${ev.when === 'live' && !ev.replay ? 'is-live' : ''} ${isFavEvent(ids) ? 'is-fav' : ''}` }, main);
    const fav = favControl(ev, li);
    if (fav) li.append(...fav);
    const others = ev.feeds.length - 1 + (ev.more || 0);
    if (others > 0) {
      const feeds = h('div', { class: 'prog-chs', hidden: true });
      const tog = h('button', { class: 'sp-more', type: 'button', 'aria-expanded': 'false',
        text: `${others} more broadcaster${others === 1 ? '' : 's'}` });
      tog.addEventListener('click', () => {
        const open = feeds.hidden;
        if (open && !feeds.childElementCount) feeds.append(...ev.feeds.map(feedButton));
        feeds.hidden = !open;
        tog.setAttribute('aria-expanded', String(open));
        tog.textContent = open ? 'Hide broadcasters' : `${others} more broadcaster${others === 1 ? '' : 's'}`;
      });
      li.append(tog, feeds);
    }
    return li;
  }

  paint();
  fixtures();
  // another device changed favourites or hidden channels: show it (For You asks the Pi again)
  state.redraw = (changed) => {
    if (changed.has('sfav') && pick.sport === 'foryou') reload();
    else if (changed.has('hide') || changed.has('sfav')) paint();
  };
  const timer = setInterval(() => { reload(true).catch(() => {}); }, 60000);
  state.cleanup.push(() => clearInterval(timer), () => { state.redraw = null; });
}

// ------------------------------------------------------------------ category picker
// One sheet to choose what a view shows and to hide categories everywhere (synced like
// favourites). Searchable, because providers have hundreds of categories. For Live it
// also lists hidden channels so they can be shown again.
//   kind: 'live' | 'movie' | 'series'; extra: rows above the categories, e.g. [['', 'All categories']]
function openCategoryPicker({ title, kind, list, current, extra, onPick, onHide }) {
  const cats = usedCats(list).map(([id, name, n]) => ({ id, name, n, key: fold(name) }));
  const q = h('input', { class: 'input', type: 'search', placeholder: `Search ${cats.length.toLocaleString()} categories`,
    'aria-label': 'Search categories', enterkeyhint: 'search', autocomplete: 'off' });
  let mode = 'all';
  const modeChips = h('div', { class: 'chips', role: 'tablist', 'aria-label': 'Which categories' });
  for (const [m, label] of [['all', 'All'], ['shown', 'Shown'], ['hidden', 'Hidden']]) {
    const b = h('button', { class: 'chip', type: 'button', role: 'tab', text: label, 'aria-selected': String(m === mode) });
    b.addEventListener('click', () => {
      mode = m;
      for (const c of modeChips.children) c.setAttribute('aria-selected', String(c === b));
      draw();
    });
    modeChips.append(b);
  }
  const bulk = h('div', { class: 'actions picker-bulk' });
  const ul = h('ul', { class: 'picker-list', role: 'list' });
  const chBox = kind === 'live' ? h('section', { class: 'picker-channels' }) : null;
  const terms = () => fold(q.value).split(/\s+/).filter(Boolean);

  const pick = (id) => { closeSheet(false); onPick(id); };
  const hide = (cs, on) => {
    const n = store.hidden.setCats(kind, cs.map((c) => [c.id, c.name]), on);
    if (cs.length > 1) toast(`${on ? 'Hidden' : 'Showing'}: ${n} categor${n === 1 ? 'y' : 'ies'}`);
    draw();
    onHide();
  };

  function row(c, off) {
    const li = h('li', { class: `pick-row${off ? ' is-hidden' : ''}${c.id === current ? ' is-current' : ''}` });
    const name = h('button', { class: 'pick-name', type: 'button', onclick: () => pick(c.id) },
      h('span', { class: 'pick-text', text: c.name }), h('span', { class: 'muted small', text: c.n.toLocaleString() }));
    const eye = h('button', { class: 'fav-btn eye-btn', type: 'button', 'aria-pressed': String(off),
      'aria-label': off ? `Show ${c.name}` : `Hide ${c.name}`, onclick: () => hide([c], !off) }, icon(off ? 'eye-off' : 'eye'));
    li.append(name, eye);
    return li;
  }

  function drawChannels() {
    const tt = terms();
    const all = store.hidden.channelList();
    const shown = all.filter((c) => tt.every((term) => fold(String(c.name || '')).includes(term)));
    if (!all.length) {
      chBox.replaceChildren(h('h3', { class: 'sub-title', text: 'Hidden channels' }),
        h('p', { class: 'muted small', text: 'None. Use “Hide channels” on the Live TV page to hide single channels.' }));
      return;
    }
    chBox.replaceChildren(h('h3', { class: 'sub-title', text: `Hidden channels (${all.length})` }),
      h('ul', { class: 'picker-list', role: 'list' }, shown.map((c) => h('li', { class: 'pick-row is-hidden' },
        h('span', { class: 'pick-name static' }, h('span', { class: 'pick-text', text: c.name || c.id }),
          c.num ? h('span', { class: 'muted small', text: String(c.num) }) : null),
        h('button', { class: 'btn small', type: 'button', text: 'Show', onclick: () => {
          store.hidden.setChannel(c.id, c, false);
          drawChannels();
          onHide();
        } })))));
  }

  function draw() {
    const off = store.hidden.cats(kind);
    const tt = terms();
    const matches = cats.filter((c) => tt.every((term) => c.key.includes(term))
      && (mode === 'all' || (mode === 'hidden') === off.has(c.id)));
    const rows = [];
    if (!tt.length && mode === 'all') {
      for (const [id, label] of extra) {
        rows.push(h('li', { class: `pick-row${id === current ? ' is-current' : ''}` },
          h('button', { class: 'pick-name', type: 'button', onclick: () => pick(id) }, h('span', { class: 'pick-text', text: label }))));
      }
    }
    for (const c of matches) rows.push(row(c, off.has(c.id)));
    if (!matches.length) rows.push(h('li', { class: 'muted small picker-empty', text: mode === 'hidden' && !tt.length ? 'No hidden categories.' : 'No categories match.' }));
    ul.replaceChildren(...rows);
    const toHide = matches.filter((c) => !off.has(c.id));
    const toShow = matches.filter((c) => off.has(c.id));
    bulk.replaceChildren(...[
      tt.length && toHide.length > 1 ? h('button', { class: 'btn small', type: 'button', onclick: () => hide(toHide, true) }, icon('eye-off'), `Hide all ${toHide.length}`) : null,
      tt.length && toShow.length > 1 ? h('button', { class: 'btn small', type: 'button', onclick: () => hide(toShow, false) }, icon('eye'), `Show all ${toShow.length}`) : null,
    ].filter(Boolean));
    if (chBox) drawChannels();
  }

  let timer = 0;
  q.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(draw, 80); });
  openSheet(h('div', { class: 'picker' },
    h('h2', { id: 'sheet-title', class: 'picker-title', text: title }),
    h('p', { class: 'muted small', text: 'Tap a category to show it. The eye hides a category from “All” and from search, on all your devices.' }),
    q, modeChips, bulk, ul, chBox), title);
  draw();
  if (matchMedia('(hover: hover)').matches) setTimeout(() => q.focus({ preventScroll: true }), 60);   // no keyboard pop-up on phones
}

// ------------------------------------------------------------------ MOVIES / SERIES grids
// Progress bar / watched tick on a movie poster (series cards carry neither).
function setPosterBadges(poster, id) {
  for (const el of poster.querySelectorAll(':scope > .pbar, :scope > .watched')) el.remove();
  const frac = store.progress.fraction(id);
  if (frac > 0.01 && frac < 0.999) poster.append(progressBar(frac, 'on-poster'));
  if ((store.progress.get(id) || {}).done) poster.append(h('span', { class: 'watched', 'aria-label': 'Watched' }, icon('check')));
}

function cardFor(kind, it) {
  const a = h('a', { class: 'card', href: `#/${kind}/${it.id}`, 'aria-label': it.name, dataset: { id: it.id, kind } });
  const poster = thumb(it.img, it.name, 'poster');
  if (kind === 'movies') setPosterBadges(poster, it.id);
  a.append(poster, h('span', { class: 'card-title', text: it.name }), h('span', { class: 'card-sub', text: [it.year || '', it.rating ? `★ ${Number(it.rating).toFixed(1)}` : ''].filter(Boolean).join('  ·  ') }));
  return a;
}

async function viewGrid(kind) {
  const title = kind === 'movies' ? 'Movies' : 'Series';
  const head = viewHead(title);
  const body = h('div', {}, skeletonGrid(18));
  view.replaceChildren(head, body);
  let list;
  try { list = await catalogue(kind); } catch (e) { body.replaceChildren(errorBox(e.message, () => render())); return; }
  if (state.section !== kind) return;
  if (!list.items.length) { body.replaceChildren(emptyBox(`No ${title.toLowerCase()}`, 'The provider has none, or the catalogue is still loading.', providerHint())); return; }
  const pk = `${kind}.`;
  const hideKind = STORE_KIND[kind];
  let cat = store.prefs.get(pk + 'cat', '');
  let sort = store.prefs.get(pk + 'sort', kind === 'movies' ? 'added' : 'name');
  if (cat && cat !== '__fav' && !list.counts.has(cat)) cat = '';
  const extra = [['', 'All categories'], ['__fav', '★ Favourites']];
  const sorts = kind === 'movies' ? [['added', 'Recently added'], ['name', 'A–Z'], ['year', 'Newest'], ['rating', 'Top rated']] : [['name', 'A–Z'], ['year', 'Newest'], ['rating', 'Top rated']];
  const count = h('span', { class: 'muted small' });
  const grid = h('div', { class: 'grid' });
  const catBtn = h('button', { class: 'select cat-btn', type: 'button', 'aria-haspopup': 'dialog' });
  catBtn.addEventListener('click', () => openCategoryPicker({
    title: `${title} categories`, kind: hideKind, list, current: cat, extra,
    onPick: (id) => { cat = id; store.prefs.set(pk + 'cat', id); draw(); }, onHide: () => draw(),
  }));
  const sortSel = selectEl('Sort', sorts, sort, (v) => { sort = v; store.prefs.set(pk + 'sort', v); draw(); });
  sortSel.classList.add('sort-sel');
  const langSel = selectEl('Language', langPrefs(), langPref(), (v) => { store.prefs.set('catLang', v); draw(); });
  langSel.classList.add('sort-sel');
  langSel.title = 'Your language is chosen in Settings';
  const q = h('input', { class: 'input', type: 'search', placeholder: `Filter ${title.toLowerCase()}`, 'aria-label': `Filter ${title.toLowerCase()}`, enterkeyhint: 'search', autocomplete: 'off' });
  q.value = state.gridQuery[kind] || '';
  let qTimer = 0;
  q.addEventListener('input', () => { clearTimeout(qTimer); qTimer = setTimeout(() => { state.gridQuery[kind] = q.value; draw(); }, 150); });
  head.lastChild.append(count);
  body.replaceChildren(h('div', { class: 'filters' }, h('div', { class: 'filter-row' }, catBtn, sortSel, langSel, q)), grid);
  let stop = null;
  function draw() {
    const hc = store.hidden.cats(hideKind);
    const name = cat ? (extra.find(([k]) => k === cat) || [])[1] || list.cats.get(cat) || 'Category' : 'All categories';
    catBtn.textContent = `${name}${hc.size ? ` · ${hc.size} hidden` : ''}`;
    let items = langItems(list, sort, langPref());       // sorted once per session; filtering keeps the order
    if (cat === '__fav') { const f = store.favs.all(); items = items.filter((i) => f[i.id]); } else if (cat) items = items.filter((i) => i.cat === cat);
    else if (hc.size) items = items.filter((i) => !hc.has(i.cat));
    const terms = fold(q.value).split(/\s+/).filter(Boolean);
    if (terms.length) items = items.filter((i) => terms.every((term) => i.key.includes(term)));
    count.textContent = `${items.length.toLocaleString()} title${items.length === 1 ? '' : 's'}`;
    if (stop) stop();
    if (!items.length) {
      grid.replaceChildren(terms.length ? emptyBox('No matches', `Nothing here matches “${q.value.trim()}”.`)
        : emptyBox(cat === '__fav' ? 'No favourites yet' : 'Nothing here', cat === '__fav' ? 'Open a title and tap the star.' : ''));
      return;
    }
    stop = chunked(grid, items, (it) => cardFor(kind, it), { chunk: 60 });
  }
  draw();
  state.cleanup.push(() => clearTimeout(qTimer));
  state.redraw = (changed) => {
    if (changed.has('pref') && langSel.value !== langPref()) { langSel.value = langPref(); draw(); }
    if (changed.has('hide') || (cat === '__fav' && changed.has('fav'))) draw();
  };
  state.cleanup.push(() => stop && stop(), () => { state.redraw = null; });
}

// Update badges on the cards already on screen. Re-rendering the grid instead would
// rebuild it from the first chunk and lose the scroll position.
function refreshProgressBadges() {
  for (const a of view.querySelectorAll('a.card[data-kind="movies"]')) {
    const poster = a.querySelector('.thumb');
    if (poster) setPosterBadges(poster, a.dataset.id);
  }
}

// ------------------------------------------------------------------ details sheet
let sheetOpenedByUs = false;
function openSheet(content, label) {
  const sheet = $('sheet');
  sheet.replaceChildren(
    h('button', { class: 'icon-btn sheet-close', type: 'button', 'aria-label': 'Close', onclick: () => closeSheet(true) }, icon('close')),
    content);
  sheet.setAttribute('aria-label', label || 'Details');
  sheet.hidden = false;
  $('sheet-backdrop').hidden = false;
  document.body.classList.add('sheet-open');
  sheet.scrollTop = 0;
  requestAnimationFrame(() => sheet.querySelector('.sheet-close').focus({ preventScroll: true }));
}

function closeSheet(navigate) {
  const sheet = $('sheet');
  if (sheet.hidden) return;
  sheet.hidden = true;
  $('sheet-backdrop').hidden = true;
  document.body.classList.remove('sheet-open');
  sheet.replaceChildren();
  if (navigate) {
    const { sec, id } = parseHash();
    if (id) {
      if (sheetOpenedByUs && history.length > 1) history.back();
      else history.replaceState(null, '', `#/${sec}`);
    }
  }
  sheetOpenedByUs = false;
}
$('sheet-backdrop').addEventListener('click', () => closeSheet(true));
view.addEventListener('click', (e) => { if (e.target.closest('a.card')) sheetOpenedByUs = true; });

async function openDetails(kind, id) {
  const loading = h('div', { class: 'details' }, h('div', { class: 'sk-hero' }), skeletonRows(4));
  openSheet(loading, 'Loading');
  try {
    if (kind === 'movies') await movieDetails(id); else await seriesDetails(id);
  } catch (e) {
    const { id: cur } = parseHash();
    if (cur === id) openSheet(errorBox(e.message, () => openDetails(kind, id)), 'Error');
  }
}

function hero(d) {
  const top = h('div', { class: 'hero' });
  if (d.backdrop) {
    const bd = h('img', { class: 'hero-bg', src: img(d.backdrop), alt: '', decoding: 'async' });
    bd.addEventListener('error', () => bd.remove(), { once: true });
    top.append(bd);
  }
  top.append(thumb(d.img, d.name, 'poster hero-poster'));
  const meta = [d.year || '', d.dur ? fmtDuration(d.dur) : '', d.rating ? `★ ${Number(d.rating).toFixed(1)}` : '', d.genre || ''].filter(Boolean);
  top.append(h('div', { class: 'hero-text' }, d.cat ? h('span', { class: 'eyebrow', text: d.cat }) : null,
    h('h2', { id: 'sheet-title', text: d.name }), h('p', { class: 'muted', text: meta.join('  ·  ') })));
  return top;
}

async function movieDetails(id) {
  const d = await api(`/movie/${id}`);
  if (parseHash().id !== id) return;
  const meta = { name: d.name, img: d.img, kind: 'movie' };
  const item = { id: d.id, kind: 'movie', name: d.name, img: d.img, sub: [d.year, d.genre].filter(Boolean).join(' · ') };
  const p = store.progress.get(id);
  const actions = h('div', { class: 'actions' });
  const play = (resume) => { closeSheet(true); playVod(item, resume); };
  if (p && !p.done && p.t > 15) {
    actions.append(h('button', { class: 'btn primary', type: 'button', onclick: () => play(p.t) }, icon('play'), `Resume from ${fmtTime(p.t)}`),
      h('button', { class: 'btn', type: 'button', onclick: () => play(0), text: 'Start over' }));
  } else {
    actions.append(h('button', { class: 'btn primary', type: 'button', onclick: () => play(0) }, icon('play'), p && p.done ? 'Play again' : 'Play'));
  }
  actions.append(favButton(id, meta));
  const watched = h('button', { class: 'btn ghost small', type: 'button', text: p && p.done ? 'Mark as unwatched' : 'Mark as watched' });
  watched.addEventListener('click', () => {
    if (p && p.done) store.progress.clear(id); else store.progress.markDone(id, meta);
    movieDetails(id);
    refreshProgressBadges();
  });
  actions.append(watched);
  const content = h('div', { class: 'details' }, hero(d), p && !p.done && p.d ? progressBar(p.t / p.d) : null, actions,
    d.plot ? h('p', { class: 'plot', text: d.plot }) : null,
    d.cast ? h('p', { class: 'muted small', text: `Cast: ${d.cast}` }) : null,
    d.director ? h('p', { class: 'muted small', text: `Director: ${d.director}` }) : null);
  openSheet(content, d.name);
}

async function getSeries(id) {
  const c = state.seriesCache.get(id);
  if (c && Date.now() - c.at < 10 * 60000) return c.d;
  const d = await api(`/series/${id}`);
  state.seriesCache.set(id, { d, at: Date.now() });
  return d;
}

function episodeItem(d, season, ep) {
  return { id: ep.id, kind: 'episode', name: ep.name, img: ep.img || d.img, series: d.id, seriesName: d.name,
    season: season.n, episode: ep.episode, sub: `${d.name} · S${season.n} E${ep.episode}` };
}

function flatEpisodes(d) {
  const out = [];
  for (const s of d.seasons) for (const ep of s.episodes) out.push({ s, ep });
  return out;
}

function nextUpFor(d) {
  const flat = flatEpisodes(d);
  if (!flat.length) return null;
  let best = null;
  for (let i = 0; i < flat.length; i++) {
    const p = store.progress.get(flat[i].ep.id);
    if (p && (!best || p.at > best.p.at)) best = { i, p };
  }
  if (!best) return { ...flat[0], resume: 0, label: `Play S${flat[0].s.n} E${flat[0].ep.episode}` };
  if (!best.p.done) return { ...flat[best.i], resume: best.p.t, label: `Resume S${flat[best.i].s.n} E${flat[best.i].ep.episode}` };
  const n = flat[best.i + 1];
  return n ? { ...n, resume: 0, label: `Next: S${n.s.n} E${n.ep.episode}` } : null;
}

async function nextEpisode(item) {
  if (!item.series) return null;
  const d = await getSeries(item.series);
  const flat = flatEpisodes(d);
  const i = flat.findIndex((x) => x.ep.id === item.id);
  const n = i >= 0 ? flat[i + 1] : null;
  return n ? episodeItem(d, n.s, n.ep) : null;
}

async function seriesDetails(id) {
  const d = await getSeries(id);
  if (parseHash().id !== id) return;
  const meta = { name: d.name, img: d.img, kind: 'series' };
  const actions = h('div', { class: 'actions' });
  const up = nextUpFor(d);
  if (up) {
    actions.append(h('button', { class: 'btn primary', type: 'button', onclick: () => { closeSheet(true); playVod(episodeItem(d, up.s, up.ep), up.resume); } },
      icon('play'), up.label));
  }
  actions.append(favButton(id, meta));
  const tabs = h('div', { class: 'chips seasons', role: 'tablist', 'aria-label': 'Seasons' });
  const eps = h('ol', { class: 'episodes' });
  const total = d.seasons.reduce((n, s) => n + s.episodes.length, 0);
  const epQ = total > 12 ? h('input', { class: 'input ep-filter', type: 'search', placeholder: `Filter ${total} episodes`,
    'aria-label': 'Filter episodes', enterkeyhint: 'search', autocomplete: 'off' }) : null;
  let season = null;
  // withSeason: rows from several seasons (filter results) say which season they are from
  const epRow = (s, ep, withSeason) => {
    const p = store.progress.get(ep.id);
    const it = episodeItem(d, s, ep);
    const b = h('button', { class: 'ep', type: 'button', 'aria-label': `Season ${s.n}, episode ${ep.episode}: ${ep.name}` },
      thumb(ep.img || d.img, ep.name, 'still'),
      h('span', { class: 'ep-text' },
        h('span', { class: 'ep-title', text: withSeason ? `S${s.n} E${ep.episode} · ${ep.name}` : `${ep.episode}. ${ep.name}` }),
        h('span', { class: 'muted small', text: [ep.dur ? fmtDuration(ep.dur) : '', p && !p.done && p.t > 15 ? `${fmtTime(p.t)} watched` : ''].filter(Boolean).join('  ·  ') }),
        ep.plot ? h('span', { class: 'ep-plot', text: ep.plot }) : null,
        p && !p.done && p.d ? progressBar(p.t / p.d, 'thin') : null),
      p && p.done ? h('span', { class: 'watched inline', 'aria-label': 'Watched' }, icon('check')) : null);
    b.addEventListener('click', () => { closeSheet(true); playVod(it, p && !p.done ? p.t : 0); });
    return h('li', {}, b);
  };
  const showSeason = (s) => {
    season = s;
    if (epQ) epQ.value = '';
    for (const t of tabs.children) t.setAttribute('aria-selected', t.dataset.n === String(s.n) ? 'true' : 'false');
    store.prefs.set(`season.${id}`, s.n);
    eps.replaceChildren(...s.episodes.map((ep) => epRow(s, ep, false)));
  };
  if (epQ) {
    const keyed = d.seasons.flatMap((s) => s.episodes.map((ep) => ({ s, ep, key: fold(`${ep.name} ${ep.plot || ''}`) })));
    let timer = 0;
    epQ.addEventListener('input', () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        const terms = fold(epQ.value).split(/\s+/).filter(Boolean);
        if (!terms.length) { if (season) showSeason(season); return; }
        for (const t of tabs.children) t.setAttribute('aria-selected', 'false');
        const hits = keyed.filter(({ s, ep, key }) => terms.every((term) => key.includes(term)
          || term === String(ep.episode) || term === `s${s.n}e${ep.episode}`));
        eps.replaceChildren(...(hits.length ? hits.map(({ s, ep }) => epRow(s, ep, true))
          : [h('li', {}, emptyBox('No matches', `No episode matches “${epQ.value.trim()}”.`))]));
      }, 120);
    });
  }
  for (const s of d.seasons) {
    const t = h('button', { class: 'chip', type: 'button', role: 'tab', dataset: { n: String(s.n) }, text: `Season ${s.n}` });
    t.addEventListener('click', () => showSeason(s));
    tabs.append(t);
  }
  const content = h('div', { class: 'details' }, hero(d), actions, d.plot ? h('p', { class: 'plot', text: d.plot }) : null,
    d.cast ? h('p', { class: 'muted small', text: `Cast: ${d.cast}` }) : null,
    d.seasons.length ? tabs : emptyBox('No episodes', 'The provider did not list any episodes for this series.'), epQ, eps);
  openSheet(content, d.name);
  if (d.seasons.length) {
    const want = up ? up.s.n : store.prefs.get(`season.${id}`, d.seasons[0].n);
    showSeason(d.seasons.find((s) => s.n === want) || d.seasons[0]);
  }
}

// ------------------------------------------------------------------ SEARCH
async function viewSearch() {
  const input = h('input', { class: 'input big', type: 'search', placeholder: 'Search channels, movies and series', 'aria-label': 'Search', enterkeyhint: 'search', autocomplete: 'off', spellcheck: 'false' });
  const status = h('p', { class: 'muted small', 'aria-live': 'polite' });
  const results = h('div', { class: 'results' });
  view.replaceChildren(viewHead('Search'), input, status, results);
  input.value = store.prefs.get('lastSearch', '');
  setTimeout(() => input.focus(), 50);
  status.textContent = 'Loading catalogue…';
  const loaded = {};
  const kinds = ['live', 'movies', 'series'];
  await Promise.all(kinds.map((k) => catalogue(k).then((l) => { loaded[k] = l; }).catch(() => {})));
  if (state.section !== 'search') return;
  status.textContent = '';
  let timer = 0;
  state.cleanup.push(() => clearTimeout(timer));
  const run = () => {
    const q = input.value.trim();
    store.prefs.set('lastSearch', q);
    if (!q) { results.replaceChildren(emptyBox('Type to search', 'Search runs instantly on this device - nothing is sent to the provider.')); return; }
    const sections = [];
    let total = 0;
    const hiddenCh = store.hidden.channels();
    const anyHidden = hiddenCh.size > 0 || kinds.some((k) => store.hidden.cats(STORE_KIND[k]).size > 0);
    if (loaded.live) {
      const hc = store.hidden.cats('live');
      const r = search(loaded.live, q, 40, (i) => !hiddenCh.has(i.id) && !hc.has(i.cat));
      total += r.length;
      if (r.length) {
        const ul = h('ul', { class: 'ch-list compact', role: 'list' }, r.map((ch) => channelRow(ch)));
        sections.push(h('section', {}, h('h2', { class: 'section-title', text: `Channels (${r.length})` }), ul));
      }
    }
    for (const k of ['movies', 'series']) {
      if (!loaded[k]) continue;
      const hc = store.hidden.cats(STORE_KIND[k]);
      const pref = langPref();
      let r = search(loaded[k], q, pref === 'all' ? 48 : 96, hc.size ? (i) => !hc.has(i.cat) : null);
      if (pref !== 'all') r = lang.rankByLanguage(lang.applyLangPref(r, pref, vinfo), vinfo).slice(0, 48);
      total += r.length;
      if (r.length) sections.push(h('section', {}, h('h2', { class: 'section-title', text: `${k === 'movies' ? 'Movies' : 'Series'} (${r.length})` }), h('div', { class: 'grid' }, r.map((it) => cardFor(k, it)))));
    }
    if (anyHidden) sections.push(h('p', { class: 'muted small', text: 'Hidden channels and categories are left out.' }));
    results.replaceChildren(...(total ? sections : [emptyBox('No matches', `Nothing matches “${q}”.`, anyHidden ? h('p', { class: 'muted small', text: 'Hidden channels and categories are left out.' }) : null)]));
  };
  input.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(run, 60); });
  run();
}

// ------------------------------------------------------------------ LIBRARY (Continue / Favourites / Recent)
function libCard(entry, onClick, sub) {
  const b = h('button', { class: 'card lib-card', type: 'button', 'aria-label': entry.name });
  const poster = thumb(entry.img, entry.name, entry.kind === 'live' ? 'logo wide' : 'poster');
  if (entry.frac) poster.append(progressBar(entry.frac, 'on-poster'));
  b.append(poster, h('span', { class: 'card-title', text: entry.name }), sub ? h('span', { class: 'card-sub', text: sub }) : null);
  b.addEventListener('click', onClick);
  return b;
}

function openEntry(e, resume) {
  if (e.kind === 'live') { playChannel({ id: e.id, name: e.name, img: e.img, num: e.num }); return; }
  if (e.kind === 'movie') { playVod({ id: e.id, kind: 'movie', name: e.name, img: e.img }, resume); return; }
  if (e.kind === 'series') { location.hash = `#/series/${e.id}`; return; }
  if (e.kind === 'episode') {
    playVod({ id: e.id, kind: 'episode', name: e.name, img: e.img, series: e.series, seriesName: e.seriesName,
      season: e.season, episode: e.episode, sub: e.seriesName ? `${e.seriesName} · S${e.season} E${e.episode}` : '' }, resume);
  }
}

function viewLibrary() {
  const which = state.section;
  const row = () => h('div', { class: 'shelf' });
  const sec = (id, title, content, extra) => h('section', { class: 'lib-section', id: `lib-${id}` },
    h('div', { class: 'section-head' }, h('h2', { class: 'section-title', text: title }), extra || null), content);

  // Continue watching: in-progress items + next-up for finished episodes
  const cont = row();
  const inprog = store.progress.inProgress();
  for (const e of inprog.slice(0, 30)) {
    const sub = e.kind === 'episode' ? `${e.seriesName || ''} · S${e.season} E${e.episode} · ${fmtTime(e.d - e.t)} left` : `${fmtTime(e.d - e.t)} left`;
    cont.append(libCard({ ...e, frac: e.t / e.d }, () => openEntry(e, e.t), sub));
  }
  const lastBySeries = store.progress.lastBySeries();
  const pendingNext = Object.values(lastBySeries).filter((e) => e.done).sort((a, b) => b.at - a.at).slice(0, 6);
  for (const e of pendingNext) {
    getSeries(e.series).then((d) => {
      const up = nextUpFor(d);
      if (!up || up.resume) return;
      const it = episodeItem(d, up.s, up.ep);
      cont.querySelector('.state-box')?.remove();
      cont.append(libCard({ ...it, kind: 'episode' }, () => playVod(it, 0), `Up next · S${up.s.n} E${up.ep.episode}`));
    }).catch(() => {});
  }
  if (!inprog.length) cont.append(emptyBox('Nothing in progress', 'Movies and episodes you start will appear here.'));

  // Favourites by kind
  const favWrap = h('div', {});
  const kinds = [['live', 'Channels'], ['movie', 'Movies'], ['series', 'Series']];
  let anyFav = false;
  for (const [k, label] of kinds) {
    const list = store.favs.list(k);
    if (!list.length) continue;
    anyFav = true;
    favWrap.append(h('h3', { class: 'sub-title', text: label }),
      h('div', { class: 'shelf' }, list.map((e) => libCard({ ...e, kind: k }, () => openEntry({ ...e, kind: k }), k === 'live' && e.num ? `Channel ${e.num}` : ''))));
  }
  if (!anyFav) favWrap.append(emptyBox('No favourites yet', 'Use the star on any channel, movie or series.'));

  // Recent
  const rec = store.recent.list();
  const recRow = row();
  for (const e of rec) {
    const sub = e.kind === 'live' ? 'Live' : e.kind === 'episode' ? `${e.seriesName || 'Episode'} · S${e.season} E${e.episode}` : 'Movie';
    const p = e.kind !== 'live' ? store.progress.get(e.id) : null;
    recRow.append(libCard({ ...e, frac: p && !p.done ? p.t / p.d : 0 }, () => openEntry(e, p && !p.done ? p.t : 0), sub));
  }
  if (!rec.length) recRow.append(emptyBox('Nothing yet', 'Channels and titles you play will appear here.'));
  const clearBtn = rec.length ? h('button', { class: 'btn ghost small', type: 'button', text: 'Clear', onclick: () => { store.recent.clear(); render(true); } }) : null;

  view.replaceChildren(viewHead('Library', settingsLink()),
    sec('continue', 'Continue Watching', cont),
    sec('favourites', 'Favourites', favWrap),
    sec('recent', 'Recent', recRow, clearBtn));
  if (which !== 'library') {
    const target = $(`lib-${which}`);
    if (target) requestAnimationFrame(() => target.scrollIntoView({ block: 'start' }));
  }
}

// ------------------------------------------------------------------ SETTINGS
function settingsLink() {
  return h('a', { class: 'btn ghost small settings-link', href: '#/settings', 'aria-label': 'Settings' }, icon('settings'), h('span', { text: 'Settings' }));
}

function viewSettings() {
  const langBox = h('section', { class: 'lib-section status-card', id: 'set-language' });
  const tracksBox = h('section', { class: 'lib-section status-card', id: 'lib-tracks' });
  const uiBox = h('section', { class: 'lib-section status-card', id: 'lib-interface', hidden: true });
  const backupBox = h('section', { class: 'lib-section status-card', id: 'lib-backup' });
  const statusBox = h('section', { class: 'lib-section status-card', id: 'lib-status' });
  view.replaceChildren(viewHead('Settings'), langBox, tracksBox, uiBox, backupBox, statusBox);
  renderCatalogueLang(langBox);
  renderTrackPrefs(tracksBox);
  renderInterface(uiBox);
  renderBackup(backupBox);
  renderStatus(statusBox);
}

// ------------------------------------------------------------------ catalogue language
// Synced with the new interface (the same two preferences): the viewer's language, chosen from the
// ones the provider's catalogue is labelled in, and All / <language> first / <language> only for
// Movies, Series and Search. Rules: lang.js (a copy of the new interface's).
const langWant = () => { const v = store.prefs.get('catLangCode', 'en'); return /^[a-z]{2,3}$/.test(v) ? v : 'en'; };
const langPrefs = () => { const n = lang.langName(langWant()); return [['all', 'All languages'], ['first', `${n} first`], ['only', `${n} only`]]; };
const langPref = () => { const v = store.prefs.get('catLang', 'all'); return ['all', 'first', 'only'].includes(v) ? v : 'all'; };
function vinfo(it) {
  const want = langWant();
  if (!it.vi || it.vi.want !== want) {
    const f = names.film(it.name);
    it.vi = { tier: lang.versionTier(it.name, want), want, key: lang.titleKey(f.title, it.year || f.year) };
  }
  return it.vi;
}
function langItems(list, sort, pref) {
  if (pref === 'all') return sortedItems(list, sort);
  const k = `${sort}|${pref}|${langWant()}`;
  if (!list.sorted[k]) list.sorted[k] = lang.rankByLanguage(lang.applyLangPref(sortedItems(list, sort), pref, vinfo), vinfo);
  return list.sorted[k];
}

function renderCatalogueLang(box) {
  const want = langWant();
  const sel = selectEl('Your language', [[want, lang.langName(want)]], want, (v) => {
    store.prefs.set('catLangCode', v);
    toast(`Saved - Movies, Series and Search can now put ${lang.langName(v)} first`);
    renderCatalogueLang(box);
  });
  const modeSel = selectEl('Movies and series', langPrefs(), langPref(), (v) => { store.prefs.set('catLang', v); toast('Saved'); });
  const note = h('p', { class: 'muted small', text: 'Loading the languages your provider offers…' });
  Promise.all(['movies', 'series'].map((k) => catalogue(k).catch(() => null))).then((lists) => {
    if (!sel.isConnected) return;
    const have = lang.catalogueLangs(lists.filter(Boolean), 100);
    if (!have.some(([c]) => c === 'en')) have.push(['en', 0]);
    if (!have.some(([c]) => c === want)) have.push([want, 0]);
    const opts = have.map(([c, n]) => [c, `${lang.langName(c)}${n ? ` · ${n.toLocaleString()} titles` : ''}`])
      .sort((a, b) => (a[0] === 'en' ? -1 : b[0] === 'en' ? 1 : a[1].localeCompare(b[1])));
    sel.replaceChildren(...opts.map(([v, t]) => h('option', { value: v, text: t })));
    sel.value = langWant();
    note.textContent = `Languages your provider’s catalogue is labelled in (${have.length}). `
      + 'Labels (EN, DE, “dubbed”…) are a good guide, not a guarantee; unlabelled titles always stay.';
  });
  const row = (label, control) => h('label', { class: 'pref-row' }, h('span', { class: 'pref-label', text: label }), control);
  box.replaceChildren(h('h2', { class: 'section-title', text: 'Language' }),
    row('Your language', sel),
    row('Movies & series', modeSel),
    note,
    h('p', { class: 'muted small', text: '“First” lists titles in your language before the rest; “only” also hides versions dubbed into another language. Shared with all your devices and both interfaces.' }));
}

// ------------------------------------------------------------------ interface
// The Pi offers this interface and a newer one; each device chooses (the tv_ui cookie, read by the
// app when it serves /tv/), the rest get the Pi's default (`iptv ui new|classic`).
function uiChoice() {
  const m = document.cookie.match(/(?:^|;\s*)tv_ui=(new|classic)/);
  return m ? m[1] : '';
}
function setUi(v) {
  const tail = `; Path=/tv/; SameSite=Lax${location.protocol === 'https:' ? '; Secure' : ''}`;
  document.cookie = v ? `tv_ui=${v}; Max-Age=34560000${tail}` : `tv_ui=; Max-Age=0${tail}`;
  location.reload();
}
function renderInterface(box) {
  const draw = () => {
    const ui = state.status && state.status.ui;
    if (!ui || !ui.new) { box.hidden = true; return; }
    const name = (k) => (k === 'new' ? 'New' : 'Classic');
    const sel = selectEl('Interface', [['', `Automatic (${name(ui.default)})`], ['new', 'New'], ['classic', 'Classic (this one)']], uiChoice(), setUi);
    box.replaceChildren(h('h2', { class: 'section-title', text: 'Interface' }),
      h('label', { class: 'pref-row' }, h('span', { text: 'On this device' }), sel),
      h('p', { class: 'muted small', text: 'Both interfaces share your favourites, progress and settings. Automatic follows the Pi’s choice.' }));
    box.hidden = false;
  };
  draw();
  loadStatus().then(draw);
}

// ------------------------------------------------------------------ subtitles & audio
// Synced preferences (every device follows them); the player's CC / audio menus override them
// per film or series. Rules: web/static/js/tracks.js and tvapp/tracks.py.
function renderTrackPrefs(box) {
  const get = (k) => store.prefs.get(k, tracks.DEFAULTS[k]);
  const set = (k, v) => { store.prefs.set(k, v); toast('Saved - applies from the next title you play'); };
  const langInput = (key, placeholder, label) => {
    const inp = h('input', { class: 'input', type: 'text', value: get(key), placeholder, 'aria-label': label, autocomplete: 'off', spellcheck: 'false' });
    inp.addEventListener('change', () => {
      const v = tracks.langList(inp.value).join(', ');
      inp.value = v;
      set(key, v);
    });
    return inp;
  };
  const ad = h('input', { type: 'checkbox', checked: !!get('audioAd') });
  ad.addEventListener('change', () => set('audioAd', ad.checked));
  const row = (label, control) => h('label', { class: 'pref-row' }, h('span', { class: 'pref-label', text: label }), control);
  box.replaceChildren(
    h('h2', { class: 'section-title', text: 'Subtitles & audio' }),
    row('Subtitles', selectEl('Subtitles', tracks.SUB_MODES, get('subMode'), (v) => set('subMode', v))),
    row('Subtitle languages', langInput('subLangs', 'en, fr', 'Subtitle languages')),
    row('Hearing-impaired (SDH)', selectEl('SDH subtitles', tracks.SDH_MODES, get('subSdh'), (v) => set('subSdh', v))),
    row('Audio languages', langInput('audioLangs', 'original', 'Audio languages')),
    h('label', { class: 'pref-check' }, ad, h('span', { text: 'Prefer audio description' })),
    h('p', { class: 'muted small', text: 'Languages are two-letter codes in order of preference, e.g. "en, fr". Leave audio empty to hear each title in its original language. A choice made in the player (the CC and audio buttons) is remembered for that film or series.' }));
}

// ------------------------------------------------------------------ backup & sync
function syncLine() {
  const s = store.syncStatus;
  if (s.ok === null) return 'Connecting to the Pi…';
  if (s.ok) return `Synced with the Pi ${ago(s.at / 1000)}.`;
  return `Not synced (${s.error})${s.pending ? ` - ${s.pending} change${s.pending === 1 ? '' : 's'} waiting on this device` : ''}. It will retry automatically.`;
}

function renderBackup(box) {
  const line = h('p', { class: store.syncStatus.ok === false ? 'warn small' : 'muted small', text: syncLine() });
  const file = h('input', { type: 'file', accept: '.json,application/json', hidden: true });
  let mode = 'merge';
  const pick = (m) => { mode = m; file.value = ''; file.click(); };
  file.addEventListener('change', async () => {
    const f = file.files && file.files[0];
    if (!f) return;
    if (mode === 'replace' && !confirm('Replace everything saved on the Pi (favourites, progress, history, preferences) with this file? Every device will follow.')) return;
    try {
      const r = await store.importState(await f.text(), mode);
      toast(mode === 'replace' ? `Restored ${r.applied} items` : `Imported: ${r.applied} new or newer item${r.applied === 1 ? '' : 's'}`);
      render(true);
    } catch (e) { toast(e.message); }
  });
  const exportBtn = h('button', { class: 'btn small', type: 'button' }, icon('download'), 'Export backup');
  exportBtn.addEventListener('click', async () => {
    exportBtn.disabled = true;
    await store.sync();                      // include changes made here a moment ago
    const a = h('a', { href: store.EXPORT_URL, download: '' });
    document.body.append(a);
    a.click();
    a.remove();
    exportBtn.disabled = false;
  });
  box.replaceChildren(
    h('h2', { class: 'section-title', text: 'Sync & backup' }),
    h('p', { class: 'small', text: 'Favourites, progress, recently played, last channel, preferences and hidden channels/categories are saved on the Pi and shared by all your devices.' }),
    line,
    h('div', { class: 'actions' }, exportBtn,
      h('button', { class: 'btn small', type: 'button', text: 'Import (merge)…', onclick: () => pick('merge') }),
      h('button', { class: 'btn ghost small', type: 'button', text: 'Restore (replace)…', onclick: () => pick('replace') }), file),
    h('p', { class: 'muted small', text: 'The backup file is readable JSON with no provider details. Merge keeps whichever copy of each item is newer; replace makes the file the whole saved state.' }));
  if (store.syncStatus.ok === null) store.sync().then(() => { if (box.isConnected) line.textContent = syncLine(); });
}

// ------------------------------------------------------------------ status
async function loadStatus() {
  try { state.status = await api('/status', { timeout: 10000 }); } catch { /* offline */ }
  renderRailStatus();
  return state.status;
}

function ago(ts) {
  if (!ts) return 'never';
  const s = Date.now() / 1000 - ts;
  if (s < 90) return 'just now';
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 129600) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}

function renderRailStatus() {
  const box = $('rail-status');
  const st = state.status;
  if (!st) { box.replaceChildren(); return; }
  const c = st.counts;
  box.replaceChildren(
    h('span', { class: `dot ${st.error ? 'bad' : st.refreshing ? 'busy' : 'ok'}`, 'aria-hidden': 'true' }),
    h('span', { text: st.refreshing ? 'Refreshing…' : st.error ? 'Provider problem' : `${c.live} ch · ${c.movies} films · ${c.series} series` }));
}

function renderStatus(box) {
  const draw = () => {
    const st = state.status;
    if (!st) { box.replaceChildren(h('h2', { class: 'section-title', text: 'Status' }), h('p', { class: 'muted', text: 'Status unavailable.' })); return; }
    const c = st.counts;
    const dl = h('dl', { class: 'kv' },
      h('dt', { text: 'Provider' }), h('dd', { text: `${st.provider === 'xtream' ? 'Xtream Codes' : 'M3U playlist'}${st.configured ? '' : ' (not configured)'}` }),
      h('dt', { text: 'Catalogue' }), h('dd', { text: `${c.live} channels, ${c.movies} movies, ${c.series} series` }),
      h('dt', { text: 'Updated' }), h('dd', { text: st.refreshing ? 'refreshing now…' : ago(st.updated) }),
      h('dt', { text: 'Guide' }), h('dd', { text: st.epg.channels ? `${st.epg.channels} channels, ${ago(st.epg.updated)}` : (st.epg.error || 'none') }),
      h('dt', { text: 'Streams' }), h('dd', { text: `${st.jobs.length} active (limit ${st.limits.live} live / ${st.limits.vod} remux)` }),
      st.refresh && st.refresh.last_ok ? h('dt', { text: 'Last change' }) : null,
      st.refresh && st.refresh.last_ok ? h('dd', { text: `${st.refresh.last_ok.changes} (${ago(st.refresh.last_ok.t)})` }) : null,
      st.health ? h('dt', { text: 'Health' }) : null,
      st.health ? h('dd', { text: st.health.ok ? 'OK' : st.health.problems.join('; ') }) : null,
      st.state ? h('dt', { text: 'Saved' }) : null,
      st.state ? h('dd', { text: `${st.state.favourites} favourites, ${st.state.progress} in progress, ${st.state.recent} recent` }) : null);
    const btn = h('button', { class: 'btn small', type: 'button' }, icon('refresh'), 'Refresh catalogue');
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      try {
        const r = await api('/refresh');
        toast(r.started ? 'Refreshing catalogue…' : r.message);
        if (r.started) pollRefresh(draw);
      } catch (e) { toast(e.message); }
    });
    // replaceChildren() would insert a null as the text "null" (h() skips them; this does not)
    box.replaceChildren(...[h('h2', { class: 'section-title', text: 'Status' }), dl,
      st.error ? h('p', { class: 'warn small', text: st.error }) : null,
      st.epg.error && st.epg.error !== 'no EPG URL configured' ? h('p', { class: 'warn small', text: `Guide: ${st.epg.error}` }) : null,
      btn, h('p', { class: 'muted small', text: 'Provider settings are changed over SSH with the iptv command.' })].filter(Boolean));
  };
  draw();
  loadStatus().then(draw);
}

async function pollRefresh(after) {
  const before = state.status ? state.status.version : '';
  for (let i = 0; i < 90; i++) {
    await new Promise((r) => setTimeout(r, 2000));
    const st = await loadStatus();
    if (after) after();
    if (st && !st.refreshing && !st.epg.refreshing) {
      if (st.version !== before) { dropCatalogue(); toast('Catalogue updated'); }
      return;
    }
  }
}

// ------------------------------------------------------------------ keyboard
document.addEventListener('keydown', (e) => {
  if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey) return;
  const t = e.target;
  const typing = t && (t.tagName === 'INPUT' && !['range', 'button', 'checkbox'].includes(t.type) || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable);
  if (e.key === 'Escape') {
    if (!$('sheet').hidden) { closeSheet(true); e.preventDefault(); return; }
    if (player.mode === 'vod' && !document.fullscreenElement) { player.close(); e.preventDefault(); }
    return;
  }
  if (typing) return;
  const live = player.isLive || (player.mode === 'live' && player.idleChannel);
  const active = player.mode !== 'off';
  switch (e.key) {
    case 'ArrowUp': if (live) { stepChannel(1); e.preventDefault(); } break;
    case 'ArrowDown': if (live) { stepChannel(-1); e.preventDefault(); } break;
    case 'PageUp': if (live) { stepChannel(1); e.preventDefault(); } break;
    case 'PageDown': if (live) { stepChannel(-1); e.preventDefault(); } break;
    case 'ArrowLeft': if (active && !player.isLive && player.item) { player.seekTo(player.absTime() - 10); e.preventDefault(); } break;
    case 'ArrowRight': if (active && !player.isLive && player.item) { player.seekTo(player.absTime() + 30); e.preventDefault(); } break;
    case ' ': case 'k': case 'K':
      if (active && !(t && t.tagName === 'BUTTON')) { player.togglePlay(); e.preventDefault(); }
      break;
    case 'f': case 'F': if (active) { player.toggleFullscreen(); e.preventDefault(); } break;
    case 'm': case 'M': if (active) { player.toggleMute(); e.preventDefault(); } break;
    case 'p': case 'P': if (player.item) { player.togglePip(); e.preventDefault(); } break;
    case 'c': case 'C': if (player.item && !player.isLive) { player.toggleMenu('subs'); e.preventDefault(); } break;
    case 'i': case 'I': if (player.item) { player.showDiag(); e.preventDefault(); } break;
    case 'b': case 'B': case 'Backspace': if (live) { lastChannel(); e.preventDefault(); } break;
    case '/': location.hash = '#/search'; e.preventDefault(); break;
    default:
  }
});

// ------------------------------------------------------------------ sync from other devices
store.onChange((changed) => {
  if (changed.has('fav')) for (const b of document.querySelectorAll('.fav-btn[data-fav-id]')) paintFav(b);
  if (changed.has('progress')) refreshProgressBadges();
  if (state.redraw) state.redraw(changed);
  if (changed.has('last') && state.section === 'live' && !player.item) maybeIdle();
  if (['library', 'continue', 'favourites', 'recent'].includes(state.section) && !player.item && $('sheet').hidden) render(true);
});

// ------------------------------------------------------------------ boot
store.startSync();
window.addEventListener('hashchange', route);
if (!location.hash) history.replaceState(null, '', '#/live');
route();
loadStatus();
setInterval(loadStatus, 120000);
if (navigator.storage && navigator.storage.persist) navigator.storage.persist().catch(() => {});
