import os
import unittest

os.environ.setdefault("APP_PASSWORD", "test-cabin-password")
os.environ.setdefault("CONFIG_DIR", os.path.join(os.path.dirname(__file__), "..", "config"))

from server.auth import issue_token, parse_token, password_ok, token_ok
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
        self.assertEqual(parse_token(token)[0], "owner")


class SeatTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path

        from server import settings

        self.tmp = tempfile.TemporaryDirectory()
        self.settings = settings
        self.old_dir = settings.CONFIG_DIR
        settings.CONFIG_DIR = Path(self.tmp.name)
        self.env_keys = (
            "OWNER_EMAIL",
            "OWNER_PLEX_USERNAME",
            "OWNER_NAME",
            "TEST_PASSWORD",
            "TEST_EMAIL",
            "TEST_PLEX_USERNAME",
            "GOOGLE_CLIENT_ID",
            "GOOGLE_CLIENT_SECRET",
            "PUBLIC_URL",
        )
        self.old_env = {key: os.environ.get(key) for key in self.env_keys}
        for key in self.env_keys:
            os.environ.pop(key, None)

    def tearDown(self):
        self.settings.CONFIG_DIR = self.old_dir
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def test_only_two_seats_and_passwords(self):
        from server import users
        from server.auth import issue_token

        users.ensure_users()
        owner = users.authenticate(None, "test-cabin-password")
        self.assertEqual(owner["id"], "owner")
        self.assertIsNone(users.authenticate("juniorchange", "nope"))
        self.assertIsNone(users.authenticate("stranger", "test-cabin-password"))
        hint = users.tester_password_hint()
        self.assertTrue(hint)
        tester = users.authenticate("grok", hint)
        self.assertEqual(tester["role"], "tester")
        self.assertIsNone(users.match_identity("plex", {"id": 1, "username": "someone", "email": "someone@example.com"}))
        os.environ["OWNER_EMAIL"] = "owner@example.com"
        os.environ["OWNER_PLEX_USERNAME"] = "CabinOwner"
        users.ensure_users()
        matched = users.match_identity("plex", {"id": 9, "username": "cabinowner", "email": "other@example.com"})
        self.assertEqual(matched["id"], "owner")
        saved = users.remember_identity("owner", "plex", {"id": 9, "username": "CabinOwner", "email": "owner@example.com"})
        self.assertEqual(saved["plex_id"], "9")
        with self.assertRaises(ValueError):
            users.update_users([
                {"id": "owner", "name": "Andre", "username": "andre", "plex_email": "same@example.com", "google_email": "", "plex_username": "", "password": ""},
                {"id": "grok", "name": "Grok", "username": "grok", "plex_email": "same@example.com", "google_email": "", "plex_username": "", "password": ""},
            ])
        users.update_users([
            {"id": "owner", "name": "Andre", "username": "andre", "plex_email": "owner@example.com", "google_email": "owner@example.com", "plex_username": "CabinOwner", "password": ""},
            {"id": "grok", "name": "Grok", "username": "grok", "plex_email": "", "google_email": "", "plex_username": "", "password": "grok-test-pass"},
        ])
        self.assertIsNone(users.tester_password_hint())
        self.assertEqual(users.authenticate("grok", "grok-test-pass")["id"], "grok")
        token = issue_token("grok", users.get_user("grok")["session_version"])
        self.assertTrue(token_ok(token))
        users.update_users([
            {"id": "owner", "name": "Andre", "username": "andre", "plex_email": "owner@example.com", "google_email": "owner@example.com", "plex_username": "CabinOwner", "password": ""},
            {"id": "grok", "name": "Grok", "username": "grok", "plex_email": "", "google_email": "", "plex_username": "", "password": "another-pass-9"},
        ])
        self.assertIsNone(users.user_for_session("grok", parse_token(token)[1]))

    def test_google_state_is_single_use(self):
        from server import sso

        state = sso.auth.sign_payload({"purpose": "login", "origin": "https://vela.example.com"})
        self.assertEqual(sso.take_google_state(state)["origin"], "https://vela.example.com")
        self.assertIsNone(sso.take_google_state(state))
        self.assertIsNone(sso.take_google_state(state + "x"))


class HttpAuthTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path

        from server import main, settings

        self.tmp = tempfile.TemporaryDirectory()
        self.settings = settings
        self.main = main
        self.old_dir = settings.CONFIG_DIR
        settings.CONFIG_DIR = Path(self.tmp.name)
        main._attempts.clear()

    def tearDown(self):
        self.settings.CONFIG_DIR = self.old_dir
        self.main._attempts.clear()
        self.tmp.cleanup()

    def test_login_me_and_owner_gate(self):
        from fastapi.testclient import TestClient

        from server import users

        with TestClient(self.main.app) as client:
            me = client.get("/api/auth/me")
            self.assertFalse(me.json()["authenticated"])
            self.assertTrue(me.json()["methods"]["plex"])
            denied = client.post("/api/auth/login", json={"password": "nope"})
            self.assertEqual(denied.status_code, 401)
            signed = client.post("/api/auth/login", json={"password": "test-cabin-password"})
            self.assertEqual(signed.status_code, 200)
            self.assertEqual(signed.json()["user"]["role"], "owner")
            self.assertEqual(client.get("/api/auth/me").json()["user"]["username"], "juniorchange")
            seats = client.get("/api/auth/users")
            self.assertEqual(seats.status_code, 200)
            self.assertEqual(len(seats.json()["users"]), 2)
            hint = seats.json()["tester_password"]
            client.post("/api/auth/logout")
            tester = client.post("/api/auth/login", json={"username": "grok", "password": hint})
            self.assertEqual(tester.status_code, 200)
            self.assertEqual(tester.json()["user"]["role"], "tester")
            self.assertEqual(client.get("/api/settings").status_code, 403)
            self.assertEqual(client.get("/api/auth/users").status_code, 403)
            self.assertEqual(client.post("/api/auth/plex/start", json={"link_user": "owner"}).status_code, 401)
            self.assertIsNone(users.match_identity("google", {"sub": "abc", "email": "nobody@example.com"}))


if __name__ == "__main__":
    unittest.main()
