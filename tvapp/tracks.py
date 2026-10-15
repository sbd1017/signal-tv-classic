"""Embedded audio and subtitle tracks: labels and automatic choice.

ffprobe reports each stream's language tag (usually ISO 639-2, e.g. "eng"/"ger"/"deu", sometimes
"en" or "pt-BR"), a free-text title ("English [SDH]", "Brazilian", "Forced", "Audio Description")
and dispositions (default, forced, hearing_impaired, visual_impaired). Providers fill these
in inconsistently, so both are read: the tag says the language, the title adds the variant.

Choice rules (choose_audio / choose_subtitle), all from the viewer's synced preferences:
- Audio: an explicit pick wins; otherwise the first preferred language that exists; ordinary
  audio before Audio Description (unless the viewer prefers AD) and before commentary; then
  the file's default track; then file order. With no preferences this is the file's own
  default non-AD track - i.e. what played before tracks existed.
- Subtitles: an explicit pick (or "off") wins. Mode "off" shows nothing. Mode "auto" shows only
  forced subtitles (the foreign-language parts), and only in the audio's language when that is
  one of the viewer's subtitle languages (forced Polish subtitles for a Polish soundtrack are no
  use to an English reader).
  Mode "on" shows the first preferred language, full subtitles before forced-only, with SDH
  preferred / avoided / either as the viewer chose. A language the viewer did not ask for is
  never switched on automatically. Picture-based subtitles (PGS/DVD/DVB) cannot be converted
  to text without burning them into the video, so they are listed but never chosen.
"""
from __future__ import annotations

import re

TEXT_SUBS = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text", "tx3g"}
IMAGE_SUBS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub", "dvb_teletext"}
SUB_MODES = ("off", "auto", "on")
SDH_MODES = ("prefer", "avoid", "either")

# ISO 639-2 (bibliographic and terminology) and 639-1 codes -> (639-1 code, English name)
_LANGS = {
    "eng": ("en", "English"), "fre": ("fr", "French"), "fra": ("fr", "French"), "ger": ("de", "German"),
    "deu": ("de", "German"), "spa": ("es", "Spanish"), "ita": ("it", "Italian"), "por": ("pt", "Portuguese"),
    "dut": ("nl", "Dutch"), "nld": ("nl", "Dutch"), "swe": ("sv", "Swedish"), "nor": ("no", "Norwegian"),
    "nob": ("no", "Norwegian"), "nno": ("no", "Norwegian"), "dan": ("da", "Danish"), "fin": ("fi", "Finnish"),
    "ice": ("is", "Icelandic"), "isl": ("is", "Icelandic"), "pol": ("pl", "Polish"), "cze": ("cs", "Czech"),
    "ces": ("cs", "Czech"), "slo": ("sk", "Slovak"), "slk": ("sk", "Slovak"), "hun": ("hu", "Hungarian"),
    "rum": ("ro", "Romanian"), "ron": ("ro", "Romanian"), "bul": ("bg", "Bulgarian"), "gre": ("el", "Greek"),
    "ell": ("el", "Greek"), "tur": ("tr", "Turkish"), "rus": ("ru", "Russian"), "ukr": ("uk", "Ukrainian"),
    "bel": ("be", "Belarusian"), "srp": ("sr", "Serbian"), "scc": ("sr", "Serbian"), "hrv": ("hr", "Croatian"),
    "scr": ("hr", "Croatian"), "bos": ("bs", "Bosnian"), "slv": ("sl", "Slovenian"), "mac": ("mk", "Macedonian"),
    "mkd": ("mk", "Macedonian"), "alb": ("sq", "Albanian"), "sqi": ("sq", "Albanian"), "est": ("et", "Estonian"),
    "lav": ("lv", "Latvian"), "lit": ("lt", "Lithuanian"), "ara": ("ar", "Arabic"), "heb": ("he", "Hebrew"),
    "per": ("fa", "Persian"), "fas": ("fa", "Persian"), "urd": ("ur", "Urdu"), "hin": ("hi", "Hindi"),
    "ben": ("bn", "Bengali"), "pan": ("pa", "Punjabi"), "guj": ("gu", "Gujarati"), "tam": ("ta", "Tamil"),
    "tel": ("te", "Telugu"), "kan": ("kn", "Kannada"), "mal": ("ml", "Malayalam"), "mar": ("mr", "Marathi"),
    "chi": ("zh", "Chinese"), "zho": ("zh", "Chinese"), "jpn": ("ja", "Japanese"), "kor": ("ko", "Korean"),
    "tha": ("th", "Thai"), "vie": ("vi", "Vietnamese"), "ind": ("id", "Indonesian"), "may": ("ms", "Malay"),
    "msa": ("ms", "Malay"), "fil": ("fil", "Filipino"), "tgl": ("tl", "Tagalog"), "cat": ("ca", "Catalan"),
    "baq": ("eu", "Basque"), "eus": ("eu", "Basque"), "glg": ("gl", "Galician"), "wel": ("cy", "Welsh"),
    "cym": ("cy", "Welsh"), "gle": ("ga", "Irish"), "gla": ("gd", "Scottish Gaelic"), "afr": ("af", "Afrikaans"),
    "swa": ("sw", "Swahili"), "amh": ("am", "Amharic"), "som": ("so", "Somali"), "kur": ("ku", "Kurdish"),
    "arm": ("hy", "Armenian"), "hye": ("hy", "Armenian"), "geo": ("ka", "Georgian"), "kat": ("ka", "Georgian"),
    "aze": ("az", "Azerbaijani"), "kaz": ("kk", "Kazakh"), "uzb": ("uz", "Uzbek"), "mon": ("mn", "Mongolian"),
    "nep": ("ne", "Nepali"), "sin": ("si", "Sinhala"), "khm": ("km", "Khmer"), "lao": ("lo", "Lao"),
    "bur": ("my", "Burmese"), "mya": ("my", "Burmese"), "lat": ("la", "Latin"), "epo": ("eo", "Esperanto"),
    "yue": ("yue", "Cantonese"), "cmn": ("zh", "Chinese"),
}
_BY_TWO = {two: name for two, name in _LANGS.values()}
_BY_NAME = {name.lower(): two for two, name in _LANGS.values()}
_BY_NAME.update({"brazilian": "pt", "castilian": "es", "latino": "es", "mandarin": "zh", "flemish": "nl",
                 "farsi": "fa", "persian": "fa", "cantonese": "yue", "québécois": "fr", "quebecois": "fr"})

# words in a title -> region/variant label (and the language when the tag is missing)
_REGIONS = (
    (re.compile(r"\bbrazil(ian)?\b|\bpt[-_]?br\b|\bbr\b", re.I), "Brazil", "pt"),
    (re.compile(r"\b(european|portugal)\b.*\bportuguese\b|\bportuguese\b.*\b(european|portugal)\b|\bpt[-_]pt\b", re.I), "Portugal", "pt"),
    (re.compile(r"\blat(in)?[- ]?am(erican?)?\b|\blatino\b|\blatam\b|\bes[-_](419|mx|la)\b|\bmexic", re.I), "Latin America", "es"),
    (re.compile(r"\bcastilian\b|\b(european|spain)\b.*\bspanish\b|\bspanish\b.*\b(european|spain)\b|\bes[-_]es\b", re.I), "Spain", "es"),
    (re.compile(r"\bcanadian\b|\bqu[ée]b[ée]cois\b|\bfr[-_]ca\b", re.I), "Canada", "fr"),
    (re.compile(r"\bparisian\b|\b(european|france)\b.*\bfrench\b|\bfr[-_]fr\b", re.I), "France", "fr"),
    (re.compile(r"\bsimplified\b|\bzh[-_](hans|cn)\b|\bchs\b", re.I), "Simplified", "zh"),
    (re.compile(r"\btraditional\b|\bzh[-_](hant|tw)\b|\bcht\b", re.I), "Traditional", "zh"),
    (re.compile(r"\bhong ?kong\b|\bzh[-_]hk\b", re.I), "Hong Kong", "zh"),
    (re.compile(r"\bflemish\b|\bnl[-_]be\b", re.I), "Belgium", "nl"),
    (re.compile(r"\b(british|uk)\b|\ben[-_]gb\b", re.I), "UK", "en"),
    (re.compile(r"\b(american|us)\b|\ben[-_]us\b", re.I), "US", "en"),
)
_SDH = re.compile(r"\bsdh\b|\bhearing[- ]impaired\b|\bhoh\b|\bdeaf\b|\bcc\b|closed[- ]captions?", re.I)
_HI = re.compile(r"(^|[\s\[(])hi([\s\])]|$)", re.I)
_FORCED = re.compile(r"\bforced\b|\bforeign(?:[- ]parts?| only| dialogue)?\b|\bsigns?\b(?: ?(?:&|and) ?songs?)?|\bnarrative\b", re.I)
_AD = re.compile(r"(?i:audio[- ]?descri|\bdescriptive\b|described video|\bvisual(?:ly)?[- ]impaired\b)"
                 r"|(^|[\s\[(])AD([\s\])]|$)")         # a bare "AD" only in capitals
_COMMENTARY = re.compile(r"\bcommentary\b|\bkommentar\b|\bcommentaire\b", re.I)
_CHANNELS = {1: "Mono", 2: "Stereo", 6: "5.1", 7: "6.1", 8: "7.1"}


def language(tag: str, title: str = "") -> tuple[str, str, str]:
    """(code, English name, region) for a stream. code is ISO 639-1 where one exists, "" if unknown."""
    tag = (tag or "").strip()
    title = title or ""
    code, region = "", ""
    base, _, sub = tag.replace("_", "-").partition("-")
    low = base.lower()
    if low in _LANGS:
        code = _LANGS[low][0]
    elif low in _BY_TWO:
        code = low
    elif low in _BY_NAME:
        code = _BY_NAME[low]
    if sub and len(sub) in (2, 3, 4):
        region = {"br": "Brazil", "pt": "Portugal", "419": "Latin America", "mx": "Latin America", "es": "Spain",
                  "ca": "Canada", "fr": "France", "hans": "Simplified", "hant": "Traditional", "cn": "Simplified",
                  "tw": "Traditional", "hk": "Hong Kong", "gb": "UK", "us": "US", "be": "Belgium"}.get(sub.lower(), "")
    if not code:                                   # tag missing / "und": try the title's words
        for word in re.findall(r"[A-Za-zÀ-ÿ]+", title):
            if word.lower() in _BY_NAME:
                code = _BY_NAME[word.lower()]
                break
    if not region:
        for rx, label, lang in _REGIONS:
            if rx.search(title) and (not code or code == lang):
                region, code = label, code or lang
                break
    name = _BY_TWO.get(code, "")
    if code == "zh" and region in ("Hong Kong",) and re.search(r"\bcantonese\b", title, re.I):
        code, name = "yue", "Cantonese"
    return code, name, region


def _flags(stream: dict) -> tuple[dict, str, str]:
    disp = stream.get("disposition") or {}
    tags = stream.get("tags") or {}
    title = str(tags.get("title") or tags.get("handler_name") or "")
    if title.lower() in ("soundhandler", "subtitlehandler", "videohandler", "core media audio", "core media text"):
        title = ""                                 # MP4 muxer boilerplate, not a real title
    return disp, title, str(tags.get("language") or "")


def describe(streams: list[dict]) -> tuple[list[dict], list[dict]]:
    """ffprobe streams -> (audio tracks, subtitle tracks), each numbered by its position among
    streams of that type (the n in ffmpeg's -map 0:a:n / 0:s:n)."""
    audio, subs = [], []
    for st in streams:
        kind = st.get("codec_type")
        if kind not in ("audio", "subtitle"):
            continue
        disp, title, tag = _flags(st)
        code, name, region = language(tag, title)
        base = name or (title.strip() if title and not (_SDH.search(title) or _FORCED.search(title)) else "") or "Unknown language"
        if region:
            base += f" ({region})"
        if kind == "audio":
            ad = bool(disp.get("visual_impaired")) or bool(_AD.search(title))
            commentary = bool(disp.get("comment")) or bool(_COMMENTARY.search(title))
            ch = int(st.get("channels") or 0)
            label = base + (" · Audio Description" if ad else "") + (" · Commentary" if commentary else "")
            if ch in _CHANNELS and ch != 2:
                label += f" · {_CHANNELS[ch]}"
            audio.append({"i": len(audio), "lang": code, "name": name, "region": region, "label": label,
                          "ad": ad, "commentary": commentary, "def": bool(disp.get("default")), "ch": ch,
                          "codec": st.get("codec_name", ""), "title": title[:80]})
        else:
            codec = st.get("codec_name", "")
            sdh = bool(disp.get("hearing_impaired")) or bool(_SDH.search(title)) or bool(_HI.search(title))
            forced = bool(disp.get("forced")) or bool(_FORCED.search(title))
            label = base + (" (Forced)" if forced else "") + (" (SDH)" if sdh and not forced else "")
            subs.append({"i": len(subs), "lang": code, "name": name, "region": region, "label": label,
                         "sdh": sdh, "forced": forced, "def": bool(disp.get("default")), "codec": codec,
                         "ok": codec in TEXT_SUBS, "title": title[:80]})
    for group in (audio, subs):                    # two tracks both just "Spanish": number them
        seen: dict[str, int] = {}
        for t in group:
            seen[t["label"]] = seen.get(t["label"], 0) + 1
            if seen[t["label"]] > 1:
                t["label"] += f" ({seen[t['label']]})"
    return audio, subs


def parse_langs(value: str) -> list[str]:
    """'en, fr,pt' -> ['en', 'fr', 'pt'] (639-1/2/3 codes or names are accepted; unknown ones dropped)."""
    out = []
    for part in (value or "").split(","):
        code = language(part.strip())[0] if part.strip() else ""
        if code and code not in out:
            out.append(code)
    return out[:8]


def _rank(lang: str, prefs: list[str]) -> int:
    return prefs.index(lang) if lang in prefs else len(prefs)


def choose_audio(audio: list[dict], langs: list[str], prefer_ad: bool = False, explicit: int | None = None) -> int | None:
    if not audio:
        return None
    if explicit is not None and 0 <= explicit < len(audio):
        return explicit
    best = min(audio, key=lambda t: (_rank(t["lang"], langs) if langs else 0,
                                     t["ad"] != prefer_ad, t["commentary"], not t["def"], t["i"]))
    return best["i"]


def choose_subtitle(subs: list[dict], mode: str, langs: list[str], sdh: str = "either",
                    audio_lang: str = "", explicit: int | None = None) -> int | None:
    if explicit is not None:
        return explicit if 0 <= explicit < len(subs) and subs[explicit]["ok"] else None
    usable = [t for t in subs if t["ok"]]
    if mode == "auto":
        forced = [t for t in usable if t["forced"] and t["lang"] in langs and (not audio_lang or t["lang"] == audio_lang)]
        if not forced:
            return None
        return min(forced, key=lambda t: (_rank(t["lang"], langs), t["i"]))["i"]
    if mode != "on" or not langs:
        return None
    cands = [t for t in usable if t["lang"] in langs]
    if not cands:
        return None
    def key(t):
        sdh_cost = {"prefer": not t["sdh"], "avoid": t["sdh"]}.get(sdh, False)
        return (_rank(t["lang"], langs), t["forced"], sdh_cost, not t["def"], t["i"])
    return min(cands, key=key)["i"]
