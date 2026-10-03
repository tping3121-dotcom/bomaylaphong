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
    # Tạo bảng lưu danh sách nhóm
    cur.execute("""
        CREATE TABLE IF NOT EXISTS spam_groups (
            group_identifier VARCHAR(255) PRIMARY KEY
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

# Lấy danh sách nhóm từ DB
def get_spam_groups():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT group_identifier FROM spam_groups;")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return {row[0] for row in rows}

# Lệnh /start hiển thị Menu chính
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Bạn không có quyền sử dụng bot này.")
        return

    keyboard = [
        [InlineKeyboardButton("🚀 Spam ngay", callback_data="run_spam")],
        [InlineKeyboardButton("📋 Nhóm spam hiện tại", callback_data="list_groups")],
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
            await query.edit_message_text("❌ Chưa có nhóm nào trong danh sách spam.")
            return
        if spam_content == "Chưa có nội dung spam nào được thiết lập.":
            await query.edit_message_text("❌ Chưa thiết lập nội dung spam.")
            return

        success_count = 0
        for group in spam_groups:
            try:
                await context.bot.send_message(chat_id=group, text=spam_content)
                success_count += 1
            except Exception as e:
                logging.error(f"Không thể gửi tin nhắn tới {group}: {e}")

        await query.edit_message_text(f"✅ Đã spam thành công đến {success_count}/{len(spam_groups)} nhóm!")

    elif data == "list_groups":
        if not spam_groups:
            text = "Danh sách nhóm spam hiện tại: Trống."
        else:
            text = "📋 **Danh sách nhóm spam hiện tại:**\n" + "\n".join(spam_groups)
        
        keyboard = [[InlineKeyboardButton("🔙 Quay lại Menu", callback_data="back_home")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "view_content":
        text = f"📝 **Nội dung spam hiện tại:**\n\n{spam_content}"
        keyboard = [[InlineKeyboardButton("🔙 Quay lại Menu", callback_data="back_home")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "back_home":
        keyboard = [
            [InlineKeyboardButton("🚀 Spam ngay", callback_data="run_spam")],
            [InlineKeyboardButton("📋 Nhóm spam hiện tại", callback_data="list_groups")],
            [InlineKeyboardButton("📝 Nội dung spam", callback_data="view_content")],
        ]
        await query.edit_message_text("Bảng điều khiển Spam Bot:", reply_markup=InlineKeyboardMarkup(keyboard))

# Lệnh /add: Thêm nhóm vào DB
async def add_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    
    if not context.args:
        await update.message.reply_text("Sử dụng: /add @username_nhom hoặc ID nhóm")
        return

    group = context.args[0]
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("INSERT INTO spam_groups (group_identifier) VALUES (%s) ON CONFLICT (group_identifier) DO NOTHING;", (group,))
        conn.commit()
        cur.close()
        conn.close()
        await update.message.reply_text(f"✅ Đã thêm nhóm {group} vào danh sách spam (lưu vào Database).")
    except Exception as e:
        await update.message.reply_text(f"❌ Lỗi khi lưu vào Database: {e}")

# Lệnh /xoaadd: Xoá nhóm khỏi DB
async def remove_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text("Sử dụng: /xoaadd @username_nhom")
        return

    group = context.args[0]
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM spam_groups WHERE group_identifier = %s;", (group,))
    row_count = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()

    if row_count > 0:
        await update.message.reply_text(f"🗑️ Đã xoá nhóm {group} khỏi danh sách.")
    else:
        await update.message.reply_text(f"⚠️ Không tìm thấy nhóm {group} trong danh sách.")

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
    app.add_handler(CommandHandler("add", add_group))
    app.add_handler(CommandHandler("xoaadd", remove_group))
    app.add_handler(CommandHandler("nd", set_content))
    app.add_handler(CommandHandler("xoand", clear_content))
    app.add_handler(CallbackQueryHandler(button_handler))

    print("Bot kết nối PostgreSQL đang chạy...")
    app.run_polling()

if __name__ == "__main__":
    main()
