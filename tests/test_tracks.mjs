// Subtitle/audio preferences -> play request, per-title memory and the CC menu (web/static/js/tracks.js).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as tracks from '../web/static/js/tracks.js';

const D = tracks.DEFAULTS;

test('defaults: forced subtitles only, English, original audio', () => {
  assert.deepEqual(tracks.playParams(D, '', null), { sub: 'auto', sl: 'en', sdh: 'either', ad: '0' });
});

test('preferences are sent in order', () => {
  const p = tracks.playParams({ ...D, subMode: 'on', subLangs: 'fr, EN ,xx-yy', subSdh: 'prefer', audioLangs: 'de', audioAd: true }, '', null);
  assert.deepEqual(p, { sub: 'on', sl: 'fr,en', sdh: 'prefer', al: 'de', ad: '1' });
});

test('a per-title choice overrides the preferences', () => {
  assert.equal(tracks.playParams(D, 's=off', null).sub, 'off');
  assert.equal(tracks.playParams(D, 's=auto', null).sub, 'auto');
  const p = tracks.playParams({ ...D, subLangs: 'en, fr' }, 's=fr.sdh;a=fr.ad', null);
  assert.deepEqual([p.sub, p.sl, p.sdh, p.al, p.ad], ['on', 'fr,en', 'prefer', 'fr', '1']);
  assert.equal(tracks.playParams(D, 's=en', null).sdh, 'avoid');          // picked the non-SDH one
  assert.equal(tracks.playParams(D, 's=en.forced', null).sub, 'auto');
});

test('an explicit pick in the player wins (and s=-1 means off)', () => {
  const p = tracks.playParams(D, 's=en', { a: 2, s: null });
  assert.equal(p.a, '2');
  assert.equal(p.s, '-1');
  assert.equal(tracks.playParams(D, '', { a: 0, s: 3 }).s, '3');
  assert.equal(tracks.playParams(D, '', { a: 1 }).s, undefined);           // auto: the server chooses
});

test('memory keeps the other half when only one is picked', () => {
  const m1 = tracks.memoryFor({ lang: 'en', sdh: true, forced: false }, null, '');
  assert.equal(m1, 's=en.sdh');
  const m2 = tracks.memoryFor(null, { lang: 'fr', ad: false }, m1);
  assert.equal(m2, 's=en.sdh;a=fr');
  assert.equal(tracks.memoryFor('off', null, m2), 's=off;a=fr');
  assert.equal(tracks.memoryFor('auto', null, 's=off;a=fr.ad'), 's=auto;a=fr.ad');
  assert.equal(tracks.titleKey({ id: 'e_9', series: 's_1' }), 'trk.s_1');   // a series shares one choice
  assert.equal(tracks.titleKey({ id: 'm_5' }), 'trk.m_5');
});

const info = {
  a: 0, s: 2,
  audio: [{ i: 0, label: 'English', lang: 'en' }, { i: 1, label: 'English · Audio Description', lang: 'en', ad: true }],
  subs: [
    { i: 0, label: 'English (Forced)', lang: 'en', forced: true, ok: true },
    { i: 1, label: 'English', lang: 'en', ok: true },
    { i: 2, label: 'English (SDH)', lang: 'en', sdh: true, ok: true },
    { i: 3, label: 'Spanish', lang: 'es', ok: true },
    { i: 4, label: 'Arabic', lang: 'ar', ok: true },
    { i: 5, label: 'French', lang: 'fr', ok: false },
  ],
};

test('CC menu: off, auto, preferred languages, then more languages', () => {
  const m = tracks.subtitleMenu(info, { ...D, subMode: 'on' }, '');
  assert.deepEqual(m.items.map((x) => x.label), ['Off', 'Auto (forced only)', 'English', 'English (SDH)', 'English (Forced)']);
  assert.deepEqual(m.items.filter((x) => x.on).map((x) => x.label), ['English (SDH)']);
  assert.deepEqual(m.more.map((x) => x.label), ['Arabic', 'Spanish', 'French (picture-based, not supported)']);
  assert.equal(m.more[2].disabled, true);
});

test('CC menu marks Auto when forced-only is in effect', () => {
  const m = tracks.subtitleMenu({ ...info, s: 0 }, D, '');
  assert.deepEqual(m.items.filter((x) => x.on).map((x) => x.key), ['auto']);
  const off = tracks.subtitleMenu({ ...info, s: null }, D, 's=off');
  assert.deepEqual(off.items.filter((x) => x.on).map((x) => x.key), ['off']);
});

test('CC menu says when there is nothing to show', () => {
  assert.match(tracks.subtitleMenu({ subs: [], s: null }, D, '').empty, /No subtitles available/);
  assert.match(tracks.subtitleMenu({ subs: [{ i: 0, label: 'English', ok: false }], s: null }, D, '').empty, /picture-based/);
});

test('audio menu', () => {
  assert.deepEqual(tracks.audioMenu(info).map((x) => [x.label, x.on]), [['English', true], ['English · Audio Description', false]]);
});
