"""Is the provider serving streams? Every play attempt reports here.

"Persistent provider failure" (alert key provider-streams) means, within the last 15 minutes:
at least 5 failed starts, of at least 2 different titles/channels, spread over 10+ minutes,
and not one successful start. One dead channel tried five times is not a provider outage;
a working start clears it at once.
"""
from __future__ import annotations

import time
from collections import deque

from .alerts import clean

WINDOW = 15 * 60
MIN_FAILS = 5
MIN_ITEMS = 2
MIN_SPAN = 10 * 60
KEEP = 200


class ProviderHealth:
    def __init__(self, clock=time.time):
        self.clock = clock
        self.events: deque = deque(maxlen=KEEP)      # (t, ok, item_id, message)
        self.down = False

    def record(self, ok: bool, item_id: str, message: str = "") -> str | None:
        """Returns "down" when the provider has just become persistently failing, "up" when it
        recovered from that, else None."""
        now = self.clock()
        self.events.append((now, ok, item_id, clean(message, 200)))      # shown in status/diagnostics: redacted
        if ok:
            if self.down:
                self.down = False
                return "up"
            return None
        recent = [e for e in self.events if now - e[0] <= WINDOW]
        fails = [e for e in recent if not e[1]]
        if (not self.down and len(fails) >= MIN_FAILS and not any(e[1] for e in recent)
                and len({e[2] for e in fails}) >= MIN_ITEMS and now - fails[0][0] >= MIN_SPAN):
            self.down = True
            return "down"
        return None

    def summary(self) -> dict:
        now = self.clock()
        recent = [e for e in self.events if now - e[0] <= 3600]
        fails = [e for e in recent if not e[1]]
        return {"down": self.down, "starts_1h": len(recent), "failures_1h": len(fails),
                "last_failure": {"t": int(fails[-1][0]), "message": fails[-1][3]} if fails else None}

    def last_errors(self, n: int = 5) -> list[dict]:
        return [{"t": int(t), "item": i, "message": m} for t, ok, i, m in list(self.events)[::-1] if not ok][:n]
