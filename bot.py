import os
import asyncio
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from telegram.constants import ParseMode, ChatAction
import anthropic
import httpx
from datetime import datetime

# ─── Логирование ───────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─── Токены (берутся из переменных окружения) ──────────────────────────────────
TELEGRAM_TOKEN   = "8905009739:AAF5TsZre4WhAa1F4Uqdxu0J1qkZZVme_kc"
ANTHROPIC_KEY    = "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"
API_FOOTBALL_KEY = os.environ.get("API_FOOTBALL_KEY", "")   # опционально
PARTNER_LINK     = "https://1xbet.com/ru"
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

# ─── Партнёрские ссылки по виду спорта ────────────────────────────────────────
PARTNER_LINKS = {
    "football": os.environ.get("PARTNER_LINK_FOOTBALL", PARTNER_LINK),
    "cs2":      os.environ.get("PARTNER_LINK_CS2",      PARTNER_LINK),
    "dota":     os.environ.get("PARTNER_LINK_DOTA",     PARTNER_LINK),
}

# ─── Лимиты бесплатных запросов ───────────────────────────────────────────────
FREE_LIMIT = 3
user_requests: dict[int, int] = {}   # user_id → кол-во использованных запросов

# ══════════════════════════════════════════════════════════════════════════════
#  ОПРЕДЕЛЕНИЕ ВИДА СПОРТА
# ══════════════════════════════════════════════════════════════════════════════
def detect_sport(text: str) -> str:
    t = text.lower()
    cs2_words  = ["cs2", "cs 2", "counter", "navi", "g2", "faze", "astralis",
                  "vitality", "natus vincere", "героев", "hltv"]
    dota_words = ["dota", "дота", "team spirit", "tundra", "liquid", "nigma",
                  "og ", " og", "virtus", "gaimin"]
    for w in cs2_words:
        if w in t:
            return "cs2"
    for w in dota_words:
        if w in t:
            return "dota"
    return "football"

# ══════════════════════════════════════════════════════════════════════════════
#  ПОЛУЧЕНИЕ СТАТИСТИКИ ФУТБОЛ (API-Football)
# ══════════════════════════════════════════════════════════════════════════════
async def get_football_stats(team1: str, team2: str) -> str:
    """Пробуем получить реальную статистику. Если ключа нет — возвращаем пустую строку."""
    if not API_FOOTBALL_KEY:
        return ""
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            # Ищем команду 1
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
            # Последние матчи
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
                    g    = m["goals"]
                    winner = "W" if (home["winner"] and home["id"] == tid) or \
                                    (away["winner"] and away["id"] == tid) else \
                             "D" if not home["winner"] and not away["winner"] else "L"
                    results.append(f"{home['name']} {g['home']}:{g['away']} {away['name']} [{winner}]")
                return results

            last1 = await last_matches(tid1)
            last2 = await last_matches(tid2)
            stats = (
                f"Последние 5 матчей {team1}: {', '.join(last1)}\n"
                f"Последние 5 матчей {team2}: {', '.join(last2)}"
            )
            return stats
    except Exception as e:
        logger.warning(f"API-Football error: {e}")
        return ""

# ══════════════════════════════════════════════════════════════════════════════
#  АНАЛИЗ ЧЕРЕЗ CLAUDE
# ══════════════════════════════════════════════════════════════════════════════
SYSTEM_PROMPT = """Ты — профессиональный спортивный аналитик и эксперт по ставкам.
Ты анализируешь матчи по футболу, CS2 и Dota 2.

Твои ответы всегда содержат:
1. Краткий анализ обеих команд (форма, сильные/слабые стороны)
2. Ключевые факторы матча
3. Конкретный прогноз с обоснованием
4. Рекомендуемую ставку и примерный коэффициент
5. Уровень уверенности в % (честно, не завышай)

Стиль: экспертный, конкретный, без воды. Используй эмодзи для структуры.
Формат ответа строго в Telegram Markdown (жирный через *текст*, курсив __текст__).
Никогда не давай гарантий выигрыша. Всегда добавляй короткий дисклеймер.
Отвечай на русском языке."""

async def analyze_match(user_text: str, sport: str, extra_stats: str = "") -> str:
    sport_context = {
        "football": "Это матч по ФУТБОЛУ. Анализируй форму команд, xG, очные встречи, травмы, мотивацию.",
        "cs2":      "Это матч по CS2. Анализируй рейтинг HLTV, карточный пул, форму игроков, последние результаты.",
        "dota":     "Это матч по DOTA 2. Анализируй винрейт команд, текущую мету, стиль игры, пик-фазу.",
    }

    user_msg = f"""Проанализируй матч: {user_text}

{sport_context[sport]}
{"Дополнительные данные из API: " + extra_stats if extra_stats else "Используй свои знания о командах."}

Дай полный профессиональный разбор."""

    response = anthropic_client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}]
    )
    return response.content[0].text

# ══════════════════════════════════════════════════════════════════════════════
#  HANDLERS
# ══════════════════════════════════════════════════════════════════════════════
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (
        f"👋 Привет, *{user.first_name}*\\!\n\n"
        "🤖 Я — *BetAnalytics Bot*\n"
        "Напиши мне любой матч и я выдам профессиональный анализ с прогнозом\\.\n\n"
        "📌 *Примеры запросов:*\n"
        "• `Реал Мадрид — Барселона`\n"
        "• `NaVi vs G2 CS2`\n"
        "• `Team Spirit — Tundra Dota`\n\n"
        f"🎁 У тебя есть *{FREE_LIMIT} бесплатных анализа*\\.\n\n"
        "⚽🎮 Поддерживаю: Футбол, CS2, Dota 2"
    )
    keyboard = [[InlineKeyboardButton("📊 Как использовать", callback_data="help")]]
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 *Как пользоваться ботом:*\n\n"
        "Просто напиши матч в любом формате\\:\n"
        "• `Реал — Барса`\n"
        "• `Manchester City vs Arsenal`\n"
        "• `NaVi vs G2`\n"
        "• `Team Spirit Dota`\n\n"
        "🤖 Бот автоматически определит вид спорта и выдаст анализ\\.\n\n"
        "💎 *Тарифы:*\n"
        f"• Бесплатно: {FREE_LIMIT} анализа\n"
        "• Подписка: 299₽/мес — безлимит\n\n"
        "❓ Вопросы: @your\\_admin"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    used  = user_requests.get(uid, 0)
    left  = max(0, FREE_LIMIT - used)
    text  = (
        f"📊 *Твоя статистика:*\n\n"
        f"Использовано запросов: *{used}*\n"
        f"Осталось бесплатных: *{left}*\n\n"
        "Для безлимитного доступа — /subscribe"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def cmd_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("💳 Подписка 299₽/мес", callback_data="pay_monthly")],
        [InlineKeyboardButton("💎 Подписка 699₽/3 мес", callback_data="pay_3month")],
    ]
    await update.message.reply_text(
        "💎 *Безлимитный доступ к анализу любых матчей*\n\n"
        "Выбери тариф:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "help":
        await query.message.reply_text(
            "Напиши название матча — например:\n`Реал Мадрид — Барселона`\n`NaVi vs G2`",
            parse_mode=ParseMode.MARKDOWN
        )
    elif query.data in ("pay_monthly", "pay_3month"):
        await query.message.reply_text(
            "💳 Для оплаты напиши @your_admin\n"
            "После оплаты доступ активируется в течение 5 минут."
        )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    text = update.message.text.strip()

    if len(text) < 3:
        await update.message.reply_text("Напиши матч, например: `Реал — Барса`", parse_mode=ParseMode.MARKDOWN)
        return

    # Проверка лимита
    used = user_requests.get(uid, 0)
    if used >= FREE_LIMIT:
        keyboard = [[InlineKeyboardButton("💎 Получить безлимит", callback_data="pay_monthly")]]
        await update.message.reply_text(
            f"⛔ Бесплатный лимит исчерпан ({FREE_LIMIT} анализа).\n\n"
            "Оформи подписку — *299₽/мес* за безлимитный доступ:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    # Показываем что бот работает
    await update.message.chat.send_action(ChatAction.TYPING)
    wait_msg = await update.message.reply_text("🔍 Анализирую матч, подожди 10–20 секунд...")

    sport = detect_sport(text)
    sport_emoji = {"football": "⚽", "cs2": "🎮", "dota": "🐉"}[sport]

    # Пробуем получить реальную статистику для футбола
    extra_stats = ""
    if sport == "football" and API_FOOTBALL_KEY:
        parts = text.replace(" vs ", " — ").replace(" - ", " — ").split(" — ")
        if len(parts) == 2:
            extra_stats = await get_football_stats(parts[0].strip(), parts[1].strip())

    try:
        analysis = await analyze_match(text, sport, extra_stats)

        # Счётчик запросов
        user_requests[uid] = used + 1
        left = FREE_LIMIT - user_requests[uid]

        # Партнёрская кнопка
        partner_url = PARTNER_LINKS[sport]
        keyboard = [[
            InlineKeyboardButton(
                f"{'💰 Сделать ставку на матч' if sport == 'football' else '🎯 Поставить на победителя'}",
                url=partner_url
            )
        ]]

        footer = (
            f"\n\n{sport_emoji} *Вид спорта:* {sport.upper()}"
            f"\n📅 {datetime.now().strftime('%d.%m.%Y %H:%M')}"
            f"\n\n_⚠️ Это аналитика, не гарантия. Ставь ответственно._"
            + (f"\n_Осталось бесплатных запросов: {left}_" if left > 0 else
               "\n_🔴 Это был последний бесплатный запрос._")
        )

        await wait_msg.delete()
        await update.message.reply_text(
            analysis + footer,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    except Exception as e:
        logger.error(f"Analysis error: {e}")
        await wait_msg.edit_text("❌ Ошибка при анализе. Попробуй ещё раз или напиши матч по-другому.")

# ══════════════════════════════════════════════════════════════════════════════
#  ЗАПУСК
# ══════════════════════════════════════════════════════════════════════════════
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CommandHandler("stats",     cmd_stats))
    app.add_handler(CommandHandler("subscribe", cmd_subscribe))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("BetAnalytics Bot запущен")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
