// What a device learns about decoding from real playback (web/static/js/caps.js).
//   node --test tests/test_caps.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';

class MemoryStorage {
  constructor() { this.m = new Map(); }
  getItem(k) { return this.m.has(k) ? this.m.get(k) : null; }
  setItem(k, v) { this.m.set(k, String(v)); }
  removeItem(k) { this.m.delete(k); }
}
globalThis.localStorage = new MemoryStorage();
const caps = await import('../web/static/js/caps.js');

const PAFF = { v: 'h264', h: 1080, fps: 25, interlaced: true, coding: 'field' };

test('interlaced H.264 has its own capability keys', () => {
  assert.equal(caps.codecKey(PAFF), 'avcf');
  assert.equal(caps.codecKey({ ...PAFF, coding: 'frame' }), 'avci');
  assert.equal(caps.codecKey({ ...PAFF, coding: undefined }), 'avci');
  assert.equal(caps.codecKey({ v: 'h264', h: 720, fps: 50 }), 'avc');
  assert.equal(caps.codecKey({ v: 'hevc', bits: 10 }), 'hevc10');
});

test('real playback is remembered: failing, then working, and the old 1.4 cache keeps its failures', async () => {
  const ua = navigator.userAgent;
  localStorage.setItem('tv.caps.v1', JSON.stringify({ ua, native: false, at: Date.now(), list: ['avc.2160.60.s'], bad: ['hevc10.2160.50.x'] }));
  const first = await caps.get(false);                                   // re-probes (no MediaCapabilities in Node)...
  assert.ok(first.split(',').includes('hevc10.2160.50.x'));              // ...but keeps what playback taught
  caps.markFailing('avcf', 1080, 25);
  assert.ok((await caps.get(false)).split(',').includes('avcf.1080.25.x'));
  caps.markWorking('avcf', 1080, 25);                                    // e.g. an iPhone that does decode it
  const now = (await caps.get(false)).split(',');
  assert.ok(now.includes('avcf.1080.25.s'));
  assert.ok(!now.includes('avcf.1080.25.x'));                            // the newer evidence replaces the old
});
