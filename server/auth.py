"""Signed session cookie. The password never leaves the server."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from .settings import load_password

COOKIE = "vela_session"
SESSION_TTL = 60 * 60 * 24 * 30


def _secret() -> bytes:
    # Derived so rotating APP_PASSWORD invalidates sessions.
    return hashlib.sha256(b"vela-session|" + load_password().encode()).digest()


def issue_token() -> str:
    exp = str(int(time.time()) + SESSION_TTL)
    nonce = secrets.token_hex(8)
    payload = f"{exp}.{nonce}"
    sig = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def token_ok(token: str | None) -> bool:
    if not token or token.count(".") != 2:
        return False
    exp, nonce, sig = token.split(".")
    payload = f"{exp}.{nonce}"
    expected = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return False
    try:
        return int(exp) > int(time.time())
    except ValueError:
        return False


def password_ok(guess: str) -> bool:
    return hmac.compare_digest(guess.encode(), load_password().encode())
