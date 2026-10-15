// Subtitle and audio choices. Pure functions (no DOM, no storage) so tests can run them in Node.
//
// Synced preferences (store.prefs, shared by every device; a new device uses DEFAULTS until its
// first sync):
//   subMode   'off' | 'auto' (forced subtitles only) | 'on'
//   subLangs  'en, fr'   subtitle languages in order of preference
//   subSdh    'prefer' | 'avoid' | 'either'
//   audioLangs ''        audio languages; empty = the title's own default (original) audio
//   audioAd   false      prefer Audio Description tracks
// Per-title memory: pref `trk.<movie id | series id>` = 's=en.sdh;a=fr' (the last pick made in the
// player's menus for that film / series), so the next episode keeps the same languages.
// The server makes the actual choice (tvapp/tracks.py) from what is sent here.

export const DEFAULTS = { subMode: 'auto', subLangs: 'en', subSdh: 'either', audioLangs: '', audioAd: false };
export const SUB_MODES = [['off', 'Off'], ['auto', 'Forced only (foreign dialogue)'], ['on', 'On']];
export const SDH_MODES = [['either', 'No preference'], ['prefer', 'Prefer SDH'], ['avoid', 'Avoid SDH']];

export function titleKey(item) {
  return `trk.${item.series || item.id}`;
}

export function langList(text) {
  return String(text || '').split(',').map((s) => s.trim().toLowerCase()).filter((s) => /^[a-z]{2,3}$/.test(s));
}

// 's=en.sdh;a=fr.ad' -> { s: { lang: 'en', variant: 'sdh' }, a: { lang: 'fr', ad: true } } ; 's=off' / 's=auto' -> { s: 'off' / 'auto' }
export function parseMemory(text) {
  const out = {};
  for (const part of String(text || '').split(';')) {
    const [k, v] = part.split('=');
    if (!v) continue;
    const [lang, variant] = v.split('.');
    if (k === 's') out.s = v === 'off' || v === 'auto' ? v : { lang, variant: variant || '' };
    if (k === 'a') out.a = { lang, ad: variant === 'ad' };
  }
  return out;
}

export function memoryFor(subTrack, audioTrack, prev) {
  const m = parseMemory(prev);
  const parts = [];
  if (subTrack === 'off' || subTrack === 'auto') parts.push(`s=${subTrack}`);
  else if (subTrack) parts.push(`s=${subTrack.lang || 'und'}${subTrack.forced ? '.forced' : subTrack.sdh ? '.sdh' : ''}`);
  else if (m.s) parts.push(typeof m.s === 'string' ? `s=${m.s}` : `s=${m.s.lang}${m.s.variant ? '.' + m.s.variant : ''}`);
  if (audioTrack) parts.push(`a=${audioTrack.lang || 'und'}${audioTrack.ad ? '.ad' : ''}`);
  else if (m.a) parts.push(`a=${m.a.lang}${m.a.ad ? '.ad' : ''}`);
  return parts.join(';');
}

// Query parameters for /api/play: preferences, overridden by the title's memory, overridden by an
// explicit pick (track numbers in the file being played, used when switching inside the player).
export function playParams(prefs, memory, explicit) {
  const p = {
    sub: prefs.subMode, sl: langList(prefs.subLangs).join(','), sdh: prefs.subSdh,
    al: langList(prefs.audioLangs).join(','), ad: prefs.audioAd ? '1' : '0',
  };
  const m = parseMemory(memory);
  if (typeof m.s === 'string') p.sub = m.s;
  else if (m.s) {
    p.sub = m.s.variant === 'forced' ? 'auto' : 'on';
    p.sl = [m.s.lang, ...langList(p.sl).filter((l) => l !== m.s.lang)].join(',');
    if (m.s.variant !== 'forced') p.sdh = m.s.variant === 'sdh' ? 'prefer' : 'avoid';
  }
  if (m.a) {
    p.al = [m.a.lang, ...langList(p.al).filter((l) => l !== m.a.lang)].join(',');
    p.ad = m.a.ad ? '1' : '0';
  }
  if (explicit) {
    if (explicit.a != null) p.a = String(explicit.a);
    if (explicit.s !== undefined) p.s = explicit.s == null ? '-1' : String(explicit.s);
  }
  for (const k of Object.keys(p)) if (p[k] === '' || p[k] == null) delete p[k];
  return p;
}

// What the CC menu lists, given the tracks of the title being played.
export function subtitleMenu(info, prefs, memory) {
  const subs = (info && info.subs) || [];
  const usable = subs.filter((t) => t.ok);
  const current = info ? info.s : null;
  if (!subs.length) return { empty: 'No subtitles available for this title.', items: [], more: [] };
  if (!usable.length) return { empty: 'This title only has picture-based subtitles, which can’t be shown here.', items: [], more: [] };
  const want = langList(prefs.subLangs);
  const auto = parseMemory(memory).s === 'auto' || (!parseMemory(memory).s && prefs.subMode === 'auto');
  const forced = usable.filter((t) => t.forced);
  const isAuto = auto && forced.length > 0 && (current == null || (subs[current] && subs[current].forced));
  const items = [{ key: 'off', label: 'Off', on: current == null && !isAuto }];
  if (forced.length) items.push({ key: 'auto', label: 'Auto (forced only)', on: isAuto, note: 'Only foreign-language dialogue' });
  const preferred = usable.filter((t) => want.includes(t.lang))
    .sort((a, b) => want.indexOf(a.lang) - want.indexOf(b.lang) || !!a.forced - !!b.forced || !!a.sdh - !!b.sdh || a.i - b.i);
  for (const t of preferred) items.push({ key: t.i, label: t.label, on: current === t.i && !isAuto });
  const more = subs.filter((t) => !preferred.includes(t))
    .sort((a, b) => (!!b.ok - !!a.ok) || a.label.localeCompare(b.label) || a.i - b.i)
    .map((t) => ({ key: t.i, label: t.label + (t.ok ? '' : ' (picture-based, not supported)'), on: current === t.i, disabled: !t.ok }));
  return { empty: '', items, more };
}

export function audioMenu(info) {
  const audio = (info && info.audio) || [];
  return audio.map((t) => ({ key: t.i, label: t.label, on: info.a === t.i }));
}
