"""Vela HTTP server. The Tesla only ever talks to this process."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth, plex, settings, sso, users, youtube
from .ffmpeg_cmd import fmp4_command, hls_remux_command, mpeg1_command
from .streaming import StreamError, iter_ffmpeg

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger("vela")

WEB = Path(__file__).resolve().parent.parent / "web"
app = FastAPI(title="Vela", version=settings.VERSION, docs_url=None, redoc_url=None)

_SECRETISH = re.compile(r"(?i)(X-Plex-Token:\s*\S+|token=|[?&]X-Plex-Token=)[^\s&]*")


def _public_error(exc: Exception) -> str:
    text = _SECRETISH.sub("token=***", str(exc))
    text = re.sub(r"(?i)(password|cookie|client_secret|access_token)[=:]\s*\S+", r"\1=***", text)
    return text.strip()[:500] or "Request failed"


def current_user(conn) -> dict | None:
    parsed = auth.parse_token(conn.cookies.get(auth.COOKIE))
    if not parsed:
        return None
    return users.user_for_session(*parsed)


def _authed(conn) -> bool:
    return current_user(conn) is not None


def require_user(request: Request) -> dict:
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    return user


def require_owner(user: dict = Depends(require_user)) -> dict:
    if user.get("role") != "owner":
        raise HTTPException(status_code=403, detail="Only the owner can change setup")
    return user


class LoginBody(BaseModel):
    password: str
    username: str | None = None


class SettingsBody(BaseModel):
    plex_url: str | None = None
    plex_token: str | None = None
    default_quality: str | None = None


class AccessBody(BaseModel):
    users: list


class PlexStartBody(BaseModel):
    link_user: str | None = None


_attempts: dict[str, list[float]] = {}


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _recent(key: str, window: float) -> list[float]:
    now = time.time()
    hits = [stamp for stamp in _attempts.get(key, []) if now - stamp < window]
    _attempts[key] = hits
    return hits


def _too_many(key: str, limit: int, window: float) -> bool:
    return len(_recent(key, window)) >= limit


def _mark_attempt(key: str, window: float) -> None:
    _recent(key, window).append(time.time())


def _secure_cookie(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
    return proto == "https"


def _session_response(request: Request, payload: dict, user: dict | None = None, status: int = 200) -> Response:
    response = Response(content=json.dumps(payload), media_type="application/json", status_code=status)
    response.headers["Cache-Control"] = "no-store"
    if user:
        response.set_cookie(
            auth.COOKIE,
            auth.issue_token(user["id"], int(user.get("session_version") or 1)),
            max_age=auth.SESSION_TTL,
            httponly=True,
            samesite="lax",
            secure=_secure_cookie(request),
            path="/",
        )
    return response


def _auth_error_redirect(code: str) -> RedirectResponse:
    allowed = {"not-allowed", "google-failed", "state", "unconfigured"}
    if code not in allowed:
        code = "google-failed"
    return RedirectResponse(url=f"/?e={code}", status_code=302)


@app.on_event("startup")
def _startup() -> None:
    source = settings.password_source()
    settings.load_password()
    status = users.ensure_users()
    if source == "environment":
        logger.info("Vela %s ready. Seats: %s", settings.VERSION, ", ".join(status["usernames"]))
    else:
        logger.warning(
            "APP_PASSWORD is not set. A password was written to %s — set APP_PASSWORD to replace it.",
            settings.PASSWORD_PATH,
        )
    if status.get("hint_written"):
        logger.warning("Grok test password is in %s", users._hint_path())
    logger.info("Plex sign-in on. Google sign-in %s.", "on" if sso.google_enabled() else "off")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; media-src 'self'; "
        "connect-src 'self' ws: wss:; style-src 'self'; script-src 'self'; frame-ancestors 'none'",
    )
    return response


@app.get("/api/health")
async def health():
    return {"ok": True, "service": "vela", "version": settings.VERSION}


@app.get("/api/auth/me")
async def me(request: Request):
    user = current_user(request)
    body = {
        "authenticated": bool(user),
        "methods": {"password": True, "plex": True, "google": sso.google_enabled()},
    }
    if user:
        body["user"] = users.session_public(user)
    return body


@app.post("/api/auth/login")
async def login(body: LoginBody, request: Request):
    ip = _client_ip(request)
    if _too_many(f"login:{ip}", 10, 600):
        raise HTTPException(status_code=429, detail="Too many attempts. Wait a few minutes.")
    user = users.authenticate(body.username, body.password)
    if not user:
        _mark_attempt(f"login:{ip}", 600)
        raise HTTPException(status_code=401, detail="Wrong username or password")
    return _session_response(request, {"ok": True, "user": users.session_public(user)}, user)


@app.post("/api/auth/logout")
async def logout():
    response = Response(content='{"ok":true}', media_type="application/json")
    response.delete_cookie(auth.COOKIE, path="/")
    return response


@app.post("/api/auth/plex/start")
async def plex_start(body: PlexStartBody, request: Request):
    if _too_many(f"plex:{_client_ip(request)}", 8, 600):
        raise HTTPException(status_code=429, detail="Too many Plex sign-in attempts. Wait a few minutes.")
    _mark_attempt(f"plex:{_client_ip(request)}", 600)
    link_user = None
    if body.link_user:
        owner = current_user(request)
        if not owner or owner.get("role") != "owner":
            raise HTTPException(status_code=401, detail="Sign in as the owner before linking")
        if body.link_user not in {users.OWNER_ID, users.TESTER_ID}:
            raise HTTPException(status_code=400, detail="Unknown seat")
        link_user = body.link_user
    try:
        started = await sso.start_pin(link_user)
    except Exception as exc:
        logger.info("plex pin failed: %s", _public_error(exc))
        raise HTTPException(status_code=502, detail="Could not reach Plex sign-in") from exc
    return started


@app.get("/api/auth/plex/qr")
async def plex_qr(state: str):
    image = sso.pin_qr(state)
    if not image:
        raise HTTPException(status_code=404, detail="Code expired")
    return Response(content=image, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.get("/api/auth/plex/poll")
async def plex_poll(state: str, request: Request):
    result = await sso.poll_pin(state)
    status = result.get("status")
    if status == "pending":
        return _session_response(request, {"status": "pending"})
    if status == "expired":
        return _session_response(request, {"status": "expired", "detail": "That code expired. Start again."})
    if status == "error":
        return _session_response(request, {"status": "error", "detail": result.get("detail") or "Plex sign-in failed"})
    if status != "claimed":
        return _session_response(request, {"status": "error", "detail": "Plex sign-in failed"})

    profile = result.get("profile") or {}
    link_user = result.get("link_user")
    if link_user:
        owner = current_user(request)
        if not owner or owner.get("role") != "owner":
            return _session_response(request, {"status": "denied", "detail": "Sign in as the owner before linking"})
        try:
            saved = users.remember_identity(link_user, "plex", profile)
        except ValueError as exc:
            return _session_response(request, {"status": "denied", "detail": str(exc)})
        return _session_response(request, {"status": "linked", "user": users.access_public(saved)})

    matched = users.match_identity("plex", profile)
    if not matched:
        name = profile.get("username") or "That Plex account"
        logger.info("plex sign-in denied for %s", name)
        return _session_response(
            request,
            {"status": "denied", "detail": f"{name} is not allowed. Only the owner and the Grok test seat can sign in."},
        )
    saved = users.remember_identity(matched["id"], "plex", profile)
    return _session_response(request, {"status": "ok", "user": users.session_public(saved)}, saved)


@app.get("/api/auth/google/start")
async def google_start(request: Request, link_user: str | None = None):
    if not sso.google_enabled():
        return _auth_error_redirect("unconfigured")
    link = None
    if link_user:
        owner = current_user(request)
        if not owner or owner.get("role") != "owner" or link_user not in {users.OWNER_ID, users.TESTER_ID}:
            return _auth_error_redirect("state")
        link = link_user
    origin = sso.external_origin(request)
    try:
        url = sso.begin_google(origin, link)
    except Exception:
        return _auth_error_redirect("unconfigured")
    return RedirectResponse(url=url, status_code=302)


@app.get("/api/auth/google/callback")
async def google_callback(request: Request, code: str = "", state: str = ""):
    payload = sso.take_google_state(state)
    if not payload or not code:
        return _auth_error_redirect("state")
    origin = str(payload.get("origin") or sso.external_origin(request))
    try:
        profile = await sso.finish_google(code, origin)
    except Exception as exc:
        logger.info("google sign-in failed: %s", _public_error(exc))
        return _auth_error_redirect("google-failed")
    if payload.get("purpose") == "link":
        owner = current_user(request)
        link_user = str(payload.get("link_user") or "")
        if not owner or owner.get("role") != "owner":
            return _auth_error_redirect("state")
        try:
            users.remember_identity(link_user, "google", profile)
        except ValueError:
            return _auth_error_redirect("not-allowed")
        return RedirectResponse(url="/?linked=1", status_code=302)
    matched = users.match_identity("google", profile)
    if not matched:
        logger.info("google sign-in denied for %s", profile.get("email") or "unknown")
        return _auth_error_redirect("not-allowed")
    saved = users.remember_identity(matched["id"], "google", profile)
    response = RedirectResponse(url="/", status_code=302)
    response.set_cookie(
        auth.COOKIE,
        auth.issue_token(saved["id"], int(saved.get("session_version") or 1)),
        max_age=auth.SESSION_TTL,
        httponly=True,
        samesite="lax",
        secure=_secure_cookie(request),
        path="/",
    )
    return response


@app.get("/api/auth/users")
async def get_users(request: Request, _owner: dict = Depends(require_owner)):
    origin = sso.external_origin(request)
    return {
        "users": users.list_access(),
        "tester_password": users.tester_password_hint(),
        "google": {
            "enabled": sso.google_enabled(),
            "redirect": sso.google_redirect(origin),
        },
    }


@app.put("/api/auth/users")
async def put_users(body: AccessBody, _owner: dict = Depends(require_owner)):
    try:
        saved = users.update_users(body.users)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"users": saved, "tester_password": users.tester_password_hint()}


@app.get("/api/settings")
async def get_settings(_user: dict = Depends(require_owner)):
    return settings.public_settings()


@app.put("/api/settings")
async def put_settings(body: SettingsBody, _user: dict = Depends(require_owner)):
    return settings.update_settings(body.model_dump())


@app.post("/api/youtube/cookies")
async def upload_cookies(file: UploadFile = File(...), _user: dict = Depends(require_owner)):
    data = await file.read(1_000_001)
    if len(data) < 16 or len(data) > 1_000_000:
        raise HTTPException(status_code=400, detail="Cookies file is empty or too large")
    text = data.decode("utf-8", "replace")
    if "youtube" not in text.lower() and "google" not in text.lower():
        raise HTTPException(status_code=400, detail="That file does not look like YouTube cookies")
    path = settings.youtube_cookies_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.chmod(path, 0o600)
    return {"ok": True, "youtube_cookies": True}


@app.get("/api/youtube/search")
async def yt_search(q: str = "", limit: int = 24, _user: None = Depends(require_user)):
    try:
        return {"items": await youtube.search(q, limit)}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=_public_error(exc)) from exc


@app.get("/api/youtube/video/{video_id}")
async def yt_video(video_id: str, _user: None = Depends(require_user)):
    try:
        return await youtube.video(video_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=_public_error(exc)) from exc


@app.get("/api/youtube/thumb")
async def yt_thumb(u: str, _user: None = Depends(require_user)):
    if not youtube.thumb_allowed(u):
        raise HTTPException(status_code=400, detail="Thumbnail host is not allowed")
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        upstream = await client.get(u)
    if upstream.status_code >= 400:
        raise HTTPException(status_code=502, detail="Thumbnail unavailable")
    media_type = upstream.headers.get("content-type", "image/jpeg").split(";")[0]
    return Response(content=upstream.content, media_type=media_type, headers={"Cache-Control": "public, max-age=86400"})


def _quality(value: str | None) -> str:
    if value in settings.QUALITY_IDS:
        return value
    return settings.public_settings()["default_quality"]


def _stream_headers(mode: str) -> dict[str, str]:
    return {
        "Cache-Control": "no-store",
        "X-Accel-Buffering": "no",
        "X-Vela-Mode": mode,
    }


async def _fmp4_response(cmd: list[str], mode: str, on_close=None):
    try:
        iterator = iter_ffmpeg(cmd, on_close=on_close, kind="fmp4")
        # Pull the first chunk so ffmpeg failures become HTTP errors
        # instead of a 200 with an empty body.
        first = await iterator.__anext__()
    except StopAsyncIteration:
        raise HTTPException(status_code=502, detail="Stream ended immediately")
    except StreamError as exc:
        raise HTTPException(status_code=502, detail=_public_error(exc)) from exc

    async def body():
        yield first
        async for chunk in iterator:
            yield chunk

    return StreamingResponse(body(), media_type="application/octet-stream", headers=_stream_headers(mode))


@app.get("/api/youtube/stream/{video_id}")
async def yt_stream(
    video_id: str,
    quality: str | None = None,
    start: float = 0,
    _user: None = Depends(require_user),
):
    start = max(0.0, min(start, 60 * 60 * 12))
    try:
        resolved = await youtube.resolve(video_id, _quality(quality))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=_public_error(exc)) from exc
    cmd = fmp4_command(
        resolved["video_url"],
        resolved.get("audio_url"),
        video_headers=resolved.get("video_headers"),
        audio_headers=resolved.get("audio_headers"),
        start=start,
        transcode_video=resolved["transcode_video"],
        transcode_audio=resolved["transcode_audio"],
        scale_height=resolved.get("scale_height"),
        with_audio=resolved.get("with_audio", True),
    )
    logger.info("youtube %s mode=%s quality=%s start=%s", video_id, resolved["mode"], quality, int(start))
    return await _fmp4_response(cmd, resolved["mode"])


@app.get("/api/plex/status")
async def plex_status(_user: None = Depends(require_user)):
    return await plex.status()


@app.get("/api/plex/libraries")
async def plex_libraries(_user: None = Depends(require_user)):
    try:
        return {"items": await plex.libraries()}
    except plex.PlexError as exc:
        raise HTTPException(status_code=502, detail=_public_error(exc)) from exc


@app.get("/api/plex/library/{section}/items")
async def plex_items(
    section: str,
    q: str = "",
    start: int = 0,
    size: int = 36,
    _user: None = Depends(require_user),
):
    try:
        return await plex.library_items(section, query=q, start=start, size=size)
    except plex.PlexError as exc:
        raise HTTPException(status_code=502, detail=_public_error(exc)) from exc


@app.get("/api/plex/metadata/{rating_key}")
async def plex_metadata(rating_key: str, _user: None = Depends(require_user)):
    try:
        return await plex.metadata(rating_key)
    except plex.PlexError as exc:
        raise HTTPException(status_code=502, detail=_public_error(exc)) from exc


@app.get("/api/plex/children/{rating_key}")
async def plex_children(rating_key: str, _user: None = Depends(require_user)):
    try:
        return await plex.children(rating_key)
    except plex.PlexError as exc:
        raise HTTPException(status_code=502, detail=_public_error(exc)) from exc


@app.get("/api/plex/image")
async def plex_image(path: str, _user: None = Depends(require_user)):
    try:
        content, media_type = await plex.image_bytes(path)
    except plex.PlexError as exc:
        raise HTTPException(status_code=502, detail=_public_error(exc)) from exc
    return Response(content=content, media_type=media_type, headers={"Cache-Control": "private, max-age=3600"})


@app.get("/api/plex/stream/{rating_key}")
async def plex_stream(
    rating_key: str,
    quality: str | None = None,
    start: float = 0,
    media: int = 0,
    part: int = 0,
    _user: None = Depends(require_user),
):
    start = max(0.0, min(start, 60 * 60 * 12))
    try:
        resolved = await plex.resolve(
            rating_key,
            quality=_quality(quality),
            start=start,
            media_index=max(0, media),
            part_index=max(0, part),
        )
    except plex.PlexError as exc:
        raise HTTPException(status_code=502, detail=_public_error(exc)) from exc

    async def _stop():
        await plex.stop_session(resolved.get("session"))

    if resolved["mode"] == "plex":
        cmd = hls_remux_command(resolved["m3u8"], headers=resolved.get("video_headers"))
    else:
        cmd = fmp4_command(
            resolved["video_url"],
            None,
            video_headers=resolved.get("video_headers"),
            start=start,
            transcode_video=False,
            transcode_audio=resolved["transcode_audio"],
            with_audio=True,
        )
    logger.info("plex %s mode=%s quality=%s start=%s", rating_key, resolved["mode"], quality, int(start))
    return await _fmp4_response(cmd, resolved["mode"], on_close=_stop)


async def _fmp4_socket(ws: WebSocket, cmd: list[str], on_close=None) -> None:
    """Push fragmented MP4 over a websocket.

    An HTTP stream sits behind Cloudflare and Nginx until those buffers fill,
    so the canvas never gets a header. Socket frames are flushed as they exist.
    The browser sends "hold" / "go" when its decode queue gets ahead.
    """
    paused = asyncio.Event()
    paused.set()
    stopped = asyncio.Event()

    async def _controls() -> None:
        try:
            while True:
                msg = await ws.receive_text()
                if msg == "hold":
                    paused.clear()
                elif msg == "go":
                    paused.set()
        except Exception:
            stopped.set()
            paused.set()

    control = asyncio.create_task(_controls())
    try:
        async for chunk in iter_ffmpeg(cmd, on_close=on_close, kind="fmp4"):
            if stopped.is_set():
                break
            await paused.wait()
            if stopped.is_set():
                break
            await ws.send_bytes(chunk)
    except WebSocketDisconnect:
        return
    except Exception as exc:
        logger.info("stream closed: %s", _public_error(exc))
        try:
            await ws.send_json({"error": _public_error(exc)})
        except Exception:
            pass
    finally:
        control.cancel()
        try:
            await ws.close()
        except Exception:
            pass


@app.websocket("/api/ws/youtube/{video_id}")
async def ws_youtube(
    ws: WebSocket,
    video_id: str,
    quality: str | None = None,
    start: float = 0,
):
    if not _authed(ws):
        await ws.close(code=4401)
        return
    await ws.accept()
    start = max(0.0, min(start, 60 * 60 * 12))
    try:
        resolved = await youtube.resolve(video_id, _quality(quality))
    except Exception as exc:
        await ws.send_json({"error": _public_error(exc)})
        await ws.close()
        return
    cmd = fmp4_command(
        resolved["video_url"],
        resolved.get("audio_url"),
        video_headers=resolved.get("video_headers"),
        audio_headers=resolved.get("audio_headers"),
        start=start,
        transcode_video=resolved["transcode_video"],
        transcode_audio=resolved["transcode_audio"],
        scale_height=resolved.get("scale_height"),
        with_audio=resolved.get("with_audio", True),
    )
    logger.info("youtube %s mode=%s quality=%s start=%s", video_id, resolved["mode"], quality, int(start))
    await _fmp4_socket(ws, cmd)


@app.websocket("/api/ws/plex/{rating_key}")
async def ws_plex(
    ws: WebSocket,
    rating_key: str,
    quality: str | None = None,
    start: float = 0,
    media: int = 0,
    part: int = 0,
):
    if not _authed(ws):
        await ws.close(code=4401)
        return
    await ws.accept()
    start = max(0.0, min(start, 60 * 60 * 12))
    try:
        resolved = await plex.resolve(
            rating_key,
            quality=_quality(quality),
            start=start,
            media_index=max(0, media),
            part_index=max(0, part),
        )
    except plex.PlexError as exc:
        await ws.send_json({"error": _public_error(exc)})
        await ws.close()
        return

    async def _stop():
        await plex.stop_session(resolved.get("session"))

    if resolved["mode"] == "plex":
        cmd = hls_remux_command(resolved["m3u8"], headers=resolved.get("video_headers"))
    else:
        cmd = fmp4_command(
            resolved["video_url"],
            None,
            video_headers=resolved.get("video_headers"),
            start=start,
            transcode_video=False,
            transcode_audio=resolved["transcode_audio"],
            with_audio=True,
        )
    logger.info("plex %s mode=%s quality=%s start=%s", rating_key, resolved["mode"], quality, int(start))
    await _fmp4_socket(ws, cmd, on_close=_stop)


async def _compat_socket(ws: WebSocket, cmd: list[str], on_close=None) -> None:
    if not _authed(ws):
        await ws.close(code=4401)
        return
    await ws.accept()
    try:
        async for chunk in iter_ffmpeg(cmd, on_close=on_close, kind="mpegts"):
            await ws.send_bytes(chunk)
    except WebSocketDisconnect:
        return
    except Exception as exc:
        logger.info("compatibility stream closed: %s", _public_error(exc))
    finally:
        try:
            await ws.close()
        except Exception:
            pass


@app.websocket("/api/compat/youtube/{video_id}")
async def compat_youtube(
    ws: WebSocket,
    video_id: str,
    quality: str | None = None,
    start: float = 0,
):
    if not _authed(ws):
        await ws.close(code=4401)
        return
    try:
        resolved = await youtube.resolve(video_id, _quality(quality if quality != "max" else "720"))
    except Exception as exc:
        await ws.accept()
        await ws.send_json({"error": _public_error(exc)})
        await ws.close()
        return
    height = 720 if _quality(quality) in {"max", "1080"} else int(_quality(quality))
    cmd = mpeg1_command(
        resolved["video_url"],
        resolved.get("audio_url"),
        video_headers=resolved.get("video_headers"),
        audio_headers=resolved.get("audio_headers"),
        start=max(0.0, start),
        height=height,
    )
    await _compat_socket(ws, cmd)


@app.websocket("/api/compat/plex/{rating_key}")
async def compat_plex(
    ws: WebSocket,
    rating_key: str,
    quality: str | None = None,
    start: float = 0,
    media: int = 0,
    part: int = 0,
):
    if not _authed(ws):
        await ws.close(code=4401)
        return
    try:
        resolved = await plex.resolve(
            rating_key,
            quality=_quality(quality if quality != "max" else "720"),
            start=max(0.0, start),
            media_index=max(0, media),
            part_index=max(0, part),
        )
    except Exception as exc:
        await ws.accept()
        await ws.send_json({"error": _public_error(exc)})
        await ws.close()
        return

    async def _stop():
        await plex.stop_session(resolved.get("session"))

    if resolved.get("m3u8"):
        cmd = mpeg1_command(resolved["m3u8"], None, video_headers=resolved.get("video_headers"), height=720)
    else:
        cmd = mpeg1_command(
            resolved["video_url"],
            None,
            video_headers=resolved.get("video_headers"),
            start=max(0.0, start),
            height=720,
        )
    await _compat_socket(ws, cmd, on_close=_stop)


@app.get("/")
async def index():
    return FileResponse(WEB / "index.html")


app.mount("/css", StaticFiles(directory=WEB / "css"), name="css")
app.mount("/js", StaticFiles(directory=WEB / "js"), name="js")
app.mount("/vendor", StaticFiles(directory=WEB / "js" / "vendor"), name="vendor")


@app.get("/favicon.svg")
async def favicon():
    return FileResponse(WEB / "favicon.svg")


@app.get("/robots.txt")
async def robots():
    return FileResponse(WEB / "robots.txt")
