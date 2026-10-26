"""Plex browsing plus a playback plan.

Direct H.264 is remuxed locally (almost no CPU). Anything else is handed to
Plex's own transcoder so a Plex Pass hardware session can do the heavy work,
then remuxed to fragmented MP4 for the canvas player.
"""

from __future__ import annotations

import logging
import uuid
from urllib.parse import urlencode

import httpx

from .ffmpeg_cmd import PLEX_LADDER, header_blob
from .settings import secret_settings

logger = logging.getLogger("vela.plex")

CLIENT_ID = "vela-cabin-player"
PROFILE = (
    "add-transcode-target("
    "type=videoProfile"
    "&context=streaming"
    "&protocol=hls"
    "&container=mpegts"
    "&videoCodec=h264"
    "&audioCodec=aac"
    "&replace=true)"
)


class PlexError(RuntimeError):
    pass


def _cfg() -> tuple[str, str]:
    cfg = secret_settings()
    base = (cfg.get("plex_url") or "").rstrip("/")
    token = cfg.get("plex_token") or ""
    if not base or not token:
        raise PlexError("Plex URL and token are not set. Add them in Settings.")
    return base, token


def _headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "X-Plex-Token": token,
        "X-Plex-Client-Identifier": CLIENT_ID,
        "X-Plex-Product": "Vela",
        "X-Plex-Version": "0.1.0",
        "X-Plex-Platform": "Chrome",
        "X-Plex-Platform-Version": "120.0",
        "X-Plex-Device": "Tesla",
        "X-Plex-Device-Name": "Vela Cabin",
        "X-Plex-Language": "en",
    }


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(40, connect=8), follow_redirects=True)


async def _get(path: str, params: dict | None = None) -> dict:
    base, token = _cfg()
    if not path.startswith("/"):
        path = "/" + path
    async with _client() as client:
        response = await client.get(f"{base}{path}", params=params, headers=_headers(token))
    if response.status_code >= 400:
        raise PlexError(f"Plex returned {response.status_code}")
    data = response.json()
    return data.get("MediaContainer", data)


def _item(raw: dict) -> dict:
    duration_ms = raw.get("duration") or 0
    return {
        "ratingKey": str(raw.get("ratingKey") or ""),
        "title": raw.get("title") or "Untitled",
        "type": raw.get("type") or "",
        "year": raw.get("year"),
        "summary": (raw.get("summary") or "")[:400],
        "duration": int(duration_ms / 1000) if duration_ms else None,
        "thumb": raw.get("thumb") or raw.get("parentThumb") or raw.get("grandparentThumb"),
        "art": raw.get("art"),
        "index": raw.get("index"),
        "parentIndex": raw.get("parentIndex"),
        "parentTitle": raw.get("parentTitle"),
        "grandparentTitle": raw.get("grandparentTitle"),
        "leafCount": raw.get("leafCount"),
        "childCount": raw.get("childCount"),
        "viewed": bool(raw.get("viewCount")),
    }


def _safe_path(path: str) -> str:
    if not path.startswith("/") or ".." in path or "://" in path:
        raise PlexError("Invalid Plex path")
    return path


async def status() -> dict:
    try:
        container = await _get("/identity")
    except PlexError as exc:
        return {"ok": False, "error": str(exc)}
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"Cannot reach Plex ({exc.__class__.__name__})"}
    return {
        "ok": True,
        "name": container.get("friendlyName") or "Plex",
        "version": container.get("version"),
    }


async def libraries() -> list[dict]:
    container = await _get("/library/sections")
    items = []
    for directory in container.get("Directory") or []:
        if directory.get("type") not in {"movie", "show"}:
            continue
        items.append(
            {
                "key": str(directory.get("key")),
                "title": directory.get("title") or "Library",
                "type": directory.get("type"),
            }
        )
    return items


async def library_items(section: str, *, query: str = "", start: int = 0, size: int = 36) -> dict:
    if not str(section).isdigit():
        raise PlexError("Invalid library")
    params = {
        "X-Plex-Container-Start": max(0, start),
        "X-Plex-Container-Size": max(1, min(size, 80)),
        "includeGuids": 0,
    }
    if query.strip():
        path = f"/library/sections/{section}/search"
        params["query"] = query.strip()
    else:
        path = f"/library/sections/{section}/all"
    container = await _get(path, params)
    return {
        "title": container.get("title1") or container.get("librarySectionTitle") or "",
        "total": container.get("totalSize") or container.get("size") or 0,
        "items": [_item(raw) for raw in container.get("Metadata") or []],
    }


async def metadata(rating_key: str) -> dict:
    if not str(rating_key).isdigit():
        raise PlexError("Invalid item")
    container = await _get(f"/library/metadata/{rating_key}")
    rows = container.get("Metadata") or []
    if not rows:
        raise PlexError("Plex could not find that item")
    raw = rows[0]
    item = _item(raw)
    media_rows = []
    for index, media in enumerate(raw.get("Media") or []):
        part = (media.get("Part") or [{}])[0]
        streams = part.get("Stream") or []
        video = next((s for s in streams if s.get("streamType") == 1), {})
        audio = next((s for s in streams if s.get("streamType") == 2), {})
        media_rows.append(
            {
                "index": index,
                "videoCodec": video.get("codec"),
                "audioCodec": audio.get("codec"),
                "width": video.get("width") or media.get("width"),
                "height": video.get("height") or media.get("height"),
                "bitrate": media.get("bitrate"),
                "container": part.get("container") or media.get("container"),
            }
        )
    item["media"] = media_rows
    return item


async def children(rating_key: str) -> dict:
    if not str(rating_key).isdigit():
        raise PlexError("Invalid item")
    container = await _get(f"/library/metadata/{rating_key}/children")
    return {
        "title": container.get("title1") or container.get("title2") or "",
        "items": [_item(raw) for raw in container.get("Metadata") or []],
    }


def playback_plan(media: dict, quality: str) -> str:
    """Return 'remux' when the NAS can copy H.264, else 'plex' to transcode there."""
    quality = quality if quality in PLEX_LADDER else "1080"
    codec = (media.get("videoCodec") or "").lower()
    height = int(media.get("height") or 0)
    h264 = codec in {"h264", "avc"}
    target = None if quality == "max" else int(quality)
    needs_downscale = target is not None and height > target + 16
    if h264 and not needs_downscale:
        return "remux"
    return "plex"


async def _part(rating_key: str, media_index: int, part_index: int) -> tuple[dict, dict, dict]:
    if not str(rating_key).isdigit():
        raise PlexError("Invalid item")
    container = await _get(f"/library/metadata/{rating_key}")
    rows = container.get("Metadata") or []
    if not rows:
        raise PlexError("Plex could not find that item")
    raw = rows[0]
    medias = raw.get("Media") or []
    if media_index >= len(medias):
        raise PlexError("That media version does not exist")
    media = medias[media_index]
    parts = media.get("Part") or []
    if part_index >= len(parts):
        raise PlexError("That file part does not exist")
    part = parts[part_index]
    streams = part.get("Stream") or []
    video = next((s for s in streams if s.get("streamType") == 1), {})
    audio = next((s for s in streams if s.get("streamType") == 2), {})
    info = {
        "title": raw.get("title") or rating_key,
        "duration": int((raw.get("duration") or 0) / 1000) or None,
        "videoCodec": video.get("codec"),
        "audioCodec": audio.get("codec"),
        "width": video.get("width") or media.get("width"),
        "height": video.get("height") or media.get("height"),
        "partKey": part.get("key"),
    }
    return raw, part, info


def _with_token(url: str, token: str) -> str:
    if not token or "X-Plex-Token=" in url:
        return url
    joiner = "&" if "?" in url else "?"
    return f"{url}{joiner}X-Plex-Token={token}"


async def resolve(rating_key: str, *, quality: str, start: float, media_index: int, part_index: int) -> dict:
    _raw, part, info = await _part(rating_key, media_index, part_index)
    if not info.get("partKey"):
        raise PlexError("Plex did not return a file path for that item")
    quality = quality if quality in PLEX_LADDER else "1080"
    mode = playback_plan(info, quality)
    base, token = _cfg()
    headers = header_blob(_headers(token)) or ""
    audio_codec = (info.get("audioCodec") or "").lower()
    aac = audio_codec in {"aac", "mp4a"}

    if mode == "remux":
        return {
            "mode": "remux",
            "title": info["title"],
            "duration": info["duration"],
            "video_url": _with_token(f"{base}{info['partKey']}", token),
            "audio_url": None,
            "video_headers": headers,
            "audio_headers": None,
            "transcode_video": False,
            "transcode_audio": not aac,
            "scale_height": None,
            "session": None,
        }

    resolution, bitrate = PLEX_LADDER[quality]
    session = uuid.uuid4().hex
    params = {
        "hasMDE": 1,
        "path": f"/library/metadata/{rating_key}",
        "mediaIndex": media_index,
        "partIndex": part_index,
        "protocol": "hls",
        "fastSeek": 1,
        "directPlay": 0,
        "directStream": 0,
        "subtitleSize": 100,
        "audioBoost": 100,
        "location": "lan",
        "autoAdjustQuality": 0,
        "mediaBufferSize": 102400,
        "session": session,
        "subtitles": "none",
        "copyts": 0,
        "offset": max(0, int(start)),
        "videoQuality": 100,
        "maxVideoBitrate": bitrate,
        "videoBitrate": bitrate,
        "videoResolution": resolution,
        "videoCodec": "h264",
        "audioCodec": "aac",
        "X-Plex-Platform": "Chrome",
        "X-Plex-Client-Identifier": CLIENT_ID,
        "X-Plex-Product": "Vela",
        "X-Plex-Device": "Tesla",
        "X-Plex-Device-Name": "Vela Cabin",
        "X-Plex-Version": "0.1.0",
        "X-Plex-Client-Profile-Extra": PROFILE,
    }
    m3u8 = _with_token(f"{base}/video/:/transcode/universal/start.m3u8?{urlencode(params)}", token)
    return {
        "mode": "plex",
        "title": info["title"],
        "duration": info["duration"],
        "m3u8": m3u8,
        "video_headers": headers,
        "session": session,
        "start": start,
    }


async def stop_session(session: str | None) -> None:
    if not session:
        return
    try:
        base, token = _cfg()
    except PlexError:
        return
    try:
        async with _client() as client:
            await client.get(
                f"{base}/video/:/transcode/universal/stop",
                params={"session": session},
                headers=_headers(token),
            )
    except httpx.HTTPError:
        logger.info("could not stop plex session")


async def image_bytes(path: str) -> tuple[bytes, str]:
    path = _safe_path(path)
    base, token = _cfg()
    async with _client() as client:
        response = await client.get(f"{base}{path}", headers=_headers(token))
    if response.status_code >= 400:
        raise PlexError(f"Artwork request failed ({response.status_code})")
    media_type = response.headers.get("content-type", "image/jpeg").split(";")[0]
    return response.content, media_type
