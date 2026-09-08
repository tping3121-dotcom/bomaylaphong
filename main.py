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


# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")

# Admin/owner chính của bot
OWNER_ID = 7449833411

# Username hiển thị khi báo cho người dùng
OWNER_USERNAME = "@echcuto"

NSFW_THRESHOLD = float(
    os.getenv("NSFW_THRESHOLD", "0.75")
)

WARN_USER = os.getenv(
    "WARN_USER", "true"
).lower() == "true"

WARNING_SECONDS = int(
    os.getenv("WARNING_SECONDS", "5")
)

# Tự động mute bao nhiêu phút khi phát hiện NSFW
AUTO_MUTE_MINUTES = int(
    os.getenv("AUTO_MUTE_MINUTES", "2")
)

# SQLite
# Nên mount Railway Volume vào /data
DATA_DIR = os.getenv("DATA_DIR", "/data")

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
# DATABASE
# =========================================================

def db_connect():
    conn = sqlite3.connect(
        DB_PATH,
        timeout=30
    )

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


def save_group(
    chat_id: int,
    username: str | None,
    title: str
):

    conn = db_connect()

    try:

        conn.execute(
            """
            INSERT INTO groups
            (
                chat_id,
                username,
                title,
                enabled,
                notified,
                created_at
            )
            VALUES (?, ?, ?, 0, 0, ?)

            ON CONFLICT(chat_id)
            DO UPDATE SET
                username = excluded.username,
                title = excluded.title
            """,
            (
                chat_id,
                username,
                title,
                datetime.now(timezone.utc).isoformat()
            )
        )

        conn.commit()

    finally:
        conn.close()


def group_enabled(chat_id: int) -> bool:

    conn = db_connect()

    try:

        row = conn.execute(
            """
            SELECT enabled
            FROM groups
            WHERE chat_id = ?
            """,
            (chat_id,)
        ).fetchone()

        return bool(
            row and row["enabled"] == 1
        )

    finally:
        conn.close()


def set_group_enabled(
    chat_id: int,
    enabled: bool
):

    conn = db_connect()

    try:

        conn.execute(
            """
            UPDATE groups
            SET enabled = ?
            WHERE chat_id = ?
            """,
            (
                1 if enabled else 0,
                chat_id
            )
        )

        conn.commit()

    finally:
        conn.close()


def group_was_notified(chat_id: int) -> bool:

    conn = db_connect()

    try:

        row = conn.execute(
            """
            SELECT notified
            FROM groups
            WHERE chat_id = ?
            """,
            (chat_id,)
        ).fetchone()

        return bool(
            row and row["notified"] == 1
        )

    finally:
        conn.close()


def set_group_notified(chat_id: int):

    conn = db_connect()

    try:

        conn.execute(
            """
            UPDATE groups
            SET notified = 1
            WHERE chat_id = ?
            """,
            (chat_id,)
        )

        conn.commit()

    finally:
        conn.close()


def save_mute(
    chat_id: int,
    user_id: int,
    until_ts: int
):

    conn = db_connect()

    try:

        conn.execute(
            """
            INSERT INTO mutes
            (
                chat_id,
                user_id,
                until_ts
            )
            VALUES (?, ?, ?)

            ON CONFLICT(chat_id, user_id)
            DO UPDATE SET
                until_ts = excluded.until_ts
            """,
            (
                chat_id,
                user_id,
                until_ts
            )
        )

        conn.commit()

    finally:
        conn.close()


def delete_mute(
    chat_id: int,
    user_id: int
):

    conn = db_connect()

    try:

        conn.execute(
            """
            DELETE FROM mutes
            WHERE chat_id = ?
            AND user_id = ?
            """,
            (
                chat_id,
                user_id
            )
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


async def owner_only(
    update: Update
) -> bool:

    if not is_owner(update):

        if update.effective_message:

            await update.effective_message.reply_text(
                "⛔ Bạn không có quyền sử dụng lệnh này."
            )

        return False

    return True


# =========================================================
# TIME PARSER
# =========================================================

def parse_duration(text: str):

    """
    1p = 1 phút
    1h = 1 giờ
    1n = 1 ngày
    1t = 30 ngày

    Ví dụ:
    10p
    2h
    3n
    1t
    """

    text = text.strip().lower()

    match = re.fullmatch(
        r"(\d+)(p|h|n|t)",
        text
    )

    if not match:
        return None

    number = int(match.group(1))
    unit = match.group(2)

    if number <= 0:
        return None

    if unit == "p":
        seconds = number * 60

    elif unit == "h":
        seconds = number * 60 * 60

    elif unit == "n":
        seconds = number * 24 * 60 * 60

    elif unit == "t":
        # 1 tháng = 30 ngày
        seconds = number * 30 * 24 * 60 * 60

    else:
        return None

    # Telegram giới hạn khoảng thời gian
    # restriction hữu hạn ở mức khoảng 366 ngày.
    if seconds > 366 * 24 * 60 * 60:
        return None

    return seconds


# =========================================================
# GROUP RESOLVER
# =========================================================

async def resolve_group(
    context: ContextTypes.DEFAULT_TYPE,
    group_ref: str
):

    group_ref = group_ref.strip()

    try:

        # @username
        if group_ref.startswith("@"):

            chat = await context.bot.get_chat(
                group_ref
            )

        # chat ID
        else:

            chat = await context.bot.get_chat(
                int(group_ref)
            )

        if chat.type not in (
            "group",
            "supergroup"
        ):
            return None

        return chat

    except Exception as e:

        logger.warning(
            "Không tìm thấy nhóm %s: %s",
            group_ref,
            e
        )

        return None


# =========================================================
# GROUP PERMISSION / CAP QUYEN
# =========================================================

async def capquyen_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not await owner_only(update):
        return

    if not update.effective_message:
        return

    if len(context.args) != 1:

        await update.effective_message.reply_text(
            "❌ Cú pháp:\n"
            "/capquyen @tennhom\n\n"
            "Ví dụ:\n"
            "/capquyen @kiemtienfree"
        )

        return

    group_ref = context.args[0]

    chat = await resolve_group(
        context,
        group_ref
    )

    if not chat:

        await update.effective_message.reply_text(
            "❌ Không tìm thấy nhóm.\n"
            "Hãy dùng đúng @username của nhóm "
            "và đảm bảo bot đã được thêm vào nhóm."
        )

        return

    save_group(
        chat.id,
        chat.username,
        chat.title or "Không tên"
    )

    set_group_enabled(
        chat.id,
        True
    )

    username = (
        f"@{chat.username}"
        if chat.username
        else str(chat.id)
    )

    await update.effective_message.reply_text(
        "✅ CẤP QUYỀN THÀNH CÔNG\n\n"
        f"👥 Nhóm: {chat.title}\n"
        f"🔗 {username}\n"
        f"🆔 ID: {chat.id}\n\n"
        "🤖 Bot đã được phép hoạt động "
        "trong nhóm này."
    )


# =========================================================
# CHECK GROUP LICENSE
# =========================================================

async def check_group_access(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
) -> bool:

    chat = update.effective_chat

    if not chat:
        return False

    if chat.type not in (
        "group",
        "supergroup"
    ):
        return True

    save_group(
        chat.id,
        chat.username,
        chat.title or "Không tên"
    )

    if group_enabled(chat.id):
        return True

    # Chưa được cấp quyền.
    #
    # Chỉ báo 1 lần để tránh spam nhóm.
    if not group_was_notified(chat.id):

        set_group_notified(chat.id)

        try:

            await context.bot.send_message(
                chat_id=chat.id,
                text=(
                    "⚠️ Nhóm này chưa được cấp quyền "
                    "sử dụng bot.\n\n"
                    f"Vui lòng liên hệ admin "
                    f"{OWNER_USERNAME} để được cấp quyền sử dụng."
                )
            )

        except Exception as e:

            logger.warning(
                "Không gửi được thông báo nhóm: %s",
                e
            )

        # Báo cho owner.
        try:

            await context.bot.send_message(
                chat_id=OWNER_ID,
                text=(
                    "🔔 NHÓM YÊU CẦU SỬ DỤNG BOT\n\n"
                    f"👥 Nhóm: {chat.title}\n"
                    f"🆔 ID: {chat.id}\n"
                    f"🔗 @{chat.username}"
                    if chat.username
                    else
                    (
                        "🔔 NHÓM YÊU CẦU SỬ DỤNG BOT\n\n"
                        f"👥 Nhóm: {chat.title}\n"
                        f"🆔 ID: {chat.id}\n"
                        "🔗 Nhóm không có username"
                    )
                )
            )

        except Exception as e:

            logger.warning(
                "Không gửi được thông báo owner: %s",
                e
            )

    return False


# =========================================================
# MUTE PERMISSIONS
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
        can_invite_users=False,
        can_pin_messages=False,
        can_change_info=False,
        can_manage_topics=False,
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
        can_invite_users=False,
        can_pin_messages=False,
        can_change_info=False,
        can_manage_topics=False,
    )


# =========================================================
# MUTE USER
# =========================================================

async def mute_user(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
    seconds: int
):

    until_dt = datetime.now(
        timezone.utc
    ) + timedelta(
        seconds=seconds
    )

    until_ts = int(
        until_dt.timestamp()
    )

    await context.bot.restrict_chat_member(
        chat_id=chat_id,
        user_id=user_id,
        permissions=muted_permissions(),
        until_date=until_dt,
        use_independent_chat_permissions=True
    )

    save_mute(
        chat_id,
        user_id,
        until_ts
    )


# =========================================================
# UNMUTE USER
# =========================================================

async def unmute_user(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int
):

    await context.bot.restrict_chat_member(
        chat_id=chat_id,
        user_id=user_id,
        permissions=normal_permissions(),
        until_date=None,
        use_independent_chat_permissions=True
    )

    delete_mute(
        chat_id,
        user_id
    )


# =========================================================
# /MUTE COMMAND
# =========================================================

async def mute_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not await owner_only(update):
        return

    if len(context.args) != 3:

        await update.effective_message.reply_text(
            "❌ Cú pháp:\n\n"
            "/mute ID @tennhom THOIGIAN\n\n"
            "Ví dụ:\n"
            "/mute 9237293294 @kiemtienfree 10p\n\n"
            "Thời gian:\n"
            "1p = 1 phút\n"
            "1h = 1 giờ\n"
            "1n = 1 ngày\n"
            "1t = 1 tháng (30 ngày)"
        )

        return

    user_text = context.args[0]
    group_ref = context.args[1]
    duration_text = context.args[2]

    # User ID
    try:

        user_id = int(
            user_text
        )

    except ValueError:

        await update.effective_message.reply_text(
            "❌ User ID phải là số."
        )

        return

    # Duration
    seconds = parse_duration(
        duration_text
    )

    if seconds is None:

        await update.effective_message.reply_text(
            "❌ Thời gian không hợp lệ.\n\n"
            "Ví dụ: 10p, 2h, 1n, 1t"
        )

        return

    # Group
    chat = await resolve_group(
        context,
        group_ref
    )

    if not chat:

        await update.effective_message.reply_text(
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

            await update.effective_message.reply_text(
                "❌ Không thể mute admin/owner của nhóm."
            )

            return

        await mute_user(
            context,
            chat.id,
            user_id,
            seconds
        )

        await update.effective_message.reply_text(
            "🔇 ĐÃ MUTE\n\n"
            f"👤 User ID: {user_id}\n"
            f"👥 Nhóm: {chat.title}\n"
            f"⏱ Thời gian: {duration_text}"
        )

    except Exception as e:

        logger.exception(
            "Mute error"
        )

        await update.effective_message.reply_text(
            "❌ Không thể mute người dùng.\n\n"
            "Kiểm tra:\n"
            "• Bot đã là admin nhóm chưa\n"
            "• Bot có quyền Restrict Members chưa\n"
            "• User ID có đúng không\n"
            "• Nhóm có phải supergroup không"
        )


# =========================================================
# /UNMUTE COMMAND
# =========================================================

async def unmute_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not await owner_only(update):
        return

    if len(context.args) != 2:

        await update.effective_message.reply_text(
            "❌ Cú pháp:\n\n"
            "/unmute ID @tennhom\n\n"
            "Ví dụ:\n"
            "/unmute 1847194731 @kiemtienfree"
        )

        return

    try:

        user_id = int(
            context.args[0]
        )

    except ValueError:

        await update.effective_message.reply_text(
            "❌ User ID phải là số."
        )

        return

    group_ref = context.args[1]

    chat = await resolve_group(
        context,
        group_ref
    )

    if not chat:

        await update.effective_message.reply_text(
            "❌ Không tìm thấy nhóm."
        )

        return

    try:

        await unmute_user(
            context,
            chat.id,
            user_id
        )

        await update.effective_message.reply_text(
            "🔊 ĐÃ UNMUTE\n\n"
            f"👤 User ID: {user_id}\n"
            f"👥 Nhóm: {chat.title}"
        )

    except Exception as e:

        logger.exception(
            "Unmute error"
        )

        await update.effective_message.reply_text(
            "❌ Không thể mở cấm.\n"
            "Kiểm tra quyền admin của bot "
            "và User ID."
        )


# =========================================================
# /START
# =========================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    chat = update.effective_chat

    if chat and chat.type in (
        "group",
        "supergroup"
    ):

        if not await check_group_access(
            update,
            context
        ):
            return

    await update.effective_message.reply_text(
        "🤖 BOT LỌC NSFW\n\n"
        "🖼 Tự động kiểm tra ảnh\n"
        "🎥 Tự động kiểm tra video\n"
        "🗑 Phát hiện NSFW → xoá\n"
        "🔇 Người gửi → mute 2 phút\n\n"
        f"👨‍💻 Admin: {OWNER_USERNAME}"
    )


# =========================================================
# DELETE MESSAGE
# =========================================================

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
            "Đã xoá | chat=%s | user=%s | reason=%s",
            update.effective_chat.id
            if update.effective_chat
            else "?",
            update.effective_user.id
            if update.effective_user
            else "?",
            reason
        )

    except Exception as e:

        logger.error(
            "Không xoá được tin nhắn: %s",
            e
        )

        return


# =========================================================
# AUTO MUTE
# =========================================================

async def auto_mute(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.effective_chat:
        return

    if not update.effective_user:
        return

    user_id = update.effective_user.id

    try:

        member = await update.effective_chat.get_member(
            user_id
        )

        # Không mute admin nhóm
        if member.status in (
            "administrator",
            "creator"
        ):
            return

        await mute_user(
            context,
            update.effective_chat.id,
            user_id,
            AUTO_MUTE_MINUTES * 60
        )

        logger.info(
            "Auto mute user=%s chat=%s for=%s minutes",
            user_id,
            update.effective_chat.id,
            AUTO_MUTE_MINUTES
        )

    except Exception as e:

        logger.error(
            "Auto mute failed: %s",
            e
        )


# =========================================================
# WARNING
# =========================================================

async def send_warning(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

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
                f"⚠️ {name}, nội dung của bạn "
                "đã bị xoá vì chứa nội dung "
                "nhạy cảm/18+.\n\n"
                f"🔇 Bạn bị cấm chat "
                f"{AUTO_MUTE_MINUTES} phút."
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
            "Warning error: %s",
            e
        )


# =========================================================
# PROCESS PHOTO
# =========================================================

async def process_photo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    # Group chưa được cấp quyền
    if not await check_group_access(
        update,
        context
    ):
        return

    # Không xử lý admin nhóm
    if update.effective_chat:

        try:

            member = await update.effective_chat.get_member(
                update.effective_user.id
            )

            if member.status in (
                "administrator",
                "creator"
            ):
                return

        except Exception:
            pass

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
            "Ảnh result: %s",
            result
        )

        if result["nsfw"]:

            await delete_message(
                update,
                context,
                (
                    f"image:{result['label']}:"
                    f"{result['score']:.3f}"
                )
            )

            await auto_mute(
                update,
                context
            )

            await send_warning(
                update,
                context
            )

    except Exception as e:

        logger.exception(
            "Lỗi xử lý ảnh"
        )

    finally:

        if temp_path:

            try:
                Path(temp_path).unlink(
                    missing_ok=True
                )
            except Exception:
                pass


# =========================================================
# PROCESS VIDEO
# =========================================================

async def process_video(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not await check_group_access(
        update,
        context
    ):
        return

    message = update.effective_message

    if not message or not message.video:
        return

    # Admin nhóm không bị xử lý
    try:

        member = await update.effective_chat.get_member(
            update.effective_user.id
        )

        if member.status in (
            "administrator",
            "creator"
        ):
            return

    except Exception:
        pass

    video = message.video

    # Telegram getFile hiện giới hạn tải file
    # ở 20 MB.
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
            "Video result: %s",
            result
        )

        if result["nsfw"]:

            await delete_message(
                update,
                context,
                (
                    f"video:{result['label']}:"
                    f"{result['score']:.3f}"
                )
            )

            await auto_mute(
                update,
                context
            )

            await send_warning(
                update,
                context
            )

    except Exception as e:

        logger.exception(
            "Lỗi xử lý video"
        )

    finally:

        if temp_path:

            try:
                Path(temp_path).unlink(
                    missing_ok=True
                )
            except Exception:
                pass


# =========================================================
# PROCESS VIDEO DOCUMENT
# =========================================================

async def process_document(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not await check_group_access(
        update,
        context
    ):
        return

    message = update.effective_message

    if not message or not message.document:
        return

    document = message.document

    mime = document.mime_type or ""

    if not mime.startswith("video/"):
        return

    try:

        member = await update.effective_chat.get_member(
            update.effective_user.id
        )

        if member.status in (
            "administrator",
            "creator"
        ):
            return

    except Exception:
        pass

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
                (
                    f"video-file:"
                    f"{result['label']}:"
                    f"{result['score']:.3f}"
                )
            )

            await auto_mute(
                update,
                context
            )

            await send_warning(
                update,
                context
            )

    except Exception as e:

        logger.exception(
            "Lỗi xử lý video file"
        )

    finally:

        if temp_path:

            try:
                Path(temp_path).unlink(
                    missing_ok=True
                )
            except Exception:
                pass


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE
):

    logger.exception(
        "Bot error:",
        exc_info=context.error
    )


# =========================================================
# MAIN
# =========================================================

def main():

    if not BOT_TOKEN:

        raise RuntimeError(
            "Thiếu BOT_TOKEN trên Railway!"
        )

    init_db()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # Commands
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

    # Photos
    application.add_handler(
        MessageHandler(
            filters.PHOTO,
            process_photo
        )
    )

    # Videos
    application.add_handler(
        MessageHandler(
            filters.VIDEO,
            process_video
        )
    )

    # Video gửi dạng document
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
