import logging
import os
import psycopg2
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)
# =====================================================
# 1. CẤU HÌNH
# =====================================================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_PORT = os.getenv("DB_PORT", "5432")
try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
except ValueError:
    ADMIN_ID = 0
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
JOB_NAME = "broadcast_job"
BROADCAST_INTERVAL = 120
# =====================================================
# 2. KẾT NỐI DATABASE
# =====================================================
def get_db_connection():
    return psycopg2.connect(
        host=DB_HOST,
        database=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        port=DB_PORT,
        connect_timeout=10,
    )
def init_db():
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_groups (
                    group_id BIGINT PRIMARY KEY
                );
            """)
        conn.commit()
        logger.info("Database đã sẵn sàng.")
    finally:
        conn.close()
def get_bot_groups():
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT group_id FROM bot_groups;")
            rows = cur.fetchall()
        return {row[0] for row in rows}
    finally:
        conn.close()
def save_group(group_id: int):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bot_groups (group_id)
                VALUES (%s)
                ON CONFLICT (group_id) DO NOTHING;
                """,
                (group_id,),
            )
        conn.commit()
    finally:
        conn.close()
# =====================================================
# 3. KIỂM TRA QUYỀN ADMIN
# =====================================================
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID
# =====================================================
# 4. HÀM GỬI THÔNG BÁO ĐỊNH KỲ
# =====================================================
async def alarm_broadcast_callback(
    context: ContextTypes.DEFAULT_TYPE
):
    message_text = context.job.data
    try:
        groups = get_bot_groups()
    except Exception:
        logger.exception("Không thể lấy danh sách nhóm từ database.")
        return
    if not groups:
        logger.warning("Không có nhóm nhận thông báo.")
        return
    success = 0
    failed = 0
    for group_id in groups:
        try:
            await context.bot.send_message(
                chat_id=group_id,
                text=message_text,
            )
            success += 1
            logger.info(
                "Gửi thông báo định kỳ thành công đến nhóm %s",
                group_id,
            )
        except TelegramError as e:
            failed += 1
            logger.error(
                "Telegram từ chối gửi đến nhóm %s: %s",
                group_id,
                e,
            )
        except Exception:
            failed += 1
            logger.exception(
                "Lỗi không xác định khi gửi đến nhóm %s",
                group_id,
            )
    logger.info(
        "Thông báo định kỳ: thành công %s/%s, thất bại %s",
        success,
        len(groups),
        failed,
    )
# =====================================================
# 5. LỆNH /START
# =====================================================
async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    user = update.effective_user
    if not user or not is_admin(user.id):
        await update.effective_message.reply_text(
            "Bạn không có quyền sử dụng bot này."
        )
        return
    await update.effective_message.reply_text(
        "🤖 BOT THÔNG BÁO ĐANG HOẠT ĐỘNG!\n\n"
        "Các lệnh sử dụng:\n\n"
        "/addid ID_NHOM - Thêm nhóm nhận thông báo\n"
        "/lsid - Xem danh sách nhóm\n"
        "/tb nội dung - Bắt đầu thông báo mỗi 2 phút\n"
        "/stoptb - Dừng thông báo\n\n"
        "⏰ Chu kỳ gửi: 120 giây/lần."
    )
# =====================================================
# 6. LỆNH /ADDID
# =====================================================
async def addid_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    user = update.effective_user
    if not user or not is_admin(user.id):
        await update.effective_message.reply_text(
            "Bạn không có quyền sử dụng lệnh này."
        )
        return
    if not context.args:
        await update.effective_message.reply_text(
            "Cách sử dụng:\n/addid ID_NHOM"
        )
        return
    try:
        group_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text(
            "❌ ID nhóm phải là số nguyên."
        )
        return
    try:
        chat = await context.bot.get_chat(group_id)
        chat_title = chat.title or "Không có tên"
        chat_username = (
            f"@{chat.username}"
            if chat.username
            else "Không có username"
        )
        keyboard = [[
            InlineKeyboardButton(
                "✅ Xác nhận thêm nhóm",
                callback_data=f"confirm_add_{group_id}",
            )
        ]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.effective_message.reply_text(
            f"📋 THÔNG TIN NHÓM\n\n"
            f"Tên nhóm: {chat_title}\n"
            f"Username: {chat_username}\n"
            f"ID nhóm: {group_id}\n\n"
            f"Bạn có muốn thêm nhóm này vào danh sách nhận thông báo không?",
            reply_markup=reply_markup,
        )
    except TelegramError as e:
        logger.exception("Không lấy được thông tin nhóm.")
        await update.effective_message.reply_text(
            f"❌ Không thể lấy thông tin nhóm.\n\n"
            f"Kiểm tra ID nhóm và đảm bảo bot có thể truy cập nhóm.\n\n"
            f"Lỗi: {str(e)[:1000]}"
        )
# =====================================================
# 7. XỬ LÝ NÚT XÁC NHẬN THÊM NHÓM
# =====================================================
async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text(
            "Bạn không có quyền thao tác."
        )
        return
    data = query.data
    if not data.startswith("confirm_add_"):
        return
    try:
        group_id = int(data.replace("confirm_add_", "", 1))
        # Lưu ID nhóm vào database
        save_group(group_id)
        # Lấy thông tin nhóm
        chat = await context.bot.get_chat(group_id)
        chat_title = chat.title or "Không có tên"
        chat_username = (
            f"@{chat.username}"
            if chat.username
            else "Không có"
        )
        await query.edit_message_text(
            f"✅ ĐÃ THÊM NHÓM THÀNH CÔNG!\n\n"
            f"Tên nhóm: {chat_title}\n"
            f"Username: {chat_username}\n"
            f"ID nhóm: {group_id}\n\n"
            f"Nhóm đã được lưu vào PostgreSQL."
        )
    except Exception as e:
        logger.exception("Lỗi xác nhận thêm nhóm.")
        await query.edit_message_text(
            f"❌ Lỗi khi thêm nhóm:\n{str(e)[:1000]}"
        )
# =====================================================
# 8. LỆNH /LSID
# =====================================================
async def lsid_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    user = update.effective_user
    if not user or not is_admin(user.id):
        await update.effective_message.reply_text(
            "Bạn không có quyền sử dụng lệnh này."
        )
        return
    try:
        groups = sorted(get_bot_groups())
        if not groups:
            await update.effective_message.reply_text(
                "📋 Danh sách nhóm hiện tại đang trống."
            )
            return
        result = (
            f"📋 DANH SÁCH NHÓM ĐÃ THÊM\n"
            f"Tổng số: {len(groups)} nhóm\n\n"
        )
        for group_id in groups:
            try:
                chat = await context.bot.get_chat(group_id)
                title = chat.title or "Không có tên"
                username = (
                    f"@{chat.username}"
                    if chat.username
                    else "Không có"
                )
                result += (
                    f"Nhóm: {title}\n"
                    f"Username: {username}\n"
                    f"ID: {group_id}\n\n"
                )
            except TelegramError as e:
                result += (
                    f"ID: {group_id}\n"
                    f"Không lấy được thông tin: {str(e)[:150]}\n\n"
                )
        # Chia tin nhắn nếu danh sách quá dài
        for i in range(0, len(result), 4000):
            await update.effective_message.reply_text(
                result[i:i + 4000]
            )
    except Exception as e:
        logger.exception("Lỗi đọc danh sách nhóm.")
        await update.effective_message.reply_text(
            f"❌ Không đọc được database:\n{str(e)[:1000]}"
        )
# =====================================================
# 9. LỆNH /TB - GỬI NGAY VÀ LẶP LẠI MỖI 2 PHÚT
# =====================================================
async def broadcast_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    user = update.effective_user
    if not user or not is_admin(user.id):
        await update.effective_message.reply_text(
            "Bạn không có quyền sử dụng lệnh này."
        )
        return
    text = " ".join(context.args).strip()
    if not text:
        await update.effective_message.reply_text(
            "⚠️ Cách sử dụng:\n/tb Nội dung thông báo"
        )
        return
    if context.job_queue is None:
        await update.effective_message.reply_text(
            "❌ JobQueue chưa được cài đặt.\n\n"
            "Chạy lệnh:\n"
            "pip install -U 'python-telegram-bot[job-queue]'"
        )
        return
    try:
        groups = sorted(get_bot_groups())
    except Exception as e:
        logger.exception("Không thể đọc danh sách nhóm.")
        await update.effective_message.reply_text(
            f"❌ Lỗi database:\n{str(e)[:1000]}"
        )
        return
    if not groups:
        await update.effective_message.reply_text(
            "❌ Chưa có nhóm nhận thông báo.\n"
            "Hãy sử dụng /addid để thêm nhóm."
        )
        return
    current_jobs = context.job_queue.get_jobs_by_name(JOB_NAME)
    if current_jobs:
        await update.effective_message.reply_text(
            "⚠️ Đang có thông báo chạy.\n"
            "Hãy dùng /stoptb trước khi thay đổi nội dung."
        )
        return
    success = []
    failed = []
    # GỬI THÔNG BÁO NGAY LẬP TỨC
    for group_id in groups:
        try:
            await context.bot.send_message(
                chat_id=group_id,
                text=text,
            )
            success.append(group_id)
            logger.info(
                "Gửi thông báo thành công đến nhóm %s",
                group_id,
            )
        except TelegramError as e:
            failed.append((group_id, str(e)))
            logger.error(
                "Gửi thất bại đến nhóm %s: %s",
                group_id,
                e,
            )
        except Exception as e:
            failed.append((group_id, str(e)))
            logger.exception(
                "Lỗi gửi thông báo đến nhóm %s",
                group_id,
            )
    # Nếu tất cả nhóm đều thất bại thì không tạo lịch
    if not success:
        error_text = "❌ GỬI THẤT BẠI TẤT CẢ NHÓM!\n\n"
        for group_id, error in failed:
            error_text += (
                f"ID nhóm: {group_id}\n"
                f"Lỗi: {error[:250]}\n\n"
            )
        await update.effective_message.reply_text(
            error_text[:4000]
        )
        return
    # BẮT ĐẦU GỬI ĐỊNH KỲ MỖI 120 GIÂY
    try:
        context.job_queue.run_repeating(
            alarm_broadcast_callback,
            interval=BROADCAST_INTERVAL,
            first=BROADCAST_INTERVAL,
            data=text,
            name=JOB_NAME,
        )
    except Exception as e:
        logger.exception("Không tạo được lịch thông báo.")
        await update.effective_message.reply_text(
            f"⚠️ Đã gửi tin ngay đến {len(success)} nhóm "
            f"nhưng không tạo được lịch định kỳ.\n\n"
            f"Lỗi: {str(e)[:1000]}"
        )
        return
    # BÁO KẾT QUẢ
    result = (
        f"📢 ĐÃ BẮT ĐẦU THÔNG BÁO!\n\n"
        f"✅ Gửi thành công: {len(success)}/{len(groups)} nhóm\n"
        f"❌ Gửi thất bại: {len(failed)} nhóm\n"
        f"⏰ Chu kỳ: 120 giây/lần\n\n"
        f"📝 Nội dung:\n{text}\n\n"
        f"🛑 Dùng /stoptb để dừng."
    )
    if failed:
        result += "\n\n❌ CHI TIẾT NHÓM THẤT BẠI:\n"
        for group_id, error in failed:
            result += (
                f"\nID: {group_id}\n"
                f"Lỗi: {error[:200]}\n"
            )
    await update.effective_message.reply_text(
        result[:4000]
    )
# =====================================================
# 10. LỆNH /STOPTB
# =====================================================
async def stop_broadcast_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    user = update.effective_user
    if not user or not is_admin(user.id):
        await update.effective_message.reply_text(
            "Bạn không có quyền sử dụng lệnh này."
        )
        return
    if context.job_queue is None:
        await update.effective_message.reply_text(
            "JobQueue chưa được kích hoạt."
        )
        return
    current_jobs = context.job_queue.get_jobs_by_name(JOB_NAME)
    if not current_jobs:
        await update.effective_message.reply_text(
            "⚠️ Hiện không có thông báo định kỳ nào đang chạy."
        )
        return
    for job in current_jobs:
        job.schedule_removal()
    await update.effective_message.reply_text(
        "🛑 ĐÃ DỪNG THÔNG BÁO ĐỊNH KỲ THÀNH CÔNG!"
    )
# =====================================================
# 11. KHỞI ĐỘNG BOT
# =====================================================
def main():
    if not BOT_TOKEN:
        logger.error("Chưa cấu hình BOT_TOKEN trong file .env")
        return
    if not ADMIN_ID:
        logger.error("Chưa cấu hình ADMIN_ID trong file .env")
        return
    if not all([DB_NAME, DB_USER, DB_PASSWORD]):
        logger.error(
            "Thiếu DB_NAME, DB_USER hoặc DB_PASSWORD trong file .env"
        )
        return
    try:
        init_db()
    except Exception:
        logger.exception("Không thể kết nối hoặc khởi tạo PostgreSQL.")
        return
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    # ĐĂNG KÝ CÁC LỆNH
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("addid", addid_command))
    app.add_handler(CommandHandler("lsid", lsid_command))
    app.add_handler(CommandHandler("tb", broadcast_command))
    app.add_handler(CommandHandler("stoptb", stop_broadcast_command))
    # XỬ LÝ NÚT XÁC NHẬN
    app.add_handler(CallbackQueryHandler(button_handler))
    logger.info("Bot thông báo đang khởi động...")
    app.run_polling()
if __name__ == "__main__":
    main()
