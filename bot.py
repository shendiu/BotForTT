import os
import re
import json
import asyncio
import logging
import tempfile
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

import yt_dlp
from telegram import Update, Message
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from telegram.constants import ChatAction

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TIKTOK_RE = re.compile(
    r"https?://(?:www\.|vm\.|vt\.)?tiktok\.com/\S+"
)
INSTAGRAM_REELS_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/(?:reel|reels)/([^\s/\?]+)"
)
YOUTUBE_SHORTS_RE = re.compile(
    r"https?://(?:www\.)?youtube\.com/shorts/[^\s\?]+"
)

PATTERNS = [TIKTOK_RE, INSTAGRAM_REELS_RE, YOUTUBE_SHORTS_RE]

MAX_SIZE_BYTES = 49 * 1024 * 1024  # 49 MB (Telegram limit is 50 MB)

COOKIES_PATH = (
    os.environ.get("COOKIES_FILE")
    or (Path(__file__).parent / "cookies.txt")
)


def _cookies_file() -> str | None:
    path = Path(COOKIES_PATH).expanduser()
    return str(path) if path.is_file() else None


def _is_instagram(url: str) -> bool:
    return "instagram.com" in url.lower()


def _instagram_shortcode(url: str) -> str | None:
    match = INSTAGRAM_REELS_RE.search(url)
    return match.group(1) if match else None


def _extract_json_array(html: str, key: str) -> list | None:
    marker = f'"{key}":'
    start = html.find(marker)
    if start == -1:
        return None

    bracket_start = html.find("[", start)
    if bracket_start == -1:
        return None

    depth = 0
    for index in range(bracket_start, len(html)):
        char = html[index]
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return json.loads(html[bracket_start:index + 1])
    return None


def download_instagram_video(url: str, output_dir: str) -> Path:
    try:
        from curl_cffi import requests
    except ImportError as exc:
        raise yt_dlp.utils.DownloadError(
            "Для Instagram потрібен пакет curl-cffi: pip install 'curl-cffi>=0.10,<0.14'"
        ) from exc

    shortcode = _instagram_shortcode(url)
    if not shortcode:
        raise yt_dlp.utils.DownloadError("Невідомий формат Instagram URL")

    page_url = f"https://www.instagram.com/reel/{shortcode}/"
    response = requests.get(
        page_url,
        impersonate="chrome",
        timeout=30,
        headers={"Accept-Language": "en-US,en;q=0.9"},
    )
    response.raise_for_status()

    versions = _extract_json_array(response.text, "video_versions")
    if not versions:
        raise yt_dlp.utils.DownloadError(
            "Instagram не повернув відео. Можливо, це фото або приватний пост."
        )

    best = max(versions, key=lambda item: item.get("width", 0))
    video_url = best["url"].replace("\\/", "/")

    video_response = requests.get(
        video_url,
        impersonate="chrome",
        timeout=120,
        headers={"Referer": "https://www.instagram.com/"},
    )
    video_response.raise_for_status()

    output_path = Path(output_dir) / f"{shortcode}.mp4"
    output_path.write_bytes(video_response.content)
    return output_path


def find_video_url(text: str) -> str | None:
    for pattern in PATTERNS:
        m = pattern.search(text)
        if m:
            url = m.group().rstrip(")")  # strip trailing ) from markdown links
            return url
    return None


def download_video(url: str, output_dir: str) -> Path:
    if _is_instagram(url):
        return download_instagram_video(url, output_dir)

    ydl_opts = {
        "outtmpl": os.path.join(output_dir, "%(id)s.%(ext)s"),
        "format": "best[ext=mp4][filesize<49M]/best[filesize<49M]/best",
        "quiet": True,
        "no_warnings": True,
    }
    cookies = _cookies_file()
    if cookies:
        ydl_opts["cookiefile"] = cookies

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        # yt-dlp might change extension after merge
        path = Path(filename)
        if not path.exists():
            path = path.with_suffix(".mp4")
        return path


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message: Message = update.effective_message
    if not message or not message.text:
        return

    url = find_video_url(message.text)
    if not url:
        return

    logger.info("Detected video URL: %s from chat %s", url, message.chat_id)

    await context.bot.send_chat_action(message.chat_id, ChatAction.UPLOAD_VIDEO)

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            video_path = await asyncio.get_event_loop().run_in_executor(
                None, download_video, url, tmpdir
            )
        except yt_dlp.utils.DownloadError as e:
            logger.warning("Download failed for %s: %s", url, e)
            await _reply_download_error(message, url, str(e))
            return

        if not video_path.exists():
            logger.warning("Downloaded file not found: %s", video_path)
            return

        size = video_path.stat().st_size
        if size > MAX_SIZE_BYTES:
            logger.warning("File too large (%d bytes), skipping: %s", size, url)
            return

        try:
            with open(video_path, "rb") as f:
                await context.bot.send_video(
                    chat_id=message.chat_id,
                    message_thread_id=getattr(message, "message_thread_id", None),
                    video=f,
                    caption=_download_caption(url, message),
                    supports_streaming=True,
                    read_timeout=120,
                    write_timeout=120,
                )

            try:
                await message.delete()
                logger.info("Deleted original message with URL: %s", message.message_id)
            except Exception as e:
                logger.warning(
                    "Failed to delete original message %s. "
                    "Make sure the bot is an admin with Delete messages permission. Error: %s",
                    message.message_id,
                    e,
                )

        except Exception as e:
            logger.error("Failed to send video: %s", e)


def _platform_name(url: str) -> str:
    if "tiktok" in url:
        return "TikTok"
    if "instagram" in url:
        return "Instagram"
    if "youtube" in url or "youtu.be" in url:
        return "YouTube Shorts"
    return "video"


def _sender_name(message: Message) -> str:
    user = getattr(message, "from_user", None)
    if user:
        full_name = getattr(user, "full_name", None)
        if full_name and full_name.strip():
            return full_name.strip()

        username = getattr(user, "username", None)
        if username and username.strip():
            return f"@{username.strip().lstrip('@')}"

    sender_chat = getattr(message, "sender_chat", None)
    title = getattr(sender_chat, "title", None)
    if title and title.strip():
        return title.strip()

    return "невідомого відправника"


def _download_error_text(url: str, error: str) -> str:
    if _is_instagram(url):
        if "curl-cffi" in error.lower():
            return (
                "Не вдалося завантажити Instagram Reel: на сервері не встановлено curl-cffi. "
                "Виконайте: pip install 'curl-cffi>=0.10,<0.14'"
            )
        return f"Не вдалося завантажити Instagram Reel: {error}"
    return "Не вдалося завантажити відео. Спробуйте пізніше."


async def _reply_download_error(message: Message, url: str, error: str) -> None:
    try:
        await message.reply_text(
            _download_error_text(url, error),
            message_thread_id=getattr(message, "message_thread_id", None),
        )
    except Exception as e:
        logger.warning("Failed to send download error reply: %s", e)


def _download_caption(url: str, message: Message) -> str:
    return (
        f"Відео отримано від {_sender_name(message)} з {_platform_name(url)}. "
        "Для перегляду оновіть Ваші військово-облікові документи!"
    )


def main() -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = Application.builder().token(token).build()

    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    logger.info("Bot started, polling...")
    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()
