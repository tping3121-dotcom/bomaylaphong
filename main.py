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
from telegram.constants import ChatMemberStatus, ChatType
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)


# =========================================================
# ⚙️ CẤU HÌNH
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "THAY_BOT_TOKEN_CUA_BAN")
OWNER_ID = int(os.getenv("OWNER_ID", "7449833411"))
OWNER_USERNAME = os.getenv("OWNER_USERNAME", "echcuto")

NSFW_MODEL = "Falconsai/nsfw_image_detection"
NSFW_THRESHOLD = float(os.getenv("NSFW_THRESHOLD", "0.70"))
STICKER_NSFW_THRESHOLD = float(os.getenv("STICKER_NSFW_THRESHOLD", "0.55"))

MAX_VIOLATIONS = 3
MUTE_MINUTES = 2
MAX_AI_CONCURRENT = 2
DB_PATH = os.getenv("DB_PATH", "nsfw_bot.db")

# ⏱️ Chống spam cảnh báo chưa cấp quyền (giây)
UNAUTHORIZED_WARN_COOLDOWN = 300  # 5 phút

# 🧠 Cache admin 60 giây
ADMIN_CACHE_TTL = 60


# =========================================================
# 📝 LOG
# =========================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("NSFW_BOT")


# =========================================================
# 💾 DATABASE
# =========================================================

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
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
                username TEXT,
                requested_by INTEGER,
                requested_at TEXT,
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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS known_users (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                full_name TEXT,
                last_seen TEXT,
                PRIMARY KEY(chat_id, user_id)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_known_username
            ON known_users(chat_id, username)
            """
        )
        # Chống spam cảnh báo
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS warn_cooldown (
                chat_id INTEGER PRIMARY KEY,
                last_warn TEXT
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


# =========================================================
# 🔐 GROUP ACCESS
# =========================================================

def group_enabled_sync(chat_id):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT enabled FROM groups WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        return bool(row and row["enabled"])
    finally:
        conn.close()


async def group_enabled(chat_id):
    return await asyncio.to_thread(group_enabled_sync, chat_id)


def request_group_access_sync(chat_id, title, username, user_id):
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO groups(
                chat_id, enabled, title, username, requested_by, requested_at
            )
            VALUES (?, 0, ?, ?, ?, ?)

            ON CONFLICT(chat_id)
            DO UPDATE SET
                title = excluded.title,
                username = excluded.username,
                requested_by = excluded.requested_by,
                requested_at = excluded.requested_at
            """,
            (
                chat_id,
                title,
                username,
                user_id,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


async def request_group_access(chat_id, title, username, user_id):
    await asyncio.to_thread(
        request_group_access_sync, chat_id, title, username, user_id
    )


def approve_group_sync(chat_id):
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO groups(chat_id, enabled, approved_at)
            VALUES (?, 1, ?)

            ON CONFLICT(chat_id)
            DO UPDATE SET
                enabled = 1,
                approved_at = excluded.approved_at
            """,
            (chat_id, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


async def approve_group(chat_id):
    await asyncio.to_thread(approve_group_sync, chat_id)


def disable_group_sync(chat_id):
    conn = get_db()
    try:
        conn.execute(
            "UPDATE groups SET enabled = 0 WHERE chat_id = ?",
            (chat_id,),
        )
        conn.commit()
    finally:
        conn.close()


async def disable_group(chat_id):
    await asyncio.to_thread(disable_group_sync, chat_id)


def get_group_info_sync(chat_id):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM groups WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# =========================================================
# ⏱️ WARN COOLDOWN
# =========================================================

def can_warn_sync(chat_id):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT last_warn FROM warn_cooldown WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        now = datetime.now(timezone.utc)
        if row and row["last_warn"]:
            try:
                last = datetime.fromisoformat(row["last_warn"])
                if (now - last).total_seconds() < UNAUTHORIZED_WARN_COOLDOWN:
                    return False
            except Exception:
                pass

        conn.execute(
            """
            INSERT INTO warn_cooldown(chat_id, last_warn)
            VALUES (?, ?)
            ON CONFLICT(chat_id)
            DO UPDATE SET last_warn = excluded.last_warn
            """,
            (chat_id, now.isoformat()),
        )
        conn.commit()
        return True
    finally:
        conn.close()


async def can_warn(chat_id):
    return await asyncio.to_thread(can_warn_sync, chat_id)


# =========================================================
# 👤 USER DATABASE
# =========================================================

def save_known_user_sync(chat_id, user_id, username, full_name):
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO known_users(
                chat_id, user_id, username, full_name, last_seen
            )
            VALUES (?, ?, ?, ?, ?)

            ON CONFLICT(chat_id, user_id)
            DO UPDATE SET
                username = excluded.username,
                full_name = excluded.full_name,
                last_seen = excluded.last_seen
            """,
            (
                chat_id,
                user_id,
                username,
                full_name,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


async def save_known_user(chat_id, user):
    if not user:
        return
    await asyncio.to_thread(
        save_known_user_sync,
        chat_id,
        user.id,
        user.username or "",
        user.full_name or "",
    )


def find_user_by_username_sync(chat_id, username):
    conn = get_db()
    try:
        username = username.lstrip("@").lower()
        row = conn.execute(
            """
            SELECT user_id FROM known_users
            WHERE chat_id = ? AND LOWER(username) = ?
            LIMIT 1
            """,
            (chat_id, username),
        ).fetchone()
        return int(row["user_id"]) if row else None
    finally:
        conn.close()


async def find_user_by_username(chat_id, username):
    return await asyncio.to_thread(
        find_user_by_username_sync, chat_id, username
    )


# =========================================================
# ⚠️ VIOLATION
# =========================================================

def add_violation_sync(chat_id, user_id, username, full_name):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT count FROM violations WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        ).fetchone()

        now_iso = datetime.now(timezone.utc).isoformat()

        if row:
            count = int(row["count"]) + 1
            conn.execute(
                """
                UPDATE violations
                SET count = ?, username = ?, full_name = ?, last_violation = ?
                WHERE chat_id = ? AND user_id = ?
                """,
                (count, username, full_name, now_iso, chat_id, user_id),
            )
        else:
            count = 1
            conn.execute(
                """
                INSERT INTO violations(
                    chat_id, user_id, username, full_name, count, last_violation
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (chat_id, user_id, username, full_name, count, now_iso),
            )
        conn.commit()
        return count
    finally:
        conn.close()


async def add_violation(chat_id, user_id, username, full_name):
    return await asyncio.to_thread(
        add_violation_sync, chat_id, user_id, username, full_name
    )


def get_violation_count_sync(chat_id, user_id):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT count FROM violations WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        ).fetchone()
        return int(row["count"]) if row else 0
    finally:
        conn.close()


async def get_violation_count(chat_id, user_id):
    return await asyncio.to_thread(get_violation_count_sync, chat_id, user_id)


def reset_violation_sync(chat_id, user_id):
    conn = get_db()
    try:
        conn.execute(
            "DELETE FROM violations WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        )
        conn.commit()
    finally:
        conn.close()


async def reset_violation(chat_id, user_id):
    await asyncio.to_thread(reset_violation_sync, chat_id, user_id)


# =========================================================
# 🔇 MUTE DATABASE
# =========================================================

def save_mute_sync(chat_id, user_id, until_time):
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO muted_users(chat_id, user_id, until_time)
            VALUES (?, ?, ?)
            ON CONFLICT(chat_id, user_id)
            DO UPDATE SET until_time = excluded.until_time
            """,
            (chat_id, user_id, until_time.isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


async def save_mute(chat_id, user_id, until_time):
    await asyncio.to_thread(save_mute_sync, chat_id, user_id, until_time)


def delete_mute_sync(chat_id, user_id):
    conn = get_db()
    try:
        conn.execute(
            "DELETE FROM muted_users WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        )
        conn.commit()
    finally:
        conn.close()


async def delete_mute(chat_id, user_id):
    await asyncio.to_thread(delete_mute_sync, chat_id, user_id)


# =========================================================
# 🤖 NSFW AI
# =========================================================

class NsfwDetector:
    def __init__(self):
        self.pipe = None
        try:
            from transformers import pipeline
            logger.info("🔄 Đang tải model NSFW: %s", NSFW_MODEL)
            self.pipe = pipeline("image-classification", model=NSFW_MODEL)
            logger.info("✅ NSFW AI loaded.")
        except Exception as e:
            logger.exception("❌ Không thể load model: %s", e)

    def prepare_image(self, image_path, is_sticker=False):
        image = Image.open(image_path)
        image = ImageOps.exif_transpose(image)

        if image.mode in ("RGBA", "LA") or "transparency" in image.info:
            rgba = image.convert("RGBA")
            background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            background.alpha_composite(rgba)
            image = background.convert("RGB")
        else:
            image = image.convert("RGB")

        if is_sticker:
            canvas = Image.new("RGB", (512, 512), (255, 255, 255))
            image.thumbnail((480, 480), Image.Resampling.LANCZOS)
            x = (512 - image.width) // 2
            y = (512 - image.height) // 2
            canvas.paste(image, (x, y))
            image = canvas
        else:
            image.thumbnail((512, 512), Image.Resampling.LANCZOS)

        return image

    def predict_sync(self, image_path, is_sticker=False):
        if self.pipe is None:
            return {"is_nsfw": False, "score": 0.0, "label": "detector_offline"}

        try:
            image = self.prepare_image(image_path, is_sticker)
            results = self.pipe(image, top_k=5)

            threshold = (
                STICKER_NSFW_THRESHOLD if is_sticker else NSFW_THRESHOLD
            )

            best_score = 0.0
            best_label = ""

            for result in results:
                label = str(result.get("label", "")).lower().strip()
                score = float(result.get("score", 0))
                if "nsfw" in label and score > best_score:
                    best_score = score
                    best_label = label

            return {
                "is_nsfw": best_score >= threshold,
                "score": best_score,
                "label": best_label or "normal",
            }
        except Exception as e:
            logger.exception("❌ Prediction error: %s", e)
            return {"is_nsfw": False, "score": 0.0, "label": "error"}


detector = NsfwDetector()
ai_semaphore: asyncio.Semaphore | None = None


def get_ai_semaphore():
    global ai_semaphore
    if ai_semaphore is None:
        ai_semaphore = asyncio.Semaphore(MAX_AI_CONCURRENT)
    return ai_semaphore


async def detect_nsfw(image_path, is_sticker=False):
    async with get_ai_semaphore():
        return await asyncio.to_thread(
            detector.predict_sync, image_path, is_sticker
        )


# =========================================================
# 👮 KIỂM TRA ADMIN (có cache)
# =========================================================

_admin_cache: dict[tuple[int, int], tuple[float, bool]] = {}


async def is_admin(bot, chat_id, user_id, use_cache=True):
    now = asyncio.get_event_loop().time()
    key = (chat_id, user_id)

    if use_cache:
        cached = _admin_cache.get(key)
        if cached and now - cached[0] < ADMIN_CACHE_TTL:
            return cached[1]

    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        result = member.status in (
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        )
        _admin_cache[key] = (now, result)
        return result
    except Exception as e:
        logger.error("❌ is_admin chat=%s user=%s: %s", chat_id, user_id, e)
        return False


async def is_bot_admin(bot, chat_id):
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(chat_id=chat_id, user_id=me.id)
        return (
            member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER),
            member,
        )
    except Exception as e:
        logger.error("❌ Không kiểm tra được quyền bot: %s", e)
        return False, None


async def require_group_admin(update, context):
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if not message or not chat:
        return False

    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("❌ Lệnh này chỉ dùng trong nhóm.")
        return False

    if message.sender_chat and message.sender_chat.id == chat.id:
        return True

    if not user:
        await message.reply_text("❌ Không xác định được người dùng.")
        return False

    if user.id == OWNER_ID:
        return True

    if await is_admin(context.bot, chat.id, user.id):
        return True

    bot_admin, _ = await is_bot_admin(context.bot, chat.id)

    if not bot_admin:
        await message.reply_text(
            "⚠️ <b>BOT CHƯA LÀ ADMIN</b>\n\n"
            "Hãy thêm bot làm quản trị viên trong nhóm rồi cấp:\n\n"
            "🗑️ Xoá tin nhắn\n"
            "🔇 Hạn chế thành viên\n"
            "🔨 Cấm thành viên\n\n"
            "Sau đó thử lại.",
            parse_mode="HTML",
        )
        return False

    await message.reply_text(
        "❌ Chỉ quản trị viên nhóm mới dùng được lệnh này. 👮"
    )
    return False


# =========================================================
# 🔐 QUYỀN BOT
# =========================================================

async def botcheck(update, context):
    message = update.effective_message
    chat = update.effective_chat

    if not await require_group_admin(update, context):
        return

    try:
        me = await context.bot.get_me()
        member = await context.bot.get_chat_member(chat.id, me.id)

        if member.status not in (
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        ):
            await message.reply_text("❌ Bot chưa phải admin.")
            return

        delete_ok = getattr(member, "can_delete_messages", False)
        restrict_ok = getattr(member, "can_restrict_members", False)

        text = (
            "🤖 <b>KIỂM TRA QUYỀN BOT</b>\n\n"
            f"👮 Trạng thái: <b>{member.status}</b>\n\n"
            f"🗑️ Xoá tin: {'✅' if delete_ok else '❌'}\n"
            f"🔇 Mute/Bỏ mute: {'✅' if restrict_ok else '❌'}\n"
            f"🔨 Ban/Unban: {'✅' if restrict_ok else '❌'}\n"
        )

        if not delete_ok or not restrict_ok:
            text += "\n⚠️ Hãy cấp cho bot toàn bộ quyền quản trị cần thiết."
        else:
            text += "\n🎉 Bot đã đủ quyền để hoạt động."

        await message.reply_text(text, parse_mode="HTML")

    except Exception as e:
        await message.reply_text(
            f"❌ Không kiểm tra được quyền bot.\n<code>{e}</code>",
            parse_mode="HTML",
        )


# =========================================================
# 🔓 PERMISSIONS
# =========================================================

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


def normal_permissions():
    # Không dùng ChatPermissions.all_permissions() vì có thể không tồn tại
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
    )


async def mute_user(bot, chat_id, user_id, minutes=2):
    until_time = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    await bot.restrict_chat_member(
        chat_id=chat_id,
        user_id=user_id,
        permissions=mute_permissions(),
        until_date=until_time,
    )
    await save_mute(chat_id, user_id, until_time)
    return until_time


async def unmute_user(bot, chat_id, user_id):
    await bot.restrict_chat_member(
        chat_id=chat_id,
        user_id=user_id,
        permissions=normal_permissions(),
    )
    await delete_mute(chat_id, user_id)


async def ban_user(bot, chat_id, user_id):
    await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)


async def unban_user(bot, chat_id, user_id):
    await bot.unban_chat_member(
        chat_id=chat_id, user_id=user_id, only_if_banned=True
    )


# =========================================================
# 👤 RESOLVE USER
# =========================================================

async def resolve_user(update, context):
    message = update.effective_message
    chat = update.effective_chat

    if not message or not chat:
        return None

    # 1️⃣ Reply
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user

    # 2️⃣ Args
    if not context.args:
        return None

    text = context.args[0].strip()

    # ID
    if re.fullmatch(r"-?\d+", text):
        try:
            user_id = int(text)
            member = await context.bot.get_chat_member(chat.id, user_id)
            return member.user
        except Exception:
            return None

    # Username
    if text.startswith("@"):
        user_id = await find_user_by_username(chat.id, text)
        if not user_id:
            return None
        try:
            member = await context.bot.get_chat_member(chat.id, user_id)
            return member.user
        except Exception:
            return None

    return None


# =========================================================
# ▶️ START
# =========================================================

async def start(update, context):
    await update.effective_message.reply_text(
        "🤖 <b>NSFW MODERATION BOT</b>\n\n"
        "🛡️ Tự động kiểm duyệt ảnh/sticker 18+\n\n"
        "🖼️ Quét ảnh\n"
        "🎨 Quét sticker\n"
        "🚫 Không quét video\n\n"
        "🔇 Lần 1-2 → mute 2 phút\n"
        "🔨 Lần 3 → ban vĩnh viễn\n\n"
        "🔐 Nhóm phải được chủ bot cấp quyền.",
        parse_mode="HTML",
    )


# =========================================================
# 🆕 WELCOME - KHI BOT ĐƯỢC THÊM VÀO NHÓM
# =========================================================

async def on_bot_added(update, context):
    """Khi bot được thêm vào nhóm mới."""
    message = update.effective_message
    chat = update.effective_chat

    if not message or not chat:
        return

    # Kiểm tra bot có phải vừa được thêm không
    if not message.new_chat_members:
        return

    me = await context.bot.get_me()
    added = any(m.id == me.id for m in message.new_chat_members)

    if not added:
        return

    logger.info("🆕 Bot được thêm vào nhóm: %s (%s)", chat.title, chat.id)

    # Nếu đã được cấp quyền → chào mừng
    if await group_enabled(chat.id):
        try:
            await message.reply_text(
                "🎉 <b>CẢM ƠN ĐÃ THÊM BOT!</b>\n\n"
                "🛡️ Nhóm đã được cấp quyền từ trước.\n"
                "Bot sẽ tự động kiểm duyệt nội dung 18+.",
                parse_mode="HTML",
            )
        except Exception:
            pass
        return

    # Chưa được cấp quyền → thông báo + gửi yêu cầu admin
    await notify_unauthorized(chat, context, requester=None)


# =========================================================
# 🆕 THÔNG BÁO CHƯA CẤP QUYỀN + GỬI YÊU CẦU ADMIN
# =========================================================

def build_group_info_text(chat, requester=None):
    """Tạo text đầy đủ thông tin nhóm."""
    lines = [
        "🔔 <b>YÊU CẦU CẤP QUYỀN BOT</b>",
        "",
        f"📌 <b>Tên nhóm:</b> {chat.title or 'Không tên'}",
        f"🆔 <b>Chat ID:</b> <code>{chat.id}</code>",
    ]

    if getattr(chat, "username", None):
        lines.append(f"🔗 <b>Username:</b> @{chat.username}")

    if getattr(chat, "invite_link", None):
        lines.append(f"🔗 <b>Invite link:</b> {chat.invite_link}")

    lines.append(f"📊 <b>Loại:</b> {chat.type}")

    if requester:
        lines.append("")
        lines.append(
            f"👤 <b>Người yêu cầu:</b> {requester.full_name}"
        )
        lines.append(f"🆔 <b>User ID:</b> <code>{requester.id}</code>")
        if requester.username:
            lines.append(f"🔗 <b>Username:</b> @{requester.username}")
    else:
        lines.append("")
        lines.append("👤 <b>Người yêu cầu:</b> Bot tự động phát hiện")

    lines.append("")
    lines.append("👇 Chọn hành động bên dưới:")

    return "\n".join(lines)


async def notify_unauthorized(chat, context, requester=None):
    """Spam cảnh báo chưa cấp quyền + gửi yêu cầu đến OWNER."""
    # Chống spam
    if not await can_warn(chat.id):
        logger.info("⏱️ Bỏ qua warn (cooldown): %s", chat.id)
        return

    # Lưu thông tin nhóm vào DB
    await request_group_access(
        chat.id,
        chat.title or "Không tên",
        getattr(chat, "username", "") or "",
        requester.id if requester else 0,
    )

    owner_mention = f"@{OWNER_USERNAME}" if OWNER_USERNAME else "admin"

    # 1️⃣ Spam cảnh báo lên nhóm
    try:
        await context.bot.send_message(
            chat_id=chat.id,
            text=(
                "⚠️ <b>BOT CHƯA ĐƯỢC CẤP QUYỀN SỬ DỤNG</b>\n\n"
                "🚫 Nhóm này chưa được admin cấp quyền sử dụng bot.\n\n"
                f"📩 Vui lòng liên hệ admin: <b>{owner_mention}</b>\n"
                "để được cấp quyền sử dụng.\n\n"
                "⏳ Đã gửi yêu cầu cấp quyền đến admin, "
                "vui lòng chờ duyệt."
            ),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error("❌ Không gửi được cảnh báo nhóm %s: %s", chat.id, e)

    # 2️⃣ Gửi yêu cầu đến OWNER kèm nút
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
            text=build_group_info_text(chat, requester),
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        logger.info("📨 Đã gửi yêu cầu cấp quyền cho OWNER: %s", chat.id)
    except Exception as e:
        logger.error("❌ Không gửi được cho OWNER: %s", e)


# =========================================================
# 🔐 /CAPQUYEN
# =========================================================

async def capquyen(update, context):
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if not message or not chat:
        return

    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("❌ /capquyen chỉ dùng trong nhóm.")
        return

    # Anonymous admin
    if message.sender_chat and message.sender_chat.id == chat.id:
        requester = None
        requester_id = 0
    else:
        if not user:
            return
        requester = user
        requester_id = user.id

        if not await is_admin(context.bot, chat.id, user.id):
            await message.reply_text(
                "❌ <b>CHỈ ADMIN NHÓM</b> mới có thể yêu cầu cấp quyền. 👮",
                parse_mode="HTML",
            )
            return

    # Nếu đã được cấp quyền
    if await group_enabled(chat.id):
        await message.reply_text(
            "✅ Nhóm này đã được cấp quyền sử dụng bot rồi.",
        )
        return

    await request_group_access(
        chat.id,
        chat.title or "Không tên",
        getattr(chat, "username", "") or "",
        requester_id,
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
            text=build_group_info_text(chat, requester),
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    except Exception as e:
        logger.error("❌ Không gửi được cho OWNER: %s", e)
        await message.reply_text(
            "❌ Không gửi được yêu cầu tới chủ bot.\n\n"
            "⚠️ Hãy kiểm tra OWNER_ID trong code."
        )
        return

    await message.reply_text(
        "📨 <b>ĐÃ GỬI YÊU CẦU</b>\n\n"
        "⏳ Vui lòng chờ chủ bot duyệt.\n"
        "🔐 Bot sẽ chỉ hoạt động sau khi được cấp quyền.",
        parse_mode="HTML",
    )


# =========================================================
# 🔘 CALLBACK
# =========================================================

async def callback_handler(update, context):
    query = update.callback_query

    if not query or not query.from_user:
        return

    data = query.data or ""

    # =====================================================
    # APPROVE
    # =====================================================
    if data.startswith("approve:"):
        if query.from_user.id != OWNER_ID:
            await query.answer("❌ Bạn không có quyền.", show_alert=True)
            return

        try:
            chat_id = int(data.split(":", 1)[1])
        except Exception:
            await query.answer("❌ Dữ liệu lỗi.")
            return

        await approve_group(chat_id)
        await query.answer("✅ Đã cấp quyền.")

        try:
            await query.edit_message_text(
                "✅ <b>ĐÃ CẤP QUYỀN</b>\n\n"
                f"🆔 Chat ID: <code>{chat_id}</code>",
                parse_mode="HTML",
            )
        except Exception:
            pass

        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "🎉 <b>BOT ĐÃ ĐƯỢC CẤP QUYỀN!</b>\n\n"
                    "🛡️ Hệ thống kiểm duyệt đã bật.\n\n"
                    "🖼️ Ảnh → quét\n"
                    "🎨 Sticker → quét\n"
                    "🚫 Video → bỏ qua\n\n"
                    "🔇 Vi phạm 1-2 → mute 2 phút\n"
                    "🔨 Vi phạm 3 → ban vĩnh viễn"
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass
        return

    # =====================================================
    # DENY
    # =====================================================
    if data.startswith("deny:"):
        if query.from_user.id != OWNER_ID:
            await query.answer("❌ Bạn không có quyền.", show_alert=True)
            return

        try:
            chat_id = int(data.split(":", 1)[1])
        except Exception:
            await query.answer("❌ Dữ liệu lỗi.")
            return

        await disable_group(chat_id)
        await query.answer("❌ Đã từ chối.")

        try:
            await query.edit_message_text(
                "❌ <b>ĐÃ TỪ CHỐI CẤP QUYỀN</b>\n\n"
                f"🆔 Chat ID: <code>{chat_id}</code>",
                parse_mode="HTML",
            )
        except Exception:
            pass

        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "❌ <b>YÊU CẦU CẤP QUYỀN ĐÃ BỊ TỪ CHỐI</b>\n\n"
                    "Bot sẽ không hoạt động trong nhóm này.\n"
                    f"Liên hệ admin nếu cần hỗ trợ."
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass
        return

    # =====================================================
    # UNMUTE
    # =====================================================
    if data.startswith("unmute:"):
        parts = data.split(":")
        if len(parts) != 3:
            await query.answer("❌ Dữ liệu lỗi.")
            return

        chat_id, user_id = int(parts[1]), int(parts[2])

        if not await is_admin(context.bot, chat_id, query.from_user.id):
            await query.answer("❌ Chỉ admin nhóm.", show_alert=True)
            return

        try:
            await unmute_user(context.bot, chat_id, user_id)
            await query.answer("🔊 Đã bỏ mute.")
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
        except Exception as e:
            await query.answer(f"❌ Lỗi: {e}", show_alert=True)
        return

    # =====================================================
    # CANCEL MUTE
    # =====================================================
    if data.startswith("cancelmute:"):
        parts = data.split(":")
        if len(parts) != 3:
            await query.answer("❌ Dữ liệu lỗi.")
            return

        chat_id = int(parts[1])

        if not await is_admin(context.bot, chat_id, query.from_user.id):
            await query.answer("❌ Chỉ admin nhóm.", show_alert=True)
            return

        await query.answer("❌ Đã huỷ.")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    # =====================================================
    # UNBAN
    # =====================================================
    if data.startswith("unban:"):
        parts = data.split(":")
        if len(parts) != 3:
            await query.answer("❌ Dữ liệu lỗi.")
            return

        chat_id, user_id = int(parts[1]), int(parts[2])

        if not await is_admin(context.bot, chat_id, query.from_user.id):
            await query.answer("❌ Chỉ admin nhóm.", show_alert=True)
            return

        try:
            await unban_user(context.bot, chat_id, user_id)
            await query.answer("🔓 Đã unban.")
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
        except Exception as e:
            await query.answer(f"❌ Lỗi: {e}", show_alert=True)
        return

    # =====================================================
    # CANCEL BAN
    # =====================================================
    if data.startswith("cancelban:"):
        parts = data.split(":")
        if len(parts) != 3:
            await query.answer("❌ Dữ liệu lỗi.")
            return

        chat_id = int(parts[1])

        if not await is_admin(context.bot, chat_id, query.from_user.id):
            await query.answer("❌ Chỉ admin nhóm.", show_alert=True)
            return

        await query.answer("❌ Đã huỷ.")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return


# =========================================================
# 🔍 IGNORE MESSAGE
# =========================================================

async def should_ignore_message(update, context):
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if not message or not chat:
        return True

    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return True

    if user and user.is_bot:
        return True

    if message.sender_chat and message.sender_chat.id == chat.id:
        return True

    if user:
        if await is_admin(context.bot, chat.id, user.id):
            return True
        await save_known_user(chat.id, user)

    # 🔐 Chưa được cấp quyền → không xử lý ảnh
    if not await group_enabled(chat.id):
        return True

    return False


# =========================================================
# 🖼️ GET MEDIA
# =========================================================

def get_media_file_id(message):
    if message.photo:
        return message.photo[-1].file_id, "photo", ".jpg"

    if message.sticker:
        sticker = message.sticker
        if sticker.is_animated or sticker.is_video:
            if sticker.thumbnail:
                return sticker.thumbnail.file_id, "sticker", ".jpg"
            return None, "sticker_no_thumbnail", None
        return sticker.file_id, "sticker", ".webp"

    if message.document:
        mime = (message.document.mime_type or "").lower()
        if mime.startswith("image/"):
            return message.document.file_id, "image_document", ".jpg"

    return None, None, None


# =========================================================
# 🔞 PROCESS MEDIA
# =========================================================

async def process_photo(update, context):
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if not message or not chat or not user:
        return

    if await should_ignore_message(update, context):
        return

    file_id, media_type, extension = get_media_file_id(message)

    if not file_id:
        return

    is_sticker = media_type == "sticker"
    temp_path = None

    try:
        logger.info("🔍 Scan %s | chat=%s | user=%s", media_type, chat.id, user.id)

        with tempfile.NamedTemporaryFile(suffix=extension, delete=False) as tmp:
            temp_path = tmp.name

        telegram_file = await context.bot.get_file(file_id)
        await telegram_file.download_to_drive(custom_path=temp_path)

        result = await detect_nsfw(temp_path, is_sticker)

        logger.info(
            "🤖 AI | %s | %.4f | %s",
            result["label"],
            result["score"],
            result["is_nsfw"],
        )

        if not result["is_nsfw"]:
            return

        # 🗑️ Xoá tin nhắn
        try:
            await message.delete()
        except Exception as e:
            logger.error("❌ Delete failed: %s", e)

        username = f"@{user.username}" if user.username else "Không có username"
        full_name = user.full_name or "Unknown"

        count = await add_violation(chat.id, user.id, username, full_name)

        # =================================================
        # 🔇 VI PHẠM 1-2
        # =================================================
        if count < MAX_VIOLATIONS:
            try:
                await mute_user(context.bot, chat.id, user.id, MUTE_MINUTES)

                keyboard = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🔊 BỎ MUTE",
                                callback_data=f"unmute:{chat.id}:{user.id}",
                            ),
                            InlineKeyboardButton(
                                "❌ HUỶ",
                                callback_data=f"cancelmute:{chat.id}:{user.id}",
                            ),
                        ]
                    ]
                )

                await context.bot.send_message(
                    chat_id=chat.id,
                    text=(
                        "🚨 <b>PHÁT HIỆN NỘI DUNG 18+</b>\n\n"
                        f"👤 Thành viên: {user.mention_html()}\n"
                        f"🆔 ID: <code>{user.id}</code>\n"
                        f"⚠️ Vi phạm: <b>{count}/{MAX_VIOLATIONS}</b>\n"
                        f"🤖 AI: <b>{result['score']:.2%}</b>\n\n"
                        f"🔇 Đã mute <b>{MUTE_MINUTES} phút</b>.\n\n"
                        "⚠️ Gửi quá 3 lần sẽ bị ban vĩnh viễn khỏi nhóm."
                    ),
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
            except Exception as e:
                logger.exception("❌ Mute error: %s", e)
                await context.bot.send_message(
                    chat_id=chat.id,
                    text=(
                        "🚨 <b>ĐÃ XOÁ NỘI DUNG 18+</b>\n\n"
                        f"👤 {user.mention_html()}\n"
                        f"⚠️ Vi phạm: <b>{count}/3</b>\n"
                        f"🤖 AI: <b>{result['score']:.2%}</b>"
                    ),
                    parse_mode="HTML",
                )

        # =================================================
        # 🔨 VI PHẠM LẦN 3
        # =================================================
        else:
            try:
                await ban_user(context.bot, chat.id, user.id)

                keyboard = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🔓 UNBAN",
                                callback_data=f"unban:{chat.id}:{user.id}",
                            ),
                            InlineKeyboardButton(
                                "❌ HUỶ",
                                callback_data=f"cancelban:{chat.id}:{user.id}",
                            ),
                        ]
                    ]
                )

                await context.bot.send_message(
                    chat_id=chat.id,
                    text=(
                        "🚨 <b>VI PHẠM LẦN 3</b>\n\n"
                        f"👤 Thành viên: {user.mention_html()}\n"
                        f"🆔 ID: <code>{user.id}</code>\n"
                        f"⚠️ Tổng vi phạm: <b>{count}</b>\n"
                        f"🤖 AI: <b>{result['score']:.2%}</b>\n\n"
                        "🔨 <b>ĐÃ BAN VĨNH VIỄN</b>\n"
                        "🚫 Thành viên đã bị loại khỏi nhóm."
                    ),
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
            except Exception as e:
                logger.exception("❌ Ban failed: %s", e)
                await context.bot.send_message(
                    chat_id=chat.id,
                    text=(
                        "🚨 <b>PHÁT HIỆN VI PHẠM LẦN 3</b>\n\n"
                        f"👤 {user.mention_html()}\n"
                        f"⚠️ Tổng: <b>{count}</b>\n\n"
                        "❌ Bot không thể ban thành viên.\n"
                        "⚠️ Kiểm tra quyền Ban Users."
                    ),
                    parse_mode="HTML",
                )

    except Exception as e:
        logger.exception("❌ process_photo: %s", e)
    finally:
        if temp_path:
            try:
                Path(temp_path).unlink(missing_ok=True)
            except Exception:
                pass


# =========================================================
# ⚠️ /VIPHAM
# =========================================================

async def vipham(update, context):
    message = update.effective_message
    chat = update.effective_chat

    if not await require_group_admin(update, context):
        return

    target = await resolve_user(update, context)

    if not target:
        await message.reply_text(
            "⚠️ Không tìm thấy người dùng.\n\n"
            "💡 Cách dùng:\n"
            "• Reply vào tin nhắn người đó + /vipham\n"
            "• /vipham ID\n"
            "• /vipham @username\n\n"
            "📌 Với @username, bot phải từng nhìn thấy người đó."
        )
        return

    count = await get_violation_count(chat.id, target.id)

    await message.reply_text(
        "👤 <b>THÔNG TIN VI PHẠM</b>\n\n"
        f"👤 {target.mention_html()}\n"
        f"🆔 <code>{target.id}</code>\n"
        f"⚠️ Vi phạm: <b>{count}</b>\n"
        f"📊 Giới hạn: <b>{MAX_VIOLATIONS}</b>",
        parse_mode="HTML",
    )


# =========================================================
# 🔄 /RESETVIPHAM
# =========================================================

async def resetvipham(update, context):
    message = update.effective_message
    chat = update.effective_chat

    if not await require_group_admin(update, context):
        return

    target = await resolve_user(update, context)

    if not target:
        await message.reply_text(
            "⚠️ Hãy reply tin nhắn người cần reset hoặc dùng:\n\n"
            "/resetvipham ID\n"
            "/resetvipham @username"
        )
        return

    await reset_violation(chat.id, target.id)

    await message.reply_text(
        "🔄 <b>ĐÃ RESET VI PHẠM</b>\n\n"
        f"👤 {target.mention_html()}\n"
        "⚠️ Số lần vi phạm: <b>0</b>",
        parse_mode="HTML",
    )


# =========================================================
# 🔇 /MUTE
# =========================================================

async def mute_command(update, context):
    message = update.effective_message
    chat = update.effective_chat

    if not await require_group_admin(update, context):
        return

    target = await resolve_user(update, context)

    if not target:
        await message.reply_text(
            "⚠️ Hãy reply người cần mute hoặc dùng:\n\n"
            "/mute ID\n"
            "/mute @username\n"
            "/mute ID 10\n\n"
            "⏱️ Mặc định: 2 phút."
        )
        return

    minutes = MUTE_MINUTES

    # Nếu có args và arg cuối là số → đó là số phút
    if context.args and re.fullmatch(r"\d+", context.args[-1]):
        try:
            minutes = max(1, int(context.args[-1]))
        except Exception:
            pass

    try:
        await mute_user(context.bot, chat.id, target.id, minutes)
        await message.reply_text(
            "🔇 <b>ĐÃ MUTE</b>\n\n"
            f"👤 {target.mention_html()}\n"
            f"⏱️ Thời gian: <b>{minutes} phút</b>",
            parse_mode="HTML",
        )
    except Exception as e:
        await message.reply_text(
            f"❌ <b>Không thể mute.</b>\n\n<code>{e}</code>\n\n"
            "⚠️ Hãy kiểm tra quyền Restrict Members của bot.",
            parse_mode="HTML",
        )


# =========================================================
# 🔊 /UNMUTE
# =========================================================

async def unmute_command(update, context):
    message = update.effective_message
    chat = update.effective_chat

    if not await require_group_admin(update, context):
        return

    target = await resolve_user(update, context)

    if not target:
        await message.reply_text(
            "⚠️ Hãy reply người cần unmute hoặc dùng:\n\n"
            "/unmute ID\n"
            "/unmute @username"
        )
        return

    try:
        await unmute_user(context.bot, chat.id, target.id)
        await message.reply_text(
            f"🔊 <b>ĐÃ BỎ MUTE</b>\n\n👤 {target.mention_html()}",
            parse_mode="HTML",
        )
    except Exception as e:
        await message.reply_text(
            f"❌ <b>Không thể unmute.</b>\n\n<code>{e}</code>",
            parse_mode="HTML",
        )


# =========================================================
# 📢 /TB
# =========================================================

async def tb_command(update, context):
    message = update.effective_message
    user = update.effective_user

    if not user or user.id != OWNER_ID:
        await message.reply_text("❌ Chỉ chủ bot mới dùng được.")
        return

    if not context.args:
        await message.reply_text("📢 Dùng:\n/tb Nội dung thông báo")
        return

    text = " ".join(context.args)

    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT chat_id FROM groups WHERE enabled = 1"
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
                text=f"📢 <b>THÔNG BÁO</b>\n\n{text}",
                parse_mode="HTML",
            )
            success += 1
        except Exception as e:
            failed += 1
            logger.error("TB failed %s: %s", chat_id, e)

        await asyncio.sleep(0.05)

    await message.reply_text(
        "📊 <b>KẾT QUẢ GỬI THÔNG BÁO</b>\n\n"
        f"✅ Thành công: <b>{success}</b>\n"
        f"❌ Thất bại: <b>{failed}</b>",
        parse_mode="HTML",
    )


# =========================================================
# ℹ️ /TRANGTHAI
# =========================================================

async def trangthai(update, context):
    message = update.effective_message
    chat = update.effective_chat

    if not await require_group_admin(update, context):
        return

    enabled = await group_enabled(chat.id)

    status = "🟢 ĐANG HOẠT ĐỘNG" if enabled else "🔴 CHƯA ĐƯỢC CẤP QUYỀN"

    await message.reply_text(
        "🛡️ <b>TRẠNG THÁI BOT</b>\n\n"
        f"📌 Nhóm: <b>{chat.title}</b>\n"
        f"🔐 Cấp quyền: <b>{status}</b>\n\n"
        "🖼️ Ảnh: ✅\n"
        "🎨 Sticker: ✅\n"
        "🎬 Video: ❌ Không quét\n"
        "🔞 NSFW: ✅\n"
        "🔇 Mute: 2 phút\n"
        "🔨 Lần 3: Ban",
        parse_mode="HTML",
    )


# =========================================================
# ❌ ERROR
# =========================================================

async def error_handler(update, context):
    logger.exception("Telegram error:", exc_info=context.error)


# =========================================================
# 🚀 MAIN
# =========================================================

def main():
    if not BOT_TOKEN or BOT_TOKEN == "THAY_BOT_TOKEN_CUA_BAN":
        raise RuntimeError("❌ Bạn chưa cấu hình BOT_TOKEN.")

    init_db()

    logger.info("🚀 Starting NSFW moderation bot...")

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(10)
        .build()
    )

    # COMMANDS
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("capquyen", capquyen))
    application.add_handler(CommandHandler("botcheck", botcheck))
    application.add_handler(CommandHandler("trangthai", trangthai))
    application.add_handler(CommandHandler("tb", tb_command))
    application.add_handler(CommandHandler("mute", mute_command))
    application.add_handler(CommandHandler("unmute", unmute_command))
    application.add_handler(CommandHandler("vipham", vipham))
    application.add_handler(CommandHandler("resetvipham", resetvipham))

    # CALLBACK
    application.add_handler(CallbackQueryHandler(callback_handler))

    # 🆕 Bắt sự kiện bot được thêm vào nhóm
    application.add_handler(
        MessageHandler(
            filters.StatusUpdate.NEW_CHAT_MEMBERS,
            on_bot_added,
        ),
        group=-1,
    )

    # MEDIA
    media_filter = (
        filters.PHOTO
        | filters.Sticker.ALL
        | filters.Document.IMAGE
    )
    application.add_handler(
        MessageHandler(media_filter, process_photo),
        group=10,
    )

    application.add_error_handler(error_handler)

    logger.info("✅ Bot đã sẵn sàng.")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


# =========================================================
# ▶️ RUN
# =========================================================

if __name__ == "__main__":
    main()
