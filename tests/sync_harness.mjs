// Shared by the end-to-end sync tests: a real server (python -m tvapp) on a throwaway state
// directory, and "devices" = separate instances of the real web/static/js/store.js, each
// with its own localStorage. TV_TEST_PYTHON = a Python with requirements.txt installed.
import { spawn } from 'node:child_process';
import { copyFileSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

export const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const PY = process.env.TV_TEST_PYTHON || 'python3';
const PORT = 18000 + Math.floor(Math.random() * 1000);
export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

class MemoryStorage {
  constructor(init = {}) { this.m = new Map(Object.entries(init)); }
  get length() { return this.m.size; }
  key(i) { return [...this.m.keys()][i] ?? null; }
  getItem(k) { return this.m.has(k) ? this.m.get(k) : null; }
  setItem(k, v) { this.m.set(k, String(v)); }
  removeItem(k) { this.m.delete(k); }
}

// ---------------------------------------------------------------- server
let proc = null;
export let stateDir = null;
// dir: reuse a state directory (restart); dbFrom: start on a copy of this database file (restore)
export async function startServer({ dir = null, dbFrom = null } = {}) {
  stateDir = dir || mkdtempSync(join(tmpdir(), 'tv-sync-test-'));
  if (dbFrom) {
    for (const f of ['state.db-wal', 'state.db-shm']) rmSync(join(stateDir, f), { force: true });
    copyFileSync(dbFrom, join(stateDir, 'state.db'));
  }
  proc = spawn(PY, ['-m', 'tvapp'], {
    cwd: ROOT, stdio: ['ignore', 'ignore', 'pipe'],
    env: { ...process.env, TV_PORT: String(PORT), TV_CONFIG: join(stateDir, 'none.env'), STATE_DIRECTORY: stateDir,
      CACHE_DIRECTORY: join(stateDir, 'cache'), TV_LIVE_DIR: join(stateDir, 'live'), TV_WEB_DIR: join(ROOT, 'web') },
  });
  let log = '';
  proc.stderr.on('data', (d) => { log += d; });
  for (let i = 0; i < 300; i++) {                     // up to 30 s: a cold first start can be slow
    try { if ((await realFetch(`http://127.0.0.1:${PORT}/tv/api/state`)).ok) return; } catch { /* starting */ }
    await sleep(100);
  }
  throw new Error(`server did not start:\n${log}`);
}
export async function stopServer({ keep = false } = {}) {
  if (!proc) return;
  proc.kill('SIGTERM');                               // clean close: the WAL is checkpointed into state.db
  await new Promise((r) => proc.once('exit', r));
  proc = null;
  if (!keep) rmSync(stateDir, { recursive: true, force: true });
}

// What the Pi holds: {rev, rows: {"ns|key": {value, deleted, updated}}}
export async function serverState() {
  const s = await (await realFetch(`http://127.0.0.1:${PORT}/tv/api/state?since=0`)).json();
  return { db: s.db, rev: s.rev, rows: Object.fromEntries(s.changes.map((c) => [`${c.ns}|${c.key}`, c])) };
}

// The store calls fetch('/tv/api/...'); point it at the test server. setOffline() simulates no network.
const realFetch = globalThis.fetch;
let offline = false;
export const setOffline = (v) => { offline = v; };
globalThis.fetch = (url, opts) => (offline ? Promise.reject(new TypeError('network down'))
  : realFetch(`http://127.0.0.1:${PORT}${url}`, opts));

// Each device = a fresh instance of store.js bound to its own storage.
let n = 0;
export async function device(init = {}) {
  const storage = new MemoryStorage(init);
  globalThis.localStorage = storage;
  const store = await import(`../web/static/js/store.js?device=${n++}`);
  return { ...store, storage };
}
// The same browser opened again (a page reload): a new store instance on the same storage.
export const reload = (dev) => device(Object.fromEntries(dev.storage.m));
