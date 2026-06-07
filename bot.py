import os
import logging
import sqlite3
import json
import uuid
import asyncio
from datetime import datetime, timedelta
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
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
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_KEY    = os.environ["ANTHROPIC_API_KEY"]
RAPIDAPI_KEY     = os.environ["RAPIDAPI_KEY"]
ODDS_API_KEY     = os.environ["ODDS_API_KEY"]
PANDASCORE_KEY   = os.environ["PANDASCORE_KEY"]
STRATZ_TOKEN     = os.environ["STRATZ_TOKEN"]

YOKASSA_SHOP_ID  = os.environ["YOKASSA_SHOP_ID"]
YOKASSA_KEY      = os.environ["YOKASSA_KEY"]
ADMIN_ID         = 5555668323
CHANNEL_USERNAME = "@твой_канал"
WEBHOOK_URL      = "https://betbot-production-1cb7.up.railway.app"

PARTNER_LINKS = {
    "football": os.environ.get("PARTNER_LINK_FOOTBALL", "https://lknt.pro/ea418b"),
    "cs2":      os.environ.get("PARTNER_LINK_CS2",      "https://lknt.pro/ea418b"),
    "dota":     os.environ.get("PARTNER_LINK_DOTA",     "https://lknt.pro/ea418b"),
}

PLANS = {
    "basic_month": {"name": "🔵 Basic",  "price": 399,  "days": 30,  "req_per_day": 5,  "web_search": False},
    "basic_year":  {"name": "🔵 Basic",  "price": 2990, "days": 365, "req_per_day": 5,  "web_search": False},
    "pro_month":   {"name": "🔥 Pro",    "price": 799,  "days": 30,  "req_per_day": 15, "web_search": True},
    "pro_year":    {"name": "🔥 Pro",    "price": 5990, "days": 365, "req_per_day": 15, "web_search": True},
}

# Человекочитаемые периоды для отображения
PLAN_PERIOD = {
    "basic_month": "месяц",
    "basic_year":  "год",
    "pro_month":   "месяц",
    "pro_year":    "год",
}

FREE_LIMIT = 2  # запросов всего (не в день) для незарегистрированных

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
            joined_at   TEXT DEFAULT CURRENT_TIMESTAMP,
            referred_by INTEGER DEFAULT NULL,
            ref_count   INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

def ensure_user(uid, username, full_name):
    conn = sqlite3.connect("bot.db")
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id, username, full_name) VALUES (?,?,?)",
              (uid, username, full_name))
    conn.commit()
    conn.close()

def get_free_used(uid):
    conn = sqlite3.connect("bot.db")
    c = conn.cursor()
    c.execute("SELECT free_used FROM users WHERE user_id=?", (uid,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0

def increment_free(uid):
    conn = sqlite3.connect("bot.db")
    c = conn.cursor()
    c.execute("UPDATE users SET free_used=free_used+1 WHERE user_id=?", (uid,))
    conn.commit()
    conn.close()

def get_plan(uid):
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

def set_plan(uid, plan):
    days = PLANS[plan]["days"]
    until = (datetime.now() + timedelta(days=days)).isoformat()
    conn = sqlite3.connect("bot.db")
    c = conn.cursor()
    c.execute("UPDATE users SET plan=?, plan_until=? WHERE user_id=?", (plan, until, uid))
    conn.commit()
    conn.close()

def check_day_limit(uid, plan):
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
        conn = sqlite3.connect("bot.db")
        c = conn.cursor()
        c.execute("UPDATE users SET day_used=0, last_day=? WHERE user_id=?", (today, uid))
        conn.commit()
        conn.close()
        return True, 0
    limit = PLANS[plan]["req_per_day"]
    return day_used < limit, day_used

def increment_day(uid):
    today = datetime.now().strftime("%Y-%m-%d")
    conn = sqlite3.connect("bot.db")
    c = conn.cursor()
    c.execute("""UPDATE users SET
        day_used = CASE WHEN last_day=? THEN day_used+1 ELSE 1 END,
        last_day = ? WHERE user_id=?""", (today, today, uid))
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
#  PANDASCORE — CS2 статистика
# ══════════════════════════════════════════════════════════════════════════════
async def get_cs2_stats(team1: str, team2: str) -> str:
    """Получаем CS2 статистику через PandaScore — 3 запроса вместо 5"""
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            headers = {"Authorization": f"Bearer {PANDASCORE_KEY}"}

            async def search_team(name):
                r = await client.get(
                    "https://api.pandascore.co/csgo/teams",
                    params={"search[name]": name, "per_page": 1},
                    headers=headers
                )
                data = r.json()
                return data[0] if data and isinstance(data, list) else None

            async def get_recent_matches(team_id):
                r = await client.get(
                    "https://api.pandascore.co/csgo/matches/past",
                    params={"filter[opponent_id]": team_id, "per_page": 3, "sort": "-begin_at"},
                    headers=headers
                )
                matches = r.json()
                results = []
                if isinstance(matches, list):
                    for m in matches:
                        opponents = m.get("opponents", [])
                        winner = m.get("winner", {})
                        w_name = winner.get("name", "?") if winner else "?"
                        teams = " vs ".join([o.get("opponent", {}).get("name", "?") for o in opponents])
                        results.append(f"{teams} → {w_name}")
                return results

            # Параллельный поиск обеих команд — 2 запроса одновременно
            t1, t2 = await asyncio.gather(search_team(team1), search_team(team2))

            if not t1 or not t2:
                return ""

            # Параллельный запрос матчей — ещё 2 запроса одновременно (итого 4→3 раундов)
            last1, last2 = await asyncio.gather(
                get_recent_matches(t1["id"]),
                get_recent_matches(t2["id"])
            )

            stats = "📊 CS2 статистика (PandaScore):\n"
            if last1:
                stats += f"{team1} — последние матчи: {' | '.join(last1)}\n"
            if last2:
                stats += f"{team2} — последние матчи: {' | '.join(last2)}\n"
            stats += f"ID {team1}: {t1.get('id')} | ID {team2}: {t2.get('id')}\n"
            return stats

    except Exception as e:
        logger.warning(f"PandaScore CS2 error: {e}")
        return ""

# ══════════════════════════════════════════════════════════════════════════════
#  STRATZ — Dota 2 статистика (GraphQL)
# ══════════════════════════════════════════════════════════════════════════════

# Алиасы — сокращения к точным названиям команд в Stratz
DOTA_TEAM_ALIASES = {
    "bb team":          "BetBoom Team",
    "bb":               "BetBoom Team",
    "betboom":          "BetBoom Team",
    "lgd":              "PSG.LGD",
    "psg.lgd":          "PSG.LGD",
    "spirit":           "Team Spirit",
    "yandex":           "Team Yandex",
    "navi":             "Natus Vincere",
    "natus vincere":    "Natus Vincere",
    "vp":               "Virtus.pro",
    "virtus.pro":       "Virtus.pro",
    "og":               "OG",
    "liquid":           "Team Liquid",
    "tundra":           "Tundra Esports",
    "gaimin":           "Gaimin Gladiators",
    "aster":            "Team Aster",
    "falcons":          "Team Falcons",
    "aurora":           "Aurora Gaming",
    "9pandas":          "9Pandas",
    "nouns":            "Nouns",
    "twisted minds":    "Twisted Minds",
    "entity":           "Entity",
    "talon":            "Talon Esports",
    "bleed":            "Bleed Esports",
}

async def _stratz_query(query: str) -> dict:
    """Выполняем GraphQL запрос к Stratz API"""
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            "https://api.stratz.com/graphql",
            json={"query": query},
            headers={
                "Authorization": f"Bearer {STRATZ_TOKEN}",
                "Content-Type": "application/json",
                "User-Agent": "BetMindBot/1.0"
            }
        )
        r.raise_for_status()
        return r.json()

async def _stratz_search_team(name: str) -> dict | None:
    """Ищем команду по названию через Stratz"""
    search_name = DOTA_TEAM_ALIASES.get(name.lower().strip(), name)
    query = f"""
    {{
      stratz {{
        search(query: "{search_name}", filter: {{ leagueTeam: true }}) {{
          teams {{
            id
            name
            tag
          }}
        }}
      }}
    }}
    """
    try:
        data = await _stratz_query(query)
        teams = (
            data.get("data", {})
                .get("stratz", {})
                .get("search", {})
                .get("teams", [])
        )
        if teams:
            logger.info(f"Stratz found '{name}' → '{teams[0].get('name')}' (id={teams[0].get('id')})")
            return teams[0]
        logger.warning(f"Stratz: команда '{name}' (искал '{search_name}') не найдена")
        return None
    except Exception as e:
        logger.warning(f"Stratz search error for '{name}': {e}")
        return None

async def _stratz_team_matches(team_id: int) -> list[str]:
    """Получаем последние 5 матчей команды"""
    query = f"""
    {{
      team(teamId: {team_id}) {{
        matches(request: {{ take: 5 }}) {{
          id
          didRadiantWin
          radiantTeam {{ name }}
          direTeam {{ name }}
          leagueId
          endDateTime
        }}
      }}
    }}
    """
    try:
        data = await _stratz_query(query)
        matches = (
            data.get("data", {})
                .get("team", {})
                .get("matches", []) or []
        )
        results = []
        for m in matches:
            rad = (m.get("radiantTeam") or {}).get("name", "Radiant")
            dire = (m.get("direTeam") or {}).get("name", "Dire")
            radiant_won = m.get("didRadiantWin")
            # Определяем победителя
            if radiant_won is True:
                winner = rad
            elif radiant_won is False:
                winner = dire
            else:
                winner = "?"
            results.append(f"{rad} vs {dire} → {winner}")
        return results
    except Exception as e:
        logger.warning(f"Stratz matches error for team {team_id}: {e}")
        return []

async def get_dota_stats(team1: str, team2: str) -> str:
    """Получаем Dota 2 статистику через Stratz GraphQL"""
    try:
        # Параллельный поиск обеих команд
        t1, t2 = await asyncio.gather(
            _stratz_search_team(team1),
            _stratz_search_team(team2)
        )

        if not t1 or not t2:
            return ""

        if t1["id"] == t2["id"]:
            logger.warning(f"Stratz: обе команды вернули один ID {t1['id']} — пропускаем")
            return ""

        # Параллельный запрос последних матчей
        last1, last2 = await asyncio.gather(
            _stratz_team_matches(t1["id"]),
            _stratz_team_matches(t2["id"])
        )

        stats = "📊 Dota 2 статистика (Stratz):\n"
        if last1:
            stats += f"{team1} — последние: {' | '.join(last1[:3])}\n"
        if last2:
            stats += f"{team2} — последние: {' | '.join(last2[:3])}\n"
        return stats

    except Exception as e:
        logger.warning(f"Stratz Dota2 error: {e}")
        return ""

# ══════════════════════════════════════════════════════════════════════════════
#  ЮКАССА — СОЗДАНИЕ ПЛАТЕЖА
# ══════════════════════════════════════════════════════════════════════════════
async def create_yokassa_payment(uid: int, plan_key: str) -> str:
    """Создаём платёж в ЮКассе и возвращаем ссылку на оплату"""
    import uuid
    plan = PLANS[plan_key]
    idempotence_key = str(uuid.uuid4())
    payload = {
        "amount": {
            "value": str(plan["price"]) + ".00",
            "currency": "RUB"
        },
        "confirmation": {
            "type": "redirect",
            "return_url": "https://t.me/BetMindBot"
        },
        "capture": True,
        "description": f"BetMind Bot — {plan['name']} (user {uid})",
        "metadata": {
            "user_id": str(uid),
            "plan_key": plan_key
        }
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                "https://api.yookassa.ru/v3/payments",
                json=payload,
                auth=(YOKASSA_SHOP_ID, YOKASSA_KEY),
                headers={"Idempotence-Key": idempotence_key}
            )
            data = r.json()
            if r.status_code == 200:
                return data["confirmation"]["confirmation_url"]
            else:
                logger.error(f"YooKassa error: {data}")
                return ""
    except Exception as e:
        logger.error(f"YooKassa request error: {e}")
        return ""

async def check_yokassa_payment(payment_id: str) -> dict:
    """Проверяем статус платежа"""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"https://api.yookassa.ru/v3/payments/{payment_id}",
                auth=(YOKASSA_SHOP_ID, YOKASSA_KEY)
            )
            return r.json()
    except Exception as e:
        logger.error(f"YooKassa check error: {e}")
        return {}

# ══════════════════════════════════════════════════════════════════════════════
#  ОПРЕДЕЛЕНИЕ ВИДА СПОРТА
# ══════════════════════════════════════════════════════════════════════════════
def detect_sport(text):
    t = text.lower()
    cs2_words  = ["cs2","cs 2","counter-strike","navi","g2 esports","faze",
                  "astralis","vitality","natus vincere","hltv","mouz","heroic"]
    dota_words = ["dota","дота","team spirit","tundra","nigma",
                  "gaimin","betboom","virtus.pro","og dota","thunder awaken",
                  # LGD
                  "lgd","psg.lgd","lgd gaming",
                  # Team Yandex
                  "team yandex","yandex",
                  # Другие топ-команды
                  "team aster","aster","nouns","bb team","9pandas",
                  "entity","aurora","talon","twisted minds",
                  "bleed","shopify rebellion","falcons",
                  # Турниры (помогают определить дота)
                  "blast slam","ti ","the international","dreamleague",
                  "esl one dota","riyadh masters"]
    for w in cs2_words:
        if w in t: return "cs2"
    for w in dota_words:
        if w in t: return "dota"
    return "football"

# ══════════════════════════════════════════════════════════════════════════════
#  СБОР ДАННЫХ — SOFASCORE (статистика команд)
# ══════════════════════════════════════════════════════════════════════════════
async def get_sofascore_stats(team1: str, team2: str) -> str:
    """Ищем команды и получаем их последние матчи через Sofascore API"""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            headers = {
                "x-rapidapi-host": "sofascore.p.rapidapi.com",
                "x-rapidapi-key": RAPIDAPI_KEY
            }

            async def search_team(name):
                r = await client.get(
                    "https://sofascore.p.rapidapi.com/teams/search",
                    params={"name": name},
                    headers=headers
                )
                data = r.json()
                teams = data.get("teams", [])
                return teams[0] if teams else None

            async def get_last_matches(team_id, team_name):
                r = await client.get(
                    f"https://sofascore.p.rapidapi.com/teams/{team_id}/events/last/0",
                    headers=headers
                )
                data = r.json()
                events = data.get("events", [])[:5]
                results = []
                for e in events:
                    home = e.get("homeTeam", {}).get("name", "?")
                    away = e.get("awayTeam", {}).get("name", "?")
                    hs = e.get("homeScore", {}).get("current", "?")
                    as_ = e.get("awayScore", {}).get("current", "?")
                    winner = e.get("winnerCode")
                    if winner == 1:
                        w = "П1"
                    elif winner == 2:
                        w = "П2"
                    else:
                        w = "Х"
                    results.append(f"{home} {hs}:{as_} {away} [{w}]")
                return results

            t1 = await search_team(team1)
            t2 = await search_team(team2)

            if not t1 or not t2:
                return ""

            last1 = await get_last_matches(t1["id"], team1)
            last2 = await get_last_matches(t2["id"], team2)

            # Рейтинги если есть
            rating1 = t1.get("ranking", "")
            rating2 = t2.get("ranking", "")

            stats = f"📊 Статистика из Sofascore:\n"
            stats += f"{team1} — последние 5: {' | '.join(last1)}\n"
            stats += f"{team2} — последние 5: {' | '.join(last2)}\n"
            if rating1:
                stats += f"Рейтинг {team1}: #{rating1}\n"
            if rating2:
                stats += f"Рейтинг {team2}: #{rating2}\n"

            return stats

    except Exception as e:
        logger.warning(f"Sofascore error: {e}")
        return ""

# ══════════════════════════════════════════════════════════════════════════════
#  СБОР ДАННЫХ — THE ODDS API (коэффициенты)
# ══════════════════════════════════════════════════════════════════════════════
async def get_odds(team1: str, team2: str, sport: str) -> str:
    """Получаем актуальные коэффициенты букмекеров"""
    # Odds API не поддерживает esports_dota2 — возвращаем пустую строку
    if sport == "dota":
        return ""

    sport_key = {
        "football": "soccer_epl",
        "cs2": "esports_cs2",
    }.get(sport, "soccer_epl")

    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(
                f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds",
                params={
                    "apiKey": ODDS_API_KEY,
                    "regions": "eu",
                    "markets": "h2h",
                    "oddsFormat": "decimal"
                }
            )
            events = r.json()
            if not isinstance(events, list):
                return ""

            t1_lower = team1.lower()
            t2_lower = team2.lower()

            for event in events:
                home = event.get("home_team", "").lower()
                away = event.get("away_team", "").lower()
                if (t1_lower in home or t1_lower in away or
                    t2_lower in home or t2_lower in away):

                    odds_info = f"\n💰 Коэффициенты букмекеров:\n"
                    bookmakers = event.get("bookmakers", [])[:3]
                    for bm in bookmakers:
                        bm_name = bm.get("title", "")
                        markets = bm.get("markets", [])
                        for market in markets:
                            if market.get("key") == "h2h":
                                outcomes = market.get("outcomes", [])
                                odds_str = " | ".join(
                                    f"{o['name']}: {o['price']}" for o in outcomes
                                )
                                odds_info += f"  {bm_name}: {odds_str}\n"
                    return odds_info

            return ""
    except Exception as e:
        logger.warning(f"Odds API error: {e}")
        return ""

# ══════════════════════════════════════════════════════════════════════════════
#  СБОР ДАННЫХ — ВЕБ-ПОИСК НОВОСТЕЙ (через Claude web_search)
# ══════════════════════════════════════════════════════════════════════════════
async def get_news(team1: str, team2: str) -> str:
    """Поиск новостей через Google RSS — бесплатно без ключа"""
    try:
        import urllib.parse
        query = urllib.parse.quote(f"{team1} {team2} травмы состав 2026")
        url = f"https://news.google.com/rss/search?q={query}&hl=ru&gl=RU&ceid=RU:ru"
        async with httpx.AsyncClient(timeout=8, follow_redirects=True) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                import re
                titles = re.findall(r"<title><!\[CDATA\[(.*?)\]\]></title>", r.text)
                titles = [t for t in titles if team1.lower()[:4] in t.lower() or 
                         team2.lower()[:4] in t.lower()][:3]
                if titles:
                    return f"\n📰 Свежие новости:\n" + "\n".join(f"• {t}" for t in titles)
        return ""
    except Exception as e:
        logger.warning(f"News search error: {e}")
        return ""

# ══════════════════════════════════════════════════════════════════════════════
#  ПОЛНЫЙ АНАЛИЗ ЧЕРЕЗ CLAUDE
# ══════════════════════════════════════════════════════════════════════════════
SYSTEM_PROMPT = """Ты — топовый спортивный аналитик и эксперт по ставкам с 15-летним опытом.
Анализируешь матчи по футболу (топ-лиги Европы, еврокубки), CS2 и Dota 2.
Сегодняшняя дата: {current_date}. Используй это при анализе.

У тебя есть реальные данные — используй их все.

Структура анализа:
1. 📊 *Статистика и форма* — разбор последних матчей, серии, тенденции
2. 🔑 *Ключевые факторы* — травмы, дисквалификации, мотивация, усталость
3. 💰 *Анализ коэффициентов* — где ценность, движение линии
4. 📰 *Актуальные новости* — инсайды, заявления тренеров
5. 🎯 *Прогноз* — конкретная ставка с чётким обоснованием
6. 📈 *Уверенность* — честный процент (не завышай выше 75% без весомых причин)

Стиль: экспертный, конкретный, с цифрами. Без воды.
Форматируй через Telegram Markdown (*жирный*, _курсив_).
Всегда добавляй дисклеймер в конце.
Отвечай на русском языке."""

async def analyze_match(user_text: str, sport: str,
                        stats: str = "", odds: str = "", news: str = "",
                        use_web_search: bool = False) -> str:
    sport_context = {
        "football": "ФУТБОЛ. Анализируй форму, xG, очные встречи, травмы, мотивацию, турнирное положение.",
        "cs2":      "CS2. Анализируй рейтинг HLTV, карточный пул, форму игроков, последние турнирные результаты.",
        "dota":     "DOTA 2. Анализируй винрейт, текущую мету, стиль игры, пик-фазу, последние результаты.",
    }

    data_block = ""
    if stats: data_block += stats + "\n"
    if odds:  data_block += odds + "\n"
    if news:  data_block += news + "\n"

    data_prefix = "Данные из API:\n" + data_block if data_block else "Используй актуальные знания о командах."
    msg = (
        f"Проанализируй матч: {user_text}\n\n"
        f"Вид спорта: {sport_context[sport]}\n\n"
        f"{data_prefix}\n\n"
        f"Дай полный профессиональный разбор по всем пунктам."
    )

    current_date = datetime.now().strftime("%d.%m.%Y")
    system = SYSTEM_PROMPT.replace("{current_date}", current_date)

    kwargs = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 1200,
        "system": system,
        "messages": [{"role": "user", "content": msg}]
    }

    if use_web_search:
        # Pro — 1 итерация web search
        kwargs["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 1}]

    response = anthropic_client.messages.create(**kwargs)
    return "".join(b.text for b in response.content if hasattr(b, "text"))

# ══════════════════════════════════════════════════════════════════════════════
#  HANDLERS — КОМАНДЫ
# ══════════════════════════════════════════════════════════════════════════════
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username or "", user.full_name or "")

    # Обрабатываем реферальную ссылку
    if context.args:
        try:
            referrer_id = int(context.args[0])
            if referrer_id != user.id:
                was_added = add_referral(user.id, referrer_id)
                if was_added:
                    try:
                        await context.bot.send_message(
                            referrer_id,
                            f"🎉 *По твоей ссылке зарегистрировался новый пользователь!*\n\n"
                            f"+3 бесплатных анализа начислено 🎁",
                            parse_mode=ParseMode.MARKDOWN
                        )
                    except Exception:
                        pass
        except (ValueError, TypeError):
            pass

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
        f"🤖 Я — *BetMind Bot*\n"
        f"Профессиональный анализ матчей с прогнозами.\n\n"
        f"⚙️ *Бот работает в тестовом режиме*\n"
        f"Мы постоянно его улучшаем, поэтому возможны небольшие сбои — спасибо за понимание 🙏\n\n"
        f"📌 *Примеры запросов:*\n"
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
        [InlineKeyboardButton("🔗 Пригласить друга (+3 анализа)", callback_data="ref_link")],
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
        "🔵 *Basic — 5 анализов в день*\n"
        "├ 399₽/месяц\n"
        "└ 2 990₽/год _(экономия 37%)_ ⭐\n\n"
        "🔥 *Pro — 15 анализов в день*\n"
        "├ 799₽/месяц\n"
        "└ 5 990₽/год _(экономия 37%)_ 🏆\n"
        "└ + web-поиск свежих новостей\n\n"
        "🆓 *Free* — 2 анализа (всего, без подписки)\n\n"
        "_Оплата через ЮКасса — карта, СБП, МИР_"
    )
    keyboard = [
        [InlineKeyboardButton("🔵 Basic — 399₽/мес",  callback_data="pay_basic_month")],
        [InlineKeyboardButton("🔵 Basic — 2990₽/год ⭐", callback_data="pay_basic_year")],
        [InlineKeyboardButton("🔥 Pro — 799₽/мес",    callback_data="pay_pro_month")],
        [InlineKeyboardButton("🔥 Pro — 5990₽/год 🏆", callback_data="pay_pro_year")],
    ]
    await message.reply_text(
        text, parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def cmd_activate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        target_uid = int(context.args[0])
        plan_key = context.args[1]
        if plan_key not in PLANS:
            await update.message.reply_text("Неверный план. Используй: basic_month, basic_year, pro_month, pro_year")
            return
        set_plan(target_uid, plan_key)
        plan = PLANS[plan_key]
        until = (datetime.now() + timedelta(days=plan["days"])).strftime("%d.%m.%Y")
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

async def cmd_ref(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Реферальная ссылка пользователя"""
    user = update.effective_user
    ensure_user(user.id, user.username or "", user.full_name or "")
    ref_count = get_ref_count(user.id)
    bot_username = (await context.bot.get_me()).username
    ref_link = f"https://t.me/{bot_username}?start={user.id}"
    text = (
        f"🔗 *Твоя реферальная ссылка:*\n\n"
        f"`{ref_link}`\n\n"
        f"За каждого приглашённого друга — *+3 бесплатных анализа* 🎁\n\n"
        f"👥 Приглашено друзей: *{ref_count}*\n"
        f"🎁 Бонусных анализов получено: *{ref_count * 3}*\n\n"
        f"_Поделись ссылкой в соцсетях или отправь другу!_"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    users = get_all_users()
    now = datetime.now()
    total = len(users)
    active = sum(1 for u in users if u[3] and u[4] and
                 datetime.fromisoformat(u[4]) > now)
    await update.message.reply_text(
        f"📊 *Статистика бота:*\n\n"
        f"👥 Всего пользователей: *{total}*\n"
        f"✅ Активных подписок: *{active}*\n"
        f"🆓 Без подписки: *{total - active}*",
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_revenue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    users = get_all_users()
    now = datetime.now()
    counts = {k: 0 for k in PLANS}
    for u in users:
        plan, until = u[3], u[4]
        if plan and until and datetime.fromisoformat(until) > now:
            if plan in counts:
                counts[plan] += 1
    revenue = sum(counts[p] * PLANS[p]["price"] for p in counts)
    lines = []
    for pk, pdata in PLANS.items():
        period = PLAN_PERIOD[pk]
        lines.append(f"{pdata['name']} ({period}): *{counts[pk]}* → *{counts[pk]*pdata['price']}₽*")
    await update.message.reply_text(
        f"💰 *Финансовая статистика:*\n\n"
        + "\n".join(lines) +
        f"\n\n💵 *Итого: {revenue}₽*",
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("Использование: /broadcast Текст сообщения")
        return
    text = " ".join(context.args)
    users = get_all_users()
    success = 0
    failed = 0
    status_msg = await update.message.reply_text(f"📤 Отправляю {len(users)} пользователям...")
    for user in users:
        try:
            await context.bot.send_message(
                user[0],
                f"📢 *Сообщение от BetMind Bot:*\n\n{text}",
                parse_mode=ParseMode.MARKDOWN
            )
            success += 1
        except Exception:
            failed += 1
    await status_msg.edit_text(
        f"✅ *Рассылка завершена*\n\n"
        f"📨 Отправлено: *{success}*\n"
        f"❌ Не доставлено: *{failed}*",
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    users = get_all_users()
    now = datetime.now()
    lines = ["ID,Username,Имя,Тариф,Активен до,Статус"]
    for u in users:
        uid, username, name, plan, until = u
        if plan and until and datetime.fromisoformat(until) > now:
            status = "активен"
            plan_name = PLANS[plan]["name"] if plan in PLANS else plan
            until_str = datetime.fromisoformat(until).strftime("%d.%m.%Y")
        else:
            status = "нет подписки"
            plan_name = "-"
            until_str = "-"
        lines.append(f"{uid},{username or '-'},{name or '-'},{plan_name},{until_str},{status}")
    filename = f"/tmp/betmind_users_{now.strftime('%d%m%Y')}.csv"
    with open(filename, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    await context.bot.send_document(
        update.effective_user.id,
        document=open(filename, "rb"),
        filename=f"betmind_users_{now.strftime('%d%m%Y')}.csv",
        caption=f"📊 Пользователи BetMind Bot — {len(users)} чел."
    )

# ══════════════════════════════════════════════════════════════════════════════
#  CALLBACK HANDLER
# ══════════════════════════════════════════════════════════════════════════════
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
            info = (f"✅ Тариф: *{PLANS[plan]['name']}*\n"
                    f"📅 До: *{until_str}*\n"
                    f"📊 Сегодня: *{day_used}/{day_limit}*")
        else:
            info = f"❌ Подписки нет\n🎁 Бесплатных: *{free_left}*"
        await query.message.reply_text(
            f"📊 *Твой аккаунт:*\n\n{info}", parse_mode=ParseMode.MARKDOWN
        )

    elif query.data == "ref_link":
        bot_username = (await context.bot.get_me()).username
        ref_link = f"https://t.me/{bot_username}?start={uid}"
        ref_count = get_ref_count(uid)
        await query.message.reply_text(
            f"🔗 *Твоя реферальная ссылка:*\n\n"
            f"`{ref_link}`\n\n"
            f"За каждого приглашённого друга — *+3 бесплатных анализа* 🎁\n\n"
            f"👥 Приглашено: *{ref_count}* друзей",
            parse_mode=ParseMode.MARKDOWN
        )

    elif query.data == "sub_bonus":
        try:
            member = await context.bot.get_chat_member(CHANNEL_USERNAME, uid)
            if member.status in ("member", "administrator", "creator"):
                conn = sqlite3.connect("bot.db")
                c = conn.cursor()
                c.execute("SELECT free_used FROM users WHERE user_id=?", (uid,))
                row = c.fetchone()
                if row and row[0] > 1:
                    c.execute("UPDATE users SET free_used=free_used-2 WHERE user_id=?", (uid,))
                    conn.commit()
                conn.close()
                await query.message.reply_text(
                    "✅ *+2 анализа добавлено!* Спасибо за подписку 🎉",
                    parse_mode=ParseMode.MARKDOWN
                )
            else:
                keyboard = [[InlineKeyboardButton(
                    "📢 Подписаться", url=f"https://t.me/{CHANNEL_USERNAME.strip('@')}"
                )]]
                await query.message.reply_text(
                    f"Подпишись на *{CHANNEL_USERNAME}* и получи *+2 анализа* бесплатно!",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
        except Exception:
            await query.message.reply_text("Канал ещё не настроен. Скоро!")

    elif query.data in ("pay_basic_month", "pay_basic_year", "pay_pro_month", "pay_pro_year"):
        plan_key = query.data.replace("pay_", "")
        plan = PLANS[plan_key]
        period = PLAN_PERIOD[plan_key]
        uid = query.from_user.id
        payment_url = await create_yokassa_payment(uid, plan_key)
        if payment_url:
            keyboard = [[InlineKeyboardButton("💳 Перейти к оплате", url=payment_url)]]
            await query.message.reply_text(
                f"💳 *Оплата: {plan['name']} ({period}) — {plan['price']}₽*\n\n"
                f"Нажми кнопку — страница оплаты.\n"
                f"Принимаем: карта, СБП, МИР 💳\n\n"
                f"_После оплаты подписка активируется автоматически_ ✅",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await query.message.reply_text(
                "🔧 Ошибка. Напиши нам: @betmind_support"
            )
    elif query.data.startswith("paid_"):
        plan_key = query.data.replace("paid_", "")
        plan = PLANS[plan_key]
        period = PLAN_PERIOD.get(plan_key, "")
        user = query.from_user
        if ADMIN_ID:
            await context.bot.send_message(
                ADMIN_ID,
                f"💰 *Новая оплата!*\n\n"
                f"👤 {user.full_name} (@{user.username})\n"
                f"🆔 `{user.id}`\n"
                f"📦 {plan['name']} ({period}) — {plan['price']}₽\n\n"
                f"Активировать: /activate {user.id} {plan_key}",
                parse_mode=ParseMode.MARKDOWN
            )
        await query.message.reply_text(
            "⏳ Заявка отправлена! Подписка активируется в течение 15 минут.",
        )

# ══════════════════════════════════════════════════════════════════════════════
#  ГЛАВНЫЙ ОБРАБОТЧИК СООБЩЕНИЙ
# ══════════════════════════════════════════════════════════════════════════════
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = user.id
    text = update.message.text.strip()

    ensure_user(uid, user.username or "", user.full_name or "")

    if len(text) < 3:
        await update.message.reply_text("Напиши матч, например: `Реал — Барса`",
                                        parse_mode=ParseMode.MARKDOWN)
        return

    plan, until = get_plan(uid)

    if plan:
        can, day_used = check_day_limit(uid, plan)
        day_limit = PLANS[plan]["req_per_day"]
        if not can:
            await update.message.reply_text(
                f"⛔ *Дневной лимит исчерпан*\n\n"
                f"Использовано: *{day_used}/{day_limit}* сегодня.\n"
                f"Обновится завтра в 00:00 🕛",
                parse_mode=ParseMode.MARKDOWN
            )
            return
    else:
        free_used = get_free_used(uid)
        if free_used >= FREE_LIMIT:
            keyboard = [
                [InlineKeyboardButton("💎 Выбрать тариф", callback_data="show_plans")],
                [InlineKeyboardButton("🎁 +2 запроса бесплатно", callback_data="sub_bonus")],
            ]
            await update.message.reply_text(
                f"⛔ *Бесплатный лимит исчерпан*\n\n"
                f"Оформи подписку или получи +2 анализа за подписку на канал:",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return

    # Начинаем анализ
    await update.message.chat.send_action(ChatAction.TYPING)
    has_web = bool(plan and PLANS.get(plan, {}).get("web_search", False))
    wait_msg = await update.message.reply_text(
        "🔍 Собираю данные...\n"
        "📊 Статистика команд\n"
        "💰 Коэффициенты букмекеров\n"
        + ("🌐 Web-поиск свежих новостей\n" if has_web else "📰 Новости из RSS\n") +
        "\n_Подожди 25–40 секунд_ ⏳",
        parse_mode=ParseMode.MARKDOWN
    )

    sport = detect_sport(text)
    sport_emoji = {"football": "⚽", "cs2": "🎮", "dota": "🐉"}[sport]

    # Парсим команды
    # Сначала нормализуем разделители
    normalized = (text
        .replace(" vs ", " — ")
        .replace(" VS ", " — ")
        .replace(" v ", " — ")
        .replace(" против ", " — ")
        .replace(" - ", " — ")
    )

    # Убираем лишние слова-хвосты (турниры, игра, формат)
    noise_words = [
        "dota2", "dota 2", "cs2", "cs 2", "blast slam", "blast",
        "esl one", "esl", "dreamleague", "ti ", "the international",
        "riyadh masters", "dreamhack", "hltv", "bo3", "bo1", "bo5",
        "групповой этап", "плей-офф", "playoff", "grand final"
    ]
    cleaned = normalized.lower()
    for nw in noise_words:
        cleaned = cleaned.replace(nw, "")
    # Восстанавливаем оригинальный регистр через позиции из normalized
    # (проще — работаем с cleaned напрямую, т.к. названия команд обычно lowercase при поиске)

    parts = cleaned.split(" — ")
    parts = [p.strip() for p in parts if p.strip()]

    team1 = parts[0] if len(parts) >= 2 else (parts[0] if parts else text)
    team2 = parts[1] if len(parts) >= 2 else ""

    # Параллельно собираем все данные
    async def empty(): return ""

    # Выбираем источник статистики в зависимости от вида спорта
    if sport == "cs2" and team2:
        stats_coro = get_cs2_stats(team1, team2)
    elif sport == "dota" and team2:
        stats_coro = get_dota_stats(team1, team2)
    elif team2:
        stats_coro = get_sofascore_stats(team1, team2)
    else:
        stats_coro = empty()

    # Коэффициенты для футбола, CS2 и Dota
    odds_coro = get_odds(team1, team2, sport) if team2 else empty()

    # Новости только для футбола — для CS2/Dota Google RSS по-русски бесполезен
    news_coro = get_news(team1, team2) if sport == "football" else empty()

    stats, odds, news = await asyncio.gather(
        stats_coro,
        odds_coro,
        news_coro
    )

    try:
        use_web_search = bool(plan and PLANS.get(plan, {}).get("web_search", False))
        analysis = await analyze_match(text, sport, stats, odds, news, use_web_search=use_web_search)

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
                f"_🎁 Осталось бесплатных: {free_left}_" if free_left > 0
                else "_🔴 Последний бесплатный анализ. Оформи подписку!_"
            )

        keyboard = [[
            InlineKeyboardButton(
                "💰 Сделать ставку" if sport == "football" else "🎯 Поставить на победителя",
                url=PARTNER_LINKS[sport]
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

        try:
            await wait_msg.delete()
        except Exception:
            pass  # уже удалено — не страшно

        try:
            await update.message.reply_text(
                analysis + footer,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except Exception as md_err:
            logger.warning(f"Markdown parse error, retrying as plain text: {md_err}")
            # Убираем markdown-символы и шлём plain text
            plain = (analysis + footer).replace("*", "").replace("_", "").replace("`", "")
            await update.message.reply_text(
                plain,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

    except Exception as e:
        logger.error(f"Analysis error: {e}")
        try:
            await wait_msg.edit_text(
                "❌ Ошибка при анализе. Попробуй ещё раз или напиши матч по-другому."
            )
        except Exception:
            await update.message.reply_text(
                "❌ Ошибка при анализе. Попробуй ещё раз или напиши матч по-другому."
            )

# ══════════════════════════════════════════════════════════════════════════════
#  ЗАПУСК
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
#  РЕФЕРАЛЬНАЯ СИСТЕМА
# ══════════════════════════════════════════════════════════════════════════════
def get_ref_count(uid: int) -> int:
    """Сколько человек пришло по реферальной ссылке"""
    conn = sqlite3.connect("bot.db")
    c = conn.cursor()
    c.execute("SELECT ref_count FROM users WHERE user_id=?", (uid,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0

def add_referral(uid: int, referred_by: int):
    """Записываем кто привёл пользователя"""
    conn = sqlite3.connect("bot.db")
    c = conn.cursor()
    # Проверяем что реферер ещё не записан
    c.execute("SELECT referred_by FROM users WHERE user_id=?", (uid,))
    row = c.fetchone()
    if row and row[0] is None:
        c.execute("UPDATE users SET referred_by=? WHERE user_id=?", (referred_by, uid))
        # Начисляем +3 анализа рефереру
        c.execute("UPDATE users SET free_used=MAX(0, free_used-3), ref_count=ref_count+1 WHERE user_id=?", (referred_by,))
        conn.commit()
        conn.close()
        return True
    conn.close()
    return False

# ══════════════════════════════════════════════════════════════════════════════
#  ВЕБХУК ЮКАССЫ — обработчик входящих уведомлений
# ══════════════════════════════════════════════════════════════════════════════
async def yokassa_webhook_handler(request: web.Request) -> web.Response:
    """Принимаем уведомления от ЮКассы об успешной оплате"""
    try:
        data = await request.json()
        logger.info(f"YooKassa webhook: {data}")

        event = data.get("event", "")
        if event != "payment.succeeded":
            return web.Response(status=200)

        payment_obj = data.get("object", {})
        status = payment_obj.get("status", "")
        if status != "succeeded":
            return web.Response(status=200)

        metadata = payment_obj.get("metadata", {})
        uid = int(metadata.get("user_id", 0))
        plan_key = metadata.get("plan_key", "")
        amount = payment_obj.get("amount", {}).get("value", "0")

        if not uid or plan_key not in PLANS:
            logger.error(f"Invalid metadata: uid={uid}, plan={plan_key}")
            return web.Response(status=200)

        # Активируем подписку
        set_plan(uid, plan_key)
        plan = PLANS[plan_key]
        until = (datetime.now() + timedelta(days=plan["days"])).strftime("%d.%m.%Y")

        # Уведомляем пользователя
        try:
            await tg_app.bot.send_message(
                uid,
                f"✅ *Оплата прошла успешно!*\n\n"
                f"📦 Тариф: *{plan['name']}*\n"
                f"📅 Активен до: *{until}*\n"
                f"📊 Лимит: *{plan['req_per_day']} анализов в день*\n\n"
                f"Пиши любой матч — анализирую! ⚽🎮🐉",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.error(f"Failed to notify user {uid}: {e}")

        # Уведомляем админа
        if ADMIN_ID:
            try:
                await tg_app.bot.send_message(
                    ADMIN_ID,
                    f"💰 *Новая оплата!*\n\n"
                    f"🆔 User ID: `{uid}`\n"
                    f"📦 Тариф: {plan['name']}\n"
                    f"💵 Сумма: {amount}₽\n"
                    f"✅ Подписка активирована автоматически",
                    parse_mode=ParseMode.MARKDOWN
                )
            except Exception as e:
                logger.error(f"Failed to notify admin: {e}")

        return web.Response(status=200)

    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return web.Response(status=200)

async def health_check(request: web.Request) -> web.Response:
    """Health check endpoint"""
    return web.Response(text="BetMind Bot is running!", status=200)

# ══════════════════════════════════════════════════════════════════════════════
#  ЗАПУСК — бот + веб-сервер одновременно
# ══════════════════════════════════════════════════════════════════════════════
tg_app = None  # глобальная ссылка на telegram app для вебхука

async def run_bot():
    global tg_app
    init_db()
    tg_app = Application.builder().token(TELEGRAM_TOKEN).build()

    tg_app.add_handler(CommandHandler("start",      cmd_start))
    tg_app.add_handler(CommandHandler("subscribe",  cmd_subscribe))
    tg_app.add_handler(CommandHandler("ref",        cmd_ref))
    tg_app.add_handler(CommandHandler("activate",   cmd_activate))
    tg_app.add_handler(CommandHandler("users",      cmd_users))
    tg_app.add_handler(CommandHandler("revenue",    cmd_revenue))
    tg_app.add_handler(CommandHandler("broadcast",  cmd_broadcast))
    tg_app.add_handler(CommandHandler("export",     cmd_export))
    tg_app.add_handler(CallbackQueryHandler(handle_callback))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    await tg_app.initialize()
    await tg_app.start()
    await tg_app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    logger.info("BetMind Bot запущен (polling)")

async def run_web():
    """Запускаем aiohttp веб-сервер для вебхуков"""
    web_app = web.Application()
    web_app.router.add_post("/yokassa/webhook", yokassa_webhook_handler)
    web_app.router.add_get("/", health_check)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()
    logger.info("Веб-сервер запущен на порту 8080")

async def main_async():
    await run_web()
    await run_bot()
    # Держим бота запущенным
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Останавливаем бота...")
        await tg_app.updater.stop()
        await tg_app.stop()
        await tg_app.shutdown()

def main():
    asyncio.run(main_async())

if __name__ == "__main__":
    main()
