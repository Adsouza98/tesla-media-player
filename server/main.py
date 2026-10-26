"""Vela HTTP server. The Tesla only ever talks to this process."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth, plex, settings, youtube
from .ffmpeg_cmd import fmp4_command, hls_remux_command, mpeg1_command
from .streaming import StreamError, iter_ffmpeg

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger("vela")

WEB = Path(__file__).resolve().parent.parent / "web"
app = FastAPI(title="Vela", version=settings.VERSION, docs_url=None, redoc_url=None)

_SECRETISH = re.compile(r"(?i)(X-Plex-Token:\s*\S+|token=|[?&]X-Plex-Token=)[^\s&]*")


def _public_error(exc: Exception) -> str:
    text = _SECRETISH.sub("token=***", str(exc))
    text = re.sub(r"(?i)(password|cookie)[=:]\s*\S+", r"\1=***", text)
    return text.strip()[:500] or "Request failed"


def _authed(conn) -> bool:
    return auth.token_ok(conn.cookies.get(auth.COOKIE))


def require_user(request: Request) -> None:
    if not _authed(request):
        raise HTTPException(status_code=401, detail="Sign in required")


class LoginBody(BaseModel):
    password: str


class SettingsBody(BaseModel):
    plex_url: str | None = None
    plex_token: str | None = None
    default_quality: str | None = None


@app.on_event("startup")
def _startup() -> None:
    source = settings.password_source()
    # Touch the password so a generated one exists before the first request.
    settings.load_password()
    if source == "environment":
        logger.info("Vela %s ready (password from environment)", settings.VERSION)
    else:
        logger.warning(
            "APP_PASSWORD is not set. A password was written to %s — set APP_PASSWORD to replace it.",
            settings.PASSWORD_PATH,
        )


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
    return {"authenticated": _authed(request), "password_source": settings.password_source()}


@app.post("/api/auth/login")
async def login(body: LoginBody, request: Request):
    if not auth.password_ok(body.password):
        raise HTTPException(status_code=401, detail="Wrong password")
    secure = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    response = Response(content='{"ok":true}', media_type="application/json")
    response.set_cookie(
        auth.COOKIE,
        auth.issue_token(),
        max_age=auth.SESSION_TTL,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )
    return response


@app.post("/api/auth/logout")
async def logout():
    response = Response(content='{"ok":true}', media_type="application/json")
    response.delete_cookie(auth.COOKIE, path="/")
    return response


@app.get("/api/settings")
async def get_settings(_user: None = Depends(require_user)):
    return settings.public_settings()


@app.put("/api/settings")
async def put_settings(body: SettingsBody, _user: None = Depends(require_user)):
    return settings.update_settings(body.model_dump())


@app.post("/api/youtube/cookies")
async def upload_cookies(file: UploadFile = File(...), _user: None = Depends(require_user)):
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

    return StreamingResponse(body(), media_type="video/mp4", headers=_stream_headers(mode))


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
