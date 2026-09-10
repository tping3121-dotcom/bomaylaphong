import os
import re
import sqlite3
import asyncio
import logging
import tempfile
from pathlib import Path
from datetime import datetime, timedelta, timezone

from PIL import Image, ImageOps

from telegram import (
    Update,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# =========================================================
# CẤU HÌNH
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "THAY_BOT_TOKEN_CUA_BAN")

# Telegram ID của chủ bot
OWNER_ID = int(os.getenv("OWNER_ID", "7449833411"))

# Model NSFW
NSFW_MODEL = "Falconsai/nsfw_image_detection"

# Ảnh thường
NSFW_THRESHOLD = float(os.getenv("NSFW_THRESHOLD", "0.70"))

# Sticker dùng ngưỡng thấp hơn một chút
# để bắt được sticker NSFW tốt hơn.
STICKER_NSFW_THRESHOLD = float(
    os.getenv("STICKER_NSFW_THRESHOLD", "0.55")
)

# Số lần vi phạm tối đa
MAX_VIOLATIONS = 3

# Thời gian mute lần 1/2
MUTE_MINUTES = 2

# Số AI inference chạy cùng lúc
MAX_AI_CONCURRENT = 2

# Database
DB_PATH = os.getenv("DB_PATH", "nsfw_bot.db")


# =========================================================
# LOG
# =========================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("NSFW_BOT")


# =========================================================
# DATABASE
# =========================================================

db_lock = asyncio.Lock()


def get_db():
    conn = sqlite3.connect(
        DB_PATH,
        timeout=30,
        check_same_thread=False,
    )

    conn.row_factory = sqlite3.Row

    return conn


def init_db():
    conn = get_db()

    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS groups (
                chat_id INTEGER PRIMARY KEY,
                enabled INTEGER DEFAULT 0,
                title TEXT,
                requested_by INTEGER,
                approved_at TEXT
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS violations (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                full_name TEXT,
                count INTEGER DEFAULT 0,
                last_violation TEXT,
                PRIMARY KEY(chat_id, user_id)
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS muted_users (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                until_time TEXT,
                PRIMARY KEY(chat_id, user_id)
            )
            """
        )

        conn.commit()

    finally:
        conn.close()


def group_enabled_sync(chat_id: int) -> bool:
    conn = get_db()

    try:
        row = conn.execute(
            "SELECT enabled FROM groups WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()

        return bool(row and row["enabled"])

    finally:
        conn.close()


async def group_enabled(chat_id: int) -> bool:
    return await asyncio.to_thread(
        group_enabled_sync,
        chat_id,
    )


def request_group_access_sync(
    chat_id: int,
    title: str,
    user_id: int,
):
    conn = get_db()

    try:
        conn.execute(
            """
            INSERT INTO groups(
                chat_id,
                enabled,
                title,
                requested_by
            )
            VALUES (?, 0, ?, ?)
            ON CONFLICT(chat_id)
            DO UPDATE SET
                title = excluded.title,
                requested_by = excluded.requested_by
            """,
            (
                chat_id,
                title,
                user_id,
            ),
        )

        conn.commit()

    finally:
        conn.close()


async def request_group_access(
    chat_id: int,
    title: str,
    user_id: int,
):
    await asyncio.to_thread(
        request_group_access_sync,
        chat_id,
        title,
        user_id,
    )


def approve_group_sync(chat_id: int):
    conn = get_db()

    try:
        conn.execute(
            """
            UPDATE groups
            SET enabled = 1,
                approved_at = ?
            WHERE chat_id = ?
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                chat_id,
            ),
        )

        conn.commit()

    finally:
        conn.close()


async def approve_group(chat_id: int):
    await asyncio.to_thread(
        approve_group_sync,
        chat_id,
    )


def disable_group_sync(chat_id: int):
    conn = get_db()

    try:
        conn.execute(
            """
            UPDATE groups
            SET enabled = 0
            WHERE chat_id = ?
            """,
            (chat_id,),
        )

        conn.commit()

    finally:
        conn.close()


async def disable_group(chat_id: int):
    await asyncio.to_thread(
        disable_group_sync,
        chat_id,
    )


def add_violation_sync(
    chat_id: int,
    user_id: int,
    username: str,
    full_name: str,
) -> int:

    conn = get_db()

    try:
        row = conn.execute(
            """
            SELECT count
            FROM violations
            WHERE chat_id = ?
            AND user_id = ?
            """,
            (
                chat_id,
                user_id,
            ),
        ).fetchone()

        if row:
            count = int(row["count"]) + 1

            conn.execute(
                """
                UPDATE violations
                SET count = ?,
                    username = ?,
                    full_name = ?,
                    last_violation = ?
                WHERE chat_id = ?
                AND user_id = ?
                """,
                (
                    count,
                    username,
                    full_name,
                    datetime.now(timezone.utc).isoformat(),
                    chat_id,
                    user_id,
                ),
            )

        else:
            count = 1

            conn.execute(
                """
                INSERT INTO violations(
                    chat_id,
                    user_id,
                    username,
                    full_name,
                    count,
                    last_violation
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_id,
                    user_id,
                    username,
                    full_name,
                    count,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

        conn.commit()

        return count

    finally:
        conn.close()


async def add_violation(
    chat_id: int,
    user_id: int,
    username: str,
    full_name: str,
) -> int:

    return await asyncio.to_thread(
        add_violation_sync,
        chat_id,
        user_id,
        username,
        full_name,
    )


def get_violation_count_sync(
    chat_id: int,
    user_id: int,
) -> int:

    conn = get_db()

    try:
        row = conn.execute(
            """
            SELECT count
            FROM violations
            WHERE chat_id = ?
            AND user_id = ?
            """,
            (
                chat_id,
                user_id,
            ),
        ).fetchone()

        return int(row["count"]) if row else 0

    finally:
        conn.close()


async def get_violation_count(
    chat_id: int,
    user_id: int,
) -> int:

    return await asyncio.to_thread(
        get_violation_count_sync,
        chat_id,
        user_id,
    )


def reset_violation_sync(
    chat_id: int,
    user_id: int,
):
    conn = get_db()

    try:
        conn.execute(
            """
            DELETE FROM violations
            WHERE chat_id = ?
            AND user_id = ?
            """,
            (
                chat_id,
                user_id,
            ),
        )

        conn.commit()

    finally:
        conn.close()


async def reset_violation(
    chat_id: int,
    user_id: int,
):
    await asyncio.to_thread(
        reset_violation_sync,
        chat_id,
        user_id,
    )


def save_mute_sync(
    chat_id: int,
    user_id: int,
    until_time: datetime,
):
    conn = get_db()

    try:
        conn.execute(
            """
            INSERT INTO muted_users(
                chat_id,
                user_id,
                until_time
            )
            VALUES (?, ?, ?)
            ON CONFLICT(chat_id, user_id)
            DO UPDATE SET
                until_time = excluded.until_time
            """,
            (
                chat_id,
                user_id,
                until_time.isoformat(),
            ),
        )

        conn.commit()

    finally:
        conn.close()


async def save_mute(
    chat_id: int,
    user_id: int,
    until_time: datetime,
):
    await asyncio.to_thread(
        save_mute_sync,
        chat_id,
        user_id,
        until_time,
    )


def delete_mute_sync(
    chat_id: int,
    user_id: int,
):
    conn = get_db()

    try:
        conn.execute(
            """
            DELETE FROM muted_users
            WHERE chat_id = ?
            AND user_id = ?
            """,
            (
                chat_id,
                user_id,
            ),
        )

        conn.commit()

    finally:
        conn.close()


async def delete_mute(
    chat_id: int,
    user_id: int,
):
    await asyncio.to_thread(
        delete_mute_sync,
        chat_id,
        user_id,
    )


# =========================================================
# NSFW AI
# =========================================================

class NsfwDetector:

    def __init__(self):

        self.pipe = None

        try:
            from transformers import pipeline

            logger.info(
                "Đang tải model NSFW: %s",
                NSFW_MODEL,
            )

            self.pipe = pipeline(
                "image-classification",
                model=NSFW_MODEL,
            )

            logger.info(
                "NSFW AI model loaded successfully."
            )

        except Exception as e:

            logger.exception(
                "Không thể load NSFW model: %s",
                e,
            )

    def prepare_image(
        self,
        image_path: str,
        is_sticker: bool = False,
    ):

        image = Image.open(image_path)

        image = ImageOps.exif_transpose(image)

        # =================================================
        # XỬ LÝ STICKER TRONG SUỐT
        # =================================================

        if (
            image.mode in ("RGBA", "LA")
            or "transparency" in image.info
        ):

            rgba = image.convert("RGBA")

            background = Image.new(
                "RGBA",
                rgba.size,
                (255, 255, 255, 255),
            )

            background.alpha_composite(rgba)

            image = background.convert("RGB")

        else:

            image = image.convert("RGB")

        # =================================================
        # STICKER
        # =================================================

        if is_sticker:

            # Đưa sticker vào canvas 512x512
            # giúp model nhìn rõ sticker nhỏ.

            canvas = Image.new(
                "RGB",
                (512, 512),
                (255, 255, 255),
            )

            image.thumbnail(
                (480, 480),
                Image.Resampling.LANCZOS,
            )

            x = (512 - image.width) // 2
            y = (512 - image.height) // 2

            canvas.paste(
                image,
                (x, y),
            )

            image = canvas

        else:

            image.thumbnail(
                (512, 512),
                Image.Resampling.LANCZOS,
            )

        return image

    def predict_sync(
        self,
        image_path: str,
        is_sticker: bool = False,
    ):

        if self.pipe is None:

            return {
                "is_nsfw": False,
                "score": 0.0,
                "label": "detector_offline",
            }

        try:

            image = self.prepare_image(
                image_path,
                is_sticker=is_sticker,
            )

            results = self.pipe(
                image,
                top_k=5,
            )

            logger.info(
                "AI RESULT: %s",
                results,
            )

            threshold = (
                STICKER_NSFW_THRESHOLD
                if is_sticker
                else NSFW_THRESHOLD
            )

            best_nsfw_score = 0.0
            best_label = ""

            for result in results:

                label = str(
                    result.get("label", "")
                ).strip().lower()

                score = float(
                    result.get("score", 0)
                )

                if "nsfw" in label:

                    if score > best_nsfw_score:

                        best_nsfw_score = score
                        best_label = label

            is_nsfw = (
                best_nsfw_score >= threshold
            )

            return {
                "is_nsfw": is_nsfw,
                "score": best_nsfw_score,
                "label": best_label or "normal",
            }

        except Exception as e:

            logger.exception(
                "Lỗi NSFW prediction: %s",
                e,
            )

            return {
                "is_nsfw": False,
                "score": 0.0,
                "label": "error",
            }


detector = NsfwDetector()

ai_semaphore = asyncio.Semaphore(
    MAX_AI_CONCURRENT
)


async def detect_nsfw(
    image_path: str,
    is_sticker: bool = False,
):

    async with ai_semaphore:

        return await asyncio.to_thread(
            detector.predict_sync,
            image_path,
            is_sticker,
        )


# =========================================================
# TELEGRAM HELPER
# =========================================================

async def is_admin(
    bot,
    chat_id: int,
    user_id: int,
) -> bool:

    try:

        member = await bot.get_chat_member(
            chat_id,
            user_id,
        )

        return member.status in (
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        )

    except Exception as e:

        logger.error(
            "Không kiểm tra được admin: %s",
            e,
        )

        return False


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


def mute_permissions():

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


async def mute_user(
    bot,
    chat_id: int,
    user_id: int,
    minutes: int = MUTE_MINUTES,
):

    until_time = datetime.now(
        timezone.utc
    ) + timedelta(
        minutes=minutes
    )

    await bot.restrict_chat_member(
        chat_id=chat_id,
        user_id=user_id,
        permissions=mute_permissions(),
        until_date=until_time,
    )

    await save_mute(
        chat_id,
        user_id,
        until_time,
    )

    return until_time


async def unmute_user(
    bot,
    chat_id: int,
    user_id: int,
):

    await bot.restrict_chat_member(
        chat_id=chat_id,
        user_id=user_id,
        permissions=normal_permissions(),
    )

    await delete_mute(
        chat_id,
        user_id,
    )


async def ban_user(
    bot,
    chat_id: int,
    user_id: int,
):

    await bot.ban_chat_member(
        chat_id=chat_id,
        user_id=user_id,
    )


async def unban_user(
    bot,
    chat_id: int,
    user_id: int,
):

    await bot.unban_chat_member(
        chat_id=chat_id,
        user_id=user_id,
        only_if_banned=True,
    )


# =========================================================
# LẤY TARGET TỪ MESSAGE
# =========================================================

async def resolve_user(
    bot,
    chat_id: int,
    text: str,
):

    text = text.strip()

    # ID
    if re.fullmatch(
        r"-?\d+",
        text,
    ):

        try:

            user_id = int(text)

            member = await bot.get_chat_member(
                chat_id,
                user_id,
            )

            return member.user

        except Exception:

            return None

    # @username
    if text.startswith("@"):

        username = text[1:]

        try:

            member = await bot.get_chat_member(
                chat_id,
                username,
            )

            return member.user

        except Exception:

            return None

    return None


# =========================================================
# /START
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(
        "🤖 Bot kiểm duyệt nội dung 18+\n\n"
        "Bot có thể tự động quét ảnh và sticker "
        "trong nhóm.\n\n"
        "Ảnh/video 18+ sẽ bị xoá.\n"
        "Vi phạm lần 1-2: mute 2 phút.\n"
        "Vi phạm lần 3: ban vĩnh viễn."
    )


# =========================================================
# /CAPQUYEN
# =========================================================

async def capquyen(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if chat.type not in (
        "group",
        "supergroup",
    ):

        await message.reply_text(
            "❌ Lệnh này chỉ dùng trong nhóm."
        )

        return

    # Chỉ admin nhóm mới yêu cầu cấp quyền
    if not await is_admin(
        context.bot,
        chat.id,
        user.id,
    ):

        await message.reply_text(
            "❌ Chỉ quản trị viên nhóm mới có thể "
            "yêu cầu cấp quyền."
        )

        return

    await request_group_access(
        chat.id,
        chat.title or "Không tên",
        user.id,
    )

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ CẤP QUYỀN",
                    callback_data=f"approve:{chat.id}",
                ),
                InlineKeyboardButton(
                    "❌ TỪ CHỐI",
                    callback_data=f"deny:{chat.id}",
                ),
            ]
        ]
    )

    try:

        await context.bot.send_message(
            chat_id=OWNER_ID,
            text=(
                "🔔 YÊU CẦU CẤP QUYỀN BOT\n\n"
                f"📌 Nhóm: {chat.title}\n"
                f"🆔 Chat ID: `{chat.id}`\n"
                f"👤 Người yêu cầu: "
                f"{user.full_name}\n"
                f"🆔 User ID: `{user.id}`"
            ),
            parse_mode="Markdown",
            reply_markup=keyboard,
        )

    except Exception as e:

        logger.error(
            "Không gửi được yêu cầu cho owner: %s",
            e,
        )

    await message.reply_text(
        "✅ Đã gửi yêu cầu cấp quyền cho chủ bot.\n"
        "Vui lòng chờ admin duyệt."
    )


# =========================================================
# CALLBACK CẤP QUYỀN
# =========================================================

async def callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    data = query.data or ""

    # =====================================================
    # OWNER DUYỆT NHÓM
    # =====================================================

    if data.startswith("approve:"):

        if query.from_user.id != OWNER_ID:

            await query.answer(
                "❌ Bạn không có quyền.",
                show_alert=True,
            )

            return

        chat_id = int(
            data.split(":", 1)[1]
        )

        await approve_group(chat_id)

        await query.edit_message_text(
            "✅ Đã cấp quyền sử dụng bot cho nhóm:\n"
            f"`{chat_id}`",
            parse_mode="Markdown",
        )

        try:

            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "✅ Bot đã được cấp quyền sử dụng "
                    "trong nhóm này.\n\n"
                    "🛡️ Hệ thống tự động kiểm duyệt "
                    "ảnh/sticker 18+ đã được bật."
                ),
            )

        except Exception as e:

            logger.error(
                "Không gửi thông báo nhóm: %s",
                e,
            )

        return

    # =====================================================
    # OWNER TỪ CHỐI
    # =====================================================

    if data.startswith("deny:"):

        if query.from_user.id != OWNER_ID:

            await query.answer(
                "❌ Bạn không có quyền.",
                show_alert=True,
            )

            return

        chat_id = int(
            data.split(":", 1)[1]
        )

        await disable_group(chat_id)

        await query.edit_message_text(
            "❌ Đã từ chối yêu cầu cấp quyền.\n"
            f"Chat ID: `{chat_id}`",
            parse_mode="Markdown",
        )

        return

    # =====================================================
    # UNMUTE
    # =====================================================

    if data.startswith("unmute:"):

        _, chat_id, user_id = data.split(":")

        chat_id = int(chat_id)
        user_id = int(user_id)

        if not await is_admin(
            context.bot,
            chat_id,
            query.from_user.id,
        ):

            await query.answer(
                "❌ Chỉ admin nhóm mới được thao tác.",
                show_alert=True,
            )

            return

        try:

            await unmute_user(
                context.bot,
                chat_id,
                user_id,
            )

            await query.answer(
                "✅ Đã bỏ mute."
            )

            await query.edit_message_text(
                query.message.text
                + "\n\n✅ Đã được admin bỏ mute."
            )

        except Exception as e:

            await query.answer(
                f"Lỗi: {e}",
                show_alert=True,
            )

        return

    # =====================================================
    # HUỶ MUTE
    # =====================================================

    if data.startswith("cancelmute:"):

        if not await is_admin(
            context.bot,
            query.message.chat.id
            if query.message
            else 0,
            query.from_user.id,
        ):

            await query.answer(
                "❌ Không có quyền.",
                show_alert=True,
            )

            return

        await query.answer(
            "Đã huỷ thao tác."
        )

        try:

            await query.edit_message_reply_markup(
                reply_markup=None
            )

        except Exception:
            pass

        return

    # =====================================================
    # UNBAN
    # =====================================================

    if data.startswith("unban:"):

        _, chat_id, user_id = data.split(":")

        chat_id = int(chat_id)
        user_id = int(user_id)

        if not await is_admin(
            context.bot,
            chat_id,
            query.from_user.id,
        ):

            await query.answer(
                "❌ Chỉ admin nhóm mới được thao tác.",
                show_alert=True,
            )

            return

        try:

            await unban_user(
                context.bot,
                chat_id,
                user_id,
            )

            await query.answer(
                "✅ Đã unban."
            )

            await query.edit_message_text(
                query.message.text
                + "\n\n✅ Đã được admin unban."
            )

        except Exception as e:

            await query.answer(
                f"Lỗi: {e}",
                show_alert=True,
            )

        return

    # =====================================================
    # HUỶ BAN
    # =====================================================

    if data.startswith("cancelban:"):

        await query.answer(
            "Đã huỷ thao tác."
        )

        try:

            await query.edit_message_reply_markup(
                reply_markup=None
            )

        except Exception:
            pass

        return


# =========================================================
# KIỂM TRA NGƯỜI GỬI
# =========================================================

async def should_ignore_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:

    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if not message or not chat or not user:
        return True

    # Chỉ nhóm
    if chat.type not in (
        "group",
        "supergroup",
    ):

        return True

    # Bot
    if user.is_bot:
        return True

    # Admin
    if await is_admin(
        context.bot,
        chat.id,
        user.id,
    ):

        return True

    # Nhóm chưa được cấp quyền
    if not await group_enabled(chat.id):

        return True

    return False


# =========================================================
# LẤY FILE CẦN QUÉT
# =========================================================

def get_media_file_id(message):

    # =====================================================
    # ẢNH TELEGRAM
    # =====================================================

    if message.photo:

        return (
            message.photo[-1].file_id,
            "photo",
            ".jpg",
        )

    # =====================================================
    # STICKER
    # =====================================================

    if message.sticker:

        sticker = message.sticker

        # -----------------------------------------------
        # STICKER ĐỘNG / VIDEO
        # -----------------------------------------------

        if (
            sticker.is_animated
            or sticker.is_video
        ):

            # Không tải .tgs/.webm trực tiếp.
            # Dùng thumbnail để AI quét.

            if sticker.thumbnail:

                return (
                    sticker.thumbnail.file_id,
                    "sticker",
                    ".jpg",
                )

            # Không có thumbnail thì bỏ qua
            return (
                None,
                "sticker_no_thumbnail",
                None,
            )

        # -----------------------------------------------
        # STICKER TĨNH
        # -----------------------------------------------

        return (
            sticker.file_id,
            "sticker",
            ".webp",
        )

    # =====================================================
    # DOCUMENT HÌNH ẢNH
    # =====================================================

    if message.document:

        mime = (
            message.document.mime_type
            or ""
        ).lower()

        if mime.startswith("image/"):

            return (
                message.document.file_id,
                "image_document",
                ".jpg",
            )

    # =====================================================
    # KHÔNG XỬ LÝ VIDEO / GIF
    # =====================================================

    return (
        None,
        None,
        None,
    )


# =========================================================
# XỬ LÝ ẢNH / STICKER
# =========================================================

async def process_photo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if not message or not chat or not user:
        return

    # Kiểm tra có được phép quét không
    if await should_ignore_message(
        update,
        context,
    ):

        return

    (
        file_id,
        media_type,
        extension,
    ) = get_media_file_id(message)

    # Không phải ảnh/sticker
    if not file_id:

        logger.info(
            "Bỏ qua media: %s",
            media_type,
        )

        return

    is_sticker = (
        media_type == "sticker"
    )

    logger.info(
        "Đang quét %s | chat=%s | user=%s",
        media_type,
        chat.id,
        user.id,
    )

    temp_path = None

    try:

        # =================================================
        # TẠO FILE TẠM
        # =================================================

        with tempfile.NamedTemporaryFile(
            suffix=extension,
            delete=False,
        ) as tmp:

            temp_path = tmp.name

        # =================================================
        # DOWNLOAD
        # =================================================

        telegram_file = (
            await context.bot.get_file(
                file_id
            )
        )

        await telegram_file.download_to_drive(
            custom_path=temp_path
        )

        # =================================================
        # AI
        # =================================================

        result = await detect_nsfw(
            temp_path,
            is_sticker=is_sticker,
        )

        logger.info(
            "SCAN RESULT | type=%s | label=%s | score=%.4f | nsfw=%s",
            media_type,
            result["label"],
            result["score"],
            result["is_nsfw"],
        )

        # =================================================
        # KHÔNG NSFW
        # =================================================

        if not result["is_nsfw"]:

            return

        # =================================================
        # NSFW
        # =================================================

        try:

            await message.delete()

            logger.info(
                "Đã xoá NSFW | chat=%s | user=%s",
                chat.id,
                user.id,
            )

        except Exception as e:

            logger.error(
                "Không xoá được message: %s",
                e,
            )

        # =================================================
        # CỘNG VI PHẠM
        # =================================================

        username = (
            f"@{user.username}"
            if user.username
            else "Không có username"
        )

        full_name = (
            user.full_name
            or "Unknown"
        )

        count = await add_violation(
            chat.id,
            user.id,
            username,
            full_name,
        )

        # =================================================
        # LẦN 1-2 -> MUTE
        # =================================================

        if count < MAX_VIOLATIONS:

            try:

                until_time = await mute_user(
                    context.bot,
                    chat.id,
                    user.id,
                    MUTE_MINUTES,
                )

                keyboard = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🔊 BỎ MUTE",
                                callback_data=(
                                    f"unmute:{chat.id}:{user.id}"
                                ),
                            ),
                            InlineKeyboardButton(
                                "❌ HUỶ",
                                callback_data=(
                                    f"cancelmute:{chat.id}:{user.id}"
                                ),
                            ),
                        ]
                    ]
                )

                text = (
                    "🚨 <b>PHÁT HIỆN NỘI DUNG 18+</b>\n\n"
                    f"👤 Thành viên: {user.mention_html()}\n"
                    f"🆔 ID: <code>{user.id}</code>\n"
                    f"⚠️ Vi phạm: <b>{count}/{MAX_VIOLATIONS}</b>\n"
                    f"🤖 AI: <b>{result['score']:.2%}</b>\n\n"
                    f"🔇 Đã mute <b>{MUTE_MINUTES} phút</b>.\n\n"
                    "⚠️ Gửi quá 3 lần sẽ bị ban vĩnh viễn "
                    "khỏi nhóm."
                )

                await context.bot.send_message(
                    chat_id=chat.id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )

            except Exception as e:

                logger.exception(
                    "Lỗi mute user: %s",
                    e,
                )

                await context.bot.send_message(
                    chat_id=chat.id,
                    text=(
                        "🚨 <b>ĐÃ XOÁ NỘI DUNG 18+</b>\n\n"
                        f"👤 {user.mention_html()}\n"
                        f"⚠️ Vi phạm: "
                        f"<b>{count}/{MAX_VIOLATIONS}</b>\n"
                        f"🤖 AI: <b>{result['score']:.2%}</b>"
                    ),
                    parse_mode="HTML",
                )

        # =================================================
        # LẦN 3 -> BAN
        # =================================================

        else:

            try:

                await ban_user(
                    context.bot,
                    chat.id,
                    user.id,
                )

                keyboard = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🔓 UNBAN",
                                callback_data=(
                                    f"unban:{chat.id}:{user.id}"
                                ),
                            ),
                            InlineKeyboardButton(
                                "❌ HUỶ",
                                callback_data=(
                                    f"cancelban:{chat.id}:{user.id}"
                                ),
                            ),
                        ]
                    ]
                )

                text = (
                    "🚨 <b>VI PHẠM LẦN 3</b>\n\n"
                    f"👤 Thành viên: {user.mention_html()}\n"
                    f"🆔 ID: <code>{user.id}</code>\n"
                    f"⚠️ Tổng vi phạm: <b>{count}</b>\n"
                    f"🤖 AI: <b>{result['score']:.2%}</b>\n\n"
                    "🔨 <b>ĐÃ BAN VĨNH VIỄN KHỎI NHÓM</b>."
                )

                await context.bot.send_message(
                    chat_id=chat.id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )

            except Exception as e:

                logger.exception(
                    "Không ban được user: %s",
                    e,
                )

                await context.bot.send_message(
                    chat_id=chat.id,
                    text=(
                        "🚨 <b>PHÁT HIỆN VI PHẠM LẦN 3</b>\n\n"
                        f"👤 {user.mention_html()}\n"
                        f"⚠️ Tổng: <b>{count}</b>\n\n"
                        "❌ Bot không thể ban thành viên.\n"
                        "Hãy kiểm tra quyền admin của bot."
                    ),
                    parse_mode="HTML",
                )

    except Exception as e:

        logger.exception(
            "Lỗi process_photo: %s",
            e,
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
# /VIPHAM
# =========================================================

async def vipham(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    message = update.message
    chat = update.effective_chat
    user = update.effective_user

    if chat.type not in (
        "group",
        "supergroup",
    ):

        return

    if not await is_admin(
        context.bot,
        chat.id,
        user.id,
    ):

        await message.reply_text(
            "❌ Chỉ admin mới sử dụng được."
        )

        return

    # /vipham @username
    if context.args:

        target = await resolve_user(
            context.bot,
            chat.id,
            context.args[0],
        )

        if not target:

            await message.reply_text(
                "❌ Không tìm thấy thành viên."
            )

            return

        count = await get_violation_count(
            chat.id,
            target.id,
        )

        await message.reply_text(
            f"👤 {target.full_name}\n"
            f"🆔 ID: {target.id}\n"
            f"⚠️ Số lần vi phạm: {count}"
        )

        return

    await message.reply_text(
        "Dùng:\n"
        "/vipham @username\n"
        "hoặc\n"
        "/vipham ID"
    )


# =========================================================
# /RESETVIPHAM
# =========================================================

async def resetvipham(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    message = update.message
    chat = update.effective_chat
    user = update.effective_user

    if chat.type not in (
        "group",
        "supergroup",
    ):

        return

    if not await is_admin(
        context.bot,
        chat.id,
        user.id,
    ):

        await message.reply_text(
            "❌ Chỉ admin mới sử dụng được."
        )

        return

    if not context.args:

        await message.reply_text(
            "Dùng:\n"
            "/resetvipham @username\n"
            "hoặc\n"
            "/resetvipham ID"
        )

        return

    target = await resolve_user(
        context.bot,
        chat.id,
        context.args[0],
    )

    if not target:

        await message.reply_text(
            "❌ Không tìm thấy thành viên."
        )

        return

    await reset_violation(
        chat.id,
        target.id,
    )

    await message.reply_text(
        f"✅ Đã reset vi phạm của "
        f"{target.full_name}."
    )


# =========================================================
# /MUTE
# =========================================================

async def mute_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    message = update.message
    chat = update.effective_chat
    user = update.effective_user

    if chat.type not in (
        "group",
        "supergroup",
    ):

        return

    if not await is_admin(
        context.bot,
        chat.id,
        user.id,
    ):

        await message.reply_text(
            "❌ Chỉ admin mới dùng được."
        )

        return

    if not context.args:

        await message.reply_text(
            "Dùng:\n"
            "/mute @username\n"
            "/mute ID\n"
            "/mute @username 10"
        )

        return

    target = await resolve_user(
        context.bot,
        chat.id,
        context.args[0],
    )

    if not target:

        await message.reply_text(
            "❌ Không tìm thấy thành viên."
        )

        return

    minutes = 2

    if len(context.args) >= 2:

        try:

            minutes = max(
                1,
                int(context.args[1]),
            )

        except ValueError:

            pass

    try:

        await mute_user(
            context.bot,
            chat.id,
            target.id,
            minutes,
        )

        await message.reply_text(
            f"🔇 Đã mute "
            f"{target.mention_html()} "
            f"{minutes} phút.",
            parse_mode="HTML",
        )

    except Exception as e:

        await message.reply_text(
            f"❌ Không thể mute: {e}"
        )


# =========================================================
# /UNMUTE
# =========================================================

async def unmute_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    message = update.message
    chat = update.effective_chat
    user = update.effective_user

    if chat.type not in (
        "group",
        "supergroup",
    ):

        return

    if not await is_admin(
        context.bot,
        chat.id,
        user.id,
    ):

        await message.reply_text(
            "❌ Chỉ admin mới dùng được."
        )

        return

    if not context.args:

        await message.reply_text(
            "Dùng:\n"
            "/unmute @username\n"
            "/unmute ID"
        )

        return

    target = await resolve_user(
        context.bot,
        chat.id,
        context.args[0],
    )

    if not target:

        await message.reply_text(
            "❌ Không tìm thấy thành viên."
        )

        return

    try:

        await unmute_user(
            context.bot,
            chat.id,
            target.id,
        )

        await message.reply_text(
            f"🔊 Đã bỏ mute "
            f"{target.mention_html()}.",
            parse_mode="HTML",
        )

    except Exception as e:

        await message.reply_text(
            f"❌ Không thể unmute: {e}"
        )


# =========================================================
# /TB
# =========================================================

async def tb_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    message = update.message
    user = update.effective_user

    if user.id != OWNER_ID:

        await message.reply_text(
            "❌ Chỉ chủ bot mới sử dụng lệnh này."
        )

        return

    if not context.args:

        await message.reply_text(
            "Dùng:\n"
            "/tb nội dung thông báo"
        )

        return

    text = " ".join(
        context.args
    )

    conn = get_db()

    try:

        rows = conn.execute(
            """
            SELECT chat_id
            FROM groups
            WHERE enabled = 1
            """
        ).fetchall()

    finally:

        conn.close()

    success = 0
    failed = 0

    for row in rows:

        chat_id = row["chat_id"]

        try:

            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "📢 <b>THÔNG BÁO</b>\n\n"
                    + text
                ),
                parse_mode="HTML",
            )

            success += 1

        except Exception as e:

            failed += 1

            logger.error(
                "TB thất bại %s: %s",
                chat_id,
                e,
            )

        await asyncio.sleep(0.05)

    await message.reply_text(
        f"✅ Đã gửi: {success} nhóm\n"
        f"❌ Thất bại: {failed} nhóm"
    )


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):

    logger.exception(
        "Telegram error:",
        exc_info=context.error,
    )


# =========================================================
# MAIN
# =========================================================

def main():

    if (
        not BOT_TOKEN
        or BOT_TOKEN == "THAY_BOT_TOKEN_CUA_BAN"
    ):

        raise RuntimeError(
            "Bạn chưa cấu hình BOT_TOKEN."
        )

    init_db()

    logger.info(
        "Đang khởi động NSFW moderation bot..."
    )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(10)
        .build()
    )

    # =====================================================
    # COMMAND
    # =====================================================

    application.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    application.add_handler(
        CommandHandler(
            "capquyen",
            capquyen,
        )
    )

    application.add_handler(
        CommandHandler(
            "tb",
            tb_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "mute",
            mute_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "unmute",
            unmute_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "vipham",
            vipham,
        )
    )

    application.add_handler(
        CommandHandler(
            "resetvipham",
            resetvipham,
        )
    )

    # =====================================================
    # CALLBACK
    # =====================================================

    application.add_handler(
        CallbackQueryHandler(
            callback_handler
        )
    )

    # =====================================================
    # MEDIA FILTER
    #
    # QUAN TRỌNG:
    #
    # PHOTO
    # Sticker.ALL
    # Document.IMAGE
    #
    # KHÔNG CÓ VIDEO
    # KHÔNG CÓ ANIMATION
    # =====================================================

    media_filter = (
        filters.PHOTO
        | filters.Sticker.ALL
        | filters.Document.IMAGE
    )

    application.add_handler(
        MessageHandler(
            media_filter,
            process_photo,
        ),
        group=10,
    )

    application.add_error_handler(
        error_handler
    )

    logger.info(
        "Bot đã chạy."
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
