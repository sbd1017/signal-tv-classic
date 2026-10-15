"""Backups of Signal TV Classic. Run by signal-tv-classic-backup.service (daily timer; `iptv backup` starts it now):

1. as signal-tv-classic:  python -m tvapp.backup --state-snapshot [--keep 14]
   Consistent copy of the state database (+ readable JSON) into /var/lib/signal-tv-classic/backups.
   Done as the user that owns the database, so SQLite never leaves root-owned files behind.
2. as root:        python -m tvapp.backup [--keep 30] [--out /var/backups/signal-tv-classic]
   One archive with everything needed to rebuild on a new Pi, mode 0640 root:pi so the
   owner can copy it off the Pi:

    user-state/state.sqlite3   favourites, progress, recent, last channel, preferences, sports favourites,
                               match-alert setting and sent-alert record (newest snapshot)
    user-state/state.json      the same as readable JSON (also importable in the UI)
    etc/signal-tv-classic/tv.env      provider settings INCLUDING credentials - keep the archive private
    etc/signal-tv-classic/telegram.env  Telegram alert bot token + chat id (if set up)
    etc/systemd/system/...     signal-tv-classic, demo, backup and alert units
    etc/nginx/...              /tv snippets, the site config that includes them, the method map
    usr/local/bin/iptv
    code/signal-tv-classic.bundle     git history of the app (clone it to restore the source)
    code/opt-signal-tv-classic/       the deployed code exactly as running (no venv, no demo media)
    RESTORE.txt

   It also keeps a plain copy of that user-state snapshot in <out>/user-state/ (keeps 30).
3. as pi:     python -m tvapp.backup --upload gdrive:signal-tv-classic-backups
   (signal-tv-classic-backup-upload.service, started when 2 succeeds) copies <out>/user-state/ to
   Google Drive with the owner's rclone remote and deletes copies there older than 90 days.
   Only user data goes off the Pi: no tv.env, no credentials.

Left out on purpose (rebuilt automatically or not needed): catalogue.json, epg.json and
details.json (re-downloaded from the provider on start), artwork and remux caches,
secret.key (a new one just means signing in once more on each device), the venv
(requirements.txt), demo media (demo/make-media.sh), and the site-wide Basic Auth
password file /etc/nginx/private.htpasswd (shared by all private apps).
"""
from __future__ import annotations

import argparse
import grp
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .userstate import snapshot

STATE_DB = Path("/var/lib/signal-tv-classic/state.db")
SNAP_DIR = Path("/var/lib/signal-tv-classic/backups")
REPO = Path(os.environ.get("TV_REPO", "/home/pi/signal-tv-classic"))
FILES = [
    "/etc/signal-tv-classic/tv.env",
    "/etc/signal-tv-classic/telegram.env",
    "/etc/systemd/system/signal-tv-classic.service",
    "/etc/systemd/system/signal-tv-classic-demo.service",
    "/etc/systemd/system/signal-tv-classic-backup.service",
    "/etc/systemd/system/signal-tv-classic-backup.timer",
    "/etc/systemd/system/signal-tv-classic-backup-upload.service",
    "/etc/systemd/system/signal-tv-classic-alert@.service",
    "/etc/nginx/snippets/signal-tv-classic.conf",
    "/etc/nginx/snippets/signal-tv-classic-proxy.conf",
    "/etc/nginx/snippets/signal-tv-classic-headers.conf",
    "/etc/nginx/conf.d/30-signal-tv-classic.conf",
    "/etc/nginx/conf.d/10-site.conf",
    "/etc/nginx/sites-available/example.com.conf",
    "/usr/local/bin/iptv",
]
CODE_SKIP = {"venv", "__pycache__", ".git"}

RESTORE = """Signal TV Classic backup ({stamp}, app {version})

Restore on a new Pi (details: README.md, "Backup and restore"):
 1. sudo apt install nginx ffmpeg python3-venv rsync git
 2. git clone code/signal-tv-classic.bundle ~/signal-tv-classic
 3. sudo useradd --system --home /nonexistent --shell /usr/sbin/nologin signal-tv-classic
 4. Copy etc/ and usr/ from this archive to / (tv.env: root:signal-tv-classic 0640).
 5. ~/signal-tv-classic/deploy/install.sh     (code -> /opt/signal-tv-classic, venv)
 6. sudo install -d -o signal-tv-classic -g signal-tv-classic -m 0700 /var/lib/signal-tv-classic
    sudo install -o signal-tv-classic -g signal-tv-classic -m 0600 user-state/state.sqlite3 /var/lib/signal-tv-classic/state.db
 7. Re-create /etc/nginx/private.htpasswd (htpasswd) if the site is new too.
 8. sudo systemctl daemon-reload && sudo nginx -t && sudo systemctl reload nginx
    sudo systemctl enable --now signal-tv-classic signal-tv-classic-backup.timer
Devices notice the restored database and send back anything newer they hold.
"""


def state_snapshot(keep: int) -> list[Path]:
    return snapshot(STATE_DB, SNAP_DIR, keep=keep, app_version=__version__)


def _owner_readable(path: Path, mode: int = 0o640) -> None:
    """root:pi 0640 (0750 for a directory): the Pi's owner can copy/upload it, other users cannot."""
    os.chmod(path, mode)                    # explicit: the unit's UMask would otherwise make it 0600
    try:
        os.chown(path, 0, grp.getgrnam("pi").gr_gid)
    except (KeyError, PermissionError):
        pass


def _keep_user_copy(snaps: tuple[Path, Path], out_dir: Path, keep: int) -> None:
    dest = out_dir / "user-state"
    dest.mkdir(exist_ok=True)
    _owner_readable(dest, 0o750)
    for src in snaps:
        if src.exists():
            target = dest / src.name
            shutil.copyfile(src, target)
            _owner_readable(target)
    for pattern in ("state-*.sqlite3", "state-*.json"):
        for old in sorted(dest.glob(pattern))[:-keep]:
            old.unlink()


def _add_code(tar: tarfile.TarFile, root: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(root):
        rel = Path(dirpath).relative_to(root)
        dirnames[:] = sorted(d for d in dirnames if d not in CODE_SKIP and not (rel == Path("demo") and d == "media"))
        for f in sorted(filenames):
            tar.add(Path(dirpath) / f, arcname=f"code/opt-signal-tv-classic/{rel / f}")


def make_archive(out_dir: Path, keep: int) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
    target = out_dir / f"signal-tv-classic-{stamp}.tar.gz"
    part = target.with_name(target.name + ".part")
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
    try:
        with os.fdopen(fd, "wb") as fh, tarfile.open(fileobj=fh, mode="w:gz") as tar, \
                tempfile.TemporaryDirectory(prefix="tv-backup-") as tmp:
            snaps = sorted(SNAP_DIR.glob("state-*.sqlite3"))
            if snaps:
                newest = (snaps[-1], snaps[-1].with_suffix(".json"))
                tar.add(newest[0], arcname="user-state/state.sqlite3")
                if newest[1].exists():
                    tar.add(newest[1], arcname="user-state/state.json")
            else:
                print("warning: no state snapshot found (run with --state-snapshot first)", file=sys.stderr)
            for f in FILES:
                if Path(f).exists():
                    tar.add(f, arcname=f.lstrip("/"))
            if (REPO / ".git").is_dir():
                bundle = Path(tmp) / "signal-tv-classic.bundle"
                subprocess.run(["git", "-c", f"safe.directory={REPO}", "-C", str(REPO), "bundle", "create",
                                str(bundle), "--all"], check=True, capture_output=True)
                tar.add(bundle, arcname="code/signal-tv-classic.bundle")
            else:
                print(f"warning: no git repository at {REPO}", file=sys.stderr)
            _add_code(tar, Path("/opt/signal-tv-classic"))
            note = Path(tmp) / "RESTORE.txt"
            note.write_text(RESTORE.format(stamp=stamp, version=__version__))
            tar.add(note, arcname="RESTORE.txt")
        _owner_readable(part)
        os.replace(part, target)
    finally:
        part.unlink(missing_ok=True)
    for old in sorted(out_dir.glob("signal-tv-classic-*.tar.gz"))[:-keep]:
        old.unlink()
    if snaps:
        _keep_user_copy(newest, out_dir, keep)
    return target


def upload(remote: str, out_dir: Path, keep_days: int = 90) -> None:
    """Copy <out>/user-state/ (no secrets) to an rclone remote; drop copies there older than keep_days."""
    src = out_dir / "user-state"
    if not any(src.glob("state-*")):
        raise SystemExit(f"nothing to upload in {src}")
    dest = f"{remote.rstrip('/')}/user-state"
    for cmd in (["rclone", "copy", str(src), dest, "--include", "state-*"],
                ["rclone", "delete", dest, "--min-age", f"{keep_days}d", "--include", "state-*"]):
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if r.returncode:
            tail = " | ".join(line.strip() for line in r.stderr.strip().splitlines()[-3:])
            expired = any(s in r.stderr for s in ("invalid_grant", "couldn't fetch token", "token expired"))
            hint = " - Google's sign-in has expired: run `rclone config reconnect gdrive:` (see README)" if expired else ""
            raise SystemExit(f"upload to {remote} failed: {tail}{hint}")
    print(f"uploaded {src} to {dest}")


def main() -> int:
    ap = argparse.ArgumentParser(prog="tvapp.backup", description="Signal TV Classic backups (see module docstring)")
    ap.add_argument("--state-snapshot", action="store_true", help="copy the state database only (run as signal-tv-classic)")
    ap.add_argument("--keep", type=int, default=0, help="copies to keep (default 14 snapshots / 30 archives)")
    ap.add_argument("--out", type=Path, default=Path("/var/backups/signal-tv-classic"))
    ap.add_argument("--upload", metavar="REMOTE", help="copy the user-state backups to this rclone remote (run as pi)")
    args = ap.parse_args()
    if args.upload:
        upload(args.upload, args.out)
    elif args.state_snapshot:
        for p in state_snapshot(args.keep or 14):
            print(p)
    else:
        print(make_archive(args.out, args.keep or 30))
    return 0


if __name__ == "__main__":
    sys.exit(main())
