"""
Telegram bot — foydalanuvchi yozgan har bir xabarni (matn, rasm, fayl)
bazaga saqlaydi. Xabar bazaga tushgach, foydalanuvchi uni Telegram'da
o'chirib tashlasa ham, bizning bazamizdagi nusxasi saqlanib qoladi.

Funksiyalar:
- Matn, rasm va fayllarni saqlash
- /mymessages — o'z xabarlarini ko'rish (ID bilan)
- /delete <id> — bitta xabarni o'chirish
- /deleteall — barcha xabarlarini o'chirish
- /search <so'z> — xabarlar orasidan qidirish
- /admin — faqat bot egasi uchun, barcha statistikani ko'rsatadi

O'rnatish va ishga tushirish yo'riqnomasi README.md faylida.
"""

import os
import sqlite3
import logging
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ==== SOZLAMALAR ====
BOT_TOKEN = os.environ.get("BOT_TOKEN", "BU_YERGA_TOKENINGIZNI_QOYING")
# Bot egasining Telegram ID raqami — /admin buyrug'ini faqat shu odam ishlata oladi.
# ID'ingizni bilish uchun Telegram'da @userinfobot ga yozing.
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
DB_PATH = "messages.db"
PORT = int(os.environ.get("PORT", 10000))


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
         datetime.now().isoformat(timespec="seconds")),
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
    return {"text": "📝", "photo": "🖼", "document": "📎"}.get(content_type, "•")


# ==== HANDLERLAR ====

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Salom! Menga yozgan matn, rasm yoki fayllaringiz saqlanib qoladi — "
        "hatto chatdan o'chirib tashlasangiz ham.\n\n"
        "Buyruqlar:\n"
        "/mymessages — saqlangan xabarlaringizni ko'rish\n"
        "/view <id> — rasm yoki faylni qayta ko'rish\n"
        "/delete <id> — bitta xabarni o'chirish\n"
        "/deleteall — barcha xabarlaringizni o'chirish\n"
        "/search <so'z> — xabarlar orasidan qidirish\n"
        "/help — yordam"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Menga istalgan matn, rasm yoki fayl yuboring — men uni saqlab qolaman.\n\n"
        "/mymessages — saqlangan xabarlaringiz ro'yxati (ID bilan)\n"
        "/view <id> — rasm yoki faylni qayta ko'rish, masalan: /view 5\n"
        "/delete <id> — masalan: /delete 5\n"
        "/deleteall — hammasini o'chiradi\n"
        "/search <so'z> — masalan: /search salom"
    )


async def save_and_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Matnli xabarni saqlaydi."""
    user = update.effective_user
    text = update.message.text

    new_id = save_message(
        user_id=user.id, username=user.username or "", full_name=user.full_name or "",
        content_type="text", text=text, file_id=None,
    )
    await update.message.reply_text(f"✅ Xabaringiz saqlandi. (ID: {new_id})")


async def save_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Rasmni saqlaydi (eng katta o'lchamdagi versiyasini)."""
    user = update.effective_user
    photo = update.message.photo[-1]
    caption = update.message.caption or ""

    new_id = save_message(
        user_id=user.id, username=user.username or "", full_name=user.full_name or "",
        content_type="photo", text=caption, file_id=photo.file_id,
    )
    await update.message.reply_text(f"✅ Rasm saqlandi. (ID: {new_id})")


async def save_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Faylni saqlaydi."""
    user = update.effective_user
    doc = update.message.document
    caption = update.message.caption or doc.file_name or ""

    new_id = save_message(
        user_id=user.id, username=user.username or "", full_name=user.full_name or "",
        content_type="document", text=caption, file_id=doc.file_id,
    )
    await update.message.reply_text(f"✅ Fayl saqlandi. (ID: {new_id})")


async def my_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    rows = get_user_messages(user.id)

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

    success = delete_message(user.id, message_id)
    if success:
        await update.message.reply_text(f"🗑 ID:{message_id} xabar o'chirildi.")
    else:
        await update.message.reply_text("Bunday ID topilmadi (yoki bu sizniki emas).")


async def view_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Saqlangan rasm yoki faylni qayta yuboradi."""
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

    row = get_message_by_id(user.id, message_id)
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


async def delete_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    count = delete_all_messages(user.id)
    await update.message.reply_text(f"🗑 {count} ta xabar o'chirildi.")


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not context.args:
        await update.message.reply_text("Qidiruv so'zini yozing, masalan: /search salom")
        return

    keyword = " ".join(context.args)
    rows = search_messages(user.id, keyword)

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


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if ADMIN_ID == 0:
        await update.message.reply_text(
            "Admin ID sozlanmagan. Render'da ADMIN_ID environment variable qo'shing."
        )
        return

    if user.id != ADMIN_ID:
        await update.message.reply_text("Bu buyruq faqat bot egasi uchun.")
        return

    total_messages, total_users, recent = get_admin_stats()

    lines = [
        "👑 ADMIN PANEL\n",
        f"Jami xabarlar: {total_messages}",
        f"Jami foydalanuvchilar: {total_users}\n",
        "So'nggi 15 ta xabar:\n",
    ]
    for uid, username, full_name, content_type, text, saved_at in recent:
        icon = content_label(content_type)
        who = f"@{username}" if username else full_name or str(uid)
        shown_text = (text or "(matnsiz)")[:100]
        lines.append(f"{icon} {who} | 🕒 {saved_at}\n{shown_text}\n")

    full_text = "\n".join(lines)
    for i in range(0, len(full_text), 4000):
        await update.message.reply_text(full_text[i:i + 4000])


def main():
    init_db()

    threading.Thread(target=run_health_server, daemon=True).start()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("mymessages", my_messages))
    app.add_handler(CommandHandler("view", view_command))
    app.add_handler(CommandHandler("delete", delete_command))
    app.add_handler(CommandHandler("deleteall", delete_all_command))
    app.add_handler(CommandHandler("search", search_command))
    app.add_handler(CommandHandler("admin", admin_command))

    app.add_handler(MessageHandler(filters.PHOTO, save_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, save_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, save_and_reply))

    logger.info("Bot ishga tushdi...")
    app.run_polling()


if __name__ == "__main__":
    main()
