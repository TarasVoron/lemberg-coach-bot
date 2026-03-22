import os
import json
import logging
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes
)

# ---------- Setup ----------
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Copy .env.example to .env and fill BOT_TOKEN.")

MINI_APP_URL = os.getenv("MINI_APP_URL", "").strip()
DAILY_HOUR = int(os.getenv("DAILY_HOUR", "9"))  # Europe/Berlin by default
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

BERLIN = ZoneInfo("Europe/Berlin")

QUOTES_PATH = BASE_DIR / "quotes.json"
USERS_PATH = BASE_DIR / "users.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("lemberg-coach-bot")

# ---------- Data helpers ----------
def load_quotes():
    with open(QUOTES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def load_users() -> set[int]:
    try:
        with open(USERS_PATH, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()

def save_users(user_ids: set[int]):
    with open(USERS_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(list(user_ids)), f, ensure_ascii=False, indent=2)

def today_content():
    """
    Вибір контенту детермінований від дати (Europe/Berlin),
    тож протягом дня він незмінний і однаковий для всіх.
    """
    quotes = load_quotes()
    if not quotes:
        return {"quote": "Сьогодні без цитати.", "action": "Додай новий контент у quotes.json."}

    base = datetime(2025, 1, 1, tzinfo=BERLIN)
    now = datetime.now(BERLIN)
    days = (now.date() - base.date()).days
    idx = days % len(quotes)
    return quotes[idx]

# ---------- UI builders ----------
def main_menu_kb():
    buttons = [
        [InlineKeyboardButton("🔥 Отримати мотивацію", callback_data="get_motivation")],
        [InlineKeyboardButton("✅ Завдання дня", callback_data="get_action")]
    ]
    if MINI_APP_URL:
        buttons.append([InlineKeyboardButton("🧭 Відкрити Mini App", url=MINI_APP_URL)])
    return InlineKeyboardMarkup(buttons)

# ---------- Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    users = load_users()
    if chat_id not in users:
        users.add(chat_id)
        save_users(users)
        log.info("New subscriber: %s", chat_id)

    text = (
        "👋 Привіт! Це <b>Lemberg Coach Bot</b>.\n\n"
        "Щодня ти отримуватимеш:\n"
        "• 🧠 <b>Цитату дня</b>\n"
        "• 🎯 <b>Завдання для дії</b>\n\n"
        "Натисни кнопку нижче, щоб почати."
    )
    await update.effective_message.reply_text(
        text, reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Доступні команди:\n"
        "/start — меню\n"
        "/ping — швидка перевірка\n"
        "/subscribe — підписатися на щоденні повідомлення\n"
        "/unsubscribe — відписатися від щоденних повідомлень\n"
        "/today — показати сьогоднішні цитату та завдання\n"
        "/help — допомога"
    )

async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(BERLIN).strftime("%Y-%m-%d %H:%M:%S")
    await update.effective_message.reply_text(f"✅ Пінг! {now} (Europe/Berlin)")

async def subscribe_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users = load_users()
    users.add(update.effective_chat.id)
    save_users(users)
    await update.effective_message.reply_text("✅ Підписано на щоденні повідомлення о %02d:00." % DAILY_HOUR)

async def unsubscribe_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users = load_users()
    if update.effective_chat.id in users:
        users.remove(update.effective_chat.id)
        save_users(users)
    await update.effective_message.reply_text("❎ Відписано від щоденних повідомлень.")

async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    content = today_content()
    text = (
        f"🧠 <b>Цитата дня</b>\n“{content['quote']}”\n\n"
        f"🎯 <b>Завдання дня</b>\n{content['action']}"
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=main_menu_kb())

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    content = today_content()

    if query.data == "get_motivation":
        text = f"🧠 <b>Цитата</b>\n\n“{content['quote']}”"
    elif query.data == "get_action":
        text = f"🎯 <b>Завдання</b>\n\n{content['action']}"
    else:
        text = "Невідома дія. Спробуй ще раз."

    try:
        await query.edit_message_text(
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu_kb()
        )
    except BadRequest as e:
        # Коли тицяють ту саму кнопку і текст не міняється — Telegram кидає 400 "Message is not modified".
        if "Message is not modified" in str(e):
            # Просто мовчки ігноруємо — все й так показано актуальне.
            return
        raise

# ---------- Scheduler ----------
async def daily_push(context: ContextTypes.DEFAULT_TYPE):
    content = today_content()
    users = load_users()
    if not users:
        return
    text = (
        f"🧠 <b>Цитата дня</b>\n“{content['quote']}”\n\n"
        f"🎯 <b>Завдання дня</b>\n{content['action']}"
    )
    for uid in users:
        try:
            await context.bot.send_message(
                chat_id=uid,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=main_menu_kb()
            )
        except Exception as e:
            log.warning("Failed to send to %s: %s", uid, e)

def schedule_jobs(app: Application):
    # розсилка щодня о DAILY_HOUR (час Берлін)
    run_time = datetime.now(BERLIN).replace(hour=DAILY_HOUR, minute=0, second=0, microsecond=0).timetz()
    app.job_queue.run_daily(daily_push, time=run_time, name="daily_push_berlin")

async def notify_owner_started(app: Application):
    if not OWNER_ID:
        return
    try:
        now = datetime.now(BERLIN).strftime("%Y-%m-%d %H:%M:%S")
        await app.bot.send_message(
            chat_id=OWNER_ID,
            text=f"✅ BOT запущений ({now}, Europe/Berlin).",
            disable_notification=True,
        )
    except Exception as e:
        log.warning("Owner notify failed: %s", e)

# ---------- Entrypoint ----------
def main():
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("ping", ping_cmd))
    application.add_handler(CommandHandler("subscribe", subscribe_cmd))
    application.add_handler(CommandHandler("unsubscribe", unsubscribe_cmd))
    application.add_handler(CommandHandler("today", today_cmd))
    application.add_handler(CallbackQueryHandler(on_button))

    schedule_jobs(application)
    application.post_init = notify_owner_started  # сповіщення власника після старту

    log.info("Bot started. Press Ctrl+C to stop.")
    application.run_polling(close_loop=False)

if __name__ == "__main__":
    main()