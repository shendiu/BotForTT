import os
import re
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
    r"https?://(?:www\.)?instagram\.com/(?:reel|reels)/[^\s/\?]+"
)
YOUTUBE_SHORTS_RE = re.compile(
    r"https?://(?:www\.)?youtube\.com/shorts/[^\s\?]+"
)

PATTERNS = [TIKTOK_RE, INSTAGRAM_REELS_RE, YOUTUBE_SHORTS_RE]

MAX_SIZE_BYTES = 49 * 1024 * 1024  # 49 MB (Telegram limit is 50 MB)


def find_video_url(text: str) -> str | None:
    for pattern in PATTERNS:
        m = pattern.search(text)
        if m:
            url = m.group().rstrip(")")  # strip trailing ) from markdown links
            return url
    return None


def download_video(url: str, output_dir: str) -> Path:
    ydl_opts = {
        "outtmpl": os.path.join(output_dir, "%(id)s.%(ext)s"),
        "format": "best[ext=mp4][filesize<49M]/best[filesize<49M]/best",
        "quiet": True,
        "no_warnings": True,
        "cookiefile": os.environ.get("COOKIES_FILE"),  # optional: path to cookies.txt
    }
    # Remove cookiefile key if not set
    if not ydl_opts["cookiefile"]:
        del ydl_opts["cookiefile"]

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
                    caption=(
                        f"Відео отримано з {_platform_name(url)}. "
                        "Для перегляду оновіть Ваші військово-облікові документи!"
                    ),
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
