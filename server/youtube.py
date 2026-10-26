"""Resolve YouTube streams with yt-dlp. URLs stay on the server."""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from urllib.parse import urlparse

from .ffmpeg_cmd import header_blob
from .settings import cookies_ready, youtube_cookies_path

logger = logging.getLogger("vela.youtube")

VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
THUMB_HOSTS = {"i.ytimg.com", "img.youtube.com", "yt3.ggpht.com", "yt3.googleusercontent.com"}


def canonical_id(value: str) -> str | None:
    value = (value or "").strip()
    if VIDEO_ID.match(value):
        return value
    match = re.search(r"(?:v=|youtu\.be/|shorts/|embed/)([A-Za-z0-9_-]{11})", value)
    if match:
        return match.group(1)
    return None


def _ydl_opts(*, flat: bool = False) -> dict:
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "extract_flat": "in_playlist" if flat else False,
        "remote_components": ["ejs:github"],
    }
    runtimes = {}
    if shutil.which("deno"):
        runtimes["deno"] = {}
    if shutil.which("node"):
        runtimes["node"] = {}
    if runtimes:
        opts["js_runtimes"] = runtimes
    if cookies_ready():
        opts["cookiefile"] = str(youtube_cookies_path())
    return opts


def _is_h264(fmt: dict) -> bool:
    codec = (fmt.get("vcodec") or "").lower()
    return codec.startswith("avc1") or codec.startswith("h264")


def _is_aac(fmt: dict) -> bool:
    codec = (fmt.get("acodec") or "").lower()
    return codec.startswith("mp4a") or codec.startswith("aac")


def _video_formats(formats: list[dict]) -> list[dict]:
    picked = []
    for fmt in formats:
        if not fmt.get("url"):
            continue
        vcodec = fmt.get("vcodec")
        if not vcodec or vcodec == "none":
            continue
        protocol = (fmt.get("protocol") or "")
        if "mhtml" in protocol or fmt.get("format_note") == "storyboard":
            continue
        if not fmt.get("height"):
            continue
        picked.append(fmt)
    return picked


def _audio_formats(formats: list[dict]) -> list[dict]:
    picked = []
    for fmt in formats:
        if not fmt.get("url"):
            continue
        acodec = fmt.get("acodec")
        vcodec = fmt.get("vcodec")
        if not acodec or acodec == "none":
            continue
        if vcodec not in (None, "none"):
            continue
        picked.append(fmt)
    return picked


def _best(formats: list[dict], *, height: int | None = None, prefer=None) -> dict | None:
    pool = formats
    if prefer:
        preferred = [fmt for fmt in formats if prefer(fmt)]
        if preferred:
            pool = preferred
    if height:
        under = [fmt for fmt in pool if (fmt.get("height") or 0) <= height]
        if under:
            pool = under
    if not pool:
        return None
    return max(pool, key=lambda fmt: (fmt.get("height") or 0, fmt.get("tbr") or fmt.get("abr") or 0))


def choose_streams(formats: list[dict], quality: str) -> dict:
    """Pick a video URL and an audio URL. Prefer H.264 + AAC so ffmpeg can copy video."""
    quality = quality if quality in {"480", "720", "1080", "max"} else "1080"
    target = None if quality == "max" else int(quality)
    videos = _video_formats(formats)
    audios = _audio_formats(formats)
    h264 = [fmt for fmt in videos if _is_h264(fmt)]

    video = _best(h264, height=target) if h264 else None
    transcode_video = False
    if video is None:
        video = _best(videos, height=target or 1080)
        transcode_video = video is not None
    elif quality == "max":
        # Highest H.264, even above 1080, still copied.
        video = _best(h264, height=None)

    # Combined progressive file if separate audio is missing.
    audio = _best(audios, prefer=_is_aac) or _best(audios)
    transcode_audio = True
    if audio and _is_aac(audio):
        transcode_audio = False
    if audio is None and video and (video.get("acodec") not in (None, "none")):
        audio = None
        transcode_audio = not _is_aac(video)

    scale = None
    if transcode_video:
        scale = 1080 if quality == "max" else int(quality)

    return {
        "video": video,
        "audio": audio,
        "transcode_video": transcode_video,
        "transcode_audio": transcode_audio if audio or video else True,
        "scale_height": scale,
        "height": (video or {}).get("height"),
        "vcodec": (video or {}).get("vcodec"),
        "note": "transcode" if transcode_video else "remux",
    }


def quality_menu(formats: list[dict]) -> list[dict]:
    videos = _video_formats(formats)
    h264_heights = sorted({int(fmt["height"]) for fmt in videos if _is_h264(fmt) and fmt.get("height")})
    best_h264 = h264_heights[-1] if h264_heights else 0
    menu = []
    for q, label in (("480", "480p"), ("720", "720p"), ("1080", "1080p"), ("max", "Max")):
        if q == "max":
            available = bool(videos)
            note = f"H.264 {best_h264}p" if best_h264 else "transcode"
        else:
            height = int(q)
            available = any((fmt.get("height") or 0) >= height - 40 for fmt in videos) or bool(videos)
            note = "copy" if best_h264 >= height - 40 else "transcode"
        menu.append({"id": q, "label": label, "available": available, "note": note})
    return menu


def _thumb(video_id: str) -> str:
    return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"


def _summary(entry: dict) -> dict | None:
    video_id = entry.get("id") or canonical_id(entry.get("url") or "")
    if not video_id or not VIDEO_ID.match(str(video_id)):
        return None
    duration = entry.get("duration")
    try:
        duration = int(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration = None
    return {
        "id": video_id,
        "title": entry.get("title") or "Untitled",
        "channel": entry.get("channel") or entry.get("uploader") or "",
        "duration": duration,
        "thumbnail": f"/api/youtube/thumb?u={_thumb(video_id)}",
    }


def _search_sync(query: str, limit: int) -> list[dict]:
    import yt_dlp

    opts = _ydl_opts(flat=True)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
    entries = (info or {}).get("entries") or []
    results = []
    for entry in entries:
        if not entry:
            continue
        item = _summary(entry)
        if item:
            results.append(item)
    return results


def _video_sync(video_id: str) -> dict:
    import yt_dlp
    from yt_dlp.utils import DownloadError

    opts = _ydl_opts(flat=False)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
    except DownloadError as exc:
        raise RuntimeError(str(exc).split("\n")[0][:400]) from exc
    if not info:
        raise RuntimeError("YouTube returned nothing for that video")
    summary = _summary(info) or {"id": video_id, "title": info.get("title") or video_id}
    summary["description"] = (info.get("description") or "")[:280]
    summary["qualities"] = quality_menu(info.get("formats") or [])
    summary["live"] = bool(info.get("is_live"))
    summary["cookies"] = cookies_ready()
    return summary


def _resolve_sync(video_id: str, quality: str) -> dict:
    import yt_dlp
    from yt_dlp.utils import DownloadError

    opts = _ydl_opts(flat=False)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
    except DownloadError as exc:
        raise RuntimeError(str(exc).split("\n")[0][:400]) from exc
    if info.get("is_live"):
        raise RuntimeError("Live streams are not supported in this version")
    chosen = choose_streams(info.get("formats") or [], quality)
    if not chosen["video"]:
        raise RuntimeError("No playable video format was found")
    video = chosen["video"]
    audio = chosen["audio"]
    with_audio = bool(audio) or (video.get("acodec") not in (None, "none"))
    return {
        "title": info.get("title") or video_id,
        "duration": info.get("duration"),
        "video_url": video["url"],
        "audio_url": audio.get("url") if audio else None,
        "video_headers": header_blob(video.get("http_headers")),
        "audio_headers": header_blob((audio or {}).get("http_headers")),
        "transcode_video": chosen["transcode_video"],
        "transcode_audio": chosen["transcode_audio"],
        "scale_height": chosen["scale_height"],
        "with_audio": with_audio,
        "mode": chosen["note"],
        "height": chosen["height"],
    }


async def search(query: str, limit: int = 24) -> list[dict]:
    query = query.strip()
    if not query:
        return []
    direct = canonical_id(query)
    if direct and ("youtube" in query or "youtu.be" in query or VIDEO_ID.match(query)):
        try:
            one = await video(direct)
            return [one]
        except Exception:
            logger.info("direct youtube lookup failed, falling back to search")
    return await asyncio.to_thread(_search_sync, query, max(1, min(limit, 30)))


async def video(video_id: str) -> dict:
    if not VIDEO_ID.match(video_id):
        raise RuntimeError("That does not look like a YouTube video id")
    return await asyncio.to_thread(_video_sync, video_id)


async def resolve(video_id: str, quality: str) -> dict:
    if not VIDEO_ID.match(video_id):
        raise RuntimeError("That does not look like a YouTube video id")
    return await asyncio.to_thread(_resolve_sync, video_id, quality)


def thumb_allowed(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and parsed.hostname in THUMB_HOSTS
