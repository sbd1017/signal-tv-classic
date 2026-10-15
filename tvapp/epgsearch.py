"""Live TV search over the programme guide: what is on now and in the next 24 hours.

Typing "Arsenal" or "Champions League" should find the channels showing it even when the channel
name doesn't say so. The guide holds ~260k rows for ~6,900 channels, but most are the same
programme on many channels (one match on 40 feeds), so the index keeps each distinct programme
text once:

- records: distinct (title, description) pairs among rows that haven't ended and start within
  the horizon. Display strings are the guide's own objects (not copied).
- words: the distinct words, sorted, in one string ("\\nword\\nword..."), so a prefix ("ars" while
  typing "arsenal") is one find plus a walk; for each word, the records that have it (record*2,
  +1 when it is in the title) in one flat array.
- normalised titles and descriptions, to check a phrase's word order and to group events.
- occurrences: per record, the (channel, start, stop) rows that show it, in flat arrays.
Everything kept is a few long strings and arrays, not small objects: no per-programme objects for
the allocator to fragment, so the build's scratch memory goes back to the system afterwards.

Matching: a word matches from its start ("nba" finds "NBA", not "Navy CIS"; "ars" finds
"Arsenal" while typing); a word shorter than 3 characters ("F1", "v") only matches a whole word.
Several words match when they appear in that order in the title or the description, or all of
them are in the title ("arsenal chelsea" finds "Arsenal v Chelsea"). No fuzzy matching and no
abbreviation guessing ("Man Utd" finds "Man Utd" only). Built in a worker thread whenever the
guide or the channel list changes, or the index is a few hours old; searches meanwhile use the
previous one.
"""
from __future__ import annotations

import time
from collections import Counter
from array import array
from bisect import bisect_left
from typing import Callable

from . import livemeta, sports
from .sports import norm


AHEAD = 24 * 3600            # search what starts within this
REBUILD_AFTER = 3 * 3600     # an index this old is rebuilt (rows that ended are skipped meanwhile)
SOON = 3600                  # "starting soon": within the hour
SAME_EVENT = 20 * 60         # same title starting within this of each other: one event on several channels
MAX_ROW = 12 * 3600          # longer guide rows are filler or a wrong time zone (seen: a match listed for 24 h)
KEEP_PAST = 3 * 3600         # programmes that ended this recently are kept (the guide keeps 3 h): a match shown
                             # live at 20:00 makes the 22:30 airing a replay. Searches skip them.
FULL_AIRING = 90 * 60        # an earlier airing makes a later one a replay only if it was a whole match, not a
                             # 30-minute condensed version
MIN_QUERY = 2                # characters ("F1")
WHOLE_WORD = 3               # shorter words must match a whole word, not the start of one
MAX_EVENTS = 60
MAX_GROUPS = 40              # channels listed per event (the rest are counted)
HINT_LABEL = {2160: "4K", 1080: "1080p", 720: "HD", 576: "SD"}
BROAD_ROWS = 15000           # a query matching more rows than this ("the") only lists what's on now and soon
SPORT_SAME = 75 * 60         # one match listed by broadcasters starting up to this far apart (19:55 / 20:00, early build-up)
SPORTS_SOON = 3 * 3600       # the Sports view's "starting soon"
WOMEN, YOUTH, REPLAY, LIVE_TAG, EXPLICIT, DESC_TEAMS, DESC_COMP = 1, 2, 4, 8, 16, 32, 64     # sport flags per programme

def sport_clusters(rows: list) -> list[list]:
    """Rows (start, stop, ...) sorted by start -> airings: overlapping listings within SPORT_SAME
    of the time most of them give (the kick-off), not of the earliest (a channel starting its
    build-up at 19:00 for a 20:00 match, another listing it from 21:00). No overlap: another airing."""
    out: list[list] = []
    cur: list = []
    counts: dict[int, int] = {}
    stop = 0
    for r in rows:
        if cur:
            anchor = max(counts.items(), key=lambda kv: (kv[1], -kv[0]))[0]
        if not cur or r[0] >= stop or abs(r[0] - anchor) > SPORT_SAME:
            cur, counts, stop = [], {}, 0
            out.append(cur)
        cur.append(r)
        counts[r[0]] = counts.get(r[0], 0) + 1
        stop = max(stop, r[1])
    return out


class _Texts:
    """Many strings stored as two long ones (plain ASCII at one byte a character, and the rest)
    plus offsets."""

    def __init__(self):
        self._parts: tuple[list, list] = ([], [])
        self._size = [0, 0]
        self.pos = array("I")                       # 2 * offset, +1 = in the wide string
        self.len = array("I")

    def add(self, text: str) -> None:
        wide = 0 if text.isascii() else 1
        self._parts[wide].append(text)
        self.pos.append(self._size[wide] * 2 + wide)
        self.len.append(len(text))
        self._size[wide] += len(text)

    def done(self) -> None:
        self.text = ("".join(self._parts[0]), "".join(self._parts[1]))
        del self._parts

    def __getitem__(self, i: int) -> str:
        p = self.pos[i]
        return self.text[p & 1][p >> 1:(p >> 1) + self.len[i]]

    def chars(self) -> int:
        return len(self.text[0]) + len(self.text[1])


class Index:
    def __init__(self, guide: dict[str, list], now: float | None = None, horizon: float = AHEAD + REBUILD_AFTER,
                 channel_sport: dict[str, int] | None = None):
        t0 = time.time()
        now = now or t0
        self.built = now
        hi = now + horizon
        self.iids: list[str] = []
        recs: dict[tuple, int] = {}
        occ: list[array] = []                       # per record: ch, start, stop, ch, start, stop, ...
        self.titles: list[str] = []
        self.descs: list[str] = []
        for iid, rows in guide.items():
            ch = -1
            for r in rows:
                if r[1] <= now - KEEP_PAST or r[0] >= hi or r[1] - r[0] > MAX_ROW:
                    continue
                if ch < 0:
                    ch = len(self.iids)
                    self.iids.append(iid)
                extra = r[4] if len(r) > 4 else ""      # XMLTV <category> (competition), when a guide has it
                key = (r[2], r[3], extra)
                rec = recs.get(key)
                if rec is None:
                    rec = recs[key] = len(self.titles)
                    self.titles.append(r[2])
                    self.descs.append(r[3])
                    occ.append(array("I"))
                occ[rec].extend((ch, int(r[0]), int(r[1])))
        self.tnorm, self.dnorm = _Texts(), _Texts()
        words: dict[str, array] = {}
        for (title, desc, extra), rec in recs.items():
            t = norm(title)
            d = norm(f"{desc} {extra}" if extra else desc)
            self.tnorm.add(t)
            self.dnorm.add(d)
            tw = set(t.split())
            for w in tw:
                (words.get(w) or words.setdefault(w, array("I"))).append(rec * 2 + 1)
            for w in set(d.split()) - tw:
                (words.get(w) or words.setdefault(w, array("I"))).append(rec * 2)
        self.tnorm.done()
        self.dnorm.done()
        vocab = sorted(words)
        self.vocab = "\n" + "\n".join(vocab) + "\n"
        self.vpos = array("I")                      # where each word starts in vocab
        self.post = array("I")                      # postings of word i: post[pfirst[i]:pfirst[i + 1]]
        self.pfirst = array("I", [0])
        pos = 1
        for w in vocab:
            self.vpos.append(pos)
            pos += len(w) + 1
            self.post.extend(words.pop(w))
            self.pfirst.append(len(self.post))
        self.nwords = len(vocab)
        del vocab, words
        self.first = array("I", [0])
        self.occ_ch, self.occ_start, self.occ_stop = array("I"), array("I"), array("I")
        for a in occ:
            self.occ_ch.extend(a[0::3])
            self.occ_start.extend(a[1::3])
            self.occ_stop.extend(a[2::3])
            self.first.append(len(self.occ_ch))
        del occ, recs
        self.rows = len(self.occ_ch)
        t1 = time.time()
        self._classify(channel_sport or {})
        self.sports_s = round(time.time() - t1, 2)
        self.build_s = round(time.time() - t0, 2)

    def _classify(self, channel_sport: dict[str, int]) -> None:
        """What each programme is in sport terms (sports.classify), in flat arrays."""
        n = len(self.titles)
        chs = array("B", [channel_sport.get(i, 0) for i in self.iids])
        self.sp_sport, self.sp_kind, self.sp_flags, self.sp_ch = (array("B", bytes(n)) for _ in range(4))
        self.sp_comp, self.sp_a, self.sp_b = (array("h", [-1]) * n for _ in range(3))
        self.sp_key = array("I", [0]) * n
        self.sp_dated = array("I", [0]) * n
        self.keys: list[str] = [""]
        keyid: dict[str, int] = {"": 0}
        self.sport_recs = array("I")
        key_recs: dict[int, list] = {}
        ents: dict[int, list] = {}
        first, occ_ch = self.first, self.occ_ch
        for rec in range(n):
            cs = 0
            for k in range(first[rec], first[rec + 1]):
                c = chs[occ_ch[k]]
                if c and c != sports.OTHER:
                    cs = c
                    break
                cs = cs or c
            self.sp_ch[rec] = cs
            if not sports.worth_classifying(self.titles[rec], cs):
                continue
            info = sports.classify(self.titles[rec], self.descs[rec], cs)
            if info is None:
                continue
            self.sp_sport[rec], self.sp_kind[rec] = info.sport, info.kind
            self.sp_flags[rec] = (WOMEN * info.women) | (YOUTH * info.youth) | (REPLAY * info.replay) | \
                (LIVE_TAG * info.live) | (EXPLICIT * info.explicit) | (DESC_TEAMS * info.from_desc) | \
                (DESC_COMP * info.comp_desc)
            self.sp_comp[rec], (self.sp_a[rec], self.sp_b[rec]) = info.comp, info.teams
            self.sp_dated[rec] = info.dated
            kid = keyid.get(info.key)
            if kid is None:
                kid = keyid[info.key] = len(self.keys)
                self.keys.append(info.key)
            self.sp_key[rec] = kid
            if info.kind in (sports.MATCH, sports.EVENT):
                self.sport_recs.append(rec)
                key_recs.setdefault(kid, []).append(rec)
            for e in (*info.teams, info.comp):
                if e >= 0:
                    ents.setdefault(e, []).append(rec)
        self.ent_recs = {e: array("I", v) for e, v in ents.items()}
        self.key_recs = {k: array("I", v) for k, v in key_recs.items()}     # event key -> its matches / events
        self._info: dict[int, dict] = {}            # event_info cache (sport programmes only: bounded)

    def stats(self) -> dict:
        return {"records": len(self.titles), "rows": self.rows, "channels": len(self.iids),
                "words": self.nwords, "sport_programmes": len(self.sport_recs), "sports_s": self.sports_s, "text_chars": self.tnorm.chars() + self.dnorm.chars() + len(self.vocab), "built": int(self.built), "build_s": self.build_s}

    # ------------------------------------------------------------------ search
    def _word(self, w: str, prefix: bool) -> tuple[set[int], set[int]]:
        """-> (records with a word starting with w (or exactly w), those with it in the title)."""
        found: set[int] = set()
        titled: set[int] = set()
        i = self.vocab.find("\n" + w + ("" if prefix else "\n"))
        if i < 0:
            return found, titled
        k = bisect_left(self.vpos, i + 1)
        last = k + 1
        if prefix:                                  # sorted: the other words with this start follow
            while last < self.nwords and self.vocab.startswith(w, self.vpos[last]):
                last += 1
        for v in self.post[self.pfirst[k]:self.pfirst[last]]:
            found.add(v >> 1)
            if v & 1:
                titled.add(v >> 1)
        return found, titled

    def match(self, q: str) -> dict[int, int]:
        """record -> strength: 3 the title has the words in order, 2 the title has them all,
        1 the description has them in order. A query naming a team or competition ("Man Utd",
        "UCL") also matches its other names (sports.py); without one, matching is literal."""
        words = norm(q).split()
        if not words or len(" ".join(words)) < MIN_QUERY:
            return {}
        groups = sports.query_groups(words)
        if groups:
            return self._match_aliases(words, groups)
        found: set[int] | None = None
        titled: set[int] | None = None
        for w in sorted(words, key=len, reverse=True):     # longest first: usually the fewest records
            f, t = self._word(w, len(w) >= WHOLE_WORD)
            found = f if found is None else found & f
            titled = t if titled is None else titled & t
            if not found:
                return {}
        phrase = " " + " ".join(words) + ("" if len(words[-1]) >= WHOLE_WORD else " ")
        out = {}
        for rec in found:
            if phrase in f" {self.tnorm[rec]} ":
                out[rec] = 3
            elif rec in titled:
                out[rec] = 2
            elif phrase in f" {self.dnorm[rec]} ":
                out[rec] = 1
        return out

    def _phrase_recs(self, phrase: list[str], prefix_last: bool) -> set[int]:
        found: set[int] | None = None
        for i, w in enumerate(phrase):
            f, _ = self._word(w, prefix_last and i == len(phrase) - 1 and len(w) >= WHOLE_WORD)
            found = f if found is None else found & f
            if not found:
                return set()
        return found or set()

    @staticmethod
    def _where(text: str, phrase: str, prefix_last: bool, entity: sports.Entity | None) -> bool:
        """phrase in text as whole words (the last may be unfinished), and in a context the entity
        allows ("champions league" not after "afc")."""
        hay = f" {text} "
        needle = f" {phrase}" + ("" if prefix_last else " ")
        i = hay.find(needle)
        while i >= 0:
            if entity is None:
                return True
            before = hay[:i].rsplit(" ", 1)[-1] if i else ""
            after = hay[i + len(needle):].split(" ", 1)[0] if not prefix_last else ""
            if before not in entity.not_after and after not in entity.not_before:
                return True
            i = hay.find(needle, i + 1)
        return False

    def _match_aliases(self, words: list[str], groups: list[tuple[int, int, int]]) -> dict[int, int]:
        """Slots: runs of typed words (literal, in order) and named entities (any of their names,
        or programmes whose recognised teams / competition are that entity). Every slot must be in
        the title (3) or every slot in the description (1)."""
        slots: list[tuple] = []
        i = 0
        for start, end, eid in groups:
            if i < start:
                slots.append(("lit", words[i:start], None))
            slots.append(("ent", words[start:end], eid))
            i = end
        if i < len(words):
            slots.append(("lit", words[i:], None))
        last = len(slots) - 1
        masks: list[dict[int, int]] = []
        for si, (kind, typed, eid) in enumerate(slots):
            prefix = si == last
            mask: dict[int, int] = {}
            alts = [(typed, None)]                   # what was typed counts as written, in any context
            if kind == "ent":
                ent = sports.ENTITIES[eid]
                alts += [(p.split(), ent) for p in ent.names if p.split() != typed]
                from_desc = DESC_COMP if ent.kind == "competition" else DESC_TEAMS
                for rec in self.ent_recs.get(eid, ()):   # recognised teams / competition (weak names too)
                    mask[rec] = mask.get(rec, 0) | (2 if self.sp_flags[rec] & from_desc else 1)
            for phrase, ent in alts:
                text = " ".join(phrase)
                pre = prefix and ent is None
                for rec in self._phrase_recs(phrase, pre):
                    if ent is not None and ent.sport and self.sp_flags[rec] & EXPLICIT and \
                            self.sp_sport[rec] != ent.sport:
                        continue                     # "Spurs" in a basketball programme isn't Tottenham
                    m = 1 if self._where(self.tnorm[rec], text, pre, ent) else 0
                    if self._where(self.dnorm[rec], text, pre, ent):
                        m |= 2
                    if m:
                        mask[rec] = mask.get(rec, 0) | m
            if not mask:
                return {}
            masks.append(mask)
        masks.sort(key=len)
        out = {}
        for rec, m in masks[0].items():
            title, desc = bool(m & 1), bool(m & 2)
            for other in masks[1:]:
                o = other.get(rec, 0)
                title, desc = title and bool(o & 1), desc and bool(o & 2)
                if not (title or desc):
                    break
            if title:
                out[rec] = 3
            elif desc:
                out[rec] = 1
        return out

    # ------------------------------------------------------------------ events
    def _group_key(self, rec: int) -> str:
        if self.sp_kind[rec] in (sports.MATCH, sports.EVENT):
            return f"\x00{self.sp_key[rec]}"
        return self.tnorm[rec]

    def event_info(self, rec: int) -> dict:
        """Display fields of a programme: the match's teams as one title ("Arsenal vs Leeds
        United"), its competition and sport."""
        hit = self._info.get(rec)
        if hit is not None:
            return hit
        out = {"title": self.titles[rec], "sub": "", "sport": ""}
        if self.sp_kind[rec]:
            info = sports.classify(self.titles[rec], self.descs[rec], self.sp_ch[rec])
            if info:
                out["title"] = sports.display_title(info, self.titles[rec])
            if self.sp_comp[rec] >= 0:
                out["sub"] = sports.ENTITIES[self.sp_comp[rec]].name
            out["sport"] = sports.SPORT_KEYS.get(self.sp_sport[rec], "")
            self._info[rec] = out
        return out

    def search(self, q: str, now: float | None = None, ahead: float = AHEAD) -> dict:
        """-> {events, broad}. Events (one programme, possibly on many channels), most relevant
        first: on now, then starting within the hour, then later; in each, title matches before
        description ones. broad: the query matched so much ("the") that only what is on now and
        within the hour is listed."""
        now = now or time.time()
        found = self.match(q)
        first = self.first
        broad = sum(first[r + 1] - first[r] for r in found) > BROAD_ROWS
        hi = now + (SOON if broad else ahead)
        by_title: dict[str, list] = {}
        for rec, strength in found.items():
            rows = None
            for k in range(first[rec], first[rec + 1]):
                start, stop = self.occ_start[k], self.occ_stop[k]
                if stop > now and start < hi:
                    if rows is None:
                        rows = by_title.setdefault(self._group_key(rec), [])
                    rows.append((start, stop, self.occ_ch[k], rec, strength))
        events = []
        for gkey, rows in by_title.items():
            window = SPORT_SAME if gkey.startswith("\x00") else SAME_EVENT
            rows.sort()
            # On now: one event per title, whatever start each channel lists. Guides merged from
            # several sources list one match at 11:45, 11:50, 12:15... on the same channel.
            live = [r for r in rows if r[0] <= now]
            until: dict[int, int] = {}
            if live:
                ev = {"rec": live[0][3], "strength": live[0][4], "chs": {}, "times": {}, "recs": set()}
                for start, stop, ch, rec, strength in live:
                    ev["recs"].add(rec)
                    if strength > ev["strength"]:
                        ev["rec"], ev["strength"] = rec, strength
                    ev["chs"].setdefault(self.iids[ch], None)
                    ev["times"][(start, stop)] = ev["times"].get((start, stop), 0) + 1
                    until[ch] = max(until.get(ch, 0), stop)
                ev["start"], ev["stop"] = max(ev.pop("times").items(), key=lambda kv: kv[1])[0]   # the usual listing
                events.append(ev)
            later = [r for r in rows[len(live):] if r[0] >= until.get(r[2], 0)]   # not a repeat listing of what's on now
            if window == SPORT_SAME:
                for cl in sport_clusters(later):
                    ev = {"start": Counter(r[0] for r in cl).most_common(1)[0][0], "stop": max(r[1] for r in cl),
                          "rec": max(cl, key=lambda r: r[4])[3], "strength": max(r[4] for r in cl),
                          "recs": {r[3] for r in cl}, "chs": dict.fromkeys(self.iids[r[2]] for r in cl)}
                    events.append(ev)
                continue
            ev = None
            for start, stop, ch, rec, strength in later:
                if ev is None or start - ev["start"] > window:
                    ev = {"start": start, "stop": stop, "rec": rec, "strength": strength, "chs": {}, "recs": set()}
                    events.append(ev)
                elif strength > ev["strength"]:
                    ev["rec"], ev["strength"] = rec, strength
                ev["recs"].add(rec)
                ev["chs"].setdefault(self.iids[ch], None)
        out = []
        for ev in events:
            start, rec = ev["start"], ev["rec"]
            when = "live" if start <= now else "soon" if start - now <= SOON else "later"
            title, sub = self.titles[rec], ""
            # teams / competition recognised in it (also highlights and shows): for the favourite mark
            fav_ids = list(dict.fromkeys(sports.ENTITIES[t].key for r in ev["recs"] if self.sp_kind[r]
                                         for t in (self.sp_a[r], self.sp_b[r], self.sp_comp[r]) if t >= 0))
            if self.sp_kind[rec] in (sports.MATCH, sports.EVENT):
                # one match under several titles: show it as "Arsenal vs Leeds United", competition below
                best = max(ev["recs"], key=lambda r: (bool(self.sp_flags[r] & EXPLICIT),
                                                      (self.sp_a[r] >= 0) + (self.sp_b[r] >= 0),
                                                      not self.sp_flags[r] & DESC_TEAMS))
                info = self.event_info(best)
                title, sub = info["title"], info["sub"]
            out.append({"when": when, "title": title, "sub": sub, "fav_ids": fav_ids, "desc": self.descs[rec], "start": start,
                        "stop": ev["stop"], "strength": ev["strength"], "ids": list(ev["chs"]), "rec": rec})
        rank = {"live": 0, "soon": 1, "later": 2}
        out.sort(key=lambda e: (rank[e["when"]], e["strength"] < 2, e["start"] if e["when"] != "live" else 0,
                                -len(e["ids"]), e["title"]))
        return {"events": out, "broad": broad}


    # ------------------------------------------------------------------ the Sports view
    def sports(self, now: float, lo: float, hi: float) -> list[dict]:
        """Matches and events airing between lo and hi (and, when lo is now, on now), one per
        event: the same key (teams, or F1 session...) on channels whose listings overlap is one
        event, whatever each broadcaster titles it or when it says it starts. An airing after a
        whole earlier one (also one that ended in the last 3 h) is a replay. Shows (highlights,
        build-up...) are not here."""
        return self._events(self.sport_recs, now, lo, hi)

    def for_you(self, favourites: list[str], now: float, lo: float, hi: float) -> list[dict]:
        """sports() limited to events with a favourite team or competition (stable ids, as
        sports.BY_ID_KEY). Each event once, however many favourites it has: the favourites lead
        to event keys, and the events are built from those keys alone (index lookups, no scan)."""
        ents = {sports.BY_ID_KEY[k].id for k in favourites if k in sports.BY_ID_KEY}
        keys = {self.sp_key[r] for e in ents for r in self.ent_recs.get(e, ()) if self.sp_kind[r] in (sports.MATCH, sports.EVENT)}
        recs = [r for k in keys for r in self.key_recs.get(k, ())]
        return self._events(recs, now, lo, hi)

    def _events(self, recs, now: float, lo: float, hi: float) -> list[dict]:
        by_key: dict[int, list] = {}
        first = self.first
        for rec in recs:
            for k in range(first[rec], first[rec + 1]):
                start = self.occ_start[k]
                if start < hi:
                    by_key.setdefault(self.sp_key[rec], []).append((start, self.occ_stop[k], self.occ_ch[k], rec))
        out = []
        for kid, rows in by_key.items():
            rows.sort()
            ended = [r for r in rows if r[1] <= now]
            live = [r for r in rows if r[0] <= now < r[1]]
            whole_before = any(r[1] - r[0] >= FULL_AIRING for r in ended)
            until: dict[int, int] = {}
            if live and lo <= now:
                out.append(self._sport_event(self.keys[kid], live, now, earlier=whole_before))
            for start, stop, ch, rec in live:
                until[ch] = max(until.get(ch, 0), stop)
            clusters = sport_clusters([r for r in rows if r[0] > now and r[0] >= until.get(r[2], 0)])
            whole = whole_before or any(r[1] - r[0] >= FULL_AIRING for r in live)
            for cl in clusters:
                if Counter(r[0] for r in cl).most_common(1)[0][0] >= lo:
                    out.append(self._sport_event(self.keys[kid], cl, now, earlier=whole))
                whole = whole or any(r[1] - r[0] >= FULL_AIRING for r in cl)
        out.sort(key=lambda e: (-e["score"], e["start"]))
        return out

    def _sport_event(self, key: str, rows: list, now: float, earlier: bool) -> dict:
        recs = Counter(r[3] for r in rows)
        flags = 0
        for rec in recs:
            flags |= self.sp_flags[rec]
        explicit = Counter(self.sp_sport[r] for r in recs if self.sp_flags[r] & EXPLICIT)
        sport = (explicit or Counter(self.sp_sport[r] for r in recs)).most_common(1)[0][0]
        comps = [self.sp_comp[r] for r in recs if self.sp_comp[r] >= 0]
        comp = max(comps, key=lambda c: sports.ENTITIES[c].priority) if comps else -1
        # shown title: a programme naming both teams, else the one on most channels
        best = max(recs, key=lambda r: (self.sp_sport[r] == sport, (self.sp_a[r] >= 0) + (self.sp_b[r] >= 0),
                                        not self.sp_flags[r] & DESC_TEAMS, recs[r]))
        info = self.event_info(best)
        start, stop = Counter((r[0], r[1]) for r in rows).most_common(1)[0][0]
        ids = list(dict.fromkeys(self.iids[r[2]] for r in rows))
        dated = [self.sp_dated[r] for r in recs if self.sp_dated[r]]
        air = time.gmtime(start)
        air_day = air.tm_year * 10000 + air.tm_mon * 100 + air.tm_mday
        old_date = bool(dated) and all(d < air_day - 1 for d in dated)     # "HULL CITY - EVERTON 11/10/26" on the 13th
        # a broadcaster saying LIVE wins over the guess from earlier airings (which can be previews or
        # a guide's other source with the wrong time)
        replay = (earlier or old_date or all(self.sp_flags[r] & REPLAY for r in recs)) and not flags & LIVE_TAG
        known = (self.sp_a[best] >= 0) + (self.sp_b[best] >= 0)
        prio = sports.ENTITIES[comp].priority if comp >= 0 else 0
        score = prio + 12 * known + min(len(ids), 15) + 5 * bool(flags & LIVE_TAG) - 40 * replay - 25 * bool(flags & YOUTH)
        when = "live" if start <= now else "soon" if start - now <= SPORTS_SOON else "later"
        teams = list(dict.fromkeys(sports.ENTITIES[t].key for r in recs for t in (self.sp_a[r], self.sp_b[r]) if t >= 0))
        return {"key": key, "sport": sports.SPORT_KEYS.get(sport, "other"), "kind": "match" if self.sp_kind[best] == sports.MATCH else "event",
                "title": info["title"], "comp": sports.ENTITIES[comp].name if comp >= 0 else "",
                "comp_id": sports.ENTITIES[comp].key if comp >= 0 else "", "teams": teams,
                "start": start, "stop": stop, "when": when, "ids": ids, "replay": replay,
                "women": bool(flags & WOMEN), "youth": bool(flags & YOUTH), "live_tag": bool(flags & LIVE_TAG),
                "score": score}

# ------------------------------------------------------------------ channels of one event
def channel_groups(ids: list[str], vindex: livemeta.VariantIndex, meta_of: Callable[[str], dict | None],
                   caps: dict) -> list[dict]:
    """The channels showing one event, the best one for this device first.

    Versions of one channel (livemeta.VariantIndex: the same channel at other qualities) are one
    entry. Its quality is the best version this device plays smoothly among those whose format is
    already known (meta_of: the live-meta cache, learned from playing and from 1.4.0's probes);
    nothing is probed for a search. When no version's format is known yet, the quality its name
    suggests ("ᴴᴰ", "4K") is shown as an estimate. Playing an entry goes through the normal live
    path, which picks the best smooth version itself (and may probe then, as it always does).

    -> [{id, ids, versions, label, est, ok, conv}] where ok is 2 smooth, -1 not known yet, 1 may not
    be smooth, 0 not playable here (the player offers another version). conv: the original's label
    when the only way to play it here is the Pi's deinterlace (label is then that 720p50 output)."""
    groups: list[dict] = []
    for iid in ids:
        vs = set(vindex.variants(iid))
        inf = vindex.info.get(iid) or {}
        # Showing the same programme at the same time, the same name with the same prefix is the same
        # channel even where VariantIndex won't say so in general ("8K: beIN SPORTS 1 ᴿᴬᵂ" / "SD": "8K:"
        # names exist for several countries, so it doesn't pair them for every purpose).
        same = (inf.get("base"), inf.get("prefix")) if inf.get("base") else None
        for g in groups:
            if iid in g["all"] or vs & set(g["ids"]) or (same and same in g["names"]):
                g["ids"].append(iid)
                g["all"] |= vs
                if same:
                    g["names"].add(same)
                break
        else:
            groups.append({"ids": [iid], "all": vs, "names": {same} if same else set()})
    # 4K channels are HEVC in practice: on a device without smooth 4K HEVC a "4K" name says nothing
    uhd_ok = any(h >= 2160 and f == "s" for c in ("hevc", "hevc10") for h, _, f in caps.get(c, []))
    out = []
    for g in groups:
        sport = any((vindex.info.get(i) or {}).get("sport") for i in g["ids"])
        best = None                                  # (playable, score, meta)
        unknown_h = 0
        own: dict[str, float] = {}
        for v in g["all"]:
            m = meta_of(v)
            if not m or not m.get("h"):
                hint = livemeta.hint_height((vindex.info.get(v) or {}).get("hints", []))
                unknown_h = max(unknown_h, 1 if not hint or (hint >= 2160 and not uhd_ok) else hint)
                continue
            p, sc = livemeta.playable(m, caps), livemeta.score(m, sport)
            if v in g["ids"] and p == 2:
                own[v] = sc
            if best is None or (p, sc) > best[:2]:
                best = (p, sc, m)
        if best and best[0] == 2:
            ok, sc, label, est = 2, best[1], livemeta.label(best[2]), False
        elif unknown_h:
            ok, sc, est = -1, livemeta.score({"h": unknown_h}, sport) if unknown_h > 1 else 0.0, True
            label = HINT_LABEL.get(unknown_h, "")
        else:
            ok, sc, label, est = best[0], best[1], livemeta.label(best[2]), False
        conv = ""
        if ok == 0 and best[2].get("interlaced") and \
                livemeta.predicted_transcode_speed(best[2]) >= livemeta.TRANSCODE_MARGIN:
            conv, label = label, "720p 50fps · H.264"        # what the player's deinterlace gives this device
        def own_rank(i: str) -> tuple:
            hint = livemeta.hint_height((vindex.info.get(i) or {}).get("hints", []))
            return (own.get(i, -1.0), hint)
        rep = max(g["ids"], key=own_rank) if len(g["ids"]) > 1 else g["ids"][0]
        out.append({"id": rep, "ids": g["ids"], "versions": len(g["all"]), "label": label, "est": est, "ok": ok,
                    "conv": conv, "_rank": ({2: 0, -1: 1, 1: 2, 0: 3}[ok], -sc)})
    out.sort(key=lambda e: e["_rank"])
    for e in out:
        del e["_rank"]
    return out
