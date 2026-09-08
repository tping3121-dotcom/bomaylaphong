import os
import re
import sqlite3
import asyncio
import logging
import tempfile
from pathlib import Path
from datetime import datetime, timedelta, timezone

from telegram import Update, ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# =========================================================
# OPTIONAL AI IMPORTS
# =========================================================

try:
    from PIL import Image, ImageOps
    from transformers import pipeline

    HAS_TRANSFORMERS = True
except ImportError:
    HAS_TRANSFORMERS = False


# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")

OWNER_ID = int(os.getenv("OWNER_ID", "7449833411"))

OWNER_USERNAME = os.getenv(
    "OWNER_USERNAME",
    "@echcuto"
)

NSFW_THRESHOLD = float(
    os.getenv("NSFW_THRESHOLD", "0.75")
)

WARN_USER = (
    os.getenv("WARN_USER", "true").lower() == "true"
)

AUTO_MUTE_MINUTES = int(
    os.getenv("AUTO_MUTE_MINUTES", "2")
)

DATA_DIR = os.getenv(
    "DATA_DIR",
    "/data"
)

os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(
    DATA_DIR,
    "bot.db"
)


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# =========================================================
# NSFW DETECTOR
# =========================================================

class NsfwDetector:

    def __init__(self):

        self.pipe = None
        self.lock = asyncio.Lock()

        if not HAS_TRANSFORMERS:
            logger.warning(
                "Thiếu PIL hoặc transformers. "
                "Bot sẽ không thể quét NSFW."
            )
            return

        try:
            logger.info("Đang tải model NSFW...")
            self.pipe = pipeline(
                "image-classification",
                model="Falconsai/nsfw_image_detection",
                device=-1
            )
            logger.info("Đã tải model NSFW thành công.")
        except Exception as e:
            logger.exception("Không thể tải model NSFW: %s", e)
            self.pipe = None


    async def check_image(
        self,
        image_path: str,
        threshold: float = 0.75
    ) -> dict:

        if not self.pipe:
            return {"nsfw": False, "label": "normal", "score": 0.0}

        async with self.lock:
            try:
                with Image.open(image_path) as img:
                    image = ImageOps.exif_transpose(img)
                    image = image.convert("RGB")
                    image.thumbnail((768, 768), Image.Resampling.LANCZOS)
                    results = self.pipe(image, top_k=2)

                for result in results:
                    label = str(result["label"]).lower()
                    score = float(result["score"])

                    if label == "nsfw" and score >= threshold:
                        return {"nsfw": True, "label": "nsfw", "score": score}

                return {"nsfw": False, "label": "normal", "score": 0.0}

            except Exception as e:
                logger.exception("Lỗi kiểm tra ảnh NSFW: %s", e)
                return {"nsfw": False, "label": "error", "score": 0.0}


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


def get_all_groups() -> list[int]:
    conn = db_connect()
    try:
        rows = conn.execute("SELECT chat_id FROM groups").fetchall()
        return [row["chat_id"] for row in rows]
    finally:
        conn.close()


def group_enabled(chat_id: int) -> bool:
    conn = db_connect()
    try:
        row = conn.execute(
            "SELECT enabled FROM groups WHERE chat_id = ?", (chat_id,)
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
            "SELECT notified FROM groups WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        return bool(row and row["notified"] == 1)
    finally:
        conn.close()


def set_group_notified(chat_id: int):
    conn = db_connect()
    try:
        conn.execute("UPDATE groups SET notified = 1 WHERE chat_id = ?", (chat_id,))
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
            ON CONFLICT(chat_id, user_id) DO UPDATE SET until_ts = excluded.until_ts
            """,
            (chat_id, user_id, until_ts)
        )
        conn.commit()
    finally:
        conn.close()


def delete_mute(chat_id: int, user_id: int):
    conn = db_connect()
    try:
        conn.execute("DELETE FROM mutes WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
        conn.commit()
    finally:
        conn.close()


# =========================================================
# OWNER & ACCESS
# =========================================================

def is_owner(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id == OWNER_ID)


async def owner_only(update: Update) -> bool:
    if not is_owner(update):
        if update.effective_message:
            await update.effective_message.reply_text("⛔ Bạn không có quyền sử dụng lệnh này.")
        return False
    return True


# =========================================================
# HELPER PARSERS
# =========================================================

def parse_duration(text: str):
    text = text.strip().lower()
    match = re.fullmatch(r"(\d+)(p|h|n|t)", text)
    if not match:
        return None

    number, unit = int(match.group(1)), match.group(2)
    if number <= 0:
        return None

    multipliers = {"p": 60, "h": 3600, "n": 86400, "t": 30 * 86400}
    seconds = number * multipliers.get(unit, 0)
    return seconds if 0 < seconds <= 366 * 86400 else None


async def resolve_group(context: ContextTypes.DEFAULT_TYPE, group_ref: str):
    group_ref = group_ref.strip()
    try:
        chat = await context.bot.get_chat(group_ref if group_ref.startswith("@") else int(group_ref))
        return chat if chat.type in ("group", "supergroup") else None
    except Exception as e:
        logger.warning("Không tìm thấy nhóm %s: %s", group_ref, e)
        return None


# =========================================================
# PERMISSIONS & MUTE
# =========================================================

def muted_permissions():
    return ChatPermissions(
        can_send_messages=False, can_send_audios=False, can_send_documents=False,
        can_send_photos=False, can_send_videos=False, can_send_video_notes=False,
        can_send_voice_notes=False, can_send_polls=False, can_send_other_messages=False,
        can_add_web_page_previews=False,
    )


def normal_permissions():
    return ChatPermissions(
        can_send_messages=True, can_send_audios=True, can_send_documents=True,
        can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
        can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
        can_add_web_page_previews=True,
    )


async def mute_user(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, seconds: int):
    until_dt = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    await context.bot.restrict_chat_member(
        chat_id=chat_id, user_id=user_id, permissions=muted_permissions(),
        until_date=until_dt, use_independent_chat_permissions=True
    )
    save_mute(chat_id, user_id, int(until_dt.timestamp()))


async def unmute_user(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int):
    await context.bot.restrict_chat_member(
        chat_id=chat_id, user_id=user_id, permissions=normal_permissions(),
        until_date=None, use_independent_chat_permissions=True
    )
    delete_mute(chat_id, user_id)


# =========================================================
# GROUP ACCESS & SPAM CONTROL
# =========================================================

async def check_group_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup"):
        return True

    save_group(chat.id, chat.username, chat.title or "Không tên")

    if group_enabled(chat.id):
        return True

    if not group_was_notified(chat.id):
        set_group_notified(chat.id)

        # 1. Spam 5 tin nhắn cảnh báo lên nhóm
        spam_text = f"⚠️ Nhóm chưa được cấp quyền sử dụng vui lòng liên hệ admin để được cấp quyền sử dụng admin {OWNER_USERNAME}"
        for _ in range(5):
            try:
                await context.bot.send_message(chat_id=chat.id, text=spam_text)
                await asyncio.sleep(0.4)
            except Exception as e:
                logger.warning("Không gửi được spam tin nhắn: %s", e)

        # 2. Gửi yêu cầu kèm NÚT BẤM cấp quyền tới Admin
        try:
            group_info = f"🔗 @{chat.username}" if chat.username else "🔗 Nhóm không có username"
            
            keyboard = [
                [
                    InlineKeyboardButton("✅ Cấp quyền", callback_data=f"approve_{chat.id}"),
                    InlineKeyboardButton("❌ Từ chối", callback_data=f"decline_{chat.id}"),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await context.bot.send_message(
                chat_id=OWNER_ID,
                text=(
                    "🔔 **YÊU CẦU CẤP QUYỀN SỬ DỤNG BOT**\n\n"
                    f"👥 Nhóm: {chat.title}\n"
                    f"🆔 ID: `{chat.id}`\n"
                    f"{group_info}\n\n"
                    "Vui lòng chọn thao tác bên dưới:"
                ),
                reply_markup=reply_markup,
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.warning("Không gửi được thông báo tới owner: %s", e)

    return False


# =========================================================
# CALLBACK HANDLER (XỬ LÝ NÚT BẤM DUYỆT / TỪ CHỐI)
# =========================================================

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != OWNER_ID:
        await query.answer("⛔ Bạn không có quyền thực hiện thao tác này.", show_alert=True)
        return

    data = query.data
    if not (data.startswith("approve_") or data.startswith("decline_")):
        return

    action, chat_id_str = data.split("_", 1)
    chat_id = int(chat_id_str)

    if action == "approve":
        set_group_enabled(chat_id, True)
        
        # Thông báo lên nhóm
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text="✅ Nhóm đã được cấp quyền sử dụng bot!"
            )
        except Exception as e:
            logger.warning("Lỗi gửi thông báo cấp quyền lên nhóm: %s", e)

        # Cập nhật tin nhắn ở phía Admin
        await query.edit_message_text(
            text=f"{query.message.text}\n\n👉 **KẾT QUẢ:** ✅ ĐÃ CẤP QUYỀN",
            parse_mode="Markdown"
        )

    elif action == "decline":
        set_group_enabled(chat_id, False)

        # Thông báo từ chối lên nhóm
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"❌ Admin đã từ chối cấp quyền sử dụng vui lòng liên hệ admin {OWNER_USERNAME} để được cấp quyền sử dụng."
            )
        except Exception as e:
            logger.warning("Lỗi gửi thông báo từ chối lên nhóm: %s", e)

        # Cập nhật tin nhắn ở phía Admin
        await query.edit_message_text(
            text=f"{query.message.text}\n\n👉 **KẾT QUẢ:** ❌ ĐÃ TỪ CHỐI",
            parse_mode="Markdown"
        )


# =========================================================
# COMMANDS
# =========================================================

async def capquyen_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only(update):
        return

    if len(context.args) != 1:
        await update.effective_message.reply_text("❌ Cú pháp:\n/capquyen @tennhom HOẶC ID_nhóm")
        return

    chat = await resolve_group(context, context.args[0])
    if not chat:
        await update.effective_message.reply_text("❌ Không tìm thấy nhóm.")
        return

    save_group(chat.id, chat.username, chat.title or "Không tên")
    set_group_enabled(chat.id, True)

    try:
        await context.bot.send_message(chat_id=chat.id, text="✅ Nhóm đã được cấp quyền sử dụng bot!")
    except Exception:
        pass

    await update.effective_message.reply_text(
        f"✅ Đã cấp quyền thành công cho nhóm: {chat.title} (`{chat.id}`)"
    )


async def tb_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lệnh /tb gửi thông báo toàn bộ nhóm"""
    if not await owner_only(update):
        return

    if not context.args:
        await update.effective_message.reply_text(
            "❌ Cú pháp:\n/tb <nội dung thông báo>\n\nVí dụ:\n/tb Thông báo bảo trì hệ thống"
        )
        return

    message_text = " ".join(context.args)
    groups = get_all_groups()

    if not groups:
        await update.effective_message.reply_text("⚠️ Chưa có dữ liệu nhóm nào trong cơ sở dữ liệu.")
        return

    status_msg = await update.effective_message.reply_text(f"⏳ Đang gửi thông báo tới {len(groups)} nhóm...")

    success = 0
    failed = 0

    for chat_id in groups:
        try:
            await context.bot.send_message(chat_id=chat_id, text=f"📢 **THÔNG BÁO TỪ ADMIN**\n\n{message_text}", parse_mode="Markdown")
            success += 1
            await asyncio.sleep(0.1)  # Tránh vượt quá giới hạn API Telegram
        except Exception as e:
            logger.warning("Không thể gửi tb tới nhóm %s: %s", chat_id, e)
            failed += 1

    await status_msg.edit_text(
        f"📢 **KẾT QUẢ GỬI THÔNG BÁO**\n\n"
        f"✅ Thành công: {success} nhóm\n"
        f"❌ Thất bại: {failed} nhóm"
    )


async def mute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only(update):
        return

    if len(context.args) != 3:
        await update.effective_message.reply_text("❌ Cú pháp:\n/mute ID @tennhom THOIGIAN\nVí dụ:\n/mute 123456 @tennhom 10p")
        return

    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("❌ User ID phải là số.")
        return

    seconds = parse_duration(context.args[2])
    if seconds is None:
        await update.effective_message.reply_text("❌ Thời gian không hợp lệ. Ví dụ: 10p, 2h, 1n")
        return

    chat = await resolve_group(context, context.args[1])
    if not chat:
        await update.effective_message.reply_text("❌ Không tìm thấy nhóm.")
        return

    try:
        member = await context.bot.get_chat_member(chat.id, user_id)
        if member.status in ("administrator", "creator"):
            await update.effective_message.reply_text("❌ Không thể mute admin/owner.")
            return

        await mute_user(context, chat.id, user_id, seconds)
        await update.effective_message.reply_text(
            f"🔇 Đã Mute ID {user_id} tại nhóm {chat.title} trong {context.args[2]}."
        )
    except Exception as e:
        logger.exception("Mute error: %s", e)
        await update.effective_message.reply_text("❌ Không thể mute người dùng.")


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

    chat = await resolve_group(context, context.args[1])
    if not chat:
        await update.effective_message.reply_text("❌ Không tìm thấy nhóm.")
        return

    try:
        await unmute_user(context, chat.id, user_id)
        await update.effective_message.reply_text(f"🔊 Đã Unmute ID {user_id} tại nhóm {chat.title}.")
    except Exception as e:
        logger.exception("Unmute error: %s", e)
        await update.effective_message.reply_text("❌ Không thể mở cấm.")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_group_access(update, context):
        return

    await update.effective_message.reply_text(
        "🤖 BOT LỌC NSFW\n\n"
        "🖼 Tự động kiểm tra ảnh\n"
        "🗑 Phát hiện NSFW → xoá\n"
        f"🔇 Người gửi → mute {AUTO_MUTE_MINUTES} phút\n\n"
        f"👨‍💻 Admin: {OWNER_USERNAME}"
    )


# =========================================================
# GENERAL MESSAGE & PHOTO PROCESSOR
# =========================================================

async def handle_group_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kiểm tra quyền sử dụng đối với bất kỳ tin nhắn nào trong nhóm"""
    await check_group_access(update, context)


async def delete_message(update: Update, context: ContextTypes.DEFAULT_TYPE, reason: str = "NSFW"):
    message = update.effective_message
    if not message:
        return
    try:
        await message.delete()
        logger.info("Đã xoá tin nhắn | reason=%s", reason)
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
    except Exception as e:
        logger.error("Auto mute failed: %s", e)


async def send_warning(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not WARN_USER or not update.effective_chat:
        return

    try:
        user = update.effective_user
        name = user.first_name if user else "Người dùng"
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"⚠️ {name}, nội dung của bạn đã bị xoá vì chứa nội dung nhạy cảm/18+.\n\n🔇 Bạn bị cấm chat {AUTO_MUTE_MINUTES} phút."
        )
    except Exception as e:
        logger.warning("Warning error: %s", e)


async def process_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_group_access(update, context):
        return

    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return

    try:
        member = await chat.get_member(user.id)
        if member.status in ("administrator", "creator"):
            return
    except Exception:
        pass

    message = update.effective_message
    if not message or not message.photo:
        return

    photo = message.photo[1] if len(message.photo) >= 2 else message.photo[0]
    temp_path = None
    start_time = asyncio.get_running_loop().time()

    try:
        telegram_file = await context.bot.get_file(photo.file_id)
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as temp:
            temp_path = temp.name

        await telegram_file.download_to_drive(temp_path)
        result = await detector.check_image(temp_path, NSFW_THRESHOLD)

        if result["nsfw"]:
            await asyncio.gather(
                delete_message(update, context, f"image:{result['label']}:{result['score']:.3f}"),
                auto_mute(update, context),
                return_exceptions=True
            )
            await send_warning(update, context)

    except Exception as e:
        logger.exception("Lỗi xử lý ảnh: %s", e)
    finally:
        if temp_path:
            Path(temp_path).unlink(missing_ok=True)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Bot error:", exc_info=context.error)


# =========================================================
# MAIN
# =========================================================

def main():
    if not BOT_TOKEN:
        raise RuntimeError("Thiếu BOT_TOKEN trên Railway!")

    init_db()

    application = Application.builder().token(BOT_TOKEN).build()

    # Commands
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("capquyen", capquyen_command))
    application.add_handler(CommandHandler("tb", tb_command))
    application.add_handler(CommandHandler("mute", mute_command))
    application.add_handler(CommandHandler("unmute", unmute_command))

    # Handlers Nút Bấm Callback
    application.add_handler(CallbackQueryHandler(button_callback))

    # Handler Quét Ảnh NSFW
    application.add_handler(MessageHandler(filters.PHOTO, process_photo))

    # Handler Kiểm tra tất cả tin nhắn nhóm chưa được cấp quyền
    application.add_handler(MessageHandler(filters.ChatType.GROUPS & (~filters.COMMAND), handle_group_messages))

    # Error
    application.add_error_handler(error_handler)

    logger.info("🤖 NSFW IMAGE BOT STARTED!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
