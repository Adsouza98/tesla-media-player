"""ffmpeg really does emit a fragmented MP4 the player can start on."""

import asyncio
import os
import subprocess
import tempfile
import unittest

from server.ffmpeg_cmd import fmp4_command
from server.streaming import iter_ffmpeg


class StreamTests(unittest.TestCase):
    def test_local_remux_starts_with_ftyp(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "src.mp4")
            subprocess.check_call(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30:duration=1",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "30",
                    "-c:a", "aac", "-shortest", src,
                ]
            )
            cmd = fmp4_command(src, None, transcode_video=False, transcode_audio=False, with_audio=True)

            async def collect():
                chunks = []
                async for chunk in iter_ffmpeg(cmd):
                    chunks.append(chunk)
                return b"".join(chunks)

            data = asyncio.run(collect())
            self.assertIn(b"ftyp", data[:64])


if __name__ == "__main__":
    unittest.main()
