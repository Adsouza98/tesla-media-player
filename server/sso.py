"""Plex PIN sign-in and optional Google sign-in.

Plex is the path that works on the Tesla browser: the screen shows a short
code, and the phone approves it at plex.tv/link. Google is a normal OAuth
redirect for a laptop. The car browser often blocks Google's own login.
"""

from __future__ import annotations

import os
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from . import auth, settings

PLEX_API = "https://plex.tv/api/v2"
GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO = "https://www.googleapis.com/oauth2/v3/userinfo"

_pins: dict[str, "Pin"] = {}
_used_states: dict[str, float] = {}


@dataclass
class Pin:
    plex_id: int
    code: str
    client_id: str
    created: float
    expires_in: int
    link_user: str | None
    qr: bytes | None


def google_enabled() -> bool:
    return bool(_env("GOOGLE_CLIENT_ID") and _env("GOOGLE_CLIENT_SECRET"))


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def external_origin(request) -> str:
    public = _env("PUBLIC_URL").rstrip("/")
    if public:
        return public
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    host = host.split(",")[0].strip()
    proto = proto.split(",")[0].strip()
    if proto not in {"http", "https"} or not host or "/" in host or " " in host:
        return str(request.base_url).rstrip("/")
    return f"{proto}://{host}"


def google_redirect(origin: str) -> str:
    return origin.rstrip("/") + "/api/auth/google/callback"


def _plex_headers(client_id: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "X-Plex-Product": "Vela",
        "X-Plex-Version": settings.VERSION,
        "X-Plex-Client-Identifier": client_id,
        "X-Plex-Platform": "Web",
        "X-Plex-Platform-Version": "1.0",
        "X-Plex-Model": "Plex OAuth",
        "X-Plex-Device": "Vela",
        "X-Plex-Device-Name": "Vela Cabin",
    }


def _client_id() -> str:
    # Stable per config dir so Plex sees the same app.
    path = settings.CONFIG_DIR / "plex-client-id"
    if path.is_file():
        saved = path.read_text(encoding="utf-8").strip()
        if saved:
            return saved
    path.parent.mkdir(parents=True, exist_ok=True)
    saved = secrets.token_hex(16)
    path.write_text(saved + "\n", encoding="utf-8")
    return saved


def _prune_pins() -> None:
    now = time.time()
    dead = [key for key, pin in _pins.items() if pin.created + pin.expires_in < now]
    for key in dead:
        _pins.pop(key, None)


def auth_url(client_id: str, code: str) -> str:
    query = urlencode(
        {
            "clientID": client_id,
            "code": code,
            "context[device][product]": "Vela",
            "context[device][layout]": "desktop",
            "context[device][platform]": "Web",
        }
    )
    return f"https://app.plex.tv/auth#?{query}"


async def start_pin(link_user: str | None = None) -> dict:
    _prune_pins()
    if len(_pins) > 30:
        raise RuntimeError("Too many sign-in attempts. Wait a minute.")
    client_id = _client_id()
    headers = _plex_headers(client_id)
    async with httpx.AsyncClient(timeout=15) as client:
        created = await client.post(f"{PLEX_API}/pins", params={"strong": "false"}, headers=headers, json={})
        created.raise_for_status()
        body = created.json()
        code = str(body.get("code") or "")
        plex_id = int(body["id"])
        qr = None
        qr_url = body.get("qr")
        if isinstance(qr_url, str) and qr_url.startswith("https://plex.tv/"):
            try:
                image = await client.get(qr_url, timeout=15)
            except httpx.HTTPError:
                image = None
            if image is not None and image.status_code == 200 and image.headers.get("content-type", "").startswith("image/"):
                qr = image.content
    state = secrets.token_urlsafe(24)
    expires_in = int(body.get("expiresIn") or 900)
    _pins[state] = Pin(
        plex_id=plex_id,
        code=code,
        client_id=client_id,
        created=time.time(),
        expires_in=max(30, min(expires_in, 1800)),
        link_user=link_user,
        qr=qr,
    )
    return {
        "state": state,
        "code": code,
        "expires_in": expires_in,
        "link": "https://plex.tv/link",
        "url": auth_url(client_id, code),
        "qr": f"/api/auth/plex/qr?state={state}" if qr else "",
    }


def pin_qr(state: str) -> bytes | None:
    pin = _pins.get(state)
    if not pin or not pin.qr:
        return None
    if pin.created + pin.expires_in < time.time():
        return None
    return pin.qr


async def poll_pin(state: str) -> dict:
    """Ask Plex if the code was approved. Does not decide who is allowed in."""
    pin = _pins.get(state)
    if not pin:
        return {"status": "expired"}
    if pin.created + pin.expires_in < time.time():
        _pins.pop(state, None)
        return {"status": "expired"}
    headers = _plex_headers(pin.client_id)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            checked = await client.get(f"{PLEX_API}/pins/{pin.plex_id}", headers=headers)
            checked.raise_for_status()
            body = checked.json()
            token = body.get("authToken")
            if not token:
                return {"status": "pending", "link_user": pin.link_user}
            user_headers = dict(headers)
            user_headers["X-Plex-Token"] = str(token)
            profile_res = await client.get(f"{PLEX_API}/user", headers=user_headers)
            profile_res.raise_for_status()
            profile = profile_res.json()
    except httpx.HTTPError:
        return {"status": "error", "detail": "Plex sign-in is unreachable"}
    _pins.pop(state, None)
    return {
        "status": "claimed",
        "link_user": pin.link_user,
        "profile": {
            "id": profile.get("id"),
            "username": str(profile.get("username") or profile.get("title") or "").strip(),
            "email": str(profile.get("email") or "").strip().lower(),
        },
    }


def begin_google(origin: str, link_user: str | None) -> str:
    if not google_enabled():
        raise RuntimeError("Google sign-in is not configured")
    payload = {"purpose": "link" if link_user else "login", "link_user": link_user or "", "origin": origin}
    state = auth.sign_payload(payload, ttl=600)
    query = urlencode(
        {
            "client_id": _env("GOOGLE_CLIENT_ID"),
            "redirect_uri": google_redirect(origin),
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "prompt": "select_account",
            "include_granted_scopes": "false",
        }
    )
    return f"{GOOGLE_AUTH}?{query}"


def take_google_state(token: str) -> dict | None:
    now = time.time()
    expired = [key for key, seen in _used_states.items() if now - seen > 900]
    for key in expired:
        _used_states.pop(key, None)
    if token in _used_states:
        return None
    data = auth.read_payload(token)
    if not data:
        return None
    _used_states[token] = now
    return data


async def finish_google(code: str, origin: str) -> dict:
    if not google_enabled():
        raise RuntimeError("Google sign-in is not configured")
    redirect = google_redirect(origin)
    async with httpx.AsyncClient(timeout=15) as client:
        token_res = await client.post(
            GOOGLE_TOKEN,
            data={
                "code": code,
                "client_id": _env("GOOGLE_CLIENT_ID"),
                "client_secret": _env("GOOGLE_CLIENT_SECRET"),
                "redirect_uri": redirect,
                "grant_type": "authorization_code",
            },
        )
        token_res.raise_for_status()
        access = token_res.json().get("access_token")
        if not access:
            raise RuntimeError("Google did not return a token")
        info = await client.get(GOOGLE_USERINFO, headers={"Authorization": f"Bearer {access}"})
        info.raise_for_status()
        profile = info.json()
    if not profile.get("email_verified"):
        raise RuntimeError("That Google account has no verified email")
    return {
        "sub": str(profile.get("sub") or ""),
        "email": str(profile.get("email") or "").strip().lower(),
        "name": str(profile.get("name") or "").strip(),
    }
