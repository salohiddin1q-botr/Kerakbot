"""
Telegram bot — foydalanuvchi yozgan har bir xabarni bazaga saqlaydi.
Xabar bazaga tushgach, foydalanuvchi uni Telegram'da o'chirib tashlasa ham,
bizning bazamizdagi nusxasi saqlanib qoladi (chunki bot xabarni allaqachon
qabul qilib, diskka yozib ulgurgan bo'ladi).

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
# Token endi kodga yozilmaydi — Render'da "Environment Variable" sifatida kiritiladi
BOT_TOKEN = os.environ.get("BOT_TOKEN", "BU_YERGA_TOKENINGIZNI_QOYING")
DB_PATH = "messages.db"
PORT = int(os.environ.get("PORT", 10000))


# ==== RENDER UCHUN "UYG'OQ TURISH" SERVERI ====
# Render bepul tarifda web-service'ga HTTP so'rov kelib turishini kutadi.
# Shu kichik server "men ishlayapman" deb javob berib turadi,
# UptimeRobot kabi xizmat unga har necha daqiqada murojaat qilib tursa,
# bot uxlab qolmaydi.
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot ishlayapti.")

    def log_message(self, format, *args):
        pass  # ortiqcha loglarni o'chirish


def run_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthCheckHandler)
    server.serve_forever()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def init_db():
    """Baza va jadvalni yaratadi (agar mavjud bo'lmasa)."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            full_name TEXT,
            text TEXT NOT NULL,
            saved_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def save_message(user_id: int, username: str, full_name: str, text: str):
    """Bitta xabarni bazaga yozadi."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO messages (user_id, username, full_name, text, saved_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, username, full_name, text, datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    conn.close()


def get_user_messages(user_id: int, limit: int = 20):
    """Muayyan foydalanuvchining oxirgi xabarlarini qaytaradi."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT text, saved_at FROM messages WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


# ==== HANDLERLAR ====

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Salom! Menga yozgan har bir xabaringiz saqlanib qoladi — "
        "hatto chatdan o'chirib tashlasangiz ham.\n\n"
        "Buyruqlar:\n"
        "/mymessages — saqlangan xabarlaringizni ko'rish\n"
        "/help — yordam"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Menga istalgan matn yuboring — men uni saqlab qolaman.\n"
        "/mymessages — saqlangan xabarlaringiz ro'yxatini ko'rsatadi"
    )


async def save_and_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Foydalanuvchi yuborgan har qanday matnli xabarni saqlaydi."""
    user = update.effective_user
    text = update.message.text

    save_message(
        user_id=user.id,
        username=user.username or "",
        full_name=user.full_name or "",
        text=text,
    )

    await update.message.reply_text("✅ Xabaringiz saqlandi.")


async def my_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Foydalanuvchining oxirgi saqlangan xabarlarini ko'rsatadi."""
    user = update.effective_user
    rows = get_user_messages(user.id)

    if not rows:
        await update.message.reply_text("Sizdan hali saqlangan xabar yo'q.")
        return

    lines = ["📋 Saqlangan xabarlaringiz (oxirgi 20 tasi):\n"]
    for text, saved_at in rows:
        lines.append(f"🕒 {saved_at}\n{text}\n")

    # Telegram xabar uzunligi cheklangan, shuning uchun bo'lib yuboramiz
    full_text = "\n".join(lines)
    for i in range(0, len(full_text), 4000):
        await update.message.reply_text(full_text[i:i + 4000])


def main():
    init_db()

    # Keep-alive serverni alohida oqimda (thread) ishga tushiramiz
    threading.Thread(target=run_health_server, daemon=True).start()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("mymessages", my_messages))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, save_and_reply))

    logger.info("Bot ishga tushdi...")
    app.run_polling()


if __name__ == "__main__":
    main()
