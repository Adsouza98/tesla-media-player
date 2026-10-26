import os
import unittest

os.environ.setdefault("APP_PASSWORD", "test-cabin-password")
os.environ.setdefault("CONFIG_DIR", os.path.join(os.path.dirname(__file__), "..", "config"))

from server.auth import issue_token, password_ok, token_ok
from server.ffmpeg_cmd import fmp4_command, header_blob
from server.plex import playback_plan
from server.youtube import choose_streams, thumb_allowed


def fmt(height, vcodec="avc1.640028", acodec="none", url="https://example.test/v", tbr=1000, protocol="https"):
    return {
        "height": height,
        "vcodec": vcodec,
        "acodec": acodec,
        "url": url,
        "tbr": tbr,
        "protocol": protocol,
        "http_headers": {"User-Agent": "vela", "Accept-Encoding": "gzip"},
    }


class ChooseTests(unittest.TestCase):
    def test_prefers_h264_copy_under_target(self):
        formats = [
            fmt(1080, "vp9", tbr=5000),
            fmt(720, "avc1.4d401f", tbr=2000),
            fmt(1080, "avc1.640028", tbr=4000),
            {"vcodec": "none", "acodec": "mp4a.40.2", "url": "https://example.test/a", "abr": 128},
            {"vcodec": "none", "acodec": "opus", "url": "https://example.test/o", "abr": 160},
        ]
        chosen = choose_streams(formats, "1080")
        self.assertEqual(chosen["height"], 1080)
        self.assertFalse(chosen["transcode_video"])
        self.assertFalse(chosen["transcode_audio"])
        self.assertEqual(chosen["audio"]["acodec"], "mp4a.40.2")

    def test_max_keeps_highest_h264(self):
        formats = [fmt(720, "avc1", tbr=1000), fmt(1440, "avc1", tbr=8000), fmt(2160, "vp9", tbr=20000)]
        chosen = choose_streams(formats, "max")
        self.assertEqual(chosen["height"], 1440)
        self.assertFalse(chosen["transcode_video"])

    def test_transcodes_when_only_vp9(self):
        formats = [fmt(1080, "vp9", tbr=4000), {"vcodec": "none", "acodec": "opus", "url": "https://example.test/a", "abr": 128}]
        chosen = choose_streams(formats, "1080")
        self.assertTrue(chosen["transcode_video"])
        self.assertEqual(chosen["scale_height"], 1080)
        self.assertTrue(chosen["transcode_audio"])

    def test_thumb_allowlist(self):
        self.assertTrue(thumb_allowed("https://i.ytimg.com/vi/abcdefghijk/hqdefault.jpg"))
        self.assertFalse(thumb_allowed("https://evil.example/i.ytimg.com/x.jpg"))
        self.assertFalse(thumb_allowed("file:///etc/passwd"))


class PlanTests(unittest.TestCase):
    def test_remux_h264(self):
        self.assertEqual(playback_plan({"videoCodec": "h264", "height": 800}, "1080"), "remux")

    def test_downscale_uses_plex(self):
        self.assertEqual(playback_plan({"videoCodec": "h264", "height": 2160}, "1080"), "plex")

    def test_hevc_uses_plex(self):
        self.assertEqual(playback_plan({"videoCodec": "hevc", "height": 1080}, "max"), "plex")


class CommandTests(unittest.TestCase):
    def test_fmp4_pipe_and_headers(self):
        cmd = fmp4_command(
            "https://example.test/v",
            "https://example.test/a",
            video_headers=header_blob({"User-Agent": "vela", "Accept-Encoding": "gzip"}),
            start=12,
            transcode_video=False,
            transcode_audio=False,
        )
        self.assertEqual(cmd[-1], "pipe:1")
        self.assertIn("frag_keyframe+empty_moov+default_base_moof", cmd)
        self.assertNotIn("gzip", " ".join(cmd))
        self.assertIn("-ss", cmd)

    def test_auth_roundtrip(self):
        self.assertTrue(password_ok("test-cabin-password"))
        self.assertFalse(password_ok("nope"))
        token = issue_token()
        self.assertTrue(token_ok(token))
        self.assertFalse(token_ok(token + "x"))


if __name__ == "__main__":
    unittest.main()
