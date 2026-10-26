"""Persistent config. Environment variables are defaults; the settings file overrides them."""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

VERSION = "0.2.1"
QUALITY_IDS = ("480", "720", "1080", "max")


def resolve_config_dir() -> Path:
    env = os.environ.get("CONFIG_DIR")
    if env:
        path = Path(env)
    else:
        path = Path("/config")
        if not path.exists() or not os.access(path, os.W_OK):
            path = Path(__file__).resolve().parent.parent / "config"
    path.mkdir(parents=True, exist_ok=True)
    return path


CONFIG_DIR = resolve_config_dir()
SETTINGS_PATH = CONFIG_DIR / "settings.json"
PASSWORD_PATH = CONFIG_DIR / "generated-password"


def _read_json() -> dict:
    if not SETTINGS_PATH.is_file():
        return {}
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(data: dict) -> None:
    tmp = SETTINGS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(SETTINGS_PATH)


def load_password() -> str:
    env = os.environ.get("APP_PASSWORD", "").strip()
    if env:
        return env
    if PASSWORD_PATH.is_file():
        return PASSWORD_PATH.read_text(encoding="utf-8").strip()
    generated = secrets.token_urlsafe(18)
    PASSWORD_PATH.write_text(generated + "\n", encoding="utf-8")
    os.chmod(PASSWORD_PATH, 0o600)
    return generated


def password_source() -> str:
    if os.environ.get("APP_PASSWORD", "").strip():
        return "environment"
    if PASSWORD_PATH.is_file():
        return "generated-file"
    return "unset"


def youtube_cookies_path() -> Path:
    raw = os.environ.get("YOUTUBE_COOKIES", "").strip()
    if raw:
        return Path(raw)
    stored = _read_json().get("youtube_cookies")
    if stored:
        return Path(str(stored))
    return CONFIG_DIR / "youtube-cookies.txt"


def cookies_ready() -> bool:
    path = youtube_cookies_path()
    try:
        return path.is_file() and path.stat().st_size > 20
    except OSError:
        return False


def public_settings() -> dict:
    data = _read_json()
    plex_url = str(data.get("plex_url") or os.environ.get("PLEX_URL") or "").rstrip("/")
    token = str(data.get("plex_token") or os.environ.get("PLEX_TOKEN") or "")
    quality = str(data.get("default_quality") or os.environ.get("DEFAULT_QUALITY") or "1080")
    if quality not in QUALITY_IDS:
        quality = "1080"
    return {
        "version": VERSION,
        "plex_url": plex_url,
        "plex_token_set": bool(token),
        "default_quality": quality,
        "youtube_cookies": cookies_ready(),
        "youtube_cookies_path": str(youtube_cookies_path()),
        "password_source": password_source(),
    }


def secret_settings() -> dict:
    data = _read_json()
    public = public_settings()
    token = str(data.get("plex_token") or os.environ.get("PLEX_TOKEN") or "")
    return {**public, "plex_token": token}


def update_settings(payload: dict) -> dict:
    data = _read_json()
    if "plex_url" in payload and payload["plex_url"] is not None:
        data["plex_url"] = str(payload["plex_url"]).strip().rstrip("/")
    if payload.get("plex_token"):
        data["plex_token"] = str(payload["plex_token"]).strip()
    if payload.get("default_quality") in QUALITY_IDS:
        data["default_quality"] = payload["default_quality"]
    _write_json(data)
    return public_settings()
