import os
import asyncio
import logging
import json
import httpx
from datetime import datetime, timezone, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram import Bot
from telegram.constants import ParseMode
import anthropic

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
PANDASCORE_KEY   = os.environ["PANDASCORE_KEY"]
STRATZ_TOKEN     = os.environ["STRATZ_TOKEN"]

CHANNEL_ID       = os.environ["CHANNEL_ID"]          # например "-1001234567890"
BOT_LINK         = os.environ.get("BOT_LINK", "https://t.me/BetMindBot")

# Московское время = UTC+3
MSK = timezone(timedelta(hours=3))

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
bot = Bot(token=TELEGRAM_TOKEN)

# ══════════════════════════════════════════════════════════════════════════════
#  ХРАНИЛИЩЕ ДНЕВНОГО СОСТОЯНИЯ
#  Храним в памяти: утренние матчи + утренние прогнозы для вечернего разбора
# ══════════════════════════════════════════════════════════════════════════════
daily_state = {
    "date": None,
    "morning_matches": [],
    "morning_analysis": [],
}

_scheduler_ref = None  # глобальная ссылка для динамического перепланирования

def reset_daily_state():
    today = datetime.now(MSK).strftime("%d.%m.%Y")
    daily_state["date"] = today
    daily_state["morning_matches"] = []
    daily_state["morning_analysis"] = []
    logger.info(f"Daily state reset for {today}")

# ══════════════════════════════════════════════════════════════════════════════
#  ПОЛУЧЕНИЕ ТОПОВЫХ МАТЧЕЙ ДНЯ
# ══════════════════════════════════════════════════════════════════════════════

# Топовые турниры — только они попадают в канал
TOP_FOOTBALL_LEAGUES = [
    "UEFA Champions League", "UEFA Europa League", "UEFA Conference League",
    "Premier League", "La Liga", "Serie A", "Bundesliga", "Ligue 1",
    "FIFA World Cup", "World Cup", "WC 2026", "FIFA WC",
    "UEFA European Championship", "Copa America",
    "FA Cup", "Copa del Rey",
]

TOP_CS2_TOURNAMENTS = [
    "Major", "ESL Pro League", "BLAST Premier", "BLAST.tv", "IEM",
    "FACEIT", "ESL One", "DreamHack Masters",
]

TOP_DOTA_TOURNAMENTS = [
    "The International", "Riyadh Masters", "ESL One", "DreamLeague",
    "BLAST Slam", "PGL Wallachia", "BetBoom Dacha",
]

# Топ-20 сборных FIFA (рейтинг 2026)
FIFA_TOP20 = {
    "argentina", "france", "england", "belgium", "brazil",
    "portugal", "netherlands", "spain", "croatia", "italy",
    "morocco", "usa", "mexico", "germany", "colombia",
    "uruguay", "senegal", "denmark", "austria", "japan"
}

def _is_top_football(tournament: str) -> bool:
    t = tournament.lower()
    return any(top.lower() in t for top in TOP_FOOTBALL_LEAGUES)

def _has_top20_team(team1: str, team2: str) -> bool:
    t1 = team1.lower()
    t2 = team2.lower()
    return any(nat in t1 or nat in t2 for nat in FIFA_TOP20)

def _is_top_cs2(tournament: str) -> bool:
    t = tournament.lower()
    return any(top.lower() in t for top in TOP_CS2_TOURNAMENTS)

def _is_top_dota(tournament: str) -> bool:
    t = tournament.lower()
    return any(top.lower() in t for top in TOP_DOTA_TOURNAMENTS)


async def fetch_football_matches() -> list[dict]:
    """Топовые футбольные матчи на сегодня через Sofascore (RapidAPI)"""
    try:
        today = datetime.now(MSK).strftime("%Y-%m-%d")
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://sofascore.p.rapidapi.com/matches/get-list-by-date",
                params={"date": today, "sport": "football"},
                headers={
                    "x-rapidapi-host": "sofascore.p.rapidapi.com",
                    "x-rapidapi-key": RAPIDAPI_KEY.strip()
                }
            )
            if r.status_code != 200:
                logger.warning(f"Sofascore HTTP {r.status_code}: {r.text[:200]}")
                return []
            data = r.json()
            events = data.get("events", [])
            logger.info(f"Sofascore returned {len(events)} events")
            matches = []
            for e in events:
                tournament = e.get("tournament", {}).get("name", "")
                home = e.get("homeTeam", {}).get("name", "?")
                away = e.get("awayTeam", {}).get("name", "?")
                # Пропускаем если не топ турнир И нет команды из топ-20 FIFA
                if not _is_top_football(tournament) and not _has_top20_team(home, away):
                    continue
                start_ts = e.get("startTimestamp")
                dt = datetime.fromtimestamp(start_ts, tz=MSK).strftime("%H:%M") if start_ts else "?"
                matches.append({
                    "sport": "football",
                    "team1": home,
                    "team2": away,
                    "tournament": tournament,
                    "time": dt,
                    "id": e.get("id"),
                })
            return matches[:4]
    except Exception as e:
        logger.warning(f"Football fetch error: {e}")
        return []


async def fetch_cs2_matches() -> list[dict]:
    """Топовые CS2 матчи на сегодня через PandaScore"""
    try:
        today = datetime.now(MSK).date().isoformat()
        tomorrow = (datetime.now(MSK).date() + timedelta(days=1)).isoformat()
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://api.pandascore.co/csgo/matches/upcoming",
                params={
                    "range[begin_at]": f"{today}T00:00:00Z,{tomorrow}T23:59:59Z",
                    "per_page": 20,
                    "sort": "begin_at"
                },
                headers={"Authorization": f"Bearer {PANDASCORE_KEY.strip()}"}
            )
            data = r.json()
            matches = []
            if isinstance(data, list):
                logger.info(f"PandaScore returned {len(data)} matches")
                for m in data:
                    league = (m.get("league") or {}).get("name", "")
                    serie = (m.get("serie") or {}).get("full_name", "")
                    tournament_name = f"{league} {serie}".strip()
                    if not _is_top_cs2(tournament_name):
                        logger.debug(f"CS2 skip (not top): {tournament_name}")
                        continue
                    opponents = m.get("opponents", [])
                    if len(opponents) < 2:
                        continue
                    t1 = opponents[0].get("opponent", {}).get("name", "?")
                    t2 = opponents[1].get("opponent", {}).get("name", "?")
                    begin_at = m.get("begin_at", "")
                    if begin_at:
                        dt = datetime.fromisoformat(begin_at.replace("Z", "+00:00"))
                        time_str = dt.astimezone(MSK).strftime("%H:%M")
                    else:
                        time_str = "?"
                    matches.append({
                        "sport": "cs2",
                        "team1": t1,
                        "team2": t2,
                        "tournament": tournament_name,
                        "time": time_str,
                        "id": m.get("id"),
                    })
            return matches[:3]
    except Exception as e:
        logger.warning(f"CS2 fetch error: {e}")
        return []


async def fetch_dota_matches() -> list[dict]:
    """Топовые Dota 2 матчи на сегодня через Stratz GraphQL"""
    try:
        now_ts = int(datetime.now(timezone.utc).timestamp())
        end_ts = now_ts + 86400  # +24 часа

        query = """
        {
          leagues(request: {
            tier: [PROFESSIONAL, PREMIUM],
            take: 10
          }) {
            id
            displayName
            matches(request: {
              take: 20
            }) {
              id
              startDateTime
              radiantTeam { name }
              direTeam { name }
            }
          }
        }
        """
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                "https://api.stratz.com/graphql",
                json={"query": query},
                headers={
                    "Authorization": f"Bearer {STRATZ_TOKEN.strip()}",
                    "Content-Type": "application/json",
                    "User-Agent": "STRATZ_API"
                }
            )
            data = r.json()

        leagues = data.get("data", {}).get("leagues", []) or []
        matches = []
        for league in leagues:
            league_name = league.get("displayName", "")
            if not _is_top_dota(league_name):
                continue
            for m in (league.get("matches") or []):
                start = m.get("startDateTime")
                if not start:
                    continue
                if not (now_ts - 3600 <= start <= end_ts):  # -1ч буфер
                    continue
                rad = (m.get("radiantTeam") or {}).get("name", "TBD")
                dire = (m.get("direTeam") or {}).get("name", "TBD")
                if rad == "TBD" and dire == "TBD":
                    continue
                time_str = datetime.fromtimestamp(start, tz=MSK).strftime("%H:%M")
                matches.append({
                    "sport": "dota",
                    "team1": rad,
                    "team2": dire,
                    "tournament": league_name,
                    "time": time_str,
                    "id": m.get("id"),
                })
        return matches[:3]
    except Exception as e:
        logger.warning(f"Dota fetch error: {e}")
        return []


async def fetch_todays_matches() -> dict:
    """Собираем все топовые матчи дня по трём видам спорта"""
    football, cs2, dota = await asyncio.gather(
        fetch_football_matches(),
        fetch_cs2_matches(),
        fetch_dota_matches(),
    )
    return {"football": football, "cs2": cs2, "dota": dota}


# ══════════════════════════════════════════════════════════════════════════════
#  УТРЕННИЙ ПОСТ — предстоящие матчи
# ══════════════════════════════════════════════════════════════════════════════
async def morning_post():
    reset_daily_state()
    logger.info("Generating morning post...")

    matches = await fetch_todays_matches()
    football = matches["football"]
    cs2      = matches["cs2"]
    dota     = matches["dota"]

    total = len(football) + len(cs2) + len(dota)
    if total == 0:
        logger.info(f"No top matches today (football={len(football)}, cs2={len(cs2)}, dota={len(dota)}) — skipping morning post")
        return

    # Сохраняем для вечернего разбора
    daily_state["morning_matches"] = football + cs2 + dota

    # ── Динамически планируем вечерний пост ──────────────────────────────────
    # Берём время последнего матча + 2.5 часа (средний BO3/90мин футбол + буфер)
    # Минимум 22:00, максимум 02:00 следующего дня
    all_times = []
    for m in (football + cs2 + dota):
        try:
            h, mn = map(int, m["time"].split(":"))
            all_times.append(h * 60 + mn)
        except Exception:
            pass

    if all_times:
        last_match_minutes = max(all_times)
        evening_minutes = last_match_minutes + 150  # +2.5 часа
        # Минимум 22:00 МСК
        evening_minutes = max(evening_minutes, 22 * 60)
        evening_hour = (evening_minutes // 60) % 24
        evening_min  = evening_minutes % 60
        # Если перевалило за полночь — следующий день
        next_day = evening_minutes >= 24 * 60
    else:
        evening_hour, evening_min, next_day = 23, 0, False

    now_msk = datetime.now(MSK)
    evening_date = now_msk.date() + timedelta(days=1 if next_day else 0)
    evening_dt = datetime.combine(evening_date, __import__('datetime').time(evening_hour, evening_min), tzinfo=MSK)

    # Перепланируем вечерний пост
    from apscheduler.triggers.date import DateTrigger
    if _scheduler_ref is not None:
        try:
            _scheduler_ref.reschedule_job("evening", trigger=DateTrigger(run_date=evening_dt))
            logger.info(f"Evening post rescheduled to {evening_dt.strftime('%d.%m %H:%M')} MSK (last match {max(all_times)//60:02d}:{max(all_times)%60:02d})")
        except Exception as e:
            logger.warning(f"Could not reschedule evening post: {e}")
    # ─────────────────────────────────────────────────────────────────────────

    # Формируем данные для Claude
    matches_text = ""
    if football:
        matches_text += "ФУТБОЛ:\n"
        for m in football:
            matches_text += f"  {m['time']} МСК | {m['team1']} vs {m['team2']} | {m['tournament']}\n"
    if cs2:
        matches_text += "CS2:\n"
        for m in cs2:
            matches_text += f"  {m['time']} МСК | {m['team1']} vs {m['team2']} | {m['tournament']}\n"
    if dota:
        matches_text += "DOTA 2:\n"
        for m in dota:
            matches_text += f"  {m['time']} МСК | {m['team1']} vs {m['team2']} | {m['tournament']}\n"

    today_str = datetime.now(MSK).strftime("%d.%m.%Y")

    prompt = f"""Ты — автор Telegram-канала о спортивной аналитике и ставках BetMind.
Сегодня {today_str}. Напиши короткий утренний пост — расписание топовых матчей дня.

Матчи дня:
{matches_text}

Структура поста:
1. Одна строка-заголовок с датой (без длинных вступлений)
2. Блоки по видам спорта (только те, по которым есть матчи):
   ⚽ ФУТБОЛ
   🎮 CS2
   🐉 DOTA 2
   Каждый матч — ОДНА строка: время МСК, команды, турнир. Без комментариев и анализа.
3. Одна короткая строка в конце: призыв в бот {BOT_LINK}

Стиль: лаконичный, информативный, с эмодзи. Telegram Markdown (*жирный*).
ВАЖНО: не более 4000 символов, не более 15 строк итого. Язык: русский."""

    response = anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}]
    )
    post_text = response.content[0].text

    try:
        msg = await bot.send_message(
            chat_id=CHANNEL_ID,
            text=post_text,
            parse_mode=ParseMode.MARKDOWN
        )
        logger.info(f"Morning post sent: message_id={msg.message_id}")
    except Exception as e:
        logger.error(f"Failed to send morning post: {e}")
        # Пробуем без Markdown
        try:
            plain = post_text.replace("*", "").replace("_", "").replace("`", "")
            await bot.send_message(chat_id=CHANNEL_ID, text=plain)
        except Exception as e2:
            logger.error(f"Plain send also failed: {e2}")


# ══════════════════════════════════════════════════════════════════════════════
#  ДНЕВНОЙ ПОСТ — детальный анализ одного матча
# ══════════════════════════════════════════════════════════════════════════════
async def afternoon_post():
    logger.info("Generating afternoon post...")

    matches = daily_state["morning_matches"]
    if not matches:
        # Если утренний пост не был запущен (перезапуск), пробуем подтянуть матчи
        data = await fetch_todays_matches()
        matches = data["football"] + data["cs2"] + data["dota"]

    if not matches:
        logger.info("No matches for afternoon post — skipping")
        return

    # Выбираем самый интересный матч (приоритет: football > cs2 > dota, первый в списке)
    match = matches[0]
    sport_emoji = {"football": "⚽", "cs2": "🎮", "dota": "🐉"}[match["sport"]]

    prompt = f"""Ты — автор Telegram-канала о спортивной аналитике BetMind.
Напиши дневной пост-разбор команд для матча сегодня:

{match['team1']} vs {match['team2']}
Турнир: {match['tournament']}
Время: {match['time']} МСК
Вид спорта: {match['sport']}

Структура поста:
1. Заголовок с {sport_emoji} и названием матча + "Разбор дня"
2. Блок по каждой команде — форма, сильные стороны, кратко (2-3 строки каждый)
3. Ключевые факторы матча — 2-3 пункта
4. Вопрос аудитории: кто победит / кого ставишь — с вариантами ответа
5. Одна строка: для детального анализа — {BOT_LINK}

ВАЖНО: не писать конкретные ставки и коэффициенты в посте. Только разбор команд.
Стиль: профессионально, аналитически, живо. Telegram Markdown. Язык: русский.
Длина: не более 20 строк, не более 3000 символов."""

    kwargs = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 1200,
        "messages": [{"role": "user", "content": prompt}],
        "tools": [{
            "type": "web_search_20250305",
            "name": "web_search",
        }]
    }

    response = anthropic_client.messages.create(**kwargs)
    post_text = "".join(
        b.text for b in response.content
        if getattr(b, "type", None) == "text" and b.text
    )

    # Сохраняем прогноз для вечера
    daily_state["morning_analysis"].append({
        "match": f"{match['team1']} vs {match['team2']}",
        "tournament": match["tournament"],
        "sport": match["sport"],
        "analysis_text": post_text,
    })

    try:
        msg = await bot.send_message(
            chat_id=CHANNEL_ID,
            text=post_text,
            parse_mode=ParseMode.MARKDOWN
        )
        logger.info(f"Afternoon post sent: message_id={msg.message_id}")
    except Exception as e:
        logger.error(f"Failed to send afternoon post: {e}")
        try:
            plain = post_text.replace("*", "").replace("_", "").replace("`", "")
            await bot.send_message(chat_id=CHANNEL_ID, text=plain)
        except Exception as e2:
            logger.error(f"Plain send also failed: {e2}")


# ══════════════════════════════════════════════════════════════════════════════
#  ВЕЧЕРНИЙ ПОСТ — итоги дня + разбор прогнозов
# ══════════════════════════════════════════════════════════════════════════════

RESULTS_NOT_FOUND_MARKER = "##RESULTS_NOT_FOUND##"

async def evening_post():
    logger.info("Generating evening post...")

    matches = daily_state["morning_matches"]
    analyses = daily_state["morning_analysis"]

    if not matches:
        logger.info("No matches for evening post — skipping")
        return

    matches_list = "\n".join(
        f"- {m['team1']} vs {m['team2']} ({m['tournament']}, {m['sport']})"
        for m in matches
    )

    analyses_text = ""
    for a in analyses:
        analyses_text += f"\nМатч: {a['match']}\nНаш прогноз был:\n{a['analysis_text'][:500]}...\n"

    prompt = f"""Ты — автор Telegram-канала о спортивной аналитике BetMind.
Сегодня прошли эти матчи:
{matches_list}

Наши прогнозы:
{analyses_text if analyses_text else "Прогнозов не было — дай общий итог дня."}

ВАЖНО: Сначала найди через web search финальные счёта всех матчей.
Если хотя бы один матч ещё не завершён или результат не найден — ответь ТОЛЬКО одной строкой: {RESULTS_NOT_FOUND_MARKER}
Не пиши ничего другого в этом случае.

Если все результаты найдены — напиши вечерний итоговый пост:
1. Заголовок "Итоги дня 📊" с датой
2. Для каждого матча — итоговый счёт, 1 строка
3. Разбор прогноза: Зашло ✅ или не зашло ❌ + коротко почему
4. Общая статистика: X из Y прогнозов верных
5. Тизер на завтра
6. Призыв в бот: "{BOT_LINK}"

Стиль: честный. Telegram Markdown. Язык: русский. Длина: 20-30 строк."""

    kwargs = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 1200,
        "messages": [{"role": "user", "content": prompt}],
        "tools": [{
            "type": "web_search_20250305",
            "name": "web_search",
        }]
    }

    response = anthropic_client.messages.create(**kwargs)
    post_text = "".join(
        b.text for b in response.content
        if getattr(b, "type", None) == "text" and b.text
    ).strip()

    # Проверяем — матчи ещё идут?
    if RESULTS_NOT_FOUND_MARKER in post_text:
        retry_dt = datetime.now(MSK) + timedelta(hours=1)
        if _scheduler_ref is not None:
            from apscheduler.triggers.date import DateTrigger
            try:
                _scheduler_ref.reschedule_job("evening", trigger=DateTrigger(run_date=retry_dt))
                logger.info(f"Results not ready — evening post postponed to {retry_dt.strftime('%H:%M')} MSK")
            except Exception as e:
                logger.error(f"Failed to reschedule evening retry: {e}")
        return

    # Результаты есть — постим
    try:
        await bot.send_message(
            chat_id=CHANNEL_ID,
            text=post_text,
            parse_mode=ParseMode.MARKDOWN
        )
        logger.info("Evening post sent")
    except Exception as e:
        logger.error(f"Failed to send evening post: {e}")
        try:
            plain = post_text.replace("*", "").replace("_", "").replace("`", "")
            await bot.send_message(chat_id=CHANNEL_ID, text=plain)
        except Exception as e2:
            logger.error(f"Plain send also failed: {e2}")


# ══════════════════════════════════════════════════════════════════════════════
#  НОВОСТЬ ОТ ТЕБЯ — ты закидываешь текст, Claude раскрывает и постит
#  Использование: задай переменную окружения NEWS_QUEUE или
#  вызови endpoint /news через простой aiohttp сервер (описан ниже)
# ══════════════════════════════════════════════════════════════════════════════
async def post_custom_news(raw_news: str):
    """Принимает сырую новость/тезис от тебя, Claude раскрывает и постит"""
    if not raw_news or not raw_news.strip():
        return

    logger.info(f"Posting custom news: {raw_news[:80]}...")

    prompt = f"""Ты — автор Telegram-канала о спортивной аналитике BetMind.
Я даю тебе тему или новость, ты пишешь полноценный пост для канала.

Тема/новость: {raw_news}

Раскрой её в пост:
1. Цепляющий заголовок
2. Суть новости — факты, контекст, почему это важно для ставок
3. Как это влияет на предстоящие матчи (если применимо)
4. Твоё мнение аналитика — 2-3 предложения
5. Вопрос подписчикам или призыв в бот: "{BOT_LINK}"

Стиль: живой, экспертный. Telegram Markdown (*жирный*). Язык: русский.
Длина: 15-25 строк."""

    kwargs = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 900,
        "messages": [{"role": "user", "content": prompt}],
        "tools": [{
            "type": "web_search_20250305",
            "name": "web_search",
        }]
    }

    response = anthropic_client.messages.create(**kwargs)
    post_text = "".join(
        b.text for b in response.content
        if getattr(b, "type", None) == "text" and b.text
    )

    try:
        await bot.send_message(
            chat_id=CHANNEL_ID,
            text=post_text,
            parse_mode=ParseMode.MARKDOWN
        )
        logger.info("Custom news post sent")
    except Exception as e:
        logger.error(f"Failed to send custom news: {e}")
        try:
            plain = post_text.replace("*", "").replace("_", "").replace("`", "")
            await bot.send_message(chat_id=CHANNEL_ID, text=plain)
        except Exception as e2:
            logger.error(f"Plain send also failed: {e2}")


# ══════════════════════════════════════════════════════════════════════════════
#  МИНИ ВЕБЛСЕРВЕР — принимает новости от тебя через HTTP
#  POST /news  body: {"text": "Месси получил травму, пропустит El Clasico"}
#  POST /test  — тестовый запуск любого поста
# ══════════════════════════════════════════════════════════════════════════════
from aiohttp import web

async def handle_news(request: web.Request) -> web.Response:
    """Принимаем новость и постим в канал"""
    try:
        data = await request.json()
        text = data.get("text", "").strip()
        if not text:
            return web.json_response({"error": "empty text"}, status=400)
        asyncio.create_task(post_custom_news(text))
        return web.json_response({"status": "queued", "text": text[:80]})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

async def handle_test(request: web.Request) -> web.Response:
    """Ручной запуск поста для теста"""
    try:
        data = await request.json()
        post_type = data.get("type", "morning")  # morning | afternoon | evening
        if post_type == "morning":
            asyncio.create_task(morning_post())
        elif post_type == "afternoon":
            asyncio.create_task(afternoon_post())
        elif post_type == "evening":
            asyncio.create_task(evening_post())
        else:
            return web.json_response({"error": "type must be morning/afternoon/evening"}, status=400)
        return web.json_response({"status": f"{post_type} post triggered"})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

async def health(request: web.Request) -> web.Response:
    return web.Response(text="BetMind Scheduler OK")

async def run_web():
    app = web.Application()
    app.router.add_post("/news", handle_news)
    app.router.add_post("/test", handle_test)
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8081)
    await site.start()
    logger.info("Scheduler web server started on port 8081")


# ══════════════════════════════════════════════════════════════════════════════
#  ПЛАНИРОВЩИК — расписание по МСК
# ══════════════════════════════════════════════════════════════════════════════
async def main():
    global _scheduler_ref
    await run_web()

    scheduler = AsyncIOScheduler(timezone=MSK)
    _scheduler_ref = scheduler

    # Утро — 09:00 МСК (каждый день)
    scheduler.add_job(
        morning_post,
        CronTrigger(hour=9, minute=0, timezone=MSK),
        id="morning",
        replace_existing=True
    )

    # День — 14:00 МСК (каждый день)
    scheduler.add_job(
        afternoon_post,
        CronTrigger(hour=14, minute=0, timezone=MSK),
        id="afternoon",
        replace_existing=True
    )

    # Вечер — начальное время 23:00, утренний пост пересчитает его под реальный матч
    from apscheduler.triggers.date import DateTrigger
    now_msk = datetime.now(MSK)
    default_evening = now_msk.replace(hour=23, minute=0, second=0, microsecond=0)
    if default_evening <= now_msk:
        default_evening += timedelta(days=1)
    scheduler.add_job(
        evening_post,
        DateTrigger(run_date=default_evening),
        id="evening",
        replace_existing=True
    )

    # После вечернего поста — автоматически перепланируем на следующий день 23:00
    async def reschedule_evening_tomorrow():
        await evening_post()
        tomorrow_23 = datetime.now(MSK).replace(hour=23, minute=0, second=0, microsecond=0) + timedelta(days=1)
        scheduler.reschedule_job("evening", trigger=DateTrigger(run_date=tomorrow_23))
        logger.info(f"Evening post auto-rescheduled to tomorrow {tomorrow_23.strftime('%d.%m %H:%M')} MSK")

    scheduler.start()
    logger.info(f"Scheduler started. Jobs: morning 09:00, afternoon 14:00, evening dynamic (default {default_evening.strftime('%d.%m %H:%M')}) MSK")

    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        logger.info("Scheduler stopped")


if __name__ == "__main__":
    asyncio.run(main())
