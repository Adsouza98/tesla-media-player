"""ffmpeg command builders.

Output is fragmented MP4 (empty moov + moof) so the browser can demux it
incrementally. Video is H.264. Audio is AAC. The picture is never handed to
a <video> element.
"""

from __future__ import annotations

FMP4_FLAGS = "frag_keyframe+empty_moov+default_base_moof"

# Height targets. "max" is resolved by the caller.
QUALITY_HEIGHT = {"480": 480, "720": 720, "1080": 1080, "max": None}
PLEX_LADDER = {
    "480": ("854x480", 2500),
    "720": ("1280x720", 4500),
    "1080": ("1920x1080", 10000),
    "max": ("1920x1080", 16000),
}


def _base() -> list[str]:
    return [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-rw_timeout",
        "20000000",
    ]


def _input(url: str, headers: str | None, start: float, *, hls: bool = False) -> list[str]:
    args: list[str] = []
    if url.startswith(("http://", "https://")) and not hls:
        args += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
    if hls:
        args += [
            "-protocol_whitelist",
            "file,http,https,tcp,tls,crypto,data,httpproxy",
            "-allowed_extensions",
            "ALL",
        ]
    if start and start > 0:
        args += ["-ss", f"{start:.3f}"]
    if headers:
        args += ["-headers", headers]
    args += ["-i", url]
    return args


def _fmp4_tail(transcode_video: bool, audio_mode: str, scale_height: int | None) -> list[str]:
    args: list[str] = ["-avoid_negative_ts", "make_zero"]
    if transcode_video:
        vf = "format=yuv420p"
        if scale_height:
            vf = f"scale=-2:'min({int(scale_height)},ih)':flags=bicubic,format=yuv420p"
        args += [
            "-vf",
            vf,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "21",
            "-profile:v",
            "high",
            "-level",
            "4.1",
            "-g",
            "60",
            "-keyint_min",
            "60",
        ]
    else:
        args += ["-c:v", "copy", "-tag:v", "avc1"]
    if audio_mode == "aac":
        args += ["-c:a", "aac", "-ac", "2", "-b:a", "160k", "-ar", "48000"]
    elif audio_mode == "copy":
        args += ["-c:a", "copy", "-bsf:a", "aac_adtstoasc"]
    args += [
        "-movflags",
        FMP4_FLAGS,
        "-frag_duration",
        "1000000",
        "-f",
        "mp4",
        "pipe:1",
    ]
    return args


def fmp4_command(
    video_url: str,
    audio_url: str | None,
    *,
    video_headers: str | None = None,
    audio_headers: str | None = None,
    start: float = 0,
    transcode_video: bool = False,
    transcode_audio: bool = True,
    scale_height: int | None = None,
    with_audio: bool = True,
) -> list[str]:
    cmd = _base()
    cmd += _input(video_url, video_headers, start)
    if audio_url:
        cmd += _input(audio_url, audio_headers, start)
        cmd += ["-map", "0:v:0", "-map", "1:a:0"]
        audio_mode = "aac" if transcode_audio else "copy"
    elif with_audio:
        cmd += ["-map", "0:v:0", "-map", "0:a:0?"]
        audio_mode = "aac" if transcode_audio else "copy"
    else:
        cmd += ["-map", "0:v:0"]
        audio_mode = "none"
    cmd += _fmp4_tail(transcode_video, audio_mode, scale_height)
    return cmd


def hls_remux_command(m3u8_url: str, *, headers: str | None = None) -> list[str]:
    """Remux a Plex HLS session (already H.264/AAC) into fragmented MP4."""
    cmd = _base()
    cmd += _input(m3u8_url, headers, 0, hls=True)
    cmd += ["-map", "0:v:0", "-map", "0:a:0?"]
    cmd += _fmp4_tail(False, "copy", None)
    return cmd


def mpeg1_command(
    video_url: str,
    audio_url: str | None,
    *,
    video_headers: str | None = None,
    audio_headers: str | None = None,
    start: float = 0,
    height: int = 720,
) -> list[str]:
    """JSMpeg fallback. MPEG-1 + MP2 inside MPEG-TS. CPU-heavy; not the default."""
    cmd = _base()
    cmd += _input(video_url, video_headers, start)
    if audio_url:
        cmd += _input(audio_url, audio_headers, start)
        cmd += ["-map", "0:v:0", "-map", "1:a:0"]
    else:
        cmd += ["-map", "0:v:0", "-map", "0:a:0?"]
    cmd += [
        "-vf",
        f"scale=-2:'min({int(height)},ih)'",
        "-c:v",
        "mpeg1video",
        "-b:v",
        "3500k",
        "-maxrate",
        "4000k",
        "-bufsize",
        "2000k",
        "-r",
        "30",
        "-bf",
        "0",
        "-c:a",
        "mp2",
        "-b:a",
        "160k",
        "-ar",
        "44100",
        "-ac",
        "2",
        "-f",
        "mpegts",
        "pipe:1",
    ]
    return cmd


def header_blob(headers: dict | None) -> str | None:
    if not headers:
        return None
    lines = []
    for key, value in headers.items():
        if not key or value is None:
            continue
        if str(key).lower() == "accept-encoding":
            continue
        lines.append(f"{key}: {value}")
    if not lines:
        return None
    return "\r\n".join(lines) + "\r\n"
