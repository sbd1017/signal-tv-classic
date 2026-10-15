"""Telegram alerts shortly before a favourite team's or competition's match starts.

Off until the viewer picks a lead time (Sports > Favourites: off / 15 / 30 / 60 min; synced
preference "sportsAlerts"). Favourites are the synced "sfav" rows (stable ids, tvapp/sports.py).

Every minute the server asks the already-built guide index for the favourites' events starting
soon (epgsearch.Index.for_you: index lookups only, nothing re-read or re-classified) and sends
one Telegram message for everything due, through the existing alert bot (tvapp/alerts.py, its
hourly cap included). Each event is the de-duplicated one, so 17 broadcasters make one alert.

Never twice: an alert's id is the event's key (its teams' stable ids, or the F1 session) plus its
kick-off day, recorded in state.db (sports_alerts) before the message is sent. A restart, a guide
rebuild or refresh, a kick-off moved later, or the same match listed again by another source
can't repeat it; if Telegram refuses the message the record is withdrawn and the next minute
tries again. Times come from the current index each minute, so a kick-off moved earlier is
alerted at its new time; an event that disappears is simply never due.

Only what the guide shows is live: a broadcaster's LIVE tag, or at least two broadcasters showing
it at the same time. A delayed re-run on one channel with no marker ("Manchester United vs
Southampton" at 10:00 on a Tuesday) looks exactly like a match otherwise; real live matches are
nearly always carried by several broadcasters or tagged LIVE.

Not alerted: replays (a later airing after a whole earlier one, a dated title, replay words),
youth and women's games, highlights / build-up (never events), and F1 practice.
"""
from __future__ import annotations

import time

LEADS = ("15", "30", "60")                  # minutes; anything else (default "off") means off
MIN_BROADCASTERS = 2                        # without a LIVE tag, it must be on at least this many
CHECK_EVERY = 60
F1_ALERTED = {"f1|race", "f1|qualifying", "f1|sprint", "f1|sprint-qualifying"}
ICON = {"football": "⚽", "f1": "\U0001f3ce", "mma": "\U0001f94a", "boxing": "\U0001f94a",
        "basketball": "\U0001f3c0", "nfl": "\U0001f3c8", "tennis": "\U0001f3be", "cricket": "\U0001f3cf",
        "rugby": "\U0001f3c9"}


def lead_minutes(setting) -> int:
    return int(setting) if str(setting) in LEADS else 0


def alert_id(ev: dict) -> str:
    """The event's key plus its kick-off day (local): one alert per match, whatever its time."""
    return f"{ev['key']}@{time.strftime('%Y%m%d', time.localtime(ev['start']))}"


def worth_alerting(ev: dict) -> bool:
    if ev["replay"] or ev["youth"] or ev["women"]:
        return False
    if ev["kind"] == "match":
        return True
    return ev["key"] in F1_ALERTED or ev["sport"] == "mma"


def due(events: list[dict], now: float, lead_s: int) -> list[dict]:
    out = [e for e in events if 0 < e["start"] - now <= lead_s and worth_alerting(e)]
    return sorted(out, key=lambda e: (e["start"], -e.get("score", 0)))


def message(events: list[dict], now: float, broadcasters) -> str:
    """'Arsenal vs Barcelona starts in 15 minutes / Champions League · 20:00 / 17 broadcasters
    available' per event. No scores, nothing about the result."""
    blocks = []
    for e in events:
        mins = max(1, round((e["start"] - now) / 60))
        head = f"{ICON.get(e['sport'], '')} {e['title']} starts in {mins} minute{'s' if mins != 1 else ''}".strip()
        when = time.strftime("%H:%M", time.localtime(e["start"]))
        n = broadcasters(e)
        blocks.append("\n".join([head, " · ".join(x for x in (e["comp"], when) if x),
                                 f"{n} broadcaster{'s' if n != 1 else ''} available"]))
    return "\n\n".join(blocks)


class SportsAlerts:
    def __init__(self, store, alerts, clock=time.time):
        self.store, self.alerts, self.clock = store, alerts, clock
        self.last: dict = {"checked": 0, "sent": 0, "due": 0, "note": "not checked yet"}

    def check(self, idx, broadcasters) -> dict:
        """One periodic check. broadcasters(event) -> how many channels show it (for the text)."""
        now = self.clock()
        lead = lead_minutes(self.store.pref("sportsAlerts", "off"))
        self.last.update(checked=int(now), due=0, ms=0)
        if not lead:
            self.last["note"] = "alerts are off"
            return self.last
        favs = list(self.store.values("sfav"))
        if not favs:
            self.last["note"] = "no favourites"
            return self.last
        if idx is None:
            self.last["note"] = "guide not loaded yet"
            return self.last
        t0 = time.perf_counter()
        events = idx.for_you(favs, now, now, now + lead * 60 + 120)
        todo = {alert_id(e): e for e in due(events, now, lead * 60)
                if e.get("live_tag") or broadcasters(e) >= MIN_BROADCASTERS}
        sent = self.store.alerts_sent(list(todo))
        fresh = [e for i, e in todo.items() if i not in sent]
        self.last.update(due=len(todo), ms=round((time.perf_counter() - t0) * 1000, 1))
        if not fresh:
            self.last["note"] = "nothing due"
            return self.last
        if not self.alerts.configured:
            self.last["note"] = "Telegram is not set up"
            return self.last
        text = message(fresh, now, broadcasters)
        ids = [alert_id(e) for e in fresh]
        self.store.record_alerts(ids, text)            # before sending: a crash can't repeat it
        if self.alerts.notify(text):
            self.last.update(sent=int(now), note=f"sent {len(ids)}", last_text=text.splitlines()[0])
        else:
            self.store.forget_alerts(ids)               # refused / rate-limited: try again next minute
            self.last["note"] = "Telegram refused it; retrying"
        return self.last
