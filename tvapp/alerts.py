"""Telegram alerts for things that need the owner's attention.

Sent with the Pi's existing Telegram bot; its token and chat id are in
/etc/signal-tv-classic/telegram.env (TELEGRAM_BOT_TOKEN=, TELEGRAM_CHAT_ID=; root:signal-tv-classic 0640),
never in this repository or in a message. With no file, alerts are off and only logged.

What alerts (key - where it is raised):
  refresh-rejected   a catalogue refresh looked wrong and was not used     (tvapp/catalog.py)
  refresh-failing    3+ catalogue refreshes in a row failed                 (tvapp/catalog.py)
  provider-streams   the provider keeps refusing streams (5+ failures, none working, 10+ min)
  memory-guard       the TV-only memory guard restarted (or would restart) the service
  unit:<name>        a systemd unit failed: nightly backup, Google Drive upload, the service
                     itself giving up (python -m tvapp.alerts failed:<unit>, via OnFailure=)

No spam: the first occurrence of a key is sent at once; while it keeps happening, repeats are
sent no sooner than 1 h, then 4 h, 12 h, then daily ("happened 7 times since 02:10"). When the
condition clears, one "resolved" message is sent - only if an alert went out. At most 6
messages an hour in total. Text is redacted (provider credentials, URLs, tokens) before sending.
State: alerts.json in the state directory (shared by the app and the alert unit).
"""
from __future__ import annotations

import json
import os
import re
import socket
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from .util import log, redact

CONFIG = Path(os.environ.get("TV_TELEGRAM_FILE", "/etc/signal-tv-classic/telegram.env"))
STATE = Path(os.environ.get("TV_STATE_DIR", "/var/lib/signal-tv-classic")) / "alerts.json"
BACKOFF = (3600, 4 * 3600, 12 * 3600, 24 * 3600)
MAX_PER_HOUR = 6
_URL = re.compile(r"\b(?:https?|rtmp|rtsp)://\S+", re.I)
_TOKEN = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b")
_CRED_PATH = re.compile(r"/(live|movie|series)/[^/\s]+/[^/\s]+/", re.I)
UNIT_TEXT = {
    "signal-tv-classic-backup.service": ("Nightly backup failed",
                                  "The TV backup did not complete. Check: journalctl -u signal-tv-classic-backup -n 30"),
    "signal-tv-classic-backup-upload.service": ("Google Drive backup upload failed",
                                         "The user-state backup did not reach Google Drive. 'invalid_grant' in "
                                         "journalctl -u signal-tv-classic-backup-upload means the Google sign-in expired "
                                         "(rclone config reconnect gdrive:)."),
    "signal-tv-classic.service": ("TV service stopped",
                           "systemd gave up restarting signal-tv-classic. Check: iptv logs 50"),
}


def clean(text: str, limit: int = 900) -> str:
    """Remove anything secret-looking: known secrets, URLs, bot tokens, credential paths."""
    text = redact(str(text))
    text = _URL.sub("<link>", text)
    text = _TOKEN.sub("<token>", text)
    text = _CRED_PATH.sub("/<…>/", text)
    return text[:limit]


def load_config(path: Path = CONFIG) -> tuple[str, str] | None:
    try:
        lines = path.read_text().splitlines()
    except (FileNotFoundError, PermissionError):
        return None
    env = {}
    for line in lines:
        k, sep, v = line.strip().partition("=")
        if sep and not k.startswith("#"):
            env[k.strip()] = v.strip().strip('"').strip("'")
    token, chat = env.get("TELEGRAM_BOT_TOKEN", ""), env.get("TELEGRAM_CHAT_ID", "")
    return (token, chat) if token and chat else None


def post(config: tuple[str, str], text: str, timeout: float = 10) -> None:
    """Blocking send (the app calls it in a worker thread). Errors name the type only: the
    exception text could contain the request URL, which contains the token."""
    token, chat = config
    body = urllib.parse.urlencode({"chat_id": chat, "text": text, "disable_web_page_preview": "true"}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                raise RuntimeError(f"Telegram answered HTTP {r.status}")
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Telegram send failed ({type(e).__name__})") from None


class Alerts:
    def __init__(self, state_path: Path | None = None, config_path: Path | None = None, clock=time.time, sender=None):
        self.path = state_path or STATE
        self.config_path = config_path or CONFIG
        self.clock = clock
        self.sender = sender or (lambda config, text: post(config, text))

    # ------------------------------------------------------------ state
    def _load(self) -> dict:
        try:
            st = json.loads(self.path.read_text())
            if isinstance(st, dict):
                st.setdefault("keys", {})
                st.setdefault("sent", [])
                return st
        except (FileNotFoundError, ValueError, PermissionError):
            pass
        return {"keys": {}, "sent": []}

    def _save(self, st: dict) -> None:
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(st, separators=(",", ":")))
        os.replace(tmp, self.path)

    @property
    def configured(self) -> bool:
        return load_config(self.config_path) is not None

    # ------------------------------------------------------------ rules
    def _due(self, k: dict, now: float) -> bool:
        if not k.get("sent_at"):
            return True
        wait = BACKOFF[min(k.get("sends", 1) - 1, len(BACKOFF) - 1)]
        return now - k["sent_at"] >= wait

    def _deliver(self, st: dict, text: str, now: float) -> bool:
        st["sent"] = [t for t in st["sent"] if now - t < 3600]
        if len(st["sent"]) >= MAX_PER_HOUR:
            log.warning("alert not sent (more than %d this hour): %s", MAX_PER_HOUR, text.splitlines()[0])
            return False
        config = load_config(self.config_path)
        if not config:
            log.info("alert (Telegram not configured): %s", text.replace("\n", " | "))
            return False
        try:
            self.sender(config, text)
        except Exception as e:  # noqa: BLE001
            log.warning("alert could not be sent: %s", e)
            return False
        st["sent"].append(now)
        return True

    def notify(self, text: str) -> bool:
        """A plain message (match alerts, tvapp/sportsalerts.py): same bot, redaction and hourly cap,
        no repeat/resolve bookkeeping. True if it went out."""
        st = self._load()
        ok = self._deliver(st, clean(text, 3500), self.clock())
        self._save(st)
        return ok

    def raise_(self, key: str, title: str, detail: str = "") -> bool:
        """The condition `key` is happening (again). Returns True if a message went out."""
        now = self.clock()
        st = self._load()
        k = st["keys"].setdefault(key, {"first": now, "count": 0, "sends": 0})
        if not k.get("open"):
            k.update(first=now, count=0, sends=0, sent_at=0, open=True)
        k["count"] += 1
        k["last"] = now
        k["title"], k["detail"] = clean(title, 120), clean(detail, 400)
        sent = False
        if self._due(k, now):
            extra = ""
            if k["count"] > 1:
                extra = f"\n(happened {k['count']} times since {time.strftime('%d %b %H:%M', time.localtime(k['first']))})"
            sent = self._deliver(st, f"📺 Signal TV Classic: {clean(title, 120)}\n{clean(detail)}{extra}", now)
            if sent:
                k["sent_at"] = now
                k["sends"] = k.get("sends", 0) + 1
        self._save(st)
        return sent

    def resolve(self, key: str, detail: str = "") -> bool:
        """The condition cleared. Sends one 'resolved' note if an alert had been sent for it."""
        now = self.clock()
        st = self._load()
        k = st["keys"].get(key)
        if not k or not k.get("open"):
            return False
        k["open"] = False
        k["resolved"] = now
        sent = False
        if k.get("sends"):
            sent = self._deliver(st, f"✅ Signal TV Classic: resolved - {k.get('title', key)}"
                                     f"{chr(10) + clean(detail) if detail else ''}", now)
        self._save(st)
        return sent

    def open_problems(self) -> list[dict]:
        st = self._load()
        return [{"key": key, "title": k.get("title", key), "since": int(k.get("first", 0)), "count": k.get("count", 0)}
                for key, k in st["keys"].items() if k.get("open")]

    def summary(self) -> dict:
        st = self._load()
        last = max((t for t in st["sent"]), default=0)
        return {"configured": self.configured, "open": self.open_problems(), "last_sent": int(last)}


# ------------------------------------------------------------------ systemd hooks
def main(argv: list[str]) -> int:
    """signal-tv-classic-alert@failed:<unit>.service / @ok:<unit>.service, and `test`."""
    if not argv:
        print("usage: python -m tvapp.alerts failed:UNIT | ok:UNIT | test", file=sys.stderr)
        return 2
    a = Alerts()
    arg = argv[0]
    if arg == "test":
        cfg = load_config()
        if not cfg:
            print(f"Telegram is not configured ({CONFIG} missing or incomplete)")
            return 1
        post(cfg, f"📺 Signal TV Classic: test alert from {socket.gethostname()} - alerts are working.")
        print("sent")
        return 0
    state, _, unit = arg.partition(":")
    if unit and "." not in unit:                    # started by hand as @failed:<name>.service
        unit += ".service"
    title, detail = UNIT_TEXT.get(unit, (f"{unit} failed", f"Check: journalctl -u {unit} -n 30"))
    if state == "failed":
        a.raise_(f"unit:{unit}", title, detail)
    elif state == "ok":
        a.resolve(f"unit:{unit}", "It ran successfully this time.")
    else:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
