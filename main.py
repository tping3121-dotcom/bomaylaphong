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

UNAUTHORIZED_WARN_COOLDOWN = 300
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
# 🆕 GROUP LOOKUP BY USERNAME
# =========================================================

def find_group_by_username_sync(username):
    conn = get_db()
    try:
        username = username.lstrip("@").lower()
        row = conn.execute(
            """
            SELECT chat_id, title, username, enabled
            FROM groups
            WHERE LOWER(username) = ?
            LIMIT 1
            """,
            (username,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


async def find_group_by_username(username):
    return await asyncio.to_thread(find_group_by_username_sync, username)


def find_group_by_chat_id_sync(chat_id):
    conn = get_db()
    try:
        row = conn.execute(
            """
            SELECT chat_id, title, username, enabled
            FROM groups
            WHERE chat_id = ?
            LIMIT 1
            """,
            (chat_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


async def find_group_by_chat_id(chat_id):
    return await asyncio.to_thread(find_group_by_chat_id_sync, chat_id)


def list_all_groups_sync(limit=50):
    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT chat_id, title, username, enabled
            FROM groups
            ORDER BY requested_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


async def list_all_groups(limit=50):
    return await asyncio.to_thread(list_all_groups_sync, limit)


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
ai_semaphore = None


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

_admin_cache = {}


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


# =========================================================
# 👮 BỘ GIẢI MÃ TARGET CHAT DÙNG CHO CẢ NHÓM LẪN CHAT RIÊNG
# =========================================================

async def resolve_target_chat(update, context):
    """
    Xác định chat_id cần thao tác:
    - Nếu ở trong nhóm: Trả về chính nhóm đó.
    - Nếu ở chat riêng: Yêu cầu Admin phải nhập chat_id ở đầu tham số.
    """
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if not message or not chat or not user:
        return None, False

    # 1. Trường hợp gửi trực tiếp trong nhóm
    if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        if message.sender_chat and message.sender_chat.id == chat.id:
            return chat.id, True

        if user.id == OWNER_ID or await is_admin(context.bot, chat.id, user.id):
            return chat.id, True
        else:
            await message.reply_text("❌ Chỉ quản trị viên mới dùng được lệnh này. 👮")
            return None, False

    # 2. Trường hợp gửi trong khung chat riêng với Bot (Private)
    if chat.type == ChatType.PRIVATE:
        if not context.args:
            await message.reply_text(
                "💡 <b>HƯỚNG DẪN DÙNG TRONG CHAT RIÊNG</b>\n\n"
                "Khi dùng trong chat riêng với Bot, bạn vui lòng nhập thêm <b>Chat ID của nhóm</b> vào đầu lệnh:\n\n"
                "• <code>/mute &lt;chat_id&gt; &lt;user_id/@username&gt; [phút]</code>\n"
                "• <code>/unmute &lt;chat_id&gt; &lt;user_id/@username&gt;</code>\n"
                "• <code>/vipham &lt;chat_id&gt; &lt;user_id/@username&gt;</code>\n"
                "• <code>/resetvipham &lt;chat_id&gt; &lt;user_id/@username&gt;</code>\n\n"
                "<i>Ví dụ:</i> <code>/mute -1002415030100 @username 10</code>",
                parse_mode="HTML"
            )
            return None, False

        target_chat_arg = context.args[0]
        if not re.fullmatch(r"-?\d+", target_chat_arg):
            await message.reply_text("❌ Chat ID nhóm không hợp lệ (Chat ID là dãy số, thường bắt đầu bằng -100).")
            return None, False

        target_chat_id = int(target_chat_arg)

        # Check xem user có phải Admin nhóm đó hoặc OWNER không
        if user.id != OWNER_ID and not await is_admin(context.bot, target_chat_id, user.id):
            await message.reply_text("❌ Bạn không phải là Admin của nhóm này.")
            return None, False

        # Loại bỏ chat_id khỏi args để các hàm resolve user tiếp theo không bị lệch tham số
        context.args.pop(0)
        return target_chat_id, True

    return None, False


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

async def resolve_user(update, context, target_chat_id=None):
    message = update.effective_message
    chat = update.effective_chat
    
    # Nếu không truyền target_chat_id thì lấy chat_id hiện tại
    chat_id = target_chat_id if target_chat_id else chat.id

    if not message:
        return None

    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user

    if not context.args:
        return None

    text = context.args[0].strip()

    if re.fullmatch(r"-?\d+", text):
        try:
            user_id = int(text)
            member = await context.bot.get_chat_member(chat_id, user_id)
            return member.user
        except Exception:
            return None

    if text.startswith("@"):
        user_id = await find_user_by_username(chat_id, text)
        if not user_id:
            return None
        try:
            member = await context.bot.get_chat_member(chat_id, user_id)
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
# 🆕 BUILD GROUP INFO TEXT
# =========================================================

def build_group_info_text(chat, requester=None):
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
        lines.append(f"👤 <b>Người yêu cầu:</b> {requester.full_name}")
        lines.append(f"🆔 <b>User ID:</b> <code>{requester.id}</code>")
        if requester.username:
            lines.append(f"🔗 <b>Username:</b> @{requester.username}")
    else:
        lines.append("")
        lines.append("👤 <b>Người yêu cầu:</b> Bot tự động phát hiện")

    lines.append("")
    lines.append("👇 Chọn hành động bên dưới:")

    return "\n".join(lines)


# =========================================================
# 🆕 SPAM 3 TIN NHẮN CẢNH BÁO CHƯA CẤP QUYỀN
# =========================================================

async def notify_unauthorized(chat, context, requester=None):
    if not await can_warn(chat.id):
        logger.info("⏱️ Bỏ qua warn (cooldown): %s", chat.id)
        return

    await request_group_access(
        chat.id,
        chat.title or "Không tên",
        getattr(chat, "username", "") or "",
        requester.id if requester else 0,
    )

    owner_mention = f"@{OWNER_USERNAME}" if OWNER_USERNAME else "admin"

    spam_messages = [
        (
            "⚠️ <b>THÔNG BÁO QUAN TRỌNG</b> ⚠️\n\n"
            "🚫 <b>BOT CHƯA ĐƯỢC CẤP QUYỀN SỬ DỤNG</b>\n\n"
            "Nhóm này chưa được admin cấp quyền sử dụng bot.\n"
            "Bot sẽ <b>KHÔNG HOẠT ĐỘNG</b> cho đến khi được duyệt."
        ),
        (
            "📩 <b>LIÊN HỆ ADMIN ĐỂ ĐƯỢC CẤP QUYỀN</b>\n\n"
            f"👤 Admin: <b>{owner_mention}</b>\n\n"
            "Vui lòng liên hệ admin để được cấp quyền sử dụng bot.\n"
            "Hoặc admin nhóm có thể dùng lệnh /capquyen trong nhóm này."
        ),
        (
            "⏳ <b>ĐÃ GỬI YÊU CẦU ĐẾN ADMIN</b>\n\n"
            f"📨 Yêu cầu cấp quyền đã được gửi tới: <b>{owner_mention}</b>\n\n"
            "Vui lòng chờ admin duyệt.\n"
            "🔐 Bot sẽ tự động hoạt động sau khi được cấp quyền."
        ),
    ]

    for idx, msg_text in enumerate(spam_messages, 1):
        try:
            await context.bot.send_message(
                chat_id=chat.id,
                text=msg_text,
                parse_mode="HTML",
            )
            logger.info("📤 Spam %d/3 → %s", idx, chat.id)
            await asyncio.sleep(1)
        except Exception as e:
            logger.error("❌ Không spam được tin %d tới %s: %s", idx, chat.id, e)
            break

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
            ],
            [
                InlineKeyboardButton(
                    "📋 XEM TẤT CẢ NHÓM CHỜ DUYỆT",
                    callback_data="pending_list",
                ),
            ],
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
# 🆕 HANDLE BOT JOINED
# =========================================================

async def handle_bot_joined(chat, context):
    if await group_enabled(chat.id):
        try:
            await context.bot.send_message(
                chat_id=chat.id,
                text=(
                    "🎉 <b>CẢM ƠN ĐÃ THÊM BOT!</b>\n\n"
                    "🛡️ Nhóm đã được cấp quyền từ trước.\n"
                    "Bot sẽ tự động kiểm duyệt nội dung 18+."
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass
        return

    await notify_unauthorized(chat, context, requester=None)


# =========================================================
# 🆕 ON BOT ADDED (NEW_CHAT_MEMBERS)
# =========================================================

async def on_bot_added(update, context):
    message = update.effective_message
    chat = update.effective_chat

    if not message or not chat:
        return

    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    if not message.new_chat_members:
        return

    try:
        me = await context.bot.get_me()
    except Exception:
        return

    added = any(m.id == me.id for m in message.new_chat_members)

    if not added:
        return

    logger.info("🆕 [NEW_CHAT_MEMBERS] Bot vào nhóm: %s (%s)", chat.title, chat.id)
    await handle_bot_joined(chat, context)


# =========================================================
# 🆕 FALLBACK: Bắt mọi tin nhắn trong nhóm chưa cấp quyền
# =========================================================

async def fallback_group_detect(update, context):
    message = update.effective_message
    chat = update.effective_chat

    if not message or not chat:
        return

    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    if await group_enabled(chat.id):
        return

    if message.from_user and message.from_user.is_bot:
        return

    if message.text and message.text.startswith(
        ("/capquyen", "/start", "/pending", "/groups", "/admin", "/cap", "/uncap", "/grant")
    ):
        return

    try:
        me = await context.bot.get_me()
        member = await context.bot.get_chat_member(chat.id, me.id)
        if member.status not in (
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.RESTRICTED,
        ):
            return
    except Exception:
        return

    await notify_unauthorized(chat, context, requester=None)


# =========================================================
# 🔐 /CAPQUYEN (trong nhóm)
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
# 🆕 /CAP @username | /cap <chat_id> (OWNER - cấp quyền trực tiếp)
# =========================================================

async def cap_command(update, context):
    message = update.effective_message
    user = update.effective_user

    if not user or user.id != OWNER_ID:
        await message.reply_text("❌ Chỉ chủ bot mới dùng được lệnh này.")
        return

    if not context.args:
        await message.reply_text(
            "📖 <b>HƯỚNG DẪN DÙNG /cap</b>\n\n"
            "• <code>/cap @tennhom</code> — cấp quyền cho nhóm có username\n"
            "• <code>/cap -1001234567890</code> — cấp quyền cho nhóm theo chat_id\n"
            "• <code>/cap</code> — xem hướng dẫn này\n\n"
            "💡 Danh sách nhóm: /pending hoặc /groups",
            parse_mode="HTML",
        )
        return

    arg = context.args[0].strip()

    target_chat_id = None
    target_info = None

    if re.fullmatch(r"-?\d+", arg):
        try:
            target_chat_id = int(arg)
            target_info = await find_group_by_chat_id(target_chat_id)
        except Exception:
            await message.reply_text("❌ Chat ID không hợp lệ.")
            return

    elif arg.startswith("@"):
        target_info = await find_group_by_username(arg)
        if target_info:
            target_chat_id = target_info["chat_id"]
        else:
            try:
                uname = arg.lstrip("@")
                chat_obj = await context.bot.get_chat(f"@{uname}")
                target_chat_id = chat_obj.id
                target_info = {
                    "chat_id": chat_obj.id,
                    "title": chat_obj.title or "Không tên",
                    "username": chat_obj.username or "",
                    "enabled": 0,
                }
                await request_group_access(
                    target_chat_id,
                    target_info["title"],
                    target_info["username"],
                    0,
                )
            except Exception as e:
                await message.reply_text(
                    f"❌ Không tìm thấy nhóm <b>{arg}</b> trong DB.\n\n"
                    "⚠️ Hãy để user trong nhóm gõ /capquyen trước "
                    "hoặc dùng /cap &lt;chat_id&gt;.\n\n"
                    f"<code>{e}</code>",
                    parse_mode="HTML",
                )
                return
    else:
        await message.reply_text(
            "❌ Sai cú pháp.\n\n"
            "Dùng: <code>/cap @tennhom</code> "
            "hoặc <code>/cap -1001234567890</code>",
            parse_mode="HTML",
        )
        return

    if not target_chat_id:
        await message.reply_text("❌ Không xác định được nhóm.")
        return

    already = await group_enabled(target_chat_id)
    if already:
        await message.reply_text(
            f"ℹ️ Nhóm <b>{target_info.get('title', target_chat_id)}</b> "
            f"đã được cấp quyền từ trước.\n\n"
            f"🆔 <code>{target_chat_id}</code>",
            parse_mode="HTML",
        )
        return

    await approve_group(target_chat_id)

    title = target_info.get("title") if target_info else None
    username = target_info.get("username") if target_info else None

    confirm_text = (
        "✅ <b>ĐÃ CẤP QUYỀN THÀNH CÔNG</b>\n\n"
        f"📌 Nhóm: <b>{title or 'Không tên'}</b>\n"
        f"🆔 Chat ID: <code>{target_chat_id}</code>\n"
    )
    if username:
        confirm_text += f"🔗 Username: @{username}\n"

    confirm_text += (
        "\n🤖 Bot đã sẵn sàng hoạt động trong nhóm này."
    )

    await message.reply_text(confirm_text, parse_mode="HTML")

    try:
        await context.bot.send_message(
            chat_id=target_chat_id,
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
    except Exception as e:
        logger.warning("⚠️ Không gửi được thông báo vào nhóm %s: %s", target_chat_id, e)
        await message.reply_text(
            "⚠️ Đã cấp quyền trong DB nhưng <b>không gửi được thông báo "
            "vào nhóm</b> (bot có thể chưa ở trong nhóm đó).",
            parse_mode="HTML",
        )


# =========================================================
# 🆕 /UNCAP @username | /uncap <chat_id> (OWNER - thu hồi quyền)
# =========================================================

async def uncap_command(update, context):
    message = update.effective_message
    user = update.effective_user

    if not user or user.id != OWNER_ID:
        await message.reply_text("❌ Chỉ chủ bot mới dùng được lệnh này.")
        return

    if not context.args:
        await message.reply_text(
            "📖 <b>HƯỚNG DẪN DÙNG /uncap</b>\n\n"
            "• <code>/uncap @tennhom</code> — thu hồi quyền nhóm\n"
            "• <code>/uncap -1001234567890</code> — thu hồi theo chat_id",
            parse_mode="HTML",
        )
        return

    arg = context.args[0].strip()
    target_chat_id = None
    target_info = None

    if re.fullmatch(r"-?\d+", arg):
        try:
            target_chat_id = int(arg)
            target_info = await find_group_by_chat_id(target_chat_id)
        except Exception:
            await message.reply_text("❌ Chat ID không hợp lệ.")
            return
    elif arg.startswith("@"):
        target_info = await find_group_by_username(arg)
        if target_info:
            target_chat_id = target_info["chat_id"]
    else:
        await message.reply_text("❌ Sai cú pháp.")
        return

    if not target_chat_id:
        await message.reply_text(f"❌ Không tìm thấy nhóm <b>{arg}</b> trong DB.", parse_mode="HTML")
        return

    if not await group_enabled(target_chat_id):
        await message.reply_text(
            f"ℹ️ Nhóm <b>{target_info.get('title', target_chat_id)}</b> "
            f"chưa được cấp quyền.",
            parse_mode="HTML",
        )
        return

    await disable_group(target_chat_id)

    await message.reply_text(
        "🚫 <b>ĐÃ THU HỒI QUYỀN</b>\n\n"
        f"📌 Nhóm: <b>{target_info.get('title', 'Không tên')}</b>\n"
        f"🆔 Chat ID: <code>{target_chat_id}</code>",
        parse_mode="HTML",
    )

    try:
        await context.bot.send_message(
            chat_id=target_chat_id,
            text=(
                "🚫 <b>BOT ĐÃ BỊ THU HỒI QUYỀN</b>\n\n"
                "Bot sẽ không hoạt động trong nhóm này nữa.\n"
                "Liên hệ admin nếu cần hỗ trợ."
            ),
            parse_mode="HTML",
        )
    except Exception:
        pass


# =========================================================
# 🔘 CALLBACK
# =========================================================

async def callback_handler(update, context):
    query = update.callback_query

    if not query or not query.from_user:
        return

    data = query.data or ""

    if data == "pending_list":
        if query.from_user.id != OWNER_ID:
            await query.answer("❌ Bạn không có quyền.", show_alert=True)
            return

        conn = get_db()
        try:
            rows = conn.execute(
                "SELECT chat_id, title, username, requested_at FROM groups WHERE enabled = 0 ORDER BY requested_at DESC LIMIT 20"
            ).fetchall()
        finally:
            conn.close()

        if not rows:
            await query.answer("✅ Không có nhóm nào chờ duyệt.", show_alert=True)
            return

        text_lines = ["📋 <b>DANH SÁCH NHÓM CHỜ DUYỆT</b>\n"]
        buttons = []

        for r in rows:
            title = r["title"] or "Không tên"
            uname = f" (@{r['username']})" if r["username"] else ""
            text_lines.append(
                f"• <b>{title}</b>{uname}\n"
                f"  🆔 <code>{r['chat_id']}</code>"
            )
            buttons.append(
                [
                    InlineKeyboardButton(
                        f"✅ {title[:20]}",
                        callback_data=f"approve:{r['chat_id']}",
                    ),
                    InlineKeyboardButton(
                        "❌",
                        callback_data=f"deny:{r['chat_id']}",
                    ),
                ]
            )

        buttons.append(
            [InlineKeyboardButton("🔄 LÀM MỚI", callback_data="pending_list")]
        )

        try:
            await query.edit_message_text(
                "\n".join(text_lines),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
        except Exception:
            await query.message.reply_text(
                "\n".join(text_lines),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(buttons),
            )

        await query.answer()
        return

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
                    "Liên hệ admin nếu cần hỗ trợ."
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass
        return

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

        try:
            await message.delete()
        except Exception as e:
            logger.error("❌ Delete failed: %s", e)

        username = f"@{user.username}" if user.username else "Không có username"
        full_name = user.full_name or "Unknown"

        count = await add_violation(chat.id, user.id, username, full_name)

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
    target_chat_id, ok = await resolve_target_chat(update, context)
    if not ok:
        return

    target = await resolve_user(update, context, target_chat_id)

    if not target:
        await update.effective_message.reply_text(
            "⚠️ Không tìm thấy người dùng.\n\n"
            "💡 Cách dùng:\n"
            "• Reply vào tin nhắn + /vipham\n"
            "• Trong nhóm: <code>/vipham ID/@username</code>\n"
            "• Chat riêng: <code>/vipham &lt;chat_id&gt; ID/@username</code>",
            parse_mode="HTML"
        )
        return

    count = await get_violation_count(target_chat_id, target.id)

    await update.effective_message.reply_text(
        "👤 <b>THÔNG TIN VI PHẠM</b>\n\n"
        f"📌 Nhóm ID: <code>{target_chat_id}</code>\n"
        f"👤 Thành viên: {target.mention_html()}\n"
        f"🆔 ID: <code>{target.id}</code>\n"
        f"⚠️ Vi phạm: <b>{count}</b>\n"
        f"📊 Giới hạn: <b>{MAX_VIOLATIONS}</b>",
        parse_mode="HTML",
    )


# =========================================================
# 🔄 /RESETVIPHAM
# =========================================================

async def resetvipham(update, context):
    target_chat_id, ok = await resolve_target_chat(update, context)
    if not ok:
        return

    target = await resolve_user(update, context, target_chat_id)

    if not target:
        await update.effective_message.reply_text(
            "⚠️ Hãy reply tin nhắn người cần reset hoặc dùng:\n\n"
            "• Trong nhóm: <code>/resetvipham ID/@username</code>\n"
            "• Chat riêng: <code>/resetvipham &lt;chat_id&gt; ID/@username</code>",
            parse_mode="HTML"
        )
        return

    await reset_violation(target_chat_id, target.id)

    await update.effective_message.reply_text(
        "🔄 <b>ĐÃ RESET VI PHẠM</b>\n\n"
        f"📌 Nhóm ID: <code>{target_chat_id}</code>\n"
        f"👤 Thành viên: {target.mention_html()}\n"
        "⚠️ Số lần vi phạm: <b>0</b>",
        parse_mode="HTML",
    )


# =========================================================
# 🔇 /MUTE
# =========================================================

async def mute_command(update, context):
    target_chat_id, ok = await resolve_target_chat(update, context)
    if not ok:
        return

    target = await resolve_user(update, context, target_chat_id)

    if not target:
        await update.effective_message.reply_text(
            "⚠️ Hãy reply người cần mute hoặc dùng:\n\n"
            "• Trong nhóm: <code>/mute ID/@username [phút]</code>\n"
            "• Chat riêng: <code>/mute &lt;chat_id&gt; ID/@username [phút]</code>\n\n"
            "⏱️ Mặc định: 2 phút.",
            parse_mode="HTML"
        )
        return

    minutes = MUTE_MINUTES

    if context.args and re.fullmatch(r"\d+", context.args[-1]):
        try:
            minutes = max(1, int(context.args[-1]))
        except Exception:
            pass

    try:
        await mute_user(context.bot, target_chat_id, target.id, minutes)
        await update.effective_message.reply_text(
            "🔇 <b>ĐÃ MUTE</b>\n\n"
            f"📌 Nhóm ID: <code>{target_chat_id}</code>\n"
            f"👤 Thành viên: {target.mention_html()}\n"
            f"⏱️ Thời gian: <b>{minutes} phút</b>",
            parse_mode="HTML",
        )
    except Exception as e:
        await update.effective_message.reply_text(
            f"❌ <b>Không thể mute.</b>\n\n<code>{e}</code>\n\n"
            "⚠️ Hãy kiểm tra quyền Restrict Members của bot.",
            parse_mode="HTML",
        )


# =========================================================
# 🔊 /UNMUTE
# =========================================================

async def unmute_command(update, context):
    target_chat_id, ok = await resolve_target_chat(update, context)
    if not ok:
        return

    target = await resolve_user(update, context, target_chat_id)

    if not target:
        await update.effective_message.reply_text(
            "⚠️ Hãy reply người cần unmute hoặc dùng:\n\n"
            "• Trong nhóm: <code>/unmute ID/@username</code>\n"
            "• Chat riêng: <code>/unmute &lt;chat_id&gt; ID/@username</code>",
            parse_mode="HTML"
        )
        return

    try:
        await unmute_user(context.bot, target_chat_id, target.id)
        await update.effective_message.reply_text(
            "🔊 <b>ĐÃ BỎ MUTE</b>\n\n"
            f"📌 Nhóm ID: <code>{target_chat_id}</code>\n"
            f"👤 Thành viên: {target.mention_html()}",
            parse_mode="HTML",
        )
    except Exception as e:
        await update.effective_message.reply_text(
            f"❌ <b>Không thể unmute.</b>\n\n<code>{e}</code>",
            parse_mode="HTML",
        )


# =========================================================
# 🔐 /BOTCHECK
# =========================================================

async def botcheck(update, context):
    target_chat_id, ok = await resolve_target_chat(update, context)
    if not ok:
        return

    try:
        me = await context.bot.get_me()
        member = await context.bot.get_chat_member(target_chat_id, me.id)

        if member.status not in (
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        ):
            await update.effective_message.reply_text("❌ Bot chưa phải admin trong nhóm này.")
            return

        delete_ok = getattr(member, "can_delete_messages", False)
        restrict_ok = getattr(member, "can_restrict_members", False)

        text = (
            "🤖 <b>KIỂM TRA QUYỀN BOT</b>\n\n"
            f"📌 Nhóm ID: <code>{target_chat_id}</code>\n"
            f"👮 Trạng thái: <b>{member.status}</b>\n\n"
            f"🗑️ Xoá tin: {'✅' if delete_ok else '❌'}\n"
            f"🔇 Mute/Bỏ mute: {'✅' if restrict_ok else '❌'}\n"
            f"🔨 Ban/Unban: {'✅' if restrict_ok else '❌'}\n"
        )

        if not delete_ok or not restrict_ok:
            text += "\n⚠️ Hãy cấp cho bot toàn bộ quyền quản trị cần thiết."
        else:
            text += "\n🎉 Bot đã đủ quyền để hoạt động."

        await update.effective_message.reply_text(text, parse_mode="HTML")

    except Exception as e:
        await update.effective_message.reply_text(
            f"❌ Không kiểm tra được quyền bot.\n<code>{e}</code>",
            parse_mode="HTML",
        )


# =========================================================
# ℹ️ /TRANGTHAI
# =========================================================

async def trangthai(update, context):
    target_chat_id, ok = await resolve_target_chat(update, context)
    if not ok:
        return

    enabled = await group_enabled(target_chat_id)
    status = "🟢 ĐANG HOẠT ĐỘNG" if enabled else "🔴 CHƯA ĐƯỢC CẤP QUYỀN"

    await update.effective_message.reply_text(
        "🛡️ <b>TRẠNG THÁI BOT</b>\n\n"
        f"📌 Nhóm ID: <code>{target_chat_id}</code>\n"
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
# 🆕 /PENDING
# =========================================================

async def pending_command(update, context):
    message = update.effective_message
    user = update.effective_user

    if not user or user.id != OWNER_ID:
        await message.reply_text("❌ Chỉ chủ bot mới dùng được.")
        return

    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT chat_id, title, username, requested_at FROM groups WHERE enabled = 0 ORDER BY requested_at DESC LIMIT 30"
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        await message.reply_text("✅ Không có nhóm nào chờ duyệt.")
        return

    text_lines = ["📋 <b>DANH SÁCH NHÓM CHỜ DUYỆT</b>\n"]
    buttons = []

    for r in rows:
        title = r["title"] or "Không tên"
        uname = f" (@{r['username']})" if r["username"] else ""
        text_lines.append(
            f"• <b>{title}</b>{uname}\n"
            f"  🆔 <code>{r['chat_id']}</code>"
        )
        buttons.append(
            [
                InlineKeyboardButton(
                    f"✅ {title[:20]}",
                    callback_data=f"approve:{r['chat_id']}",
                ),
                InlineKeyboardButton(
                    "❌",
                    callback_data=f"deny:{r['chat_id']}",
                ),
            ]
        )

    buttons.append(
        [InlineKeyboardButton("🔄 LÀM MỚI", callback_data="pending_list")]
    )

    await message.reply_text(
        "\n".join(text_lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# =========================================================
# 🆕 /GROUPS
# =========================================================

async def groups_command(update, context):
    message = update.effective_message
    user = update.effective_user

    if not user or user.id != OWNER_ID:
        await message.reply_text("❌ Chỉ chủ bot mới dùng được.")
        return

    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT chat_id, title, username, approved_at FROM groups WHERE enabled = 1 ORDER BY approved_at DESC LIMIT 30"
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        await message.reply_text("📭 Chưa có nhóm nào được cấp quyền.")
        return

    text_lines = ["✅ <b>NHÓM ĐÃ CẤP QUYỀN</b>\n"]
    for r in rows:
        uname = f" (@{r['username']})" if r["username"] else ""
        text_lines.append(
            f"• <b>{r['title'] or 'Không tên'}</b>{uname}\n"
            f"  🆔 <code>{r['chat_id']}</code>"
        )

    await message.reply_text("\n".join(text_lines), parse_mode="HTML")


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

    # GROUP -2: Fallback phát hiện nhóm chưa cấp quyền
    application.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & ~filters.StatusUpdate.ALL,
            fallback_group_detect,
        ),
        group=-2,
    )

    # GROUP -1: Bắt sự kiện bot được thêm vào nhóm
    application.add_handler(
        MessageHandler(
            filters.StatusUpdate.NEW_CHAT_MEMBERS,
            on_bot_added,
        ),
        group=-1,
    )

    # COMMANDS
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("capquyen", capquyen))
    application.add_handler(CommandHandler("cap", cap_command))
    application.add_handler(CommandHandler("grant", cap_command))
    application.add_handler(CommandHandler("uncap", uncap_command))
    application.add_handler(CommandHandler("botcheck", botcheck))
    application.add_handler(CommandHandler("trangthai", trangthai))
    application.add_handler(CommandHandler("tb", tb_command))
    application.add_handler(CommandHandler("mute", mute_command))
    application.add_handler(CommandHandler("unmute", unmute_command))
    application.add_handler(CommandHandler("vipham", vipham))
    application.add_handler(CommandHandler("resetvipham", resetvipham))
    application.add_handler(CommandHandler("pending", pending_command))
    application.add_handler(CommandHandler("groups", groups_command))
    application.add_handler(CommandHandler("admin", pending_command))

    # CALLBACK
    application.add_handler(CallbackQueryHandler(callback_handler))

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
