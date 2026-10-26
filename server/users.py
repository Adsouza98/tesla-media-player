"""Two seats, and only two: the owner and the Grok test user.

Plex and Google only open a seat that already lists that account.
Nothing here auto-claims the first person who shows up.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from . import auth, settings

OWNER_ID = "owner"
TESTER_ID = "grok"
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_USERNAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def _users_path() -> Path:
    return settings.CONFIG_DIR / "users.json"


def _hint_path() -> Path:
    return settings.CONFIG_DIR / "test-user"


def _read() -> dict:
    path = _users_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(data: dict) -> None:
    path = _users_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)


def _stamp(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def clean_username(value: str) -> str:
    text = (value or "").strip().lower()
    return text if _USERNAME.fullmatch(text) else ""


def clean_email(value: str) -> str:
    text = (value or "").strip().lower()
    if not text:
        return ""
    if not _EMAIL.fullmatch(text) or len(text) > 120:
        raise ValueError("Enter a real email address")
    return text


def _blank(user_id: str, name: str, username: str, role: str) -> dict:
    return {
        "id": user_id,
        "name": name[:80],
        "username": username,
        "role": role,
        "password_hash": "",
        "password_stamp": "",
        "session_version": 1,
        "plex_id": "",
        "plex_username": "",
        "plex_email": "",
        "google_sub": "",
        "google_email": "",
    }


def _fill(user: dict, key: str, value: str) -> None:
    if value and not str(user.get(key) or "").strip():
        user[key] = value


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def ensure_users() -> dict:
    """Create the two seats on first start. Returns a small status, no secrets."""
    data = _read()
    users = data.get("users")
    changed = not isinstance(users, list)
    if not isinstance(users, list):
        owner_name = _env("OWNER_NAME") or "JuniorChange"
        owner_user = clean_username(_env("OWNER_USERNAME")) or "juniorchange"
        tester_name = _env("TEST_NAME") or "Grok"
        tester_user = clean_username(_env("TEST_USERNAME")) or "grok"
        if tester_user == owner_user:
            tester_user = "grok"
        users = [
            _blank(OWNER_ID, owner_name, owner_user, "owner"),
            _blank(TESTER_ID, tester_name, tester_user, "tester"),
        ]
        data = {"users": users}
    by_id = {user.get("id"): user for user in users if isinstance(user, dict)}
    if OWNER_ID not in by_id or TESTER_ID not in by_id:
        owner_name = _env("OWNER_NAME") or "JuniorChange"
        owner_user = clean_username(_env("OWNER_USERNAME")) or "juniorchange"
        tester_name = _env("TEST_NAME") or "Grok"
        tester_user = clean_username(_env("TEST_USERNAME")) or "grok"
        if tester_user == owner_user:
            tester_user = "grok"
        if OWNER_ID not in by_id:
            by_id[OWNER_ID] = _blank(OWNER_ID, owner_name, owner_user, "owner")
        if TESTER_ID not in by_id:
            by_id[TESTER_ID] = _blank(TESTER_ID, tester_name, tester_user, "tester")
        changed = True
    owner = by_id[OWNER_ID]
    tester = by_id[TESTER_ID]
    for user, template in (
        (owner, _blank(OWNER_ID, "JuniorChange", "juniorchange", "owner")),
        (tester, _blank(TESTER_ID, "Grok", "grok", "tester")),
    ):
        for key, value in template.items():
            user.setdefault(key, value)
    before = json.dumps({"users": [owner, tester]}, sort_keys=True)

    _fill(owner, "name", _env("OWNER_NAME"))
    _fill(owner, "plex_username", _env("OWNER_PLEX_USERNAME"))
    owner_email = _env("OWNER_EMAIL").lower()
    if owner_email:
        _fill(owner, "google_email", owner_email)
        _fill(owner, "plex_email", owner_email)
    _fill(tester, "name", _env("TEST_NAME"))
    _fill(tester, "plex_username", _env("TEST_PLEX_USERNAME"))
    test_email = _env("TEST_EMAIL").lower()
    if test_email:
        _fill(tester, "google_email", test_email)
        _fill(tester, "plex_email", test_email)

    test_password = _env("TEST_PASSWORD")
    hint_written = False
    if test_password:
        stamp = _stamp(test_password)
        if tester.get("password_stamp") != stamp:
            if tester.get("password_stamp"):
                tester["session_version"] = int(tester.get("session_version") or 1) + 1
            tester["password_hash"] = auth.hash_password(test_password)
            tester["password_stamp"] = stamp
            _hint_path().unlink(missing_ok=True)
    elif not tester.get("password_hash"):
        generated = secrets_password()
        tester["password_hash"] = auth.hash_password(generated)
        tester["password_stamp"] = _stamp(generated)
        _write_hint(tester.get("username") or "grok", generated)
        hint_written = True

    data["users"] = [owner, tester]
    if json.dumps(data, sort_keys=True) != before or changed:
        _write(data)
    return {
        "usernames": [owner.get("username"), tester.get("username")],
        "hint_written": hint_written or _hint_path().is_file(),
    }


def secrets_password() -> str:
    import secrets

    return secrets.token_urlsafe(12)


def _write_hint(username: str, password: str) -> None:
    path = _hint_path()
    path.write_text(f"username={username}\npassword={password}\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _load() -> list[dict]:
    ensure_users()
    users = _read().get("users") or []
    return [user for user in users if isinstance(user, dict)]


def get_user(user_id: str) -> dict | None:
    for user in _load():
        if user.get("id") == user_id:
            return user
    return None


def user_for_session(user_id: str, version: int) -> dict | None:
    user = get_user(user_id)
    if not user:
        return None
    try:
        current = int(user.get("session_version") or 0)
    except (TypeError, ValueError):
        return None
    if current != int(version):
        return None
    return user


def session_public(user: dict) -> dict:
    return {
        "id": user["id"],
        "name": user.get("name") or user.get("username") or user["id"],
        "username": user.get("username") or "",
        "role": user.get("role") or "tester",
    }


def access_public(user: dict) -> dict:
    return {
        **session_public(user),
        "plex_username": user.get("plex_username") or "",
        "plex_email": user.get("plex_email") or "",
        "google_email": user.get("google_email") or "",
        "plex_linked": bool(user.get("plex_id")),
        "google_linked": bool(user.get("google_sub")),
    }


def list_access() -> list[dict]:
    return [access_public(user) for user in _load()]


def tester_password_hint() -> str | None:
    path = _hint_path()
    if not path.is_file():
        return None
    password = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("password="):
            password = line.split("=", 1)[1].strip()
    if not password:
        return None
    tester = get_user(TESTER_ID)
    if not tester or not auth.verify_password(password, tester.get("password_hash") or ""):
        return None
    return password


def authenticate(username: str | None, password: str) -> dict | None:
    if not password or len(password) > 200:
        return None
    users = _load()
    typed = clean_username(username or "")
    if typed:
        user = next((item for item in users if clean_username(item.get("username") or "") == typed), None)
        if not user:
            auth.verify_password(password, _dummy_hash())
            return None
        if user.get("id") == OWNER_ID and auth.password_ok(password):
            return user
        if auth.verify_password(password, user.get("password_hash") or ""):
            return user
        return None

    owner = next((item for item in users if item.get("id") == OWNER_ID), None)
    if owner and (auth.password_ok(password) or auth.verify_password(password, owner.get("password_hash") or "")):
        return owner
    hits = [
        item
        for item in users
        if item.get("id") != OWNER_ID and auth.verify_password(password, item.get("password_hash") or "")
    ]
    if len(hits) == 1:
        return hits[0]
    return None


_DUMMY: str | None = None


def _dummy_hash() -> str:
    global _DUMMY
    if _DUMMY is None:
        digest = hashlib.scrypt(b"vela-missing-user", salt=b"vela-dummy-salt!!", n=2**14, r=8, p=1, dklen=32)
        _DUMMY = "scrypt$" + b"vela-dummy-salt!!".hex() + "$" + digest.hex()
    return _DUMMY


def _norm_email(value: str) -> str:
    return (value or "").strip().lower()


def match_identity(kind: str, profile: dict) -> dict | None:
    plex_id = str(profile.get("id") or "").strip()
    username = str(profile.get("username") or "").strip().casefold()
    email = _norm_email(str(profile.get("email") or ""))
    sub = str(profile.get("sub") or "").strip()
    hits: list[dict] = []
    for user in _load():
        if kind == "plex":
            if user.get("plex_id") and plex_id and str(user["plex_id"]) == plex_id:
                hits.append(user)
                continue
            saved_name = str(user.get("plex_username") or "").strip().casefold()
            if saved_name and username and saved_name == username:
                hits.append(user)
                continue
            saved_emails = {_norm_email(user.get("plex_email") or ""), _norm_email(user.get("google_email") or "")}
            saved_emails.discard("")
            if email and email in saved_emails:
                hits.append(user)
        elif kind == "google":
            if user.get("google_sub") and sub and user["google_sub"] == sub:
                hits.append(user)
                continue
            saved_emails = {_norm_email(user.get("google_email") or ""), _norm_email(user.get("plex_email") or "")}
            saved_emails.discard("")
            if email and email in saved_emails:
                hits.append(user)
    if len(hits) == 1:
        return hits[0]
    return None


def _save_user(updated: dict) -> dict:
    data = _read()
    users = []
    for user in data.get("users") or []:
        users.append(updated if user.get("id") == updated.get("id") else user)
    data["users"] = users
    _write(data)
    return updated


def remember_identity(user_id: str, kind: str, profile: dict) -> dict:
    user = get_user(user_id)
    if not user:
        raise ValueError("Unknown seat")
    other = match_identity(kind, profile)
    if other and other.get("id") != user_id:
        raise ValueError("That account is already linked to the other seat")
    if kind == "plex":
        if profile.get("id"):
            user["plex_id"] = str(profile["id"])
        if profile.get("username"):
            user["plex_username"] = str(profile["username"]).strip()
        if profile.get("email"):
            user["plex_email"] = _norm_email(str(profile["email"]))
    elif kind == "google":
        if profile.get("sub"):
            user["google_sub"] = str(profile["sub"]).strip()
        if profile.get("email"):
            user["google_email"] = _norm_email(str(profile["email"]))
    else:
        raise ValueError("Unknown identity")
    return _save_user(user)


def update_users(payload: list) -> list[dict]:
    if not isinstance(payload, list) or len(payload) != 2:
        raise ValueError("Both seats have to stay")
    current = {user["id"]: user for user in _load()}
    incoming = {item.get("id"): item for item in payload if isinstance(item, dict)}
    if set(incoming) != {OWNER_ID, TESTER_ID}:
        raise ValueError("Only the owner and Grok seats exist")
    next_users = []
    seen_names: set[str] = set()
    seen_emails: set[str] = set()
    for user_id in (OWNER_ID, TESTER_ID):
        user = dict(current[user_id])
        item = incoming[user_id]
        name = str(item.get("name") or "").strip()
        if not name or len(name) > 80:
            raise ValueError("Each seat needs a name")
        username = clean_username(str(item.get("username") or ""))
        if not username:
            raise ValueError("Usernames use letters, numbers, and dashes")
        if username in seen_names:
            raise ValueError("Usernames have to be different")
        seen_names.add(username)
        plex_email = clean_email(str(item.get("plex_email") or ""))
        google_email = clean_email(str(item.get("google_email") or ""))
        own_emails: set[str] = set()
        for email in (plex_email, google_email):
            if not email:
                continue
            if email in seen_emails:
                raise ValueError("That email is already on the other seat")
            own_emails.add(email)
        seen_emails.update(own_emails)
        plex_username = str(item.get("plex_username") or "").strip()
        if len(plex_username) > 80:
            raise ValueError("Plex username is too long")
        user["name"] = name
        user["username"] = username
        user["plex_username"] = plex_username
        user["plex_email"] = plex_email
        user["google_email"] = google_email
        if plex_username.casefold() != str(current[user_id].get("plex_username") or "").casefold():
            user["plex_id"] = ""
        if google_email != _norm_email(current[user_id].get("google_email") or ""):
            user["google_sub"] = ""
        new_password = str(item.get("password") or "")
        if new_password:
            if len(new_password) < 8 or len(new_password) > 200:
                raise ValueError("Passwords need at least 8 characters")
            user["password_hash"] = auth.hash_password(new_password)
            user["password_stamp"] = _stamp(new_password)
            user["session_version"] = int(user.get("session_version") or 1) + 1
            if user_id == TESTER_ID:
                _hint_path().unlink(missing_ok=True)
        next_users.append(user)
    _write({"users": next_users})
    return [access_public(user) for user in next_users]
