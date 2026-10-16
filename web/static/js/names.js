// Provider names -> what the interface shows. Providers decorate names with superscript tags
// ("ᴿᴬᵂ ⁶⁰ᶠᵖˢ"), country/package prefixes ("UK: ", "US| ", "YP: "), trailing quality words
// ("… HD") and separator rows ("##### YUPP TV PREMIUM #####"). The raw name is never changed
// anywhere else (search, favourites, sync all keep it); this is presentation only.

// Superscript/modifier letters and digits (their NFKC form is the plain character).
const SUP = '²³¹ʰ-˿ᴬ-ᶿ⁰-₟ⱼⱽꚜꚝꝰꟸꟹ';
const SUP_RUN = new RegExp(`[${SUP}]+(?:[\\s/+|·]*[${SUP}]+)*`, 'gu');
const TRAIL = /\s+(UHD|FHD|HD|SD|4K|8K|HEVC|H265|RAW|HDR|50FPS|60FPS|VIP|LQ|HQ)$/i;
const PREFIX = /^([A-Z0-9&+]{2,6})\s*[:|]\s*(?=\S)/u;
const SEP_EDGE = /^[\s#=*~_\-–—★☆✦•|>.<+]{3,}|[\s#=*~_\-–—★☆✦•|>.<+]{3,}$/gu;

const cache = new Map();

function tagText(t) {
  const s = t.normalize('NFKC').toUpperCase();
  return /^\d+FPS$/.test(s) ? s.replace('FPS', 'fps') : s;
}

// -> { title, prefix, tags: [..], sep: bool }
export function parse(raw) {
  const name = String(raw || '');
  const hit = cache.get(name);
  if (hit) return hit;
  let s = name;
  const tags = [];
  s = s.replace(SUP_RUN, (run) => {
    for (const t of run.split(/[\s/+|·]+/)) if (t) tags.push(tagText(t));
    return ' ';
  });
  s = s.replace(/\s{2,}/g, ' ').trim();
  const sep = /^[\s#=*~_\-–—★☆✦•|>.<+]{3,}/u.test(name) || /^[\s#=*~_\-–—★☆✦•|]+$/u.test(name);
  if (sep) s = s.replace(SEP_EDGE, '').trim();
  // a provider's marker in front of the name, alone or as its "prefix": "◉: beIN Sp⚽rts 1", "● UK: Sky Sports"
  if (!sep) s = s.replace(/^[◉●○◎•★☆✦✪▶►]+\s*:?\s*/u, '') || s;
  let prefix = '';
  const m = !sep && s.match(PREFIX);
  if (m && s.length - m[0].length >= 2) { prefix = m[1]; s = s.slice(m[0].length); }
  for (let i = 0, t; i < 3 && (t = s.match(TRAIL)); i++) { tags.unshift(t[1].toUpperCase().replace('FPS', 'fps')); s = s.slice(0, t.index).trim(); }
  const out = { title: s || name.trim(), prefix, tags: [...new Set(tags)], sep };
  if (cache.size > 20000) cache.clear();
  cache.set(name, out);
  return out;
}

// Category names: "UK| SPORTS ᴴᴰ/ᴿᴬᵂ" -> region "UK", title "SPORTS", tags.
export function category(raw) {
  const p = parse(raw);
  if (!p.prefix) {
    const m = p.title.match(/^([A-Z0-9+&]{1,6}(?:-[A-Z0-9+&]{1,6}){0,2})\s+[-–]\s+(?=\S)/);
    if (m) return { region: m[1], title: p.title.slice(m[0].length), tags: p.tags };
  }
  return { region: p.prefix, title: p.title, tags: p.tags };
}

// When other channels in the list share this one's cleaned name ("BR: GLOBO SP HD" twice), the first
// real detail that tells it apart from all of them: region, quality, both, its category - else the
// provider's own channel number. '' when the name is unique. items: [{id, name, key, cat, num}],
// key = the name folded lower-case (api.js); cats: Map category id -> provider category name.
const foldName = (x) => String(x || '').toLowerCase().normalize('NFKD').replace(/[\u0300-\u036f]/g, '');
export function twinLabel(entry, items, cats = new Map()) {
  const title = foldName(parse(entry.name).title).trim();
  if (!title) return '';
  const group = items.filter((it) => it.key.includes(title) && foldName(parse(it.name).title).trim() === title);
  if (group.filter((it) => it.id !== entry.id).length < 1) return '';
  const facts = (it) => {
    const p = parse(it.name);
    const c = cats.get(it.cat) ? category(cats.get(it.cat)) : null;
    const quality = p.tags.join(' · ');
    return { region: p.prefix, quality, both: [p.prefix, quality].filter(Boolean).join(' · '), cat: c ? c.title : '', num: it.num };
  };
  const me = facts(items.find((it) => it.id === entry.id) || { name: entry.name, cat: '', num: entry.num });
  const others = group.filter((it) => it.id !== entry.id).map(facts);
  for (const f of ['region', 'quality', 'both', 'cat']) {
    if (me[f] && others.every((o) => o[f] !== me[f])) return me[f];
  }
  return me.num ? `Ch ${me.num}` : '';
}

// The name for a compact channel card. Event channels pack the event between provider labels, status
// words, dates and package names ("(Fanatiz 064) | Alianza vs Inter Bogotá (2026-10-24 17:05:00)",
// "End | Palmeiras vs. Corinthians | all | 11-10-2026 | 22:00 (GMT) | 8K EXCLUSIVE | BR: SOCCER PPV 11"):
// keep the first part that is none of those. Other names, including ordinary ones with a bar
// ("FOX 4 LOCAL DALLAS | FORT WORTH"): parse().title.
const EVENT_NOISE = [
  /^(?:\(?[A-Z]{2,3}\)?\s*)?(?:\([^()]*\d[^()]*\)\s*)+$/u,          // (Fanatiz 064) · US (ESPN+ 106) · (US) (BTN+ 029)
  /^(?:end|ended|next|live|now|soon|all)$/i,
  /^\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}$/,                            // 11-10-2026, 2026-10-11
  /^\d{1,2}:\d{2}(?::\d{2})?(?:\s*\([A-Z]{2,5}\))?$/,             // 22:00 (GMT)
  /^\d+K EXCLUSIVE$/i,
  /^[A-Z]{2,3}:\s.*\bPPV\s*\d*$/,                                  // BR: SOCCER PPV 11
];
export function cardTitle(raw) {
  const title = parse(raw).title;
  if (!title.includes(' | ')) return title;
  const parts = title.split(' | ').map((s) => s.replace(/\s*\(\d{4}-\d{2}-\d{2}[ T][\d:]+\)$/, '').trim());
  const noise = (s) => !s || EVENT_NOISE.some((re) => re.test(s));
  if (!parts.some(noise)) return title;
  const part = parts.find((s) => !noise(s) && !/^[A-Z]{2,3}$/.test(s));        // and a bare region ("CA")
  return part ? part.replace(/^[\s\-–—]+|[\s\-–—]+$/g, '') || part : title;   // "- NO EVENT STREAMING -" -> "NO EVENT STREAMING"
}

// An event channel's own status word ("End | …", "NEXT | …", "Live | …"), for the line under its
// card title (cardTitle leaves it out); '' for anything else.
export function eventStatus(raw) {
  const m = String(raw || '').match(/^\s*(end|ended|next|live)\s*\|\s*\S/i);     // parse() reads "NEXT |" as a prefix
  return m ? { end: 'Ended', ended: 'Ended', next: 'Up next', live: 'Live now' }[m[1].toLowerCase()] : '';
}

// "RC Lens vs Sporting CP" -> ["RC Lens", "Sporting CP"]; null when the title is not a pairing.
// A cricket stage in front of the first side is not part of its name: "4th Test England vs India".
const STAGE = /^(?:\d+(?:st|nd|rd|th)\s+(?:test|odi|t20i?|one-day international|match|game)|(?:first|second|third|fourth|fifth)\s+test)\s*[:\-–]?\s+/i;
// A side that is a programme word ("Sunday of Pain - Podsumowanie": Polish for "summary") means a show.
const SHOW_SIDE = /^(podsumowanie|skr[oó]ty?|magazyn|zapowied[zź]|studio|highlights?|preview|review|analysis|reaction|sestřih|sažetak|rezumat|özet|resumen|resumo|zusammenfassung|samenvatting)$/i;
export function teams(title) {
  const m = String(title || '').match(/^(.+?)\s+(?:vs\.?|v\.?|x|-|–)\s+(.+)$/i);
  if (!m || m[1].length > 40 || m[2].length > 40) return null;
  if (SHOW_SIDE.test(m[1].trim()) || SHOW_SIDE.test(m[2].trim())) return null;
  return [m[1].trim().replace(STAGE, ''), m[2].trim()];
}

// Film / series titles: "4K-FR-HDR - La Course des animaux (2026)" -> prefix "4K-FR-HDR",
// title "La Course des animaux", year 2026; "EN - The Day of the Jackal (2024) (GB)" -> region GB.
const FILM_PREFIX = /^([A-Z0-9+&]{1,6}(?:-[A-Z0-9+&]{1,6}){0,3})\s+[-|:]\s+(?=\S)/;
const FILM_TAIL = /\s*\((\d{4}|[A-Z]{2})\)\s*$/;
const films = new Map();
export function film(raw) {
  const name = String(raw || '');
  const hit = films.get(name);
  if (hit) return hit;
  let s = name.trim();
  let prefix = '';
  const m = s.match(FILM_PREFIX);
  if (m && s.length - m[0].length >= 1) { prefix = m[1]; s = s.slice(m[0].length); }
  let year = 0;
  let region = '';
  for (let i = 0, t; i < 3 && (t = s.match(FILM_TAIL)); i++) {
    if (/^\d{4}$/.test(t[1])) year = year || Number(t[1]); else region = region || t[1];
    s = s.slice(0, t.index);
  }
  // "(2020) (ES)" in the middle before a translated title: "Kasaba (2025) (TR) شهر"
  const mid = s.match(/^(.*?\S)\s*\((\d{4})\)(?:\s*\(([A-Z]{2})\))?\s+(\S.*)$/u);
  if (mid && !/[A-Za-z]/.test(mid[4])) { s = mid[1]; year = year || Number(mid[2]); region = region || mid[3] || ''; }
  const out = { title: s.trim() || name, prefix, year, region, uhd: /(^|-)(4K|UHD|8K)(-|$)/.test(prefix), hdr: /(^|-)HDR(-|$)/.test(prefix) };
  if (films.size > 30000) films.clear();
  films.set(name, out);
  return out;
}

// Capitals for display, by the script's own convention (the original spelling stays in the page as
// title / aria-label). CSS text-transform is not reliable enough across browsers for these:
//   Greek: stress marks go in capitals, the diaeresis stays - "Ρεάλ Μαδρίτης" -> "ΡΕΑΛ ΜΑΔΡΙΤΗΣ"
//   Turkish: dotted and dotless i stay distinct - "Beşiktaş" -> "BEŞİKTAŞ"
//   everything else: accents kept - "Saint-Étienne" -> "SAINT-ÉTIENNE", "Śląsk" -> "ŚLĄSK"
// -> { text, lang } (lang: 'el' | 'tr' | 'ru' | '' for the element's lang attribute)
const GREEK = /[Ͱ-Ͽἀ-῿]/;
const TURKISH = /[ğışİĞŞ]/;
const CYRILLIC = /[Ѐ-ӿ]/;
export function caps(text) {
  const s = String(text || '');
  if (GREEK.test(s)) {
    const t = s.normalize('NFD').replace(/[̀́͂̓̔ͅ]/g, '').toLocaleUpperCase('el').normalize('NFC');
    return { text: t, lang: 'el' };
  }
  if (TURKISH.test(s)) return { text: s.toLocaleUpperCase('tr'), lang: 'tr' };
  return { text: s.toLocaleUpperCase('en'), lang: CYRILLIC.test(s) ? 'ru' : '' };
}
