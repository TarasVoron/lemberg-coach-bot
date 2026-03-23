import os
import json
import random
import logging
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
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
DAILY_HOUR = int(os.getenv("DAILY_HOUR", "9"))
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

BERLIN = ZoneInfo("Europe/Berlin")

DATA_DIR = BASE_DIR / "data"
MOTIVATIONS_PATH = DATA_DIR / "motivations.json"
TASKS_PATH = DATA_DIR / "tasks.json"
TIPS_PATH = DATA_DIR / "tips.json"
USERS_PATH = BASE_DIR / "users.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("lemberg-coach-bot")

# Антиповтор для кнопки "Ще імпульс"
user_last_extra_motivation: dict[int, str] = {}


# ---------- Data helpers ----------
def load_json_list(path: Path, fallback: list[str]) -> list[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list) and data:
                return data
    except Exception as e:
        log.warning("Failed to load %s: %s", path.name, e)
    return fallback


def load_motivations() -> list[str]:
    return load_json_list(
        MOTIVATIONS_PATH,
        ["Сьогодні твоя сила — просто не зупинятися."]
    )


def load_tasks() -> list[str]:
    return load_json_list(
        TASKS_PATH,
        ["Зроби одну маленьку, але корисну дію для свого майбутнього."]
    )


def load_tips() -> list[str]:
    return load_json_list(
        TIPS_PATH,
        ["Краще маленький прогрес, ніж велике відкладання."]
    )


def load_users() -> set[int]:
    try:
        with open(USERS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return set(data if isinstance(data, list) else [])
    except Exception:
        return set()


def save_users(user_ids: set[int]) -> None:
    with open(USERS_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(list(user_ids)), f, ensure_ascii=False, indent=2)


def get_day_index() -> int:
    base = datetime(2025, 1, 1, tzinfo=BERLIN)
    now = datetime.now(BERLIN)
    return (now.date() - base.date()).days


def get_today_motivation() -> str:
    items = load_motivations()
    return items[get_day_index() % len(items)]


def get_today_task() -> str:
    items = load_tasks()
    return items[get_day_index() % len(items)]


def get_today_tip() -> str:
    items = load_tips()
    return items[get_day_index() % len(items)]


def today_content() -> dict[str, str]:
    return {
        "motivation": get_today_motivation(),
        "task": get_today_task(),
        "tip": get_today_tip(),
    }


def get_extra_motivation_for_user(user_id: int) -> str:
    items = load_motivations()
    if not items:
        return "Рухайся вперед."

    if len(items) == 1:
        result = items[0]
        user_last_extra_motivation[user_id] = result
        return result

    last = user_last_extra_motivation.get(user_id)
    available = [item for item in items if item != last]

    if not available:
        available = items

    result = random.choice(available)
    user_last_extra_motivation[user_id] = result
    return result


# ---------- UI builders ----------
def main_menu_kb() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("🔥 Отримати мотивацію", callback_data="get_motivation")],
        [InlineKeyboardButton("✨ Ще імпульс", callback_data="extra_motivation")],
        [InlineKeyboardButton("✅ Завдання дня", callback_data="get_task")],
        [InlineKeyboardButton("💡 Порада дня", callback_data="get_tip")],
    ]
    if MINI_APP_URL:
        buttons.append([InlineKeyboardButton("🧭 Відкрити Mini App", url=MINI_APP_URL)])
    return InlineKeyboardMarkup(buttons)


# ---------- Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_message:
        return

    chat_id = update.effective_chat.id
    users = load_users()
    if chat_id not in users:
        users.add(chat_id)
        save_users(users)
        log.info("New subscriber: %s", chat_id)

    text = (
        "👋 Привіт! Це <b>Lemberg Coach Bot</b>.\n\n"
        "Щодня ти отримуватимеш:\n"
        "• 🧠 <b>Мотивацію дня</b>\n"
        "• 🎯 <b>Завдання дня</b>\n"
        "• 💡 <b>Пораду дня</b>\n\n"
        "Натисни кнопку нижче."
    )
    await update.effective_message.reply_text(
        text=text,
        reply_markup=main_menu_kb(),
        parse_mode=ParseMode.HTML
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message:
        return

    await update.effective_message.reply_text(
        "Доступні команди:\n"
        "/start — меню\n"
        "/ping — перевірка\n"
        "/subscribe — підписка\n"
        "/unsubscribe — відписка\n"
        "/today — показати весь контент дня\n"
        "/help — допомога"
    )


async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message:
        return

    now = datetime.now(BERLIN).strftime("%Y-%m-%d %H:%M:%S")
    await update.effective_message.reply_text(f"✅ Пінг! {now} (Europe/Berlin)")


async def subscribe_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_message:
        return

    users = load_users()
    users.add(update.effective_chat.id)
    save_users(users)

    await update.effective_message.reply_text(
        f"✅ Підписано на щоденні повідомлення о {DAILY_HOUR:02d}:00."
    )


async def unsubscribe_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_message:
        return

    users = load_users()
    if update.effective_chat.id in users:
        users.remove(update.effective_chat.id)
        save_users(users)

    await update.effective_message.reply_text("❎ Відписано від щоденних повідомлень.")


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message:
        return

    content = today_content()
    text = (
        f"🧠 <b>Мотивація дня</b>\n{content['motivation']}\n\n"
        f"🎯 <b>Завдання дня</b>\n{content['task']}\n\n"
        f"💡 <b>Порада дня</b>\n{content['tip']}"
    )
    await update.effective_message.reply_text(
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_kb()
    )


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.message:
        return

    await query.answer()
    content = today_content()

    if query.data == "get_motivation":
        text = f"🧠 <b>Мотивація дня</b>\n\n{content['motivation']}"
    elif query.data == "extra_motivation":
        extra = get_extra_motivation_for_user(query.from_user.id)
        text = f"✨ <b>Імпульс</b>\n\n{extra}"
    elif query.data == "get_task":
        text = f"🎯 <b>Завдання дня</b>\n\n{content['task']}"
    elif query.data == "get_tip":
        text = f"💡 <b>Порада дня</b>\n\n{content['tip']}"
    else:
        text = "Невідома дія. Спробуй ще раз."

    await query.message.reply_text(
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_kb()
    )


# ---------- Scheduler ----------
async def daily_push(context: ContextTypes.DEFAULT_TYPE) -> None:
    content = today_content()
    users = load_users()
    if not users:
        return

    text = (
        f"🧠 <b>Мотивація дня</b>\n{content['motivation']}\n\n"
        f"🎯 <b>Завдання дня</b>\n{content['task']}\n\n"
        f"💡 <b>Порада дня</b>\n{content['tip']}"
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


def schedule_jobs(app: Application) -> None:
    run_time = datetime.now(BERLIN).replace(
        hour=DAILY_HOUR,
        minute=0,
        second=0,
        microsecond=0
    ).timetz()
    if app.job_queue is None:
        log.warning("JobQueue is not available. Daily push was not scheduled.")
        return
    app.job_queue.run_daily(daily_push, time=run_time, name="daily_push_berlin")


async def notify_owner_started(app: Application) -> None:
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
def main() -> None:
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("ping", ping_cmd))
    application.add_handler(CommandHandler("subscribe", subscribe_cmd))
    application.add_handler(CommandHandler("unsubscribe", unsubscribe_cmd))
    application.add_handler(CommandHandler("today", today_cmd))
    application.add_handler(CallbackQueryHandler(on_button))

    schedule_jobs(application)
    application.post_init = notify_owner_started

    log.info("Bot started. Press Ctrl+C to stop.")
    application.run_polling(close_loop=False)


if __name__ == "__main__":
    main()