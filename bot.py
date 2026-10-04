import asyncio
import logging
import os
import psycopg2
from dotenv import load_dotenv
from telethon import TelegramClient, events

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

try:
    API_ID = int(API_ID) if API_ID else None
except ValueError:
    API_ID = None

client = TelegramClient('userbot_session', API_ID, API_HASH)

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
            # Thay đổi bảng nội dung để hỗ trợ nhiều dòng có thứ tự (SERIAL id)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_content (
                    id SERIAL PRIMARY KEY,
                    content TEXT
                );
            """)
        conn.commit()
        logger.info("Database đã sẵn sàng.")
        
        # Tự động nạp sẵn 6 ID nhóm mới vào DB nếu chưa có
        default_groups = [
            -1002275518129,
            -1002055223315,
            -1002484088604,
            -1002042998230,
            -1002601295906,
            -1004344547504
        ]
        with conn.cursor() as cur:
            for gid in default_groups:
                cur.execute(
                    "INSERT INTO bot_groups (group_id) VALUES (%s) ON CONFLICT (group_id) DO NOTHING;",
                    (gid,)
                )
        conn.commit()
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

def add_db_content(text: str):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO bot_content (content) VALUES (%s);", (text,))
        conn.commit()
    finally:
        conn.close()

def get_db_contents():
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, content FROM bot_content ORDER BY id ASC;")
            rows = cur.fetchall()
        return rows # Trả về list dạng [(1, 'nội dung 1'), (2, 'nội dung 2'), ...]
    finally:
        conn.close()

def delete_db_content_by_index(target_id: int):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM bot_content WHERE id = %s;", (target_id,))
        conn.commit()
    finally:
        conn.close()

def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

# =====================================================
# 3. VÒNG LẶP GỬI THÔNG BÁO ĐỊNH KỲ (30 giây/lần hoặc cấu hình)
# =====================================================
async def periodic_broadcast():
    global broadcast_task
    try:
        while True:
            await asyncio.sleep(30)  # Đợi 30 giây
            contents = get_db_contents()
            groups = get_bot_groups()
            
            if not contents or not groups:
                continue

            for group_id in groups:
                for row_id, message_text in contents:
                    try:
                        await client.send_message(group_id, message_text)
                        logger.info(f"Đã tự động gửi nội dung #{row_id} tới nhóm {group_id}")
                        await asyncio.sleep(1) # Tránh flood limit
                    except Exception as e:
                        logger.error(f"Lỗi gửi tin tới nhóm {group_id}: {e}")
    except asyncio.CancelledError:
        logger.info("Tiến trình định kỳ đã bị dừng.")

# =====================================================
# 4. CÁC LỆNH ĐIỀU KHIỂN
# =====================================================
@client.on(events.NewMessage(pattern='/start'))
async def start(event):
    if not is_admin(event.sender_id):
        return
    await event.respond(
        "🤖 **USERBOT QUẢN LÝ THÔNG BÁO ĐANG CHẠY!**\n\n"
        "• `/nhom @username` - Thêm nhóm bằng username\n"
        "• `/lsid` - Xem danh sách nhóm đã thêm\n"
        "• `/themnd [nội dung]` - Thêm nội dung thông báo mới\n"
        "• `/sttnd` - Xem danh sách nội dung kèm số thứ tự\n"
        "• `/xoand [số thứ tự]` - Xóa nội dung theo số thứ tự\n"
        "• `/batdau` - Bắt đầu tự động gửi (30 giây/lần)\n"
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
    add_db_content(text)
    contents = get_db_contents()
    await event.respond(f"✅ **Đã thêm nội dung thành công (STT {len(contents)}):**\n{text}", parse_mode='md')

@client.on(events.NewMessage(pattern='/sttnd'))
async def sttnd_command(event):
    if not is_admin(event.sender_id):
        return
    contents = get_db_contents()
    if not contents:
        await event.respond("📋 Danh sách nội dung tin nhắn đang trống.")
        return
    
    text = "📋 **Danh sách nội dung tin nhắn:**\n\n"
    for idx, (row_id, msg) in enumerate(contents, start=1):
        text += f"{idx}. {msg} (ID: {row_id})\n"
    await event.respond(text, parse_mode='md')

@client.on(events.NewMessage(pattern=r'/xoand (\d+)'))
async def xoand_command(event):
    if not is_admin(event.sender_id):
        return
    try:
        stt = int(event.pattern_match.group(1))
        contents = get_db_contents()
        if 1 <= stt <= len(contents):
            target_id, msg_content = contents[stt - 1]
            delete_db_content_by_index(target_id)
            await event.respond(f"🗑️ Đã xóa thành công nội dung số {stt}: `{msg_content}`", parse_mode='md')
        else:
            await event.respond("⚠️ Số thứ tự không hợp lệ! Hãy dùng `/sttnd` để xem lại danh sách.", parse_mode='md')
    except Exception as e:
        await event.respond(f"❌ Lỗi: {e}")

@client.on(events.NewMessage(pattern='/nhom'))
async def nhom_command(event):
    if not is_admin(event.sender_id):
        return
    parts = event.raw_text.split()
    if len(parts) < 2:
        await event.respond("⚠️ Sử dụng: `/nhom @username_nhom`", parse_mode='md')
        return
    
    group_username = parts[1]
    try:
        entity = await client.get_entity(group_username)
        group_id = entity.id
        save_group(group_id)
        title = getattr(entity, 'title', 'Không có tên')
        await event.respond(f"✅ **Đã thêm nhóm thành công!**\n• Tên: {title}\n• ID: `{group_id}`", parse_mode='md')
    except Exception as e:
        await event.respond(f"❌ Không tìm thấy nhóm hoặc tài khoản chưa tham gia nhóm này: {e}")

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

@client.on(events.NewMessage(pattern='/batdau'))
async def batdau_command(event):
    global broadcast_task
    if not is_admin(event.sender_id):
        return
    
    contents = get_db_contents()
    groups = get_bot_groups()
    if not contents:
        await event.respond("⚠️ Chưa có nội dung thông báo. Hãy dùng `/themnd` trước.")
        return
    if not groups:
        await event.respond("❌ Chưa có nhóm nào được thêm.")
        return

    if broadcast_task and not broadcast_task.done():
        await event.respond("⚠️ Tiến trình tự động gửi đang chạy rồi.")
        return

    broadcast_task = asyncio.create_task(periodic_broadcast())
    await event.respond(f"📢 **Đã bắt đầu tự động gửi tin bằng tài khoản của bạn!**\n- Tổng số nhóm: {len(groups)}\n- Chu kỳ: 30 giây/lần", parse_mode='md')

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
    client.start()
    print("UserBot đã đăng nhập thành công bằng tài khoản cá nhân!")
    
    client.run_until_disconnected()

if __name__ == '__main__':
    main()
