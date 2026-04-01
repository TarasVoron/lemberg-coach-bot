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
            if isinstance(data, dict) and data:
                return data
    except Exception as e:
        log.warning("Failed to load %s: %s", path.name, e)
    return fallback


def load_motivations() -> list[str]:
    return load_json_list(
        MOTIVATIONS_PATH,
        ["Твоя проблема зараз не в мотивації, а в відсутності одного чіткого кроку. Визнач його."]
    )


def load_tasks() -> list[str]:
    return load_json_list(
        TASKS_PATH,
        ["Не думай про весь шлях. Обери одну конкретну справу і закрий її сьогодні."]
    )


def load_tips() -> list[str]:
    return load_json_list(
        TIPS_PATH,
        ["Не шукай ідеальний день. Збери цей день і зроби одну правильну дію."]
    )


def load_followups() -> dict[str, list[str]]:
    fallback = {
        "after_task_2h": [
            "⏱ Минуло трохи часу. Ти вже почав ту дію, яку визначив?",
            "⏱ Я повертаю тебе в дію. Що вже зроблено по твоїй задачі?",
            "⏱ Не зливай імпульс. Ти вже рушив чи ще стоїш?"
        ],
        "after_task_4h": [
            "⚡ Другий дотик. Є прогрес чи ти знову відклав?",
            "⚡ Чесно: що вже зроблено по задачі?",
            "⚡ Якщо ще не почав — зроби мінімум прямо зараз. Яка буде дія?"
        ],
        "evening_check": [
            "🌙 Вечірній check-in. Ти закрив сьогодні головну задачу чи ні?",
            "🌙 Підсумок дня: що ти реально зробив сьогодні?",
            "🌙 Не прикрашай. Який один результат дня ти можеш назвати?"
        ],
        "churn_1d": [
            "👀 Ти зник на день. Не зливай ритм. Яка твоя одна дія сьогодні?",
            "👀 Пауза — не проблема. Зникнути надовго — проблема. З чого повертаєшся сьогодні?",
        ],
        "churn_3d": [
            "⚠️ Ти випав з ритму вже на кілька днів. Поверни контроль. Яка одна дія сьогодні?",
            "⚠️ Зараз не час думати широко. Назви одну задачу, яку ти реально закриєш сьогодні.",
        ],
        "churn_7d": [
            "🔥 Ти надто довго поза ритмом. Повернення починається з одного кроку. Що робиш сьогодні?",
            "🔥 Стоп. Без самокритики. Просто назви одну дію і виконай її сьогодні.",
        ],
    }
    return load_json_dict(FOLLOWUPS_PATH, fallback)


def today_str() -> str:
    return datetime.now(BERLIN).date().isoformat()


def parse_date(value: str) -> date:
    return date.fromisoformat(value)


def now_berlin() -> datetime:
    return datetime.now(BERLIN)


def dt_to_str(dt: datetime) -> str:
    return dt.isoformat()


def parse_dt(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def default_user_payload() -> dict:
    return {
        "streak": 0,
        "last_seen": "",
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
        "last_user_message_at": "",
        "last_churn_sent_at": "",
        "last_churn_type": "",
    }


def load_users_data() -> dict:
    try:
        with open(USERS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, list):
            result = {}
            today = today_str()
            for user_id in data:
                payload = default_user_payload()
                payload["streak"] = 1
                payload["last_seen"] = today
                result[str(user_id)] = payload
            save_users_data(result)
            return result

        if isinstance(data, dict):
            changed = False
            defaults = default_user_payload()
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
        data[uid] = default_user_payload()
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
        data[uid] = default_user_payload()

    for k, v in fields.items():
        data[uid][k] = v

    save_users_data(data)


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
    today = datetime.now(BERLIN).date()

    if uid not in data:
        payload = default_user_payload()
        payload["streak"] = 1
        payload["last_seen"] = today.isoformat()
        data[uid] = payload
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
        return "Ти зараз не зібраний, бо не визначив один чіткий крок. Визнач його."

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


def save_task_followup(
    user_id: int,
    task_text: str,
    delay_hours: int = 2,
    followup_type: str = "after_task_2h",
) -> None:
    due_at = now_berlin() + timedelta(hours=delay_hours)
    update_user_fields(
        user_id,
        pending_task=task_text,
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

    text = get_random_followup(kind)
    if pending_task:
        text += f"\n\n🎯 <b>Твоя задача:</b> {pending_task}"

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
    now = now_berlin()
    users = get_subscribed_user_ids()

    for uid in users:
        user = get_user(uid)

        due_at_raw = user.get("followup_due_at", "")
        followup_type = user.get("followup_type", "")
        followup_sent = bool(user.get("followup_sent", False))

        if not due_at_raw or not followup_type or followup_sent:
            continue

        due_at = parse_dt(due_at_raw)
        if due_at is None:
            continue

        if now >= due_at:
            await send_followup(uid, followup_type)

            if followup_type == "after_task_2h":
                next_due = now + timedelta(hours=2)
                update_user_fields(
                    uid,
                    followup_due_at=dt_to_str(next_due),
                    followup_type="after_task_4h",
                    followup_sent=False,
                )
            else:
                update_user_fields(
                    uid,
                    followup_due_at="",
                    followup_type="",
                    followup_sent=True,
                )


async def evening_checkin(context: ContextTypes.DEFAULT_TYPE) -> None:
    users = get_subscribed_user_ids()
    for uid in users:
        try:
            await send_followup(uid, "evening_check")
        except Exception as e:
            log.warning("Failed evening check-in to %s: %s", uid, e)


async def check_anti_churn(context: ContextTypes.DEFAULT_TYPE) -> None:
    if TG_APP is None:
        return

    now = now_berlin()
    users = get_subscribed_user_ids()

    for uid in users:
        user = get_user(uid)
        last_msg_raw = user.get("last_user_message_at", "")
        if not last_msg_raw:
            continue

        last_msg_dt = parse_dt(last_msg_raw)
        if last_msg_dt is None:
            continue

        diff = now - last_msg_dt
        kind = ""

        if diff >= timedelta(days=7):
            kind = "churn_7d"
        elif diff >= timedelta(days=3):
            kind = "churn_3d"
        elif diff >= timedelta(days=1):
            kind = "churn_1d"
        else:
            continue

        last_churn_type = user.get("last_churn_type", "")
        last_churn_sent_at = parse_dt(user.get("last_churn_sent_at", ""))

        if last_churn_type == kind and last_churn_sent_at and (now - last_churn_sent_at) < timedelta(hours=20):
            continue

        text = get_random_followup(kind)

        try:
            await TG_APP.bot.send_message(
                chat_id=uid,
                text=text,
                parse_mode=ParseMode.HTML,
            )
            update_user_fields(
                uid,
                last_churn_type=kind,
                last_churn_sent_at=dt_to_str(now),
            )
        except Exception as e:
            log.warning("Failed anti-churn to %s: %s", uid, e)


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


# ---------- UI ----------
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


def build_menu_text(user_id: int, include_comeback: bool = False) -> str:
    streak = get_user_streak(user_id)
    streak_msg = get_streak_message(streak)
    premium_badge = "💎 <b>Premium активний</b>\n\n" if is_premium(user_id) else ""
    comeback = f"{get_comeback_message()}\n\n" if include_comeback else ""

    return (
        comeback
        + premium_badge
        + "👋 <b>Lemberg Coach</b>\n\n"
        + "Я — <b>AI-коуч для дії</b>.\n\n"
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


# ---------- Request scope ----------
def is_coach_request(text: str) -> bool:
    text = text.lower().strip()

    blocked_keywords = [
        "код", "python", "javascript", "програмування",
        "курс валют", "погода", "новини",
        "переклади", "translate", "википедия", "вікіпедія",
        "намалюй", "створи картинку", "що це", "опиши фото"
    ]

    for word in blocked_keywords:
        if word in text:
            return False

    return True


# ---------- GPT ----------
def ask_gpt(user_text: str) -> str:
    if not is_coach_request(user_text):
        return "Я працюю як AI-коуч для дії. Напиши свою ціль або проблему — і я допоможу."

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

                        "ПРАВИЛА:\n"
                        "- Не давай списки з інтернету.\n"
                        "- Не перетворюйся на енциклопедію.\n"
                        "- Фокусуйся на 1 конкретній дії.\n"
                        "- Часто став 1 сильне уточнююче питання.\n"
                        "- Іноді говори прямо, але без грубості.\n"
                        "- Не розмазуй відповідь.\n"
                        "- Не йди в абстракцію.\n"
                        "- Твоя мета — допомогти людині зробити наступний крок.\n\n"

                        "СТРУКТУРА ВІДПОВІДІ:\n"
                        "1. Коротко скажи, що зараз відбувається з людиною\n"
                        "2. Дай 1 конкретну дію\n"
                        "3. Задай 1 питання\n\n"

                        "ПРИКЛАД СТИЛЮ:\n"
                        "Ти зараз не зібраний, бо в тебе немає однієї головної задачі.\n"
                        "Назви її.\n"
                        "Що для тебе зараз №1?\n\n"

                        "Будь коротким, конкретним і енергійним.\n"
                        "Не пиши довгі відповіді.\n"
                        "Максимум 3–5 речень."
                    ),
                },
                {"role": "user", "content": user_text},
            ],
        )
        return response.choices[0].message.content or "Зроби одну дію прямо зараз."
    except Exception as e:
        log.warning("OpenAI error: %s", e)
        return "⚠️ GPT тимчасово недоступний. Спробуй ще раз пізніше."


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
    await render_panel(chat_id, "menu", include_comeback=lost)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat:
        return
    await render_panel(update.effective_chat.id, "menu")


async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_message:
        return
    now = datetime.now(BERLIN).strftime("%Y-%m-%d %H:%M:%S")
    await update.effective_message.reply_text(f"✅ Пінг! {now} (Europe/Berlin)")


async def streak_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat:
        return
    await render_panel(update.effective_chat.id, "menu")


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat:
        return
    await render_panel(update.effective_chat.id, "today")


async def upgrade_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat:
        return
    await render_panel(update.effective_chat.id, "premium")


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return

    await query.answer()
    user_id = query.from_user.id
    data = query.data or ""

    if data.startswith("view:"):
        panel = data.split(":", 1)[1]
        await render_panel(user_id, panel)
        return

    await render_panel(user_id, "menu")


async def unsupported_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message:
        return

    await update.effective_message.reply_text(
        "📷 Я поки не аналізую фото в цьому боті.\n\n"
        "Опиши словами свою ситуацію, ціль або проблему — і я допоможу як коуч."
    )


async def unsupported_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message:
        return

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
    update_user_fields(user_id, last_user_message_at=dt_to_str(now_berlin()))

    if not is_premium(user_id):
        await update.effective_message.reply_text(
            "🔒 GPT-коуч доступний тільки в Premium.\n\n"
            "Натисни кнопку Premium у меню, щоб відкрити доступ."
        )
        return

    user = get_user(user_id)
    if user.get("awaiting_task_answer", False):
        clear_task_followup(user_id)

    increment_message_count(user_id)
    reply = ask_gpt(text)
    await update.effective_message.reply_text(reply)

    low = text.lower()
    trigger_phrases = (
        "план на день",
        "дай план",
        "що робити",
        "не можу зібратись",
        "не можу зібратися",
        "мені важко почати",
        "дай дію",
        "з чого почати",
        "я не можу зібратись",
        "я не можу зібратися",
        "я не можу почати",
    )

    if any(p in low for p in trigger_phrases):
        save_task_followup(
            user_id,
            task_text=text,
            delay_hours=2,
            followup_type="after_task_2h",
        )


# ---------- Scheduler ----------
async def daily_push(context: ContextTypes.DEFAULT_TYPE) -> None:
    users = get_subscribed_user_ids()
    if not users:
        return

    for uid in users:
        try:
            await render_panel(uid, "today")
        except Exception as e:
            log.warning("Failed to send daily push to %s: %s", uid, e)


def schedule_jobs(app: Application) -> None:
    morning_time = datetime.now(BERLIN).replace(
        hour=DAILY_HOUR,
        minute=0,
        second=0,
        microsecond=0,
    ).timetz()

    evening_time = datetime.now(BERLIN).replace(
        hour=EVENING_HOUR,
        minute=0,
        second=0,
        microsecond=0,
    ).timetz()

    if app.job_queue is None:
        log.warning("JobQueue is not available. Jobs were not scheduled.")
        return

    app.job_queue.run_daily(daily_push, time=morning_time, name="daily_push_berlin")

    app.job_queue.run_daily(
        evening_checkin,
        time=evening_time,
        name="evening_checkin_berlin",
    )

    app.job_queue.run_repeating(
        check_followups,
        interval=300,
        first=60,
        name="check_followups",
    )

    app.job_queue.run_repeating(
        check_anti_churn,
        interval=3600,
        first=120,
        name="check_anti_churn",
    )


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
    application.add_handler(MessageHandler(filters.VOICE, unsupported_voice))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat_with_coach))

    schedule_jobs(application)
    application.post_init = notify_owner_started

    log.info("Bot started. Press Ctrl+C to stop.")
    application.run_polling(close_loop=False)


if __name__ == "__main__":
    main()