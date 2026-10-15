// Pure helpers in web/static/js/api.js: category counts, cached sort orders, search with a filter.
//   node --test tests/test_api.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';

const { fold, usedCats, sortedItems, search } = await import('../web/static/js/api.js');

const item = (id, name, cat, extra = {}) => ({ id, name, cat, img: '', key: fold(name), ...extra });
function list(items, cats) {
  const counts = new Map();
  for (const i of items) counts.set(i.cat, (counts.get(i.cat) || 0) + 1);
  return { cats: new Map(cats), items, byId: new Map(items.map((i) => [i.id, i])), counts, sorted: {} };
}

test('fold lowercases and strips accents', () => {
  assert.equal(fold('Café Ünïcode ÅSA'), 'cafe unicode asa');
});

test('usedCats keeps provider order, drops empty categories, adds counts', () => {
  const l = list([item('a', 'A', 'c2'), item('b', 'B', 'c1'), item('c', 'C', 'c2')], [['c1', 'One'], ['c0', 'Empty'], ['c2', 'Two']]);
  assert.deepEqual(usedCats(l), [['c1', 'One', 1], ['c2', 'Two', 2]]);
});

test('sortedItems sorts once per order, never mutates the list, is stable', () => {
  const items = [item('1', 'beta', 'x', { year: 2001, added: 3 }), item('2', 'Alpha', 'x', { year: 2001, added: 1 }),
    item('3', 'gamma', 'x', { year: 1999, added: 2 })];
  const l = list(items, [['x', 'X']]);
  const byName = sortedItems(l, 'name');
  assert.deepEqual(byName.map((i) => i.id), ['2', '1', '3']);
  assert.equal(sortedItems(l, 'name'), byName);                     // cached
  assert.deepEqual(sortedItems(l, 'year').map((i) => i.id), ['1', '2', '3']);   // ties keep catalogue order
  assert.deepEqual(sortedItems(l, 'added').map((i) => i.id), ['1', '3', '2']);
  assert.deepEqual(l.items.map((i) => i.id), ['1', '2', '3']);       // original untouched
  assert.equal(sortedItems(l, 'nonsense'), l.items);
});

test('search ranks prefix matches first and honours the keep filter', () => {
  const l = list([item('1', 'The Sky News', 'news'), item('2', 'Sky Sports', 'sport'), item('3', 'Sky Arts', 'hidden')],
    [['news', 'N'], ['sport', 'S'], ['hidden', 'H']]);
  assert.deepEqual(search(l, 'sky', 10).map((i) => i.id), ['3', '2', '1']);
  assert.deepEqual(search(l, 'sky', 10, (i) => i.cat !== 'hidden').map((i) => i.id), ['2', '1']);
  assert.deepEqual(search(l, '   ', 10), []);
});

test('searchGuide asks the server once per query and device for a minute, never caches "pending"', async () => {
  const { searchGuide } = await import('../web/static/js/api.js');
  const calls = [];
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (url) => {
    calls.push(url);
    const pending = url.includes('pending');
    return { ok: true, status: 200, json: async () => (pending ? { events: [], pending: true } : { events: [{ title: 'Arsenal v Leeds' }] }) };
  };
  try {
    const a = await searchGuide('Arsenal', 'avc.1080.60.s');
    const b = await searchGuide('arsenal ', 'avc.1080.60.s');            // same words: from the cache
    assert.equal(a, b);
    assert.equal(calls.length, 1);
    assert.match(calls[0], /\/tv\/api\/live\/search\?q=Arsenal&caps=avc\.1080\.60\.s$/);
    await searchGuide('Arsenal', 'avc.2160.60.s,hevc10.2160.60.s');      // another device: asked again
    assert.equal(calls.length, 2);
    await searchGuide('pending', '');
    await searchGuide('pending', '');
    assert.equal(calls.length, 4);
  } finally {
    globalThis.fetch = realFetch;
  }
});
