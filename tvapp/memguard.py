"""TV-only memory safety net.

This Pi boots with the kernel memory cgroup disabled, so systemd's MemoryMax=2G for this
unit is not enforced (enabling it Pi-wide would also enforce the limits of 14 unrelated
services - see docs/INFRA-memory-cgroups.md). This guard does the job for the TV app only:

* Every CHECK_SECONDS it adds up the resident memory of this process and its ffmpeg jobs.
* Over the limit (TV_MEMORY_LIMIT_MB, default 2048) without a break for SUSTAIN_SECONDS, or
  over HARD_FACTOR x the limit at any single check (a runaway can fill RAM fast) -> restart.
  Legitimate peaks never trigger it: a catalogue refresh peaks at ~1.0-1.2 GB for seconds.
* "Restart" = log the reason with a breakdown, record it in memory-guard.json, stop the
  ffmpeg jobs cleanly and exit with EXIT_CODE; systemd (Restart=on-failure) starts it again.
* Budget: at most MAX_RESTARTS in WINDOW_SECONDS. After that it only logs (CRITICAL, at most
  every NAG_SECONDS) until the window has passed, so it can never cause a restart loop.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

CHECK_SECONDS = 10
SUSTAIN_SECONDS = 60
HARD_FACTOR = 1.5
MAX_RESTARTS = 3
WINDOW_SECONDS = 6 * 3600
NAG_SECONDS = 1800
KEEP_EVENTS = 20
EXIT_CODE = 75                     # EX_TEMPFAIL: "try again" - systemd restarts on any failure code


def rss_mb(pid: int | str = "self") -> float:
    """Resident memory of one process in MB (0 if it has gone)."""
    try:
        with open(f"/proc/{pid}/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except (FileNotFoundError, ProcessLookupError, ValueError):
        pass
    return 0.0


class MemoryGuard:
    def __init__(self, limit_mb: float, path: Path, clock=time.time):
        self.limit = float(limit_mb)
        self.path = path
        self.clock = clock
        self.over_since: float | None = None
        self.last_nag = 0.0
        self.events: list[dict] = self._load()

    def _load(self) -> list[dict]:
        try:
            events = json.loads(self.path.read_text())
            return [e for e in events if isinstance(e, dict) and "at" in e][-KEEP_EVENTS:]
        except (FileNotFoundError, ValueError, TypeError):
            return []

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.events, indent=1))
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def recent_restarts(self, now: float | None = None) -> int:
        now = self.clock() if now is None else now
        return sum(1 for e in self.events if e.get("action") == "restart" and now - e["at"] < WINDOW_SECONDS)

    def observe(self, total_mb: float) -> tuple[str, str]:
        """-> (action, reason). action: "ok" | "over" (counting) | "restart" | "suppressed"."""
        now = self.clock()
        if total_mb <= self.limit:
            self.over_since = None
            return "ok", ""
        if self.over_since is None:
            self.over_since = now
        if total_mb > self.limit * HARD_FACTOR:
            reason = f"{total_mb:.0f} MB is over {HARD_FACTOR:g}x the {self.limit:.0f} MB limit"
        elif now - self.over_since >= SUSTAIN_SECONDS:
            reason = f"{total_mb:.0f} MB, over the {self.limit:.0f} MB limit for {now - self.over_since:.0f}s"
        else:
            return "over", ""
        if self.recent_restarts(now) >= MAX_RESTARTS:
            if now - self.last_nag < NAG_SECONDS:
                return "over", ""
            self.last_nag = now
            return "suppressed", reason
        return "restart", reason

    def record(self, action: str, reason: str, details: dict) -> dict:
        event = {"at": self.clock(), "action": action, "reason": reason, **details}
        self.events = (self.events + [event])[-KEEP_EVENTS:]
        try:
            self._save()
        except OSError:
            pass                                      # the log line still says what happened
        return event

    def summary(self) -> dict:
        last = next((e for e in reversed(self.events) if e.get("action") in ("restart", "suppressed")), None)
        return {"limit_mb": int(self.limit), "restarts_last_6h": self.recent_restarts(),
                "last_event": last and {k: last[k] for k in ("at", "action", "reason") if k in last}}
