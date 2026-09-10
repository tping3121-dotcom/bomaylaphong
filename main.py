import os
import re
import html
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

OWNER_ID = int(
    os.getenv("OWNER_ID") or "7449833411"
)

OWNER_USERNAME = os.getenv(
    "OWNER_USERNAME",
    "@echcuto"
)

NSFW_THRESHOLD = float(
    os.getenv("NSFW_THRESHOLD") or "0.75"
)

WARN_USER = os.getenv(
    "WARN_USER",
    "true"
).lower() in (
    "1",
    "true",
    "yes"
)

AUTO_MUTE_MINUTES = int(
    os.getenv("AUTO_MUTE_MINUTES") or "2"
)

# Số lần vi phạm tối đa trước khi ban
MAX_VIOLATIONS = int(
    os.getenv("MAX_VIOLATIONS") or "3"
)

DATA_DIR = Path(
    os.getenv("DATA_DIR", "data")
)

DATA_DIR.mkdir(
    parents=True,
    exist_ok=True
)

DB_PATH = DATA_DIR / "bot.db"

MAX_AI_CONCURRENT = int(
    os.getenv("MAX_AI_CONCURRENT") or "2"
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

    conn.execute(
        "PRAGMA busy_timeout=30000"
    )

    return conn


def init_db():

    conn = db_connect()

    cur = conn.cursor()

    # -----------------------------------------------------
    # GROUPS
    # -----------------------------------------------------

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

    # -----------------------------------------------------
    # MUTES
    # -----------------------------------------------------

    cur.execute("""
        CREATE TABLE IF NOT EXISTS mutes (
            chat_id INTEGER,
            user_id INTEGER,
            until_ts INTEGER,
            PRIMARY KEY(chat_id, user_id)
        )
    """)

    # -----------------------------------------------------
    # VIOLATIONS
    # -----------------------------------------------------

    cur.execute("""
        CREATE TABLE IF NOT EXISTS violations (
            chat_id INTEGER,
            user_id INTEGER,
            count INTEGER DEFAULT 0,
            updated_at TEXT,
            PRIMARY KEY(chat_id, user_id)
        )
    """)

    conn.commit()
    conn.close()

    logger.info(
        "Database initialized"
    )


# =========================================================
# GROUP DATABASE
# =========================================================

def save_group(
    chat_id,
    username,
    title
):

    conn = db_connect()

    conn.execute("""
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
        SELECT
            chat_id,
            username,
            title
        FROM groups
        WHERE enabled = 1
    """).fetchall()

    conn.close()

    return rows


def group_enabled(
    chat_id
):

    conn = db_connect()

    row = conn.execute("""
        SELECT enabled
        FROM groups
        WHERE chat_id = ?
    """, (
        chat_id,
    )).fetchone()

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


def group_was_notified(
    chat_id
):

    conn = db_connect()

    row = conn.execute("""
        SELECT notified
        FROM groups
        WHERE chat_id = ?
    """, (
        chat_id,
    )).fetchone()

    conn.close()

    return bool(
        row and row[0]
    )


def set_group_notified(
    chat_id
):

    conn = db_connect()

    conn.execute("""
        UPDATE groups
        SET notified = 1
        WHERE chat_id = ?
    """, (
        chat_id,
    ))

    conn.commit()
    conn.close()


# =========================================================
# VIOLATION SYSTEM
# =========================================================

def add_violation(
    chat_id,
    user_id
):
    """
    Tăng số lần vi phạm một cách an toàn.

    Dùng transaction IMMEDIATE để tránh trường hợp
    nhiều ảnh được xử lý đồng thời làm sai số lần.
    """

    conn = db_connect()

    try:

        conn.execute(
            "BEGIN IMMEDIATE"
        )

        row = conn.execute("""
            SELECT count
            FROM violations
            WHERE chat_id = ?
            AND user_id = ?
        """, (
            chat_id,
            user_id
        )).fetchone()

        if row:

            count = int(
                row[0]
            ) + 1

            conn.execute("""
                UPDATE violations
                SET
                    count = ?,
                    updated_at = ?
                WHERE chat_id = ?
                AND user_id = ?
            """, (
                count,
                datetime.now(
                    timezone.utc
                ).isoformat(),
                chat_id,
                user_id
            ))

        else:

            count = 1

            conn.execute("""
                INSERT INTO violations
                (
                    chat_id,
                    user_id,
                    count,
                    updated_at
                )
                VALUES (?, ?, ?, ?)
            """, (
                chat_id,
                user_id,
                count,
                datetime.now(
                    timezone.utc
                ).isoformat()
            ))

        conn.commit()

        return count

    except Exception:

        conn.rollback()

        logger.exception(
            "Không thể tăng violation."
        )

        return 0

    finally:

        conn.close()


def get_violation_count(
    chat_id,
    user_id
):

    conn = db_connect()

    row = conn.execute("""
        SELECT count
        FROM violations
        WHERE chat_id = ?
        AND user_id = ?
    """, (
        chat_id,
        user_id
    )).fetchone()

    conn.close()

    if not row:
        return 0

    return int(
        row[0]
    )


def reset_violations(
    chat_id,
    user_id
):

    conn = db_connect()

    conn.execute("""
        DELETE FROM violations
        WHERE chat_id = ?
        AND user_id = ?
    """, (
        chat_id,
        user_id
    ))

    conn.commit()
    conn.close()


# =========================================================
# MUTE DATABASE
# =========================================================

def save_mute(
    chat_id,
    user_id,
    until_ts
):

    conn = db_connect()

    conn.execute("""
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
        self.ai_semaphore = None

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

            if ImageOps is not None:

                image = ImageOps.exif_transpose(
                    image
                )

            image = image.convert(
                "RGB"
            )

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
                    result.get(
                        "label",
                        ""
                    )
                ).lower()

                score = float(
                    result.get(
                        "score",
                        0
                    )
                )

                if label == "nsfw":

                    nsfw_score = max(
                        nsfw_score,
                        score
                    )

            return {
                "status": "ok",
                "is_nsfw": (
                    nsfw_score >= threshold
                ),
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

        if self.ai_semaphore is None:

            self.ai_semaphore = asyncio.Semaphore(
                MAX_AI_CONCURRENT
            )

        async with self.ai_semaphore:

            return await asyncio.to_thread(
                self._check_image_sync,
                image_path,
                threshold
            )


detector = NsfwDetector()


# =========================================================
# OWNER
# =========================================================

def is_owner(
    update
):

    user = update.effective_user

    return bool(
        user
        and user.id == OWNER_ID
    )


# =========================================================
# ADMIN CHECK
# =========================================================

async def is_admin(
    context,
    chat_id,
    user_id
):

    if user_id == OWNER_ID:
        return True

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

        logger.exception(
            "Không kiểm tra được quyền admin."
        )

        return False


# =========================================================
# DURATION
# =========================================================

def parse_duration(
    value
):

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

            return await context.bot.get_chat(
                value
            )

        return await context.bot.get_chat(
            int(value)
        )

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
# MUTE USER
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
            int(
                until_dt.timestamp()
            )
        )

        logger.info(
            "Muted user=%s chat=%s until=%s",
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
# SEND ADMIN MUTE NOTIFICATION
# =========================================================

async def send_mute_admin_notification(
    context,
    chat,
    user,
    violation_count
):

    username = (
        f"@{user.username}"
        if user.username
        else "Không có username"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🔓 MỞ MUTE",
                callback_data=(
                    f"unmute:{chat.id}:{user.id}"
                )
            ),
            InlineKeyboardButton(
                "❌ HUỶ",
                callback_data=(
                    f"cancelmute:{chat.id}:{user.id}"
                )
            )
        ]
    ])

    text = (
        "🔇 <b>THÔNG BÁO MUTE</b>\n\n"
        f"👤 Người dùng: {user.mention_html()}\n"
        f"🆔 ID: <code>{user.id}</code>\n"
        f"👤 Username: {html.escape(username)}\n"
        f"🏷 Nhóm: <b>{html.escape(chat.title or 'Nhóm')}</b>\n"
        f"⚠️ Lần vi phạm: <b>{violation_count}/{MAX_VIOLATIONS}</b>\n"
        f"⏱ Mute: <b>{AUTO_MUTE_MINUTES} phút</b>\n\n"
        "Admin có thể mở mute sớm bằng nút bên dưới."
    )

    try:

        await context.bot.send_message(
            chat_id=chat.id,
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard
        )

    except Exception:

        logger.exception(
            "Không gửi được thông báo mute cho nhóm."
        )

    # Đồng thời báo cho chủ bot
    try:

        await context.bot.send_message(
            chat_id=OWNER_ID,
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard
        )

    except Exception:

        # Owner có thể chưa /start bot
        logger.info(
            "Không thể gửi thông báo mute riêng cho OWNER."
        )


# =========================================================
# BAN USER
# =========================================================

async def ban_user(
    context,
    chat_id,
    user_id
):

    try:

        await context.bot.ban_chat_member(
            chat_id=chat_id,
            user_id=user_id
        )

        logger.info(
            "Permanently banned user=%s chat=%s",
            user_id,
            chat_id
        )

        return True

    except Exception:

        logger.exception(
            "Không thể ban user=%s",
            user_id
        )

        return False


# =========================================================
# SEND ADMIN BAN NOTIFICATION
# =========================================================

async def send_ban_admin_notification(
    context,
    chat,
    user,
    violation_count
):

    username = (
        f"@{user.username}"
        if user.username
        else "Không có username"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🔓 MỞ BAN",
                callback_data=(
                    f"unban:{chat.id}:{user.id}"
                )
            ),
            InlineKeyboardButton(
                "❌ HUỶ",
                callback_data=(
                    f"cancelban:{chat.id}:{user.id}"
                )
            )
        ]
    ])

    text = (
        "🚫 <b>THÔNG BÁO BAN VĨNH VIỄN</b>\n\n"
        f"👤 Người dùng: {user.mention_html()}\n"
        f"🆔 ID: <code>{user.id}</code>\n"
        f"👤 Username: {html.escape(username)}\n"
        f"🏷 Nhóm: <b>{html.escape(chat.title or 'Nhóm')}</b>\n"
        f"⚠️ Lần vi phạm: <b>{violation_count}/{MAX_VIOLATIONS}</b>\n\n"
        "🚫 Người dùng đã bị ban vĩnh viễn vì "
        f"vi phạm đủ {MAX_VIOLATIONS} lần.\n\n"
        "Admin có thể mở ban bằng nút bên dưới."
    )

    try:

        await context.bot.send_message(
            chat_id=chat.id,
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard
        )

    except Exception:

        logger.exception(
            "Không gửi được thông báo ban cho nhóm."
        )

    try:

        await context.bot.send_message(
            chat_id=OWNER_ID,
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard
        )

    except Exception:

        logger.info(
            "Không thể gửi thông báo ban riêng cho OWNER."
        )


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

    if not group_was_notified(
        chat.id
    ):

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
                    callback_data=(
                        f"approve_{chat.id}"
                    )
                ),
                InlineKeyboardButton(
                    "❌ TỪ CHỐI",
                    callback_data=(
                        f"decline_{chat.id}"
                    )
                )
            ]
        ])

        try:

            title_escaped = html.escape(
                chat.title or "Nhóm"
            )

            username_str = (
                f"@{chat.username}"
                if chat.username
                else "Không có"
            )

            await context.bot.send_message(
                chat_id=OWNER_ID,
                text=(
                    "📥 <b>YÊU CẦU CẤP QUYỀN NHÓM</b>\n\n"
                    f"🏷 Tên: {title_escaped}\n"
                    f"🆔 ID: <code>{chat.id}</code>\n"
                    f"🔗 Username: {html.escape(username_str)}"
                ),
                parse_mode="HTML",
                reply_markup=keyboard
            )

            set_group_notified(
                chat.id
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

    if query is None:

        return

    data = query.data or ""

    # -----------------------------------------------------
    # ACCESS REQUEST
    # -----------------------------------------------------

    if data.startswith(
        "approve_"
    ) or data.startswith(
        "decline_"
    ):

        if query.from_user.id != OWNER_ID:

            await query.answer(
                "❌ Bạn không có quyền.",
                show_alert=True
            )

            return

        await query.answer()

        try:

            chat_id = int(
                data.split("_", 1)[1]
            )

        except Exception:

            return

        if data.startswith(
            "approve_"
        ):

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

                pass

            await query.edit_message_text(
                "✅ Đã CẤP QUYỀN nhóm."
            )

        else:

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

        return

    # -----------------------------------------------------
    # MODERATION CALLBACK
    # -----------------------------------------------------

    parts = data.split(":")

    if len(parts) != 3:

        await query.answer(
            "❌ Nút không hợp lệ.",
            show_alert=True
        )

        return

    action = parts[0]

    try:

        chat_id = int(
            parts[1]
        )

        user_id = int(
            parts[2]
        )

    except ValueError:

        await query.answer(
            "❌ Dữ liệu không hợp lệ.",
            show_alert=True
        )

        return

    # -----------------------------------------------------
    # CHECK ADMIN
    # -----------------------------------------------------

    allowed = await is_admin(
        context,
        chat_id,
        query.from_user.id
    )

    if not allowed:

        await query.answer(
            "❌ Chỉ admin nhóm mới được sử dụng nút này.",
            show_alert=True
        )

        return

    await query.answer()

    # -----------------------------------------------------
    # UNMUTE
    # -----------------------------------------------------

    if action == "unmute":

        try:

            await context.bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                permissions=normal_permissions()
            )

            delete_mute(
                chat_id,
                user_id
            )

            await query.edit_message_text(
                f"🔓 Đã MỞ MUTE cho user "
                f"<code>{user_id}</code>.",
                parse_mode="HTML"
            )

        except Exception:

            logger.exception(
                "Callback unmute failed."
            )

            await query.answer(
                "❌ Không thể mở mute. Kiểm tra quyền bot.",
                show_alert=True
            )

        return

    # -----------------------------------------------------
    # CANCEL MUTE
    # -----------------------------------------------------

    if action == "cancelmute":

        try:

            await query.edit_message_reply_markup(
                reply_markup=None
            )

            await query.answer(
                "Đã huỷ thao tác."
            )

        except Exception:

            pass

        return

    # -----------------------------------------------------
    # UNBAN
    # -----------------------------------------------------

    if action == "unban":

        try:

            await context.bot.unban_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                only_if_banned=True
            )

            await query.edit_message_text(
                f"🔓 Đã MỞ BAN cho user "
                f"<code>{user_id}</code>.",
                parse_mode="HTML"
            )

        except Exception:

            logger.exception(
                "Callback unban failed."
            )

            await query.answer(
                "❌ Không thể mở ban. Kiểm tra quyền bot.",
                show_alert=True
            )

        return

    # -----------------------------------------------------
    # CANCEL BAN
    # -----------------------------------------------------

    if action == "cancelban":

        try:

            await query.edit_message_reply_markup(
                reply_markup=None
            )

            await query.answer(
                "Đã huỷ thao tác."
            )

        except Exception:

            pass

        return

    await query.answer(
        "❌ Không rõ thao tác.",
        show_alert=True
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

    chat = await resolve_group(
        context,
        context.args[0]
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

    duration = parse_duration(
        context.args[2]
    )

    if duration is None:

        await update.message.reply_text(
            "❌ Thời gian không hợp lệ.\n\n"
            "10p = 10 phút\n"
            "2h = 2 giờ\n"
            "1n = 1 ngày\n"
            "1t = 30 ngày"
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
            f"✅ Đã mute <code>{user_id}</code> "
            f"trong <b>{html.escape(chat.title or 'Nhóm')}</b> "
            f"thời gian {html.escape(context.args[2])}.",
            parse_mode="HTML"
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
            f"✅ Đã unmute <code>{user_id}</code> "
            f"trong <b>{html.escape(chat.title or 'Nhóm')}</b>.",
            parse_mode="HTML"
        )

    except Exception:

        logger.exception(
            "Unmute failed"
        )

        await update.message.reply_text(
            "❌ Không thể unmute."
        )


# =========================================================
# /VI PHẠM
# =========================================================

async def vipham_command(
    update,
    context
):

    if not is_owner(update):

        return

    if len(context.args) != 2:

        await update.message.reply_text(
            "Dùng:\n"
            "/vipham USER_ID @GROUP"
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

    count = get_violation_count(
        chat.id,
        user_id
    )

    await update.message.reply_text(
        f"⚠️ User <code>{user_id}</code>\n"
        f"🏷 Nhóm: <b>{html.escape(chat.title or 'Nhóm')}</b>\n"
        f"📊 Vi phạm: <b>{count}/{MAX_VIOLATIONS}</b>",
        parse_mode="HTML"
    )


# =========================================================
# /RESETVI PHẠM
# =========================================================

async def resetvipham_command(
    update,
    context
):

    if not is_owner(update):

        return

    if len(context.args) != 2:

        await update.message.reply_text(
            "Dùng:\n"
            "/resetvipham USER_ID @GROUP"
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

    reset_violations(
        chat.id,
        user_id
    )

    await update.message.reply_text(
        f"✅ Đã reset số lần vi phạm của "
        f"<code>{user_id}</code> về 0.",
        parse_mode="HTML"
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
        "Bot tự động kiểm tra ảnh/sticker.\n\n"
        "🔞 Phát hiện nội dung 18+\n"
        "🗑 Tự động xoá\n"
        f"🔇 Mute {AUTO_MUTE_MINUTES} phút\n"
        f"⚠️ {MAX_VIOLATIONS} lần vi phạm = BAN VĨNH VIỄN\n"
        "👮 Bỏ qua admin\n"
        "🎥 Không quét video."
    )


# =========================================================
# PROCESS MEDIA
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

    # -----------------------------------------------------
    # GROUP ACCESS
    # -----------------------------------------------------

    allowed = await check_group_access(
        update,
        context
    )

    if not allowed:

        return

    # -----------------------------------------------------
    # IGNORE ADMIN
    # -----------------------------------------------------

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

    # -----------------------------------------------------
    # GET FILE
    # -----------------------------------------------------

    target_file_id = None

    if message.photo:

        target_file_id = (
            message.photo[-1].file_id
        )

    elif message.sticker:

        # Sticker tĩnh thường có thumbnail
        if message.sticker.thumbnail:

            target_file_id = (
                message.sticker.thumbnail.file_id
            )

        else:

            target_file_id = (
                message.sticker.file_id
            )

    elif message.animation:

        # Không quét video.
        # Animation GIF vẫn có thể lấy thumbnail
        # nếu Telegram cung cấp.
        if message.animation.thumbnail:

            target_file_id = (
                message.animation.thumbnail.file_id
            )

    elif message.document:

        mime = (
            message.document.mime_type or ""
        )

        if mime.startswith(
            "image/"
        ):

            target_file_id = (
                message.document.file_id
            )

        elif message.document.thumbnail:

            target_file_id = (
                message.document.thumbnail.file_id
            )

    if not target_file_id:

        return

    logger.info(
        "📷 Nhận file | chat=%s user=%s message=%s",
        chat.id,
        user.id if user else None,
        message.message_id
    )

    temp_path = None

    try:

        # -------------------------------------------------
        # TEMP FILE
        # -------------------------------------------------

        with tempfile.NamedTemporaryFile(
            suffix=".jpg",
            delete=False
        ) as temp_file:

            temp_path = temp_file.name

        # -------------------------------------------------
        # DOWNLOAD
        # -------------------------------------------------

        logger.info(
            "⬇️ Download ảnh %s",
            message.message_id
        )

        telegram_file = await context.bot.get_file(
            target_file_id
        )

        await telegram_file.download_to_drive(
            temp_path
        )

        # -------------------------------------------------
        # AI SCAN
        # -------------------------------------------------

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

        # -------------------------------------------------
        # NSFW DETECTED
        # -------------------------------------------------

        logger.warning(
            "🔞 PHÁT HIỆN NSFW | chat=%s user=%s "
            "message=%s score=%.3f",
            chat.id,
            user.id if user else None,
            message.message_id,
            result["score"]
        )

        # -------------------------------------------------
        # DELETE MESSAGE
        # -------------------------------------------------

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

        if not user:

            return

        # -------------------------------------------------
        # ADD VIOLATION
        # -------------------------------------------------

        violation_count = add_violation(
            chat.id,
            user.id
        )

        logger.warning(
            "⚠️ USER %s VIOLATION %s/%s",
            user.id,
            violation_count,
            MAX_VIOLATIONS
        )

        # -------------------------------------------------
        # THIRD VIOLATION = PERMANENT BAN
        # -------------------------------------------------

        if violation_count >= MAX_VIOLATIONS:

            ban_success = await ban_user(
                context,
                chat.id,
                user.id
            )

            if ban_success:

                # Thông báo user
                try:

                    await context.bot.send_message(
                        chat_id=chat.id,
                        text=(
                            f"🚫 {user.mention_html()} "
                            "đã bị <b>BAN VĨNH VIỄN</b> khỏi nhóm.\n\n"
                            f"⚠️ Lý do: gửi nội dung 18+ "
                            f"{MAX_VIOLATIONS} lần.\n\n"
                            "📢 Admin đã nhận được thông báo."
                        ),
                        parse_mode="HTML"
                    )

                except Exception:

                    logger.exception(
                        "Không gửi được thông báo ban."
                    )

                # Thông báo admin + nút
                await send_ban_admin_notification(
                    context,
                    chat,
                    user,
                    violation_count
                )

            else:

                try:

                    await context.bot.send_message(
                        chat_id=chat.id,
                        text=(
                            f"⚠️ Không thể ban "
                            f"{user.mention_html()}.\n"
                            "Hãy kiểm tra bot có quyền ban thành viên."
                        ),
                        parse_mode="HTML"
                    )

                except Exception:

                    pass

            return

        # -------------------------------------------------
        # VIOLATION 1 / 2 = MUTE
        # -------------------------------------------------

        duration = timedelta(
            minutes=AUTO_MUTE_MINUTES
        )

        mute_success = await mute_user(
            context,
            chat.id,
            user.id,
            duration
        )

        if mute_success:

            # Cảnh báo trong nhóm
            if WARN_USER:

                try:

                    await context.bot.send_message(
                        chat_id=chat.id,
                        text=(
                            f"⚠️ {user.mention_html()} "
                            "đã gửi nội dung không phù hợp.\n\n"
                            f"📊 Vi phạm: "
                            f"<b>{violation_count}/{MAX_VIOLATIONS}</b>\n"
                            f"🔇 Đã bị mute "
                            f"<b>{AUTO_MUTE_MINUTES} phút</b>.\n\n"
                            "🚨 "
                            "Gửi quá 3 lần sẽ bị ban vĩnh viễn khỏi nhóm."
                        ),
                        parse_mode="HTML"
                    )

                except Exception:

                    logger.exception(
                        "Không gửi được cảnh báo."
                    )

            # Gửi admin
            await send_mute_admin_notification(
                context,
                chat,
                user,
                violation_count
            )

        else:

            try:

                await context.bot.send_message(
                    chat_id=chat.id,
                    text=(
                        f"⚠️ {user.mention_html()} "
                        f"đã vi phạm lần "
                        f"{violation_count}/{MAX_VIOLATIONS}, "
                        "nhưng bot không thể mute.\n"
                        "Kiểm tra quyền Restrict Members của bot."
                    ),
                    parse_mode="HTML"
                )

            except Exception:

                pass

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
        .concurrent_updates(10)
        .build()
    )

    # -----------------------------------------------------
    # COMMANDS
    # -----------------------------------------------------

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

    application.add_handler(
        CommandHandler(
            "vipham",
            vipham_command
        )
    )

    application.add_handler(
        CommandHandler(
            "resetvipham",
            resetvipham_command
        )
    )

    # -----------------------------------------------------
    # CALLBACK BUTTON
    # -----------------------------------------------------

    application.add_handler(
        CallbackQueryHandler(
            button_callback
        )
    )

    # -----------------------------------------------------
    # MEDIA FILTER
    #
    # QUAN TRỌNG:
    # Không dùng filters.STICKER
    #
    # Python-telegram-bot mới:
    # filters.Sticker.ALL
    # -----------------------------------------------------

    media_filter = (
        filters.PHOTO
        | filters.Sticker.ALL
        | filters.ANIMATION
        | filters.Document.IMAGE
    )

    application.add_handler(
        MessageHandler(
            media_filter,
            process_photo
        )
    )

    # -----------------------------------------------------
    # OTHER GROUP MESSAGES
    # -----------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS
            & (~filters.COMMAND)
            & (~media_filter),
            handle_group_messages
        )
    )

    # -----------------------------------------------------
    # ERROR
    # -----------------------------------------------------

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
        "⚠️ MAX violations: %s",
        MAX_VIOLATIONS
    )

    logger.info(
        "🎥 Video scanning: DISABLED"
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":

    main()
