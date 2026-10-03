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

# Khởi tạo bảng và dữ liệu mẫu nếu chưa có
def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Tạo bảng lưu nội dung spam nhóm
    cur.execute("""
        CREATE TABLE IF NOT EXISTS spam_settings (
            id INT PRIMARY KEY,
            content TEXT
        );
    """)
    # Tạo bảng lưu danh sách nhóm
    cur.execute("""
        CREATE TABLE IF NOT EXISTS spam_groups (
            group_id BIGINT PRIMARY KEY
        );
    """)
    # Tạo bảng lưu danh sách người dùng từng nhắn tin với bot
    cur.execute("""
        CREATE TABLE IF NOT EXISTS spam_users (
            user_id BIGINT PRIMARY KEY
        );
    """)
    
    # Thêm dòng nội dung mặc định nếu chưa tồn tại
    cur.execute("SELECT COUNT(*) FROM spam_settings WHERE id = 1;")
    if cur.fetchone()[0] == 0:
        cur.execute("INSERT INTO spam_settings (id, content) VALUES (1, 'Chưa có nội dung spam nào được thiết lập.');")
        
    conn.commit()
    cur.close()
    conn.close()

init_db()

# Kiểm tra quyền Admin
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

# Lấy nội dung spam từ DB
def get_spam_content():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT content FROM spam_settings WHERE id = 1;")
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row[0] if row else "Chưa có nội dung spam nào được thiết lập."

# Lấy danh sách ID nhóm từ DB
def get_spam_groups():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT group_id FROM spam_groups;")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return {row[0] for row in rows}

# Lấy danh sách ID người dùng từ DB
def get_spam_users():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM spam_users;")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return {row[0] for row in rows}

# Hàm chạy định kỳ gửi tin nhắn spam nhóm mỗi 2 phút (120 giây)
async def alarm_spam_callback(context: ContextTypes.DEFAULT_TYPE):
    spam_groups = get_spam_groups()
    spam_content = get_spam_content()

    if not spam_groups or spam_content == "Chưa có nội dung spam nào được thiết lập.":
        logging.warning("Job spam nhóm dừng lại vì chưa có nhóm hoặc nội dung spam.")
        return

    success_count = 0
    for group_id in spam_groups:
        try:
            await context.bot.send_message(chat_id=group_id, text=spam_content)
            success_count += 1
        except Exception as e:
            logging.error(f"Không thể gửi tin nhắn tự động tới nhóm {group_id}: {e}")
    
    logging.info(f"Đã chạy vòng lặp spam nhóm: Gửi thành công đến {success_count}/{len(spam_groups)} nhóm.")

# Hàm chạy định kỳ gửi thông báo (/tb) đến toàn bộ người dùng và nhóm mỗi 2 phút (120 giây)
async def alarm_broadcast_callback(context: ContextTypes.DEFAULT_TYPE):
    message_text = context.job.data
    groups = get_spam_groups()
    users = get_spam_users()

    success_groups = 0
    success_users = 0

    # Gửi đến các nhóm
    for group_id in groups:
        try:
            await context.bot.send_message(chat_id=group_id, text=message_text)
            success_groups += 1
        except Exception as e:
            logging.error(f"Lỗi gửi thông báo đến nhóm {group_id}: {e}")

    # Gửi đến cá nhân người dùng
    for user_id in users:
        try:
            await context.bot.send_message(chat_id=user_id, text=message_text)
            success_users += 1
        except Exception as e:
            logging.error(f"Lỗi gửi thông báo đến người dùng {user_id}: {e}")

    logging.info(f"Đã gửi thông báo định kỳ: {success_groups} nhóm, {success_users} người dùng.")

# Lệnh /start hiển thị Menu chính
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user and update.effective_chat.type == "private":
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("INSERT INTO spam_users (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING;", (user.id,))
        conn.commit()
        cur.close()
        conn.close()

    if not is_admin(user.id):
        await update.message.reply_text("Bạn không có quyền sử dụng bot này.")
        return

    keyboard = [
        [InlineKeyboardButton("🚀 Bật Spam tự động (2 phút/lần)", callback_data="run_spam")],
        [InlineKeyboardButton("⏹ Dừng Spam tự động", callback_data="stop_spam")],
        [InlineKeyboardButton("📋 Danh sách ID nhóm hiện tại", callback_data="list_groups")],
        [InlineKeyboardButton("📝 Nội dung spam", callback_data="view_content")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Bảng điều khiển Spam Bot (Lưu trên PostgreSQL):", reply_markup=reply_markup)

# Xử lý các nút bấm trong Menu
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        await query.edit_message_text("Bạn không có quyền thao tác.")
        return

    data = query.data
    spam_groups = get_spam_groups()
    spam_content = get_spam_content()

    if data == "run_spam":
        if not spam_groups:
            await query.edit_message_text("❌ Chưa có nhóm nào lưu trong hệ thống. Hãy thêm bot vào các nhóm chat trước.")
            return
        if spam_content == "Chưa có nội dung spam nào được thiết lập.":
            await query.edit_message_text("❌ Chưa thiết lập nội dung spam. Hãy dùng lệnh /nd để tạo nội dung.")
            return

        current_jobs = context.job_queue.get_jobs_by_name("spam_job")
        if current_jobs:
            await query.edit_message_text("⚠️ Tiến trình spam tự động đã đang chạy từ trước rồi!")
            return

        context.job_queue.run_repeating(
            alarm_spam_callback, 
            interval=120, 
            first=0, 
            name="spam_job"
        )
        await query.edit_message_text("✅ Đã kích hoạt chế độ tự động spam vào nhóm mỗi **2 phút/lần**!", parse_mode="Markdown")

    elif data == "stop_spam":
        current_jobs = context.job_queue.get_jobs_by_name("spam_job")
        if not current_jobs:
            await query.edit_message_text("⚠️ Hiện tại không có tiến trình spam nào đang chạy.")
            return
        
        for job in current_jobs:
            job.schedule_removal()
        
        await query.edit_message_text("🛑 Đã dừng thành công tiến trình tự động spam!")

    elif data == "list_groups":
        if not spam_groups:
            text = "Danh sách nhóm spam hiện tại: Trống."
        else:
            text = "📋 **Danh sách ID các nhóm bot đang tham gia:**\n" + "\n".join(str(g) for g in spam_groups)
        
        keyboard = [[InlineKeyboardButton("🔙 Quay lại Menu", callback_data="back_home")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "view_content":
        text = f"📝 **Nội dung spam hiện tại:**\n\n{spam_content}"
        keyboard = [[InlineKeyboardButton("🔙 Quay lại Menu", callback_data="back_home")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "back_home":
        keyboard = [
            [InlineKeyboardButton("🚀 Bật Spam tự động (2 phút/lần)", callback_data="run_spam")],
            [InlineKeyboardButton("⏹️ Dừng Spam tự động", callback_data="stop_spam")],
            [InlineKeyboardButton("📋 Danh sách ID nhóm hiện tại", callback_data="list_groups")],
            [InlineKeyboardButton("📝 Nội dung spam", callback_data="view_content")],
        ]
        await query.edit_message_text("Bảng điều khiển Spam Bot:", reply_markup=InlineKeyboardMarkup(keyboard))

# Hàm ghi nhận nhóm và người dùng tương tác tự động
async def track_chats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user

    conn = get_db_connection()
    cur = conn.cursor()

    try:
        if chat and chat.type in ["group", "supergroup"]:
            cur.execute(
                "INSERT INTO spam_groups (group_id) VALUES (%s) ON CONFLICT (group_id) DO NOTHING;", 
                (chat.id,)
            )
        if user and chat and chat.type == "private":
            cur.execute(
                "INSERT INTO spam_users (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING;", 
                (user.id,)
            )
        conn.commit()
    except Exception as e:
        logging.error(f"Lỗi khi tự động lưu dữ liệu tương tác: {e}")
    finally:
        cur.close()
        conn.close()

# Lệnh /tb: Bắt đầu gửi thông báo lặp lại mỗi 2 phút đến tất cả user và nhóm
async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    # Tự động lưu admin vào danh sách user luôn để đảm bảo nhận được tin nhắn
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("INSERT INTO spam_users (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING;", (user.id,))
    conn.commit()
    cur.close()
    conn.close()

    if not is_admin(user.id):
        return

    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("Sử dụng: /tb [Nội dung thông báo cần gửi định kỳ]")
        return

    current_jobs = context.job_queue.get_jobs_by_name("broadcast_job")
    if current_jobs:
        await update.message.reply_text("⚠️ Đang có một tiến trình thông báo (/tb) chạy rồi. Hãy dùng /stoptb trước nếu muốn thay đổi!")
        return

    context.job_queue.run_repeating(
        alarm_broadcast_callback,
        interval=120,
        first=0,
        data=text,
        name="broadcast_job"
    )

    await update.message.reply_text(f"📢 Đã bắt đầu phát thông báo định kỳ **cứ 2 phút/lần** đến toàn bộ người dùng và nhóm!\n\nNội dung:\n{text}")

# Lệnh /stoptb: Dừng tiến trình thông báo định kỳ
async def stop_broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    current_jobs = context.job_queue.get_jobs_by_name("broadcast_job")
    if not current_jobs:
        await update.message.reply_text("⚠ Hiện không có tiến trình thông báo (/tb) nào đang chạy.")
        return

    for job in current_jobs:
        job.schedule_removal()

    await update.message.reply_text("🛑 Đã dừng thành công tiến trình phát thông báo định kỳ!")

# Lệnh /nd: Thêm/Sửa nội dung spam vào DB
async def set_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("Sử dụng: /nd [Nội dung bạn muốn spam]")
        return

    conn = get_db_connection()
    cur = conn.cursor()
    conn.commit()
    cur.close()
    conn.close()

    await update.message.reply_text(f"✅ Đã cập nhật nội dung spam vào Database:\n\n{text}")

# Lệnh /xoand: Xoá nội dung spam trong DB
async def clear_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    default_text = "Chưa có nội dung spam nào được thiết lập."
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE spam_settings SET content = %s WHERE id = 1;", (default_text,))
    conn.commit()
    cur.close()
    conn.close()

    await update.message.reply_text("🗑️ Đã xoá nội dung spam.")

def main():
    if not BOT_TOKEN:
        print("Lỗi: Chưa cấu hình BOT_TOKEN trong tệp .env")
        return

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Đăng ký các Handler
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("nd", set_content))
    app.add_handler(CommandHandler("xoand", clear_content))
    app.add_handler(CommandHandler("tb", broadcast_command))
    app.add_handler(CommandHandler("stoptb", stop_broadcast_command))
    
    # Lắng nghe tin nhắn chữ và sự kiện thêm nhóm chuẩn xác
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, track_chats))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, track_chats))
    
    app.add_handler(CallbackQueryHandler(button_handler))

    print("Bot tự động spam đang chạy...")
    app.run_polling()

if __name__ == "__main__":
    main()
