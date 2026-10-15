"""Settings for signal-tv-classic.

Everything provider-specific lives in ONE protected file (default
/etc/signal-tv-classic/tv.env, root:signal-tv-classic 0640). The app reads it itself at start
and again on SIGHUP (`iptv reload`), so changing a URL never needs a code edit.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("TV_CONFIG", "/etc/signal-tv-classic/tv.env"))

KEYS = [
    "IPTV_PROVIDER_TYPE", "IPTV_M3U_URL", "IPTV_EPG_URL",
    "XTREAM_BASE_URL", "XTREAM_USERNAME", "XTREAM_PASSWORD",
    "IPTV_USER_AGENT", "IPTV_REFRESH_HOURS", "IPTV_EPG_REFRESH_HOURS",
    "IPTV_MAX_LIVE_STREAMS", "IPTV_MAX_VOD_REMUX",
]
SECRET_KEYS = {"XTREAM_PASSWORD", "XTREAM_USERNAME", "IPTV_M3U_URL", "IPTV_EPG_URL", "XTREAM_BASE_URL"}


def read_env(path: Path = CONFIG_PATH) -> dict[str, str]:
    """KEY=value per line; '#' comments; optional surrounding quotes. Never sourced by a shell."""
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        out[key] = value
    return out


def write_env_value(key: str, value: str, path: Path = CONFIG_PATH) -> None:
    """Replace (or append) one KEY=value line, keeping comments and other keys."""
    if key not in KEYS:
        raise ValueError(f"unknown setting {key}")
    if "\n" in value or "\r" in value:
        raise ValueError("value must be a single line")
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    done = False
    for i, raw in enumerate(lines):
        if raw.strip().startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            done = True
    if not done:
        lines.append(f"{key}={value}")
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o640)
    try:
        st = path.stat()
        os.chown(tmp, st.st_uid, st.st_gid)
    except (FileNotFoundError, PermissionError):
        pass
    os.replace(tmp, path)


def _num(env: dict, key: str, default: float) -> float:
    try:
        return float(env.get(key) or default)
    except ValueError:
        return default


@dataclass
class Settings:
    provider_type: str
    m3u_url: str
    epg_url: str
    xtream_base: str
    xtream_user: str
    xtream_pass: str
    user_agent: str
    refresh_hours: float
    epg_refresh_hours: float
    max_live: int
    max_vod: int
    state_dir: Path
    cache_dir: Path
    live_dir: Path
    vod_dir: Path
    web_dir: Path
    host: str
    port: int
    public_origin: str
    memory_limit_mb: int = 2048          # TV-only memory guard (tvapp/memguard.py)

    @property
    def configured(self) -> bool:
        if self.provider_type == "xtream":
            return bool(self.xtream_base and self.xtream_user and self.xtream_pass)
        return bool(self.m3u_url)


def load() -> Settings:
    env = read_env()
    state = Path(os.environ.get("STATE_DIRECTORY", "/var/lib/signal-tv-classic").split(":")[0])
    cache = Path(os.environ.get("CACHE_DIRECTORY", "/var/cache/signal-tv-classic").split(":")[0])
    return Settings(
        provider_type=(env.get("IPTV_PROVIDER_TYPE") or "m3u").strip().lower(),
        m3u_url=env.get("IPTV_M3U_URL", "").strip(),
        epg_url=env.get("IPTV_EPG_URL", "").strip(),
        xtream_base=env.get("XTREAM_BASE_URL", "").strip().rstrip("/"),
        xtream_user=env.get("XTREAM_USERNAME", "").strip(),
        xtream_pass=env.get("XTREAM_PASSWORD", "").strip(),
        user_agent=env.get("IPTV_USER_AGENT", "").strip() or "VLC/3.0.20 LibVLC/3.0.20",
        refresh_hours=max(1.0, _num(env, "IPTV_REFRESH_HOURS", 12)),
        epg_refresh_hours=max(1.0, _num(env, "IPTV_EPG_REFRESH_HOURS", 12)),
        max_live=int(max(1, _num(env, "IPTV_MAX_LIVE_STREAMS", 2))),
        max_vod=int(max(1, _num(env, "IPTV_MAX_VOD_REMUX", 2))),
        state_dir=state,
        cache_dir=cache,
        live_dir=Path(os.environ.get("TV_LIVE_DIR", "/dev/shm/signal-tv-classic")),
        vod_dir=cache / "vod",
        web_dir=Path(os.environ.get("TV_WEB_DIR", "/opt/signal-tv-classic/web")),
        host=os.environ.get("TV_HOST", "127.0.0.1"),
        port=int(os.environ.get("TV_PORT", "8790")),
        public_origin=os.environ.get("TV_PUBLIC_ORIGIN", "https://example.com"),
        memory_limit_mb=int(_num(os.environ, "TV_MEMORY_LIMIT_MB", 2048)),
    )
