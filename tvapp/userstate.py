"""Synced app state: favourites, playback progress, recently played, last channel, preferences,
hidden channels and categories, favourite sports teams and competitions.

Stored in SQLite (`state.db` in the state directory), one row per (namespace, key):

* Every write carries the writing device's timestamp (ms). Per row, the newer write
  wins (last-writer-wins); a write that is not newer than what is stored is ignored,
  so a device holding stale data can never overwrite newer progress or favourites.
  Exact ties are broken by content (_order), so arrival order never matters.
* Nothing is deleted because a device has *no* copy of it: a deletion only comes from an
  explicit tombstone (the user removed something). An empty or new browser only reads.
* Deletions are stored as tombstones, so removing a favourite also beats older copies
  still held by other devices. Tombstones are purged after TOMBSTONE_DAYS.
* Each applied write gets the next value of a server-wide revision counter; devices
  ask for "everything after revision N" and only receive what changed.
* `db_id` is created with the database. If a device sees a different one (recreated),
  or the same one with a lower revision than it last saw (restored from an older
  snapshot), it re-sends everything it holds before taking anything, and the normal
  newer-wins rule merges it back in.

Nothing here comes from or reveals the provider: values are names, opaque item IDs,
artwork keys, positions and UI preferences.

Sports favourites ("sfav") are keyed by the stable ids of tvapp/sports.py ("team:arsenal",
"comp:premier-league"); the value only keeps the name and kind for showing the list. The
sports_alerts table is the Pi's own record of match alerts already sent (not synced to devices,
not in the JSON export; in every database snapshot and backup), so a restart, a guide rebuild or
a restore can't send one twice.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .util import log

NAMESPACES = ("fav", "progress", "recent", "last", "pref", "hide", "sfav")
KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
MAX_VALUE = 2048                       # bytes of JSON per value
MAX_BATCH = 500                        # changes per request
CAPS = {"progress": 2000, "recent": 100}   # newest rows kept; older ones are dropped
TOMBSTONE_DAYS = 180
FORMAT = "signal-tv-classic-state"            # export/import file format name

# export section names; progress fields get readable names in the file
SECTIONS = {"fav": "favourites", "progress": "progress", "recent": "recent", "last": "channels", "pref": "preferences",
            "hide": "hidden", "sfav": "sports_favourites"}
ALERT_KEEP_DAYS = 8                    # sent match alerts remembered this long
READABLE = {"t": "position_s", "d": "duration_s"}

SCHEMA = [
    """CREATE TABLE state (
        ns      TEXT    NOT NULL,
        key     TEXT    NOT NULL,
        value   TEXT,                       -- JSON, NULL for a deletion (tombstone)
        updated INTEGER NOT NULL,           -- ms since the epoch, from the writing device (never ahead of the server)
        deleted INTEGER NOT NULL DEFAULT 0,
        rev     INTEGER NOT NULL,           -- server revision of the write that set this row
        device  TEXT    NOT NULL DEFAULT '',
        PRIMARY KEY (ns, key)
    ) WITHOUT ROWID""",
    "CREATE INDEX state_rev ON state (rev)",
    "CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)",
]
SCHEMA_V2 = [
    """CREATE TABLE sports_alerts (
        id   TEXT    PRIMARY KEY,           -- event id + kick-off day (tvapp/sportsalerts.py)
        sent INTEGER NOT NULL,              -- ms since the epoch
        text TEXT    NOT NULL DEFAULT ''
    ) WITHOUT ROWID""",
]


class StateError(ValueError):
    """A problem with submitted state; the message is safe to show."""


def _order(updated: int, text: str | None) -> tuple:
    """Which of two writes to one row wins: the newer one. On an exact tie (same ms) a value
    beats a deletion, then the larger JSON text wins - arbitrary but fixed, so the result
    never depends on which device's write arrived first."""
    return (updated, text is not None, text or "")


def now_ms() -> int:
    return int(time.time() * 1000)


EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def iso(ms: int) -> str:
    """ms timestamp -> '2026-10-09T21:15:02.123Z' (exact: integer arithmetic, no float rounding)."""
    return (EPOCH + timedelta(milliseconds=ms)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_iso(value: object) -> int:
    if not isinstance(value, str):
        raise StateError("bad timestamp")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise StateError("bad timestamp") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - EPOCH) // timedelta(milliseconds=1)


class StateStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        try:
            self._open()
        except sqlite3.DatabaseError as e:
            # Damaged file (e.g. SD card trouble): keep it for inspection and start a new one.
            # Devices notice the new database id and send back everything they hold.
            if getattr(self, "db", None) is not None:
                self.db.close()
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            for suffix in ("", "-wal", "-shm"):
                old = Path(f"{path}{suffix}")
                if old.exists():
                    old.rename(f"{path}.damaged-{stamp}{suffix}")
            log.error("state database %s was damaged (%s); moved aside as %s.damaged-%s, starting a new one "
                      "- devices will send back what they hold", path, e, path.name, stamp)
            self._open()

    def _open(self) -> None:
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        os.chmod(self.path, 0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        check = self.db.execute("PRAGMA quick_check").fetchone()[0]
        if check != "ok":
            raise sqlite3.DatabaseError(f"quick_check: {check}")
        with self._lock:
            self._migrate()
            self.db_id = self._meta("db_id")

    def close(self) -> None:
        with self._lock:
            self.db.close()

    # ------------------------------------------------------------ internals
    @contextmanager
    def _tx(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self.db.execute("COMMIT")

    def _migrate(self) -> None:
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version < 1:
            with self._tx() as db:
                for stmt in SCHEMA:
                    db.execute(stmt)
                db.execute("INSERT INTO meta VALUES ('db_id', ?), ('rev', '0'), ('created', ?)",
                           (secrets.token_hex(8), iso(now_ms())))
                db.execute("PRAGMA user_version = 1")
        if version < 2:
            with self._tx() as db:
                for stmt in SCHEMA_V2:
                    db.execute(stmt)
                db.execute("PRAGMA user_version = 2")

    def _meta(self, k: str) -> str:
        return self.db.execute("SELECT v FROM meta WHERE k = ?", (k,)).fetchone()[0]

    @staticmethod
    def _check(ns: object, key: object, value: object, deleted: bool) -> str | None:
        """Validate one row; returns the value as JSON (None for a deletion)."""
        if ns not in NAMESPACES:
            raise StateError("unknown namespace")
        if not isinstance(key, str) or not KEY_RE.match(key):
            raise StateError("bad key")
        if deleted:
            return None
        if ns != "pref" and not isinstance(value, dict):
            raise StateError(f"{ns} values must be objects")
        if ns == "pref" and isinstance(value, (dict, list)):
            raise StateError("preference values must be plain values")
        text = json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        if len(text.encode()) > MAX_VALUE:
            raise StateError("value too large")
        return text

    def _write(self, db, rev: int, ns: str, key: str, text: str | None, updated: int, device: str) -> None:
        db.execute("INSERT INTO state (ns, key, value, updated, deleted, rev, device) VALUES (?, ?, ?, ?, ?, ?, ?) "
                   "ON CONFLICT (ns, key) DO UPDATE SET value = excluded.value, updated = excluded.updated, "
                   "deleted = excluded.deleted, rev = excluded.rev, device = excluded.device",
                   (ns, key, text, updated, 1 if text is None else 0, rev, device))

    def _prune(self, db) -> None:
        for ns, cap in CAPS.items():
            db.execute("DELETE FROM state WHERE ns = ? AND deleted = 0 AND key NOT IN "
                       "(SELECT key FROM state WHERE ns = ? AND deleted = 0 ORDER BY updated DESC LIMIT ?)",
                       (ns, ns, cap))

    # -------------------------------------------------------------- public
    def apply(self, changes: list, device: str = "") -> dict:
        """Merge a batch of changes from a device; returns counts. Invalid rows are skipped, not fatal."""
        if not isinstance(changes, list):
            raise StateError("changes must be a list")
        if len(changes) > MAX_BATCH:
            raise StateError(f"at most {MAX_BATCH} changes per request")
        applied = rejected = 0
        with self._lock, self._tx() as db:
            now = now_ms()
            rev = int(self._meta("rev"))
            for ch in changes:
                try:
                    if not isinstance(ch, dict):
                        raise StateError("change must be an object")
                    deleted = bool(ch.get("deleted"))
                    text = self._check(ch.get("ns"), ch.get("key"), ch.get("value"), deleted)
                    upd = ch.get("updated")
                    if isinstance(upd, bool) or not isinstance(upd, (int, float)) or not upd > 0:
                        raise StateError("bad timestamp")
                except StateError:
                    rejected += 1
                    continue
                upd = min(int(upd), now)         # a device clock running ahead cannot pin a value forever
                row = db.execute("SELECT updated, value FROM state WHERE ns = ? AND key = ?",
                                 (ch["ns"], ch["key"])).fetchone()
                if row and _order(row[0], row[1]) >= _order(upd, text):
                    continue                     # stale: what is stored is newer (or wins the tie)
                rev += 1
                self._write(db, rev, ch["ns"], ch["key"], text, upd, device)
                applied += 1
            if applied:
                db.execute("UPDATE meta SET v = ? WHERE k = 'rev'", (str(rev),))
                self._prune(db)
        return {"applied": applied, "rejected": rejected}

    def changes_since(self, since: int) -> dict:
        """Rows written after revision `since` (all rows, tombstones included, for 0 or an unknown revision)."""
        with self._lock:
            rev = int(self._meta("rev"))
            if since < 0 or since > rev:
                since = 0
            rows = self.db.execute("SELECT ns, key, value, updated, deleted FROM state WHERE rev > ? ORDER BY rev",
                                   (since,)).fetchall()
        return {"db": self.db_id, "rev": rev, "now": now_ms(),
                "changes": [{"ns": ns, "key": key, "updated": upd, "deleted": bool(dl),
                             "value": None if dl else json.loads(val)} for ns, key, val, upd, dl in rows]}

    def maintain(self) -> int:
        """Purge old tombstones; returns how many were removed."""
        cutoff = now_ms() - TOMBSTONE_DAYS * 86400 * 1000
        with self._lock, self._tx() as db:
            return db.execute("DELETE FROM state WHERE deleted = 1 AND updated < ?", (cutoff,)).rowcount

    def values(self, ns: str) -> dict:
        """{key: value} of one namespace, deletions left out (the Pi reading synced state)."""
        with self._lock:
            rows = self.db.execute("SELECT key, value FROM state WHERE ns = ? AND deleted = 0", (ns,)).fetchall()
        return {k: json.loads(v) for k, v in rows}

    def pref(self, key: str, default=None):
        with self._lock:
            row = self.db.execute("SELECT value FROM state WHERE ns = 'pref' AND key = ? AND deleted = 0", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    # ---------------------------------------------------- sent match alerts (not synced)
    def alerts_sent(self, ids: list[str]) -> set[str]:
        if not ids:
            return set()
        with self._lock:
            marks = ",".join("?" * len(ids))
            return {r[0] for r in self.db.execute(f"SELECT id FROM sports_alerts WHERE id IN ({marks})", ids)}

    def record_alerts(self, ids: list[str], text: str) -> None:
        """Recorded before the message is sent: a crash in between loses one alert, never repeats it."""
        cutoff = now_ms() - ALERT_KEEP_DAYS * 86400 * 1000
        with self._lock, self._tx() as db:
            for i in ids:
                db.execute("INSERT OR IGNORE INTO sports_alerts (id, sent, text) VALUES (?, ?, ?)", (i, now_ms(), text[:500]))
            db.execute("DELETE FROM sports_alerts WHERE sent < ?", (cutoff,))

    def forget_alerts(self, ids: list[str]) -> None:
        with self._lock, self._tx() as db:
            for i in ids:
                db.execute("DELETE FROM sports_alerts WHERE id = ?", (i,))

    def summary(self) -> dict:
        with self._lock:
            counts = dict(self.db.execute("SELECT ns, COUNT(*) FROM state WHERE deleted = 0 GROUP BY ns").fetchall())
            last = self.db.execute("SELECT MAX(updated) FROM state").fetchone()[0]
            rev = int(self._meta("rev"))
        return {"rev": rev, "favourites": counts.get("fav", 0), "sports_favourites": counts.get("sfav", 0),
                "progress": counts.get("progress", 0),
                "recent": counts.get("recent", 0), "preferences": counts.get("pref", 0),
                "last_change": int(last / 1000) if last else 0}

    # -------------------------------------------------------- export / import
    def export(self, **about) -> dict:
        """Human-readable copy of everything stored (no tombstones). Contains no provider URLs or credentials."""
        with self._lock:
            return export_doc(self.db, about)

    def import_doc(self, doc: object, mode: str = "merge") -> dict:
        """merge: newer-wins per row, using the file's timestamps.
        replace: the file becomes the whole state (stamped now); anything not in it is deleted."""
        if mode not in ("merge", "replace"):
            raise StateError("mode must be merge or replace")
        if not isinstance(doc, dict) or doc.get("format") != FORMAT or doc.get("version") != 1:
            raise StateError("this is not a Signal TV Classic state export")
        back = {v: k for k, v in READABLE.items()}
        changes = []
        for ns, name in SECTIONS.items():
            entries = doc.get(name, [])
            if not isinstance(entries, list):
                raise StateError(f"'{name}' must be a list")
            for e in entries:
                if not isinstance(e, dict):
                    raise StateError(f"bad entry in '{name}'")
                if ns == "pref":
                    value = e.get("value")
                else:
                    value = {back.get(k, k): v for k, v in e.items() if k not in ("key", "updated")}
                changes.append({"ns": ns, "key": e.get("key"), "value": value, "updated": parse_iso(e.get("updated"))})
        if mode == "merge":
            out = {"applied": 0, "rejected": 0}
            for i in range(0, len(changes), MAX_BATCH):
                r = self.apply(changes[i:i + MAX_BATCH], device="import")
                out = {k: out[k] + r[k] for k in out}
            return out
        # replace: validate everything first so a bad file changes nothing
        checked = [(c["ns"], c["key"], self._check(c["ns"], c["key"], c["value"], False)) for c in changes]
        keep = {(ns, key) for ns, key, _ in checked}
        with self._lock, self._tx() as db:
            now = max(now_ms(), (db.execute("SELECT MAX(updated) FROM state").fetchone()[0] or 0) + 1)
            rev = int(self._meta("rev"))
            removed = 0
            for ns, key in db.execute("SELECT ns, key FROM state WHERE deleted = 0").fetchall():
                if (ns, key) not in keep:
                    rev += 1
                    self._write(db, rev, ns, key, None, now, "import")
                    removed += 1
            for ns, key, text in checked:
                rev += 1
                self._write(db, rev, ns, key, text, now, "import")
            db.execute("UPDATE meta SET v = ? WHERE k = 'rev'", (str(rev),))
            self._prune(db)
        return {"applied": len(checked), "removed": removed, "rejected": 0}


def export_doc(db: sqlite3.Connection, about: dict) -> dict:
    rows = db.execute("SELECT ns, key, value, updated FROM state WHERE deleted = 0 ORDER BY ns, updated DESC").fetchall()
    doc: dict = {"format": FORMAT, "version": 1, "exported_at": iso(now_ms()), **about,
                 "about": "Favourites, playback progress, recently played, last channel, preferences, "
                          "hidden channels/categories and favourite sports teams/competitions for Signal TV Classic. Restore with Library > Backup > Import (merge or replace). "
                          "Contains no provider URLs or credentials.",
                 **{name: [] for name in SECTIONS.values()}}
    for ns, key, val, upd in rows:
        value = json.loads(val)
        entry = {"key": key, "updated": iso(upd)}
        if ns == "pref":
            entry["value"] = value
        else:
            entry.update({READABLE.get(k, k): v for k, v in value.items()})
        doc[SECTIONS[ns]].append(entry)
    return doc


def snapshot(db_path: Path, out_dir: Path, keep: int = 30, **about) -> list[Path]:
    """Consistent online copy (SQLite backup API) + readable JSON export into out_dir; keeps the newest `keep`."""
    out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = out_dir / f"state-{stamp}.sqlite3"
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    dst = sqlite3.connect(target)
    with dst:
        src.backup(dst)
    src.close()
    dst.execute("PRAGMA journal_mode=DELETE")       # a self-contained single file (no -wal/-shm)
    check = dst.execute("PRAGMA integrity_check").fetchone()[0]
    if check != "ok":
        dst.close()
        target.unlink()
        raise RuntimeError(f"backup failed integrity check: {check}")
    doc = export_doc(dst, about)
    dst.close()
    js = target.with_suffix(".json")
    fd = os.open(js, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
    for pattern in ("state-*.sqlite3", "state-*.json"):
        for old in sorted(out_dir.glob(pattern))[:-keep]:
            old.unlink()
    return [target, js]
