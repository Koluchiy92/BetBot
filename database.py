import os
import asyncpg
from datetime import datetime, timedelta

# ══════════════════════════════════════════════════════════════════════════════
#  ПОДКЛЮЧЕНИЕ К POSTGRESQL (Railway)
# ══════════════════════════════════════════════════════════════════════════════
DATABASE_URL = os.environ["DATABASE_URL"]

_pool: asyncpg.Pool | None = None

async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    return _pool

async def close_pool():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None

# ══════════════════════════════════════════════════════════════════════════════
#  ИНИЦИАЛИЗАЦИЯ ТАБЛИЦЫ
# ══════════════════════════════════════════════════════════════════════════════
async def init_db():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id     BIGINT PRIMARY KEY,
                username    TEXT,
                full_name   TEXT,
                free_used   INTEGER DEFAULT 0,
                plan        TEXT DEFAULT NULL,
                plan_until  TIMESTAMP DEFAULT NULL,
                day_used    INTEGER DEFAULT 0,
                last_day    DATE DEFAULT NULL,
                joined_at   TIMESTAMP DEFAULT NOW(),
                referred_by BIGINT DEFAULT NULL,
                ref_count   INTEGER DEFAULT 0
            )
        """)

# ══════════════════════════════════════════════════════════════════════════════
#  ПОЛЬЗОВАТЕЛИ
# ══════════════════════════════════════════════════════════════════════════════
async def ensure_user(uid: int, username: str, full_name: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO users (user_id, username, full_name)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id) DO NOTHING
        """, uid, username, full_name)

async def get_free_used(uid: int) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT free_used FROM users WHERE user_id=$1", uid
        )
        return row["free_used"] if row else 0

async def increment_free(uid: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET free_used=free_used+1 WHERE user_id=$1", uid
        )

# ══════════════════════════════════════════════════════════════════════════════
#  ПОДПИСКИ
# ══════════════════════════════════════════════════════════════════════════════
async def get_plan(uid: int) -> tuple[str | None, str | None]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT plan, plan_until FROM users WHERE user_id=$1", uid
        )
        if not row or not row["plan"]:
            return None, None
        plan_until = row["plan_until"]
        if plan_until and plan_until < datetime.now():
            return None, None
        return row["plan"], plan_until.isoformat() if plan_until else None

async def set_plan(uid: int, plan: str, days: int):
    until = datetime.now() + timedelta(days=days)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE users SET plan=$1, plan_until=$2 WHERE user_id=$3
        """, plan, until, uid)

# ══════════════════════════════════════════════════════════════════════════════
#  ДНЕВНЫЕ ЛИМИТЫ
# ══════════════════════════════════════════════════════════════════════════════
async def check_day_limit(uid: int, req_per_day: int) -> tuple[bool, int]:
    today = datetime.now().date()
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT day_used, last_day FROM users WHERE user_id=$1", uid
        )
        if not row:
            return True, 0
        day_used = row["day_used"]
        last_day = row["last_day"]
        if last_day != today:
            await conn.execute(
                "UPDATE users SET day_used=0, last_day=$1 WHERE user_id=$2",
                today, uid
            )
            return True, 0
        return day_used < req_per_day, day_used

async def increment_day(uid: int):
    today = datetime.now().date()
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE users SET
                day_used = CASE WHEN last_day=$1 THEN day_used+1 ELSE 1 END,
                last_day = $1
            WHERE user_id=$2
        """, today, uid)

# ══════════════════════════════════════════════════════════════════════════════
#  СТАТИСТИКА (АДМИН)
# ══════════════════════════════════════════════════════════════════════════════
async def get_all_users() -> list:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT user_id, username, full_name, plan, plan_until FROM users"
        )
        return [tuple(r) for r in rows]

async def get_users_count() -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT COUNT(*) FROM users")

# ══════════════════════════════════════════════════════════════════════════════
#  КЕШ АНАЛИТИКИ (только Pro)
# ══════════════════════════════════════════════════════════════════════════════
async def init_cache_table():
    """Создаём таблицу кеша если не существует"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS match_cache (
                cache_key     TEXT PRIMARY KEY,
                response      TEXT NOT NULL,
                created_at    TIMESTAMP DEFAULT NOW(),
                last_accessed TIMESTAMP DEFAULT NOW()
            )
        """)

async def cache_get(cache_key: str) -> str | None:
    """Получить кешированный ответ и обновить last_accessed"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT response FROM match_cache WHERE cache_key=$1", cache_key
        )
        if row:
            await conn.execute(
                "UPDATE match_cache SET last_accessed=NOW() WHERE cache_key=$1",
                cache_key
            )
            return row["response"]
        return None

async def cache_set(cache_key: str, response: str):
    """Сохранить ответ в кеш"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO match_cache (cache_key, response, created_at, last_accessed)
            VALUES ($1, $2, NOW(), NOW())
            ON CONFLICT (cache_key) DO UPDATE
                SET response=EXCLUDED.response,
                    last_accessed=NOW()
        """, cache_key, response)

async def cache_cleanup(days: int = 7):
    """Удалить записи без обращений более N дней"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        deleted = await conn.fetchval("""
            DELETE FROM match_cache
            WHERE last_accessed < NOW() - INTERVAL '1 day' * $1
            RETURNING COUNT(*)
        """, days)
        return deleted or 0


# ══════════════════════════════════════════════════════════════════════════════
#  РЕФЕРАЛЬНАЯ СИСТЕМА
# ══════════════════════════════════════════════════════════════════════════════
async def get_ref_count(uid: int) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT ref_count FROM users WHERE user_id=$1", uid
        )
        return row["ref_count"] if row else 0

async def add_referral(uid: int, referred_by: int) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT referred_by FROM users WHERE user_id=$1", uid
        )
        if row and row["referred_by"] is None:
            await conn.execute(
                "UPDATE users SET referred_by=$1 WHERE user_id=$2",
                referred_by, uid
            )
            await conn.execute("""
                UPDATE users
                SET free_used = GREATEST(0, free_used - 3),
                    ref_count = ref_count + 1
                WHERE user_id=$1
            """, referred_by)
            return True
        return False
