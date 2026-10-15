// End-to-end sync: the real browser store (web/static/js/store.js) running as several
// "devices" (each with its own localStorage) against the real server (python -m tvapp)
// with a throwaway state directory.
//
//   TV_TEST_PYTHON=/opt/signal-tv-classic/venv/bin/python node --test tests/test_sync.mjs
// (needs Node 18+; any Python with requirements.txt installed)
import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { device, sleep, startServer, stopServer, setOffline } from './sync_harness.mjs';

before(startServer);
after(stopServer);

test('favourites added or removed on one device reach the others', async () => {
  const a = await device();
  const b = await device();
  a.favs.toggle('l_aaaaaaaaaaaaaaaa', { name: 'Sky Sports', kind: 'live', num: 401 });
  await a.sync();
  await b.sync();
  assert.equal(b.favs.has('l_aaaaaaaaaaaaaaaa'), true);
  assert.equal(b.favs.list('live')[0].name, 'Sky Sports');
  b.favs.toggle('l_aaaaaaaaaaaaaaaa', { name: 'Sky Sports', kind: 'live' });   // removed on B
  await b.sync();
  await a.sync();
  assert.equal(a.favs.has('l_aaaaaaaaaaaaaaaa'), false);
});

test('favourite sports teams, competitions and the alert setting reach the other devices', async () => {
  const phone = await device();
  const tv = await device();
  phone.sportsFavs.toggle('team:arsenal', { name: 'Arsenal', kind: 'team', sport: 'football' });
  phone.sportsFavs.toggle('comp:uefa-champions-league', { name: 'UEFA Champions League', kind: 'comp', sport: 'football' });
  phone.prefs.set('sportsAlerts', '15');
  await phone.sync();
  await tv.sync();
  assert.deepEqual(tv.sportsFavs.ids().sort(), ['comp:uefa-champions-league', 'team:arsenal']);
  assert.equal(tv.sportsFavs.list()[0].name, 'Arsenal');
  assert.equal(tv.prefs.get('sportsAlerts', 'off'), '15');
  tv.sportsFavs.toggle('team:arsenal');                                    // removed on the TV
  await tv.sync();
  await phone.sync();
  assert.deepEqual(phone.sportsFavs.ids(), ['comp:uefa-champions-league']);
});

test('last channel follows you between devices', async () => {
  const desktop = await device();
  const phone = await device();
  desktop.lastChannel.set('l_bbc0000000000001', { name: 'BBC One', kind: 'live' });
  desktop.lastChannel.set('l_sky0000000000001', { name: 'Sky Sports', kind: 'live' });
  await desktop.sync();
  await phone.sync();
  assert.equal(phone.lastChannel.get().id, 'l_sky0000000000001');
  assert.equal(phone.lastChannel.previous().id, 'l_bbc0000000000001');
});

test('stale progress from an offline device does not overwrite newer progress', async () => {
  const a = await device();
  const b = await device();
  setOffline(true);
  b.progress.set('m_film000000000001', 300, 6000, { name: 'Film', kind: 'movie' });     // older, unsent
  await b.sync();
  setOffline(false);
  assert.equal(b.syncStatus.ok, false);
  assert.equal(b.syncStatus.pending > 0, true);
  await sleep(5);
  a.progress.set('m_film000000000001', 2400, 6000, { name: 'Film', kind: 'movie' });    // newer
  await a.sync();
  await b.sync();                                                                         // back online
  assert.equal(b.progress.get('m_film000000000001').t, 2400);
  await a.sync();
  assert.equal(a.progress.get('m_film000000000001').t, 2400);
  assert.equal(b.syncStatus.pending, 0);
});

test('a newer change made offline wins once it is sent', async () => {
  const a = await device();
  const b = await device();
  a.progress.set('m_film000000000002', 100, 6000, { name: 'Film 2', kind: 'movie' });
  await a.sync();
  await b.sync();
  setOffline(true);
  await sleep(5);
  b.progress.set('m_film000000000002', 5000, 6000, { name: 'Film 2', kind: 'movie' });
  await b.sync();
  setOffline(false);
  await b.sync();
  await a.sync();
  assert.equal(a.progress.get('m_film000000000002').t, 5000);
});

test('recent list syncs, and clearing it clears it everywhere', async () => {
  const a = await device();
  const b = await device();
  a.recent.add('l_rec0000000000001', { name: 'One', kind: 'live' });
  await sleep(2);
  a.recent.add('m_rec0000000000002', { name: 'Two', kind: 'movie' });
  await a.sync();
  await b.sync();
  assert.deepEqual(b.recent.list().slice(0, 2).map((e) => e.id), ['m_rec0000000000002', 'l_rec0000000000001']);
  b.recent.clear();
  await b.sync();
  await a.sync();
  assert.equal(a.recent.list().length, 0);
});

test('preferences sync, but volume / mute / last search stay on the device', async () => {
  const a = await device();
  const b = await device();
  a.prefs.set('movies.sort', 'rating');
  a.prefs.set('volume', 0.3);
  await a.sync();
  await b.sync();
  assert.equal(b.prefs.get('movies.sort', 'added'), 'rating');
  assert.equal(b.prefs.get('volume', 1), 1);
  assert.equal(a.prefs.get('volume', 1), 0.3);
});

test('hidden categories and channels sync, including un-hiding', async () => {
  const a = await device();
  const b = await device();
  assert.equal(a.hidden.setCats('live', [['12', 'Arabic'], ['13', 'Kids'], ['a b/é', 'Odd id']], true), 3);
  assert.equal(a.hidden.setCats('live', [['12', 'Arabic']], true), 0);              // already hidden
  a.hidden.setCats('movie', [['7', 'ES Films']], true);
  a.hidden.setChannel('l_hide000000000001', { name: 'Shopping', num: 99, kind: 'live' }, true);
  await a.sync();
  await b.sync();
  assert.deepEqual([...b.hidden.cats('live')].sort(), ['12', '13', 'a b/é']);
  assert.deepEqual([...b.hidden.cats('movie')], ['7']);
  assert.equal(b.hidden.cats('series').size, 0);
  assert.deepEqual(b.hidden.channelList().map((c) => [c.id, c.name, c.num]), [['l_hide000000000001', 'Shopping', 99]]);
  b.hidden.setCats('live', [['13', 'Kids']], false);
  b.hidden.setChannel('l_hide000000000001', {}, false);
  await b.sync();
  await a.sync();
  assert.deepEqual([...a.hidden.cats('live')].sort(), ['12', 'a b/é']);
  assert.equal(a.hidden.channels().size, 0);
});

test('remote changes are announced to the UI', async () => {
  const a = await device();
  const b = await device();
  await b.sync();
  const seen = [];
  b.onChange((nss) => seen.push([...nss].sort().join(',')));
  a.favs.toggle('m_note000000000001', { name: 'Noted', kind: 'movie' });
  a.prefs.set('liveMode', 'fav');
  await a.sync();
  await b.sync();
  assert.deepEqual(seen, ['fav,pref']);
  await b.sync();
  assert.deepEqual(seen, ['fav,pref']);                 // nothing new: no event
});

test('browser-only data from before sync is migrated and merged', async () => {
  const a = await device();
  a.prefs.set('series.sort', 'year');                   // already synced elsewhere
  await a.sync();
  const legacy = {
    'tv.v1.favs': JSON.stringify({ l_old0000000000001: { name: 'Old Fav', kind: 'live', num: 7, at: 1000 } }),
    'tv.v1.progress': JSON.stringify({ e_old0000000000001: { name: 'Ep', kind: 'episode', series: 's_old0000000000001', t: 600, d: 2400, done: false, at: 2000 } }),
    'tv.v1.recent': JSON.stringify([{ id: 'l_old0000000000001', name: 'Old Fav', kind: 'live', at: 3000 }]),
    'tv.v1.lastChannel': JSON.stringify({ id: 'l_old0000000000001', name: 'Old Fav', kind: 'live' }),
    'tv.v1.pref.series.sort': JSON.stringify('name'),   // older than the synced value: must not win
    'tv.v1.pref.volume': JSON.stringify(0.5),           // device-local
  };
  const old = await device(legacy);
  assert.equal(old.favs.has('l_old0000000000001'), true); // usable before the first sync
  assert.equal(old.prefs.get('volume', 1), 0.5);
  await old.sync();
  assert.equal(old.prefs.get('series.sort', 'name'), 'year');
  const other = await device();
  await other.sync();
  assert.equal(other.favs.has('l_old0000000000001'), true);
  assert.equal(other.progress.get('e_old0000000000001').t, 600);
  assert.equal(other.prefs.get('series.sort', ''), 'year');
  assert.equal(old.storage.getItem('tv.v1.favs') !== null, true);   // legacy keys left for rollback
});

test('import merges a backup file and replace restores it exactly', async () => {
  const a = await device();
  a.favs.toggle('l_imp0000000000001', { name: 'Keep', kind: 'live' });
  await a.sync();
  const exported = await (await globalThis.fetch(a.EXPORT_URL)).text();
  a.favs.toggle('l_imp0000000000002', { name: 'Added later', kind: 'live' });
  await a.sync();
  const r = await a.importState(exported, 'merge');
  assert.equal(r.applied, 0);                            // nothing in the file is newer
  assert.equal(a.favs.has('l_imp0000000000002'), true);
  await a.importState(exported, 'replace');
  assert.equal(a.favs.has('l_imp0000000000002'), false);
  assert.equal(a.favs.has('l_imp0000000000001'), true);
  await assert.rejects(a.importState('not json'), /not a Signal TV Classic backup/);
});

test('after the Pi loses its database, devices send back what they hold', async () => {
  const a = await device();
  a.favs.toggle('l_surv000000000001', { name: 'Survivor', kind: 'live' });
  a.progress.set('m_surv000000000001', 900, 5400, { name: 'Long film', kind: 'movie' });
  await a.sync();
  await stopServer();                                    // SD card dies; fresh empty database
  await startServer();
  await a.sync();
  const fresh = await device();
  await fresh.sync();
  assert.equal(fresh.favs.has('l_surv000000000001'), true);
  assert.equal(fresh.progress.get('m_surv000000000001').t, 900);
});
