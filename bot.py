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

# антидубль імпульсу
user_last_extra_motivation: dict[int, str] = {}

# ---------- Scope / coach filters ----------
COACH_KEYWORDS = (
    "план", "день", "ціль", "цілі", "дисцип", "фокус", "продуктив",
    "мотивац", "звич", "саморозвит", "прокраст", "відкладан",
    "рутин", "енергі", "стрес", "втом", "концентрац", "час",
    "завдан", "пріоритет", "результ", "розклад", "ранок", "вечір",
    "рішення", "сумнів", "страх", "дія", "коуч", "звички",
    "вигоран", "прогрес", "цілеспрям", "успіх", "поштовх",
    "habit", "focus", "discipline", "productivity", "routine",
    "goal", "goals", "plan", "day plan", "motivation", "progress",
    "entscheidung", "fokus", "disziplin", "ziel", "ziele", "planen",
    "gewohnheit", "produktiv", "routine", "fortschritt"
)

OFFTOPIC_PATTERNS = (
    "що це", "what is this", "як цим користуватися", "how to use this",
    "що на фото", "опиши фото", "переклади", "translate",
    "скільки коштує", "де купити", "новини", "погода", "курс валют",
    "who is this", "хто це", "news", "weather", "price",
    "wie viel kostet", "wetter", "nachrichten"
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
                    "lang": "uk",
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
                    "lang": "uk",
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
            "lang": "uk",
        }
        save_users_data(data)

    return data[uid]


def get_user(user_id: int) -> dict:
    ensure_user(user_id)
    data = load_users_data()
    return data[str(user_id)]


def update_user_field(user_id: int, key: str, value) -> None:
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
            "lang": "uk",
        }
    data[uid][key] = value
    save_users_data(data)


def get_menu_message_id(user_id: int) -> int:
    return int(get_user(user_id).get("menu_message_id", 0) or 0)


def set_menu_message_id(user_id: int, message_id: int) -> None:
    update_user_field(user_id, "menu_message_id", int(message_id))


def get_user_lang(user_id: int) -> str:
    return str(get_user(user_id).get("lang", "uk"))


def set_user_lang(user_id: int, lang: str) -> None:
    if lang not in ("uk", "en", "de"):
        return
    update_user_field(user_id, "lang", lang)


def is_premium(user_id: int) -> bool:
    return bool(get_user(user_id).get("premium", False))


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
            "lang": "uk",
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
            "lang": "uk",
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
            "lang": "uk",
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
    return int(get_user(user_id).get("streak", 0))


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


# ---------- i18n ----------
TEXTS = {
    "uk": {
        "coach_scope_reply": (
            "Я тут не як універсальний ChatGPT.\n\n"
            "Я працюю як коуч для:\n"
            "• дисципліни\n"
            "• фокусу\n"
            "• планування дня\n"
            "• звичок\n"
            "• особистого прогресу\n\n"
            "Опиши словами свою ціль, проблему або день, який хочеш зібрати — і я допоможу."
        ),
        "photo_reply": (
            "📷 Я поки не аналізую фото в цьому боті.\n\n"
            "Опиши словами свою ситуацію, ціль або проблему — і я допоможу як коуч."
        ),
        "premium_locked": (
            "🔒 GPT-коуч доступний тільки в Premium.\n\n"
            "Натисни /upgrade, щоб відкрити доступ."
        ),
        "payment_error": "⚠️ Не вдалося створити сторінку оплати. Спробуй ще раз трохи пізніше.",
        "gpt_error": "⚠️ GPT тимчасово недоступний. Спробуй ще раз трохи пізніше.",
        "premium_activated": (
            "🎉 <b>Вітаємо! Premium активовано.</b>\n\n"
            "Тепер тобі доступний GPT-коуч 24/7.\n"
            "Просто напиши мені повідомлення — і я відповім."
        ),
        "back_to_menu": "⬅️ Назад у меню",
        "get_motivation": "🔥 Отримати мотивацію",
        "extra_push": "✨ Ще імпульс",
        "task_day": "✅ Завдання дня",
        "tip_day": "💡 Порада дня",
        "premium_btn": "🚀 Premium",
        "change_lang": "🌍 Мова",
        "open_mini_app": "🧭 Відкрити Mini App",
        "buy_premium": "💳 Оформити Premium",
        "language_title": (
            "🌍 <b>Обери мову</b>\n\n"
            "Зараз доступні:\n"
            "• Українська\n"
            "• English\n"
            "• Deutsch"
        ),
    },
    "en": {
        "coach_scope_reply": (
            "I’m not here as a universal ChatGPT.\n\n"
            "I work as a coach for:\n"
            "• discipline\n"
            "• focus\n"
            "• day planning\n"
            "• habits\n"
            "• personal progress\n\n"
            "Describe your goal, problem, or the day you want to organize — and I’ll help."
        ),
        "photo_reply": (
            "📷 I don’t analyze photos in this bot yet.\n\n"
            "Describe your situation, goal, or problem in words — and I’ll help as a coach."
        ),
        "premium_locked": (
            "🔒 GPT coach is available only in Premium.\n\n"
            "Tap /upgrade to unlock access."
        ),
        "payment_error": "⚠️ Could not create the payment page. Please try again later.",
        "gpt_error": "⚠️ GPT is temporarily unavailable. Please try again a bit later.",
        "premium_activated": (
            "🎉 <b>Congrats! Premium is activated.</b>\n\n"
            "GPT coach 24/7 is now available.\n"
            "Just send me a message — and I’ll reply."
        ),
        "back_to_menu": "⬅️ Back to menu",
        "get_motivation": "🔥 Get motivation",
        "extra_push": "✨ Extra push",
        "task_day": "✅ Task of the day",
        "tip_day": "💡 Tip of the day",
        "premium_btn": "🚀 Premium",
        "change_lang": "🌍 Language",
        "open_mini_app": "🧭 Open Mini App",
        "buy_premium": "💳 Get Premium",
        "language_title": (
            "🌍 <b>Choose a language</b>\n\n"
            "Available now:\n"
            "• Українська\n"
            "• English\n"
            "• Deutsch"
        ),
    },
    "de": {
        "coach_scope_reply": (
            "Ich bin hier nicht als universelles ChatGPT.\n\n"
            "Ich arbeite als Coach für:\n"
            "• Disziplin\n"
            "• Fokus\n"
            "• Tagesplanung\n"
            "• Gewohnheiten\n"
            "• persönlichen Fortschritt\n\n"
            "Beschreibe dein Ziel, Problem oder deinen Tag — und ich helfe dir."
        ),
        "photo_reply": (
            "📷 Ich analysiere in diesem Bot noch keine Fotos.\n\n"
            "Beschreibe dein Ziel, deine Situation oder dein Problem in Worten — und ich helfe dir als Coach."
        ),
        "premium_locked": (
            "🔒 Der GPT-Coach ist nur in Premium verfügbar.\n\n"
            "Tippe /upgrade, um den Zugang freizuschalten."
        ),
        "payment_error": "⚠️ Die Zahlungsseite konnte nicht erstellt werden. Bitte versuche es später erneut.",
        "gpt_error": "⚠️ GPT ist vorübergehend nicht verfügbar. Bitte versuche es später erneut.",
        "premium_activated": (
            "🎉 <b>Glückwunsch! Premium ist aktiviert.</b>\n\n"
            "Der GPT-Coach 24/7 ist jetzt verfügbar.\n"
            "Schreib mir einfach eine Nachricht — ich antworte."
        ),
        "back_to_menu": "⬅️ Zurück zum Menü",
        "get_motivation": "🔥 Motivation",
        "extra_push": "✨ Extra Impuls",
        "task_day": "✅ Tagesaufgabe",
        "tip_day": "💡 Tipp des Tages",
        "premium_btn": "🚀 Premium",
        "change_lang": "🌍 Sprache",
        "open_mini_app": "🧭 Mini App öffnen",
        "buy_premium": "💳 Premium holen",
        "language_title": (
            "🌍 <b>Sprache wählen</b>\n\n"
            "Jetzt verfügbar:\n"
            "• Українська\n"
            "• English\n"
            "• Deutsch"
        ),
    },
}


def t(user_id: int, key: str) -> str:
    lang = get_user_lang(user_id)
    return TEXTS.get(lang, TEXTS["uk"]).get(key, TEXTS["uk"].get(key, key))


# ---------- Streak messaging ----------
def get_streak_message(streak: int, lang: str = "uk") -> str:
    if lang == "en":
        if streak >= 21:
            return "🔥 21 days — you are already becoming a different person."
        elif streak >= 14:
            return "⚡ 14 days — this is already a system, not an accident."
        elif streak >= 10:
            return "💥 10 days — your old habits are already under pressure."
        elif streak >= 7:
            return "🚀 7 days — you’re building a new version of yourself."
        elif streak >= 5:
            return "📈 5 days — discipline is starting to work."
        elif streak >= 3:
            return "🔥 3 days — you’re already in rhythm."
        elif streak >= 1:
            return "✨ Starting is already more than most people do."
        return ""

    if lang == "de":
        if streak >= 21:
            return "🔥 21 Tage — du wirst bereits zu einer neuen Version von dir."
        elif streak >= 14:
            return "⚡ 14 Tage — das ist schon ein System, kein Zufall."
        elif streak >= 10:
            return "💥 10 Tage — deine alten Gewohnheiten geraten unter Druck."
        elif streak >= 7:
            return "🚀 7 Tage — du baust eine neue Version von dir auf."
        elif streak >= 5:
            return "📈 5 Tage — Disziplin beginnt zu wirken."
        elif streak >= 3:
            return "🔥 3 Tage — du bist schon im Rhythmus."
        elif streak >= 1:
            return "✨ Anfangen ist bereits mehr als die meisten tun."
        return ""

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


def get_comeback_message(lang: str = "uk") -> str:
    if lang == "en":
        return (
            "⚠️ You fell out of rhythm.\n\n"
            "But coming back is stronger than never falling.\n\n"
            "Today = a new start."
        )
    if lang == "de":
        return (
            "⚠️ Du bist aus dem Rhythmus gefallen.\n\n"
            "Aber zurückzukommen ist stärker, als nie zu fallen.\n\n"
            "Heute = ein neuer Start."
        )
    return (
        "⚠️ Ти випав з ритму.\n\n"
        "Але повернутись — це сильніше, ніж не падати.\n\n"
        "Сьогодні = новий старт."
    )


# ---------- UI texts ----------
def build_main_menu_text(user_id: int, include_comeback: bool = False) -> str:
    lang = get_user_lang(user_id)
    streak = get_user_streak(user_id)
    streak_msg = get_streak_message(streak, lang=lang)
    premium_badge = ""

    if lang == "en":
        premium_badge = "💎 <b>Premium active</b>\n\n" if is_premium(user_id) else ""
        extra = get_comeback_message(lang) + "\n\n" if include_comeback else ""
        return (
            extra
            + premium_badge
            + "👋 <b>Lemberg Coach</b>\n\n"
            + "Your AI coach for discipline, focus, and real action.\n\n"
            + "Every day you get:\n"
            + "• 🧠 <b>Motivation of the day</b>\n"
            + "• ✅ <b>Task of the day</b>\n"
            + "• 💡 <b>Tip of the day</b>\n"
            + "• ✨ <b>Extra push</b>\n\n"
            + f"🔥 <b>Your streak:</b> {streak} day(s)\n"
            + f"{streak_msg}\n\n"
            + "Choose an action below or send me a message."
        )

    if lang == "de":
        premium_badge = "💎 <b>Premium aktiv</b>\n\n" if is_premium(user_id) else ""
        extra = get_comeback_message(lang) + "\n\n" if include_comeback else ""
        return (
            extra
            + premium_badge
            + "👋 <b>Lemberg Coach</b>\n\n"
            + "Dein AI-Coach für Disziplin, Fokus und echte Handlung.\n\n"
            + "Jeden Tag bekommst du:\n"
            + "• 🧠 <b>Motivation des Tages</b>\n"
            + "• ✅ <b>Tagesaufgabe</b>\n"
            + "• 💡 <b>Tipp des Tages</b>\n"
            + "• ✨ <b>Impuls</b>\n\n"
            + f"🔥 <b>Deine Serie:</b> {streak} Tag(e)\n"
            + f"{streak_msg}\n\n"
            + "Wähle unten eine Aktion oder schreibe mir eine Nachricht."
        )

    premium_badge = "💎 <b>Premium активний</b>\n\n" if is_premium(user_id) else ""
    extra = get_comeback_message(lang) + "\n\n" if include_comeback else ""
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


def build_motivation_text(user_id: int) -> str:
    lang = get_user_lang(user_id)
    body = get_today_motivation()
    if lang == "en":
        return f"🧠 <b>Motivation of the day</b>\n\n{body}"
    if lang == "de":
        return f"🧠 <b>Motivation des Tages</b>\n\n{body}"
    return f"🧠 <b>Мотивація дня</b>\n\n{body}"


def build_extra_text(user_id: int) -> str:
    lang = get_user_lang(user_id)
    body = get_extra_motivation_for_user(user_id)
    if lang == "en":
        return f"✨ <b>Extra push</b>\n\n{body}"
    if lang == "de":
        return f"✨ <b>Impuls</b>\n\n{body}"
    return f"✨ <b>Імпульс</b>\n\n{body}"


def build_task_text(user_id: int) -> str:
    lang = get_user_lang(user_id)
    body = get_today_task()
    if lang == "en":
        return f"✅ <b>Task of the day</b>\n\n{body}"
    if lang == "de":
        return f"✅ <b>Tagesaufgabe</b>\n\n{body}"
    return f"✅ <b>Завдання дня</b>\n\n{body}"


def build_tip_text(user_id: int) -> str:
    lang = get_user_lang(user_id)
    body = get_today_tip()
    if lang == "en":
        return f"💡 <b>Tip of the day</b>\n\n{body}"
    if lang == "de":
        return f"💡 <b>Tipp des Tages</b>\n\n{body}"
    return f"💡 <b>Порада дня</b>\n\n{body}"


def build_premium_text(user_id: int) -> str:
    lang = get_user_lang(user_id)

    if lang == "en":
        if is_premium(user_id):
            return (
                "💎 <b>Lemberg Coach Premium</b>\n\n"
                "Your Premium is already active.\n\n"
                "What is unlocked now:\n"
                "• GPT coach 24/7\n"
                "• personal replies for your situation\n"
                "• fast access without repeat payment\n\n"
                "Just send me a message — and I’ll reply."
            )
        return (
            "🚀 <b>Lemberg Coach Premium</b>\n\n"
            "This is your personal AI coach that helps you:\n"
            "• stop wasting the day\n"
            "• keep focus\n"
            "• make decisions faster\n"
            "• move without chaos\n\n"
            "<b>What unlocks:</b>\n"
            "• GPT coach 24/7\n"
            "• personal replies for your goals\n"
            "• future premium features\n\n"
            "Tap the button below for secure payment."
        )

    if lang == "de":
        if is_premium(user_id):
            return (
                "💎 <b>Lemberg Coach Premium</b>\n\n"
                "Dein Premium ist bereits aktiv.\n\n"
                "Jetzt freigeschaltet:\n"
                "• GPT-Coach 24/7\n"
                "• persönliche Antworten für deine Situation\n"
                "• schneller Zugriff ohne erneute Zahlung\n\n"
                "Schreib mir einfach — ich antworte."
            )
        return (
            "🚀 <b>Lemberg Coach Premium</b>\n\n"
            "Das ist dein persönlicher AI-Coach, der dir hilft:\n"
            "• den Tag nicht zu verlieren\n"
            "• den Fokus zu halten\n"
            "• schneller Entscheidungen zu treffen\n"
            "• ohne Chaos voranzukommen\n\n"
            "<b>Was freigeschaltet wird:</b>\n"
            "• GPT-Coach 24/7\n"
            "• persönliche Antworten auf deine Ziele\n"
            "• zukünftige Premium-Funktionen\n\n"
            "Tippe unten für eine sichere Zahlung."
        )

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


def build_today_text(user_id: int) -> str:
    lang = get_user_lang(user_id)
    content = today_content()
    streak = get_user_streak(user_id)
    streak_text = get_streak_message(streak, lang=lang)

    if lang == "en":
        return (
            f"🧠 <b>Motivation of the day</b>\n{content['motivation']}\n\n"
            f"✅ <b>Task of the day</b>\n{content['task']}\n\n"
            f"💡 <b>Tip of the day</b>\n{content['tip']}\n\n"
            f"🔥 <b>Your streak:</b> {streak} day(s)\n"
            f"{streak_text}"
        )

    if lang == "de":
        return (
            f"🧠 <b>Motivation des Tages</b>\n{content['motivation']}\n\n"
            f"✅ <b>Tagesaufgabe</b>\n{content['task']}\n\n"
            f"💡 <b>Tipp des Tages</b>\n{content['tip']}\n\n"
            f"🔥 <b>Deine Serie:</b> {streak} Tag(e)\n"
            f"{streak_text}"
        )

    return (
        f"🧠 <b>Мотивація дня</b>\n{content['motivation']}\n\n"
        f"✅ <b>Завдання дня</b>\n{content['task']}\n\n"
        f"💡 <b>Порада дня</b>\n{content['tip']}\n\n"
        f"🔥 <b>Серія:</b> {streak} дн.\n"
        f"{streak_text}"
    )


# ---------- Keyboards ----------
def main_menu_kb(user_id: int) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(t(user_id, "get_motivation"), callback_data="get_motivation")],
        [InlineKeyboardButton(t(user_id, "extra_push"), callback_data="extra_motivation")],
        [InlineKeyboardButton(t(user_id, "task_day"), callback_data="get_task")],
        [InlineKeyboardButton(t(user_id, "tip_day"), callback_data="get_tip")],
        [InlineKeyboardButton(t(user_id, "premium_btn"), callback_data="upgrade")],
        [InlineKeyboardButton(t(user_id, "change_lang"), callback_data="change_lang")],
    ]
    if MINI_APP_URL:
        buttons.append([InlineKeyboardButton(t(user_id, "open_mini_app"), url=MINI_APP_URL)])
    return InlineKeyboardMarkup(buttons)


def back_menu_kb(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(t(user_id, "back_to_menu"), callback_data="back_menu")]]
    )


def premium_kb(user_id: int, checkout_url: str | None = None, is_active: bool = False) -> InlineKeyboardMarkup:
    if is_active:
        return back_menu_kb(user_id)

    buttons = []
    if checkout_url:
        buttons.append([InlineKeyboardButton(t(user_id, "buy_premium"), url=checkout_url)])
    buttons.append([InlineKeyboardButton(t(user_id, "back_to_menu"), callback_data="back_menu")])
    return InlineKeyboardMarkup(buttons)


def language_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🇺🇦 Українська", callback_data="lang_uk")],
            [InlineKeyboardButton("🇬🇧 English", callback_data="lang_en")],
            [InlineKeyboardButton("🇩🇪 Deutsch", callback_data="lang_de")],
            [InlineKeyboardButton("⬅️ Назад у меню", callback_data="back_menu")],
        ]
    )


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
def ask_gpt(user_id: int, user_text: str) -> str:
    lang = get_user_lang(user_id)

    if not is_coach_request(user_text):
        return t(user_id, "coach_scope_reply")

    language_hint = {
        "uk": "Відповідай українською.",
        "en": "Reply in English.",
        "de": "Antworte auf Deutsch.",
    }.get(lang, "Відповідай українською.")

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
                        f"{language_hint} "
                        "Звертайся до користувача на 'ти' або природно для вибраної мови. "
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
        return t(user_id, "gpt_error")


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


# ---------- Panel logic ----------
async def remove_keyboard(chat_id: int, message_id: int) -> None:
    if TG_APP is None or not message_id:
        return
    try:
        await TG_APP.bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=None,
        )
    except Exception as e:
        log.warning("Failed to remove keyboard from old panel %s/%s: %s", chat_id, message_id, e)


async def send_main_panel(chat_id: int, text: str, reply_markup) -> int:
    if TG_APP is None:
        return 0

    msg = await TG_APP.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup,
    )
    return msg.message_id


async def open_fresh_main_panel(chat_id: int, include_comeback: bool = False) -> None:
    old_id = get_menu_message_id(chat_id)
    if old_id:
        await remove_keyboard(chat_id, old_id)

    new_id = await send_main_panel(
        chat_id,
        build_main_menu_text(chat_id, include_comeback=include_comeback),
        main_menu_kb(chat_id),
    )
    if new_id:
        set_menu_message_id(chat_id, new_id)


async def edit_active_panel_or_recreate(chat_id: int, message, text: str, reply_markup) -> None:
    current_menu_id = get_menu_message_id(chat_id)
    clicked_message_id = message.message_id

    # Якщо тиснуть стару панель — знімаємо з неї кнопки і працюємо з актуальною
    if current_menu_id and clicked_message_id != current_menu_id:
        try:
            await message.edit_reply_markup(reply_markup=None)
        except Exception as e:
            log.warning("Failed to clear stale panel keyboard: %s", e)
        return

    try:
        await message.edit_text(
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )
        set_menu_message_id(chat_id, message.message_id)
    except Exception as e:
        err = str(e).lower()
        if "message is not modified" in err:
            set_menu_message_id(chat_id, message.message_id)
            return

        log.warning("Panel edit failed, recreating panel: %s", e)

        old_id = get_menu_message_id(chat_id)
        if old_id:
            await remove_keyboard(chat_id, old_id)

        new_id = await send_main_panel(chat_id, text, reply_markup)
        if new_id:
            set_menu_message_id(chat_id, new_id)


# ---------- Premium notify ----------
async def send_premium_activated_message(user_id: int):
    if TG_APP is None:
        return

    try:
        await TG_APP.bot.send_message(
            chat_id=user_id,
            text=t(user_id, "premium_activated"),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        log.warning("Failed to send premium activation message to %s: %s", user_id, e)


# ---------- Telegram handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat:
        return

    chat_id = update.effective_chat.id
    _, lost = update_user_streak(chat_id)
    ensure_user(chat_id)

    await open_fresh_main_panel(chat_id, include_comeback=lost)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat:
        return
    await open_fresh_main_panel(update.effective_chat.id, include_comeback=False)


async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat:
        return
    await open_fresh_main_panel(update.effective_chat.id, include_comeback=False)


async def streak_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat:
        return
    await open_fresh_main_panel(update.effective_chat.id, include_comeback=False)


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_message:
        return

    chat_id = update.effective_chat.id
    old_id = get_menu_message_id(chat_id)
    if old_id:
        await remove_keyboard(chat_id, old_id)

    msg = await update.effective_message.reply_text(
        build_today_text(chat_id),
        parse_mode=ParseMode.HTML,
        reply_markup=back_menu_kb(chat_id),
    )
    set_menu_message_id(chat_id, msg.message_id)


async def upgrade_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_message:
        return

    chat_id = update.effective_chat.id
    old_id = get_menu_message_id(chat_id)
    if old_id:
        await remove_keyboard(chat_id, old_id)

    if is_premium(chat_id):
        msg = await update.effective_message.reply_text(
            build_premium_text(chat_id),
            parse_mode=ParseMode.HTML,
            reply_markup=back_menu_kb(chat_id),
        )
        set_menu_message_id(chat_id, msg.message_id)
        return

    try:
        checkout_url = create_checkout_session(chat_id)
    except Exception as e:
        log.warning("Stripe checkout session error: %s", e)
        await update.effective_message.reply_text(t(chat_id, "payment_error"))
        return

    msg = await update.effective_message.reply_text(
        build_premium_text(chat_id),
        parse_mode=ParseMode.HTML,
        reply_markup=premium_kb(chat_id, checkout_url=checkout_url),
    )
    set_menu_message_id(chat_id, msg.message_id)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.message:
        return

    await query.answer()
    user_id = query.from_user.id
    message = query.message

    if query.data == "back_menu":
        await edit_active_panel_or_recreate(
            user_id,
            message,
            build_main_menu_text(user_id),
            main_menu_kb(user_id),
        )
        return

    if query.data == "get_motivation":
        await edit_active_panel_or_recreate(
            user_id,
            message,
            build_motivation_text(user_id),
            back_menu_kb(user_id),
        )
        return

    if query.data == "extra_motivation":
        await edit_active_panel_or_recreate(
            user_id,
            message,
            build_extra_text(user_id),
            back_menu_kb(user_id),
        )
        return

    if query.data == "get_task":
        await edit_active_panel_or_recreate(
            user_id,
            message,
            build_task_text(user_id),
            back_menu_kb(user_id),
        )
        return

    if query.data == "get_tip":
        await edit_active_panel_or_recreate(
            user_id,
            message,
            build_tip_text(user_id),
            back_menu_kb(user_id),
        )
        return

    if query.data == "change_lang":
        await edit_active_panel_or_recreate(
            user_id,
            message,
            t(user_id, "language_title"),
            language_kb(),
        )
        return

    if query.data in ("lang_uk", "lang_en", "lang_de"):
        lang = query.data.split("_", 1)[1]
        set_user_lang(user_id, lang)
        await edit_active_panel_or_recreate(
            user_id,
            message,
            build_main_menu_text(user_id),
            main_menu_kb(user_id),
        )
        return

    if query.data == "upgrade":
        if is_premium(user_id):
            await edit_active_panel_or_recreate(
                user_id,
                message,
                build_premium_text(user_id),
                back_menu_kb(user_id),
            )
            return

        try:
            checkout_url = create_checkout_session(user_id)
        except Exception as e:
            log.warning("Stripe checkout session error: %s", e)
            await edit_active_panel_or_recreate(
                user_id,
                message,
                t(user_id, "payment_error"),
                back_menu_kb(user_id),
            )
            return

        await edit_active_panel_or_recreate(
            user_id,
            message,
            build_premium_text(user_id),
            premium_kb(user_id, checkout_url=checkout_url),
        )
        return

    await edit_active_panel_or_recreate(
        user_id,
        message,
        build_main_menu_text(user_id),
        main_menu_kb(user_id),
    )


async def unsupported_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_message:
        return
    await update.effective_message.reply_text(t(update.effective_chat.id, "photo_reply"))


async def chat_with_coach(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_message:
        return

    user_id = update.effective_chat.id
    text = (update.effective_message.text or "").strip()

    if not text or text.startswith("/"):
        return

    ensure_user(user_id)

    if not is_premium(user_id):
        await update.effective_message.reply_text(t(user_id, "premium_locked"))
        return

    increment_message_count(user_id)
    reply = ask_gpt(user_id, text)
    await update.effective_message.reply_text(reply)


# ---------- Scheduler ----------
async def daily_push(context: ContextTypes.DEFAULT_TYPE) -> None:
    content = today_content()
    users = get_subscribed_user_ids()
    if not users:
        return

    for uid in users:
        lang = get_user_lang(uid)

        if lang == "en":
            text = (
                f"🧠 <b>Motivation of the day</b>\n{content['motivation']}\n\n"
                f"✅ <b>Task of the day</b>\n{content['task']}\n\n"
                f"💡 <b>Tip of the day</b>\n{content['tip']}"
            )
        elif lang == "de":
            text = (
                f"🧠 <b>Motivation des Tages</b>\n{content['motivation']}\n\n"
                f"✅ <b>Tagesaufgabe</b>\n{content['task']}\n\n"
                f"💡 <b>Tipp des Tages</b>\n{content['tip']}"
            )
        else:
            text = (
                f"🧠 <b>Мотивація дня</b>\n{content['motivation']}\n\n"
                f"✅ <b>Завдання дня</b>\n{content['task']}\n\n"
                f"💡 <b>Порада дня</b>\n{content['tip']}"
            )

        try:
            await context.bot.send_message(
                chat_id=uid,
                text=text,
                parse_mode=ParseMode.HTML,
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