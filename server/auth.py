"""Signed session cookies and password hashes.

The cabin password and the Plex/Google tokens never go back to the browser.
A session cookie names the seat (owner or tester) and a version, so changing
that seat's password signs it out.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import time

from . import settings
from .settings import load_password

COOKIE = "vela_session"
SESSION_TTL = 60 * 60 * 24 * 30
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_SCRYPT_N = 2**14


def safe_id(value: str) -> bool:
    return bool(_ID.fullmatch(value or ""))


def _secret() -> bytes:
    path = settings.CONFIG_DIR / "session.key"
    if path.is_file():
        raw = path.read_text(encoding="utf-8").strip().lower()
        try:
            key = bytes.fromhex(raw)
        except ValueError:
            key = b""
        if len(key) == 32:
            return key
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    path.write_text(key.hex() + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    if not password or not stored or len(password) > 200:
        return False
    try:
        kind, salt_hex, digest_hex = stored.split("$")
        if kind != "scrypt":
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except ValueError:
        return False
    got = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=8, p=1, dklen=32)
    return hmac.compare_digest(got, expected)


def password_ok(guess: str) -> bool:
    """Owner cabin password from APP_PASSWORD or the generated file."""
    if not guess or len(guess) > 200:
        return False
    expected = load_password()
    if not expected:
        return False
    return hmac.compare_digest(guess.encode(), expected.encode())


def issue_token(user_id: str = "owner", version: int = 1) -> str:
    if not safe_id(user_id):
        raise ValueError("bad user id")
    exp = str(int(time.time()) + SESSION_TTL)
    nonce = secrets.token_hex(8)
    payload = f"{user_id}.{int(version)}.{exp}.{nonce}"
    sig = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def parse_token(token: str | None) -> tuple[str, int] | None:
    if not token or token.count(".") != 4:
        return None
    user_id, version, exp, nonce, sig = token.split(".")
    if not safe_id(user_id):
        return None
    payload = f"{user_id}.{version}.{exp}.{nonce}"
    expected = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        if int(exp) <= int(time.time()):
            return None
        return user_id, int(version)
    except ValueError:
        return None


def token_ok(token: str | None) -> bool:
    return parse_token(token) is not None


def sign_payload(data: dict, ttl: int = 600) -> str:
    import json

    body = dict(data)
    body["exp"] = int(time.time()) + ttl
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    encoded = _b64(raw)
    sig = hmac.new(_secret(), encoded.encode(), hashlib.sha256).hexdigest()
    return f"{encoded}.{sig}"


def read_payload(token: str | None) -> dict | None:
    import json

    if not token or token.count(".") != 1:
        return None
    encoded, sig = token.split(".")
    expected = hmac.new(_secret(), encoded.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        data = json.loads(_b64decode(encoded))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        if int(data.get("exp", 0)) <= int(time.time()):
            return None
    except (TypeError, ValueError):
        return None
    return data


def _b64(raw: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64decode(value: str) -> bytes:
    import base64

    pad = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + pad)
