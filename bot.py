import os
import logging
import sqlite3
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, PreCheckoutQueryHandler,
    ContextTypes, filters
)
from telegram.constants import ParseMode, ChatAction
import anthropic
import httpx

# ─── Логирование ───────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─── Конфиг ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN    = "8905009739:AAF5TsZre4WhAa1F4Uqdxu0J1qkZZVme_kc"
ANTHROPIC_KEY     = "ВСТАВЬ_СВОЙ_КЛЮЧ_СЮДА"
API_FOOTBALL_KEY  = ""
ADMIN_ID          = 0  # Вставь свой Telegram ID (узнай у @userinfobot)
CHANNEL_USERNAME  = "@твой_канал"  # username твоего канала

PARTNER_LINKS = {
    "football": "https://1xbet.com/ru",
    "cs2":      "https://1xbet.com/ru",
    "dota":     "https://1xbet.com/ru",
}

# ─── Тарифы ────────────────────────────────────────────────────────────────────
PLANS = {
    "week":  {"name": "🗓 Неделя",  "price": 199,  "days": 7,   "req_per_day": 10},
    "month": {"name": "📅 Месяц",   "price": 599,  "days": 30,  "req_per_day": 15},
    "year":  {"name": "🏆 Год",     "price": 4999, "days": 365, "req_per_day": 20},
}

FREE_LIMIT = 3  # бесплатных запросов всего

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

# ══════════════════════════════════════════════════════════════════════════════
#  БАЗА ДАННЫХ
# ══════════════════════════════════════════════════════════════════════════════
def init_db():
    conn = sqlite3.connect("bot.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id     INTEGER PRIMARY KEY,
            username    TEXT,
            full_name   TEXT,
            free_used   INTEGER DEFAULT 0,
            plan        TEXT DEFAULT NULL,
            plan_until  TEXT DEFAULT NULL,
            day_used    INTEGER DEFAULT 0,
            last_day    TEXT DEFAULT NULL,
            joined_at   TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def get_user(uid: int):
    conn = sqlite3.connect("bot.db")
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id=?", (uid,))
    row = c.fetchone()
    conn.close()
    return row

def ensure_user(uid: int, username: str, full_name: str):
    conn = sqlite3.connect("bot.db")
    c = conn.cursor()
    c.execute("""
        INSERT OR IGNORE INTO users (user_id, username, full_name)
        VALUES (?, ?, ?)
    """, (uid, username, full_name))
    conn.commit()
    conn.close()

def get_free_used(uid: int) -> int:
    conn = sqlite3.connect("bot.db")
    c = conn.cursor()
    c.execute("SELECT free_used FROM users WHERE user_id=?", (uid,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0

def increment_free(uid: int):
    conn = sqlite3.connect("bot.db")
    c = conn.cursor()
    c.execute("UPDATE users SET free_used=free_used+1 WHERE user_id=?", (uid,))
    conn.commit()
    conn.close()

def get_plan(uid: int):
    """Возвращает (plan, plan_until) или (None, None)"""
    conn = sqlite3.connect("bot.db")
    c = conn.cursor()
    c.execute("SELECT plan, plan_until FROM users WHERE user_id=?", (uid,))
    row = c.fetchone()
    conn.close()
    if not row or not row[0]:
        return None, None
    plan, until = row
    if datetime.fromisoformat(until) < datetime.now():
        return None, None
    return plan, until

def set_plan(uid: int, plan: str):
    days = PLANS[plan]["days"]
    until = (datetime.now() + timedelta(days=days)).isoformat()
    conn = sqlite3.connect("bot.db")
    c = conn.cursor()
    c.execute("UPDATE users SET plan=?, plan_until=? WHERE user_id=?", (plan, until, uid))
    conn.commit()
    conn.close()

def check_day_limit(uid: int, plan: str) -> tuple[bool, int]:
    """Возвращает (можно_делать_запрос, использовано_сегодня)"""
    today = datetime.now().strftime("%Y-%m-%d")
    conn = sqlite3.connect("bot.db")
    c = conn.cursor()
    c.execute("SELECT day_used, last_day FROM users WHERE user_id=?", (uid,))
    row = c.fetchone()
    conn.close()
    if not row:
        return True, 0
    day_used, last_day = row
    if last_day != today:
        # Новый день — сброс
        conn = sqlite3.connect("bot.db")
        c = conn.cursor()
        c.execute("UPDATE users SET day_used=0, last_day=? WHERE user_id=?", (today, uid))
        conn.commit()
        conn.close()
        return True, 0
    limit = PLANS[plan]["req_per_day"]
    return day_used < limit, day_used

def increment_day(uid: int):
    today = datetime.now().strftime("%Y-%m-%d")
    conn = sqlite3.connect("bot.db")
    c = conn.cursor()
    c.execute("""
        UPDATE users SET
            day_used = CASE WHEN last_day=? THEN day_used+1 ELSE 1 END,
            last_day = ?
        WHERE user_id=?
    """, (today, today, uid))
    conn.commit()
    conn.close()

def get_all_users():
    conn = sqlite3.connect("bot.db")
    c = conn.cursor()
    c.execute("SELECT user_id, username, full_name, plan, plan_until FROM users")
    rows = c.fetchall()
    conn.close()
    return rows

# ══════════════════════════════════════════════════════════════════════════════
#  ОПРЕДЕЛЕНИЕ ВИДА СПОРТА
# ══════════════════════════════════════════════════════════════════════════════
def detect_sport(text: str) -> str:
    t = text.lower()
    cs2_words  = ["cs2", "cs 2", "counter-strike", "navi", "g2", "faze",
                  "astralis", "vitality", "natus vincere", "hltv", "ксту"]
    dota_words = ["dota", "дота", "team spirit", "tundra", "liquid",
                  "nigma", "gaimin", "betboom", "virtus.pro"]
    for w in cs2_words:
        if w in t:
            return "cs2"
    for w in dota_words:
        if w in t:
            return "dota"
    return "football"

# ══════════════════════════════════════════════════════════════════════════════
#  ФУТБОЛЬНАЯ СТАТИСТИКА
# ══════════════════════════════════════════════════════════════════════════════
async def get_football_stats(team1: str, team2: str) -> str:
    if not API_FOOTBALL_KEY:
        return ""
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r1 = await client.get(
                "https://v3.football.api-sports.io/teams",
                params={"search": team1},
                headers={"x-apisports-key": API_FOOTBALL_KEY}
            )
            r2 = await client.get(
                "https://v3.football.api-sports.io/teams",
                params={"search": team2},
                headers={"x-apisports-key": API_FOOTBALL_KEY}
            )
            d1 = r1.json().get("response", [])
            d2 = r2.json().get("response", [])
            if not d1 or not d2:
                return ""
            tid1 = d1[0]["team"]["id"]
            tid2 = d2[0]["team"]["id"]

            async def last_matches(tid):
                r = await client.get(
                    "https://v3.football.api-sports.io/fixtures",
                    params={"team": tid, "last": 5, "status": "FT"},
                    headers={"x-apisports-key": API_FOOTBALL_KEY}
                )
                matches = r.json().get("response", [])
                results = []
                for m in matches:
                    home = m["teams"]["home"]
                    away = m["teams"]["away"]
                    g = m["goals"]
                    w = "W" if (home["winner"] and home["id"]==tid) or \
                               (away["winner"] and away["id"]==tid) else \
                        "D" if not home["winner"] and not away["winner"] else "L"
                    results.append(f"{home['name']} {g['home']}:{g['away']} {away['name']} [{w}]")
                return results

            last1 = await last_matches(tid1)
            last2 = await last_matches(tid2)
            return (f"Последние 5 матчей {team1}: {', '.join(last1)}\n"
                    f"Последние 5 матчей {team2}: {', '.join(last2)}")
    except Exception as e:
        logger.warning(f"API-Football error: {e}")
        return ""

# ══════════════════════════════════════════════════════════════════════════════
#  АНАЛИЗ ЧЕРЕЗ CLAUDE
# ══════════════════════════════════════════════════════════════════════════════
SYSTEM_PROMPT = """Ты — профессиональный спортивный аналитик и эксперт по ставкам.
Анализируешь матчи по футболу (топ-лиги Европы, еврокубки), CS2 и Dota 2.

Структура ответа:
1. 📊 Краткий анализ обеих команд (форма, сильные/слабые стороны)
2. 🔑 Ключевые факторы матча (травмы, мотивация, статистика)
3. 🎯 Прогноз с чётким обоснованием
4. 💡 Рекомендуемая ставка и примерный коэффициент
5. 📈 Уровень уверенности в % (честно, не завышай выше 75% без весомых причин)

Стиль: экспертный, конкретный, без воды. Используй эмодзи для структуры.
Форматируй через обычный Markdown Telegram (*жирный*, _курсив_).
Никогда не давай гарантий. Всегда добавляй дисклеймер в конце.
Отвечай на русском языке."""

async def analyze_match(user_text: str, sport: str, extra_stats: str = "") -> str:
    sport_context = {
        "football": "ФУТБОЛ. Анализируй форму команд, xG, очные встречи, травмы, мотивацию, турнирное положение.",
        "cs2":      "CS2. Анализируй рейтинг HLTV, карточный пул, форму игроков, последние результаты на турнирах.",
        "dota":     "DOTA 2. Анализируй винрейт команд, текущую мету, стиль игры, пик-фазу, последние результаты.",
    }
    msg = f"""Проанализируй матч: {user_text}

Вид спорта: {sport_context[sport]}
{"Данные из API: " + extra_stats if extra_stats else "Используй актуальные знания о командах."}

Дай полный профессиональный разбор."""

    response = anthropic_client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": msg}]
    )
    return response.content[0].text

# ══════════════════════════════════════════════════════════════════════════════
#  ХЭНДЛЕРЫ
# ══════════════════════════════════════════════════════════════════════════════
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username or "", user.full_name or "")

    plan, until = get_plan(user.id)
    free_used = get_free_used(user.id)
    free_left = max(0, FREE_LIMIT - free_used)

    if plan:
        until_str = datetime.fromisoformat(until).strftime("%d.%m.%Y")
        status = f"✅ Подписка *{PLANS[plan]['name']}* активна до {until_str}"
    else:
        status = f"🎁 Бесплатных анализов: *{free_left}* из {FREE_LIMIT}"

    text = (
        f"👋 Привет, *{user.first_name}*!\n\n"
        f"🤖 Я — *StatEdge Bot*\n"
        f"Напиши любой матч — получи профессиональный анализ с прогнозом.\n\n"
        f"📌 *Примеры:*\n"
        f"• `Реал Мадрид — Барселона`\n"
        f"• `NaVi vs G2 CS2`\n"
        f"• `Team Spirit — Tundra Dota`\n\n"
        f"{status}\n\n"
        f"⚽🎮🐉 Футбол • CS2 • Dota 2"
    )
    keyboard = [
        [InlineKeyboardButton("💎 Тарифы и подписка", callback_data="show_plans")],
        [InlineKeyboardButton("📊 Моя статистика", callback_data="my_stats")],
        [InlineKeyboardButton("🎁 +2 запроса за подписку на канал", callback_data="sub_bonus")],
    ]
    await update.message.reply_text(
        text, parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def cmd_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_plans(update.message)

async def show_plans(message):
    text = (
        "💎 *Выбери тариф:*\n\n"
        "🗓 *Неделя — 199₽*\n"
        "└ 10 анализов в день • 7 дней\n\n"
        "📅 *Месяц — 599₽* ⭐ популярный\n"
        "└ 15 анализов в день • 30 дней\n\n"
        "🏆 *Год — 4 999₽* 🔥 выгоднее на 44%\n"
        "└ 20 анализов в день • 365 дней\n\n"
        "_Оплата через ЮКасса — карта, СБП, МИР_"
    )
    keyboard = [
        [InlineKeyboardButton("🗓 Неделя — 199₽", callback_data="pay_week")],
        [InlineKeyboardButton("📅 Месяц — 599₽ ⭐", callback_data="pay_month")],
        [InlineKeyboardButton("🏆 Год — 4 999₽ 🔥", callback_data="pay_year")],
    ]
    await message.reply_text(
        text, parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    await query.answer()

    if query.data == "show_plans":
        await show_plans(query.message)

    elif query.data == "my_stats":
        plan, until = get_plan(uid)
        free_used = get_free_used(uid)
        free_left = max(0, FREE_LIMIT - free_used)
        if plan:
            until_str = datetime.fromisoformat(until).strftime("%d.%m.%Y")
            can, day_used = check_day_limit(uid, plan)
            day_limit = PLANS[plan]["req_per_day"]
            plan_info = (
                f"✅ Тариф: *{PLANS[plan]['name']}*\n"
                f"📅 Активен до: *{until_str}*\n"
                f"📊 Сегодня запросов: *{day_used}/{day_limit}*"
            )
        else:
            plan_info = f"❌ Подписки нет\n🎁 Бесплатных осталось: *{free_left}*"
        await query.message.reply_text(
            f"📊 *Твой аккаунт:*\n\n{plan_info}",
            parse_mode=ParseMode.MARKDOWN
        )

    elif query.data == "sub_bonus":
        # Проверяем подписку на канал
        try:
            member = await context.bot.get_chat_member(CHANNEL_USERNAME, uid)
            if member.status in ("member", "administrator", "creator"):
                # Уже подписан — даём бонус
                conn = __import__("sqlite3").connect("bot.db")
                c = conn.cursor()
                c.execute("SELECT free_used FROM users WHERE user_id=?", (uid,))
                row = c.fetchone()
                if row and row[0] > 1:
                    c.execute("UPDATE users SET free_used=free_used-2 WHERE user_id=?", (uid,))
                    conn.commit()
                    conn.close()
                    await query.message.reply_text(
                        "✅ *+2 анализа добавлено!*\n\nСпасибо за подписку на канал 🎉",
                        parse_mode=ParseMode.MARKDOWN
                    )
                else:
                    conn.close()
                    await query.message.reply_text("✅ Ты уже подписан! Бонус уже был начислен.")
            else:
                keyboard = [[InlineKeyboardButton(
                    "📢 Подписаться на канал", url=f"https://t.me/{CHANNEL_USERNAME.strip('@')}"
                )]]
                await query.message.reply_text(
                    f"📢 Подпишись на канал *{CHANNEL_USERNAME}*\nи получи *+2 бесплатных анализа*!",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
        except Exception:
            await query.message.reply_text(
                "Сначала укажи username канала в настройках бота."
            )

    elif query.data in ("pay_week", "pay_month", "pay_year"):
        plan_key = query.data.replace("pay_", "")
        plan = PLANS[plan_key]
        text = (
            f"🔧 *Оплата временно недоступна*\n\n"
            f"Система оплаты для тарифа *{plan['name']}* "
            f"сейчас дорабатывается и скоро будет запущена.\n\n"
            f"🔔 Напиши нам — оформим подписку вручную:\n"
            f"👉 @statedge\\_support\n\n"
            f"_Приносим извинения за неудобства!_"
        )
        await query.message.reply_text(
            text, parse_mode=ParseMode.MARKDOWN
        )

    elif query.data.startswith("paid_"):
        plan_key = query.data.replace("paid_", "")
        plan = PLANS[plan_key]
        user = query.from_user
        # Уведомляем админа
        if ADMIN_ID:
            await context.bot.send_message(
                ADMIN_ID,
                f"💰 *Новая оплата!*\n\n"
                f"👤 {user.full_name} (@{user.username})\n"
                f"🆔 ID: `{user.id}`\n"
                f"📦 Тариф: {plan['name']} — {plan['price']}₽\n\n"
                f"Активировать: /activate {user.id} {plan_key}",
                parse_mode=ParseMode.MARKDOWN
            )
        await query.message.reply_text(
            "⏳ *Заявка отправлена!*\n\n"
            "Подписка будет активирована в течение 15 минут.\n"
            "Ожидай сообщения от бота.",
            parse_mode=ParseMode.MARKDOWN
        )

async def cmd_activate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для админа: /activate USER_ID PLAN"""
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        target_uid = int(context.args[0])
        plan_key = context.args[1]
        if plan_key not in PLANS:
            await update.message.reply_text("Неверный план. Используй: week, month, year")
            return
        set_plan(target_uid, plan_key)
        plan = PLANS[plan_key]
        until = (datetime.now() + timedelta(days=plan["days"])).strftime("%d.%m.%Y")
        # Уведомляем пользователя
        await context.bot.send_message(
            target_uid,
            f"✅ *Подписка активирована!*\n\n"
            f"📦 Тариф: *{plan['name']}*\n"
            f"📅 Активна до: *{until}*\n"
            f"📊 Лимит: *{plan['req_per_day']} анализов в день*\n\n"
            f"Пиши любой матч — анализирую! ⚽🎮🐉",
            parse_mode=ParseMode.MARKDOWN
        )
        await update.message.reply_text(f"✅ Подписка {plan['name']} активирована для {target_uid}")
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}\nИспользование: /activate USER_ID PLAN")

async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Статистика пользователей для админа"""
    if update.effective_user.id != ADMIN_ID:
        return
    users = get_all_users()
    total = len(users)
    active = sum(1 for u in users if u[3] and u[4] and
                 datetime.fromisoformat(u[4]) > datetime.now())
    text = (
        f"📊 *Статистика бота:*\n\n"
        f"👥 Всего пользователей: *{total}*\n"
        f"✅ Активных подписок: *{active}*\n"
        f"🆓 Без подписки: *{total - active}*"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = user.id
    text = update.message.text.strip()

    ensure_user(uid, user.username or "", user.full_name or "")

    if len(text) < 3:
        await update.message.reply_text(
            "Напиши матч, например: `Реал — Барса`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # Проверяем подписку
    plan, until = get_plan(uid)

    if plan:
        # Платный — проверяем дневной лимит
        can, day_used = check_day_limit(uid, plan)
        day_limit = PLANS[plan]["req_per_day"]
        if not can:
            await update.message.reply_text(
                f"⛔ *Дневной лимит исчерпан*\n\n"
                f"Использовано: *{day_used}/{day_limit}* анализов сегодня.\n"
                f"Лимит обновится завтра в 00:00 🕛",
                parse_mode=ParseMode.MARKDOWN
            )
            return
    else:
        # Бесплатный
        free_used = get_free_used(uid)
        if free_used >= FREE_LIMIT:
            keyboard = [
                [InlineKeyboardButton("💎 Выбрать тариф", callback_data="show_plans")],
                [InlineKeyboardButton("🎁 +2 запроса бесплатно", callback_data="sub_bonus")],
            ]
            await update.message.reply_text(
                f"⛔ *Бесплатный лимит исчерпан* ({FREE_LIMIT} анализа)\n\n"
                f"Оформи подписку или получи +2 анализа бесплатно за подписку на канал:",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return

    # Анализируем
    await update.message.chat.send_action(ChatAction.TYPING)
    wait_msg = await update.message.reply_text("🔍 Анализирую матч, подожди 15–20 секунд...")

    sport = detect_sport(text)
    sport_emoji = {"football": "⚽", "cs2": "🎮", "dota": "🐉"}[sport]

    extra_stats = ""
    if sport == "football" and API_FOOTBALL_KEY:
        parts = text.replace(" vs ", " — ").replace(" - ", " — ").split(" — ")
        if len(parts) == 2:
            extra_stats = await get_football_stats(parts[0].strip(), parts[1].strip())

    try:
        analysis = await analyze_match(text, sport, extra_stats)

        # Обновляем счётчики
        if plan:
            increment_day(uid)
            can, day_used = check_day_limit(uid, plan)
            day_limit = PLANS[plan]["req_per_day"]
            counter_info = f"_📊 Сегодня запросов: {day_used}/{day_limit}_"
        else:
            increment_free(uid)
            free_used = get_free_used(uid)
            free_left = max(0, FREE_LIMIT - free_used)
            counter_info = (
                f"_🎁 Осталось бесплатных: {free_left}_"
                if free_left > 0 else
                "_🔴 Это был последний бесплатный анализ. Оформи подписку!_"
            )

        partner_url = PARTNER_LINKS[sport]
        keyboard = [[
            InlineKeyboardButton(
                "💰 Сделать ставку" if sport == "football" else "🎯 Поставить на победителя",
                url=partner_url
            )
        ]]
        if not plan:
            keyboard.append([InlineKeyboardButton("💎 Оформить подписку", callback_data="show_plans")])

        footer = (
            f"\n\n{sport_emoji} *{sport.upper()}* • "
            f"📅 {datetime.now().strftime('%d.%m.%Y %H:%M')}\n"
            f"{counter_info}\n"
            f"_⚠️ Это аналитика, не гарантия. Ставь ответственно._"
        )

        await wait_msg.delete()
        await update.message.reply_text(
            analysis + footer,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    except Exception as e:
        logger.error(f"Analysis error: {e}")
        await wait_msg.edit_text(
            "❌ Ошибка при анализе. Попробуй ещё раз или напиши матч по-другому."
        )

# ══════════════════════════════════════════════════════════════════════════════
#  ЗАПУСК
# ══════════════════════════════════════════════════════════════════════════════
def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("subscribe", cmd_subscribe))
    app.add_handler(CommandHandler("activate",  cmd_activate))
    app.add_handler(CommandHandler("users",     cmd_users))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("StatEdge Bot запущен")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
