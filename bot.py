import logging
import os
import psycopg2
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# Tải cấu hình từ tệp .env
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_PORT = os.getenv("DB_PORT", "5432")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))

# Cấu hình logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

# Hàm kết nối Database PostgreSQL
def get_db_connection():
    conn = psycopg2.connect(
        host=DB_HOST,
        database=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        port=DB_PORT
    )
    return conn

# Khởi tạo bảng lưu danh sách nhóm
def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bot_groups (
            group_id BIGINT PRIMARY KEY
        );
    """)
        
    conn.commit()
    cur.close()
    conn.close()

init_db()

# Kiểm tra quyền Admin
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

# Lấy danh sách ID nhóm từ DB
def get_bot_groups():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT group_id FROM bot_groups;")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return {row[0] for row in rows}

# Hàm chạy định kỳ gửi thông báo đến các nhóm mỗi 2 phút (120 giây)
async def alarm_broadcast_callback(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    message_text = job.data
    groups = get_bot_groups()

    if not groups:
        logging.warning("Job thông báo dừng lại vì chưa có nhóm nào.")
        return

    success_count = 0
    for group_id in groups:
        try:
            await context.bot.send_message(chat_id=group_id, text=message_text)
            success_count += 1
        except Exception as e:
            logging.error(f"Lỗi gửi thông báo định kỳ đến nhóm {group_id}: {e}")

    logging.info(f"Đã gửi thông báo định kỳ thành công đến {success_count}/{len(groups)} nhóm.")

# Lệnh /start kiểm tra bot
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("Bạn không có quyền sử dụng bot này.")
        return
    
    await update.message.reply_text(
        "🤖 **Bot Thông Báo Định Kỳ đang hoạt động!**\n\n"
        "• `/addid [id_nhóm]` - Thêm nhóm bằng ID (có nút xác nhận)\n"
        "• `/lsid` - Xem danh sách ID nhóm đã thêm\n"
        "• `/tb [nội dung]` - Phát thông báo định kỳ (2 phút/lần)\n"
        "• `/stoptb` - Dừng quá trình phát thông báo",
        parse_mode="Markdown"
    )

# Lệnh /addid: Kiểm tra thông tin nhóm và hiển thị nút xác nhận
async def addid_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("Bạn không có quyền sử dụng lệnh này.")
        return

    if not context.args:
        await update.message.reply_text("⚠️ Sử dụng: `/addid [ID_nhóm]`", parse_mode="Markdown")
        return

    try:
        group_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID nhóm phải là một số nguyên.")
        return

    try:
        chat = await context.bot.get_chat(group_id)
        chat_title = chat.title or "Không có tên"
        chat_username = f"@{chat.username}" if chat.username else "Không có username (@)"
        
        keyboard = [[InlineKeyboardButton("✅ Xác nhận thêm nhóm", callback_data=f"confirm_add_{group_id}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            f"📋 **Thông tin nhóm tìm thấy:**\n\n"
            f"• **Tên nhóm:** {chat_title}\n"
            f"• **Username:** {chat_username}\n"
            f"• **ID nhóm:** `{group_id}`\n\n"
            f"Bạn có chắc chắn muốn thêm nhóm này vào danh sách nhận thông báo không?",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Không thể lấy thông tin nhóm. Hãy chắc chắn bot đã được thêm vào nhóm này.\nLỗi: {e}")

# Xử lý nút bấm xác nhận thêm nhóm
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        await query.edit_message_text("Bạn không có quyền thao tác.")
        return

    data = query.data
    if data.startswith("confirm_add_"):
        group_id = int(data.split("_")[2])

        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute(
                "INSERT INTO bot_groups (group_id) VALUES (%s) ON CONFLICT (group_id) DO NOTHING;", 
                (group_id,)
            )
            conn.commit()
            
            chat = await context.bot.get_chat(group_id)
            chat_title = chat.title or "Không có tên"
            chat_username = f"@{chat.username}" if chat.username else "Không có"

            await query.edit_message_text(
                f"✅ **Đã thêm thành công nhóm vào cơ sở dữ liệu!**\n\n"
                f"• **Tên nhóm:** {chat_title}\n"
                f"• **Username:** {chat_username}\n"
                f"• **ID nhóm:** `{group_id}`",
                parse_mode="Markdown"
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Lỗi khi lưu nhóm vào database: {e}")
        finally:
            cur.close()
            conn.close()

# Lệnh /lsid: Kiểm tra danh sách ID nhóm đã thêm kèm full thông tin
async def lsid_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("Bạn không có quyền sử dụng lệnh này.")
        return

    groups = get_bot_groups()
    if not groups:
        await update.message.reply_text("📋 Danh sách ID nhóm hiện tại: Trống.")
        return

    text = "📋 **Danh sách các nhóm đã thêm vào hệ thống:**\n\n"
    for group_id in groups:
        try:
            chat = await context.bot.get_chat(group_id)
            chat_title = chat.title or "Không có tên"
            chat_username = f"@{chat.username}" if chat.username else "Không có"
            text += f"• **Tên:** {chat_title}\n  **Username:** {chat_username}\n  **ID:** `{group_id}`\n\n"
        except Exception:
            text += f"• **Tên:** Không thể lấy thông tin (Bot có thể đã bị kick)\n  **ID:** `{group_id}`\n\n"

    await update.message.reply_text(text, parse_mode="Markdown")

# Lệnh /tb: Bắt đầu phát thông báo định kỳ mỗi 2 phút đến các nhóm
async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("Bạn không có quyền sử dụng lệnh này.")
        return

    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("⚠️ Sử dụng: `/tb [Nội dung thông báo cần gửi định kỳ]`", parse_mode="Markdown")
        return

    groups = get_bot_groups()
    if not groups:
        await update.message.reply_text("❌ Hiện tại bot chưa có nhóm nào trong danh sách. Hãy dùng `/addid` để thêm.")
        return

    current_jobs = context.job_queue.get_jobs_by_name("broadcast_job")
    if current_jobs:
        await update.message.reply_text("⚠️ Đang có một tiến trình thông báo chạy rồi. Hãy dùng `/stoptb` trước nếu muốn thay đổi nội dung!")
        return

    # Gửi ngay lập tức một tin nhắn thử nghiệm ra các nhóm để kiểm tra
    for group_id in groups:
        try:
            await context.bot.send_message(chat_id=group_id, text=text)
        except Exception as e:
            logging.error(f"Lỗi gửi tin nhắn tức thời tới nhóm {group_id}: {e}")

    # Đặt lịch lặp lại mỗi 120 giây (2 phút)
    context.job_queue.run_repeating(
        alarm_broadcast_callback,
        interval=120,
        first=120,
        data=text,
        name="broadcast_job"
    )

    await update.message.reply_text(
        f"📢 **Đã bắt đầu phát thông báo định kỳ (2 phút/lần)** đến `{len(groups)}` nhóm!\n\n"
        f"📝 **Nội dung:**\n{text}\n\n"
        f"🛑 Dùng lệnh `/stoptb` bất cứ lúc nào để dừng.",
        parse_mode="Markdown"
    )

# Lệnh /stoptb: Dừng tiến trình thông báo định kỳ
async def stop_broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Bạn không có quyền sử dụng lệnh này.")
        return

    current_jobs = context.job_queue.get_jobs_by_name("broadcast_job")
    if not current_jobs:
        await update.message.reply_text("⚠️ Hiện không có tiến trình thông báo nào đang chạy.")
        return

    for job in current_jobs:
        job.schedule_removal()

    await update.message.reply_text("🛑 **Đã dừng thành công tiến trình thông báo định kỳ!**", parse_mode="Markdown")

def main():
    if not BOT_TOKEN:
        print("Lỗi: Chưa cấu hình BOT_TOKEN trong tệp .env")
        return

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Đăng ký các Handler
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("addid", addid_command))
    app.add_handler(CommandHandler("lsid", lsid_command))
    app.add_handler(CommandHandler("tb", broadcast_command))
    app.add_handler(CommandHandler("stoptb", stop_broadcast_command))
    app.add_handler(CallbackQueryHandler(button_handler))

    print("Bot thông báo định kỳ đang chạy...")
    app.run_polling()

if __name__ == "__main__":
    main()
