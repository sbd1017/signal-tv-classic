"""Root-side helper for the `iptv` command (run via sudo). Values arrive on stdin,
never in argv, so secrets do not show up in `ps` or shell history.

  python -m tvapp.cli set KEY        < value
  python -m tvapp.cli show           (secrets masked)
  python -m tvapp.cli check-url      < url
"""
from __future__ import annotations

import sys
from urllib.parse import urlsplit

from .config import CONFIG_PATH, KEYS, SECRET_KEYS, read_env, write_env_value


def mask_url(value: str) -> str:
    try:
        p = urlsplit(value)
    except ValueError:
        return "(invalid)"
    if not p.scheme:
        return "(set)"
    host = p.hostname or "?"
    return f"{p.scheme}://{host}{':' + str(p.port) if p.port else ''}/…  (path/query hidden)"


def show() -> None:
    env = read_env()
    print(f"# {CONFIG_PATH}")
    for k in KEYS:
        v = env.get(k, "")
        if not v:
            shown = "(empty)"
        elif k in ("XTREAM_PASSWORD", "XTREAM_USERNAME"):
            shown = "******** (set)"
        elif k in SECRET_KEYS:
            shown = mask_url(v)
        else:
            shown = v
        print(f"{k:24} {shown}")


def format_status() -> None:
    import json
    import time
    s = json.load(sys.stdin)

    def ago(t):
        return "never" if not t else f"{int((time.time() - t) // 60)} min ago"

    c, e = s["counts"], s["epg"]
    print(f"provider:      {s['provider']}{'' if s['configured'] else ' (NOT configured)'}")
    print(f"catalogue:     {c['live']} live, {c['movies']} movies, {c['series']} series, {c['episodes']} episodes "
          f"(updated {ago(s['updated'])}{', refreshing' if s['refreshing'] else ''})")
    if s["error"]:
        print(f"last error:    {s['error']}")
    r = s.get("refresh") or {}
    if r.get("last_ok"):
        ok = r["last_ok"]
        print(f"last refresh:  {ok['changes']} ({ago(ok['t'])})"
              + (f"; warnings: {'; '.join(ok['warnings'])}" if ok.get("warnings") else ""))
    last = r.get("last")
    if last and last["status"] != "accepted":
        why = "; ".join(last.get("anomalies") or []) or last.get("error", "")
        print(f"REFRESH {last['status'].upper()} {ago(last['t'])}: {why}")
        print(f"               previous catalogue kept; {r.get('failing', 1)} in a row"
              + (f"; next try {time.strftime('%H:%M', time.localtime(r['next_try']))}" if r.get("next_try") else "")
              + "; if the change is real: iptv refresh --accept-changes")
    if r.get("accept_next"):
        print("               the next refresh will be accepted whatever it looks like (--accept-changes)")
    print(f"guide:         {e['channels']} channels matched (updated {ago(e['updated'])})"
          f"{' - ' + e['error'] if e['error'] else ''}")
    print(f"streams:       {len(s['jobs'])} active (limits: {s['limits']['live']} live, {s['limits']['vod']} remux)")
    if s.get("memory"):
        m = s["memory"]
        ev = m.get("last_event")
        guard = ("never triggered" if not ev else
                 f"LAST {ev['action'].upper()} {time.strftime('%Y-%m-%d %H:%M', time.localtime(ev['at']))}: {ev['reason']}"
                 " (journalctl -u signal-tv-classic | grep 'MEMORY GUARD')")
        print(f"memory:        {m['total_mb']} MB (app {m['app_mb']} + ffmpeg {m['ffmpeg_mb']}), guard limit "
              f"{m['limit_mb']} MB; guard {guard}")
    if s.get("state"):
        st = s["state"]
        print(f"saved state:   {st['favourites']} favourites, {st['progress']} in progress, {st['recent']} recent, "
              f"{st['preferences']} preferences (last change {ago(st['last_change'])})")
    h = s.get("health")
    if h:
        al = h["alerts"]
        print(f"health:        {'OK' if h['ok'] else 'NEEDS ATTENTION - ' + '; '.join(h['problems'])}")
        print(f"alerts:        Telegram {'on' if al['configured'] else 'OFF (no /etc/signal-tv-classic/telegram.env)'}"
              f"{', last sent ' + ago(al['last_sent']) if al['last_sent'] else ''}"
              f"{'; open: ' + ', '.join(p['title'] for p in al['open']) if al['open'] else ''}")
        st = h["streams"]
        if st["starts_1h"]:
            print(f"stream starts: {st['starts_1h']} in the last hour, {st['failures_1h']} failed"
                  + (f" (last: {st['last_failure']['message']})" if st.get("last_failure") else ""))
    for j in s["jobs"]:
        print(f"  {j['job']} {j['kind']:4} {j['state']:8} viewers={j['viewers']} age={j['age']}s"
              f"{' transcoding' if j['transcode'] else ''}")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    cmd = sys.argv[1]
    if cmd == "format-status":
        format_status()
        return 0
    if cmd == "show":
        show()
        return 0
    if cmd == "check-url":
        v = sys.stdin.readline().strip()
        p = urlsplit(v)
        ok = p.scheme in ("http", "https") and bool(p.hostname)
        return 0 if ok else 1
    if cmd == "set" and len(sys.argv) == 3:
        key = sys.argv[2]
        if key not in KEYS:
            print(f"unknown key {key}", file=sys.stderr)
            return 2
        value = sys.stdin.readline().rstrip("\r\n")
        write_env_value(key, value)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
