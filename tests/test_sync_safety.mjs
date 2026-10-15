// Sync safety: the Pi is the source of truth, and nothing a device does *not* have can
// remove anything from it. Real store.js as several devices against a real server.
//   TV_TEST_PYTHON=<python with requirements.txt> node --test tests/test_sync_safety.mjs
//
// Model: browser-only data -> merged into the Pi once (migration) -> the Pi is canonical ->
// every device syncs with it. Per item the newer change wins; deletions only come from an
// explicit removal (tombstone); exact ties are settled by content, never by arrival order.
import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { copyFileSync } from 'node:fs';
import { join } from 'node:path';
import { device, reload, serverState, setOffline, sleep, startServer, stateDir, stopServer } from './sync_harness.mjs';

before(() => startServer());
after(() => stopServer());

const CH = (n) => `l_${String(n).padStart(16, '0')}`;
const MV = (n) => `m_${String(n).padStart(16, '0')}`;
const ch = (n) => ({ name: `Channel ${n}`, kind: 'live', num: n });
const mv = (n) => ({ name: `Film ${n}`, kind: 'movie' });

// A device that has put one of everything on the Pi; returns it.
async function seededDevice(tag) {
  const a = await device();
  a.favs.toggle(CH(tag), ch(tag));
  a.progress.set(MV(tag), 1800, 6000, mv(tag));
  a.recent.add(MV(tag), mv(tag));
  a.lastChannel.set(CH(tag), ch(tag));
  a.prefs.set('movies.sort', 'rating');
  a.hidden.setCats('live', [[`cat${tag}`, `Category ${tag}`]], true);
  await a.sync();
  assert.equal(a.syncStatus.pending, 0);
  return a;
}

test('a completely new browser receives everything and changes nothing on the Pi', async () => {
  await seededDevice(1);
  const before = await serverState();
  const fresh = await device();                             // empty localStorage
  await fresh.sync();
  assert.equal(fresh.favs.has(CH(1)), true);
  assert.equal(fresh.progress.get(MV(1)).t, 1800);
  assert.equal(fresh.recent.list()[0].id, MV(1));
  assert.equal(fresh.lastChannel.get().id, CH(1));
  assert.equal(fresh.prefs.get('movies.sort', 'added'), 'rating');
  assert.equal(fresh.hidden.cats('live').has('cat1'), true);
  assert.deepEqual(await serverState(), before);            // not a single write
});

test('empty local favourites / progress / history never wipe valid Pi data', async () => {
  const a = await seededDevice(2);
  const before = await serverState();
  // the same browser, with parts of its local copy lost (cleared, corrupted, quota)
  for (const lost of ['tv.v2.rows.fav', 'tv.v2.rows.progress', 'tv.v2.rows.recent']) a.storage.removeItem(lost);
  a.storage.setItem('tv.v2.rows.last', '{corrupt');
  const again = await reload(a);
  await again.sync();
  assert.deepEqual(await serverState(), before);            // the Pi is untouched...
  assert.equal(again.favs.has(CH(2)), true);                // ...and the device got everything back
  assert.equal(again.progress.get(MV(2)).t, 1800);
  assert.equal(again.recent.list().some((e) => e.id === MV(2)), true);
  assert.equal(again.lastChannel.get().id, CH(2));
});

test('a browser with old favourites/progress merges in without undoing newer changes', async () => {
  const a = await device();
  a.favs.toggle(CH(30), ch(30));
  a.favs.toggle(CH(31), ch(31));
  a.progress.set(MV(30), 3000, 6000, mv(30));
  await a.sync();
  await sleep(5);
  a.favs.toggle(CH(31), ch(31));                            // removed later, on purpose
  await a.sync();
  const legacy = {                                          // Firefox before the update: older data
    'tv.v1.favs': JSON.stringify({ [CH(30)]: { ...ch(30), at: 1000 }, [CH(31)]: { ...ch(31), at: 1000 },
      [CH(32)]: { ...ch(32), at: 1000 } }),
    'tv.v1.progress': JSON.stringify({ [MV(30)]: { ...mv(30), t: 60, d: 6000, done: false, at: 1000 } }),
  };
  const old = await device(legacy);
  await old.sync();
  const s = await serverState();
  assert.equal(s.rows[`fav|${CH(31)}`].deleted, true);     // the later removal still stands
  assert.equal(s.rows[`fav|${CH(32)}`].deleted, false);    // only-in-Firefox favourite was added
  assert.equal(s.rows[`progress|${MV(30)}`].value.t, 3000); // newer progress kept
  assert.equal(old.favs.has(CH(31)), false);
  assert.equal(old.progress.get(MV(30)).t, 3000);
  assert.equal(old.storage.getItem('tv.v1.favs'), legacy['tv.v1.favs']);   // old keys never touched
});

test('migration is only marked done once the Pi has accepted it, and survives reloads offline', async () => {
  const legacy = { 'tv.v1.favs': JSON.stringify({ [CH(40)]: { ...ch(40), at: 2000 } }),
    'tv.v1.pref.liveMode': JSON.stringify('fav') };
  setOffline(true);
  const ff = await device(legacy);
  await ff.sync();                                          // Pi unreachable
  assert.equal(ff.storage.getItem('tv.v2.migrated'), null);
  assert.equal(ff.favs.has(CH(40)), true);                  // usable meanwhile
  ff.favs.toggle(CH(41), ch(41));                           // and changed meanwhile
  const ff2 = await reload(ff);                             // reloaded while still offline
  await ff2.sync();
  setOffline(false);
  const ff3 = await reload(ff2);
  await ff3.sync();
  assert.equal(ff3.storage.getItem('tv.v2.migrated'), 'true');
  const s = await serverState();
  assert.equal(s.rows[`fav|${CH(40)}`].deleted, false);
  assert.equal(s.rows[`fav|${CH(41)}`].deleted, false);
  assert.equal(s.rows['pref|liveMode'].value, 'fav');
  const ff4 = await reload(ff3);                            // later reload: migration does not run again
  ff4.favs.toggle(CH(40), ch(40));                          // user removes the migrated favourite
  await ff4.sync();
  const ff5 = await reload(ff4);
  await ff5.sync();
  assert.equal(ff5.favs.has(CH(40)), false);                // not resurrected from tv.v1.*
  assert.equal((await serverState()).rows[`fav|${CH(40)}`].deleted, true);
});

test('a deletion reaches a device that still has the item, and stays deleted', async () => {
  const a = await device();
  const b = await device();
  a.favs.toggle(CH(50), ch(50));
  await a.sync();
  await b.sync();
  assert.equal(b.favs.has(CH(50)), true);
  await sleep(5);
  a.favs.toggle(CH(50), ch(50));
  await a.sync();
  await b.sync();
  assert.equal(b.favs.has(CH(50)), false);
  const b2 = await reload(b);
  await b2.sync();
  assert.equal(b2.favs.has(CH(50)), false);
  assert.equal((await serverState()).rows[`fav|${CH(50)}`].deleted, true);
});

test('two devices changing things at once: both kept, conflicts settled the same either way', async () => {
  const a = await device();
  const b = await device();
  await a.sync();
  await b.sync();
  setOffline(true);
  a.favs.toggle(CH(60), ch(60));                            // different items
  b.favs.toggle(CH(61), ch(61));
  a.progress.set(MV(60), 100, 6000, mv(60));                // same item: B's change is later
  a.progress.set(MV(61), 100, 6000, mv(61));
  await sleep(5);
  b.progress.set(MV(60), 200, 6000, mv(60));
  b.progress.set(MV(61), 200, 6000, mv(61));
  setOffline(false);
  await a.sync();                                           // A first, then B
  await b.sync();
  await a.sync();
  const s = await serverState();
  assert.equal(s.rows[`progress|${MV(60)}`].value.t, 200);
  assert.equal(s.rows[`progress|${MV(61)}`].value.t, 200);
  for (const d of [a, b]) {
    assert.equal(d.favs.has(CH(60)) && d.favs.has(CH(61)), true);
    assert.equal(d.progress.get(MV(60)).t, 200);
  }
  const c = await device();                                 // reverse arrival order on fresh items
  const e = await device();
  await c.sync();
  await e.sync();
  setOffline(true);
  c.progress.set(MV(62), 100, 6000, mv(62));
  await sleep(5);
  e.progress.set(MV(62), 200, 6000, mv(62));
  setOffline(false);
  await e.sync();                                           // the newer one arrives first this time
  await c.sync();
  await e.sync();
  assert.equal((await serverState()).rows[`progress|${MV(62)}`].value.t, 200);
  assert.equal(c.progress.get(MV(62)).t, 200);
});

test('a device back from offline: its newer changes win, its stale ones do not', async () => {
  const a = await device();
  const b = await device();
  a.progress.set(MV(70), 500, 6000, mv(70));
  a.progress.set(MV(71), 500, 6000, mv(71));
  await a.sync();
  await b.sync();
  setOffline(true);
  b.progress.set(MV(70), 900, 6000, mv(70));                // B (offline) moves on with 70...
  await b.sync();
  setOffline(false);
  await sleep(5);
  a.progress.set(MV(71), 4000, 6000, mv(71));               // ...while A moves on with 71
  await a.sync();
  const b2 = await reload(b);                               // B comes back (even after a reload)
  await b2.sync();
  await a.sync();
  for (const d of [a, b2]) {
    assert.equal(d.progress.get(MV(70)).t, 900);
    assert.equal(d.progress.get(MV(71)).t, 4000);
  }
});

test('Pi restored from an older copy of its database: devices put the newer data back', async () => {
  const a = await device();
  a.favs.toggle(CH(80), ch(80));
  a.progress.set(MV(80), 600, 6000, mv(80));
  await a.sync();
  const dir = stateDir;
  await stopServer({ keep: true });                         // take the "backup"
  const snap = join(dir, 'backup-copy.db');
  copyFileSync(join(dir, 'state.db'), snap);
  await startServer({ dir });
  await sleep(5);
  a.favs.toggle(CH(81), ch(81));                            // changes made after the backup
  a.progress.set(MV(80), 4200, 6000, mv(80));
  a.recent.add(MV(80), mv(80));
  await a.sync();
  const newerRev = (await serverState()).rev;
  await stopServer({ keep: true });                         // the Pi is restored from the backup:
  await startServer({ dir, dbFrom: snap });                 // same database id, older revision
  const restored = await serverState();
  assert.ok(restored.rev < newerRev);
  assert.equal(restored.rows[`fav|${CH(81)}`], undefined);
  await a.sync();                                           // A notices and sends everything back
  const s = await serverState();
  assert.equal(s.rows[`fav|${CH(81)}`].deleted, false);
  assert.equal(s.rows[`progress|${MV(80)}`].value.t, 4200);
  assert.equal(a.progress.get(MV(80)).t, 4200);             // never replaced by the older copy
  const fresh = await device();
  await fresh.sync();
  assert.equal(fresh.favs.has(CH(80)) && fresh.favs.has(CH(81)), true);
  assert.equal(fresh.progress.get(MV(80)).t, 4200);
});
