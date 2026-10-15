"""Catalogue refresh safety check and change history.

Before a freshly fetched catalogue replaces the one being served, it is compared with it. A
provider having a bad moment usually still answers "200 OK" - with an empty list, half a list,
a list of junk, or no categories - and accepting that would wipe titles, break favourites and
progress (they point at IDs), and throw away the cached lists. A suspicious refresh is
REJECTED: the last known-good catalogue stays, the anomaly is logged, recorded here, shown in
`iptv status` and alerted; the next scheduled refresh tries again. `iptv refresh --accept-changes`
accepts the next refresh whatever it looks like (for a genuine big change).

Thresholds (per section, against the catalogue being replaced). Normal churn for this provider
is small: the first recorded refresh (10 Oct 2026) changed +13/-0 movies in about 50 minutes, on ~181k movies / 48k series / 57k channels; refresh-history.json now records every
refresh, so the limits can be checked against real 12-hourly churn. They sit far above normal
churn and far below what a broken reply looks like. Providers do sometimes drop a whole category
of a few thousand titles for real, so one category going is only a warning.

  reject  section was >= 100 items and came back empty
  reject  net shrink beyond the section's limit:  live > 30% and > 2000 channels
                                                  movie > 15% and > 5000 titles
                                                  series > 15% and > 2000 titles
          (live channels come and go with events more than films do)
  reject  more than half of the previous IDs are gone, even if the count looks right (a
          renumbered catalogue would orphan every favourite and resume point)
  reject  categories that disappeared held > 25% of the section's items
  reject  malformed reply: > 2% of rows unusable (no id), or the category list missing (> 30%
          of items "Uncategorised" where there were < 5% before)
  warn    net shrink > 5%, or a category of >= 200 items disappeared (accepted, recorded)

A refresh where one section failed outright (an HTTP error or non-JSON reply) never gets here:
the provider raises and the whole old catalogue is kept (recorded as "failed", naming the
section).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

SECTIONS = ("live", "movie", "series")
LABEL = {"live": "live channels", "movie": "movies", "series": "series"}
LIMITS = {"live": (0.30, 2000), "movie": (0.15, 5000), "series": (0.15, 2000)}
EMPTY_MIN = 100
WARN_SHRINK = 0.05
ID_CHURN = 0.5
CAT_SHARE = 0.25
CAT_WARN_ITEMS = 200
BAD_ROWS = 0.02
UNCAT_NEW, UNCAT_OLD = 0.30, 0.05
HISTORY_KEEP = 60                      # refreshes kept (12-hourly: about a month)
IDS_KEEP_ENTRIES = 6                   # entries that keep the added-title IDs ("recently added")
IDS_MAX = 2000


def _cat_names(data: dict, kind: str) -> dict:
    return data.get("cats", {}).get(kind, {})


def _uncat_share(data: dict, kind: str) -> float:
    items = data.get(kind) or []
    if not items:
        return 0.0
    names = _cat_names(data, kind)
    return sum(1 for it in items if names.get(it.get("cat"), "Uncategorised") == "Uncategorised") / len(items)


def compare(old: dict, new: dict, stats: dict | None = None) -> dict:
    """What changed between two catalogues, and whether the new one looks safe to use.
    Pure (runs in a worker thread: it builds sets of a few hundred thousand IDs)."""
    stats = stats or {}
    first = not any(old.get(k) for k in SECTIONS)
    out = {"status": "accepted", "anomalies": [], "warnings": [], "counts": {}, "added": {}, "removed": {},
           "cats_added": {}, "cats_removed": {}, "new_titles": {}, "new_ids": {}}
    for kind in SECTIONS:
        old_items, new_items = old.get(kind) or [], new.get(kind) or []
        n_old, n_new = len(old_items), len(new_items)
        out["counts"][kind] = n_new
        old_ids = {it["id"] for it in old_items}
        new_ids = {it["id"] for it in new_items}
        added = new_ids - old_ids
        removed = old_ids - new_ids
        out["added"][kind], out["removed"][kind] = len(added), len(removed)
        if kind in ("movie", "series") and added and not first:
            fresh = [it for it in new_items if it["id"] in added]
            fresh.sort(key=lambda it: -(it.get("added") or 0))
            out["new_titles"][kind] = [it.get("name", "") for it in fresh[:12]]
            out["new_ids"][kind] = [it["id"] for it in fresh[:IDS_MAX]]
        old_cats, new_cats = _cat_names(old, kind), _cat_names(new, kind)
        old_names, new_names = set(old_cats.values()), set(new_cats.values())
        out["cats_added"][kind] = sorted(new_names - old_names)[:20]
        gone = old_names - new_names
        out["cats_removed"][kind] = sorted(gone)[:20]
        label = LABEL[kind]
        seen, bad = stats.get(kind, (n_new, 0))
        if seen and bad / seen > BAD_ROWS and bad >= 50:
            out["anomalies"].append(f"{label}: {bad} of {seen} rows were unusable (malformed reply)")
        if first:
            continue
        if n_old >= EMPTY_MIN and n_new == 0:
            out["anomalies"].append(f"{label}: came back empty (had {n_old})")
            continue
        frac, absolute = LIMITS[kind]
        shrink = n_old - n_new
        if n_old and shrink > 0 and shrink / n_old > frac and shrink > absolute:
            out["anomalies"].append(f"{label}: {n_old} -> {n_new} ({shrink / n_old:.0%} fewer)")
        elif n_old and shrink > 0 and shrink / n_old > WARN_SHRINK:
            out["warnings"].append(f"{label}: {n_old} -> {n_new} ({shrink / n_old:.0%} fewer)")
        if n_old >= EMPTY_MIN and len(removed) / n_old > ID_CHURN:
            out["anomalies"].append(f"{label}: {len(removed)} of {n_old} IDs disappeared (renumbered?)")
        if gone and n_old:
            by_cat: dict = {}
            for it in old_items:
                name = old_cats.get(it.get("cat"), "")
                if name in gone:
                    by_cat[name] = by_cat.get(name, 0) + 1
            lost = sum(by_cat.values())
            big = sorted((n, c) for c, n in by_cat.items() if n >= CAT_WARN_ITEMS)
            if lost / n_old > CAT_SHARE:
                out["anomalies"].append(f"{label}: {len(gone)} categories with {lost} items disappeared")
            elif big:
                out["warnings"].append(f"{label}: categories gone: " +
                                       ", ".join(f"{c} ({n})" for n, c in reversed(big[-5:])))
        if n_new >= EMPTY_MIN and _uncat_share(new, kind) > UNCAT_NEW and _uncat_share(old, kind) < UNCAT_OLD:
            out["anomalies"].append(f"{label}: category list missing ({_uncat_share(new, kind):.0%} uncategorised)")
    if out["anomalies"]:
        out["status"] = "rejected"
    return out


def change_line(entry: dict) -> str:
    """'+312 / -40 movies, +2 series, live unchanged' for status lines and alerts."""
    parts = []
    for kind in SECTIONS:
        a, r = entry.get("added", {}).get(kind, 0), entry.get("removed", {}).get(kind, 0)
        if a or r:
            parts.append(f"+{a}/-{r} {LABEL[kind]}")
    return ", ".join(parts) or "no changes"


class History:
    """Bounded record of refresh outcomes (refresh-history.json in the state directory)."""

    def __init__(self, path: Path, clock=time.time):
        self.path = path
        self.clock = clock
        try:
            self.entries = json.loads(path.read_text())
            if not isinstance(self.entries, list):
                self.entries = []
        except (FileNotFoundError, ValueError):
            self.entries = []

    def record(self, status: str, reason: str, report: dict | None = None, error: str = "",
               seconds: float = 0.0) -> dict:
        entry = {"t": int(self.clock()), "status": status, "reason": reason or "manual", "seconds": round(seconds, 1)}
        if error:
            entry["error"] = error[:300]
        if report:
            entry.update({k: report[k] for k in ("counts", "added", "removed", "cats_added", "cats_removed",
                                                 "new_titles", "new_ids", "anomalies", "warnings") if k in report})
        self.entries = (self.entries + [entry])[-HISTORY_KEEP:]
        for old in self.entries[:-IDS_KEEP_ENTRIES]:
            old.pop("new_ids", None)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.entries, separators=(",", ":"), ensure_ascii=False))
        os.replace(tmp, self.path)
        return entry

    def last(self, status: str | None = None) -> dict | None:
        for e in reversed(self.entries):
            if status is None or e["status"] == status:
                return e
        return None

    def failing_streak(self) -> int:
        """Consecutive refreshes that did not succeed (rejected or failed), most recent first."""
        n = 0
        for e in reversed(self.entries):
            if e["status"] == "accepted":
                break
            n += 1
        return n

    def summary(self) -> dict:
        last, ok = self.last(), self.last("accepted")
        return {
            "last": {k: last[k] for k in ("t", "status", "reason", "error", "anomalies", "warnings") if k in last} if last else None,
            "last_ok": {"t": ok["t"], "changes": change_line(ok), "warnings": ok.get("warnings", []),
                        "new_titles": ok.get("new_titles", {})} if ok else None,
            "failing": self.failing_streak(),
        }
