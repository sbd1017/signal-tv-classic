// Language of what the provider sends, from its own labels only (no network, no guessing beyond
// them). Two separate questions:
//   feedLang(name, category)  - a Live channel's commentary: 'en' | 'xx' (foreign) | '' (unknown)
//   versionLang(name)         - a film/series version: tier 0 English, 1 unknown, 2 foreign
//                               catalogue (likely original audio, local subtitles), 3 non-English audio
// Unknown is never treated as foreign: it stays visible everywhere and ranks between the two.

const fold = (s) => String(s || '').normalize('NFKC').toUpperCase();

// ---------------------------------------------------------------- Live / Sports feeds
// Region as tvapp/livemeta.py name_info() reads it: the name's own prefix ("UK:", "GR:"), except
// packages and quality words, whose region is their category's ("UK| SPORT VIP", "DE| WOW SPORT").
const PREFIX = /^\s*([A-Z0-9+]{2,6})\s*[:|]\s*/;
const QUALITY = new Set(['4K', '8K', 'UHD', 'FHD', 'HD', 'SD', 'RAW', 'HDR']);
// Two-letter codes this provider uses as countries, plus its own region words. Anything else in
// front of a name ("SAT:", "DSTV:", "WOW:", "M+:", "SKYGO:") is a package, as VIP:/NOW: are.
const COUNTRIES = new Set(('UK GB ENG IE US CA AU NZ ZA AFR DE AT CH FR BE NL LU ES PT IT GR CY TR PL CZ SK HU RO BG HR SI SR RS BH BA ' +
  'MK ME AL XK EXYU SE NO DK FI FL IS EE LV LT RU UA BY IL AR KU IR IN PK BD SG MY ID PH TH VN CN HK TW JP KR MX BR LA LAT ' +
  'CO CL PE VE QC MT ASIA MENA').split(' '));
const ENGLISH = new Set(['UK', 'GB', 'ENG', 'IE', 'US', 'CA', 'AU', 'NZ', 'ZA']);
// Countries whose feeds can be in either English or another language: unknown unless the name says.
const MIXED = new Set(['AFR', 'CH', 'BE', 'LU', 'IN', 'SG', 'MY', 'PH', 'MT', 'ASIA', 'MENA', 'HK']);
// Words in a channel name that settle it, whatever the region.
const SAYS_EN = /\b(ENGLISH|ENG)\b|\(EN\)|SUPERSPORT/;
const SAYS_OTHER = /\b(ARABIC|FRANCE|FRENCH|ESPA[NÑ]OL|ESPANA|DEPORTES|TUDN|UNIVISION|TELEMUNDO|HINDI|TAMIL|TELUGU|KANNADA|BANGLA|MARATHI|URDU|TVA|RDS|CANAL\+|DAZN ES|MAGYAR|PORTUGU[EÊ]S)\b/;

const LANG_SUFFIX = /\s(D|DE|DEU|F|FR|FRA|I|IT|ITA|NL|VL)$/;
const QUAL_TAIL = /(\s+(RAW|HD|FHD|UHD|SD|4K|8K|HEVC|50FPS|60FPS|VIP|◉))+\s*$/;

export function feedRegion(name, category) {
  const n = fold(name);
  const c = fold(category);
  const m = n.match(PREFIX);
  let reg = m ? m[1] : '';
  if (!COUNTRIES.has(reg)) {                      // package, quality word or nothing: the category's region
    const cm = c.match(PREFIX);
    reg = cm && COUNTRIES.has(cm[1]) ? cm[1] : '';
  }
  if (reg === 'GB' || reg === 'ENG') reg = 'UK';
  return reg;
}

// -> { lang: 'en' | 'xx' | '', region }
export function feedLang(name, category) {
  const n = fold(name);
  const region = feedRegion(name, category);
  if (SAYS_EN.test(n)) return { lang: 'en', region };
  if (SAYS_OTHER.test(n)) return { lang: 'xx', region };
  // multilingual countries' channels often end in their language: "BLUE SPORT 2 LIVE D", "BLUE ZOOM FR"
  if (MIXED.has(region) && LANG_SUFFIX.test(n.replace(QUAL_TAIL, ''))) return { lang: 'xx', region };
  if (!region || MIXED.has(region)) return { lang: '', region };
  return { lang: ENGLISH.has(region) ? 'en' : 'xx', region };
}

// Order an event's broadcasters for a preference, keeping the server's device-aware quality order
// inside each group: 'en' English first, 'intl' foreign first, anything else unchanged. Unknown
// feeds always sit between the two; a feed this device can't play smoothly (ok === 0) never
// outranks one it can. feeds: [{..., ok, lang}] in server order.
export function orderFeeds(feeds, pref) {
  if (pref !== 'en' && pref !== 'intl') return feeds.slice();
  const want = pref === 'en' ? 'en' : 'xx';
  const other = pref === 'en' ? 'xx' : 'en';
  const rank = (f) => (f.ok === 0 ? 3 : 0) + (f.lang === want ? 0 : f.lang === other ? 2 : 1);
  return feeds.map((f, i) => [rank(f), i, f]).sort((a, b) => a[0] - b[0] || a[1] - b[1]).map((x) => x[2]);
}

// ---------------------------------------------------------------- films / series versions
const FILM_PREFIX = /^([A-Z0-9+&]{1,6}(?:-[A-Z0-9+&]{1,6}){0,3})\s+[-|:]\s+(?=\S)/;
const V_EN = new Set(['EN', 'UK', 'US', 'AF-EN']);
// Foreign-language audio: dubbed catalogues and films made in that language.
const V_DUB = new Set(['DE', 'FR', 'ES', 'IT', 'TR', 'RU', 'PL', 'CZ', 'HU', 'LA', 'QC', 'BR', 'IN', 'IN-EN', 'IN-TL', 'IN-TU', 'IN-MM', 'IN-KN',
  'IN-PI', 'IN-GU', 'IN-KD', 'BN', 'PK', 'AR', 'AR-DUB', 'AR-EG', 'AR-DE', 'AR-TR', 'AR-TR-D', 'AR-TR-S', 'AR-IN-S', 'AR-IN-D', 'AR-AS',
  'AR-AS-S', 'AR-ANM-S', 'AR-KH', 'AR-SHAM', 'AR-MA', 'AR-IRQ', 'AR-KID', 'AR-DOC-D', 'AR-POD', 'SH', 'AJ', 'MT', 'SO-IN', 'SC-D']);
// Foreign catalogues that usually keep the original audio and add local subtitles: English audio is
// possible, so these are only ever hidden when the same title also exists in English or unknown.
const V_SUB = new Set(['AR-CAM', 'GR', 'SC', 'SE', 'DK', 'NO', 'IS', 'FI', 'NL', 'BE-NL', 'AL', 'RO', 'BG', 'EX', 'IL', 'IR', 'PT', 'PH', 'KU', 'KU-S',
  'KU-B', 'AR-SUBS', 'AR-DOC', 'AR-DOC-S', 'HR', 'SR', 'SI']);

// Base language code of a prefix: "4K-FR-HDR" -> "FR", "DE-DO" -> "DE", "4K-EN" -> "EN",
// "EN-TOP" -> "EN" (the provider's chart lists: "EN-TOP - 17. GoodFellas (1990)").
export function versionCode(prefix) {
  let p = String(prefix || '').toUpperCase();
  p = p.replace(/^(4K|8K|UHD)-/, '').replace(/-(HDR|DV|DO|DOLBY)$/, '').replace(/-(HDR|DV|DO)$/, '').replace(/-TOP$/, '');
  return p;
}

// EN is tier 0: the viewer's language (English unless they chose another; see versionTier)
export const TIER = { EN: 0, UNKNOWN: 1, SUB: 2, DUB: 3 };

// The title itself can say: "Peter Pan (GR AUDIO)", "… دوبله" (Persian: dubbed), "(Ger Sub)".
const SAYS_DUB = /\((?!EN\b|ENG\b)[A-Z]{2,3} AUDIO\)|\bDUBBED\b|\bDOBLADO\b|\bDUBLADO\b|\bSYNCHRO\b|دوبله|مدبلج/i;
const SAYS_EN_AUDIO = /\((EN|ENG) AUDIO\)/i;

// The languages a catalogue label stands for: "DE" German, "AR-SUBS" Arabic (subtitles), "SC" any of the
// Nordic ones. Labels that don't name a language (NF, 4K, TOP, AS…) have none.
const CODE_LANGS = {
  EN: 'en', UK: 'en', US: 'en', 'AF-EN': 'en', DE: 'de', FR: 'fr', QC: 'fr', ES: 'es', LA: 'es', IT: 'it', TR: 'tr', RU: 'ru',
  PL: 'pl', CZ: 'cs', HU: 'hu', PT: 'pt', BR: 'pt', GR: 'el', NL: 'nl', 'BE-NL': 'nl', SE: 'sv', DK: 'da', NO: 'no', IS: 'is',
  FI: 'fi', SC: 'sv da no', AL: 'sq', RO: 'ro', BG: 'bg', HR: 'hr', SR: 'sr', SI: 'sl', EX: 'sh', IL: 'he', IR: 'fa',
  KU: 'ku', PH: 'fil', PK: 'ur', BN: 'bn', AR: 'ar', IN: 'hi', 'IN-TL': 'te', 'IN-TU': 'ta', 'IN-MM': 'ml', 'IN-KN': 'kn',
  'IN-PI': 'pa', 'IN-GU': 'gu', 'IN-EN': 'hi', 'SO-IN': '', 'SO-EN': '',     // IN-EN: English films dubbed into Hindi
};
export function codeLangs(code) {
  const c = versionCode(code);
  const v = c in CODE_LANGS ? CODE_LANGS[c] : CODE_LANGS[c.split('-')[0]] || '';
  return v ? v.split(' ') : [];
}

const SAYS_AUDIO = /\(([A-Z]{2,3}) AUDIO\)/i;
// -> the languages a version is labelled with (its "(XX AUDIO)" note, else its catalogue prefix)
export function versionLangs(rawName) {
  const a = String(rawName || '').match(SAYS_AUDIO);
  if (a) return a[1].toUpperCase() === 'ENG' ? ['en'] : codeLangs(a[1].toUpperCase());
  const m = String(rawName || '').match(FILM_PREFIX);
  return m ? codeLangs(m[1]) : [];
}

// A viewer's language other than English ('de', 'el', 'ar'…): the same four tiers, seen from there -
// 0 that language (dubbed or subtitled), 1 unlabelled, 2 English or another subtitled catalogue
// (likely the original audio), 3 another language's dub. For 'en' the rules above, unchanged.
export function versionTier(rawName, want = 'en') {
  if (want && want !== 'en') {
    const langs = versionLangs(rawName);
    if (langs.includes(want)) return TIER.EN;
    const t = englishTier(rawName);
    if (t === TIER.UNKNOWN && !langs.length) return TIER.UNKNOWN;
    return t === TIER.DUB ? TIER.DUB : TIER.SUB;
  }
  return englishTier(rawName);
}

// Languages the provider's catalogue carries, with how many titles: [[code, count], …], largest first.
export function catalogueLangs(lists, min = 1) {
  const n = new Map();
  for (const list of lists) {
    for (const it of list.items) for (const l of versionLangs(it.name)) n.set(l, (n.get(l) || 0) + 1);
  }
  return [...n].filter(([, c]) => c >= min).sort((a, b) => b[1] - a[1]);
}

let names = null;
const OWN_NAMES = { sh: 'Serbo-Croatian (Ex-Yu)' };          // the provider's EX catalogue
export function langName(code) {
  if (OWN_NAMES[code]) return OWN_NAMES[code];
  try { names = names || new Intl.DisplayNames(['en'], { type: 'language' }); return names.of(code) || code; } catch { return code; }
}

function englishTier(rawName) {
  if (SAYS_EN_AUDIO.test(rawName)) return TIER.EN;
  if (SAYS_DUB.test(rawName)) return TIER.DUB;
  const m = String(rawName || '').match(FILM_PREFIX);
  if (!m) return TIER.UNKNOWN;
  const code = versionCode(m[1]);
  if (V_EN.has(code)) return TIER.EN;
  if (V_DUB.has(code)) return TIER.DUB;
  if (V_SUB.has(code)) return TIER.SUB;
  return TIER.UNKNOWN;
}

// Same underlying title for ordering purposes only (never merged): cleaned title + year. Items
// without a year never share a key (WWE weekly shows, events), so they keep their own place.
export function titleKey(clean, year) {
  if (!year) return '';
  const k = String(clean || '').toLowerCase().normalize('NFKD').replace(/[̀-ͯ]/g, '').replace(/[^a-z0-9]+/g, ' ').trim();
  return k ? `${k}|${year}` : '';
}

// Browsing (Films / Series grids) with 'English first' or 'English only': English titles before
// unlabelled ones, then foreign catalogues, then dubbed - each block keeping the viewer's own sort
// order (newest, A-Z…). Without it, a provider's newest additions can be a whole foreign catalogue.
export function rankByLanguage(items, info) {
  return items.map((it, i) => [info(it).tier, i, it]).sort((a, b) => a[0] - b[0] || a[1] - b[1]).map((x) => x[2]);
}

// Apply the catalogue preference to a list already in the order the viewer asked for.
//   'all'   unchanged
//   'first' versions of the same title are gathered at the first one's place, English first
//   'only'  as 'first', then drop non-English-audio versions, and foreign-catalogue versions whose
//           title also exists in English or unknown. Unknown is never dropped.
// info(item) -> { tier, key }. Returns a new array; O(n).
export function applyLangPref(items, pref, info) {
  if (pref !== 'first' && pref !== 'only') return items;
  const groups = new Map();
  const order = [];                               // entries: an item on its own, or a group's member list
  for (const it of items) {
    const { tier, key } = info(it);
    if (!key) { order.push({ it, tier }); continue; }
    let g = groups.get(key);
    if (!g) { g = { members: [] }; groups.set(key, g); order.push(g); }
    g.members.push({ it, tier });
  }
  const out = [];
  for (const e of order) {
    const members = e.members ? e.members.slice().sort((a, b) => a.tier - b.tier) : [e];   // stable sort
    const hasPlain = members.some((m) => m.tier <= TIER.UNKNOWN);
    for (const m of members) {
      if (pref === 'only' && (m.tier === TIER.DUB || (m.tier === TIER.SUB && hasPlain && members.length > 1))) continue;
      out.push(m.it);
    }
  }
  return out;
}
