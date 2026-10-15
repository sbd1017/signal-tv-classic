// What this device can decode, for choosing a live channel's version (tvapp/livemeta.py).
//
// MediaCapabilities is asked per codec (H.264, HEVC 8-bit, HEVC 10-bit) down a resolution ladder
// at 50 fps: "s" = smooth, "u" = supported but not smooth. Its answer is not always true - Firefox
// on the Pi says 4K HEVC is smooth and then drops 98% of the frames in software - so real playback
// is watched too (player.js): a format that drops frames badly is recorded as "x" (failing) and the
// server avoids it from then on. Cached per device and browser version; re-checked weekly.
//
// Interlaced H.264 can't be asked about (MediaCapabilities has no field for it), and browsers differ
// (measured for 1.8.0): frame-coded interlace ("avci") decodes wherever H.264 does; field-coded
// ("avcf", e.g. some 1080i25 broadcast feeds) fails in Chrome, Firefox and macOS Safari. So "avcf"
// is reported only from real playback (markWorking / markFailing), and iPhone/iPad start with a
// "supported but unproven" entry, as the iOS Simulator decoded it.
const KEY = 'tv.caps.v2';
const OLD_KEY = 'tv.caps.v1';
const WEEK = 7 * 86400 * 1000;
const CODECS = { avc: 'avc1.640033', hevc: 'hvc1.1.6.L153.B0', hevc10: 'hvc1.2.4.L153.B0' };
const LADDER = [[3840, 2160], [1920, 1080], [1280, 720]];

function load() {
  try { return JSON.parse(localStorage.getItem(KEY) || 'null'); } catch { return null; }
}

function save(v) {
  try { localStorage.setItem(KEY, JSON.stringify(v)); } catch { /* private mode: detect again next time */ }
}

async function probe(native) {
  const out = [];
  const mc = navigator.mediaCapabilities;
  for (const [codec, c] of Object.entries(CODECS)) {
    const type = `video/mp4; codecs="${c}"`;
    const basic = native ? !!document.createElement('video').canPlayType(type)
      : !!(window.MediaSource && MediaSource.isTypeSupported && MediaSource.isTypeSupported(type));
    if (!basic) continue;
    let smooth = 0;
    let supported = 0;
    for (const [w, h] of LADDER) {
      if (!mc || !mc.decodingInfo) { supported = supported || h; continue; }
      try {
        const r = await mc.decodingInfo({ type: native ? 'file' : 'media-source',
          video: { contentType: type, width: w, height: h, framerate: 50, bitrate: h >= 2160 ? 20e6 : 8e6 } });
        if (r.supported && r.smooth && !smooth) smooth = h;
        if (r.supported && !supported) supported = h;
      } catch { /* this size not answerable: try the next */ }
    }
    if (smooth) out.push(`${codec}.${smooth}.60.s`);
    if (supported && supported > smooth) out.push(`${codec}.${supported}.60.u`);
  }
  const appleTouch = /iP(hone|ad|od)/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  if (native && appleTouch) out.push('avcf.1080.30.u');
  return out;
}

// The weekly re-check keeps what real playback taught: failures ("x") and proven formats ("s").
export async function get(native) {
  let c = load();
  if (!c) {                                         // 1.4-1.7 cache: keep its failures
    try { const old = JSON.parse(localStorage.getItem(OLD_KEY) || 'null'); if (old) c = { ua: old.ua, bad: old.bad || [], good: [] }; } catch { /* none */ }
  }
  const ua = navigator.userAgent;
  const learned = (x) => (x && x.ua === ua ? (x.good || []).concat(x.bad || []) : []);
  if (c && c.ua === ua && c.native === native && Date.now() - c.at < WEEK) return c.list.concat(learned(c)).join(',');
  const list = await probe(native).catch(() => []);
  save({ ua, native, at: Date.now(), list, bad: (c && c.ua === ua && c.bad) || [], good: (c && c.ua === ua && c.good) || [] });
  return list.concat(learned(c)).join(',');
}

// Real playback showed this device can't keep up with this format.
export function markFailing(codec, height, fps) {
  const c = load();
  if (!c) return;
  const tok = `${codec}.${height}.${Math.round(fps) || 50}.x`;
  c.bad = Array.from(new Set([...(c.bad || []), tok])).slice(-8);
  c.good = (c.good || []).filter((t) => !t.startsWith(`${codec}.`) || Number(t.split('.')[1]) < height);
  save(c);
}

// Real playback showed this device decodes this format smoothly (used for interlaced H.264,
// which no capability API can describe).
export function markWorking(codec, height, fps) {
  const c = load();
  if (!c) return;
  const tok = `${codec}.${height}.${Math.round(fps) || 50}.s`;
  c.good = Array.from(new Set([...(c.good || []), tok])).slice(-8);
  c.bad = (c.bad || []).filter((t) => !t.startsWith(`${codec}.`) || Number(t.split('.')[1]) > height);
  save(c);
}

export function codecKey(meta) {
  if (!meta) return '';
  if (meta.v === 'h264') return meta.interlaced ? (meta.coding === 'field' ? 'avcf' : 'avci') : 'avc';
  if (meta.v === 'hevc') return meta.bits === 10 ? 'hevc10' : 'hevc';
  return meta.v || '';
}
