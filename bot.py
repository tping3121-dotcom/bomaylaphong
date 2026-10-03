import asyncio
import logging
import os
import psycopg2
from dotenv import load_dotenv
from telethon import Button, TelegramClient, events

# =====================================================
# 1. CẤU HÌNH
# =====================================================
load_dotenv()
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
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

# Khởi tạo Telethon Client (dùng session riêng cho tài khoản cá nhân)
client = TelegramClient('userbot_session', API_ID, API_HASH)

# Biến toàn cục quản lý tiến trình gửi định kỳ
broadcast_task = None

# =====================================================
# 2. KẾT NỐI DATABASE & KHỞI TẠO BẢNG
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
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_content (
                    id INT PRIMARY KEY,
                    content TEXT
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

def set_db_content(text: str):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM bot_content;")
            cur.execute("INSERT INTO bot_content (id, content) VALUES (1, %s);", (text,))
        conn.commit()
    finally:
        conn.close()

def get_db_content():
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT content FROM bot_content WHERE id = 1;")
            row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()

def delete_db_content():
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM bot_content;")
        conn.commit()
    finally:
        conn.close()

def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

# =====================================================
# 3. VÒNG LẶP GỬI THÔNG BÁO ĐỊNH KỲ (2 PHÚT/LẦN)
# =====================================================
async def periodic_broadcast():
    global broadcast_task
    try:
        while True:
            await asyncio.sleep(120)  # Đợi 2 phút
            message_text = get_db_content()
            groups = get_bot_groups()
            
            if not message_text or not groups:
                continue

            for group_id in groups:
                try:
                    # Gửi tin nhắn bằng chính tài khoản cá nhân của bạn
                    await client.send_message(group_id, message_text)
                    logger.info(f"Đã tự động gửi tin nhắn đến nhóm {group_id}")
                except Exception as e:
                    logger.error(f"Lỗi gửi tin tới nhóm {group_id}: {e}")
    except asyncio.CancelledError:
        logger.info("Tiến trình định kỳ đã bị dừng.")

# =====================================================
# 4. CÁC LỆNH ĐIỀU KHIỂN (Nhắn qua Saved Messages hoặc chat riêng với tài khoản)
# =====================================================
@client.on(events.NewMessage(pattern='/start'))
async def start(event):
    if not is_admin(event.sender_id):
        return
    await event.respond(
        "🤖 **USERBOT QUẢN LÝ THÔNG BÁO ĐANG CHẠY!**\n\n"
        "• `/addid [id_nhóm]` - Thêm nhóm\n"
        "• `/lsid` - Xem danh sách nhóm\n"
        "• `/themnd [nội dung]` - Cài đặt nội dung\n"
        "• `/nd` - Xem nội dung hiện tại\n"
        "• `/xoand` - Xóa nội dung\n"
        "• `/tb` - Bắt đầu tự động gửi (2 phút/lần)\n"
        "• `/stoptb` - Dừng tự động gửi",
        parse_mode='md'
    )

@client.on(events.NewMessage(pattern='/themnd'))
async def themnd_command(event):
    if not is_admin(event.sender_id):
        return
    text = event.raw_text.replace('/themnd', '').strip()
    if not text:
        await event.respond("⚠️ Vui lòng nhập nội dung. Ví dụ: `/themnd Chào mọi người`", parse_mode='md')
        return
    set_db_content(text)
    await event.respond(f"✅ **Đã lưu nội dung thông báo:**\n{text}", parse_mode='md')

@client.on(events.NewMessage(pattern='/nd'))
async def nd_command(event):
    if not is_admin(event.sender_id):
        return
    content = get_db_content()
    if not content:
        await event.respond("📋 Nội dung hiện tại đang trống.")
        return
    await event.respond(f"📋 **Nội dung hiện tại:**\n\n{content}", parse_mode='md')

@client.on(events.NewMessage(pattern='/xoand'))
async def xoand_command(event):
    if not is_admin(event.sender_id):
        return
    delete_db_content()
    await event.respond("🗑️ Đã xóa nội dung thông báo thành công!")

@client.on(events.NewMessage(pattern='/addid'))
async def addid_command(event):
    if not is_admin(event.sender_id):
        return
    parts = event.raw_text.split()
    if len(parts) < 2:
        await event.respond("⚠️️ Sử dụng: `/addid [ID_nhóm]`", parse_mode='md')
        return
    try:
        group_id = int(parts[1])
        save_group(group_id)
        chat = await client.get_entity(group_id)
        title = getattr(chat, 'title', 'Không có tên')
        await event.respond(f"✅ **Đã thêm nhóm thành công!**\n• Tên: {title}\n• ID: `{group_id}`", parse_mode='md')
    except Exception as e:
        await event.respond(f"❌ Lỗi thêm nhóm: {e}")

@client.on(events.NewMessage(pattern='/lsid'))
async def lsid_command(event):
    if not is_admin(event.sender_id):
        return
    groups = get_bot_groups()
    if not groups:
        await event.respond("📋 Danh sách nhóm trống.")
        return
    text = f"📋 **Danh sách nhóm ({len(groups)}):**\n\n"
    for gid in groups:
        try:
            chat = await client.get_entity(gid)
            title = getattr(chat, 'title', 'Không có tên')
            text += f"• {title} (`{gid}`)\n"
        except Exception:
            text += f"• (`{gid}`)\n"
    await event.respond(text, parse_mode='md')

@client.on(events.NewMessage(pattern='/tb'))
async def tb_command(event):
    global broadcast_task
    if not is_admin(event.sender_id):
        return
    
    text = get_db_content()
    groups = get_bot_groups()
    if not text:
        await event.respond("⚠️ Chưa có nội dung thông báo. Hãy dùng `/themnd` trước.")
        return
    if not groups:
        await event.respond("❌ Chưa có nhóm nào được thêm.")
        return

    if broadcast_task and not broadcast_task.done():
        await event.respond("⚠️ Tiến trình tự động gửi đang chạy rồi.")
        return

    # Gửi ngay lập tức lần đầu tiên bằng tài khoản của bạn
    success = 0
    for gid in groups:
        try:
            await client.send_message(gid, text)
            success += 1
        except Exception as e:
            logger.error(f"Lỗi gửi ngay tới {gid}: {e}")

    # Bắt đầu chạy vòng lặp nền
    broadcast_task = asyncio.create_task(periodic_broadcast())
    await event.respond(f"📢 **Đã bắt đầu tự động gửi tin bằng tài khoản của bạn!**\n- Thành công gửi ngay: {success} nhóm\n- Chu kỳ: 2 phút/lần", parse_mode='md')

@client.on(events.NewMessage(pattern='/stoptb'))
async def stoptb_command(event):
    global broadcast_task
    if not is_admin(event.sender_id):
        return
    if broadcast_task and not broadcast_task.done():
        broadcast_task.cancel()
        broadcast_task = None
        await event.respond("🛑 Đã dừng tiến trình tự động gửi thông báo!")
    else:
        await event.respond("⚠️ Không có tiến trình nào đang chạy.")

# =====================================================
# 5. KHỞI CHẠY
# =====================================================
def main():
    if not API_ID or not API_HASH:
        logger.error("Chưa cấu hình API_ID hoặc API_HASH trong tệp .env")
        return
    init_db()
    
    print("UserBot đang khởi động và kết nối Telegram...")
    # Khi chạy lệnh này lần đầu tiên, Telethon sẽ yêu cầu bạn nhập SĐT và mã OTP trực tiếp tại Terminal VPS
    client.start()
    print("UserBot đã đăng nhập thành công bằng tài khoản cá nhân!")
    
    client.run_until_disconnected()

if __name__ == '__main__':
    main()
