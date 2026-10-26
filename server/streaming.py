"""Run ffmpeg and yield bytes once a real container header is on the pipe."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable

logger = logging.getLogger("vela.stream")

STREAMS = asyncio.Semaphore(2)


class StreamError(RuntimeError):
    pass


def _container_ready(buf: bytearray, kind: str) -> bool:
    if kind == "mpegts":
        return len(buf) >= 188 and buf[0] == 0x47
    return b"ftyp" in buf


async def _drain_stderr(proc: asyncio.subprocess.Process, bucket: list[str]) -> None:
    assert proc.stderr is not None
    while True:
        line = await proc.stderr.readline()
        if not line:
            break
        text = line.decode("utf-8", "replace").rstrip()
        if text:
            bucket.append(text)
            del bucket[:-40]


async def _kill(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass


async def open_ffmpeg(cmd: list[str], *, kind: str = "fmp4") -> tuple[asyncio.subprocess.Process, bytearray]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdout is not None
    stderr: list[str] = []
    stderr_task = asyncio.create_task(_drain_stderr(proc, stderr))
    buf = bytearray()
    try:
        while not _container_ready(buf, kind):
            chunk = await asyncio.wait_for(proc.stdout.read(16384), timeout=45)
            if not chunk:
                try:
                    await asyncio.wait_for(stderr_task, timeout=2)
                except asyncio.TimeoutError:
                    pass
                detail = " | ".join(stderr[-8:]) or "ffmpeg produced no output"
                raise StreamError(detail[:800])
            buf += chunk
            if len(buf) > 4_000_000:
                break
        if not _container_ready(buf, kind):
            raise StreamError("ffmpeg output was not a playable container")
        return proc, buf
    except Exception:
        stderr_task.cancel()
        await _kill(proc)
        raise


async def iter_ffmpeg(
    cmd: list[str],
    on_close: Callable[[], Awaitable[None]] | None = None,
    *,
    kind: str = "fmp4",
) -> AsyncIterator[bytes]:
    try:
        await asyncio.wait_for(STREAMS.acquire(), timeout=0.2)
    except asyncio.TimeoutError as exc:
        raise StreamError("This server is already playing the maximum number of streams") from exc

    try:
        proc, buf = await open_ffmpeg(cmd, kind=kind)
    except Exception:
        STREAMS.release()
        if on_close:
            await on_close()
        raise

    try:
        yield bytes(buf)
        assert proc.stdout is not None
        while True:
            chunk = await proc.stdout.read(64 * 1024)
            if not chunk:
                break
            yield chunk
    finally:
        await _kill(proc)
        STREAMS.release()
        if on_close:
            try:
                await on_close()
            except Exception:
                logger.exception("stream cleanup failed")
