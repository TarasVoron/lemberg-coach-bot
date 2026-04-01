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
EVENING_HOUR = int(os.getenv("EVENING_HOUR", "20"))
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
FOLLOWUPS_PATH = DATA_DIR / "followups.json"
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

# Антиповтор для "Заряд"
user_last_extra_motivation: dict[int, str] = {}

# ---------- Time helpers ----------
def now_berlin() -> datetime:
    return datetime.now(BERLIN)


def today_str() -> str:
    return now_berlin().date().isoformat()


def dt_to_str(value: datetime) -> str:
    return value.isoformat()


def dt_from_str(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=BERLIN)
        return dt.astimezone(BERLIN)
    except Exception:
        return None


def parse_date(value: str) -> date:
    return date.fromisoformat(value)


# ---------- Coach request filters ----------
OFFTOPIC_BLOCKED = [
    "код",
    "python",
    "javascript",
    "програмування",
    "курс валют",
    "погода",
    "новини",
    "переклади",
    "translate",
    "википедия",
    "вікіпедія",
    "що це",
    "what is this",
    "who is this",
    "намалюй",
    "створи картинку",
]


def is_coach_request(text: str) -> bool:
    low = (text or "").strip().lower()
    if not low:
        return False

    for word in OFFTOPIC_BLOCKED:
        if word in low:
            return False

    return True


def detect_coach_mode(user_text: str) -> str:
    low = (user_text or "").lower()

    soft_markers = [
        "втомився", "втомилась", "не маю сили", "нема сил", "вигорів", "вигоріла",
        "вигорання", "тривога", "страшно", "не вивожу", "мені важко",
        "не можу зібратись", "не можу зібратися", "розгубився", "розгубилась",
        "опускаються руки", "зневірився", "зневірилась",
    ]

    push_markers = [
        "відкладаю", "прокрастиную", "злив день", "знову не зробив", "лінь",
        "лінуюсь", "лінився", "розмазався", "розфокус", "завис", "тягну час",
        "уникаю", "саботаж", "саботую",
    ]

    for marker in soft_markers:
        if marker in low:
            return "soft"

    for marker in push_markers:
        if marker in low:
            return "push"

    return "focus"


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


def load_json_dict(path: Path, fallback: dict) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
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


def load_followups() -> dict:
    return load_json_dict(
        FOLLOWUPS_PATH,
        {
            "after_task_2h": ["Що по твоїй задачі?"],
            "after_task_4h": ["Що вже зроблено по задачі?"],
            "evening_check": ["Підсумок дня: що ти реально зробив сьогодні?"],
            "no_response": ["Я не отримав відповіді. Що зробиш прямо зараз?"],
            "churn_1d": ["Ти зник на день. Яка твоя одна дія сьогодні?"],
            "churn_3d": ["Ти випав з ритму. Яка одна задача сьогодні найважливіша?"],
            "churn_7d": ["Повернення починається з одного кроку. Що робиш сьогодні?"],
        },
    )


def load_users_data() -> dict:
    try:
        with open(USERS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, list):
            result = {}
            today = today_str()
            now = dt_to_str(now_berlin())
            for user_id in data:
                result[str(user_id)] = {
                    "streak": 1,
                    "last_seen": today,
                    "last_interaction_at": now,
                    "premium": False,
                    "messages_count": 0,
                    "stripe_customer_id": "",
                    "stripe_subscription_id": "",
                    "panel_message_id": 0,
                    "pending_task": "",
                    "followup_due_at": "",
                    "followup_type": "",
                    "followup_sent": False,
                    "awaiting_task_answer": False,
                    "last_evening_check_date": "",
                    "last_churn_stage": "",
                    "last_churn_sent_at": "",
                }
            save_users_data(result)
            return result

        if isinstance(data, dict):
            changed = False
            defaults = {
                "streak": 0,
                "last_seen": "",
                "last_interaction_at": "",
                "premium": False,
                "messages_count": 0,
                "stripe_customer_id": "",
                "stripe_subscription_id": "",
                "panel_message_id": 0,
                "pending_task": "",
                "followup_due_at": "",
                "followup_type": "",
                "followup_sent": False,
                "awaiting_task_answer": False,
                "last_evening_check_date": "",
                "last_churn_stage": "",
                "last_churn_sent_at": "",
            }
            for _, user in data.items():
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
            "last_interaction_at": "",
            "premium": False,
            "messages_count": 0,
            "stripe_customer_id": "",
            "stripe_subscription_id": "",
            "panel_message_id": 0,
            "pending_task": "",
            "followup_due_at": "",
            "followup_type": "",
            "followup_sent": False,
            "awaiting_task_answer": False,
            "last_evening_check_date": "",
            "last_churn_stage": "",
            "last_churn_sent_at": "",
        }
        save_users_data(data)

    return data[uid]


def get_user(user_id: int) -> dict:
    ensure_user(user_id)
    data = load_users_data()
    return data.get(str(user_id), {})


def update_user_fields(user_id: int, **fields) -> None:
    data = load_users_data()
    uid = str(user_id)

    if uid not in data:
        data[uid] = {
            "streak": 0,
            "last_seen": "",
            "last_interaction_at": "",
            "premium": False,
            "messages_count": 0,
            "stripe_customer_id": "",
            "stripe_subscription_id": "",
            "panel_message_id": 0,
            "pending_task": "",
            "followup_due_at": "",
            "followup_type": "",
            "followup_sent": False,
            "awaiting_task_answer": False,
            "last_evening_check_date": "",
            "last_churn_stage": "",
            "last_churn_sent_at": "",
        }

    for k, v in fields.items():
        data[uid][k] = v

    save_users_data(data)


def mark_interaction(user_id: int) -> None:
    update_user_fields(
        user_id,
        last_interaction_at=dt_to_str(now_berlin()),
        last_churn_stage="",
        last_churn_sent_at="",
    )


def get_panel_message_id(user_id: int) -> int:
    return int(get_user(user_id).get("panel_message_id", 0) or 0)


def set_panel_message_id(user_id: int, message_id: int) -> None:
    update_user_fields(user_id, panel_message_id=message_id)


def is_premium(user_id: int) -> bool:
    return bool(get_user(user_id).get("premium", False))


def set_premium(
    user_id: int,
    value: bool = True,
    stripe_customer_id: str = "",
    stripe_subscription_id: str = "",
) -> None:
    payload = {"premium": value}
    if stripe_customer_id:
        payload["stripe_customer_id"] = stripe_customer_id
    if stripe_subscription_id:
        payload["stripe_subscription_id"] = stripe_subscription_id
    update_user_fields(user_id, **payload)


def find_user_id_by_subscription(subscription_id: str) -> int | None:
    data = load_users_data()
    for uid, user in data.items():
        if user.get("stripe_subscription_id") == subscription_id:
            return int(uid)
    return None


def increment_message_count(user_id: int) -> None:
    current = int(get_user(user_id).get("messages_count", 0))
    update_user_fields(user_id, messages_count=current + 1)


def update_user_streak(user_id: int) -> tuple[int, bool]:
    data = load_users_data()
    uid = str(user_id)
    today = now_berlin().date()

    if uid not in data:
        data[uid] = {
            "streak": 1,
            "last_seen": today.isoformat(),
            "last_interaction_at": dt_to_str(now_berlin()),
            "premium": False,
            "messages_count": 0,
            "stripe_customer_id": "",
            "stripe_subscription_id": "",
            "panel_message_id": 0,
            "pending_task": "",
            "followup_due_at": "",
            "followup_type": "",
            "followup_sent": False,
            "awaiting_task_answer": False,
            "last_evening_check_date": "",
            "last_churn_stage": "",
            "last_churn_sent_at": "",
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
    user["last_interaction_at"] = dt_to_str(now_berlin())
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
    now = now_berlin()
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


# ---------- Follow-up helpers ----------
def get_random_followup(kind: str) -> str:
    items = load_followups().get(kind, [])
    if not items:
        return "Що по твоїй задачі?"
    return random.choice(items)


def is_actionable_user_text(text: str) -> bool:
    low = (text or "").strip().lower()
    if not low:
        return False

    action_markers = [
        "зроблю",
        "почну",
        "починаю",
        "сьогодні",
        "моя задача",
        "моя дія",
        "планую",
        "буду робити",
        "хочу зробити",
        "потрібно",
        "треба",
        "зараз зроблю",
        "перша дія",
        "моє завдання",
    ]
    return any(marker in low for marker in action_markers)


def save_task_followup(
    user_id: int,
    task_text: str,
    delay_hours: int = 2,
    followup_type: str = "after_task_2h",
) -> None:
    due_at = now_berlin() + timedelta(hours=delay_hours)
    update_user_fields(
        user_id,
        pending_task=task_text.strip(),
        followup_due_at=dt_to_str(due_at),
        followup_type=followup_type,
        followup_sent=False,
        awaiting_task_answer=True,
    )


def clear_task_followup(user_id: int) -> None:
    update_user_fields(
        user_id,
        pending_task="",
        followup_due_at="",
        followup_type="",
        followup_sent=False,
        awaiting_task_answer=False,
    )


async def send_followup(user_id: int, kind: str) -> None:
    if TG_APP is None:
        return

    user = get_user(user_id)
    pending_task = user.get("pending_task", "").strip()
    base_text = get_random_followup(kind)

    if kind in ("after_task_2h", "after_task_4h") and pending_task:
        text = f"{base_text}\n\n🎯 <b>Твоя задача:</b> {pending_task}"
    else:
        text = base_text

    try:
        await TG_APP.bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode=ParseMode.HTML,
        )
        update_user_fields(
            user_id,
            followup_sent=True,
            awaiting_task_answer=True,
        )
    except Exception as e:
        log.warning("Failed to send follow-up to %s: %s", user_id, e)


async def check_followups(context: ContextTypes.DEFAULT_TYPE) -> None:
    users = get_subscribed_user_ids()
    now = now_berlin()

    for user_id in users:
        user = get_user(user_id)
        due_raw = user.get("followup_due_at", "")
        followup_type = user.get("followup_type", "")
        followup_sent = bool(user.get("followup_sent", False))

        if not due_raw or not followup_type or followup_sent:
            continue

        due_at = dt_from_str(due_raw)
        if not due_at:
            continue

        if now >= due_at:
            await send_followup(user_id, followup_type)


# ---------- Anti-churn helpers ----------
async def send_anti_churn(user_id: int, kind: str) -> None:
    if TG_APP is None:
        return

    text = get_random_followup(kind)
    try:
        await TG_APP.bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode=ParseMode.HTML,
        )
        update_user_fields(
            user_id,
            last_churn_stage=kind,
            last_churn_sent_at=dt_to_str(now_berlin()),
        )
    except Exception as e:
        log.warning("Failed to send anti-churn to %s: %s", user_id, e)


async def check_anti_churn(context: ContextTypes.DEFAULT_TYPE) -> None:
    users = get_subscribed_user_ids()
    now = now_berlin()

    for user_id in users:
        user = get_user(user_id)
        last_interaction_raw = user.get("last_interaction_at", "")
        if not last_interaction_raw:
            continue

        last_interaction = dt_from_str(last_interaction_raw)
        if not last_interaction:
            continue

        delta = now - last_interaction
        last_stage = user.get("last_churn_stage", "")

        if delta >= timedelta(days=7):
            if last_stage != "churn_7d":
                await send_anti_churn(user_id, "churn_7d")
        elif delta >= timedelta(days=3):
            if last_stage not in ("churn_3d", "churn_7d"):
                await send_anti_churn(user_id, "churn_3d")
        elif delta >= timedelta(days=1):
            if last_stage not in ("churn_1d", "churn_3d", "churn_7d"):
                await send_anti_churn(user_id, "churn_1d")


# ---------- Streak messaging ----------
def get_streak_message(streak: int) -> str:
    if streak >= 21:
        return "🔥 21 день — ти вже інша людина."
    if streak >= 14:
        return "⚡ 14 днів — це вже система, не випадковість."
    if streak >= 10:
        return "💥 10 днів — ти вже небезпечний для своїх старих звичок."
    if streak >= 7:
        return "🚀 7 днів — ти вже будуєш нову версію себе."
    if streak >= 5:
        return "📈 5 днів — дисципліна починає працювати."
    if streak >= 3:
        return "🔥 3 дні — ти вже в ритмі."
    if streak >= 1:
        return "✨ Початок — це вже більше, ніж у більшості."
    return ""


def get_comeback_message() -> str:
    return (
        "⚠️ Ти випав з ритму.\n\n"
        "Але повернутись — це сильніше, ніж не падати.\n\n"
        "Сьогодні = новий старт."
    )


# ---------- Stripe checkout ----------
def create_checkout_session(telegram_user_id: int) -> str:
    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
        success_url=f"{APP_BASE_URL}/payment-success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{APP_BASE_URL}/payment-cancelled",
        client_reference_id=str(telegram_user_id),
        metadata={"telegram_user_id": str(telegram_user_id)},
        allow_promotion_codes=True,
    )
    return session.url


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


# ---------- UI ----------
def panel_keyboard(user_id: int, panel: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    if panel == "premium" and not is_premium(user_id):
        try:
            checkout_url = create_checkout_session(user_id)
            rows.append([InlineKeyboardButton("💳 Оформити Premium", url=checkout_url)])
        except Exception as e:
            log.warning("Failed to create checkout URL for keyboard: %s", e)
    elif is_premium(user_id):
        rows.append([InlineKeyboardButton("💎 Premium активний", callback_data="view:premium")])
    else:
        rows.append([InlineKeyboardButton("🚀 Premium", callback_data="view:premium")])

    rows.extend([
        [InlineKeyboardButton("🔥 Отримати мотивацію", callback_data="view:motivation")],
        [InlineKeyboardButton("⚡ Заряд", callback_data="view:charge")],
        [InlineKeyboardButton("✅ Завдання дня", callback_data="view:task")],
        [InlineKeyboardButton("💡 Порада дня", callback_data="view:tip")],
        [InlineKeyboardButton("🏠 Головне меню", callback_data="view:menu")],
    ])

    if MINI_APP_URL:
        rows.append([InlineKeyboardButton("🧭 Відкрити Mini App", url=MINI_APP_URL)])

    return InlineKeyboardMarkup(rows)


def quick_daily_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏠 Відкрити меню", callback_data="view:menu")],
        [InlineKeyboardButton("⚡ Заряд", callback_data="view:charge")],
    ])


def build_menu_text(user_id: int, include_comeback: bool = False) -> str:
    streak = get_user_streak(user_id)
    streak_msg = get_streak_message(streak)
    premium_badge = "💎 <b>Premium активний</b>\n\n" if is_premium(user_id) else ""
    comeback = f"{get_comeback_message()}\n\n" if include_comeback else ""

    return (
        comeback
        + premium_badge
        + "👋 <b>Lemberg Coach</b>\n\n"
        + "Я — AI-коуч для дії.\n\n"
        + "Допомагаю:\n"
        + "• розкласти ціль на кроки\n"
        + "• зібрати план на день\n"
        + "• вийти з прокрастинації\n"
        + "• почати діяти вже сьогодні\n\n"
        + "Щодня ти отримуєш:\n"
        + "• 🧠 <b>Мотивацію дня</b>\n"
        + "• ✅ <b>Завдання дня</b>\n"
        + "• 💡 <b>Пораду дня</b>\n"
        + "• ⚡ <b>Заряд</b>\n\n"
        + f"🔥 <b>Твоя серія:</b> {streak} дн.\n"
        + f"{streak_msg}\n\n"
        + "Обери дію нижче або напиши мені повідомлення."
    )


def build_motivation_text() -> str:
    return f"🧠 <b>Мотивація дня</b>\n\n{get_today_motivation()}"


def build_charge_text(user_id: int) -> str:
    return f"⚡ <b>Заряд</b>\n\n{get_extra_motivation_for_user(user_id)}"


def build_task_text() -> str:
    return f"✅ <b>Завдання дня</b>\n\n{get_today_task()}"


def build_tip_text() -> str:
    return f"💡 <b>Порада дня</b>\n\n{get_today_tip()}"


def build_premium_text(user_id: int) -> str:
    if is_premium(user_id):
        return (
            "💎 <b>Lemberg Coach Premium</b>\n\n"
            "У тебе вже активний Premium.\n\n"
            "Що відкрито зараз:\n"
            "• GPT-коуч 24/7\n"
            "• ранкові й вечірні дотики\n"
            "• персональні відповіді під твою ситуацію\n"
            "• швидкий доступ без повторної оплати\n\n"
            "Просто напиши мені повідомлення — і я відповім."
        )

    return (
        "🚀 <b>Lemberg Coach Premium</b>\n\n"
        "Це не просто чат.\n\n"
        "Це система, яка:\n"
        "• тримає фокус\n"
        "• не дає зливати день\n"
        "• повертає тебе в дію\n\n"
        "<b>Що відкривається:</b>\n"
        "• GPT-коуч 24/7\n"
        "• ранкові й вечірні дотики\n"
        "• персональні відповіді під твої цілі\n"
        "• майбутні premium-функції\n\n"
        "Оформи доступ кнопкою нижче."
    )


def build_today_text(user_id: int) -> str:
    content = today_content()
    streak = get_user_streak(user_id)
    return (
        f"🧠 <b>Мотивація дня</b>\n{content['motivation']}\n\n"
        f"✅ <b>Завдання дня</b>\n{content['task']}\n\n"
        f"💡 <b>Порада дня</b>\n{content['tip']}\n\n"
        f"🔥 <b>Серія:</b> {streak} дн.\n"
        f"{get_streak_message(streak)}"
    )


def build_panel_text(user_id: int, panel: str, include_comeback: bool = False) -> str:
    if panel == "menu":
        return build_menu_text(user_id, include_comeback=include_comeback)
    if panel == "motivation":
        return build_motivation_text()
    if panel == "charge":
        return build_charge_text(user_id)
    if panel == "task":
        return build_task_text()
    if panel == "tip":
        return build_tip_text()
    if panel == "premium":
        return build_premium_text(user_id)
    if panel == "today":
        return build_today_text(user_id)
    return build_menu_text(user_id)


async def render_panel(user_id: int, panel: str, include_comeback: bool = False) -> None:
    if TG_APP is None:
        return

    text = build_panel_text(user_id, panel, include_comeback=include_comeback)
    markup = panel_keyboard(user_id, panel)
    current_message_id = get_panel_message_id(user_id)

    if current_message_id:
        try:
            await TG_APP.bot.edit_message_text(
                chat_id=user_id,
                message_id=current_message_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )
            return
        except Exception as e:
            err = str(e).lower()
            if "message is not modified" in err:
                try:
                    await TG_APP.bot.edit_message_reply_markup(
                        chat_id=user_id,
                        message_id=current_message_id,
                        reply_markup=markup,
                    )
                    return
                except Exception:
                    pass

            log.warning("Panel edit failed for user %s: %s", user_id, e)

    sent = await TG_APP.bot.send_message(
        chat_id=user_id,
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=markup,
    )
    set_panel_message_id(user_id, sent.message_id)


# ---------- GPT ----------
def ask_gpt(user_text: str) -> str:
    if not is_coach_request(user_text):
        return "Я працюю як AI-коуч для дії. Напиши свою ціль або проблему — і я допоможу."

    mode = detect_coach_mode(user_text)

    if mode == "soft":
        style_block = (
            "РЕЖИМ SOFT:\n"
            "- Будь м'якшим і спокійнішим.\n"
            "- Не тисни жорстко.\n"
            "- Дай людині відчути опору.\n"
            "- Все одно веди до 1 маленької дії.\n"
        )
    elif mode == "push":
        style_block = (
            "РЕЖИМ PUSH:\n"
            "- Говори коротко, прямо і жорсткіше.\n"
            "- Не будь грубим, але не сюсюкайся.\n"
            "- Покажи людині, де вона себе зливає.\n"
            "- Веди до 1 конкретної дії прямо зараз.\n"
        )
    else:
        style_block = (
            "РЕЖИМ FOCUS:\n"
            "- Говори чітко і зібрано.\n"
            "- Допоможи звузити хаос до 1 головної задачі.\n"
            "- Веди до конкретного кроку.\n"
        )

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.7,
            max_tokens=240,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Ти персональний AI-коуч Lemberg Coach.\n"
                        "Відповідай українською мовою.\n"
                        "Звертайся до користувача на 'ти'.\n\n"
                        "Твоя роль — НЕ давати загальні поради, а зрушувати людину в дію.\n\n"
                        f"{style_block}\n"
                        "ПРАВИЛА:\n"
                        "- Не давай списки з інтернету.\n"
                        "- Не пиши як енциклопедія.\n"
                        "- Фокусуйся на 1 конкретній дії.\n"
                        "- Часто став 1 уточнююче питання.\n"
                        "- Якщо людина розгублена — звужуй до одного кроку.\n"
                        "- Якщо людина вже має напрям — допоможи визначити наступний конкретний крок.\n\n"
                        "СТРУКТУРА ВІДПОВІДІ:\n"
                        "1. Коротко віддзеркаль, що зараз з людиною\n"
                        "2. Дай 1 конкретну дію\n"
                        "3. Постав 1 питання\n\n"
                        "Максимум 3–5 речень.\n"
                        "Будь конкретним, енергійним і живим.\n"
                    ),
                },
                {"role": "user", "content": user_text},
            ],
        )
        return response.choices[0].message.content or "Зроби одну дію прямо зараз."
    except Exception as e:
        log.warning("OpenAI error: %s", e)
        return "⚠️ GPT тимчасово недоступний. Спробуй ще раз пізніше."


# ---------- Premium notify ----------
async def send_premium_activated_message(user_id: int):
    if TG_APP is None:
        return

    text = (
        "🎉 <b>Premium активовано.</b>\n\n"
        "Тепер я твій AI-коуч 24/7.\n"
        "Напиши свою ціль або задачу."
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
        await render_panel(user_id, "premium")
    except Exception as e:
        log.warning("Failed to refresh panel after premium activation: %s", e)


# ---------- Telegram handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat:
        return

    chat_id = update.effective_chat.id
    _, lost = update_user_streak(chat_id)
    ensure_user(chat_id)
    mark_interaction(chat_id)
    await render_panel(chat_id, "menu", include_comeback=lost)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat:
        return
    mark_interaction(update.effective_chat.id)
    await render_panel(update.effective_chat.id, "menu")


async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_message:
        return
    mark_interaction(update.effective_chat.id)
    now = now_berlin().strftime("%Y-%m-%d %H:%M:%S")
    await update.effective_message.reply_text(f"✅ Пінг! {now} (Europe/Berlin)")


async def streak_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat:
        return
    mark_interaction(update.effective_chat.id)
    await render_panel(update.effective_chat.id, "menu")


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat:
        return
    mark_interaction(update.effective_chat.id)
    await render_panel(update.effective_chat.id, "today")


async def upgrade_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat:
        return
    mark_interaction(update.effective_chat.id)
    await render_panel(update.effective_chat.id, "premium")


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return

    await query.answer()
    user_id = query.from_user.id
    data = query.data or ""

    mark_interaction(user_id)

    if data.startswith("view:"):
        panel = data.split(":", 1)[1]
        await render_panel(user_id, panel)
        return

    await render_panel(user_id, "menu")


async def unsupported_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message or not update.effective_chat:
        return

    mark_interaction(update.effective_chat.id)

    await update.effective_message.reply_text(
        "📷 Я поки не аналізую фото в цьому боті.\n\n"
        "Опиши словами свою ситуацію, ціль або проблему — і я допоможу як коуч."
    )


async def unsupported_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message or not update.effective_chat:
        return

    mark_interaction(update.effective_chat.id)

    await update.effective_message.reply_text(
        "🎤 Голосові я поки не аналізую.\n\n"
        "Напиши текстом свою ціль, проблему або ситуацію — і я допоможу."
    )


async def chat_with_coach(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_message:
        return

    user_id = update.effective_chat.id
    text = (update.effective_message.text or "").strip()

    if not text or text.startswith("/"):
        return

    ensure_user(user_id)
    mark_interaction(user_id)

    if not is_premium(user_id):
        await update.effective_message.reply_text(
            "🔒 GPT-коуч доступний тільки в Premium.\n\n"
            "Натисни кнопку Premium у меню, щоб відкрити доступ."
        )
        return

    user = get_user(user_id)
    if bool(user.get("awaiting_task_answer", False)):
        clear_task_followup(user_id)

    increment_message_count(user_id)
    reply = ask_gpt(text)
    await update.effective_message.reply_text(reply)

    if is_actionable_user_text(text):
        save_task_followup(
            user_id=user_id,
            task_text=text,
            delay_hours=2,
            followup_type="after_task_2h",
        )


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
                reply_markup=quick_daily_kb(),
            )
        except Exception as e:
            log.warning("Failed to send daily push to %s: %s", uid, e)


async def evening_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    today = today_str()
    users = get_subscribed_user_ids()

    for uid in users:
        if not is_premium(uid):
            continue

        user = get_user(uid)
        if user.get("last_evening_check_date") == today:
            continue

        try:
            text = get_random_followup("evening_check")
            await context.bot.send_message(
                chat_id=uid,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=quick_daily_kb(),
            )
            update_user_fields(uid, last_evening_check_date=today)
        except Exception as e:
            log.warning("Failed to send evening check to %s: %s", uid, e)


def schedule_jobs(app: Application) -> None:
    if app.job_queue is None:
        log.warning("JobQueue is not available. Jobs were not scheduled.")
        return

    daily_time = now_berlin().replace(
        hour=DAILY_HOUR,
        minute=0,
        second=0,
        microsecond=0,
    ).timetz()

    evening_time = now_berlin().replace(
        hour=EVENING_HOUR,
        minute=0,
        second=0,
        microsecond=0,
    ).timetz()

    app.job_queue.run_daily(daily_push, time=daily_time, name="daily_push_berlin")
    app.job_queue.run_daily(evening_check, time=evening_time, name="evening_check_berlin")
    app.job_queue.run_repeating(check_followups, interval=600, first=30, name="check_followups")
    app.job_queue.run_repeating(check_anti_churn, interval=1800, first=60, name="check_anti_churn")


async def notify_owner_started(app: Application) -> None:
    if not OWNER_ID:
        return
    try:
        now = now_berlin().strftime("%Y-%m-%d %H:%M:%S")
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

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(notify_owner_started)
        .build()
    )
    TG_APP = application

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("ping", ping_cmd))
    application.add_handler(CommandHandler("today", today_cmd))
    application.add_handler(CommandHandler("streak", streak_cmd))
    application.add_handler(CommandHandler("upgrade", upgrade_cmd))
    application.add_handler(CallbackQueryHandler(on_button))
    application.add_handler(MessageHandler(filters.PHOTO, unsupported_media))
    application.add_handler(MessageHandler(filters.VOICE, unsupported_voice))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat_with_coach))

    schedule_jobs(application)

    log.info("Bot started. Press Ctrl+C to stop.")
    application.run_polling(close_loop=False)


if __name__ == "__main__":
    main()