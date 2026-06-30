import importlib
import sys
import types
import unittest
from types import SimpleNamespace


def load_bot_module():
    telegram = types.ModuleType("telegram")
    telegram.Update = object
    telegram.Message = object

    telegram_ext = types.ModuleType("telegram.ext")
    telegram_ext.Application = object
    telegram_ext.MessageHandler = object
    telegram_ext.ContextTypes = SimpleNamespace(DEFAULT_TYPE=object)
    telegram_ext.filters = SimpleNamespace(
        TEXT=object(),
        COMMAND=object(),
    )

    telegram_constants = types.ModuleType("telegram.constants")
    telegram_constants.ChatAction = SimpleNamespace(UPLOAD_VIDEO="upload_video")

    yt_dlp = types.ModuleType("yt_dlp")
    yt_dlp.YoutubeDL = object
    yt_dlp.utils = SimpleNamespace(DownloadError=Exception)

    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *args, **kwargs: None

    sys.modules.update(
        {
            "dotenv": dotenv,
            "telegram": telegram,
            "telegram.ext": telegram_ext,
            "telegram.constants": telegram_constants,
            "yt_dlp": yt_dlp,
        }
    )

    sys.modules.pop("bot", None)
    return importlib.import_module("bot")


class CaptionTests(unittest.TestCase):
    def setUp(self):
        self.bot = load_bot_module()

    def test_caption_includes_full_name_sender_and_platform(self):
        message = SimpleNamespace(
            from_user=SimpleNamespace(full_name="Olena Petrenko", username="olena")
        )

        caption = self.bot._download_caption(
            "https://www.tiktok.com/@someone/video/123",
            message,
        )

        self.assertIn("Olena Petrenko", caption)
        self.assertIn("TikTok", caption)

    def test_sender_name_falls_back_to_username(self):
        message = SimpleNamespace(
            from_user=SimpleNamespace(full_name="", username="quiet_user")
        )

        self.assertEqual(self.bot._sender_name(message), "@quiet_user")

    def test_instagram_shortcode(self):
        url = "https://www.instagram.com/reel/DYnF5FOIp5s/?igsh=foo"
        self.assertEqual(self.bot._instagram_shortcode(url), "DYnF5FOIp5s")

    def test_extract_json_array(self):
        html = '{"video_versions":[{"width":720,"url":"https://example.com/a.mp4"},{"width":480,"url":"https://example.com/b.mp4"}]}'
        versions = self.bot._extract_json_array(html, "video_versions")
        self.assertEqual(len(versions), 2)
        self.assertEqual(versions[0]["width"], 720)

    def test_instagram_error_without_curl_cffi(self):
        text = self.bot._download_error_text(
            "https://www.instagram.com/reel/abc",
            "Для Instagram потрібен пакет curl-cffi",
        )
        self.assertIn("curl-cffi", text)


if __name__ == "__main__":
    unittest.main()
