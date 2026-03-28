import os
import json
import random
import logging
import threading
import asyncio
from pathlib import Path
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from flask import Flask, request, jsonify

import stripe
from openai import OpenAI

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---------- Setup ----------
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID")
APP_BASE_URL = os.getenv("APP_BASE_URL", "").rstrip("/")
MINI_APP_URL = os.getenv("MINI_APP_URL", "").strip()
DAILY_HOUR = int(os.getenv("DAILY_HOUR", "8"))
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
PORT = int(os.getenv("PORT", "8080"))

required = {
    "BOT_TOKEN": BOT_TOKEN,
    "OPENAI_API_KEY": OPENAI_API_KEY,
    "STRIPE_SECRET_KEY": STRIPE_SECRET_KEY,
    "STRIPE_WEBHOOK_SECRET": STRIPE_WEBHOOK_SECRET,
    "STRIPE_PRICE_ID": STRIPE_PRICE_ID,
    "APP_BASE_URL": APP_BASE_URL,
}
missing = [k for k, v in required.items() if not v]
if missing:
    raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")

BERLIN = ZoneInfo("Europe/Berlin")

DATA_DIR = BASE_DIR / "data"
MOTIVATIONS_PATH = DATA_DIR / "motivations.json"
TASKS_PATH = DATA_DIR / "tasks.json"
TIPS_PATH = DATA_DIR / "tips.json"
USERS_PATH = BASE_DIR / "users.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("lemberg-coach-bot")

client = OpenAI(api_key=OPENAI_API_KEY)
stripe.api_key = STRIPE_SECRET_KEY

app_flask = Flask(__name__)
TG_APP: Application | None = None

# антиповтор "ще імпульс"
user_last_extra_motivation: dict[int, str] = {}

# ---------- Coach request filters ----------
COACH_KEYWORDS = (
    "план", "день", "ціль", "цілі", "дисцип", "фокус", "продуктив",
    "мотивац", "звич", "саморозвит", "прокраст", "відкладан",
    "рутин", "енергі", "стрес", "втом", "концентрац", "час",
    "завдан", "пріоритет", "результ", "розклад", "ранок", "вечір",
    "рішення", "сумнів", "страх", "дія", "коуч", "звички",
    "вигоран", "прогрес", "цілеспрям", "успіх", "поштовх"
)

OFFTOPIC_PATTERNS = (
    "що це", "what is this", "як цим користуватися", "how to use this",
    "що на фото", "опиши фото", "переклади", "translate",
    "скільки коштує", "де купити", "новини", "погода", "курс валют",
    "who is this", "хто це"
)


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


def today_str() -> str:
    return datetime.now(BERLIN).date().isoformat()


def parse_date(value: str) -> date:
    return date.fromisoformat(value)


def load_users_data() -> dict:
    try:
        with open(USERS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, list):
            result = {}
            today = today_str()
            for user_id in data:
                result[str(user_id)] = {
                    "streak": 1,
                    "last_seen": today,
                    "premium": False,
                    "messages_count": 0,
                    "stripe_customer_id": "",
                    "stripe_subscription_id": "",
                    "menu_message_id": 0,
                }
            save_users_data(result)
            return result

        if isinstance(data, dict):
            changed = False
            for uid, user in data.items():
                defaults = {
                    "streak": 0,
                    "last_seen": "",
                    "premium": False,
                    "messages_count": 0,
                    "stripe_customer_id": "",
                    "stripe_subscription_id": "",
                    "menu_message_id": 0,
                }
                for k, v in defaults.items():
                    if k not in user:
                        user[k] = v
                        changed = True
            if changed:
                save_users_data(data)
            return data

        return {}
    except Exception:
        return {}


def save_users_data(data: dict) -> None:
    with open(USERS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def ensure_user(user_id: int) -> dict:
    data = load_users_data()
    uid = str(user_id)

    if uid not in data:
        data[uid] = {
            "streak": 0,
            "last_seen": "",
            "premium": False,
            "messages_count": 0,
            "stripe_customer_id": "",
            "stripe_subscription_id": "",
            "menu_message_id": 0,
        }
        save_users_data(data)

    return data[uid]


def is_premium(user_id: int) -> bool:
    data = load_users_data()
    uid = str(user_id)
    return bool(data.get(uid, {}).get("premium", False))


def set_premium(
    user_id: int,
    value: bool = True,
    stripe_customer_id: str = "",
    stripe_subscription_id: str = "",
) -> None:
    data = load_users_data()
    uid = str(user_id)

    if uid not in data:
        data[uid] = {
            "streak": 0,
            "last_seen": "",
            "premium": value,
            "messages_count": 0,
            "stripe_customer_id": stripe_customer_id,
            "stripe_subscription_id": stripe_subscription_id,
            "menu_message_id": 0,
        }
    else:
        data[uid]["premium"] = value
        if stripe_customer_id:
            data[uid]["stripe_customer_id"] = stripe_customer_id
        if stripe_subscription_id:
            data[uid]["stripe_subscription_id"] = stripe_subscription_id

    save_users_data(data)


def find_user_id_by_subscription(subscription_id: str) -> int | None:
    data = load_users_data()
    for uid, user in data.items():
        if user.get("stripe_subscription_id") == subscription_id:
            return int(uid)
    return None


def increment_message_count(user_id: int) -> None:
    data = load_users_data()
    uid = str(user_id)

    if uid not in data:
        data[uid] = {
            "streak": 0,
            "last_seen": "",
            "premium": False,
            "messages_count": 1,
            "stripe_customer_id": "",
            "stripe_subscription_id": "",
            "menu_message_id": 0,
        }
    else:
        data[uid]["messages_count"] = int(data[uid].get("messages_count", 0)) + 1

    save_users_data(data)


def update_user_streak(user_id: int) -> tuple[int, bool]:
    data = load_users_data()
    uid = str(user_id)
    today = datetime.now(BERLIN).date()

    if uid not in data:
        data[uid] = {
            "streak": 1,
            "last_seen": today.isoformat(),
            "premium": False,
            "messages_count": 0,
            "stripe_customer_id": "",
            "stripe_subscription_id": "",
            "menu_message_id": 0,
        }
        save_users_data(data)
        return 1, False

    user = data[uid]
    last_seen_raw = user.get("last_seen", "")
    streak = int(user.get("streak", 0))
    lost = False

    if not last_seen_raw:
        streak = 1
    else:
        last_seen = parse_date(last_seen_raw)
        if last_seen == today:
            pass
        elif last_seen == today - timedelta(days=1):
            streak += 1
        else:
            streak = 1
            lost = True

    user["streak"] = streak
    user["last_seen"] = today.isoformat()
    data[uid] = user
    save_users_data(data)
    return streak, lost


def get_user_streak(user_id: int) -> int:
    data = load_users_data()
    uid = str(user_id)
    if uid not in data:
        return 0
    return int(data[uid].get("streak", 0))


def get_subscribed_user_ids() -> list[int]:
    data = load_users_data()
    return [int(uid) for uid in data.keys()]


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


def get_menu_message_id(user_id: int) -> int:
    data = load_users_data()
    uid = str(user_id)
    return int(data.get(uid, {}).get("menu_message_id", 0) or 0)


def set_menu_message_id(user_id: int, message_id: int) -> None:
    data = load_users_data()
    uid = str(user_id)

    if uid not in data:
        data[uid] = {
            "streak": 0,
            "last_seen": "",
            "premium": False,
            "messages_count": 0,
            "stripe_customer_id": "",
            "stripe_subscription_id": "",
            "menu_message_id": message_id,
        }
    else:
        data[uid]["menu_message_id"] = message_id

    save_users_data(data)


# ---------- Streak messaging ----------
def get_streak_message(streak: int) -> str:
    if streak >= 21:
        return "🔥 21 день — ти вже інша людина."
    elif streak >= 14:
        return "⚡ 14 днів — це вже система, не випадковість."
    elif streak >= 10:
        return "💥 10 днів — ти вже небезпечний для своїх старих звичок."
    elif streak >= 7:
        return "🚀 7 днів — ти вже будуєш нову версію себе."
    elif streak >= 5:
        return "📈 5 днів — дисципліна починає працювати."
    elif streak >= 3:
        return "🔥 3 дні — ти вже в ритмі."
    elif streak >= 1:
        return "✨ Початок — це вже більше, ніж у більшості."
    return ""


def get_comeback_message() -> str:
    return (
        "⚠️ Ти випав з ритму.\n\n"
        "Але повернутись — це сильніше, ніж не падати.\n\n"
        "Сьогодні = новий старт."
    )


# ---------- UI texts ----------
def build_main_menu_text(user_id: int, include_comeback: bool = False) -> str:
    streak = get_user_streak(user_id)
    streak_msg = get_streak_message(streak)
    premium_badge = "💎 <b>Premium активний</b>\n\n" if is_premium(user_id) else ""
    extra = get_comeback_message() + "\n\n" if include_comeback else ""

    return (
        extra
        + premium_badge
        + "👋 <b>Lemberg Coach</b>\n\n"
        + "Твій AI-коуч для дисципліни, фокусу і реальної дії.\n\n"
        + "Щодня ти отримуєш:\n"
        + "• 🧠 <b>Мотивацію дня</b>\n"
        + "• ✅ <b>Завдання дня</b>\n"
        + "• 💡 <b>Пораду дня</b>\n"
        + "• ✨ <b>Імпульс</b>\n\n"
        + f"🔥 <b>Твоя серія:</b> {streak} дн.\n"
        + f"{streak_msg}\n\n"
        + "Обери дію нижче або напиши мені повідомлення."
    )


def build_premium_text(user_id: int) -> str:
    if is_premium(user_id):
        return (
            "💎 <b>Lemberg Coach Premium</b>\n\n"
            "У тебе вже активний Premium.\n\n"
            "Що відкрито зараз:\n"
            "• GPT-коуч 24/7\n"
            "• персональні відповіді під твою ситуацію\n"
            "• швидкий доступ без повторної оплати\n\n"
            "Просто напиши мені повідомлення — і я відповім."
        )

    return (
        "🚀 <b>Lemberg Coach Premium</b>\n\n"
        "Це твій персональний AI-коуч, який допомагає:\n"
        "• не зливати день\n"
        "• тримати фокус\n"
        "• швидше приймати рішення\n"
        "• рухатись без хаосу\n\n"
        "<b>Що відкривається:</b>\n"
        "• GPT-коуч 24/7\n"
        "• персональні відповіді під твої цілі\n"
        "• майбутні premium-функції\n\n"
        "Натисни кнопку нижче для безпечної оплати."
    )


def build_motivation_text() -> str:
    return f"🧠 <b>Мотивація дня</b>\n\n{get_today_motivation()}"


def build_task_text() -> str:
    return f"✅ <b>Завдання дня</b>\n\n{get_today_task()}"


def build_tip_text() -> str:
    return f"💡 <b>Порада дня</b>\n\n{get_today_tip()}"


def build_extra_text(user_id: int) -> str:
    return f"✨ <b>Імпульс</b>\n\n{get_extra_motivation_for_user(user_id)}"


# ---------- Keyboards ----------
def main_menu_kb() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("🔥 Отримати мотивацію", callback_data="get_motivation")],
        [InlineKeyboardButton("✨ Ще імпульс", callback_data="extra_motivation")],
        [InlineKeyboardButton("✅ Завдання дня", callback_data="get_task")],
        [InlineKeyboardButton("💡 Порада дня", callback_data="get_tip")],
        [InlineKeyboardButton("🚀 Premium", callback_data="upgrade")],
    ]
    if MINI_APP_URL:
        buttons.append([InlineKeyboardButton("🧭 Відкрити Mini App", url=MINI_APP_URL)])
    return InlineKeyboardMarkup(buttons)


def back_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Назад у меню", callback_data="back_menu")]]
    )


def premium_kb(checkout_url: str | None = None, is_active: bool = False) -> InlineKeyboardMarkup:
    buttons = []

    if is_active:
        buttons.append([InlineKeyboardButton("⬅️ Назад у меню", callback_data="back_menu")])
    else:
        if checkout_url:
            buttons.append([InlineKeyboardButton("💳 Оформити Premium", url=checkout_url)])
        buttons.append([InlineKeyboardButton("⬅️ Назад у меню", callback_data="back_menu")])

    return InlineKeyboardMarkup(buttons)


# ---------- Request scope ----------
def is_coach_request(text: str) -> bool:
    low = (text or "").strip().lower()

    if not low:
        return False

    if any(p in low for p in OFFTOPIC_PATTERNS):
        return False

    if any(k in low for k in COACH_KEYWORDS):
        return True

    if len(low.split()) <= 6:
        return False

    return False


# ---------- GPT ----------
def ask_gpt(user_text: str) -> str:
    if not is_coach_request(user_text):
        return (
            "Я тут не як універсальний ChatGPT.\n\n"
            "Я працюю як коуч для:\n"
            "• дисципліни\n"
            "• фокусу\n"
            "• планування дня\n"
            "• звичок\n"
            "• особистого прогресу\n\n"
            "Опиши словами свою ціль, проблему або день, який хочеш зібрати — і я допоможу."
        )

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.7,
            max_tokens=220,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Ти персональний AI-коуч Lemberg Coach. "
                        "Відповідай українською. "
                        "Звертайся до користувача на 'ти'. "
                        "Ти НЕ універсальний ChatGPT. "
                        "Ти працюєш тільки як коуч з дисципліни, фокусу, продуктивності, "
                        "звичок, планування дня, особистого прогресу та прийняття рішень. "
                        "Якщо запит виходить за ці межі — коротко поверни розмову в коучинг. "
                        "Будь коротким, конкретним і корисним. "
                        "Не пиши довгі есе. "
                        "Якщо доречно, використовуй формат:\n"
                        "1) короткий висновок\n"
                        "2) 2-4 практичні кроки\n"
                        "3) 1 сильна фінальна фраза.\n"
                        "Не будь токсичним, не принижуй, не моралізуй."
                    ),
                },
                {"role": "user", "content": user_text},
            ],
        )
        return response.choices[0].message.content or "Не зупиняйся."
    except Exception as e:
        log.warning("OpenAI error: %s", e)
        return "⚠️ GPT тимчасово недоступний. Спробуй ще раз трохи пізніше."


# ---------- Stripe checkout ----------
def create_checkout_session(telegram_user_id: int) -> str:
    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[
            {
                "price": STRIPE_PRICE_ID,
                "quantity": 1,
            }
        ],
        success_url=f"{APP_BASE_URL}/payment-success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{APP_BASE_URL}/payment-cancelled",
        client_reference_id=str(telegram_user_id),
        metadata={"telegram_user_id": str(telegram_user_id)},
        allow_promotion_codes=True,
    )
    return session.url


# ---------- Stripe helpers ----------
def stripe_attr(obj, key: str, default=None):
    if obj is None:
        return default
    try:
        value = getattr(obj, key)
        return default if value is None else value
    except Exception:
        pass
    try:
        value = obj[key]
        return default if value is None else value
    except Exception:
        return default


# ---------- Telegram helpers ----------
async def clear_message_keyboard(chat_id: int, message_id: int):
    if TG_APP is None or not message_id:
        return

    try:
        await TG_APP.bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=None,
        )
    except Exception as e:
        log.warning("Failed to clear keyboard for %s/%s: %s", chat_id, message_id, e)


async def send_or_update_panel(chat_id: int, text: str, reply_markup=None):
    if TG_APP is None:
        return

    old_message_id = get_menu_message_id(chat_id)

    if old_message_id:
        try:
            await TG_APP.bot.edit_message_text(
                chat_id=chat_id,
                message_id=old_message_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            )
            return
        except Exception as e:
            err = str(e).lower()
            if "message is not modified" in err:
                return
            log.warning("Panel edit failed for %s: %s", chat_id, e)

    msg = await TG_APP.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup,
    )

    if old_message_id and old_message_id != msg.message_id:
        await clear_message_keyboard(chat_id, old_message_id)

    set_menu_message_id(chat_id, msg.message_id)


async def send_premium_activated_message(user_id: int):
    if TG_APP is None:
        return

    text = (
        "🎉 <b>Вітаємо! Premium активовано.</b>\n\n"
        "Тепер тобі доступний GPT-коуч 24/7.\n"
        "Просто напиши мені повідомлення — і я відповім."
    )

    try:
        await TG_APP.bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        log.warning("Failed to send premium activation message to %s: %s", user_id, e)

    try:
        await send_or_update_panel(
            user_id,
            build_main_menu_text(user_id),
            reply_markup=main_menu_kb(),
        )
    except Exception as e:
        log.warning("Failed to refresh main panel after premium activation: %s", e)


# ---------- Telegram handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat:
        return

    chat_id = update.effective_chat.id
    streak, lost = update_user_streak(chat_id)
    ensure_user(chat_id)

    await send_or_update_panel(
        chat_id,
        build_main_menu_text(chat_id, include_comeback=lost),
        reply_markup=main_menu_kb(),
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message:
        return

    await update.effective_message.reply_text(
        "Команди:\n"
        "/start — головне меню\n"
        "/ping — перевірка\n"
        "/today — весь контент дня\n"
        "/streak — твоя серія\n"
        "/upgrade — Premium\n"
        "/help — допомога\n\n"
        "Premium відкриває GPT-коуча 24/7."
    )


async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message:
        return
    now = datetime.now(BERLIN).strftime("%Y-%m-%d %H:%M:%S")
    await update.effective_message.reply_text(f"✅ Пінг! {now} (Europe/Berlin)")


async def streak_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_message:
        return

    streak = get_user_streak(update.effective_chat.id)
    if streak <= 0:
        text = "🔥 Серія ще не почалась. Натисни /start і починай ритм."
    else:
        text = f"🔥 <b>Твоя серія:</b> {streak} дн.\n{get_streak_message(streak)}"

    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_message:
        return

    content = today_content()
    streak = get_user_streak(update.effective_chat.id)

    text = (
        f"🧠 <b>Мотивація дня</b>\n{content['motivation']}\n\n"
        f"✅ <b>Завдання дня</b>\n{content['task']}\n\n"
        f"💡 <b>Порада дня</b>\n{content['tip']}\n\n"
        f"🔥 <b>Серія:</b> {streak} дн.\n"
        f"{get_streak_message(streak)}"
    )
    await update.effective_message.reply_text(
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_kb(),
    )


async def upgrade_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_message:
        return

    chat_id = update.effective_chat.id

    if is_premium(chat_id):
        await update.effective_message.reply_text(
            build_premium_text(chat_id),
            parse_mode=ParseMode.HTML,
            reply_markup=premium_kb(is_active=True),
        )
        return

    try:
        checkout_url = create_checkout_session(chat_id)
    except Exception as e:
        log.warning("Stripe checkout session error: %s", e)
        await update.effective_message.reply_text(
            "⚠️ Не вдалося створити сторінку оплати. Спробуй ще раз трохи пізніше."
        )
        return

    await update.effective_message.reply_text(
        build_premium_text(chat_id),
        parse_mode=ParseMode.HTML,
        reply_markup=premium_kb(checkout_url=checkout_url),
    )


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.message:
        return

    await query.answer()
    user_id = query.from_user.id
    current_panel_id = get_menu_message_id(user_id)
    clicked_message_id = query.message.message_id

    # Якщо натиснули кнопку на старому повідомленні —
    # прибираємо з нього клавіатуру і повертаємо до актуальної панелі
    if current_panel_id and clicked_message_id != current_panel_id:
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception as e:
            log.warning("Failed to remove keyboard from stale message: %s", e)

        await send_or_update_panel(
            user_id,
            build_main_menu_text(user_id),
            reply_markup=main_menu_kb(),
        )
        return

    if query.data == "back_menu":
        await send_or_update_panel(
            user_id,
            build_main_menu_text(user_id),
            reply_markup=main_menu_kb(),
        )
        return

    if query.data == "get_motivation":
        await send_or_update_panel(
            user_id,
            build_motivation_text(),
            reply_markup=back_menu_kb(),
        )
        return

    if query.data == "extra_motivation":
        await send_or_update_panel(
            user_id,
            build_extra_text(user_id),
            reply_markup=back_menu_kb(),
        )
        return

    if query.data == "get_task":
        await send_or_update_panel(
            user_id,
            build_task_text(),
            reply_markup=back_menu_kb(),
        )
        return

    if query.data == "get_tip":
        await send_or_update_panel(
            user_id,
            build_tip_text(),
            reply_markup=back_menu_kb(),
        )
        return

    if query.data == "upgrade":
        if is_premium(user_id):
            await send_or_update_panel(
                user_id,
                build_premium_text(user_id),
                reply_markup=premium_kb(is_active=True),
            )
            return

        try:
            checkout_url = create_checkout_session(user_id)
        except Exception as e:
            log.warning("Stripe checkout session error: %s", e)
            await send_or_update_panel(
                user_id,
                "⚠️ Не вдалося створити сторінку оплати. Спробуй ще раз трохи пізніше.",
                reply_markup=back_menu_kb(),
            )
            return

        await send_or_update_panel(
            user_id,
            build_premium_text(user_id),
            reply_markup=premium_kb(checkout_url=checkout_url),
        )
        return

    await send_or_update_panel(
        user_id,
        "Невідома дія. Спробуй ще раз.",
        reply_markup=main_menu_kb(),
    )


async def unsupported_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message:
        return

    await update.effective_message.reply_text(
        "📷 Я поки не аналізую фото в цьому боті.\n\n"
        "Опиши словами свою ситуацію, ціль або проблему — і я допоможу як коуч."
    )


async def chat_with_coach(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_message:
        return

    user_id = update.effective_chat.id
    text = (update.effective_message.text or "").strip()

    if not text or text.startswith("/"):
        return

    ensure_user(user_id)

    if not is_premium(user_id):
        await update.effective_message.reply_text(
            "🔒 GPT-коуч доступний тільки в Premium.\n\n"
            "Натисни /upgrade, щоб відкрити доступ."
        )
        return

    increment_message_count(user_id)
    reply = ask_gpt(text)
    await update.effective_message.reply_text(reply)


# ---------- Scheduler ----------
async def daily_push(context: ContextTypes.DEFAULT_TYPE) -> None:
    content = today_content()
    users = get_subscribed_user_ids()
    if not users:
        return

    text = (
        f"🧠 <b>Мотивація дня</b>\n{content['motivation']}\n\n"
        f"✅ <b>Завдання дня</b>\n{content['task']}\n\n"
        f"💡 <b>Порада дня</b>\n{content['tip']}"
    )

    for uid in users:
        try:
            await context.bot.send_message(
                chat_id=uid,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=main_menu_kb(),
            )
        except Exception as e:
            log.warning("Failed to send to %s: %s", uid, e)


def schedule_jobs(app: Application) -> None:
    run_time = datetime.now(BERLIN).replace(
        hour=DAILY_HOUR,
        minute=0,
        second=0,
        microsecond=0,
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


# ---------- Web ----------
@app_flask.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True}), 200


@app_flask.route("/payment-success", methods=["GET"])
def payment_success():
    return """
    <html>
      <body style="font-family:sans-serif;padding:40px">
        <h1>Оплата пройшла успішно ✅</h1>
        <p>Повернись у Telegram-бота. Premium активується автоматично за кілька секунд.</p>
      </body>
    </html>
    """, 200


@app_flask.route("/payment-cancelled", methods=["GET"])
def payment_cancelled():
    return """
    <html>
      <body style="font-family:sans-serif;padding:40px">
        <h1>Оплату скасовано</h1>
        <p>Ти можеш повернутись у Telegram і спробувати ще раз.</p>
      </body>
    </html>
    """, 200


@app_flask.route("/stripe-webhook", methods=["POST"])
def stripe_webhook():
    payload = request.get_data(as_text=False)
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=STRIPE_WEBHOOK_SECRET,
        )
    except ValueError:
        return "Invalid payload", 400
    except stripe.error.SignatureVerificationError:
        return "Invalid signature", 400

    event_type = event["type"]
    obj = event["data"]["object"]

    if event_type == "checkout.session.completed":
        client_reference_id = stripe_attr(obj, "client_reference_id", "")
        metadata = stripe_attr(obj, "metadata", {}) or {}
        customer_id = stripe_attr(obj, "customer", "")
        subscription_id = stripe_attr(obj, "subscription", "")

        tg_raw = None
        try:
            tg_raw = metadata.get("telegram_user_id")
        except Exception:
            tg_raw = None

        tg_raw = tg_raw or client_reference_id

        if tg_raw and str(tg_raw).isdigit():
            tg_user_id = int(tg_raw)

            set_premium(
                tg_user_id,
                True,
                stripe_customer_id=str(customer_id or ""),
                stripe_subscription_id=str(subscription_id or ""),
            )
            log.info("Premium enabled for Telegram user %s", tg_user_id)

            try:
                asyncio.run(send_premium_activated_message(tg_user_id))
            except Exception as e:
                log.warning("Failed to notify premium activation: %s", e)

    elif event_type == "customer.subscription.deleted":
        subscription_id = str(stripe_attr(obj, "id", "") or "")
        tg_user_id = find_user_id_by_subscription(subscription_id)
        if tg_user_id:
            set_premium(tg_user_id, False)
            log.info("Premium disabled for Telegram user %s (subscription deleted)", tg_user_id)

    elif event_type == "customer.subscription.updated":
        subscription_id = str(stripe_attr(obj, "id", "") or "")
        status = str(stripe_attr(obj, "status", "") or "")
        tg_user_id = find_user_id_by_subscription(subscription_id)

        if tg_user_id and status not in ("active", "trialing"):
            set_premium(tg_user_id, False)
            log.info("Premium disabled for Telegram user %s (status=%s)", tg_user_id, status)

    return "ok", 200


def run_web_server() -> None:
    app_flask.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)


# ---------- Main ----------
def main() -> None:
    global TG_APP

    web_thread = threading.Thread(target=run_web_server, daemon=True)
    web_thread.start()

    application = Application.builder().token(BOT_TOKEN).build()
    TG_APP = application

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("ping", ping_cmd))
    application.add_handler(CommandHandler("today", today_cmd))
    application.add_handler(CommandHandler("streak", streak_cmd))
    application.add_handler(CommandHandler("upgrade", upgrade_cmd))
    application.add_handler(CallbackQueryHandler(on_button))
    application.add_handler(MessageHandler(filters.PHOTO, unsupported_media))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat_with_coach))

    schedule_jobs(application)
    application.post_init = notify_owner_started

    log.info("Bot started. Press Ctrl+C to stop.")
    application.run_polling(close_loop=False)


if __name__ == "__main__":
    main()