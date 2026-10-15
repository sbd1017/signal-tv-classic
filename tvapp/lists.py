"""Pre-built, gzipped responses for the big catalogue lists.

/tv/api/movies alone is ~18 MB of JSON (180k titles). Building it took ~2 s of CPU on
every request (even for a 304) and stalled everything else the server does, live
video included. Each list is now built once per catalogue/guide version, in a worker
thread, and kept gzipped; a request is a dict lookup.

Encoding happens in chunks of ROWS_PER_CHUNK rows: json.dumps holds the interpreter
lock for a whole call, so one call over 180k rows would still freeze the event loop for
about a second even from a thread. Short calls let it run in between; zlib releases
the lock while compressing.
"""
from __future__ import annotations

import asyncio
import json
import time
import zlib
from typing import Callable, Iterable

from .util import log

ROWS_PER_CHUNK = 4000


def _dumps(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def encode_gz(head: dict, rows: Iterable[list]) -> bytes:
    """gzip of {**head, "items": [*rows]} - the same JSON as one json.dumps, built in chunks."""
    if not head:
        raise ValueError("head must have at least one key")
    comp = zlib.compressobj(6, zlib.DEFLATED, 31)          # wbits 31 = gzip container
    parts = [comp.compress((_dumps(head)[:-1] + ',"items":[').encode())]
    batch: list = []
    first = True
    for row in rows:
        batch.append(row)
        if len(batch) >= ROWS_PER_CHUNK:
            parts.append(comp.compress((("" if first else ",") + _dumps(batch)[1:-1]).encode()))
            first = False
            batch = []
    if batch:
        parts.append(comp.compress((("" if first else ",") + _dumps(batch)[1:-1]).encode()))
    parts.append(comp.compress(b"]}"))
    parts.append(comp.flush())
    return b"".join(parts)


class ListCache:
    """sources: {kind: (tag(), build() -> gzip bytes)}. tag() must be cheap; build() runs in a thread."""

    def __init__(self, sources: dict[str, tuple[Callable[[], str], Callable[[], bytes]]], quiet=()):
        self.sources = sources
        self.quiet = set(quiet)                    # kinds rebuilt often (no info log per build)
        self.entries: dict[str, tuple[str, bytes]] = {}
        self.locks = {k: asyncio.Lock() for k in sources}
        self.builds = 0

    def tag(self, kind: str) -> str:
        return self.sources[kind][0]()

    async def get(self, kind: str) -> tuple[str, bytes]:
        """(tag, gzip body) for the current version, building it if needed (once, however many ask)."""
        entry = self.entries.get(kind)
        if entry and entry[0] == self.tag(kind):
            return entry
        async with self.locks[kind]:
            tag = self.tag(kind)
            entry = self.entries.get(kind)
            if entry and entry[0] == tag:
                return entry                       # built while we waited for the lock
            t0 = time.monotonic()
            body = await asyncio.to_thread(self.sources[kind][1])
            self.entries[kind] = entry = (tag, body)
            self.builds += 1
            (log.debug if kind in self.quiet else log.info)(
                "list %s built: %d KB gzipped in %.1fs", kind, len(body) >> 10, time.monotonic() - t0)
            return entry

    def warm_needed(self, kinds: Iterable[str]) -> bool:
        """True if any of these lists is missing or out of date."""
        return any(self.entries.get(k, ("",))[0] != self.tag(k) for k in kinds)

    async def warm(self, kinds: Iterable[str]) -> None:
        for kind in kinds:
            try:
                await self.get(kind)
            except Exception as e:  # noqa: BLE001 - a request will retry the build
                log.warning("list %s: build failed: %s", kind, e)

    def sizes(self) -> dict[str, int]:
        return {k: len(v[1]) for k, v in self.entries.items()}
