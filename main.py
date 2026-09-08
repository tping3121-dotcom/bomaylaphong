import os
import re
import sqlite3
import asyncio
import logging
import tempfile
from pathlib import Path
from datetime import datetime, timedelta, timezone

from telegram import (
    Update,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# =========================================================
# OPTIONAL LIBRARIES
# =========================================================

try:
    from PIL import Image, ImageOps
except Exception:
    Image = None
    ImageOps = None

try:
    from transformers import pipeline
except Exception:
    pipeline = None


# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

OWNER_ID = int(os.getenv("OWNER_ID", "7449833411"))
OWNER_USERNAME = os.getenv("OWNER_USERNAME", "@echcuto")

NSFW_THRESHOLD = float(
    os.getenv("NSFW_THRESHOLD", "0.75")
)

WARN_USER = os.getenv(
    "WARN_USER", "true"
).lower() in ("1", "true", "yes")

AUTO_MUTE_MINUTES = int(
    os.getenv("AUTO_MUTE_MINUTES", "2")
)

DATA_DIR = Path(
    os.getenv("DATA_DIR", "/data")
)

DATA_DIR.mkdir(
    parents=True,
    exist_ok=True
)

DB_PATH = DATA_DIR / "bot.db"

# Số ảnh AI được quét cùng lúc.
# Railway yếu thì để 1.
# Railway khỏe có thể thử 2.
MAX_AI_CONCURRENT = int(
    os.getenv("MAX_AI_CONCURRENT", "2")
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
# DATABASE
# =========================================================

def db_connect():
    conn = sqlite3.connect(
        DB_PATH,
        timeout=30,
        check_same_thread=False
    )

    conn.execute(
        "PRAGMA journal_mode=WAL"
    )

    return conn


def init_db():

    conn = db_connect()

    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS groups (
            chat_id INTEGER PRIMARY KEY,
            username TEXT,
            title TEXT,
            enabled INTEGER DEFAULT 0,
            notified INTEGER DEFAULT 0,
            created_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS mutes (
            chat_id INTEGER,
            user_id INTEGER,
            until_ts INTEGER,
            PRIMARY KEY(chat_id, user_id)
        )
    """)

    conn.commit()
    conn.close()

    logger.info("Database initialized")


def save_group(
    chat_id,
    username,
    title
):

    conn = db_connect()

    conn.execute("""
        INSERT INTO groups
        (chat_id, username, title, enabled, notified, created_at)
        VALUES (?, ?, ?, 0, 0, ?)
        ON CONFLICT(chat_id)
        DO UPDATE SET
            username = excluded.username,
            title = excluded.title
    """, (
        chat_id,
        username,
        title,
        datetime.now(
            timezone.utc
        ).isoformat()
    ))

    conn.commit()
    conn.close()


def get_all_groups():

    conn = db_connect()

    rows = conn.execute("""
        SELECT chat_id, username, title
        FROM groups
        WHERE enabled = 1
    """).fetchall()

    conn.close()

    return rows


def group_enabled(chat_id):

    conn = db_connect()

    row = conn.execute("""
        SELECT enabled
        FROM groups
        WHERE chat_id = ?
    """, (chat_id,)).fetchone()

    conn.close()

    return bool(
        row and row[0]
    )


def set_group_enabled(
    chat_id,
    enabled
):

    conn = db_connect()

    conn.execute("""
        UPDATE groups
        SET enabled = ?
        WHERE chat_id = ?
    """, (
        1 if enabled else 0,
        chat_id
    ))

    conn.commit()
    conn.close()


def group_was_notified(chat_id):

    conn = db_connect()

    row = conn.execute("""
        SELECT notified
        FROM groups
        WHERE chat_id = ?
    """, (chat_id,)).fetchone()

    conn.close()

    return bool(
        row and row[0]
    )


def set_group_notified(chat_id):

    conn = db_connect()

    conn.execute("""
        UPDATE groups
        SET notified = 1
        WHERE chat_id = ?
    """, (chat_id,))

    conn.commit()
    conn.close()


def save_mute(
    chat_id,
    user_id,
    until_ts
):

    conn = db_connect()

    conn.execute("""
        INSERT INTO mutes
        (chat_id, user_id, until_ts)
        VALUES (?, ?, ?)
        ON CONFLICT(chat_id, user_id)
        DO UPDATE SET until_ts = excluded.until_ts
    """, (
        chat_id,
        user_id,
        until_ts
    ))

    conn.commit()
    conn.close()


def delete_mute(
    chat_id,
    user_id
):

    conn = db_connect()

    conn.execute("""
        DELETE FROM mutes
        WHERE chat_id = ?
        AND user_id = ?
    """, (
        chat_id,
        user_id
    ))

    conn.commit()
    conn.close()


# =========================================================
# NSFW DETECTOR
# =========================================================

class NsfwDetector:

    def __init__(self):

        self.pipe = None

        self.ai_semaphore = asyncio.Semaphore(
            MAX_AI_CONCURRENT
        )

        if pipeline is None:

            logger.error(
                "transformers chưa được cài đặt."
            )

            return

        try:

            logger.info(
                "Loading NSFW AI model..."
            )

            self.pipe = pipeline(
                "image-classification",
                model="Falconsai/nsfw_image_detection",
                device=-1
            )

            logger.info(
                "NSFW AI model loaded successfully."
            )

        except Exception:

            logger.exception(
                "Không thể load NSFW model."
            )

            self.pipe = None


    def _check_image_sync(
        self,
        image_path,
        threshold
    ):

        if self.pipe is None:

            return {
                "status": "detector_offline",
                "is_nsfw": False,
                "score": 0
            }

        if Image is None:

            return {
                "status": "pillow_offline",
                "is_nsfw": False,
                "score": 0
            }

        try:

            image = Image.open(
                image_path
            )

            # Sửa hướng ảnh theo EXIF
            if ImageOps is not None:

                image = ImageOps.exif_transpose(
                    image
                )

            image = image.convert(
                "RGB"
            )

            # Resize để AI xử lý nhanh hơn
            image.thumbnail(
                (512, 512)
            )

            results = self.pipe(
                image,
                top_k=2
            )

            logger.info(
                "AI results: %s",
                results
            )

            nsfw_score = 0.0

            for result in results:

                label = str(
                    result.get("label", "")
                ).lower()

                score = float(
                    result.get("score", 0)
                )

                if label == "nsfw":

                    nsfw_score = max(
                        nsfw_score,
                        score
                    )

            is_nsfw = (
                nsfw_score >= threshold
            )

            return {
                "status": "ok",
                "is_nsfw": is_nsfw,
                "score": nsfw_score
            }

        except Exception:

            logger.exception(
                "Lỗi khi quét ảnh."
            )

            return {
                "status": "error",
                "is_nsfw": False,
                "score": 0
            }


    async def check_image(
        self,
        image_path,
        threshold
    ):

        # Giới hạn số AI chạy cùng lúc
        async with self.ai_semaphore:

            # Đẩy AI CPU nặng ra thread
            return await asyncio.to_thread(
                self._check_image_sync,
                image_path,
                threshold
            )


detector = NsfwDetector()


# =========================================================
# OWNER
# =========================================================

def is_owner(update):

    user = update.effective_user

    return bool(
        user and user.id == OWNER_ID
    )


# =========================================================
# DURATION
# =========================================================

def parse_duration(value):

    match = re.fullmatch(
        r"(\d+)(p|h|n|t)",
        value.lower().strip()
    )

    if not match:
        return None

    number = int(
        match.group(1)
    )

    unit = match.group(2)

    if number <= 0:
        return None

    if unit == "p":
        return timedelta(
            minutes=number
        )

    if unit == "h":
        return timedelta(
            hours=number
        )

    if unit == "n":
        return timedelta(
            days=number
        )

    if unit == "t":
        return timedelta(
            days=number * 30
        )

    return None


# =========================================================
# RESOLVE GROUP
# =========================================================

async def resolve_group(
    context,
    value
):

    value = value.strip()

    try:

        if value.startswith("@"):

            chat = await context.bot.get_chat(
                value
            )

        else:

            chat = await context.bot.get_chat(
                int(value)
            )

        return chat

    except Exception:

        logger.exception(
            "Không tìm thấy group: %s",
            value
        )

        return None


# =========================================================
# PERMISSIONS
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
        can_change_info=False,
        can_invite_users=False,
        can_pin_messages=False,
        can_manage_topics=False
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
        can_change_info=False,
        can_invite_users=True,
        can_pin_messages=False,
        can_manage_topics=False
    )


# =========================================================
# MUTE
# =========================================================

async def mute_user(
    context,
    chat_id,
    user_id,
    duration
):

    until_dt = (
        datetime.now(timezone.utc)
        + duration
    )

    try:

        await context.bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            permissions=muted_permissions(),
            until_date=until_dt
        )

        save_mute(
            chat_id,
            user_id,
            int(until_dt.timestamp())
        )

        logger.info(
            "Muted user %s in %s until %s",
            user_id,
            chat_id,
            until_dt
        )

        return True

    except Exception:

        logger.exception(
            "Không thể mute user %s",
            user_id
        )

        return False


# =========================================================
# GROUP ACCESS
# =========================================================

async def check_group_access(
    update,
    context
):

    chat = update.effective_chat

    if chat is None:
        return False

    if chat.type not in (
        "group",
        "supergroup"
    ):
        return True

    save_group(
        chat.id,
        chat.username or "",
        chat.title or ""
    )

    if group_enabled(
        chat.id
    ):

        return True

    # Chỉ thông báo một lần
    if not group_was_notified(
        chat.id
    ):

        set_group_notified(
            chat.id
        )

        try:

            await context.bot.send_message(
                chat_id=chat.id,
                text=(
                    "⚠️ Nhóm này chưa được cấp quyền sử dụng bot.\n\n"
                    f"Vui lòng liên hệ admin {OWNER_USERNAME} "
                    "để được cấp quyền."
                )
            )

        except Exception:

            logger.exception(
                "Không thể gửi thông báo nhóm."
            )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "✅ CẤP QUYỀN",
                    callback_data=f"approve_{chat.id}"
                ),
                InlineKeyboardButton(
                    "❌ TỪ CHỐI",
                    callback_data=f"decline_{chat.id}"
                )
            ]
        ])

        try:

            await context.bot.send_message(
                chat_id=OWNER_ID,
                text=(
                    "📥 YÊU CẦU CẤP QUYỀN NHÓM\n\n"
                    f"🏷 Tên: {chat.title}\n"
                    f"🆔 ID: `{chat.id}`\n"
                    f"🔗 Username: "
                    f"@{chat.username}"
                    if chat.username
                    else
                    f"📥 YÊU CẦU CẤP QUYỀN NHÓM\n\n"
                    f"🏷 Tên: {chat.title}\n"
                    f"🆔 ID: `{chat.id}`"
                ),
                parse_mode="Markdown",
                reply_markup=keyboard
            )

        except Exception:

            logger.exception(
                "Không thể báo owner."
            )

    return False


# =========================================================
# CALLBACK
# =========================================================

async def button_callback(
    update,
    context
):

    query = update.callback_query

    await query.answer()

    if query.from_user.id != OWNER_ID:

        await query.answer(
            "Bạn không có quyền.",
            show_alert=True
        )

        return

    data = query.data

    if data.startswith(
        "approve_"
    ):

        chat_id = int(
            data.split("_", 1)[1]
        )

        set_group_enabled(
            chat_id,
            True
        )

        try:

            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "✅ Bot đã được admin cấp quyền "
                    "và bắt đầu hoạt động."
                )
            )

        except Exception:

            logger.exception(
                "Không thể báo nhóm đã cấp quyền."
            )

        await query.edit_message_text(
            "✅ Đã CẤP QUYỀN nhóm."
        )

    elif data.startswith(
        "decline_"
    ):

        chat_id = int(
            data.split("_", 1)[1]
        )

        set_group_enabled(
            chat_id,
            False
        )

        try:

            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "❌ Yêu cầu sử dụng bot đã bị từ chối."
                )
            )

        except Exception:

            pass

        await query.edit_message_text(
            "❌ Đã TỪ CHỐI nhóm."
        )


# =========================================================
# /CAPQUYEN
# =========================================================

async def capquyen_command(
    update,
    context
):

    if not is_owner(update):

        return

    if not context.args:

        await update.message.reply_text(
            "Dùng:\n"
            "/capquyen @username\n"
            "hoặc\n"
            "/capquyen -100123456789"
        )

        return

    group_value = context.args[0]

    chat = await resolve_group(
        context,
        group_value
    )

    if chat is None:

        await update.message.reply_text(
            "❌ Không tìm thấy nhóm."
        )

        return

    if chat.type not in (
        "group",
        "supergroup"
    ):

        await update.message.reply_text(
            "❌ Đây không phải nhóm."
        )

        return

    save_group(
        chat.id,
        chat.username or "",
        chat.title or ""
    )

    set_group_enabled(
        chat.id,
        True
    )

    await update.message.reply_text(
        f"✅ Đã cấp quyền cho:\n"
        f"🏷 {chat.title}\n"
        f"🆔 {chat.id}"
    )

    try:

        await context.bot.send_message(
            chat_id=chat.id,
            text=(
                "✅ Nhóm đã được cấp quyền sử dụng bot.\n"
                "Bot đã bắt đầu tự động kiểm tra ảnh."
            )
        )

    except Exception:

        pass


# =========================================================
# /TB
# =========================================================

async def tb_command(
    update,
    context
):

    if not is_owner(update):

        return

    if not context.args:

        await update.message.reply_text(
            "Dùng:\n"
            "/tb nội dung thông báo"
        )

        return

    text = " ".join(
        context.args
    )

    groups = get_all_groups()

    success = 0
    failed = 0

    for chat_id, username, title in groups:

        try:

            await context.bot.send_message(
                chat_id=chat_id,
                text=text
            )

            success += 1

        except Exception:

            failed += 1

        await asyncio.sleep(
            0.05
        )

    await update.message.reply_text(
        f"📢 Đã gửi thông báo.\n"
        f"✅ Thành công: {success}\n"
        f"❌ Thất bại: {failed}"
    )


# =========================================================
# /MUTE
# =========================================================

async def mute_command(
    update,
    context
):

    if not is_owner(update):

        return

    if len(context.args) != 3:

        await update.message.reply_text(
            "Dùng:\n"
            "/mute USER_ID @GROUP 2p\n\n"
            "Ví dụ:\n"
            "/mute 123456789 @mygroup 10p"
        )

        return

    try:

        user_id = int(
            context.args[0]
        )

    except ValueError:

        await update.message.reply_text(
            "❌ USER_ID không hợp lệ."
        )

        return

    group_value = context.args[1]

    duration = parse_duration(
        context.args[2]
    )

    if duration is None:

        await update.message.reply_text(
            "❌ Thời gian không hợp lệ.\n\n"
            "Ví dụ:\n"
            "10p = 10 phút\n"
            "2h = 2 giờ\n"
            "1n = 1 ngày\n"
            "1t = 30 ngày"
        )

        return

    chat = await resolve_group(
        context,
        group_value
    )

    if chat is None:

        await update.message.reply_text(
            "❌ Không tìm thấy nhóm."
        )

        return

    try:

        member = await context.bot.get_chat_member(
            chat.id,
            user_id
        )

        if member.status in (
            "administrator",
            "creator"
        ):

            await update.message.reply_text(
                "❌ Không thể mute admin/chủ nhóm."
            )

            return

    except Exception:

        await update.message.reply_text(
            "❌ Không lấy được thông tin thành viên."
        )

        return

    success = await mute_user(
        context,
        chat.id,
        user_id,
        duration
    )

    if success:

        await update.message.reply_text(
            f"✅ Đã mute `{user_id}` "
            f"trong nhóm `{chat.title}` "
            f"thời gian {context.args[2]}.",
            parse_mode="Markdown"
        )

    else:

        await update.message.reply_text(
            "❌ Không thể mute."
        )


# =========================================================
# /UNMUTE
# =========================================================

async def unmute_command(
    update,
    context
):

    if not is_owner(update):

        return

    if len(context.args) != 2:

        await update.message.reply_text(
            "Dùng:\n"
            "/unmute USER_ID @GROUP"
        )

        return

    try:

        user_id = int(
            context.args[0]
        )

    except ValueError:

        await update.message.reply_text(
            "❌ USER_ID không hợp lệ."
        )

        return

    chat = await resolve_group(
        context,
        context.args[1]
    )

    if chat is None:

        await update.message.reply_text(
            "❌ Không tìm thấy nhóm."
        )

        return

    try:

        await context.bot.restrict_chat_member(
            chat_id=chat.id,
            user_id=user_id,
            permissions=normal_permissions()
        )

        delete_mute(
            chat.id,
            user_id
        )

        await update.message.reply_text(
            f"✅ Đã unmute `{user_id}` "
            f"trong `{chat.title}`.",
            parse_mode="Markdown"
        )

    except Exception:

        logger.exception(
            "Unmute failed"
        )

        await update.message.reply_text(
            "❌ Không thể unmute."
        )


# =========================================================
# START
# =========================================================

async def start_command(
    update,
    context
):

    if update.effective_chat.type in (
        "group",
        "supergroup"
    ):

        allowed = await check_group_access(
            update,
            context
        )

        if not allowed:

            return

    await update.message.reply_text(
        "🤖 NSFW MODERATION BOT\n\n"
        "Bot tự động kiểm tra ảnh được gửi "
        "trong nhóm.\n\n"
        "🔞 Phát hiện ảnh 18+\n"
        "🗑 Tự động xoá ảnh\n"
        f"🔇 Tự động mute {AUTO_MUTE_MINUTES} phút\n"
        "👮 Bỏ qua admin\n\n"
        "⚡ Không quét video."
    )


# =========================================================
# CHECK ADMIN
# =========================================================

async def is_admin(
    context,
    chat_id,
    user_id
):

    try:

        member = await context.bot.get_chat_member(
            chat_id,
            user_id
        )

        return member.status in (
            "administrator",
            "creator"
        )

    except Exception:

        return False


# =========================================================
# PROCESS PHOTO
# =========================================================

async def process_photo(
    update,
    context
):

    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if message is None or chat is None:

        return

    if chat.type not in (
        "group",
        "supergroup"
    ):

        return

    logger.info(
        "📷 Nhận ảnh | chat=%s user=%s message=%s",
        chat.id,
        user.id if user else None,
        message.message_id
    )

    # Kiểm tra nhóm có quyền
    allowed = await check_group_access(
        update,
        context
    )

    if not allowed:

        return

    # Bỏ qua admin
    if user:

        if await is_admin(
            context,
            chat.id,
            user.id
        ):

            logger.info(
                "👮 Bỏ qua admin %s",
                user.id
            )

            return

    # Lấy ảnh lớn nhất
    photo = message.photo[-1]

    temp_path = None

    try:

        # Tạo file tạm
        with tempfile.NamedTemporaryFile(
            suffix=".jpg",
            delete=False
        ) as temp_file:

            temp_path = temp_file.name

        logger.info(
            "⬇️ Download ảnh %s",
            message.message_id
        )

        telegram_file = await context.bot.get_file(
            photo.file_id
        )

        await telegram_file.download_to_drive(
            temp_path
        )

        logger.info(
            "🔍 Đang quét ảnh %s",
            message.message_id
        )

        result = await detector.check_image(
            temp_path,
            NSFW_THRESHOLD
        )

        logger.info(
            "🔎 Kết quả message=%s result=%s",
            message.message_id,
            result
        )

        # Nếu detector lỗi thì KHÔNG xoá ảnh
        if result["status"] != "ok":

            logger.error(
                "Detector không hoạt động: %s",
                result["status"]
            )

            return

        if not result["is_nsfw"]:

            logger.info(
                "✅ Ảnh sạch | message=%s",
                message.message_id
            )

            return

        logger.warning(
            "🔞 PHÁT HIỆN NSFW | chat=%s user=%s "
            "message=%s score=%.3f",
            chat.id,
            user.id if user else None,
            message.message_id,
            result["score"]
        )

        # Xoá ảnh NGAY
        try:

            await message.delete()

            logger.info(
                "🗑 Đã xoá ảnh %s",
                message.message_id
            )

        except Exception:

            logger.exception(
                "❌ Không xoá được ảnh %s",
                message.message_id
            )

        # Mute người gửi
        if user:

            duration = timedelta(
                minutes=AUTO_MUTE_MINUTES
            )

            await mute_user(
                context,
                chat.id,
                user.id,
                duration
            )

            # Cảnh báo
            if WARN_USER:

                try:

                    await context.bot.send_message(
                        chat_id=chat.id,
                        text=(
                            f"⚠️ {user.mention_html()} "
                            "đã gửi nội dung không phù hợp.\n"
                            f"🔇 Đã bị mute "
                            f"{AUTO_MUTE_MINUTES} phút."
                        ),
                        parse_mode="HTML"
                    )

                except Exception:

                    logger.exception(
                        "Không gửi được cảnh báo."
                    )

    except Exception:

        logger.exception(
            "❌ Lỗi process_photo"
        )

    finally:

        if temp_path:

            try:

                os.remove(
                    temp_path
                )

            except Exception:

                pass


# =========================================================
# GROUP MESSAGE HANDLER
# =========================================================

async def handle_group_messages(
    update,
    context
):

    # Không xử lý video.
    # Chỉ dùng handler này để phát hiện nhóm
    # chưa được cấp quyền.

    if update.effective_chat is None:

        return

    if update.effective_chat.type not in (
        "group",
        "supergroup"
    ):

        return

    await check_group_access(
        update,
        context
    )


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(
    update,
    context
):

    logger.error(
        "Telegram error: %s",
        context.error,
        exc_info=context.error
    )


# =========================================================
# MAIN
# =========================================================

def main():

    if not BOT_TOKEN:

        raise RuntimeError(
            "BOT_TOKEN chưa được cấu hình."
        )

    init_db()

    logger.info(
        "Starting bot..."
    )

    application = (
        Application.builder()
        .token(BOT_TOKEN)

        # Cho phép xử lý nhiều update
        # thay vì chờ từng ảnh một.
        .concurrent_updates(10)

        .build()
    )

    # -----------------------------
    # COMMANDS
    # -----------------------------

    application.add_handler(
        CommandHandler(
            "start",
            start_command
        )
    )

    application.add_handler(
        CommandHandler(
            "capquyen",
            capquyen_command
        )
    )

    application.add_handler(
        CommandHandler(
            "tb",
            tb_command
        )
    )

    application.add_handler(
        CommandHandler(
            "mute",
            mute_command
        )
    )

    application.add_handler(
        CommandHandler(
            "unmute",
            unmute_command
        )
    )

    # -----------------------------
    # ADMIN BUTTON
    # -----------------------------

    application.add_handler(
        CallbackQueryHandler(
            button_callback
        )
    )

    # -----------------------------
    # PHOTO ONLY
    # -----------------------------

    application.add_handler(
        MessageHandler(
            filters.PHOTO,
            process_photo
        )
    )

    # -----------------------------
    # OTHER GROUP MESSAGES
    # -----------------------------

    application.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS
            & (~filters.COMMAND)
            & (~filters.PHOTO),
            handle_group_messages
        )
    )

    application.add_error_handler(
        error_handler
    )

    logger.info(
        "🤖 BOT RUNNING"
    )

    logger.info(
        "⚡ AI concurrent limit: %s",
        MAX_AI_CONCURRENT
    )

    logger.info(
        "🔞 NSFW threshold: %s",
        NSFW_THRESHOLD
    )

    logger.info(
        "🎥 Video scanning: DISABLED"
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":

    main()
