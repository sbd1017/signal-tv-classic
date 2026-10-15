"""Small shared helpers: stable IDs, metadata sanitising, log redaction, big-JSON helpers."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from urllib.parse import urlsplit

log = logging.getLogger("tv")

# l_ live channel, m_ movie, s_ series, e_ episode; 16 hex chars of sha256
ID_RE = re.compile(r"^[lmse]_[0-9a-f]{16}$")
# control chars, zero-width spaces/marks, bidi embeddings/overrides/isolates
_CTRL = re.compile(r"[\x00-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069]")
_TAGS = re.compile(r"<[^>]{0,200}>")
_SPACES = re.compile(r"\s+")


def make_id(kind: str, *parts: object) -> str:
    h = hashlib.sha256("\x1f".join(str(p) for p in parts).encode("utf-8", "replace")).hexdigest()
    return f"{kind}_{h[:16]}"


def valid_id(value: str, kinds: str = "lmse") -> bool:
    return bool(ID_RE.match(value or "")) and value[0] in kinds


def clean(value: object, limit: int = 200) -> str:
    """Provider metadata -> plain single-line text (the UI also inserts it as text, never HTML)."""
    if value is None:
        return ""
    text = unicodedata.normalize("NFC", str(value))
    text = _TAGS.sub(" ", text)
    text = _CTRL.sub("", text)
    text = _SPACES.sub(" ", text).strip()
    return text[:limit]


def clean_long(value: object, limit: int = 2000) -> str:
    return clean(value, limit)


def http_url(value: str) -> str:
    """Return the URL if it is a plain http(s) URL, else ''. Blocks file:, data:, ffmpeg pseudo-protocols."""
    value = (value or "").strip()
    try:
        parts = urlsplit(value)
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return ""
    if any(c in value for c in " \t\r\n\"'<>\\"):
        return ""
    return value


def to_int(value: object, default: int = 0) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def to_float(value: object, default: float = 0.0) -> float:
    try:
        f = float(str(value).strip())
        return f if f == f else default
    except (TypeError, ValueError):
        return default


def fold(text: str) -> str:
    """Search key: lowercase, accents removed."""
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


# ------------------------------------------------------------------ big JSON
# json.loads/json.dumps hold the interpreter lock for a whole call. For the provider's
# 100 MB replies and the 126 MB saved catalogue that froze the server (live video
# included) for seconds even from a worker thread. These do the same work in many short
# calls, so the event loop keeps running between them.
_WS = re.compile(r"[ \t\n\r]*")
JSON_CHUNK = 4000


def iter_json_array(raw: bytes):
    """Yield the elements of a top-level JSON array one by one. Anything that is not an array
    (an object, null) yields nothing; malformed JSON raises ValueError."""
    text = raw.decode("utf-8", errors="replace").lstrip("\ufeff")
    del raw
    dec = json.JSONDecoder()
    i = _WS.match(text).end()
    if i == len(text) or text[i] != "[":
        json.loads(text)                            # validate; a non-array has no elements
        return
    i = _WS.match(text, i + 1).end()
    if text[i:i + 1] == "]":
        return
    while True:
        value, i = dec.raw_decode(text, i)
        yield value
        i = _WS.match(text, i).end()
        c = text[i:i + 1]
        if c == ",":
            i = _WS.match(text, i + 1).end()
        elif c == "]":
            return
        else:
            raise ValueError(f"bad JSON array at character {i}")


def write_json(fh, obj, chunk: int = JSON_CHUNK) -> None:
    """json.dump(obj, fh, separators=(",", ":")) with long lists encoded a slice at a time.
    Same output (dict keys must be strings)."""
    if isinstance(obj, dict):
        fh.write("{")
        for n, (k, v) in enumerate(obj.items()):
            fh.write(("," if n else "") + json.dumps(k) + ":")
            write_json(fh, v, chunk)
        fh.write("}")
    elif isinstance(obj, list) and len(obj) > chunk:
        fh.write("[")
        for i in range(0, len(obj), chunk):
            fh.write(("," if i else "") + json.dumps(obj[i:i + chunk], separators=(",", ":"))[1:-1])
        fh.write("]")
    else:
        fh.write(json.dumps(obj, separators=(",", ":")))


class Redactor:
    """Strips URLs and credentials from text before it reaches a log or the browser."""

    _URL = re.compile(r"(?i)\b(?:https?|rtmp|rtsp|udp|tcp|tls|file)://[^\s'\"<>]+")

    def __init__(self) -> None:
        self.secrets: list[str] = []

    def set_secrets(self, values) -> None:
        self.secrets = sorted({v for v in values if v and len(v) >= 3}, key=len, reverse=True)

    def __call__(self, text: object) -> str:
        out = str(text)
        for s in self.secrets:
            out = out.replace(s, "***")
        return self._URL.sub("<url>", out)


redact = Redactor()
