"""XMLTV guide: downloaded on a schedule, parsed in a thread, trimmed to a window
around now and to the channels that are actually in the catalogue."""
from __future__ import annotations

import gzip
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from .util import clean, fold, write_json

KEEP_BEFORE = 3 * 3600
KEEP_AFTER = 36 * 3600
_NORM = re.compile(r"[^a-z0-9]+")


def _norm(name: str) -> str:
    n = _NORM.sub(" ", fold(name)).strip()
    n = re.sub(r"\b(hd|fhd|uhd|4k|sd|hevc|h265|backup|\+1)\b", "", n)
    return n.replace(" ", "")


def _ts(value: str) -> float:
    value = (value or "").strip()
    try:
        if len(value) >= 19:
            return datetime.strptime(value[:20].strip(), "%Y%m%d%H%M%S %z").timestamp()
        return datetime.strptime(value[:14], "%Y%m%d%H%M%S").timestamp()   # local time, no offset
    except ValueError:
        return 0.0


def parse_xmltv(path: Path, live: list[dict]) -> dict[str, list]:
    """Returns {live_item_id: [[start, stop, title, desc(, categories)], ...]} sorted by start.
    categories (XMLTV <category>, e.g. the competition) only when the guide has any; the Live
    search (epgsearch.py) searches them with the description."""
    by_epg: dict[str, list[str]] = {}
    by_name: dict[str, list[str]] = {}
    for it in live:
        if it.get("epg"):
            by_epg.setdefault(it["epg"].lower(), []).append(it["id"])
        for n in {it.get("epg_name") or "", it.get("name") or ""}:
            if n:
                by_name.setdefault(_norm(n), []).append(it["id"])
    chan_map: dict[str, list[str]] = {}
    now = time.time()
    lo, hi = now - KEEP_BEFORE, now + KEEP_AFTER
    out: dict[str, list] = {}
    with path.open("rb") as raw:
        magic = raw.read(2)
    opener = gzip.open if magic == b"\x1f\x8b" else open
    with opener(path, "rb") as fh:
        for _, el in ET.iterparse(fh, events=("end",)):
            tag = el.tag
            if tag == "channel":
                cid = (el.get("id") or "").strip()
                ids = list(by_epg.get(cid.lower(), []))
                if not ids:
                    for dn in el.findall("display-name"):
                        ids = by_name.get(_norm(dn.text or ""), [])
                        if ids:
                            break
                if ids:
                    chan_map[cid] = ids
                el.clear()
            elif tag == "programme":
                cid = (el.get("channel") or "").strip()
                ids = chan_map.get(cid) or by_epg.get(cid.lower())
                if ids:
                    start, stop = _ts(el.get("start", "")), _ts(el.get("stop", ""))
                    if start and stop > lo and start < hi:
                        title = clean(el.findtext("title"), 160) or "Untitled"
                        desc = clean(el.findtext("desc"), 600)
                        sub = clean(el.findtext("sub-title"), 120)
                        if sub and sub.lower() != title.lower():
                            desc = f"{sub}. {desc}" if desc else sub
                        row = [int(start), int(stop or start + 1800), title, desc]
                        cats = [c for c in dict.fromkeys(clean(e.text, 60) for e in el.findall("category")) if c]
                        if cats:
                            row.append(", ".join(cats)[:200])
                        for iid in ids:
                            out.setdefault(iid, []).append(row)
                el.clear()
            elif tag == "tv":
                el.clear()
    for rows in out.values():
        rows.sort(key=lambda r: r[0])
        # de-duplicate overlapping duplicates from multiple sources
        dedup, last = [], None
        for r in rows:
            if last and r[0] == last[0]:
                continue
            dedup.append(r)
            last = r
        rows[:] = dedup
    return out


class Guide:
    def __init__(self, state_dir: Path):
        self.path = state_dir / "epg.json"
        self.data: dict[str, list] = {}
        self.updated = 0.0
        self.error = ""
        self.channels = 0

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text())
            self.data, self.updated = raw.get("data", {}), raw.get("updated", 0)
            self.channels = len(self.data)
        except (FileNotFoundError, ValueError):
            pass

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w") as fh:
            write_json(fh, {"updated": self.updated, "data": self.data})
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def now_next(self, iid: str, now: float | None = None):
        rows = self.data.get(iid)
        if not rows:
            return None
        now = now or time.time()
        for i, r in enumerate(rows):
            if r[0] <= now < r[1]:
                nxt = rows[i + 1] if i + 1 < len(rows) else None
                return {"now": r[:3], "next": nxt[:3] if nxt else None}
            if r[0] > now:
                return {"now": None, "next": r[:3]}
        return None

    def schedule(self, iid: str):
        now = time.time()
        return [r for r in self.data.get(iid, []) if r[1] > now - 3600][:60]
