"""HMAC helpers.

* Session cookie: issued only after nginx has accepted the site's Basic Auth
  login (nginx passes the authenticated user name in X-TV-Auth-User, which it
  always overwrites). nginx accepts the cookie *instead of* Basic Auth via
  auth_request -> /tv/api/auth-check, so the iPhone home-screen app does not
  ask for the password on every launch. `iptv revoke-sessions` rotates the key.
* Media token: short-lived, scoped to one stream job / one item, embedded in the
  media path because native players (AVPlayer, PiP, AirPlay) do not reliably
  send cookies or Basic Auth with media requests.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import time
from pathlib import Path

COOKIE = "tv_s"
COOKIE_DAYS = 30
TOKEN_TTL = 12 * 3600
TOKEN_RE = re.compile(r"^([0-9a-f]{8,10})\.([0-9a-f]{12})\.([A-Za-z0-9_-]{22})$")
CLIENT_RE = re.compile(r"^[0-9a-f]{12}$")


class Keys:
    def __init__(self, state_dir: Path):
        self.path = state_dir / "secret.key"
        self.key = self._load()

    def _load(self) -> bytes:
        try:
            key = self.path.read_bytes()
            if len(key) >= 32:
                return key
        except FileNotFoundError:
            pass
        key = secrets.token_bytes(48)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
        os.replace(tmp, self.path)
        return key

    def reload(self) -> None:
        self.key = self._load()

    def _mac(self, msg: str, n: int = 16) -> str:
        d = hmac.new(self.key, msg.encode(), hashlib.sha256).digest()[:n]
        return base64.urlsafe_b64encode(d).decode().rstrip("=")

    # ---- session cookie: "<exp hex>.<nonce>.<mac>"
    def make_cookie(self) -> str:
        exp = int(time.time()) + COOKIE_DAYS * 86400
        nonce = secrets.token_hex(6)
        return f"{exp:x}.{nonce}.{self._mac(f'c:{exp}:{nonce}')}"

    def cookie_ok(self, value: str | None) -> tuple[bool, int]:
        m = TOKEN_RE.match(value or "")
        if not m:
            return False, 0
        exp = int(m.group(1), 16)
        good = hmac.compare_digest(m.group(3), self._mac(f"c:{exp}:{m.group(2)}"))
        return good and exp > time.time(), exp

    # ---- media token: "<exp hex>.<client>.<mac over scope>"
    def make_token(self, scope: str, client: str) -> str:
        exp = int(time.time()) + TOKEN_TTL
        return f"{exp:x}.{client}.{self._mac(f'm:{scope}:{client}:{exp}')}"

    def token_client(self, token: str, scope: str) -> str | None:
        m = TOKEN_RE.match(token or "")
        if not m:
            return None
        exp = int(m.group(1), 16)
        if exp < time.time():
            return None
        if not hmac.compare_digest(m.group(3), self._mac(f"m:{scope}:{m.group(2)}:{exp}")):
            return None
        return m.group(2)
