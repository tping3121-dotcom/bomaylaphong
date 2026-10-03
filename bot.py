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
    
    # Tạo bảng lưu nội dung spam
    cur.execute("""
        CREATE TABLE IF NOT EXISTS spam_settings (
            id INT PRIMARY KEY,
            content TEXT
        );
    """)
    # Tạo bảng lưu danh sách nhóm (Lưu dưới dạng chat_id kiểu BIGINT để chính xác tuyệt đối)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS spam_groups (
            group_id BIGINT PRIMARY KEY
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

# Hàm chạy định kỳ gửi tin nhắn spam mỗi 2 phút (120 giây)
async def alarm_spam_callback(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    spam_groups = get_spam_groups()
    spam_content = get_spam_content()

    if not spam_groups or spam_content == "Chưa có nội dung spam nào được thiết lập.":
        logging.warning("Job spam dừng lại vì chưa có nhóm hoặc nội dung spam.")
        return

    success_count = 0
    for group_id in spam_groups:
        try:
            await context.bot.send_message(chat_id=group_id, text=spam_content)
            success_count += 1
        except Exception as e:
            logging.error(f"Không thể gửi tin nhắn tự động tới nhóm {group_id}: {e}")
    
    logging.info(f"Đã chạy vòng lặp spam tự động: Gửi thành công đến {success_count}/{len(spam_groups)} nhóm.")

# Lệnh /start hiển thị Menu chính
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Bạn không có quyền sử dụng bot này.")
        return

    keyboard = [
        [InlineKeyboardButton("🚀 Bật Spam tự động (2 phút/lần)", callback_data="run_spam")],
        [InlineKeyboardButton("⏹️ Dừng Spam tự động", callback_data="stop_spam")],
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

        # Kiểm tra xem job spam đã chạy chưa để tránh bị lặp nhiều tiến trình trùng lặp
        current_jobs = context.job_queue.get_jobs_by_name("spam_job")
        if current_jobs:
            await query.edit_message_text("⚠️ Tiến trình spam tự động đã đang chạy từ trước rồi!")
            return

        # Đặt lịch chạy định kỳ mỗi 120 giây (2 phút), chạy ngay lập tức lần đầu tiên (first=0)
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
        await query.edit_message_text("Bảng điều khiển Spam Bot:", reply_markup=reply_markup)

# Hàm tự động ghi nhận nhóm khi bot được thêm vào nhóm hoặc có tin nhắn trong nhóm
async def track_chats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat and chat.type in ["group", "supergroup"]:
        group_id = chat.id
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO spam_groups (group_id) VALUES (%s) ON CONFLICT (group_id) DO NOTHING;", 
                (group_id,)
            )
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            logging.error(f"Lỗi khi tự động lưu nhóm {group_id}: {e}")

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
    cur.execute("UPDATE spam_settings SET content = %s WHERE id = 1;", (text,))
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
    
    # Lắng nghe mọi tin nhắn hoặc sự kiện thêm nhóm để tự động lưu ID nhóm vào DB
    from telegram.ext import MessageHandler, filters
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, track_chats))
    
    app.add_handler(CallbackQueryHandler(button_handler))

    print("Bot tự động spam đang chạy...")
    app.run_polling()

if __name__ == "__main__":
    main()
