"""
Telegram bot — foydalanuvchi yozgan har bir xabarni (matn, rasm, fayl)
bazaga saqlaydi. Xabar bazaga tushgach, foydalanuvchi uni Telegram'da
o'chirib tashlasa ham, bizning bazamizdagi nusxasi saqlanib qoladi.

Funksiyalar:
- Matn, rasm va fayllarni saqlash
- /mymessages — o'z xabarlarini ko'rish (ID bilan)
- /view <id> — rasm yoki faylni qayta ko'rish
- /delete <id> — bitta xabarni o'chirish
- /deleteall — barcha xabarlarini o'chirish
- /search <so'z> — xabarlar orasidan qidirish
- /admin — faqat admin uchun, barcha statistikani ko'rsatadi
- /viewuser, /deleteuser, /broadcast — admin uchun qo'shimcha imkoniyatlar
- /gencode, /unlock — vaqtinchalik (20 daqiqalik) admin kodi tizimi

O'rnatish va ishga tushirish yo'riqnomasi README.md faylida.
"""

import os
import sqlite3
import logging
import threading
import random
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import (
    Update,
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ==== SOZLAMALAR ====
BOT_TOKEN = os.environ.get("BOT_TOKEN", "BU_YERGA_TOKENINGIZNI_QOYING")
# Bot egasining Telegram ID raqami — doimiy admin, avtomatik huquqli.
# ID'ingizni bilish uchun Telegram'da @userinfobot ga yozing.
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
DB_PATH = "messages.db"
PORT = int(os.environ.get("PORT", 10000))
TEMP_ADMIN_MINUTES = 20
USER_LOGIN_MINUTES = 10


# ==== RENDER UCHUN "UYG'OQ TURISH" SERVERI ====
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot ishlayapti.")

    def log_message(self, format, *args):
        pass


def run_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthCheckHandler)
    server.serve_forever()


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ==== BAZA ====

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            full_name TEXT,
            content_type TEXT NOT NULL,   -- 'text', 'photo', 'document'
            text TEXT,                    -- matn yoki rasm/fayl tagidagi izoh
            file_id TEXT,                 -- rasm/fayl uchun Telegram file_id
            saved_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_codes (
            code TEXT PRIMARY KEY,
            expires_at TEXT NOT NULL,
            used INTEGER DEFAULT 0
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS temp_admins (
            user_id INTEGER PRIMARY KEY,
            expires_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS user_login_codes (
            code TEXT PRIMARY KEY,
            owner_user_id INTEGER NOT NULL,
            expires_at TEXT NOT NULL,
            used INTEGER DEFAULT 0
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS user_sessions (
            session_user_id INTEGER PRIMARY KEY,
            owner_user_id INTEGER NOT NULL,
            expires_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            task_text TEXT NOT NULL,
            remind_at TEXT NOT NULL,
            notified INTEGER DEFAULT 0
        )
        """
    )
    conn.commit()
    conn.close()


def save_message(user_id, username, full_name, content_type, text, file_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO messages
           (user_id, username, full_name, content_type, text, file_id, saved_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (user_id, username, full_name, content_type, text, file_id,
         local_now().isoformat(timespec="seconds")),
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def get_user_messages(user_id: int, limit: int = 20):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """SELECT id, content_type, text, saved_at FROM messages
           WHERE user_id = ? ORDER BY id DESC LIMIT ?""",
        (user_id, limit),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def delete_message(user_id: int, message_id: int) -> bool:
    """Faqat o'ziga tegishli xabarni o'chira oladi. Muvaffaqiyatli bo'lsa True."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM messages WHERE id = ? AND user_id = ?",
        (message_id, user_id),
    )
    deleted = cur.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


def delete_all_messages(user_id: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM messages WHERE user_id = ?", (user_id,))
    count = cur.rowcount
    conn.commit()
    conn.close()
    return count


def search_messages(user_id: int, keyword: str, limit: int = 20):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """SELECT id, content_type, text, saved_at FROM messages
           WHERE user_id = ? AND text LIKE ? ORDER BY id DESC LIMIT ?""",
        (user_id, f"%{keyword}%", limit),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def get_message_by_id(user_id: int, message_id: int):
    """Bitta xabarni to'liq ma'lumoti bilan qaytaradi (faqat egasiga)."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """SELECT content_type, text, file_id, saved_at FROM messages
           WHERE id = ? AND user_id = ?""",
        (message_id, user_id),
    )
    row = cur.fetchone()
    conn.close()
    return row


# ==== VAQTINCHALIK ADMIN KODLARI ====

def generate_admin_code(custom_code: str):
    """20 daqiqa amal qiladigan, admin o'zi tanlagan bir martalik kod yaratadi.
    Agar shu kod band bo'lsa (hali amal qilayotgan bo'lsa), None qaytaradi."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("SELECT expires_at, used FROM admin_codes WHERE code = ?", (custom_code,))
    row = cur.fetchone()
    if row:
        expires_at_str, used = row
        if not used and datetime.fromisoformat(expires_at_str) > datetime.now():
            conn.close()
            return None  # band, boshqa kod tanlash kerak

    expires_at = (datetime.now() + timedelta(minutes=TEMP_ADMIN_MINUTES)).isoformat()
    cur.execute(
        "INSERT OR REPLACE INTO admin_codes (code, expires_at, used) VALUES (?, ?, 0)",
        (custom_code, expires_at),
    )
    conn.commit()
    conn.close()
    return custom_code


def redeem_admin_code(code: str, user_id: int) -> bool:
    """Kodni tekshiradi, agar to'g'ri va muddati o'tmagan bo'lsa,
    o'sha foydalanuvchiga 20 daqiqalik admin huquqi beradi."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT expires_at, used FROM admin_codes WHERE code = ?", (code,))
    row = cur.fetchone()

    if not row:
        conn.close()
        return False

    expires_at_str, used = row
    if used or datetime.fromisoformat(expires_at_str) < datetime.now():
        conn.close()
        return False

    cur.execute("UPDATE admin_codes SET used = 1 WHERE code = ?", (code,))

    temp_expires_at = (datetime.now() + timedelta(minutes=TEMP_ADMIN_MINUTES)).isoformat()
    cur.execute(
        "INSERT OR REPLACE INTO temp_admins (user_id, expires_at) VALUES (?, ?)",
        (user_id, temp_expires_at),
    )
    conn.commit()
    conn.close()
    return True


def is_admin(user_id: int) -> bool:
    """Doimiy admin (ADMIN_ID) yoki muddati o'tmagan vaqtinchalik adminmi, tekshiradi."""
    if ADMIN_ID != 0 and user_id == ADMIN_ID:
        return True

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT expires_at FROM temp_admins WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()

    if not row:
        return False
    return datetime.fromisoformat(row[0]) > datetime.now()


def generate_user_login_code(owner_user_id: int, custom_code: str):
    """10 daqiqa amal qiladigan, foydalanuvchi o'zi tanlagan bir martalik kod
    yaratadi. Agar shu kod band bo'lsa, None qaytaradi."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute(
        "SELECT expires_at, used FROM user_login_codes WHERE code = ?", (custom_code,)
    )
    row = cur.fetchone()
    if row:
        expires_at_str, used = row
        if not used and datetime.fromisoformat(expires_at_str) > datetime.now():
            conn.close()
            return None  # band, boshqa kod tanlash kerak

    expires_at = (datetime.now() + timedelta(minutes=USER_LOGIN_MINUTES)).isoformat()
    cur.execute(
        "INSERT OR REPLACE INTO user_login_codes (code, owner_user_id, expires_at, used) VALUES (?, ?, ?, 0)",
        (custom_code, owner_user_id, expires_at),
    )
    conn.commit()
    conn.close()
    return custom_code


def redeem_user_login_code(code: str, session_user_id: int):
    """Kodni tekshiradi va to'g'ri bo'lsa, shu qurilmaga 10 daqiqalik
    sessiya ochadi. Muvaffaqiyatli bo'lsa owner_user_id qaytaradi, aks holda None."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT owner_user_id, expires_at, used FROM user_login_codes WHERE code = ?",
        (code,),
    )
    row = cur.fetchone()

    if not row:
        conn.close()
        return None

    owner_user_id, expires_at_str, used = row
    if used or datetime.fromisoformat(expires_at_str) < datetime.now():
        conn.close()
        return None

    cur.execute("UPDATE user_login_codes SET used = 1 WHERE code = ?", (code,))

    session_expires_at = (datetime.now() + timedelta(minutes=USER_LOGIN_MINUTES)).isoformat()
    cur.execute(
        "INSERT OR REPLACE INTO user_sessions (session_user_id, owner_user_id, expires_at) VALUES (?, ?, ?)",
        (session_user_id, owner_user_id, session_expires_at),
    )
    conn.commit()
    conn.close()
    return owner_user_id


def end_user_session(session_user_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM user_sessions WHERE session_user_id = ?", (session_user_id,))
    conn.commit()
    conn.close()


def get_effective_user_id(session_user_id: int) -> int:
    """Agar bu qurilmada faol sessiya bo'lsa (boshqa hisobga kirilgan bo'lsa),
    o'sha hisob egasining ID sini qaytaradi. Aks holda o'zinikini."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT owner_user_id, expires_at FROM user_sessions WHERE session_user_id = ?",
        (session_user_id,),
    )
    row = cur.fetchone()
    conn.close()

    if not row:
        return session_user_id

    owner_user_id, expires_at_str = row
    if datetime.fromisoformat(expires_at_str) < datetime.now():
        return session_user_id

    return owner_user_id


TASHKENT_OFFSET = timedelta(hours=5)


def local_now() -> datetime:
    """Render serveri UTC vaqtida ishlaydi, shuning uchun O'zbekiston
    (Toshkent, UTC+5) vaqtini hisoblab qaytaradi."""
    return datetime.utcnow() + TASHKENT_OFFSET


def add_task(user_id: int, task_text: str, remind_at: datetime) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO tasks (user_id, task_text, remind_at, notified) VALUES (?, ?, ?, 0)",
        (user_id, task_text, remind_at.isoformat()),
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def get_upcoming_tasks(user_id: int, limit: int = 20):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """SELECT id, task_text, remind_at FROM tasks
           WHERE user_id = ? AND notified = 0 ORDER BY remind_at ASC LIMIT ?""",
        (user_id, limit),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def delete_task(user_id: int, task_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id))
    deleted = cur.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


def get_due_tasks():
    """Vaqti kelgan, hali eslatilmagan barcha vazifalarni qaytaradi."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, user_id, task_text FROM tasks WHERE notified = 0 AND remind_at <= ?",
        (local_now().isoformat(),),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def mark_task_notified(task_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE tasks SET notified = 1 WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()


def get_all_user_ids():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT user_id FROM messages")
    ids = [row[0] for row in cur.fetchall()]
    conn.close()
    return ids


def get_messages_for_user_admin(target_user_id: int, limit: int = 20):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """SELECT id, content_type, text, saved_at FROM messages
           WHERE user_id = ? ORDER BY id DESC LIMIT ?""",
        (target_user_id, limit),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def delete_message_admin(target_user_id: int, message_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM messages WHERE id = ? AND user_id = ?",
        (message_id, target_user_id),
    )
    deleted = cur.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


def get_admin_stats():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM messages")
    total_messages = cur.fetchone()[0]
    cur.execute("SELECT COUNT(DISTINCT user_id) FROM messages")
    total_users = cur.fetchone()[0]
    cur.execute(
        """SELECT user_id, username, full_name, content_type, text, saved_at
           FROM messages ORDER BY id DESC LIMIT 15"""
    )
    recent = cur.fetchall()
    conn.close()
    return total_messages, total_users, recent


# ==== YORDAMCHI ====

def content_label(content_type: str) -> str:
    return {"text": "📝", "photo": "🖼", "document": "📎", "video": "🎬"}.get(content_type, "•")


# ==== HANDLERLAR ====

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📋 Mening xabarlarim", callback_data="btn_mymessages"),
            InlineKeyboardButton("🔍 Qidirish", callback_data="btn_search"),
        ],
        [
            InlineKeyboardButton("⏰ Vazifa qo'shish", callback_data="btn_addtask"),
            InlineKeyboardButton("🔑 Mening kodim", callback_data="btn_mycode"),
        ],
        [
            InlineKeyboardButton("❓ Yordam", callback_data="btn_help"),
        ],
    ])

    await update.message.reply_text(
        "Salom! Menga yozgan matn, rasm yoki fayllaringiz saqlanib qoladi — "
        "hatto chatdan o'chirib tashlasangiz ham.\n\n"
        "Quyidagi tugmalardan foydalaning yoki buyruq yozing:",
        reply_markup=keyboard,
    )


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inline tugmalar bosilganda ishlaydi."""
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    data = query.data

    if data == "btn_mymessages":
        effective_id = get_effective_user_id(user.id)
        rows = get_user_messages(effective_id)

        if not rows:
            await query.message.reply_text("Sizdan hali saqlangan xabar yo'q.")
            return

        lines = ["📋 Saqlangan xabarlaringiz (oxirgi 20 tasi):\n"]
        for msg_id, content_type, text, saved_at in rows:
            icon = content_label(content_type)
            shown_text = text if text else "(matnsiz)"
            lines.append(f"{icon} ID:{msg_id} | 🕒 {saved_at}\n{shown_text}\n")
        lines.append("Ko'rish uchun: /view <id>   |   O'chirish uchun: /delete <id>")

        full_text = "\n".join(lines)
        for i in range(0, len(full_text), 4000):
            await query.message.reply_text(full_text[i:i + 4000])

    elif data == "btn_search":
        await query.message.reply_text(
            "Qidiruv so'zini yozing, masalan:\n/search salom"
        )

    elif data == "btn_addtask":
        await query.message.reply_text(
            "Vazifani shu ko'rinishda yozing:\n"
            "/addtask 2026-07-20 14:00 Video montaj qilish"
        )

    elif data == "btn_mycode":
        await query.message.reply_text(
            "O'zingiz xohlagan parolni yozing, masalan:\n/mycode mening_parolim1"
        )

    elif data == "btn_help":
        await query.message.reply_text(
            "Menga istalgan matn, rasm, video yoki fayl yuboring — men uni saqlab qolaman.\n\n"
            "/mymessages — saqlangan xabarlaringiz ro'yxati (ID bilan)\n"
            "/view <id> — rasm yoki faylni qayta ko'rish, masalan: /view 5\n"
            "/delete <id> — masalan: /delete 5\n"
            "/deleteall — hammasini o'chiradi\n"
            "/search <so'z> — masalan: /search salom\n\n"
            "📱 Boshqa telefondan kirish:\n"
            "/mycode <parol> — o'zingiz parol o'ylab, shu yerda faollashtirasiz\n"
            "/login <parol> — boshqa telefonda shu parolni kiritasiz"
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Menga istalgan matn, rasm yoki fayl yuboring — men uni saqlab qolaman.\n\n"
        "/mymessages — saqlangan xabarlaringiz ro'yxati (ID bilan)\n"
        "/view <id> — rasm yoki faylni qayta ko'rish, masalan: /view 5\n"
        "/delete <id> — masalan: /delete 5\n"
        "/deleteall — hammasini o'chiradi\n"
        "/search <so'z> — masalan: /search salom\n\n"
        "📱 Boshqa telefondan kirish:\n"
        "/mycode <parol> — o'zingiz parol o'ylab, shu yerda faollashtirasiz\n"
        "/login <parol> — boshqa telefonda shu parolni kiritasiz\n\n"
        "📅 Vazifa va eslatmalar:\n"
        "/addtask 2026-07-20 14:00 Video montaj qilish\n"
        "/mytasks — rejalashtirilgan vazifalar\n"
        "/deltask <id> — vazifani o'chirish"
    )


async def save_and_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text
    effective_id = get_effective_user_id(user.id)

    new_id = save_message(
        user_id=effective_id, username=user.username or "", full_name=user.full_name or "",
        content_type="text", text=text, file_id=None,
    )
    await update.message.reply_text(f"✅ Xabaringiz saqlandi. (ID: {new_id})")


async def save_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    photo = update.message.photo[-1]
    caption = update.message.caption or ""
    effective_id = get_effective_user_id(user.id)

    new_id = save_message(
        user_id=effective_id, username=user.username or "", full_name=user.full_name or "",
        content_type="photo", text=caption, file_id=photo.file_id,
    )
    await update.message.reply_text(f"✅ Rasm saqlandi. (ID: {new_id})")


async def save_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    doc = update.message.document
    caption = update.message.caption or doc.file_name or ""
    effective_id = get_effective_user_id(user.id)

    new_id = save_message(
        user_id=effective_id, username=user.username or "", full_name=user.full_name or "",
        content_type="document", text=caption, file_id=doc.file_id,
    )
    await update.message.reply_text(f"✅ Fayl saqlandi. (ID: {new_id})")


async def save_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    video = update.message.video
    caption = update.message.caption or ""
    effective_id = get_effective_user_id(user.id)

    new_id = save_message(
        user_id=effective_id, username=user.username or "", full_name=user.full_name or "",
        content_type="video", text=caption, file_id=video.file_id,
    )
    await update.message.reply_text(f"✅ Video saqlandi. (ID: {new_id})")


async def my_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    effective_id = get_effective_user_id(user.id)
    rows = get_user_messages(effective_id)

    if not rows:
        await update.message.reply_text("Sizdan hali saqlangan xabar yo'q.")
        return

    lines = ["📋 Saqlangan xabarlaringiz (oxirgi 20 tasi):\n"]
    for msg_id, content_type, text, saved_at in rows:
        icon = content_label(content_type)
        shown_text = text if text else "(matnsiz)"
        lines.append(f"{icon} ID:{msg_id} | 🕒 {saved_at}\n{shown_text}\n")

    lines.append("Ko'rish uchun: /view <id>   |   O'chirish uchun: /delete <id>")

    full_text = "\n".join(lines)
    for i in range(0, len(full_text), 4000):
        await update.message.reply_text(full_text[i:i + 4000])


async def view_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not context.args:
        await update.message.reply_text(
            "Qaysi xabarni ko'rishni yozing, masalan: /view 5\n"
            "ID raqamini /mymessages orqali ko'rishingiz mumkin."
        )
        return

    try:
        message_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID raqam bo'lishi kerak, masalan: /view 5")
        return

    row = get_message_by_id(get_effective_user_id(user.id), message_id)
    if not row:
        await update.message.reply_text("Bunday ID topilmadi (yoki bu sizniki emas).")
        return

    content_type, text, file_id, saved_at = row

    if content_type == "text":
        await update.message.reply_text(f"📝 {saved_at}\n\n{text}")
    elif content_type == "photo":
        await update.message.reply_photo(photo=file_id, caption=text or None)
    elif content_type == "document":
        await update.message.reply_document(document=file_id, caption=text or None)
    elif content_type == "video":
        await update.message.reply_video(video=file_id, caption=text or None)


async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not context.args:
        await update.message.reply_text(
            "Qaysi xabarni o'chirishni yozing, masalan: /delete 5\n"
            "ID raqamini /mymessages orqali ko'rishingiz mumkin."
        )
        return

    try:
        message_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID raqam bo'lishi kerak, masalan: /delete 5")
        return

    success = delete_message(get_effective_user_id(user.id), message_id)
    if success:
        await update.message.reply_text(f"🗑 ID:{message_id} xabar o'chirildi.")
    else:
        await update.message.reply_text("Bunday ID topilmadi (yoki bu sizniki emas).")


async def delete_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    count = delete_all_messages(get_effective_user_id(user.id))
    await update.message.reply_text(f"🗑 {count} ta xabar o'chirildi.")


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not context.args:
        await update.message.reply_text("Qidiruv so'zini yozing, masalan: /search salom")
        return

    keyword = " ".join(context.args)
    rows = search_messages(get_effective_user_id(user.id), keyword)

    if not rows:
        await update.message.reply_text(f"'{keyword}' bo'yicha hech narsa topilmadi.")
        return

    lines = [f"🔍 '{keyword}' bo'yicha natijalar:\n"]
    for msg_id, content_type, text, saved_at in rows:
        icon = content_label(content_type)
        shown_text = text if text else "(matnsiz)"
        lines.append(f"{icon} ID:{msg_id} | 🕒 {saved_at}\n{shown_text}\n")

    full_text = "\n".join(lines)
    for i in range(0, len(full_text), 4000):
        await update.message.reply_text(full_text[i:i + 4000])


async def addtask_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Vazifa qo'shish: /addtask 2026-07-20 14:00 Video montaj qilish
    Vaqt ko'rsatilmasa, standart 09:00 olinadi: /addtask 2026-07-20 Video montaj"""
    user = update.effective_user
    effective_id = get_effective_user_id(user.id)

    if len(context.args) < 2:
        await update.message.reply_text(
            "To'g'ri format:\n"
            "/addtask KUN-OY-YIL SOAT:DAQIQA Vazifa matni\n"
            "Masalan: /addtask 2026-07-20 14:00 Video montaj qilish\n\n"
            "Vaqtsiz ham bo'ladi (standart 09:00):\n"
            "/addtask 2026-07-20 Video montaj qilish"
        )
        return

    date_str = context.args[0]
    remaining = context.args[1:]

    # Vaqt ko'rsatilganmi tekshirish (HH:MM formatida)
    time_str = "09:00"
    task_words = remaining
    if ":" in remaining[0] and len(remaining[0]) <= 5:
        time_str = remaining[0]
        task_words = remaining[1:]

    task_text = " ".join(task_words)
    if not task_text:
        await update.message.reply_text("Vazifa matnini ham yozing.")
        return

    try:
        remind_at = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    except ValueError:
        await update.message.reply_text(
            "Sana formati noto'g'ri. To'g'ri format: 2026-07-20 14:00"
        )
        return

    if remind_at < local_now():
        await update.message.reply_text("Bu sana allaqachon o'tib ketgan.")
        return

    task_id = add_task(effective_id, task_text, remind_at)
    await update.message.reply_text(
        f"✅ Vazifa qo'shildi (ID: {task_id})\n"
        f"🕒 {remind_at.strftime('%Y-%m-%d %H:%M')} da eslataman:\n{task_text}"
    )


async def mytasks_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    effective_id = get_effective_user_id(user.id)
    rows = get_upcoming_tasks(effective_id)

    if not rows:
        await update.message.reply_text("Sizda rejalashtirilgan vazifa yo'q.")
        return

    lines = ["📅 Rejalashtirilgan vazifalaringiz:\n"]
    for task_id, task_text, remind_at in rows:
        dt = datetime.fromisoformat(remind_at)
        lines.append(f"🔔 ID:{task_id} | {dt.strftime('%Y-%m-%d %H:%M')}\n{task_text}\n")

    lines.append("O'chirish uchun: /deltask <id>")

    full_text = "\n".join(lines)
    for i in range(0, len(full_text), 4000):
        await update.message.reply_text(full_text[i:i + 4000])


async def deltask_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    effective_id = get_effective_user_id(user.id)

    if not context.args:
        await update.message.reply_text("Vazifa ID sini yozing, masalan: /deltask 3")
        return

    try:
        task_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID raqam bo'lishi kerak.")
        return

    success = delete_task(effective_id, task_id)
    if success:
        await update.message.reply_text(f"🗑 ID:{task_id} vazifa o'chirildi.")
    else:
        await update.message.reply_text("Bunday ID topilmadi.")


async def check_due_tasks(context: ContextTypes.DEFAULT_TYPE):
    """Har daqiqada ishga tushib, vaqti kelgan vazifalarni eslatib turadi."""
    for task_id, user_id, task_text in get_due_tasks():
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"⏰ ESLATMA!\n\n{task_text}",
            )
        except Exception:
            pass
        mark_task_notified(task_id)


async def mycode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Har qanday foydalanuvchi — o'zi tanlagan parolni faollashtirib,
    boshqa telefondan o'z hisobiga kirishi mumkin."""
    user = update.effective_user
    owner_id = get_effective_user_id(user.id)

    if not context.args:
        await update.message.reply_text(
            "O'zingiz xohlagan parolni yozing, masalan:\n/mycode mening_parolim1"
        )
        return

    custom_code = context.args[0]
    result = generate_user_login_code(owner_id, custom_code)

    if result is None:
        await update.message.reply_text("❌ Bu parol hozir band. Boshqa parol tanlang.")
        return

    await update.message.reply_text(
        f"🔑 Parolingiz faollashtirildi: {custom_code}\n\n"
        f"Boshqa telefonda botga o'ting va yozing:\n/login {custom_code}\n\n"
        f"Bu parol {USER_LOGIN_MINUTES} daqiqa amal qiladi, faqat bir marta ishlatiladi.\n"
        f"⚠️ Bu parolni hech kimga bermang — kim kiritsa, sizning xabarlaringizni ko'ra oladi."
    )


async def login_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Boshqa telefonda — kodni kiritib, asl hisobga vaqtincha kirish."""
    user = update.effective_user

    if not context.args:
        await update.message.reply_text("Kodni yozing, masalan: /login 583920")
        return

    code = context.args[0]
    owner_id = redeem_user_login_code(code, user.id)

    if owner_id:
        await update.message.reply_text(
            f"✅ Kirish muvaffaqiyatli! {USER_LOGIN_MINUTES} daqiqa davomida "
            f"o'z xabarlaringizni ko'rishingiz mumkin.\n\n"
            f"/mymessages — xabarlaringizni ko'rish\n"
            f"/logout — sessiyani darhol tugatish"
        )
    else:
        await update.message.reply_text("❌ Kod noto'g'ri, eskirgan yoki allaqachon ishlatilgan.")


async def logout_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Faol sessiyani darhol tugatadi."""
    user = update.effective_user
    end_user_session(user.id)
    await update.message.reply_text("Sessiya tugatildi.")


async def gencode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Faqat asosiy admin (ADMIN_ID) uchun — o'zi tanlagan parolni faollashtiradi."""
    user = update.effective_user

    if ADMIN_ID == 0 or user.id != ADMIN_ID:
        await update.message.reply_text("Bu buyruq faqat bot egasi uchun.")
        return

    if not context.args:
        await update.message.reply_text(
            "O'zingiz xohlagan parolni yozing, masalan:\n/gencode mening_parolim1"
        )
        return

    custom_code = context.args[0]
    result = generate_admin_code(custom_code)

    if result is None:
        await update.message.reply_text("❌ Bu parol hozir band. Boshqa parol tanlang.")
        return

    await update.message.reply_text(
        f"🔑 Parolingiz faollashtirildi: {custom_code}\n\n"
        f"Boshqa telefonda botga yozing:\n/unlock {custom_code}\n\n"
        f"Bu parol {TEMP_ADMIN_MINUTES} daqiqa amal qiladi va faqat bir marta ishlatiladi."
    )


async def unlock_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Istalgan foydalanuvchi — kodni kiritib, vaqtinchalik admin bo'lish."""
    user = update.effective_user

    if not context.args:
        await update.message.reply_text("Kodni yozing, masalan: /unlock 583920")
        return

    code = context.args[0]
    success = redeem_admin_code(code, user.id)

    if success:
        await update.message.reply_text(
            f"✅ Admin huquqi berildi ({TEMP_ADMIN_MINUTES} daqiqaga).\n"
            f"/admin buyrug'ini ishlatishingiz mumkin."
        )
    else:
        await update.message.reply_text("❌ Kod noto'g'ri, eskirgan yoki allaqachon ishlatilgan.")


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin — barcha foydalanuvchilarga xabar yuboradi."""
    user = update.effective_user

    if not is_admin(user.id):
        await update.message.reply_text("Bu buyruq faqat admin uchun.")
        return

    if not context.args:
        await update.message.reply_text(
            "Yuboriladigan xabarni yozing, masalan:\n/broadcast Salom hammaga!"
        )
        return

    text = " ".join(context.args)
    user_ids = get_all_user_ids()

    sent, failed = 0, 0
    for uid in user_ids:
        try:
            await context.bot.send_message(chat_id=uid, text=f"📢 {text}")
            sent += 1
        except Exception:
            failed += 1

    await update.message.reply_text(
        f"✅ Yuborildi: {sent} ta foydalanuvchiga\n❌ Yetkazilmadi: {failed} ta"
    )


async def viewuser_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin — istalgan foydalanuvchining xabarlarini ko'rish."""
    user = update.effective_user

    if not is_admin(user.id):
        await update.message.reply_text("Bu buyruq faqat admin uchun.")
        return

    if not context.args:
        await update.message.reply_text(
            "Foydalanuvchi ID sini yozing, masalan: /viewuser 847362915"
        )
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID raqam bo'lishi kerak.")
        return

    rows = get_messages_for_user_admin(target_id)
    if not rows:
        await update.message.reply_text("Bu foydalanuvchidan xabar topilmadi.")
        return

    lines = [f"👤 Foydalanuvchi {target_id} xabarlari:\n"]
    for msg_id, content_type, text, saved_at in rows:
        icon = content_label(content_type)
        shown_text = (text or "(matnsiz)")[:200]
        lines.append(f"{icon} ID:{msg_id} | 🕒 {saved_at}\n{shown_text}\n")

    lines.append(f"O'chirish uchun: /deleteuser {target_id} <xabar_id>")

    full_text = "\n".join(lines)
    for i in range(0, len(full_text), 4000):
        await update.message.reply_text(full_text[i:i + 4000])


async def deleteuser_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin — istalgan foydalanuvchining bitta xabarini o'chirish."""
    user = update.effective_user

    if not is_admin(user.id):
        await update.message.reply_text("Bu buyruq faqat admin uchun.")
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "Foydalanuvchi ID va xabar ID sini yozing, masalan: /deleteuser 847362915 5"
        )
        return

    try:
        target_id = int(context.args[0])
        message_id = int(context.args[1])
    except ValueError:
        await update.message.reply_text("Ikkala qiymat ham raqam bo'lishi kerak.")
        return

    success = delete_message_admin(target_id, message_id)
    if success:
        await update.message.reply_text(f"🗑 Foydalanuvchi {target_id}'ning ID:{message_id} xabari o'chirildi.")
    else:
        await update.message.reply_text("Bunday xabar topilmadi.")


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not is_admin(user.id):
        await update.message.reply_text("Bu buyruq faqat admin uchun.")
        return

    total_messages, total_users, recent = get_admin_stats()

    lines = [
        "👑 ADMIN PANEL\n",
        f"Jami xabarlar: {total_messages}",
        f"Jami foydalanuvchilar: {total_users}\n",
        "Buyruqlar:",
        "/viewuser <user_id> — foydalanuvchi xabarlarini ko'rish",
        "/deleteuser <user_id> <xabar_id> — xabarni o'chirish",
        "/broadcast <matn> — barchaga xabar yuborish\n",
        "So'nggi 15 ta xabar:\n",
    ]
    for uid, username, full_name, content_type, text, saved_at in recent:
        icon = content_label(content_type)
        who = f"@{username}" if username else full_name or str(uid)
        shown_text = (text or "(matnsiz)")[:100]
        lines.append(f"{icon} {who} (ID:{uid}) | 🕒 {saved_at}\n{shown_text}\n")

    full_text = "\n".join(lines)
    for i in range(0, len(full_text), 4000):
        await update.message.reply_text(full_text[i:i + 4000])


async def post_init(application):
    """Bot ishga tushganda, Telegram'ning ☰ Menyu tugmasi uchun
    buyruqlar ro'yxatini o'rnatadi."""
    common_commands = [
        BotCommand("start", "Botni ishga tushirish"),
        BotCommand("help", "Yordam"),
        BotCommand("mymessages", "Saqlangan xabarlarimni ko'rish"),
        BotCommand("view", "Rasm/faylni qayta ko'rish"),
        BotCommand("delete", "Bitta xabarni o'chirish"),
        BotCommand("deleteall", "Barcha xabarlarni o'chirish"),
        BotCommand("search", "Xabarlar orasidan qidirish"),
        BotCommand("addtask", "Eslatma/vazifa qo'shish"),
        BotCommand("mytasks", "Eslatmalarimni ko'rish"),
        BotCommand("deltask", "Eslatmani o'chirish"),
        BotCommand("mycode", "Boshqa telefondan kirish uchun parol"),
        BotCommand("login", "Boshqa telefonda parolni kiritish"),
        BotCommand("logout", "Sessiyani tugatish"),
    ]

    # Barcha foydalanuvchilar uchun umumiy menyu
    await application.bot.set_my_commands(common_commands, scope=BotCommandScopeDefault())

    # Faqat admin uchun — qo'shimcha buyruqlar bilan to'liq menyu
    if ADMIN_ID != 0:
        admin_commands = common_commands + [
            BotCommand("admin", "Admin panel"),
            BotCommand("viewuser", "Foydalanuvchi xabarlarini ko'rish"),
            BotCommand("deleteuser", "Foydalanuvchi xabarini o'chirish"),
            BotCommand("broadcast", "Barchaga xabar yuborish"),
            BotCommand("gencode", "Vaqtinchalik admin parolini yaratish"),
        ]
        try:
            await application.bot.set_my_commands(
                admin_commands, scope=BotCommandScopeChat(chat_id=ADMIN_ID)
            )
        except Exception:
            pass  # admin hali botga /start bosmagan bo'lishi mumkin


def main():
    init_db()

    threading.Thread(target=run_health_server, daemon=True).start()

    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("mymessages", my_messages))
    app.add_handler(CommandHandler("view", view_command))
    app.add_handler(CommandHandler("delete", delete_command))
    app.add_handler(CommandHandler("deleteall", delete_all_command))
    app.add_handler(CommandHandler("search", search_command))
    app.add_handler(CommandHandler("mycode", mycode_command))
    app.add_handler(CommandHandler("login", login_command))
    app.add_handler(CommandHandler("logout", logout_command))
    app.add_handler(CommandHandler("addtask", addtask_command))
    app.add_handler(CommandHandler("mytasks", mytasks_command))
    app.add_handler(CommandHandler("deltask", deltask_command))
    app.add_handler(CommandHandler("gencode", gencode_command))
    app.add_handler(CommandHandler("unlock", unlock_command))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("viewuser", viewuser_command))
    app.add_handler(CommandHandler("deleteuser", deleteuser_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))

    app.add_handler(MessageHandler(filters.PHOTO, save_photo))
    app.add_handler(MessageHandler(filters.VIDEO, save_video))
    app.add_handler(MessageHandler(filters.Document.ALL, save_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, save_and_reply))

    # Har 60 soniyada vazifalarni tekshirib, vaqti kelganlarini eslatib turadi
    app.job_queue.run_repeating(check_due_tasks, interval=60, first=10)

    logger.info("Bot ishga tushdi...")
    app.run_polling()


if __name__ == "__main__":
    main()
