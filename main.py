import os
import asyncio
import logging
import tempfile
from pathlib import Path

from telegram import Update
from telegram.ext import (
    Application,
    MessageHandler,
    ContextTypes,
    filters,
)

from nsfw_detector import detector


BOT_TOKEN = os.getenv("BOT_TOKEN")

NSFW_THRESHOLD = float(
    os.getenv("NSFW_THRESHOLD", "0.75")
)

WARN_USER = os.getenv(
    "WARN_USER", "true"
).lower() == "true"

WARNING_SECONDS = int(
    os.getenv("WARNING_SECONDS", "5")
)

IGNORE_ADMINS = os.getenv(
    "IGNORE_ADMINS", "true"
).lower() == "true"


logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


async def is_admin(update: Update) -> bool:
    if not IGNORE_ADMINS:
        return False

    if not update.effective_chat:
        return False

    if not update.effective_user:
        return False

    try:
        member = await update.effective_chat.get_member(
            update.effective_user.id
        )

        return member.status in (
            "administrator",
            "creator"
        )

    except Exception as e:
        logger.warning(
            "Không kiểm tra được admin: %s",
            e
        )
        return False


async def delete_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    reason: str = "NSFW"
):

    message = update.effective_message

    if not message:
        return

    try:
        await message.delete()

        logger.info(
            "Đã xoá | user=%s | reason=%s",
            update.effective_user.id
            if update.effective_user else "?",
            reason
        )

    except Exception as e:
        logger.error(
            "Không xoá được tin nhắn: %s",
            e
        )
        return

    if not WARN_USER:
        return

    try:
        user = update.effective_user

        name = (
            user.first_name
            if user
            else "Người dùng"
        )

        warning = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=(
                f"⚠️ {name}, tin nhắn của bạn "
                "đã bị xoá vì chứa nội dung "
                "nhạy cảm/18+."
            )
        )

        await asyncio.sleep(
            WARNING_SECONDS
        )

        try:
            await warning.delete()
        except Exception:
            pass

    except Exception as e:
        logger.warning(
            "Không gửi được cảnh báo: %s",
            e
        )


async def process_photo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if await is_admin(update):
        return

    message = update.effective_message

    if not message or not message.photo:
        return

    photo = message.photo[-1]

    temp_path = None

    try:

        telegram_file = await context.bot.get_file(
            photo.file_id
        )

        with tempfile.NamedTemporaryFile(
            suffix=".jpg",
            delete=False
        ) as temp:

            temp_path = temp.name

        await telegram_file.download_to_drive(
            temp_path
        )

        result = await asyncio.to_thread(
            detector.check_image,
            temp_path,
            NSFW_THRESHOLD
        )

        logger.info(
            "Ảnh: %s",
            result
        )

        if result["nsfw"]:

            await delete_message(
                update,
                context,
                f"image:{result['label']}:"
                f"{result['score']:.3f}"
            )

    except Exception as e:

        logger.exception(
            "Lỗi xử lý ảnh: %s",
            e
        )

    finally:

        if temp_path:
            try:
                Path(temp_path).unlink(
                    missing_ok=True
                )
            except Exception:
                pass


async def process_video(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if await is_admin(update):
        return

    message = update.effective_message

    if not message or not message.video:
        return

    video = message.video

    # Telegram Bot API giới hạn tải file
    # thông qua getFile ở 20 MB.

    if (
        video.file_size
        and video.file_size > 20 * 1024 * 1024
    ):
        logger.info(
            "Video > 20MB, bỏ qua."
        )
        return

    temp_path = None

    try:

        telegram_file = await context.bot.get_file(
            video.file_id
        )

        with tempfile.NamedTemporaryFile(
            suffix=".mp4",
            delete=False
        ) as temp:

            temp_path = temp.name

        await telegram_file.download_to_drive(
            temp_path
        )

        result = await asyncio.to_thread(
            detector.check_video,
            temp_path,
            NSFW_THRESHOLD
        )

        logger.info(
            "Video: %s",
            result
        )

        if result["nsfw"]:

            await delete_message(
                update,
                context,
                f"video:{result['label']}:"
                f"{result['score']:.3f}"
            )

    except Exception as e:

        logger.exception(
            "Lỗi xử lý video: %s",
            e
        )

    finally:

        if temp_path:
            try:
                Path(temp_path).unlink(
                    missing_ok=True
                )
            except Exception:
                pass


async def process_document(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if await is_admin(update):
        return

    message = update.effective_message

    if not message or not message.document:
        return

    document = message.document

    mime = document.mime_type or ""

    if not mime.startswith("video/"):
        return

    if (
        document.file_size
        and document.file_size > 20 * 1024 * 1024
    ):
        return

    temp_path = None

    try:

        telegram_file = await context.bot.get_file(
            document.file_id
        )

        with tempfile.NamedTemporaryFile(
            suffix=".mp4",
            delete=False
        ) as temp:

            temp_path = temp.name

        await telegram_file.download_to_drive(
            temp_path
        )

        result = await asyncio.to_thread(
            detector.check_video,
            temp_path,
            NSFW_THRESHOLD
        )

        if result["nsfw"]:

            await delete_message(
                update,
                context,
                f"video-file:{result['label']}:"
                f"{result['score']:.3f}"
            )

    except Exception as e:

        logger.exception(
            "Lỗi xử lý video file: %s",
            e
        )

    finally:

        if temp_path:
            try:
                Path(temp_path).unlink(
                    missing_ok=True
                )
            except Exception:
                pass


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(
        "🤖 Bot lọc NSFW đang hoạt động!\n\n"
        "🖼 Ảnh: kiểm tra tự động\n"
        "🎥 Video: kiểm tra tự động\n"
        "🗑 NSFW: tự động xoá"
    )


async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE
):

    logger.exception(
        "Bot error:",
        exc_info=context.error
    )


def main():

    if not BOT_TOKEN:
        raise RuntimeError(
            "Thiếu BOT_TOKEN trên Railway!"
        )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(
        MessageHandler(
            filters.COMMAND
            & filters.Regex(r"^/start"),
            start
        )
    )

    application.add_handler(
        MessageHandler(
            filters.PHOTO,
            process_photo
        )
    )

    application.add_handler(
        MessageHandler(
            filters.VIDEO,
            process_video
        )
    )

    application.add_handler(
        MessageHandler(
            filters.Document.ALL,
            process_document
        )
    )

    application.add_error_handler(
        error_handler
    )

    logger.info(
        "🤖 NSFW Telegram Bot started!"
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
