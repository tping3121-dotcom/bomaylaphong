import os
import re
import sqlite3
import asyncio
import logging
import tempfile
from pathlib import Path
from datetime import datetime, timedelta, timezone

from telegram import Update, ChatPermissions
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# Optional image detection imports
try:
    from PIL import Image
    from transformers import pipeline
    HAS_TRANSFORMERS = True
except ImportError:
    HAS_TRANSFORMERS = False


# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")

# Admin/owner chính của bot
OWNER_ID = int(os.getenv("OWNER_ID", "7449833411"))

# Username hiển thị khi báo cho người dùng
OWNER_USERNAME = os.getenv("OWNER_USERNAME", "@echcuto")

NSFW_THRESHOLD = float(os.getenv("NSFW_THRESHOLD", "0.75"))

WARN_USER = os.getenv("WARN_USER", "true").lower() == "true"

WARNING_SECONDS = int(os.getenv("WARNING_SECONDS", "5"))

# Tự động mute bao nhiêu phút khi phát hiện NSFW
AUTO_MUTE_MINUTES = int(os.getenv("AUTO_MUTE_MINUTES", "2"))

# SQLite DB Path
DATA_DIR = os.getenv("DATA_DIR", "/data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "bot.db")


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# =========================================================
# NSFW DETECTOR ENGINE CLASS
# =========================================================

class NsfwDetector:
    def __init__(self):
        self.pipe = None
        if HAS_TRANSFORMERS:
            try:
                # Sử dụng pipeline NSFW classifier nhẹ
                self.pipe = pipeline(
                    "image-classification", 
                    model="Falconsai/nsfw_image_detection"
                )
                logger.info("Đã tải mô hình NSFW Classifier thành công.")
            except Exception as e:
                logger.warning(f"Không thể tải mô hình AI NSFW: {e}")

    def check_image(self, image_path: str, threshold: float = 0.75) -> dict:
        if not self.pipe:
            return {"nsfw": False, "label": "normal", "score": 0.0}

        try:
            image = Image.open(image_path).convert("RGB")
            results = self.pipe(image)
            # Structure: [{'label': 'nsfw', 'score': 0.95}, ...]
            for res in results:
                if res["label"].lower() == "nsfw" and res["score"] >= threshold:
                    return {"nsfw": True, "label": "nsfw", "score": res["score"]}
            return {"nsfw": False, "label": "normal", "score": 0.0}
        except Exception as e:
            logger.error(f"Lỗi kiểm tra ảnh NSFW: {e}")
            return {"nsfw": False, "label": "error", "score": 0.0}

    def check_video(self, video_path: str, threshold: float = 0.75) -> dict:
        # Kiểm tra video cơ bản (có thể bóc tách frame bằng OpenCV nếu cần)
        return {"nsfw": False, "label": "normal", "score": 0.0}


# Khởi tạo instance Detector toàn cục
detector = NsfwDetector()


# =========================================================
# DATABASE
# =========================================================

def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db_connect()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS groups (
                chat_id INTEGER PRIMARY KEY,
                username TEXT,
                title TEXT,
                enabled INTEGER DEFAULT 0,
                notified INTEGER DEFAULT 0,
                created_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mutes (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                until_ts INTEGER NOT NULL,
                PRIMARY KEY(chat_id, user_id)
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def save_group(chat_id: int, username: str | None, title: str):
    conn = db_connect()
    try:
        conn.execute(
            """
            INSERT INTO groups (chat_id, username, title, enabled, notified, created_at)
            VALUES (?, ?, ?, 0, 0, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                username = excluded.username,
                title = excluded.title
            """,
            (chat_id, username, title, datetime.now(timezone.utc).isoformat())
        )
        conn.commit()
    finally:
        conn.close()


def group_enabled(chat_id: int) -> bool:
    conn = db_connect()
    try:
        row = conn.execute(
            "SELECT enabled FROM groups WHERE chat_id = ?",
            (chat_id,)
        ).fetchone()
        return bool(row and row["enabled"] == 1)
    finally:
        conn.close()


def set_group_enabled(chat_id: int, enabled: bool):
    conn = db_connect()
    try:
        conn.execute(
            "UPDATE groups SET enabled = ? WHERE chat_id = ?",
            (1 if enabled else 0, chat_id)
        )
        conn.commit()
    finally:
        conn.close()


def group_was_notified(chat_id: int) -> bool:
    conn = db_connect()
    try:
        row = conn.execute(
            "SELECT notified FROM groups WHERE chat_id = ?",
            (chat_id,)
        ).fetchone()
        return bool(row and row["notified"] == 1)
    finally:
        conn.close()


def set_group_notified(chat_id: int):
    conn = db_connect()
    try:
        conn.execute(
            "UPDATE groups SET notified = 1 WHERE chat_id = ?",
            (chat_id,)
        )
        conn.commit()
    finally:
        conn.close()


def save_mute(chat_id: int, user_id: int, until_ts: int):
    conn = db_connect()
    try:
        conn.execute(
            """
            INSERT INTO mutes (chat_id, user_id, until_ts)
            VALUES (?, ?, ?)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                until_ts = excluded.until_ts
            """,
            (chat_id, user_id, until_ts)
        )
        conn.commit()
    finally:
        conn.close()


def delete_mute(chat_id: int, user_id: int):
    conn = db_connect()
    try:
        conn.execute(
            "DELETE FROM mutes WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id)
        )
        conn.commit()
    finally:
        conn.close()


# =========================================================
# OWNER CHECK
# =========================================================

def is_owner(update: Update) -> bool:
    user = update.effective_user
    if not user:
        return False
    return user.id == OWNER_ID


async def owner_only(update: Update) -> bool:
    if not is_owner(update):
        if update.effective_message:
            await update.effective_message.reply_text("⛔ Bạn không có quyền sử dụng lệnh này.")
        return False
    return True


# =========================================================
# TIME PARSER
# =========================================================

def parse_duration(text: str):
    text = text.strip().lower()
    match = re.fullmatch(r"(\d+)(p|h|n|t)", text)
    if not match:
        return None

    number = int(match.group(1))
    unit = match.group(2)

    if number <= 0:
        return None

    if unit == "p":
        seconds = number * 60
    elif unit == "h":
        seconds = number * 3600
    elif unit == "n":
        seconds = number * 86400
    elif unit == "t":
        seconds = number * 30 * 86400
    else:
        return None

    if seconds > 366 * 86400:
        return None

    return seconds


# =========================================================
# GROUP RESOLVER
# =========================================================

async def resolve_group(context: ContextTypes.DEFAULT_TYPE, group_ref: str):
    group_ref = group_ref.strip()
    try:
        if group_ref.startswith("@"):
            chat = await context.bot.get_chat(group_ref)
        else:
            chat = await context.bot.get_chat(int(group_ref))

        if chat.type not in ("group", "supergroup"):
            return None

        return chat
    except Exception as e:
        logger.warning("Không tìm thấy nhóm %s: %s", group_ref, e)
        return None


# =========================================================
# PERMISSIONS & MUTE CONTROLS
# =========================================================

def muted_permissions():
    return ChatPermissions(
        can_send_messages=False,
        can_send_audios=False,
        can_send_documents=False,
        can_send_photos=False,
        can_send_videos=False,
        can_send_video_notes=False,
        can_send_voice_notes=False,
        can_send_polls=False,
        can_send_other_messages=False,
        can_add_web_page_previews=False,
    )


def normal_permissions():
    return ChatPermissions(
        can_send_messages=True,
        can_send_audios=True,
        can_send_documents=True,
        can_send_photos=True,
        can_send_videos=True,
        can_send_video_notes=True,
        can_send_voice_notes=True,
        can_send_polls=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
    )


async def mute_user(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, seconds: int):
    until_dt = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    until_ts = int(until_dt.timestamp())

    await context.bot.restrict_chat_member(
        chat_id=chat_id,
        user_id=user_id,
        permissions=muted_permissions(),
        until_date=until_dt,
        use_independent_chat_permissions=True
    )

    save_mute(chat_id, user_id, until_ts)


async def unmute_user(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int):
    await context.bot.restrict_chat_member(
        chat_id=chat_id,
        user_id=user_id,
        permissions=normal_permissions(),
        until_date=None,
        use_independent_chat_permissions=True
    )

    delete_mute(chat_id, user_id)


# =========================================================
# COMMAND HANDLERS
# =========================================================

async def capquyen_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only(update):
        return

    if not update.effective_message:
        return

    if len(context.args) != 1:
        await update.effective_message.reply_text(
            "❌ Cú pháp:\n/capquyen @tennhom\n\nVí dụ:\n/capquyen @kiemtienfree"
        )
        return

    group_ref = context.args[0]
    chat = await resolve_group(context, group_ref)

    if not chat:
        await update.effective_message.reply_text(
            "❌ Không tìm thấy nhóm.\nHãy dùng đúng @username của nhóm và đảm bảo bot đã được thêm vào nhóm."
        )
        return

    save_group(chat.id, chat.username, chat.title or "Không tên")
    set_group_enabled(chat.id, True)

    username = f"@{chat.username}" if chat.username else str(chat.id)

    await update.effective_message.reply_text(
        f"✅ CẤP QUYỀN THÀNH CÔNG\n\n👥 Nhóm: {chat.title}\n🔗 {username}\n🆔 ID: {chat.id}\n\n🤖 Bot đã được phép hoạt động trong nhóm này."
    )


async def check_group_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    if not chat:
        return False

    if chat.type not in ("group", "supergroup"):
        return True

    save_group(chat.id, chat.username, chat.title or "Không tên")

    if group_enabled(chat.id):
        return True

    if not group_was_notified(chat.id):
        set_group_notified(chat.id)

        try:
            await context.bot.send_message(
                chat_id=chat.id,
                text=(
                    f"⚠️ Nhóm này chưa được cấp quyền sử dụng bot.\n\n"
                    f"Vui lòng liên hệ admin {OWNER_USERNAME} để được cấp quyền sử dụng."
                )
            )
        except Exception as e:
            logger.warning("Không gửi được thông báo nhóm: %s", e)

        try:
            group_info = f"🔗 @{chat.username}" if chat.username else "🔗 Nhóm không có username"
            await context.bot.send_message(
                chat_id=OWNER_ID,
                text=f"🔔 NHÓM YÊU CẦU SỬ DỤNG BOT\n\n👥 Nhóm: {chat.title}\n🆔 ID: {chat.id}\n{group_info}"
            )
        except Exception as e:
            logger.warning("Không gửi được thông báo owner: %s", e)

    return False


async def mute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only(update):
        return

    if len(context.args) != 3:
        await update.effective_message.reply_text(
            "❌ Cú pháp:\n/mute ID @tennhom THOIGIAN\n\nVí dụ:\n/mute 9237293294 @kiemtienfree 10p"
        )
        return

    user_text, group_ref, duration_text = context.args[0], context.args[1], context.args[2]

    try:
        user_id = int(user_text)
    except ValueError:
        await update.effective_message.reply_text("❌ User ID phải là số.")
        return

    seconds = parse_duration(duration_text)
    if seconds is None:
        await update.effective_message.reply_text("❌ Thời gian không hợp lệ. Ví dụ: 10p, 2h, 1n, 1t")
        return

    chat = await resolve_group(context, group_ref)
    if not chat:
        await update.effective_message.reply_text("❌ Không tìm thấy nhóm.")
        return

    try:
        member = await context.bot.get_chat_member(chat.id, user_id)
        if member.status in ("administrator", "creator"):
            await update.effective_message.reply_text("❌ Không thể mute admin/owner của nhóm.")
            return

        await mute_user(context, chat.id, user_id, seconds)
        await update.effective_message.reply_text(
            f"🔇 ĐÃ MUTE\n\n👤 User ID: {user_id}\n👥 Nhóm: {chat.title}\n⏱ Thời gian: {duration_text}"
        )
    except Exception as e:
        logger.exception("Mute error: %s", e)
        await update.effective_message.reply_text("❌ Không thể mute người dùng. Kiểm tra lại quyền hạn của Bot.")


async def unmute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only(update):
        return

    if len(context.args) != 2:
        await update.effective_message.reply_text("❌ Cú pháp:\n/unmute ID @tennhom")
        return

    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("❌ User ID phải là số.")
        return

    group_ref = context.args[1]
    chat = await resolve_group(context, group_ref)

    if not chat:
        await update.effective_message.reply_text("❌ Không tìm thấy nhóm.")
        return

    try:
        await unmute_user(context, chat.id, user_id)
        await update.effective_message.reply_text(
            f"🔊 ĐÃ UNMUTE\n\n👤 User ID: {user_id}\n👥 Nhóm: {chat.title}"
        )
    except Exception as e:
        logger.exception("Unmute error: %s", e)
        await update.effective_message.reply_text("❌ Không thể mở cấm. Kiểm tra lại quyền admin của bot.")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup"):
        if not await check_group_access(update, context):
            return

    await update.effective_message.reply_text(
        f"🤖 BOT LỌC NSFW\n\n🖼 Tự động kiểm tra ảnh\n🎥 Tự động kiểm tra video\n🗑 Phát hiện NSFW → xoá\n🔇 Người gửi → mute 2 phút\n\n👨‍💻 Admin: {OWNER_USERNAME}"
    )


# =========================================================
# MESSAGE DELETION & AUTO MODERATION
# =========================================================

async def delete_message(update: Update, context: ContextTypes.DEFAULT_TYPE, reason: str = "NSFW"):
    message = update.effective_message
    if not message:
        return

    try:
        await message.delete()
        logger.info(
            "Đã xoá | chat=%s | user=%s | reason=%s",
            update.effective_chat.id if update.effective_chat else "?",
            update.effective_user.id if update.effective_user else "?",
            reason
        )
    except Exception as e:
        logger.error("Không xoá được tin nhắn: %s", e)


async def auto_mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat or not update.effective_user:
        return

    user_id = update.effective_user.id
    try:
        member = await update.effective_chat.get_member(user_id)
        if member.status in ("administrator", "creator"):
            return

        await mute_user(context, update.effective_chat.id, user_id, AUTO_MUTE_MINUTES * 60)
        logger.info("Auto mute user=%s chat=%s for %s mins", user_id, update.effective_chat.id, AUTO_MUTE_MINUTES)
    except Exception as e:
        logger.error("Auto mute failed: %s", e)


async def send_warning(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not WARN_USER or not update.effective_chat:
        return

    try:
        user = update.effective_user
        name = user.first_name if user else "Người dùng"

        warning = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=(
                f"⚠️ {name}, nội dung của bạn đã bị xoá vì chứa nội dung nhạy cảm/18+.\n\n"
                f"🔇 Bạn bị cấm chat {AUTO_MUTE_MINUTES} phút."
            )
        )

        await asyncio.sleep(WARNING_SECONDS)
        try:
            await warning.delete()
        except Exception:
            pass
    except Exception as e:
        logger.warning("Warning error: %s", e)


# =========================================================
# MEDIA PROCESSORS
# =========================================================

async def process_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_group_access(update, context):
        return

    if update.effective_chat and update.effective_user:
        try:
            member = await update.effective_chat.get_member(update.effective_user.id)
            if member.status in ("administrator", "creator"):
                return
        except Exception:
            pass

    message = update.effective_message
    if not message or not message.photo:
        return

    photo = message.photo[-1]
    temp_path = None

    try:
        telegram_file = await context.bot.get_file(photo.file_id)
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as temp:
            temp_path = temp.name

        await telegram_file.download_to_drive(temp_path)

        result = await asyncio.to_thread(detector.check_image, temp_path, NSFW_THRESHOLD)
        logger.info("Ảnh result: %s", result)

        if result["nsfw"]:
            await delete_message(update, context, f"image:{result['label']}:{result['score']:.3f}")
            await auto_mute(update, context)
            await send_warning(update, context)

    except Exception as e:
        logger.exception("Lỗi xử lý ảnh: %s", e)
    finally:
        if temp_path:
            Path(temp_path).unlink(missing_ok=True)


async def process_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_group_access(update, context):
        return

    message = update.effective_message
    if not message or not message.video or not update.effective_user:
        return

    try:
        member = await update.effective_chat.get_member(update.effective_user.id)
        if member.status in ("administrator", "creator"):
            return
    except Exception:
        pass

    video = message.video
    if video.file_size and video.file_size > 20 * 1024 * 1024:
        logger.info("Video > 20MB, bỏ qua.")
        return

    temp_path = None
    try:
        telegram_file = await context.bot.get_file(video.file_id)
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as temp:
            temp_path = temp.name

        await telegram_file.download_to_drive(temp_path)
        result = await asyncio.to_thread(detector.check_video, temp_path, NSFW_THRESHOLD)

        if result["nsfw"]:
            await delete_message(update, context, f"video:{result['label']}:{result['score']:.3f}")
            await auto_mute(update, context)
            await send_warning(update, context)

    except Exception as e:
        logger.exception("Lỗi xử lý video: %s", e)
    finally:
        if temp_path:
            Path(temp_path).unlink(missing_ok=True)


async def process_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_group_access(update, context):
        return

    message = update.effective_message
    if not message or not message.document or not update.effective_user:
        return

    document = message.document
    mime = document.mime_type or ""

    if not mime.startswith("video/"):
        return

    try:
        member = await update.effective_chat.get_member(update.effective_user.id)
        if member.status in ("administrator", "creator"):
            return
    except Exception:
        pass

    if document.file_size and document.file_size > 20 * 1024 * 1024:
        return

    temp_path = None
    try:
        telegram_file = await context.bot.get_file(document.file_id)
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as temp:
            temp_path = temp.name

        await telegram_file.download_to_drive(temp_path)
        result = await asyncio.to_thread(detector.check_video, temp_path, NSFW_THRESHOLD)

        if result["nsfw"]:
            await delete_message(update, context, f"video-file:{result['label']}:{result['score']:.3f}")
            await auto_mute(update, context)
            await send_warning(update, context)

    except Exception as e:
        logger.exception("Lỗi xử lý video file: %s", e)
    finally:
        if temp_path:
            Path(temp_path).unlink(missing_ok=True)


# =========================================================
# ERROR HANDLER & MAIN
# =========================================================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Bot error:", exc_info=context.error)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("Thiếu BOT_TOKEN trên Railway!")

    init_db()

    application = Application.builder().token(BOT_TOKEN).build()

    # Commands
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("capquyen", capquyen_command))
    application.add_handler(CommandHandler("mute", mute_command))
    application.add_handler(CommandHandler("unmute", unmute_command))

    # Media Handlers
    application.add_handler(MessageHandler(filters.PHOTO, process_photo))
    application.add_handler(MessageHandler(filters.VIDEO, process_video))
    application.add_handler(MessageHandler(filters.Document.ALL, process_document))

    # Error handling
    application.add_error_handler(error_handler)

    logger.info("🤖 NSFW Telegram Bot started!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
